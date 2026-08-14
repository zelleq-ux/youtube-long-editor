"""
Test de escala del pipeline completo de src/edit/run.py
(apply_cuts_with_zoom + normalize_audio) contra un vídeo sintético de 1h a
60fps con ~400 cortes y ~50 tramos de zoom -- la misma escala usada para
validar el rediseño en dos pasos (shards) del 2026-08-05 (ver status.md),
pero con una comprobación EXPLÍCITA de monotonía/continuidad de PTS que
aquel test NO tenía.

Motivación (bug real encontrado en producción, 2026-08-08): una ejecución
completa contra dinoblade_1 (grabación real de 1h39m) produjo un
`final.mp4` que se congelaba en el reproductor alrededor del minuto 10:29
y saltaba al minuto ~67, sin audio a partir de ahí. El test de escala
anterior NO detectó nada porque solo comprobaba la DURACIÓN total del
vídeo de salida (que puede seguir siendo correcta incluso con timestamps
internos rotos: un salto de PTS hacia delante en mitad del archivo no
cambia cuántos frames hay en total, así que el contador de frames/duración
declarada en el contenedor puede seguir "cuadrando"). Solo una inspección
paquete a paquete (ffprobe) de PTS/DTS revela el problema.

Causa raíz encontrada (investigada en dos rondas, ver el docstring de
src/edit/run.py para el detalle completo): `_cut_video` particionaba los
tramos a conservar en "shards" (una llamada de ffmpeg por shard, con un
filtro `concat` interno) usando primero solo un presupuesto de CARACTERES
del filter_complex (`_MAX_FILTER_COMPLEX_CHARS`, 20000) -- con tramos
cortos esto permitía shards de ciento y pico de tramos concatenados en un
único filtro `concat`, lo que producía una discontinuidad de PTS a mitad
de la salida de ese shard. Un primer fix (limitar cada shard a 40 tramos
como máximo) se validó contra este mismo test sintético (640x360/60fps) y
parecía suficiente -- pero al revalidarlo contra `dinoblade_1` completo
(1920x1080 real) el problema SEGUÍA apareciendo con esos mismos 40 tramos:
aislando el shard se confirmó que el filtro `concat` de ffmpeg pierde
FRAMES DE VÍDEO (no solo "la cuenta del PTS") al reunir muchas ramas
trim+setpts, y es sensible también a la resolución -- 640x360 no
reproduce el fallo ni con 133 tramos, 1920x1080 sí con solo 40. Este test
sintético a baja resolución NO puede reproducir esa sensibilidad a
resolución, así que NO sirve para validar ningún límite de nº de
tramos/shard basado en `concat` -- la validación real de ese aspecto se
hizo directamente contra `dinoblade_1` (ver status.md).

Fix definitivo (no solo acotar el fan-in, sino evitarlo): `_cut_video` ya
no usa el filtro `concat` en absoluto para el corte -- cada tramo se
recorta a su propio archivo aislado (_cut_segment, un simple trim de
entrada sin filtros) y se pegan con el concat DEMUXER (_glue_video_files),
que opera a nivel de contenedor y no pasa por el filtro `concat`. Este
test sigue sirviendo como regresión de ese mecanismo (demuxer + tramos
aislados, con muchos cortes y tramos de zoom) aunque no pueda, por su
resolución reducida, reproducir por sí solo el bug original del filtro
`concat`.

Sincronización audio/vídeo en varios puntos (2026-08-14, ver "Fronteras
internas espurias del renderizado parcial sin pérdida" en el docstring de
src/edit/run.py): las comprobaciones de arriba (continuidad de PTS,
duración de vídeo vs. audio) son AGREGADAS -- pasan aunque haya
desincronismo LOCAL entre audio y vídeo, porque el reestiramiento final
de audio (`_resample_to_length`) corrige la duración TOTAL sin corregir
cómo se reparte ese ajuste por la línea de tiempo. Confirmado como falso
negativo real: el bug de desincronización del micro-crossfade de audio
(fronteras internas espurias del renderizado parcial sin pérdida tratadas
como cortes reales) pasaba este test sin ningún problema pese a producir
hasta ~1.5s de desfase local en un vídeo real. `check_av_sync` tapa ese
hueco: el vídeo sintético se genera con varios marcadores (flash de vídeo
+ beep de audio, embebidos en el MISMO instante exacto, con silencio/negro
de guarda a cada lado para un onset inequívoco) repartidos a
~5/25/50/75/95% de la duración, y tras correr el pipeline completo se
localiza el instante REAL de cada marcador en el vídeo de salida y en el
audio de salida por separado, comprobando que coinciden dentro de
_AV_SYNC_TOLERANCE_SECONDS -- exactamente lo que un desincronismo local sí
puede romper (ver el comentario junto a esa constante, y el apartado
siguiente, para por qué su valor no son unas pocas decenas de ms).

IMPORTANTE -- el patrón de cortes tiene que ser NO uniforme para que esto
funcione (encontrado validando el propio test, 2026-08-14): la primera
versión de `_generate_cuts` usaba un período FIJO (un corte cada 9.0s,
igual que el test de escala original de 2026-08-08). Con período fijo,
cada tramo conservado mide lo mismo y genera (si supera el intervalo de
keyframe) exactamente una frontera interna espuria cada N segundos de
forma perfectamente regular -- la densidad de "acortamiento de más" por
unidad de tiempo queda CONSTANTE en toda la línea de tiempo, así que el
reestiramiento global (uniforme por construcción) termina corrigiendo el
desincronismo LOCAL casi perfectamente también, por pura coincidencia
geométrica. Confirmado empíricamente revirtiendo aposta el fix de
"Fronteras internas espurias..." (`git stash` de src/edit/run.py) y
corriendo este mismo test: con período uniforme, `check_av_sync` pasaba
en verde IGUAL con el bug presente que arreglado (diffs de framerate,
unas pocas decenas de ms en ambos casos) -- un falso negativo real del
propio test, no hipotético. `_generate_cuts` se cambió a alternar bloques
de `_CUT_BLOCK_SECONDS` entre período denso (`_DENSE_CUT_PERIOD_SECONDS`)
y disperso (`_SPARSE_CUT_PERIOD_SECONDS`), imitando la irregularidad real
de un `cuts.json` (ráfagas de cortes seguidos en tramos de lectura/acción
rápida, huecos largos en tramos de habla continua) -- con este patrón,
revalidado que `check_av_sync` SÍ falla con el fix revertido (ver
status.md para las cifras exactas de esa revalidación) y pasa limpio con
el fix aplicado.

Uso:
    cd <repo_root>
    python tests/scale_test_edit_pipeline.py

Genera sus propios ficheros de trabajo bajo un directorio temporal del
sistema (no toca data/ ni ningún vídeo real) y los reutiliza entre
ejecuciones si ya existen (el vídeo sintético de 1h tarda un rato en
generarse). Termina con código de salida 0 si todas las comprobaciones
pasan, 1 si alguna falla.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.common.timeline import map_to_edited_timeline  # noqa: E402
from src.edit.run import apply_cuts_with_zoom, normalize_audio  # noqa: E402

# "_sync2" al final (2026-08-14): el vídeo sintético ahora lleva marcadores
# de sincronización embebidos (ver check_av_sync más abajo) Y un patrón de
# cortes NO uniforme (ver _generate_cuts) -- ninguno de los dos existía en
# las versiones cacheadas anteriores de este vídeo (ni siquiera en la
# primera versión de "_sync", ver el historial de este archivo) -- un id
# de vídeo distinto evita reutilizar por accidente un .mp4 cacheado
# desactualizado.
VIDEO_ID = "scale_test_1h_sync2"
DURATION_SECONDS = 3600
FPS = 60  # igual que las grabaciones reales del proyecto -- el bug es sensible a esto (ver docstring)
WIDTH, HEIGHT = 640, 360  # resolución reducida a propósito: solo por velocidad, el bug no depende de ella

# Patrón de cortes NO uniforme -- bloques de _CUT_BLOCK_SECONDS alternando
# entre densos (cortes cada _DENSE_CUT_PERIOD_SECONDS) y dispersos (cada
# _SPARSE_CUT_PERIOD_SECONDS). Por qué NO un período uniforme (como la
# primera versión de este test, período fijo de 9.0s -- ver "Sincronización
# audio/vídeo en varios puntos" en el docstring del módulo para la
# investigación completa): con período uniforme, cada tramo conservado
# mide lo mismo y genera (si supera el intervalo de keyframe) exactamente
# UNA frontera interna espuria cada N segundos de forma perfectamente
# regular -- la densidad de "acortamiento de más" por unidad de tiempo
# queda CONSTANTE en toda la línea de tiempo, así que el reestiramiento
# global (uniforme por construcción) termina corrigiendo el desincronismo
# LOCAL casi perfectamente también, por pura coincidencia geométrica, y el
# bug de fronteras internas tratadas como reales queda enmascarado incluso
# con el código SIN el fix -- confirmado empíricamente: la versión
# uniforme de este test pasaba en verde tanto con el código arreglado como
# con el código con el bug revertido a propósito. Alternar bloques densos
# (muchas fronteras espurias por segundo) y dispersos (pocas) rompe esa
# coincidencia -- imita la irregularidad real de cuts.json (ráfagas de
# cortes seguidos en tramos de lectura/acción rápida, huecos largos en
# tramos de habla continua), que es precisamente lo que hizo que el bug
# real produjera desincronismo LOCAL medible en shift_at_midnight_2 pese a
# que la duración total agregada siempre cuadraba.
_CUT_BLOCK_SECONDS = 600.0
_DENSE_CUT_PERIOD_SECONDS = 6.0
_SPARSE_CUT_PERIOD_SECONDS = 30.0
_CUT_DURATION_SECONDS = 0.8

# Cada cuántos segundos hay una tanda de "habla" (palabras cada 0.3s
# durante WORD_RUN_SECONDS, luego silencio) -- genera ~48 tramos de habla
# larga tras agrupar por hueco, mismo orden de magnitud que los 47 reales.
_WORD_RUN_SECONDS = 15.0
_WORD_GAP_SECONDS = 60.0

# Umbral (segundos) por encima del cual un hueco entre PTS consecutivos
# (ordenados) se considera una discontinuidad anómala, no jitter normal de
# reordenamiento por B-frames. A 60fps un frame dura ~0.0167s; cualquier
# hueco de más de 1s es varias decenas de frames y no tiene explicación
# normal en un vídeo CFR.
_PTS_GAP_ANOMALY_THRESHOLD_SECONDS = 1.0

# Diferencia máxima tolerada entre la duración del stream de vídeo y el de
# audio del archivo final -- más que esto indica que uno de los dos se
# cortó/truncó antes que el otro (el síntoma de "sin audio a partir de
# cierto punto" del bug real).
_MAX_AV_DURATION_DIFF_SECONDS = 2.0

# Marcadores de sincronización audio/vídeo (ver "Sincronización
# audio/vídeo en varios puntos" en el docstring del módulo), repartidos a
# ~5/25/50/75/95% de DURATION_SECONDS. La posición EXACTA de cada uno se
# calcula en tiempo de ejecución (_find_safe_marker_time) buscando el
# punto más cercano a cada fracción que quede lejos de cualquier corte y
# de cualquier tramo de habla larga/zoom -- con el patrón de cortes ya no
# uniforme (ver _generate_cuts) unas coordenadas fijas podrían aterrizar
# dentro de un corte real dependiendo de en qué bloque denso/disperso
# caigan, así que ya no tiene sentido fijarlas a mano.
_MARKER_ANCHOR_FRACTIONS = [0.05, 0.25, 0.50, 0.75, 0.95]
_MARKER_DURATION_SECONDS = 0.3
_MARKER_GUARD_SECONDS = 0.5  # negro/silencio total a cada lado del marcador
_MARKER_SAFETY_MARGIN_SECONDS = 1.5  # separación mínima exigida a cortes/tramos de zoom
_MARKER_SEARCH_MARGIN_SECONDS = 5.0  # ventana de búsqueda alrededor de la posición esperada
# Calibrado empíricamente (2026-08-14, ver "Sincronización audio/vídeo en
# varios puntos" y "Reestirado LOCAL en vez de global" en el docstring del
# módulo). Con el reestirado GLOBAL (primera versión del mecanismo, ya
# sustituido) este patrón de cortes deliberadamente denso/disperso dejaba
# hasta ~387ms de desfase local incluso con el código de clasificación de
# fronteras ya arreglado -- de ahí que la tolerancia tuviera que fijarse
# en 420ms, muy por encima del ruido de ASR habitual (50-100ms), solo para
# no dar falsos positivos con código correcto. Con el reestirado LOCAL
# (mecanismo actual, cada `keep_segment` recupera su propia longitud justo
# tras su crossfade en vez de repartir un único factor global) ese
# problema desaparece: los 5 marcadores de esta ejecución concreta miden
# entre -6.7ms y +68.3ms con el código correcto, así que la tolerancia
# puede volver a una escala de ruido real. 150ms dobla el máximo medido
# (68.3ms) para no ser frágil ante jitter de medición (granularidad de
# vídeo ~33ms/frame, ventanas de envolvente de audio de 5ms).
_AV_SYNC_TOLERANCE_SECONDS = 0.15


def _workdir() -> Path:
    d = Path(tempfile.gettempdir()) / "yt_long_editor_scale_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_marker_filters(marker_times: list[float]) -> tuple[str, str]:
    """
    Devuelve (video_filter, audio_volume_expr) para embeber los
    marcadores de sincronización en el vídeo sintético: un flash BLANCO a
    pantalla completa (vídeo) y el mismo tono base a amplitud máxima
    (audio) durante [t, t+_MARKER_DURATION_SECONDS] de cada marcador, con
    negro/silencio TOTAL durante _MARKER_GUARD_SECONDS a cada lado -- así
    el onset de ambos eventos es inequívoco (una transición brusca
    silencio/negro -> máxima amplitud/blanco) y no depende del contenido
    de fondo (test pattern / tono continuo) en ningún punto cercano.
    """
    marker_cond = "+".join(
        f"between(t,{t:.3f},{t + _MARKER_DURATION_SECONDS:.3f})" for t in marker_times
    )
    guard_cond = "+".join(
        f"between(t,{t - _MARKER_GUARD_SECONDS:.3f},{t + _MARKER_DURATION_SECONDS + _MARKER_GUARD_SECONDS:.3f})"
        for t in marker_times
    )
    # vídeo: negro durante el margen de guarda, blanco (encima) durante el
    # propio marcador -- el segundo drawbox se dibuja sobre el primero.
    video_filter = (
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='{guard_cond}',"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{marker_cond}'"
    )
    # audio: volumen normal (1) fuera de cualquier marcador/guarda, 0
    # (silencio) durante la guarda, 1 (máxima amplitud del tono base)
    # durante el propio marcador -- eval=frame re-evalúa por cada frame de
    # audio (~21ms a 48kHz), sobrada precisión para un margen de guarda de
    # medio segundo.
    audio_volume_expr = f"if({marker_cond},1,if({guard_cond},0,1))"
    return video_filter, audio_volume_expr


def _generate_synthetic_video(path: Path, marker_times: list[float]) -> None:
    if path.exists():
        print(f"Reutilizando vídeo sintético existente ({path})")
        return
    print(
        f"Generando vídeo sintético de {DURATION_SECONDS}s @ {FPS}fps ({WIDTH}x{HEIGHT}), "
        f"con {len(marker_times)} marcador(es) de sincronización..."
    )
    t0 = time.monotonic()
    video_filter, audio_volume_expr = _build_marker_filters(marker_times)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION_SECONDS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION_SECONDS}",
            "-filter_complex",
            f"[0:v]{video_filter}[vout];[1:a]volume=eval=frame:volume='{audio_volume_expr}'[aout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    print(f"  generado en {time.monotonic() - t0:.1f}s")


def _generate_cuts() -> list[dict]:
    """
    Alterna bloques de _CUT_BLOCK_SECONDS entre período denso y disperso
    (ver el comentario junto a _DENSE_CUT_PERIOD_SECONDS más arriba para
    el porqué de NO usar un período uniforme).
    """
    cuts = []
    t = 3.0
    while t < DURATION_SECONDS - 5:
        block_idx = int(t // _CUT_BLOCK_SECONDS)
        period = _DENSE_CUT_PERIOD_SECONDS if block_idx % 2 == 0 else _SPARSE_CUT_PERIOD_SECONDS
        cuts.append({
            "start": round(t, 3), "end": round(t + _CUT_DURATION_SECONDS, 3),
            "type": "silence", "reason": "scale_test sintético",
        })
        t += period
    return cuts


def _generate_transcript_words() -> list[dict]:
    words = []
    t = 5.0
    idx = 0
    while t < DURATION_SECONDS - 20:
        run_end = t + _WORD_RUN_SECONDS
        wt = t
        while wt < run_end:
            words.append({"word": f"w{idx}", "start": round(wt, 3), "end": round(wt + 0.25, 3)})
            wt += 0.3
            idx += 1
        t = run_end + _WORD_GAP_SECONDS
    return words


def _word_run_intervals() -> list[tuple[float, float]]:
    """
    Mismos tramos de habla (y por tanto de zoom hacia la webcam, ver
    detect_long_speech_segments en src/edit/run.py) que
    _generate_transcript_words, sin generar las palabras en sí -- usado
    solo por _find_safe_marker_time para mantener los marcadores de
    sincronización lejos de cualquier tramo de zoom.
    """
    runs: list[tuple[float, float]] = []
    t = 5.0
    while t < DURATION_SECONDS - 20:
        runs.append((t, t + _WORD_RUN_SECONDS))
        t += _WORD_RUN_SECONDS + _WORD_GAP_SECONDS
    return runs


def _is_marker_time_safe(mt: float, cuts: list[dict], word_runs: list[tuple[float, float]]) -> bool:
    """
    True si un marcador de sincronización en `mt` (extendido por su
    duración + el margen de guarda + _MARKER_SAFETY_MARGIN_SECONDS) NO se
    solapa con ningún corte sintético ni con ningún tramo de habla
    larga/zoom -- ninguno de los dos debe pisar la ventana del marcador,
    o check_av_sync daría un resultado sin sentido (marcador solapado con
    un corte real, o con una ventana de zoom que sí recorta/desplaza el
    frame).
    """
    lo = mt - _MARKER_GUARD_SECONDS - _MARKER_SAFETY_MARGIN_SECONDS
    hi = mt + _MARKER_DURATION_SECONDS + _MARKER_GUARD_SECONDS + _MARKER_SAFETY_MARGIN_SECONDS
    for c in cuts:
        if lo < c["end"] and hi > c["start"]:
            return False
    for run_start, run_end in word_runs:
        if lo < run_end and hi > run_start:
            return False
    return True


def _find_safe_marker_time(
    anchor_t: float, cuts: list[dict], word_runs: list[tuple[float, float]],
    search_radius: float = 60.0, step: float = 0.5,
) -> float:
    """
    Punto más cercano a `anchor_t` que satisface _is_marker_time_safe,
    buscando hacia afuera en pasos de `step` (primero hacia adelante,
    luego hacia atrás, alternando) hasta `search_radius`. Con el patrón de
    cortes ya no uniforme (ver _generate_cuts) unas coordenadas fijas de
    marcador podrían aterrizar dentro de un corte real según en qué bloque
    denso/disperso caiga cada fracción -- esta búsqueda sustituye la
    antigua lista de constantes fijas + una comprobación de aserción
    aparte.
    """
    if _is_marker_time_safe(anchor_t, cuts, word_runs):
        return anchor_t
    offset = step
    while offset <= search_radius:
        for candidate in (anchor_t + offset, anchor_t - offset):
            if 0.0 <= candidate <= DURATION_SECONDS and _is_marker_time_safe(candidate, cuts, word_runs):
                return candidate
        offset += step
    raise AssertionError(
        f"no se encontró ningún instante seguro para un marcador de sincronización cerca de "
        f"t={anchor_t}s (radio de búsqueda {search_radius}s agotado) -- revisar la densidad de "
        f"cortes/tramos de habla larga en esta zona del vídeo sintético"
    )


def _dump_packets(path: Path, select_stream: str) -> list[tuple[float, float]]:
    """[(pts_time, dts_time), ...] EN EL ORDEN EN QUE APARECEN EN EL ARCHIVO (orden de decodificación)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", select_stream,
        "-show_entries", "packet=pts_time,dts_time",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    rows: list[tuple[float, float]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(",")
        if len(parts) != 2:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return rows


def check_pts_continuity(path: Path, select_stream: str, label: str) -> list[str]:
    """
    Comprueba, para el stream `select_stream` (p.ej. "v:0" o "a:0") de
    `path`, que:

    1. El DTS (orden de decodificación) es monótono no decreciente en el
       orden en que los paquetes aparecen en el archivo -- una violación
       aquí es un stream objetivamente corrupto (no es un tema de
       jitter/reordenamiento, DTS SIEMPRE debe ser no decreciente).
    2. Ordenando los PTS de todos los paquetes, ningún hueco entre valores
       consecutivos supera _PTS_GAP_ANOMALY_THRESHOLD_SECONDS -- esto es
       lo que detecta el bug real (un salto de cientos/miles de segundos
       en mitad del archivo). Se usa el orden ORDENADO (no el de
       aparición) porque el reordenamiento normal por B-frames hace que,
       en el orden de aparición, el PTS "vaya y venga" unos pocos frames
       sin que eso sea un problema.

    Devuelve una lista de descripciones de problemas encontrados (vacía si
    todo está bien).
    """
    rows = _dump_packets(path, select_stream)
    problems: list[str] = []
    if not rows:
        return [f"[{label}] no se encontró ningún paquete"]

    dts_values = [d for _, d in rows]
    dts_violations = [
        (i, dts_values[i], dts_values[i + 1])
        for i in range(len(dts_values) - 1)
        if dts_values[i + 1] < dts_values[i]
    ]
    if dts_violations:
        first = dts_violations[0]
        problems.append(
            f"[{label}] DTS no monótono: {len(dts_violations)} violación(es), "
            f"primera en el paquete #{first[0]} ({first[1]:.3f}s -> {first[2]:.3f}s)"
        )

    pts_sorted = sorted(p for p, _ in rows)
    for i in range(len(pts_sorted) - 1):
        gap = pts_sorted[i + 1] - pts_sorted[i]
        if gap > _PTS_GAP_ANOMALY_THRESHOLD_SECONDS:
            problems.append(
                f"[{label}] salto anómalo de PTS: {gap:.3f}s entre {pts_sorted[i]:.3f}s y "
                f"{pts_sorted[i + 1]:.3f}s (umbral {_PTS_GAP_ANOMALY_THRESHOLD_SECONDS}s)"
            )

    return problems


def check_av_duration_consistency(path: Path) -> list[str]:
    """
    Comprueba que la duración del stream de vídeo y la del de audio de
    `path` no difieren en más de _MAX_AV_DURATION_DIFF_SECONDS -- detecta
    el síntoma de "un stream se corta antes que el otro" (en el bug real,
    el audio de final.mp4 terminaba a los 614s con un vídeo de 5533s).
    """
    problems: list[str] = []
    durations: dict[str, float | None] = {}
    for select_stream, key in (("v:0", "video"), ("a:0", "audio")):
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", select_stream,
            "-show_entries", "stream=duration", "-of", "csv=p=0",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        try:
            durations[key] = float(result.stdout.strip())
        except ValueError:
            durations[key] = None
            problems.append(f"no se pudo leer la duración del stream de {key}")

    if durations.get("video") is not None and durations.get("audio") is not None:
        diff = abs(durations["video"] - durations["audio"])
        if diff > _MAX_AV_DURATION_DIFF_SECONDS:
            problems.append(
                f"duración de vídeo ({durations['video']:.2f}s) y audio ({durations['audio']:.2f}s) "
                f"difieren {diff:.2f}s (> {_MAX_AV_DURATION_DIFF_SECONDS}s) -- posible truncamiento de un stream"
            )
    return problems


def _decode_audio_window(path: Path, start: float, duration: float, sr: int = 48000) -> np.ndarray:
    """Decodifica el audio MONO (float32) de [start, start+duration] de `path` -- una ventana pequeña, no el archivo completo."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{max(0.0, start):.6f}", "-t", f"{duration:.6f}",
        "-i", str(path),
        "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(sr), "-ac", "1",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype=np.float32)


def _decode_video_window_frames(path: Path, start: float, duration: float, width: int, height: int) -> np.ndarray:
    """Decodifica los frames RGB24 crudos de [start, start+duration] de `path` -- una ventana pequeña, no el vídeo completo."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{max(0.0, start):.6f}", "-t", f"{duration:.6f}",
        "-i", str(path),
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)
    frame_bytes = width * height * 3
    n_frames = len(result.stdout) // frame_bytes
    return np.frombuffer(result.stdout[: n_frames * frame_bytes], dtype=np.uint8).reshape(n_frames, height, width, 3)


