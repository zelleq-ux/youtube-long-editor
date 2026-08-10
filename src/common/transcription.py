"""
Núcleo de transcripción con faster-whisper, compartido entre
src/transcribe/run.py (transcribe data/raw/<video_id>.mp4 completo y
guarda data/transcripts/<video_id>.json) y src/subtitles/run.py
(transcribe data/output/<video_id>/intro.mp4 bajo demanda, sin persistir
a disco -- ver docstring de subtitles/run.py sobre por qué).

Vive en src/common/ y no en src/transcribe/ porque CLAUDE.md documenta
como regla de arquitectura que "ningún módulo importa lógica de negocio
de otro directamente" -- src/common/ es el sitio designado para
utilidades compartidas (mismo motivo por el que map_to_edited_timeline
vive en src/common/timeline.py en vez de en edit/run.py, donde se
implementó originalmente).

Extraído de src/transcribe/run.py (2026-08-10) al añadir soporte de
intro: el comportamiento del modelo (resolución de device/compute_type,
VAD, descarte de palabras sin timestamp, logging de progreso) no cambia,
solo se mueve la parte que no depende de dónde vive el archivo de origen
ni de cómo se persiste el resultado.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from faster_whisper import WhisperModel

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


def transcribe_file(input_path: Path, config: dict, *, log_progress: bool = True) -> dict:
    """
    Transcribe `input_path` (cualquier archivo de audio/vídeo legible por
    ffmpeg) con faster-whisper, usando
    config['transcribe']['whisper_model'|'device'|'language'|'vad_filter']
    (y, si está presente, config['transcribe']['compute_type']) -- el mismo
    modelo/parámetros que usa la etapa de transcripción principal.

    `log_progress=False` silencia el log periódico de avance (pensado para
    clips cortos como un intro de 1-2 min, donde no aporta nada y solo
    ensucia el log de otro módulo que no es la etapa de transcripción).

    Returns:
        dict con {"language": str, "language_probability": float,
        "duration_s": float, "words": [...], "segments": [...]} -- mismo
        contrato que el payload de data/transcripts/<video_id>.json, sin
        el campo "video_id" (lo añade el llamador si lo necesita).
    """
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

        if log_progress:
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

    return {
        "language": info.language,
        "language_probability": info.language_probability,
        "duration_s": info.duration,
        "words": words,
        "segments": segments,
    }
