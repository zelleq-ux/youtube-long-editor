"""
Etapa 2: Transcripción.

Usa faster-whisper (CTranslate2) sobre data/raw/<video_id>.mp4 para obtener
una transcripción con timestamps por palabra, y la guarda en
data/transcripts/<video_id>.json con el contrato:

    {
        "video_id": str,
        "language": str,                # idioma detectado o forzado por config
        "language_probability": float,  # confianza del detector de idioma
        "duration_s": float,            # duración del audio analizado (según faster-whisper)
        "words": [
            {"word": str, "start": float, "end": float, "probability": float},
            ...
        ],
        "segments": [
            {"id": int, "start": float, "end": float, "text": str},
            ...
        ],
    }

faster-whisper devuelve los segmentos como un generador: el modelo no carga
la transcripción completa en memoria de golpe, sino que la va produciendo
segmento a segmento a medida que se consume el generador (streaming). Para
grabaciones de 1-2h esto ya resuelve el problema de memoria que menciona la
versión anterior de este stub, sin necesidad de partir el audio en chunks
manualmente.

Por defecto se activa el VAD (Voice Activity Detection, Silero) de
faster-whisper (`config['transcribe']['vad_filter']`, True si la clave no
está presente en config): descarta tramos sin voz antes de transcribir, lo
que evita que Whisper "alucine" texto repetido/basura en los silencios
largos típicos de una grabación de 1-2h.
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from faster_whisper import WhisperModel

from src.common import db
from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

# Cada cuántos segmentos (o segundos de pared, lo que ocurra antes) se loguea
# el progreso. En grabaciones de 1-2h un proceso silencioso durante horas
# sería inaceptable, así que se reporta avance de forma periódica.
_PROGRESS_EVERY_SEGMENTS = 20
_PROGRESS_EVERY_SECONDS = 30.0


def _resolve_device(device_cfg: str) -> str:
    """
    Resuelve config['transcribe']['device'] ("auto", "cpu" o "cuda") a un
    valor concreto ("cpu" o "cuda").

    faster-whisper acepta "auto" directamente y lo resolvería igual
    internamente, pero aquí se hace explícito para poder loguear con
    claridad qué dispositivo se usa realmente y para elegir un compute_type
    sensato a partir de él (ver _resolve_compute_type).
    """
    device_cfg = (device_cfg or "auto").strip().lower()
    if device_cfg in ("cpu", "cuda"):
        return device_cfg
    if device_cfg != "auto":
        logger.warning("device=%r no reconocido en config; se trata como 'auto'.", device_cfg)

    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception as exc:  # noqa: BLE001 - queremos degradar a cpu ante cualquier fallo de detección
        logger.warning("No se pudo comprobar la disponibilidad de CUDA (%s); se usará cpu.", exc)
        return "cpu"
    return "cpu"


def _resolve_compute_type(device: str, transcribe_config: dict) -> str:
    """
    Elige compute_type para CTranslate2.

    config/settings.yaml no define hoy una clave para esto, así que se
    escoge un valor sensato según el dispositivo resuelto: float16 en GPU
    (cuda), int8 en CPU (más rápido y con footprint de memoria menor, al
    coste de algo de precisión). Si el usuario añade explícitamente
    config['transcribe']['compute_type'], se respeta ese valor.
    """
    override = transcribe_config.get("compute_type")
    if override:
        return override
    return "float16" if device == "cuda" else "int8"


def run(video_id: str, config: dict) -> dict:
    """
    Transcribe data/raw/<video_id>.mp4 con faster-whisper y guarda el
    resultado en data/transcripts/<video_id>.json.

    Args:
        video_id: identificador del vídeo; debe existir ya
            data/raw/<video_id>.mp4 (generado por la etapa de ingesta).
        config: dict cargado de config/settings.yaml. Se usan
            config['transcribe']['whisper_model'|'device'|'language'|'vad_filter']
            y, si está presente, config['transcribe']['compute_type']
            (no es una clave requerida ni definida hoy en settings.yaml).
            'vad_filter' se trata como True si la clave no está presente en
            config; un `false` explícito lo desactiva genuinamente.

    Returns:
        dict con:
            - "video_id": str
            - "transcript_path": str — ruta al JSON generado
            - "language": str — idioma detectado o forzado por config
            - "language_probability": float
            - "word_count": int
            - "segment_count": int
            - "duration_s": float — duración del audio procesado

    El JSON escrito en disco sigue el contrato {"words": [...], "segments":
    [...]} descrito en el docstring del módulo.
    """
    raw_dir = (REPO_ROOT / config["paths"]["raw"]).resolve()
    input_path = raw_dir / f"{video_id}.mp4"
    if not input_path.exists():
        raise FileNotFoundError(
            f"No existe el vídeo de entrada para '{video_id}': {input_path}. "
            "Ejecuta primero la etapa de ingesta "
            "(python -m src.ingest.run --file <ruta_al_mp4_de_obs>)."
        )

    transcribe_config = config.get("transcribe", {})
    model_size = transcribe_config.get("whisper_model", "medium")
    language = transcribe_config.get("language") or None

    device = _resolve_device(transcribe_config.get("device", "auto"))
    compute_type = _resolve_compute_type(device, transcribe_config)
    # bool(...) deliberado: un `false` explícito en YAML debe desactivar el
    # VAD de verdad, no solo la ausencia de la clave (que sí debe dejarlo
    # activado por defecto).
    vad_filter = bool(transcribe_config.get("vad_filter", True))
    logger.info(
        "Configuración de transcripción: modelo=%s device=%s compute_type=%s idioma=%s vad_filter=%s",
        model_size, device, compute_type, language or "auto-detección", vad_filter,
    )

    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    logger.info("Transcribiendo %s...", input_path)
    segments_iter, info = model.transcribe(
        str(input_path),
        language=language,
        word_timestamps=True,
        vad_filter=vad_filter,
    )

    words: list[dict] = []
    segments: list[dict] = []
    palabras_sin_timestamp = 0

    inicio = time.monotonic()
    ultimo_log = inicio

    for i, segment in enumerate(segments_iter, start=1):
        segments.append({
            "id": segment.id,
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

        # faster-whisper puede, en casos raros, devolver palabras sin
        # timestamp de inicio/fin (None). Escribir esos nulos tal cual
        # rompería a las etapas siguientes (detect_cuts asume floats), así
        # que se descartan defensivamente y se cuenta cuántas veces pasa.
        for word in segment.words or []:
            if word.start is None or word.end is None:
                palabras_sin_timestamp += 1
                continue
            words.append({
                "word": word.word,
                "start": word.start,
                "end": word.end,
                "probability": word.probability,
            })

        ahora = time.monotonic()
        if i % _PROGRESS_EVERY_SEGMENTS == 0 or (ahora - ultimo_log) >= _PROGRESS_EVERY_SECONDS:
            logger.info(
                "Progreso: %d segmentos procesados (última marca temporal: %.1fs de audio, "
                "%.1fs de proceso transcurridos)",
                i, segment.end, ahora - inicio,
            )
            ultimo_log = ahora

    if palabras_sin_timestamp:
        logger.warning(
            "%d palabra(s) sin timestamp de inicio/fin válido fueron descartadas.",
            palabras_sin_timestamp,
        )

    logger.info(
        "Transcripción completa: %d segmentos, %d palabras, idioma=%s (prob=%.2f), duración=%.2fs",
        len(segments), len(words), info.language, info.language_probability, info.duration,
    )

    transcripts_dir = (REPO_ROOT / config["paths"]["transcripts"]).resolve()
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    output_path = transcripts_dir / f"{video_id}.json"

    payload = {
        "video_id": video_id,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration_s": info.duration,
        "words": words,
        "segments": segments,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info("Transcripción guardada en %s", output_path)

    # Solo se marca como "transcribed" una vez el JSON está escrito con éxito.
    db.set_status(video_id, "transcribed")

    return {
        "video_id": video_id,
        "transcript_path": str(output_path),
        "language": info.language,
        "language_probability": info.language_probability,
        "word_count": len(words),
        "segment_count": len(segments),
        "duration_s": info.duration,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Transcribir vídeo ingerido")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    run(args.video_id, config)


if __name__ == "__main__":
    _cli()
