"""
Etapa 5: Edición final.

Aplica los cortes de data/cuts/<video_id>/cuts.json sobre
data/raw/<video_id>.mp4:

1. Elimina cada tramo marcado (con el margen de seguridad
   config['detect_cuts']['cut_margin_seconds'] ya aplicado en detect_cuts).
2. En cada punto de corte, aplica un micro-zoom
   (config['edit']['cut_zoom_factor'], duración
   config['edit']['cut_zoom_duration_seconds']) para disimular el salto en
   vez de un jump-cut seco.
3. Normaliza el audio con ffmpeg loudnorm si config['edit']['loudnorm'].
4. Si config['edit']['append_outro'], concatena assets/outro/outro.mp4 al
   final.

Guarda en data/output/<video_id>/final.mp4.

Nota de orden: si detect_chapters ya generó timestamps sobre el vídeo
editado, este módulo debe correr ANTES de detect_chapters, o
detect_chapters debe recalcular sus timestamps a partir de los cortes
aplicados aquí — mantener consistencia, ver CLAUDE.md.
"""
from __future__ import annotations

import argparse


def apply_cuts_with_zoom(video_id: str, cuts: list[dict], config: dict) -> str:
    """Corta los tramos marcados y aplica micro-zoom en cada punto de corte."""
    raise NotImplementedError("TODO: ffmpeg (concat de segmentos válidos + zoom en los bordes de cada corte)")


def normalize_audio(clip_path: str, config: dict) -> str:
    """Aplica ffmpeg loudnorm."""
    raise NotImplementedError("TODO: ffmpeg -af loudnorm")


def append_outro(clip_path: str, config: dict) -> str:
    """Concatena assets/outro/outro.mp4 al final."""
    raise NotImplementedError("TODO: ffmpeg concat demuxer, validar misma resolución/codec que el clip principal")


def run(video_id: str, config: dict) -> dict:
    """
    Returns:
        dict con {"video_id", "output_path"}
    """
    raise NotImplementedError("TODO: orquestar apply_cuts_with_zoom, normalize_audio, append_outro")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Editar el vídeo final")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()
    raise NotImplementedError("TODO: cargar config y llamar a run(args.video_id, config)")


if __name__ == "__main__":
    _cli()
