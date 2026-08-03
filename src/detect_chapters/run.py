"""
Etapa 4: Detección de capítulos.

Analiza la transcripción completa buscando cambios de tema/juego/sección,
usando Claude (config['detect_chapters']['claude_model']), y genera una
lista de capítulos con timestamp + título, respetando
config['detect_chapters']['min_chapter_seconds'] como duración mínima entre
capítulos consecutivos.

Guarda en data/chapters/<video_id>/chapters.json y también un
data/output/<video_id>/chapters.txt en formato listo para pegar en la
descripción de YouTube, p.ej.:

    00:00 Introducción
    04:32 Empieza la partida de Rust
    18:10 Evento random en el mapa
    ...

IMPORTANTE: los timestamps de los capítulos deben calcularse sobre el vídeo
YA EDITADO (después de aplicar los cortes de detect_cuts), no sobre el
original — si este módulo corre antes de edit/, hay que remapear los
timestamps restando la duración de los cortes previos a cada punto.
"""
from __future__ import annotations

import argparse


def run(video_id: str, config: dict) -> dict:
    """
    Returns:
        dict con {"video_id", "chapters_path", "chapters_txt_path",
                  "chapters": [{"timestamp_s": float, "title": str}, ...]}
    """
    raise NotImplementedError("TODO: llamar a Claude sobre la transcripción completa, segmentar por tema, remapear timestamps post-corte")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generar capítulos de un vídeo")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()
    raise NotImplementedError("TODO: cargar config y llamar a run(args.video_id, config)")


if __name__ == "__main__":
    _cli()
