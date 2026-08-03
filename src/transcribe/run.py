"""
Etapa 2: Transcripción.

Idéntico enfoque que en newclips-viral-pipeline/src/transcribe/run.py:
faster-whisper sobre data/raw/<video_id>.mp4, timestamps por palabra,
guardado en data/transcripts/<video_id>.json con formato
{words: [...], segments: [...]}.

Para vídeos de 1-2h, considera procesar por chunks si la memoria es un
problema — evaluar al implementar según el hardware real disponible.
"""
from __future__ import annotations

import argparse


def run(video_id: str, config: dict) -> dict:
    raise NotImplementedError("TODO: adaptar directamente desde newclips-viral-pipeline/src/transcribe/run.py")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Transcribir vídeo ingerido")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()
    raise NotImplementedError("TODO: cargar config y llamar a run(args.video_id, config)")


if __name__ == "__main__":
    _cli()
