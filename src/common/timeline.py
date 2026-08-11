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


_CUT_TYPE_PRIORITY = ["intro", "filler", "silence"]


def _cut_type_rank(cut_type: str) -> int:
    """
    Mismo criterio de prioridad que _merge_overlapping_cuts en
    detect_cuts/run.py (duplicado deliberadamente, no importado -- ver la
    regla de arquitectura de CLAUDE.md de que ningún módulo importa
    lógica de negocio de otro directamente): "intro" nunca debe quedar
    enmascarado por un corte de silencio/muletilla con el que se fusione,
    y entre "filler"/"silence" gana "filler" (una muletilla implica habla
    real, señal más específica que un silencio genérico).
    """
    try:
        return _CUT_TYPE_PRIORITY.index(cut_type)
    except ValueError:
        return len(_CUT_TYPE_PRIORITY)


def merge_short_kept_segments(cuts: list[dict], min_kept_seconds: float) -> list[dict]:
    """
    Funde cortes consecutivos cuando el tramo conservado ENTRE ELLOS
    (el hueco entre el final de uno y el principio del siguiente) es más
    corto que `min_kept_seconds`, absorbiendo también ese tramo en vez de
    dejarlo como una isla de audio casi imperceptible entre dos cortes.

    Por qué (bug real reportado en vídeos ya publicados, investigado
    2026-08-12 -- ver "Fusión de cortes con hueco mínimo insuficiente" en
    el docstring de edit/run.py para el análisis completo): cuando
    detect_cuts encuentra varios silencios/muletillas muy seguidos (p.ej.
    las pausas naturales de una narración leída con la pantalla estática,
    donde ninguna pausa individual es lo bastante larga para fundirse por
    solape), el vídeo final encadena muchos empalmes técnicamente
    correctos pero MUY próximos entre sí -- se percibe como "ametrallado"
    aunque ninguna costura individual tenga un problema real. Esta
    función NO cambia la sensibilidad de detección (silence_min_seconds,
    motion_threshold... siguen intactos, así que el ritmo de qué pausas
    se marcan para cortar no cambia) -- solo evita dejar fragmentos de
    audio tan cortos que no aportan nada perceptible y multiplican el
    número de empalmes sin necesidad.

    Generaliza `_merge_overlapping_cuts` de detect_cuts/run.py (que
    fusiona cortes que YA se solapan, hueco <= 0) con un hueco de
    tolerancia > 0: es la MISMA idea, aplicada un paso más allá.
    `min_kept_seconds` <= 0 no fusiona nada (se comporta igual que antes
    de esta función existir).

    Returns:
        Nueva lista de cortes en el mismo formato {"start", "end",
        "type", "reason"} (redondeado a 3 decimales, igual que
        detect_cuts), ordenada por "start". El "type"/"reason" de un
        corte fusionado sigue el mismo criterio de prioridad que
        detect_cuts (intro > filler > silence) cuando hay más de un tipo
        involucrado en la fusión.
    """
    if not cuts:
        return []
    ordered = sorted(cuts, key=lambda c: c["start"])
    merged = [dict(ordered[0])]
    for c in ordered[1:]:
        last = merged[-1]
        gap = c["start"] - last["end"]
        if gap < min_kept_seconds:
            last["end"] = max(last["end"], c["end"])
            if _cut_type_rank(c["type"]) < _cut_type_rank(last["type"]):
                last["type"] = c["type"]
            reasons = last["reason"].split("; ")
            if c["reason"] not in reasons:
                reasons.append(c["reason"])
            last["reason"] = "; ".join(reasons)
        else:
            merged.append(dict(c))
    return [
        {
            "start": round(c["start"], 3),
            "end": round(c["end"], 3),
            "type": c["type"],
            "reason": c["reason"],
        }
        for c in merged
    ]


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
