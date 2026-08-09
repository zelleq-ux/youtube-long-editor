"""
Utilidades de remapeo de línea de tiempo, compartidas entre edit/ y
detect_chapters/ (y cualquier otro módulo que necesite traducir un
timestamp del vídeo ORIGINAL a la línea de tiempo YA CORTADA).

Viven aquí, en vez de que un módulo importe esta lógica directamente de
otro, porque CLAUDE.md documenta como regla de arquitectura que "ningún
módulo importa lógica de negocio de otro directamente" -- src/common/ es
el sitio designado para utilidades compartidas. Movido desde
src/edit/run.py (donde se implementó y validó originalmente) al añadir
detect_chapters/run.py, que necesita el mismo remapeo para los timestamps
de los capítulos.
"""
from __future__ import annotations


def compute_keep_segments(cuts: list[dict], duration: float) -> list[tuple[float, float]]:
    """
    Complemento de los tramos marcados en cuts.json dentro de [0, duration]:
    los tramos que SÍ se conservan en el vídeo final, en orden.
    """
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for c in sorted(cuts, key=lambda c: c["start"]):
        start = max(0.0, min(float(c["start"]), duration))
        end = max(0.0, min(float(c["end"]), duration))
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def map_to_edited_timeline(t: float, sorted_cuts: list[dict]) -> float:
    """
    Convierte un timestamp del vídeo ORIGINAL a su equivalente en la línea
    de tiempo YA CORTADA, restando la duración acumulada de los cortes
    anteriores a t (o la porción de un corte que contenga a t) -- el mismo
    remapeo que CLAUDE.md documenta como necesario para detect_chapters y
    para el zoom hacia la webcam de edit/.

    `sorted_cuts` debe estar ordenado por "start".
    """
    removed = 0.0
    for c in sorted_cuts:
        if c["start"] >= t:
            break
        removed += min(float(c["end"]), t) - float(c["start"])
    return max(0.0, t - removed)