_ENVELOPE_WINDOW_SAMPLES = 240  # ~5ms a 48kHz


def _low_then_high_onset(
    values: np.ndarray, low_frac: float, high_frac: float, lookback: int = 10
) -> int | None:
    """
    Primer índice i tal que values[i] >= high_frac*pico Y hay algún índice
    j < i, dentro de las últimas `lookback` posiciones (sin ningún
    high_frac de por medio desde entonces), con values[j] <= low_frac*pico
    -- localiza una transición de "silencio/negro de guarda" a "marcador a
    máxima amplitud/blanco", TOLERANDO una rampa corta de unas pocas
    posiciones entre ambos estados (no exige que sean estrictamente
    consecutivas).

    Por qué NO basta con "primer valor que cruza un umbral" (probado y
    descartado, ver el historial de este archivo): el tono de fondo fuera
    de la guarda suena exactamente a la MISMA amplitud que el propio
    marcador (ver _build_marker_filters -- ambos son "volumen=1", solo la
    guarda es 0), así que si la ventana de búsqueda ya "empieza caliente"
    (el fondo normal sonando desde la muestra 0, sin ningún silencio
    previo dentro de la ventana), un simple cruce de umbral detecta ESE
    instante en vez del marcador real -- exigir una transición baja->alta
    ignora cualquier tramo ya caliente al principio/final de la ventana.

    Por qué la adyacencia ESTRICTA (i-1 bajo, i alto) TAMPOCO basta
    (segundo bug real, encontrado 2026-08-14 validando el propio test
    contra el pipeline real con el patrón de cortes denso/disperso): el
    ataque real de un tono (silencio -> amplitud completa) no es
    instantáneo a nivel de envolvente RMS -- la ventana que CONTIENE el
    instante de transición mide una RMS INTERMEDIA (ni claramente baja ni
    claramente alta según los umbrales), así que la transición real ocupa
    DOS fronteras de ventana, no una -- con adyacencia estricta, NINGUNA
    de esas dos fronteras cumplía "bajo justo antes de alto", y el
    detector saltaba a una transición POSTERIOR y más "limpia" (por
    coincidencia, sin rampa intermedia) que no era el marcador real,
    dando un falso desfase de casi 1s. Tolerar una rampa corta
    (`lookback`, en unidades de ventana/frame) resuelve esto sin
    reintroducir el problema de "ventana ya caliente" de arriba (que
    exigía CERO estados bajos en absoluto, no una rampa corta).
    """
    peak = values.max()
    if peak <= 1e-6:
        return None
    low_thresh = low_frac * peak
    high_thresh = high_frac * peak
    last_low_idx: int | None = None
    for i, v in enumerate(values):
        if v <= low_thresh:
            last_low_idx = i
        elif v >= high_thresh:
            if last_low_idx is not None and (i - last_low_idx) <= lookback:
                return last_low_idx + 1
            last_low_idx = None
    return None


