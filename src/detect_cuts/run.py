"""
Etapa 3: Detección de cortes.

Combina TRES señales para decidir qué tramos recortar:

1. Silencio de audio: energía por debajo de config['detect_cuts']['silence_db_threshold']
   durante al menos config['detect_cuts']['silence_min_seconds'].
2. Movimiento visual: optical flow (mismo enfoque que score_motion_segment en
   newclips-viral-pipeline/src/detect/run.py). Un tramo de silencio SOLO se
   marca para corte si el movimiento visual está también por debajo de
   config['detect_cuts']['motion_threshold'] (silencio + quietud). Silencio
   con movimiento alto (acción en pantalla sin hablar) NUNCA se corta.
3. Muletillas en la transcripción (config['detect_cuts']['filler_words']),
   pasadas por el mismo filtro de contexto visual antes de marcarse.

Guarda el resultado en data/cuts/<video_id>/cuts.json y loguea un resumen
(nº de cortes, duración total eliminada) antes de que edit/ los aplique.
"""
from __future__ import annotations

import argparse


def detect_silence_segments(video_id: str, config: dict) -> list[dict]:
    """Detecta tramos de silencio de audio. Returns [{"start", "end"}, ...]."""
    raise NotImplementedError("TODO: análisis de energía de audio (librosa), umbral silence_db_threshold")


def score_motion_segment(video_id: str, start: float, end: float, config: dict) -> float:
    """Reutilizar el enfoque de newclips-viral-pipeline/src/detect/run.py (optical flow)."""
    raise NotImplementedError("TODO: adaptar score_motion_segment desde el otro proyecto")


def detect_filler_segments(video_id: str, transcript: dict, config: dict) -> list[dict]:
    """Detecta muletillas en la transcripción. Returns [{"start", "end", "word"}, ...]."""
    raise NotImplementedError("TODO: buscar config['detect_cuts']['filler_words'] en transcript['words']")


def run(video_id: str, config: dict) -> dict:
    """
    Combina las tres señales, aplica el filtro de contexto visual, y
    produce la lista final de cortes.

    Returns:
        dict con {"video_id", "cuts_path", "cuts": [{"start", "end", "type", "reason"}, ...],
                  "total_cut_seconds": float}
    """
    raise NotImplementedError("TODO: orquestar las tres señales + filtro de movimiento, guardar JSON, loguear resumen")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Detectar cortes de un vídeo transcrito")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()
    raise NotImplementedError("TODO: cargar config y llamar a run(args.video_id, config)")


if __name__ == "__main__":
    _cli()
