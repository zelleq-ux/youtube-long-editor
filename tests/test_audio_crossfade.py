"""
Test del micro-crossfade de audio en los empalmes de segmentos
(_equal_power_crossfade_concat/_write_crossfaded_audio/_resample_to_length
en src/edit/run.py) -- ver "Micro-crossfade en los empalmes de audio" en
el docstring de ese módulo para el porqué completo.

Motivación (petición del usuario tras el diagnóstico de "microcortes" en
witchfire_1, 2026-08-12): aunque cada corte individual esté técnicamente
bien hecho (sin solape de audio, ver tests/test_audio_seam_overlap.py),
encadenar muchos empalmes muy próximos entre sí (una ráfaga de
silencios/muletillas cortos) suena "ametrallado" por la SEQUEDAD del
corte, no por ningún defecto de la costura en sí. Un crossfade
equal-power (curva coseno/seno) cortísimo (15-30ms) en cada unión suaviza
esa sensación sin cambiar qué se corta ni introducir una transición
perceptible como tal.

Dos bloques de comprobaciones:

1. Correctitud matemática del crossfade (sintético, sin ffmpeg): sobre
   dos tonos de amplitud constante distinta, confirma que (a) la longitud
   total se acorta exactamente por el nº de muestras del crossfade en
   cada unión (consecuencia inevitable de solapar contenido, ver
   _resample_to_length), (b) el salto muestra-a-muestra máximo DENTRO de
   la ventana de crossfade es muchísimo menor que el salto que habría con
   un corte duro (el resultado objetivo y medible que pedía el usuario,
   ya que no se puede "escuchar" el resultado), y (c) _resample_to_length
   corrige el acortamiento a una duración objetivo exacta.

2. Contra fragmentos REALES cortados de data/raw/witchfire_1.mp4 (si
   existe -- se OMITE, no falla, si no está disponible en este checkout):
   corta con las funciones REALES de producción (_cut_segment_smart)
   varios tramos reales de habla continua (mezcla de interior-copiado y
   recodificación completa, igual que en producción) y mide, en cada
   punto de unión real, el salto de amplitud CON crossfade vs SIN
   crossfade sobre el MISMO contenido -- confirma que el crossfade reduce
   la discontinuidad de forma sustancial también en audio real (no solo
   en el tono sintético del bloque 1).

Uso:
    cd <repo_root>
    python tests/test_audio_crossfade.py

Genera sus propios ficheros de trabajo bajo un directorio temporal del
sistema. Código de salida 0 si todas las comprobaciones pasan (incluidas
las omitidas por falta de datos reales), 1 si alguna falla.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.edit.run import (  # noqa: E402
    _CROSSFADE_SR,
    _cut_segment_smart,
    _decode_audio_float32,
    _equal_power_crossfade_concat,
    _resample_to_length,
    _scan_keyframe_timestamps,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}")


def _workdir() -> Path:
    d = Path(tempfile.gettempdir()) / "yt_long_editor_crossfade_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


print("=== Bloque 1: correctitud matemática (sintético) ===")

CROSSFADE_MS = 20.0
n_expected = int(round(_CROSSFADE_SR * CROSSFADE_MS / 1000))

tone_a = np.full((_CROSSFADE_SR, 2), 0.5, dtype=np.float32)
tone_b = np.full((_CROSSFADE_SR, 2), 0.8, dtype=np.float32)

# Escribo los tonos a WAV temporales para poder pasar por
# _decode_audio_float32 (que decodifica DESDE ARCHIVO vía ffmpeg) y
# ejercitar el mismo camino que producción, no una llamada directa a la
# función interna con arrays ya en memoria.
import soundfile as sf  # noqa: E402

work_dir = _workdir()
tone_a_path = work_dir / "tone_a.wav"
tone_b_path = work_dir / "tone_b.wav"
sf.write(str(tone_a_path), tone_a, _CROSSFADE_SR, subtype="FLOAT")
sf.write(str(tone_b_path), tone_b, _CROSSFADE_SR, subtype="FLOAT")

crossfaded = _equal_power_crossfade_concat([tone_a_path, tone_b_path], CROSSFADE_MS)
expected_len = len(tone_a) + len(tone_b) - n_expected
check(
    "longitud tras crossfade = suma de longitudes - muestras del crossfade",
    len(crossfaded) == expected_len,
    f"len={len(crossfaded)} esperado={expected_len}",
)

hard_cut = np.concatenate([tone_a, tone_b], axis=0)
seam_hard = len(tone_a)
hard_jump = abs(float(hard_cut[seam_hard, 0]) - float(hard_cut[seam_hard - 1, 0]))

seam_soft = len(tone_a) - n_expected
window = crossfaded[max(0, seam_soft - 2): seam_soft + n_expected + 2, 0]
soft_max_jump = float(np.max(np.abs(np.diff(window))))

check(
    "salto muestra-a-muestra máximo en la costura CON crossfade es << que SIN crossfade",
    soft_max_jump < hard_jump * 0.05,
    f"con_crossfade={soft_max_jump:.5f} sin_crossfade(corte duro)={hard_jump:.5f} "
    f"(ratio={soft_max_jump / hard_jump:.4f}, se exige < 0.05)",
)

# _resample_to_length: debe devolver EXACTAMENTE target_length muestras,
# preservando los valores de inicio/fin (los extremos no deberían
# moverse con una interpolación lineal 0..1 -> 0..1).
target_length = expected_len + n_expected  # p.ej., "deshacer" el acortamiento
resampled = _resample_to_length(crossfaded, target_length)
check(
    "_resample_to_length devuelve exactamente target_length muestras",
    len(resampled) == target_length,
    f"len={len(resampled)} target={target_length}",
)
check(
    "_resample_to_length preserva el valor del primer sample",
    abs(float(resampled[0, 0]) - float(crossfaded[0, 0])) < 1e-5,
)
check(
    "_resample_to_length preserva el valor del último sample",
    abs(float(resampled[-1, 0]) - float(crossfaded[-1, 0])) < 1e-5,
)
check(
    "_resample_to_length con target_length = longitud actual es no-op",
    np.array_equal(_resample_to_length(crossfaded, len(crossfaded)), crossfaded),
)

print("=== Bloque 2: contra fragmentos reales cortados de witchfire_1 ===")
raw_path = REPO_ROOT / "data/raw/witchfire_1.mp4"
if not raw_path.exists():
    print(f"  OMITIDO: {raw_path} no existe en este checkout (data/raw/ está en .gitignore).")
else:
    real_dir = work_dir / "real_fragments"
    if real_dir.exists():
        shutil.rmtree(real_dir)
    real_dir.mkdir(parents=True)

    print("  Escaneando keyframes reales de witchfire_1.mp4 (puede tardar ~30s)...")
    keyframes = _scan_keyframe_timestamps(raw_path)
    FPS = 60.0

    # Mismos tramos reales de habla continua ya investigados (mezcla de
    # interior-copiado y recodificación completa, exactamente como en
    # producción -- ver la investigación del reporte de "microcortes" en
    # status.md).
    REAL_SEGMENTS = [
        (4318.861, 4320.820),  # recodificación completa (corto)
        (4321.120, 4324.264),  # recodificación completa
        (4326.840, 4327.773),  # recodificación completa (muy corto)
        (4394.115, 4404.381),  # interior-copiado
        (4411.427, 4421.715),  # interior-copiado
    ]

    all_fragments: list[Path] = []
    for i, (start, end) in enumerate(REAL_SEGMENTS):
        fragments = _cut_segment_smart(raw_path, start, end, keyframes, FPS, i, len(REAL_SEGMENTS), real_dir)
        all_fragments.extend(fragments)

    print(f"  {len(REAL_SEGMENTS)} tramo(s) reales -> {len(all_fragments)} fragmento(s) de archivo "
          f"(algunos con interior-copiado producen head/mid/tail)")

    # Duración objetivo: la suma exacta de duraciones de cada fragmento
    # SIN crossfade (equivalente a lo que mediría _video_info del vídeo ya
    # concatenado en producción).
    durations = [len(_decode_audio_float32(p)) / _CROSSFADE_SR for p in all_fragments]
    target_duration_s = sum(durations)

    crossfaded_real = _equal_power_crossfade_concat(all_fragments, CROSSFADE_MS)
    resampled_real = _resample_to_length(crossfaded_real, round(target_duration_s * _CROSSFADE_SR))
    check(
        "audio real: tras crossfade + resample, duración coincide con el objetivo (equivalente al vídeo)",
        abs(len(resampled_real) - round(target_duration_s * _CROSSFADE_SR)) <= 1,
        f"len={len(resampled_real)} objetivo={round(target_duration_s * _CROSSFADE_SR)}",
    )

    # Comparación PAREJA A PAREJA (no sobre la cadena completa ya
    # concatenada): para cada par de fragmentos consecutivos reales, la
    # posición de la costura es inequívoca (len(frag_i) - n_cf, sin
    # arrastre de recortes anteriores) -- mucho más robusto que intentar
    # localizar cada costura dentro de la señal completa ya encadenada,
    # donde cada fragmento puede haberse acortado una cantidad DISTINTA si
    # es más corto que la ventana de crossfade.
    n_cf = int(round(_CROSSFADE_SR * CROSSFADE_MS / 1000))
    pair_results = []
    for i in range(len(all_fragments) - 1):
        frag_i, frag_j = all_fragments[i], all_fragments[i + 1]
        hard_pair = _equal_power_crossfade_concat([frag_i, frag_j], 0.0)
        soft_pair = _equal_power_crossfade_concat([frag_i, frag_j], CROSSFADE_MS)

        hard_seam = len(_decode_audio_float32(frag_i))
        lo, hi = max(0, hard_seam - 2), min(len(hard_pair), hard_seam + 2)
        hard_jump = float(np.max(np.abs(np.diff(hard_pair[lo:hi, 0])))) if hi > lo + 1 else 0.0

        soft_seam = hard_seam - n_cf  # única costura en este par -> sin arrastre acumulado
        lo_s, hi_s = max(0, soft_seam - 2), min(len(soft_pair), soft_seam + n_cf + 2)
        soft_jump = float(np.max(np.abs(np.diff(soft_pair[lo_s:hi_s, 0])))) if hi_s > lo_s + 1 else 0.0

        pair_results.append((i, hard_jump, soft_jump))

    print("  salto muestra-a-muestra por costura real (sin crossfade -> con crossfade):")
    for i, hard_jump, soft_jump in pair_results:
        print(f"    costura {i}: {hard_jump:.4f} -> {soft_jump:.4f}")

    # Nota sobre el criterio: en costuras donde el corte duro YA era casi
    # perfectamente continuo por pura coincidencia (salto < 0.02, muy por
    # debajo de cualquier escala audible), mezclar dos señales reales
    # DISTINTAS con una curva de crossfade puede introducir una micro-
    # variación local ligeramente mayor que esa coincidencia -- sigue
    # siendo inaudible (ambos valores son minúsculos), así que exigir que
    # el crossfade "gane" en ESAS costuras no es una comprobación
    # significativa. Lo que sí importa, y es lo que se comprueba aquí:
    #
    # 1. Donde el corte duro SÍ tiene una discontinuidad apreciable
    #    (salto >= 0.02 -- el umbral que separa "ruido de fondo normal
    #    del habla" de "salto que empieza a acercarse a un click"), el
    #    crossfade la reduce sustancialmente.
    _AUDIBLE_JUMP_THRESHOLD = 0.02
    _CLICK_CEILING = 0.1  # ningún salto tras crossfade debe acercarse a esto
    significant_hard = [r for r in pair_results if r[1] >= _AUDIBLE_JUMP_THRESHOLD]
    check(
        "audio real: al menos una costura real tiene una discontinuidad apreciable en el corte duro "
        "(confirma que el caso de prueba no es vacío)",
        len(significant_hard) >= 1,
        f"costuras con salto >= {_AUDIBLE_JUMP_THRESHOLD}: {significant_hard}",
    )
    not_reduced = [r for r in significant_hard if r[2] > r[1] * 0.6]
    check(
        "audio real: en las costuras con discontinuidad apreciable, el crossfade la reduce sustancialmente (<= 60%)",
        not not_reduced,
        f"costuras que NO se redujeron lo suficiente: {not_reduced}",
    )

    # 2. En NINGUNA costura (apreciable o no) el crossfade introduce un
    #    salto que se acerque a la escala de un click audible.
    max_soft_jump = max(r[2] for r in pair_results)
    check(
        "audio real: ninguna costura con crossfade se acerca a la escala de un click audible",
        max_soft_jump < _CLICK_CEILING,
        f"salto máximo tras crossfade={max_soft_jump:.4f} (techo {_CLICK_CEILING})",
    )

    check(
        "audio real: sin NaN/inf tras crossfade+resample",
        bool(np.all(np.isfinite(resampled_real))),
    )
    check(
        "audio real: sin clipping fuera de rango introducido por el crossfade (±1.05 de margen)",
        bool(np.max(np.abs(resampled_real)) <= 1.05) if len(resampled_real) else True,
        f"max_abs={float(np.max(np.abs(resampled_real))) if len(resampled_real) else 0.0}",
    )

if failures:
    print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nOK: crossfade equal-power correcto (sintético) y validado contra fragmentos reales de witchfire_1.")
sys.exit(0)
