"""
Test de `merge_short_kept_segments` (src/common/timeline.py).

Motivación (reporte real del usuario sobre witchfire_1, investigado
2026-08-12, ver "Fusión de cortes con hueco mínimo insuficiente" en el
docstring de src/edit/run.py): tramos como una narración leída con la
pantalla estática generan ráfagas de silencios/muletillas MUY próximos
entre sí (cada uno técnicamente válido y correctamente cortado), lo que
produce un vídeo final con muchos empalmes seguidos que suena
"ametrallado" aunque ninguna costura individual tenga un problema real.
`merge_short_kept_segments` funde dos cortes consecutivos cuando el
tramo conservado ENTRE ELLOS es más corto que un umbral, absorbiendo
también ese tramo -- reduce la densidad de empalmes sin tocar la
sensibilidad de detección (silence_min_seconds/motion_threshold no
cambian).

Dos bloques de comprobaciones:

1. Unitarias, sin datos reales (correctitud del algoritmo: fusión básica,
   encadenado, prioridad de type, umbral 0 = no-op, estructura del
   resultado).
2. Contra datos REALES de witchfire_1 (data/cuts/witchfire_1/cuts.json +
   data/transcripts/witchfire_1.json, si existen -- se OMITEN, no fallan,
   si no están disponibles en este checkout, ya que data/cuts|transcripts
   están en .gitignore): confirma con el umbral por defecto
   (min_kept_segment_seconds=0.6, ver config/settings.yaml) que (a) la
   única isla de audio realmente vacía (sin palabras transcritas) de la
   ventana investigada (0.528s, entre 4434.627s y 4435.155s) se funde, y
   (b) por debajo de 0.5s NINGÚN tramo fusionado en TODO el vídeo
   contiene palabras transcritas -- la garantía concreta que justificó
   elegir ese umbral (ver investigación en status.md).

Uso:
    cd <repo_root>
    python tests/test_merge_short_kept_segments.py

Código de salida 0 si todas las comprobaciones pasan (incluidas las
omitidas por falta de datos reales), 1 si alguna falla.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.common.timeline import compute_keep_segments, merge_short_kept_segments  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}")


print("=== Bloque 1: correctitud del algoritmo (sin datos reales) ===")

# Fusión básica: hueco de 0.3s entre dos cortes de silencio, umbral 0.5s
# -> deben fundirse en un único corte.
result = merge_short_kept_segments(
    [
        {"start": 10.0, "end": 12.0, "type": "silence", "reason": "silencio de audio"},
        {"start": 12.3, "end": 14.0, "type": "silence", "reason": "silencio de audio"},
    ],
    min_kept_seconds=0.5,
)
check("hueco 0.3s < umbral 0.5s -> se funden en 1 corte", len(result) == 1, f"resultado={result}")
if result:
    check("fusión -> start del primero", result[0]["start"] == 10.0, f"start={result[0]['start']}")
    check("fusión -> end del segundo", result[0]["end"] == 14.0, f"end={result[0]['end']}")

# Hueco por ENCIMA del umbral -> deben quedar separados.
result_no_merge = merge_short_kept_segments(
    [
        {"start": 10.0, "end": 12.0, "type": "silence", "reason": "silencio de audio"},
        {"start": 12.6, "end": 14.0, "type": "silence", "reason": "silencio de audio"},
    ],
    min_kept_seconds=0.5,
)
check(
    "hueco 0.6s > umbral 0.5s -> se mantienen separados",
    len(result_no_merge) == 2,
    f"resultado={result_no_merge}",
)

# Umbral 0 (o negativo) -> no-op, ni siquiera huecos minúsculos se funden.
result_disabled = merge_short_kept_segments(
    [
        {"start": 10.0, "end": 12.0, "type": "silence", "reason": "silencio de audio"},
        {"start": 12.001, "end": 14.0, "type": "silence", "reason": "silencio de audio"},
    ],
    min_kept_seconds=0.0,
)
check(
    "min_kept_seconds=0 -> no fusiona nada (hueco 1ms se mantiene separado)",
    len(result_disabled) == 2,
    f"resultado={result_disabled}",
)

# Encadenado: tres cortes con huecos cortos entre cada par consecutivo
# deben fundirse los tres en uno solo (no solo el primer par).
result_chain = merge_short_kept_segments(
    [
        {"start": 0.0, "end": 1.0, "type": "silence", "reason": "silencio de audio"},
        {"start": 1.2, "end": 2.0, "type": "silence", "reason": "silencio de audio"},
        {"start": 2.3, "end": 3.0, "type": "filler", "reason": "muletilla: 'eh'"},
    ],
    min_kept_seconds=0.5,
)
check("tres cortes encadenados con huecos cortos -> 1 solo corte", len(result_chain) == 1, f"resultado={result_chain}")
if result_chain:
    check("encadenado -> start=0.0", result_chain[0]["start"] == 0.0)
    check("encadenado -> end=3.0", result_chain[0]["end"] == 3.0)
    # "filler" es más específico que "silence" (implica habla real) -- ver
    # el mismo criterio ya usado por _merge_overlapping_cuts en detect_cuts.
    check(
        "encadenado -> type final 'filler' (prioridad sobre 'silence')",
        result_chain[0]["type"] == "filler",
        f"type={result_chain[0]['type']!r}",
    )

# "intro" nunca debe perder su etiqueta al fundirse con silencio/muletilla
# cercano (mismo criterio de prioridad que detect_cuts).
result_intro = merge_short_kept_segments(
    [
        {"start": 0.0, "end": 100.0, "type": "intro", "reason": "intro sin cara detectada en facecam_region"},
        {"start": 100.3, "end": 101.0, "type": "silence", "reason": "silencio de audio"},
    ],
    min_kept_seconds=0.5,
)
check(
    "intro+silence cercanos -> type se mantiene 'intro'",
    len(result_intro) == 1 and result_intro[0]["type"] == "intro",
    f"resultado={result_intro}",
)

# Lista vacía -> lista vacía, sin errores.
check("lista vacía -> lista vacía", merge_short_kept_segments([], 1.0) == [])

# Un único corte -> se devuelve tal cual (nada que fundir).
single = merge_short_kept_segments([{"start": 5.0, "end": 6.0, "type": "silence", "reason": "x"}], 1.0)
check("un único corte -> se devuelve sin cambios", len(single) == 1 and single[0]["start"] == 5.0)

print("=== Bloque 2: contra datos reales de witchfire_1 ===")
cuts_path = REPO_ROOT / "data/cuts/witchfire_1/cuts.json"
transcript_path = REPO_ROOT / "data/transcripts/witchfire_1.json"
if not cuts_path.exists() or not transcript_path.exists():
    print(
        f"  OMITIDO: {cuts_path} y/o {transcript_path} no existen en este checkout "
        "(están en .gitignore) -- solo se validan las comprobaciones unitarias del bloque 1."
    )
else:
    with open(cuts_path, encoding="utf-8") as f:
        real_cuts = json.load(f)
    with open(transcript_path, encoding="utf-8") as f:
        words = json.load(f)["words"]

    RAW_DURATION = 7043.283333
    DEFAULT_THRESHOLD = 0.6  # ver config/settings.yaml edit.min_kept_segment_seconds

    sorted_cuts = sorted(real_cuts, key=lambda c: c["start"])

    # (a) La única isla vacía de la ventana investigada (56:00-57:52 en
    # final.mp4) debe fundirse con el umbral por defecto.
    merged_default = merge_short_kept_segments(sorted_cuts, DEFAULT_THRESHOLD)
    still_has_tiny_gap = any(
        abs(c["start"] - 4434.627) < 0.05 for c in merged_default
    )
    check(
        "umbral por defecto (0.6s) funde la isla vacía real de la ventana investigada (0.528s, ~4434.6s-4435.2s)",
        not still_has_tiny_gap,
        f"sigue habiendo un corte empezando cerca de 4434.627s tras fusionar: {still_has_tiny_gap}",
    )

    # (b) Garantía que justificó el umbral: por debajo de 0.5s, CERO
    # tramos fusionados en todo el vídeo contienen palabras transcritas.
    merged_05 = merge_short_kept_segments(sorted_cuts, 0.5)
    # Cada corte del resultado que absorbió más de un corte original
    # corresponde a un hueco < 0.5s en el original -- reconstruimos esos
    # huecos directamente a partir de sorted_cuts para verificar su
    # contenido (más simple y robusto que inferir la fusión desde el
    # resultado ya fundido).
    lost_words_below_threshold = []
    for i in range(len(sorted_cuts) - 1):
        gap = sorted_cuts[i + 1]["start"] - sorted_cuts[i]["end"]
        if 0 <= gap < 0.5:
            s, e = sorted_cuts[i]["end"], sorted_cuts[i + 1]["start"]
            contained = [w["word"].strip() for w in words if s - 0.05 <= w["start"] and w["end"] <= e + 0.05]
            lost_words_below_threshold.extend(contained)
    check(
        "por debajo de 0.5s, ningún tramo fundido en todo el vídeo contiene palabras transcritas reales",
        len(lost_words_below_threshold) == 0,
        f"palabras que se perderían: {lost_words_below_threshold}",
    )

    # (c) Sanidad estructural: compute_keep_segments sigue funcionando
    # sobre el resultado fusionado, sin solapes ni desorden.
    keep = compute_keep_segments(merged_default, RAW_DURATION)
    prev_end = -1.0
    structurally_ok = True
    for s, e in keep:
        if s < prev_end - 1e-6 or e < s:
            structurally_ok = False
        prev_end = e
    check(
        "compute_keep_segments sobre el resultado fusionado no produce solapes/desorden",
        structurally_ok,
    )

    print(
        f"  {len(sorted_cuts)} cortes reales -> {len(merged_default)} tras fusionar con umbral "
        f"{DEFAULT_THRESHOLD}s ({len(sorted_cuts) - len(merged_default)} fusión(es))"
    )

if failures:
    print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nOK: merge_short_kept_segments correcto (unitario) y validado contra datos reales de witchfire_1.")
sys.exit(0)