def _envelope_onset_time(audio: np.ndarray, sr: int) -> float | None:
    """
    Primer instante (segundos, relativo al inicio de `audio`) en el que la
    envolvente RMS (ventanas de _ENVELOPE_WINDOW_SAMPLES muestras) hace la
    transición silencio (<=10% del pico) -> tono a máxima amplitud (>=30%
    del pico) -- ver _low_then_high_onset. Precisión de unos pocos ms; el
    10%/30% (en vez de comparar contra 0/100% exactos) da margen al ruido
    de cuantización de fondo del AAC (el "silencio" de guarda, tras
    recodificar, no es perfectamente 0). None si la ventana es demasiado
    corta o no contiene ninguna transición de este tipo.
    """
    if len(audio) < _ENVELOPE_WINDOW_SAMPLES:
        return None
    n_windows = len(audio) // _ENVELOPE_WINDOW_SAMPLES
    trimmed = audio[: n_windows * _ENVELOPE_WINDOW_SAMPLES].astype(np.float64)
    rms = np.sqrt((trimmed.reshape(n_windows, _ENVELOPE_WINDOW_SAMPLES) ** 2).mean(axis=1))
    idx = _low_then_high_onset(rms, low_frac=0.1, high_frac=0.3)
    return None if idx is None else idx * _ENVELOPE_WINDOW_SAMPLES / sr


