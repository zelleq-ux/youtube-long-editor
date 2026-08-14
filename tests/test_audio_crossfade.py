"""
Test del micro-crossfade de audio en los empalmes de segmentos
(_decode_fragment_groups/_local_crossfade_concat/_write_crossfaded_audio/
_resample_to_length en src/edit/run.py) -- ver "Micro-crossfade en los
empalmes de audio" y "Reestirado LOCAL en vez de global" en el docstring
de ese módulo para el porqué completo.

Motivación (petición del usuario tras el diagnóstico de "microcortes" en
witchfire_1, 2026-08-12): aunque cada corte individual esté técnicamente
bien hecho (sin solape de audio, ver tests/test_audio_seam_overlap.py),
encadenar muchos empalmes muy próximos entre sí (una ráfaga de
silencios/muletillas cortos) suena "ametrallado" por la SEQUEDAD del
corte, no por ningún defecto de la costura en sí. Un crossfade
equal-power (curva coseno/seno) cortísimo (15-30ms) en cada unión suaviza
esa sensación sin cambiar qué se corta ni introducir una transición
perceptible como tal.

Cinco bloques de comprobaciones:

1. Correctitud matemática del crossfade (sintético, sin ffmpeg): sobre
   dos tonos de amplitud constante distinta, confirma que (a) el
   crossfade se corrige LOCALMENTE -- la longitud total del resultado es
   EXACTAMENTE la suma de las longitudes de entrada, sin ningún
   acortamiento neto (a diferencia del diseño anterior con reestirado
   GLOBAL, ver "Reestirado LOCAL en vez de global" en el docstring de
   src/edit/run.py), (b) el salto muestra-a-muestra máximo DENTRO de la
   ventana de crossfade es muchísimo menor que el salto que habría con un
   corte duro (el resultado objetivo y medible que pedía el usuario, ya
   que no se puede "escuchar" el resultado), y (c) _resample_to_length
   (la pieza que usa el reestirado local) corrige una longitud a un
   objetivo exacto preservando los extremos.

2. Contra fragmentos REALES cortados de data/raw/witchfire_1.mp4 (si
   existe -- se OMITE, no falla, si no está disponible en este checkout):
   corta con las funciones REALES de producción (_cut_segment_smart)
   varios tramos reales de habla continua (mezcla de interior-copiado y
   recodificación completa, igual que en producción) y mide, en cada
   punto de unión real, el salto de amplitud CON crossfade vs SIN
   crossfade sobre el MISMO contenido -- confirma que el crossfade reduce
   la discontinuidad de forma sustancial también en audio real (no solo
   en el tono sintético del bloque 1).

3. Regresión de `_decode_fragment_groups` (sintético, sin ffmpeg): sobre
   tres tramos con `real_boundary=[True, False]` (la primera frontera es
   un corte real, la segunda una frontera interna espuria de
   `_cut_segment_smart`, ver "Fronteras internas espurias del renderizado
   parcial sin pérdida" en el docstring de src/edit/run.py), confirma que
   la agrupación por `keep_segment` es correcta: el segundo y tercer
   fragmento (unidos por la frontera interna) se concatenan en SECO
   bit-exacto (sin ninguna mezcla) en un único grupo, y que un
   `real_boundary` de longitud incorrecta lanza `ValueError` en vez de
   fallar en silencio.

4. Regresión del reestirado LOCAL (sintético, sin ffmpeg) -- el caso que
   motivó el cambio de diseño (ver "Reestirado LOCAL en vez de global" en
   el docstring de src/edit/run.py): con tramos de duración MUY DESIGUAL
   (uno corto seguido de uno largo, varias fronteras reales), confirma
   que CADA tramo recupera su propia longitud EXACTA tras su crossfade
   (no una fracción proporcional a un reestirado global) -- comprobado
   verificando la posición exacta de un marcador dentro del tramo LARGO,
   que con el diseño anterior (reestirado global único) se habría movido
   una cantidad proporcional al acortamiento agregado de TODO el audio,
   no solo del propio tramo.

5. Regresión de las dos causas reales de desincronización encontradas
   tras el reestirado local (fragmentos REALES de witchfire_1, si existe
   -- se OMITE si no): mide la duración de cada keep_segment de tres
   formas (audio extraído; vídeo nominal `frames/fps` SIN unir; vídeo por
   PTS reales YA UNIDO) y confirma que las tres difieren entre sí de
   forma medible -- las dos discrepancias reales que motivaron este
   mecanismo (ver "Vídeo más largo que audio en cada recodificación" y
   "Redondeo del concat demuxer en el vídeo ya unido" en el docstring de
   src/edit/run.py) -- y que `_local_crossfade_concat`, alimentado con
   las duraciones por PTS real (el mecanismo actual), produce un
   resultado cuya duración total es exactamente esa suma.

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
    _count_video_frames,
    _CROSSFADE_SR,
    _cut_segment_smart,
    _decode_audio_float32,
    _decode_fragment_groups,
    _glue_video_files,
    _group_video_durations_from_pts,
    _local_crossfade_concat,
    _resample_to_length,
    _scan_keyframe_timestamps,
    _scan_video_pts,
    _video_info,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}")


def _workdir() -> Path:
    d = Path(tempfile.gettempdir()) / "yt_long_editor_crossfade_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _crossfade(paths: list[Path], crossfade_ms: float, real_boundary: list[bool]) -> np.ndarray:
    """
    Pipeline de producción con reestirado local, SIN vídeo real de por
    medio (para los bloques puramente sintéticos, que no tienen ningún
    fragmento de vídeo que medir): usa como target_lengths la propia
    longitud del audio de cada grupo -- válido para comprobar la mecánica
    del crossfade+reestirado local en sí, pero NO ejercita el fix de
    "Vídeo más largo que audio en cada recodificación" (ver Bloque 5, que
    sí mide vídeo real).
    """
    groups = _decode_fragment_groups(paths, real_boundary)
    return _local_crossfade_concat(groups, crossfade_ms, [len(g) for g in groups])


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

crossfaded = _crossfade([tone_a_path, tone_b_path], CROSSFADE_MS, [True])
expected_len = len(tone_a) + len(tone_b)  # SIN acortamiento neto -- ver Bloque 4/docstring del módulo
check(
    "longitud tras crossfade con reestirado local = EXACTAMENTE la suma de longitudes (sin acortar nada)",
    len(crossfaded) == expected_len,
    f"len={len(crossfaded)} esperado={expected_len}",
)

hard_cut = np.concatenate([tone_a, tone_b], axis=0)
seam_hard = len(tone_a)
hard_jump = abs(float(hard_cut[seam_hard, 0]) - float(hard_cut[seam_hard - 1, 0]))

# La mezcla equal-power queda dentro del primer tramo (tone_a), que no
# necesita ningún reestirado propio (ya mide exactamente len(tone_a) tras
# incorporar la mezcla) -- la costura sigue en la misma posición que con
# el diseño anterior.
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
target_length = len(crossfaded) + 500  # estirar un poco más, arbitrario
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
    # concatenado en producción) -- con el reestirado LOCAL, el resultado
    # debería quedar ya prácticamente en este objetivo sin necesitar
    # ningún reestirado global posterior (ver Bloque 1).
    durations = [len(_decode_audio_float32(p)) / _CROSSFADE_SR for p in all_fragments]
    target_duration_s = sum(durations)

    real_boundary_all = [True] * (len(all_fragments) - 1)
    crossfaded_real = _crossfade(all_fragments, CROSSFADE_MS, real_boundary_all)
    check(
        "audio real: con reestirado local, la duración YA coincide con el objetivo sin reestirado global adicional",
        abs(len(crossfaded_real) - round(target_duration_s * _CROSSFADE_SR)) <= 1,
        f"len={len(crossfaded_real)} objetivo={round(target_duration_s * _CROSSFADE_SR)}",
    )
    resampled_real = _resample_to_length(crossfaded_real, round(target_duration_s * _CROSSFADE_SR))

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
        hard_pair = _crossfade([frag_i, frag_j], 0.0, [True])
        soft_pair = _crossfade([frag_i, frag_j], CROSSFADE_MS, [True])

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
        "audio real: sin NaN/inf tras crossfade con reestirado local",
        bool(np.all(np.isfinite(resampled_real))),
    )
    check(
        "audio real: sin clipping fuera de rango introducido por el crossfade (±1.05 de margen)",
        bool(np.max(np.abs(resampled_real)) <= 1.05) if len(resampled_real) else True,
        f"max_abs={float(np.max(np.abs(resampled_real))) if len(resampled_real) else 0.0}",
    )

print("=== Bloque 3: _decode_fragment_groups agrupa por keep_segment correctamente ===")

tone_c = np.full((_CROSSFADE_SR, 2), 0.3, dtype=np.float32)
tone_c_path = work_dir / "tone_c.wav"
sf.write(str(tone_c_path), tone_c, _CROSSFADE_SR, subtype="FLOAT")

# tone_a -> [frontera REAL] -> tone_b -> [frontera INTERNA espuria] ->
# tone_c -- imita un keep_segment que empieza tras un corte real
# (tone_a->tone_b) y que _cut_segment_smart divide en head/mid
# (tone_b->tone_c) solo por la optimización de copia sin pérdida.
groups = _decode_fragment_groups([tone_a_path, tone_b_path, tone_c_path], [True, False])
check(
    "_decode_fragment_groups: 2 grupos (frontera real separa, interna no)",
    len(groups) == 2,
    f"n_groups={len(groups)}",
)
if len(groups) == 2:
    check(
        "_decode_fragment_groups: el primer grupo es tone_a sin modificar",
        np.array_equal(groups[0], tone_a),
    )
    check(
        "_decode_fragment_groups: el segundo grupo es tone_b+tone_c concatenados en SECO, bit-exacto "
        "(sin ninguna mezcla en la frontera interna)",
        np.array_equal(groups[1], np.concatenate([tone_b, tone_c], axis=0)),
    )

try:
    _decode_fragment_groups([tone_a_path, tone_b_path, tone_c_path], [True])
    check("real_boundary de longitud incorrecta lanza ValueError", False, "no se lanzó ninguna excepción")
except ValueError:
    check("real_boundary de longitud incorrecta lanza ValueError", True)

print("=== Bloque 4: reestirado LOCAL -- cada tramo recupera su propia longitud exacta ===")

# Tramo CORTO seguido de un tramo LARGO, ambos separados por fronteras
# REALES de otros tramos cortos -- el caso que expone la diferencia entre
# reestirado local (correcto) y global (el bug encontrado 2026-08-14, ver
# "Reestirado LOCAL en vez de global" en el docstring de src/edit/run.py):
# con un reestirado GLOBAL, el tramo largo se habría estirado por la
# MISMA tasa que el conjunto agregado (dominada por los tramos cortos,
# proporcionalmente muy afectados) en vez de por su propia tasa (mínima,
# al ser mucho más largo que crossfade_ms).
tone_short1 = np.full((int(0.6 * _CROSSFADE_SR), 2), 0.2, dtype=np.float32)  # 0.6s, min_kept_segment_seconds típico
tone_long = np.full((10 * _CROSSFADE_SR, 2), 0.6, dtype=np.float32)  # 10s
tone_short2 = np.full((int(0.6 * _CROSSFADE_SR), 2), 0.9, dtype=np.float32)  # 0.6s

paths4 = []
for name, tone in (("s1", tone_short1), ("long", tone_long), ("s2", tone_short2)):
    p = work_dir / f"tone_{name}.wav"
    sf.write(str(p), tone, _CROSSFADE_SR, subtype="FLOAT")
    paths4.append(p)

crossfaded4 = _crossfade(paths4, CROSSFADE_MS, [True, True])
expected_len4 = len(tone_short1) + len(tone_long) + len(tone_short2)
check(
    "reestirado local: longitud total = suma exacta (3 tramos, 2 fronteras reales)",
    len(crossfaded4) == expected_len4,
    f"len={len(crossfaded4)} esperado={expected_len4}",
)

# El tramo LARGO debería quedar prácticamente INTACTO (su propio
# acortamiento, 2*n_expected sobre 10s, es minúsculo -- ~0.4%) -- se
# comprueba que su longitud dentro de la salida es exactamente la
# esperada, verificando la posición de inicio de tone_short2 (el marcador
# de que el tramo largo terminó exactamente donde debía, sin arrastrar
# ningún estiramiento global adicional causado por los tramos cortos).
expected_short2_start = len(tone_short1) + len(tone_long)
check(
    "reestirado local: tone_short2 empieza EXACTAMENTE en su posición acumulada esperada "
    "(sin desplazamiento por un reestirado global dominado por los tramos cortos)",
    abs(float(crossfaded4[expected_short2_start, 0]) - float(tone_short2[0, 0])) < 0.05,
    f"valor_en_posicion_esperada={crossfaded4[expected_short2_start, 0]:.4f} "
    f"esperado(amplitud tone_short2)={tone_short2[0, 0]:.4f}",
)

print("=== Bloque 5: vídeo más largo que audio, y redondeo del concat demuxer (regresión, fragmentos reales) ===")
if not raw_path.exists():
    print(f"  OMITIDO: {raw_path} no existe en este checkout (data/raw/ está en .gitignore).")
else:
    # Reutiliza los mismos fragmentos reales del Bloque 2. Mide la
    # duración de cada keep_segment de TRES formas distintas para
    # confirmar las dos causas reales de desincronización encontradas
    # (ver "Vídeo más largo que audio en cada recodificación" y "Redondeo
    # del concat demuxer en el vídeo ya unido" en el docstring de
    # src/edit/run.py):
    #
    # 1. audio_durations_s: duración con la que se extrajo el AUDIO de
    #    cada fragmento (lo que usaba la primera versión del fix, antes
    #    de saber que vídeo y audio no siempre coinciden).
    # 2. video_durations_nominal_s: frames/fps de cada fragmento SIN
    #    unir (lo que usaba la segunda versión del fix -- exacta para
    #    cada fragmento por separado, pero no ve el redondeo del concat
    #    demuxer al unirlos).
    # 3. video_durations_pts_s: posición REAL en el vídeo YA UNIDO (el
    #    mecanismo actual, `_group_video_durations_from_pts`) -- la única
    #    de las tres que coincide con lo que de verdad se reproduce.
    frame_counts = [_count_video_frames(p) for p in all_fragments]
    audio_durations_s = [len(g) / _CROSSFADE_SR for g in _decode_fragment_groups(all_fragments, real_boundary_all)]
    video_durations_nominal_s = []
    cum = 0
    for i, fc in enumerate(frame_counts):
        cum += fc
        if i == len(frame_counts) - 1 or real_boundary_all[i]:
            video_durations_nominal_s.append(cum / FPS)
            cum = 0

    check(
        "vídeo real: hay al menos un keep_segment donde vídeo (nominal, sin unir) y audio difieren "
        "(confirma que el caso de prueba ejercita la discrepancia real, no uno degenerado)",
        any(abs(v - a) * 1000 > 1.0 for v, a in zip(video_durations_nominal_s, audio_durations_s)),
        f"video_s={[round(v, 4) for v in video_durations_nominal_s]} audio_s={[round(a, 4) for a in audio_durations_s]}",
    )
    print(f"  duración de vídeo (nominal, sin unir) vs audio por keep_segment (ms de diferencia): "
          f"{[round((v - a) * 1000, 2) for v, a in zip(video_durations_nominal_s, audio_durations_s)]}")

    glued_path = work_dir / "real_fragments_glued.mp4"
    _glue_video_files(all_fragments, glued_path)
    sorted_pts = _scan_video_pts(glued_path)
    total_duration = _video_info(glued_path)["duration"]
    video_durations_pts_s = _group_video_durations_from_pts(frame_counts, real_boundary_all, sorted_pts, total_duration)
    print(f"  duración de vídeo (PTS real, ya unido) por keep_segment (ms de diferencia vs. nominal): "
          f"{[round((p - n) * 1000, 2) for p, n in zip(video_durations_pts_s, video_durations_nominal_s)]}")
    check(
        "vídeo real: la suma de duraciones por PTS (ya unido) coincide con la duración total real "
        "del archivo unido, salvo el PTS del primer frame (residual minúsculo)",
        abs(sum(video_durations_pts_s) - (total_duration - sorted_pts[0])) < 1e-6,
        f"suma={sum(video_durations_pts_s):.6f} esperado={total_duration - sorted_pts[0]:.6f}",
    )
    glued_path.unlink(missing_ok=True)

    groups = _decode_fragment_groups(all_fragments, real_boundary_all)
    video_target_lengths = [round(d * _CROSSFADE_SR) for d in video_durations_pts_s]
    crossfaded_video_targeted = _local_crossfade_concat(groups, CROSSFADE_MS, video_target_lengths)
    check(
        "vídeo real: con target_lengths derivados de los PTS reales del vídeo ya unido, la duración "
        "total del resultado es EXACTAMENTE su suma",
        len(crossfaded_video_targeted) == sum(video_target_lengths),
        f"len={len(crossfaded_video_targeted)} esperado={sum(video_target_lengths)}",
    )

    try:
        _local_crossfade_concat(groups, CROSSFADE_MS, video_target_lengths[:-1])
        check("target_lengths de longitud incorrecta lanza ValueError", False, "no se lanzó ninguna excepción")
    except ValueError:
        check("target_lengths de longitud incorrecta lanza ValueError", True)

if failures:
    print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(
    "\nOK: crossfade equal-power con reestirado LOCAL correcto (sintético), validado contra fragmentos "
    "reales de witchfire_1, agrupación por keep_segment correcta, cada tramo recupera su propia "
    "longitud exacta independientemente de la duración de los demás, y el reestirado local usa la "
    "duración de VÍDEO real (no la de audio) como objetivo."
)
sys.exit(0)
