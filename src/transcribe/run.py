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

El núcleo de la transcripción (resolución de device/compute_type, VAD,
descarte de palabras sin timestamp, logging de progreso) vive en
src/common/transcription.py (transcribe_file) -- compartido con
src/subtitles/run.py, que lo usa para transcribir
data/output/<video_id>/intro.mp4 bajo demanda (ver CLAUDE.md, sección
"Intro grabado aparte"). Este módulo es la etapa "oficial" del pipeline:
resuelve la ruta de entrada estándar (data/raw/<video_id>.mp4) y persiste
el resultado en el contrato JSON de arriba.
"""
from __future__ import annotations

import argparse
import json
import logging

from src.common import db
from src.common.config import REPO_ROOT, load_config
from src.common.transcription import transcribe_file

logger = logging.getLogger(__name__)


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

    result = transcribe_file(input_path, config)

    transcripts_dir = (REPO_ROOT / config["paths"]["transcripts"]).resolve()
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    output_path = transcripts_dir / f"{video_id}.json"

    payload = {"video_id": video_id, **result}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info("Transcripción guardada en %s", output_path)

    # Solo se marca como "transcribed" una vez el JSON está escrito con éxito.
    db.set_status(video_id, "transcribed")

    return {
        "video_id": video_id,
        "transcript_path": str(output_path),
        "language": result["language"],
        "language_probability": result["language_probability"],
        "word_count": len(result["words"]),
        "segment_count": len(result["segments"]),
        "duration_s": result["duration_s"],
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