def _find_marker_audio_time(path: Path, expected_t: float) -> float | None:
    window_start = max(0.0, expected_t - _MARKER_SEARCH_MARGIN_SECONDS)
    window_dur = 2 * _MARKER_SEARCH_MARGIN_SECONDS + _MARKER_DURATION_SECONDS
    audio = _decode_audio_window(path, window_start, window_dur)
    onset = _envelope_onset_time(audio, 48000)
    return None if onset is None else window_start + onset


def _find_marker_video_time(path: Path, expected_t: float) -> float | None:
    """
    Primer instante (segundos, en la línea de tiempo de `path`) en el que
    la luminancia media del frame hace la transición negro (<=10% del
    pico) -> blanco (>=50% del pico) -- ver _low_then_high_onset;
    análogo a _envelope_onset_time pero en vídeo (localiza el onset del
    flash blanco de marcador, ignorando el contenido de fondo del test
    pattern ya presente al principio/final de la ventana de búsqueda).
    """
    window_start = max(0.0, expected_t - _MARKER_SEARCH_MARGIN_SECONDS)
    window_dur = 2 * _MARKER_SEARCH_MARGIN_SECONDS + _MARKER_DURATION_SECONDS
    frames = _decode_video_window_frames(path, window_start, window_dur, WIDTH, HEIGHT)
    if len(frames) == 0:
        return None
    brightness = frames.astype(np.float64).mean(axis=(1, 2, 3))
    idx = _low_then_high_onset(brightness, low_frac=0.1, high_frac=0.5)
    return None if idx is None else window_start + idx / FPS


