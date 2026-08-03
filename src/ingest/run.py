"""
Etapa 1: Ingesta.

A diferencia de newclips-viral-pipeline, aquí la fuente es SIEMPRE un
archivo local (la grabación horizontal de OBS), no una URL. Normaliza el
vídeo con ffmpeg y lo deja listo en data/raw/<video_id>.mp4.
"""
from __future__ import annotations

import argparse


def run(video_id: str, config: dict, *, local_path: str) -> dict:
    """
    Normaliza el vídeo local de OBS a mp4 en data/raw/<video_id>.mp4.

    Args:
        video_id: identificador único del vídeo dentro del pipeline.
        config: dict cargado de config/settings.yaml.
        local_path: ruta al archivo de grabación de OBS.

    Returns:
        dict con {"video_id": str, "raw_path": str, "duration_s": float}
    """
    raise NotImplementedError("TODO: normalizar con ffmpeg (mismo enfoque que newclips-viral-pipeline/src/ingest/run.py, sin la parte de yt-dlp)")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ingesta de grabación local de OBS")
    parser.add_argument("--file", required=True, help="Ruta al archivo de OBS")
    parser.add_argument("--video-id", help="Identificador a usar (por defecto, derivado del nombre de archivo)")
    args = parser.parse_args()
    raise NotImplementedError("TODO: cargar config, derivar video_id si no se pasa, llamar a run()")


if __name__ == "__main__":
    _cli()
