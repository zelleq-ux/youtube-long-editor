"""
Test unitario (sin ffmpeg, sin vídeo) de `_merge_overlapping_cuts` en
src/detect_cuts/run.py.

Motivación (bug real encontrado en producción, 2026-08-08): al fusionar un
corte "intro" (el recorte automático de la intro sin cara,
ver detect_intro_face_cut) con un candidato "silence" solapado -- algo que
pasa casi siempre, porque la intro larga suele contener silencios de
verdad además de la propia ausencia de cara --, `_merge_overlapping_cuts`
sobrescribía el `type` del corte fusionado a "silence" incondicionalmente,
perdiendo la etiqueta "intro" (el corte en sí seguía siendo correcto -- el
`reason` conservaba el texto "intro sin cara detectada..." -- pero
`cuts.json` ya no permitía distinguir ese corte de un silencio normal).
Confirmado en `dinoblade_1` real: el corte 0.00s-1043.80s (intro) quedó
etiquetado `"type": "silence"` en vez de `"type": "intro"`.

Fix: `_CUT_TYPE_PRIORITY` (intro > filler > silence) sustituye la regla
anterior ("silence" siempre gana") por una prioridad explícita donde
"intro" nunca se sobrescribe.

Uso:
    cd <repo_root>
    python tests/test_merge_overlapping_cuts.py

Sin dependencias de vídeo/ffmpeg: termina en menos de un segundo. Código
de salida 0 si todas las comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.detect_cuts.run import _merge_overlapping_cuts  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}")


# Caso principal reportado por el usuario: "intro" solapando con "silence"
# -- el resultado debe seguir siendo "intro", no "silence".
result = _merge_overlapping_cuts([
    {"start": 0.0, "end": 1043.8, "type": "intro", "reason": "intro sin cara detectada en facecam_region"},
    {"start": 1000.0, "end": 1050.0, "type": "silence", "reason": "silencio de audio"},
])
check("intro+silence -> 1 tramo fusionado", len(result) == 1, f"se obtuvieron {len(result)}")
if result:
    check("intro+silence -> type se mantiene 'intro'", result[0]["type"] == "intro", f"type={result[0]['type']!r}")
    check(
        "intro+silence -> end se extiende al máximo",
        result[0]["end"] == 1050.0,
        f"end={result[0]['end']}",
    )
    check(
        "intro+silence -> reason conserva ambos motivos",
        "intro sin cara detectada en facecam_region" in result[0]["reason"]
        and "silencio de audio" in result[0]["reason"],
        f"reason={result[0]['reason']!r}",
    )

# Mismo caso pero con el orden de entrada invertido (silence antes que
# intro en la lista de entrada) -- _merge_overlapping_cuts ordena por
# `start`, así que el resultado no debería depender del orden de entrada.
result_reversed_input = _merge_overlapping_cuts([
    {"start": 1000.0, "end": 1050.0, "type": "silence", "reason": "silencio de audio"},
    {"start": 0.0, "end": 1043.8, "type": "intro", "reason": "intro sin cara detectada en facecam_region"},
])
check(
    "intro+silence (orden de entrada invertido) -> type sigue siendo 'intro'",
    len(result_reversed_input) == 1 and result_reversed_input[0]["type"] == "intro",
    f"resultado={result_reversed_input}",
)

# "intro" solapando con "filler" -- también debe ganar "intro".
result_intro_filler = _merge_overlapping_cuts([
    {"start": 0.0, "end": 500.0, "type": "intro", "reason": "intro sin cara detectada en facecam_region"},
    {"start": 400.0, "end": 410.0, "type": "filler", "reason": "muletilla: 'esto'"},
])
check(
    "intro+filler -> type se mantiene 'intro'",
    len(result_intro_filler) == 1 and result_intro_filler[0]["type"] == "intro",
    f"resultado={result_intro_filler}",
)

# "filler" solapando con "silence" -- "filler" debe ganar (más específico
# que silencio: implica habla real, no silencio de verdad).
result_filler_silence = _merge_overlapping_cuts([
    {"start": 10.0, "end": 12.0, "type": "filler", "reason": "muletilla: 'eh'"},
    {"start": 11.0, "end": 13.0, "type": "silence", "reason": "silencio de audio"},
])
check(
    "filler+silence -> type se mantiene 'filler'",
    len(result_filler_silence) == 1 and result_filler_silence[0]["type"] == "filler",
    f"resultado={result_filler_silence}",
)

# Tres tipos solapando en cadena (intro -> silence -> filler, cada uno
# solapando solo con el siguiente): el resultado final debe ser "intro"
# sin importar en qué orden se van fusionando.
result_chain = _merge_overlapping_cuts([
    {"start": 0.0, "end": 100.0, "type": "intro", "reason": "intro sin cara detectada en facecam_region"},
    {"start": 90.0, "end": 110.0, "type": "silence", "reason": "silencio de audio"},
    {"start": 105.0, "end": 115.0, "type": "filler", "reason": "muletilla: 'o sea'"},
])
check(
    "intro+silence+filler encadenados -> type final 'intro'",
    len(result_chain) == 1 and result_chain[0]["type"] == "intro",
    f"resultado={result_chain}",
)
if result_chain:
    check("intro+silence+filler -> end final 115.0", result_chain[0]["end"] == 115.0, f"end={result_chain[0]['end']}")

# Caso de control: cortes que NO se solapan deben seguir separados, cada
# uno con su propio type (ningún efecto colateral del cambio de prioridad).
result_disjoint = _merge_overlapping_cuts([
    {"start": 0.0, "end": 10.0, "type": "intro", "reason": "intro sin cara detectada en facecam_region"},
    {"start": 20.0, "end": 21.0, "type": "silence", "reason": "silencio de audio"},
    {"start": 30.0, "end": 31.0, "type": "filler", "reason": "muletilla: 'esto'"},
])
check(
    "cortes disjuntos -> se mantienen 3 tramos separados",
    len(result_disjoint) == 3,
    f"se obtuvieron {len(result_disjoint)}: {result_disjoint}",
)
if len(result_disjoint) == 3:
    check(
        "cortes disjuntos -> cada uno conserva su propio type",
        [c["type"] for c in result_disjoint] == ["intro", "silence", "filler"],
        f"types={[c['type'] for c in result_disjoint]}",
    )

if failures:
    print(f"FALLO: {len(failures)} comprobación(es) fallida(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("OK: _merge_overlapping_cuts prioriza 'intro' > 'filler' > 'silence' correctamente.")
sys.exit(0)