def check_av_sync(final_path: Path, cuts: list[dict], marker_times: list[float]) -> list[str]:
    """
    Para cada marcador embebido en el vídeo sintético de entrada (ver
    _build_marker_filters), localiza en `final_path` -- ya cortado con
    micro-crossfade de audio, con zoom y con loudnorm aplicados -- el
    instante REAL en que aparece el flash de vídeo y el instante REAL en
    que aparece el beep de audio, y comprueba que ambos coinciden dentro
    de _AV_SYNC_TOLERANCE_SECONDS. Es la comprobación que el bug de
    desincronización del micro-crossfade de audio necesitaba y no tenía
    (ver "Sincronización audio/vídeo en varios puntos" en el docstring
    del módulo): una comprobación de duración/PTS AGREGADA (las de más
    arriba) pasa aunque haya desincronismo LOCAL.
    """
    problems: list[str] = []
    sorted_cuts = sorted(cuts, key=lambda c: c["start"])
    print("  desfase audio/vídeo por marcador (positivo = audio llega DESPUÉS que el vídeo):")
    for mt in marker_times:
        expected_t = map_to_edited_timeline(mt, sorted_cuts)
        video_t = _find_marker_video_time(final_path, expected_t)
        audio_t = _find_marker_audio_time(final_path, expected_t)
        if video_t is None or audio_t is None:
            problems.append(
                f"marcador original t={mt:.1f}s (esperado ~{expected_t:.2f}s editado): no se pudo "
                f"localizar en el resultado (vídeo={video_t}, audio={audio_t})"
            )
            continue
        diff = audio_t - video_t
        print(f"    t_original={mt:.1f}s -> vídeo={video_t:.3f}s audio={audio_t:.3f}s diff={diff * 1000:+.1f}ms")
        if abs(diff) > _AV_SYNC_TOLERANCE_SECONDS:
            problems.append(
                f"marcador original t={mt:.1f}s: desfase audio/vídeo de {diff * 1000:+.1f}ms "
                f"(> {_AV_SYNC_TOLERANCE_SECONDS * 1000:.0f}ms de tolerancia) -- vídeo={video_t:.3f}s "
                f"audio={audio_t:.3f}s"
            )
    return problems


def main() -> int:
    work_dir = _workdir()
    raw_dir = work_dir / "raw"
    transcripts_dir = work_dir / "transcripts"
    output_dir = work_dir / "output"
    for d in (raw_dir, transcripts_dir, output_dir):
        d.mkdir(parents=True, exist_ok=True)

    cuts = _generate_cuts()
    print(f"{len(cuts)} cortes sintéticos (patrón denso/disperso alternado)")

    word_runs = _word_run_intervals()
    marker_times = [
        _find_safe_marker_time(frac * DURATION_SECONDS, cuts, word_runs)
        for frac in _MARKER_ANCHOR_FRACTIONS
    ]
    print(f"marcadores de sincronización en: {[round(m, 1) for m in marker_times]}")

    input_path = raw_dir / f"{VIDEO_ID}.mp4"
    _generate_synthetic_video(input_path, marker_times)

    words = _generate_transcript_words()
    with open(transcripts_dir / f"{VIDEO_ID}.json", "w", encoding="utf-8") as f:
        json.dump({"words": words, "segments": []}, f)
    print(f"{len(words)} palabras sintéticas ({len(words) and 'generadas' or 'ninguna'})")

    config = {
        "paths": {
            "raw": str(raw_dir),
            "transcripts": str(transcripts_dir),
            "output": str(output_dir),
            "outro": str(work_dir / "no_outro.mp4"),  # no existe -> append_outro se omite
        },
        # Fracción 0.0-1.0 del frame (cambiado de píxeles absolutos el 2026-08-10,
        # ver src.common.face_detection) -- conversión exacta de x=15,y=60,w=200,h=120
        # sobre WIDTH=640,HEIGHT=360, para no cambiar el comportamiento de este test.
        "facecam_region": {"x": 15 / WIDTH, "y": 60 / HEIGHT, "w": 200 / WIDTH, "h": 120 / HEIGHT},
        "edit": {
            "long_speech_min_seconds": 10,
            "long_speech_gap_seconds": 1.2,
            "long_speech_zoom_factor": 1.13,
            "zoom_in_duration_seconds": 4.5,
            "loudnorm": True,
            "append_outro": False,
        },
    }

    print("\n=== apply_cuts_with_zoom (corte + zoom, pipeline real) ===")
    t0 = time.monotonic()
    clip_path = apply_cuts_with_zoom(VIDEO_ID, cuts, config)
    print(f"completado en {time.monotonic() - t0:.1f}s -> {clip_path}")

    print("\n=== normalize_audio (loudnorm, 2 pasadas) ===")
    t0 = time.monotonic()
    normalized_path = normalize_audio(clip_path, config)
    print(f"completado en {time.monotonic() - t0:.1f}s -> {normalized_path}")

    final_path = output_dir / f"{VIDEO_ID}_final.mp4"
    Path(normalized_path).replace(final_path)
    if Path(clip_path) != Path(normalized_path) and Path(clip_path).exists():
        Path(clip_path).unlink(missing_ok=True)

    print(f"\n=== Verificando {final_path} ===")
    problems: list[str] = []
    problems += check_pts_continuity(final_path, "v:0", "vídeo")
    problems += check_pts_continuity(final_path, "a:0", "audio")
    problems += check_av_duration_consistency(final_path)

    print("\n=== Verificando sincronización audio/vídeo en marcadores ===")
    problems += check_av_sync(final_path, cuts, marker_times)

    if problems:
        print(f"\nFALLO: {len(problems)} problema(s) encontrado(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        "\nOK: sin discontinuidades de PTS, sin desajuste de duración audio/vídeo, y sin desincronismo "
        f"local en ningún marcador (< {_AV_SYNC_TOLERANCE_SECONDS * 1000:.0f}ms)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
