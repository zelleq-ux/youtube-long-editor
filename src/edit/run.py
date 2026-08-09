"""
Etapa 5: Edición final.

Aplica los cortes de data/cuts/<video_id>/cuts.json sobre
data/raw/<video_id>.mp4:

1. Elimina cada tramo marcado (con el margen de seguridad
   config['detect_cuts']['cut_margin_seconds'] ya aplicado en detect_cuts).
2. Durante los tramos de habla continua de
   config['edit']['long_speech_min_seconds'] segundos o más (derivados de
   data/transcripts/<video_id>.json, agrupando palabras consecutivas cuyo
   hueco es menor que config['edit']['long_speech_gap_seconds']), aplica el
   zoom típico de streamer: sube lento y suave (curva coseno, sin saltos)
   desde 1.0 hasta config['edit']['long_speech_zoom_factor'] durante los
   primeros config['edit']['zoom_in_duration_seconds'] del tramo (por
   defecto 4.5s), dirigido hacia config['facecam_region'] (posición
   aproximada de la webcam sobre el frame original, compartida con
   detect_cuts); y CORTA
   SECO a 1.0 (salto instantáneo, sin transición de salida) exactamente en
   el instante en que se completa esa rampa — no se mantiene sostenido el
   resto del tramo de habla, ni el corte espera a que el tramo termine. Si
   el tramo de habla dura menos que zoom_in_duration_seconds no da tiempo
   a completar la rampa, así que no se aplica zoom en absoluto en ese
   tramo. NO hay zoom en los puntos de corte en sí.
3. Normaliza el audio con ffmpeg loudnorm si config['edit']['loudnorm'].
4. Si config['edit']['append_outro'], concatena assets/outro/outro.mp4 al
   final.

Guarda en data/output/<video_id>/final.mp4.

Nota de orden: si detect_chapters ya generó timestamps sobre el vídeo
editado, este módulo debe correr ANTES de detect_chapters, o
detect_chapters debe recalcular sus timestamps a partir de los cortes
aplicados aquí — mantener consistencia, ver CLAUDE.md.

Los tramos de habla continua se identifican sobre los timestamps del
transcript, que son del vídeo ORIGINAL (antes de cortar). Como el zoom se
aplica sobre el vídeo YA CORTADO, cada timestamp se remapea restando la
duración acumulada de los cortes anteriores — el mismo remapeo que
CLAUDE.md documenta como necesario para detect_chapters.

Rendimiento y arquitectura en dos pasos (rediseñado 2026-08-05): con
grabaciones largas y muchos cientos de cortes, un único filter_complex
combinando TODOS los trim/atrim de corte + la concat + el zoom (el diseño
original) genera una cadena de texto que puede superar el límite de
longitud de línea de comandos de Windows (~32767 caracteres vía
CreateProcess; WinError 206 "el nombre del archivo o la extensión es
demasiado largo") — confirmado con un caso real de 1h39m/181 cortes/47
tramos de zoom (filtro de 44051 caracteres). El build de ffmpeg usado en
desarrollo tampoco soporta -filter_complex_script ni el indirect @archivo
para -filter_complex (verificado: ambos devuelven "option not found"), así
que no hay forma de sacar el grafo de filtros de la línea de comandos —
hay que MANTENERLO ACOTADO en caracteres.

apply_cuts_with_zoom se divide por eso en dos pasos independientes:

1. _cut_video: recorta CADA tramo a conservar a su propio archivo aislado
   (_cut_segment: un simple trim de entrada -ss/-to antes de -i, preciso a
   nivel de frame al recodificar) — SIN ningún filtro `concat` ni
   `filter_complex` en absoluto para el corte (ver "Fuga de frames de
   vídeo en el filtro concat de ffmpeg" más abajo para el porqué de
   evitarlo por completo en vez de solo acotar su fan-in, que es lo que se
   intentó primero y no bastó). Los archivos resultantes se pegan después
   con el concat DEMUXER (_glue_video_files, sin inpoint/outpoint, solo
   `file '...'` + `-c copy`) — rápido y exacto porque no recorta dentro de
   ningún archivo, y nunca pasa por el filtro `concat`.

   IMPORTANTE — por qué NO se usa el concat demuxer con inpoint/outpoint
   directamente sobre el vídeo original (el enfoque obvio para "evitar
   filter_complex del todo" desde el principio): se probó empíricamente y
   el inpoint del concat demuxer, al no alinear con un keyframe, NO
   recorta con precisión de frame al re-codificar — salta al keyframe
   anterior e incluye de más TODOS los frames intermedios (hasta un GOP
   entero, ~0.8s en la prueba), exactamente lo que CLAUDE.md prohíbe
   (comerse habla/acción por un corte impreciso). El outpoint sí es exacto
   (solo hay que dejar de leer paquetes); el problema es específico del
   inpoint. Por eso cada tramo se corta con -ss/-to ANTES de -i
   directamente sobre el vídeo ORIGINAL (frame-accurate al recodificar,
   sin depender del demuxer para el recorte en sí), y el concat demuxer
   solo se usa para pegar tramos ya completos.

2. _apply_zoom: aplica el zoom hacia la webcam en un filter_complex
   APARTE, sobre el vídeo YA CORTADO por _cut_video — mucho más corto que
   el combinado anterior porque solo cubre los tramos de habla larga (p.ej.
   47), no los cientos de cortes. El audio no pasa por este filtro (el
   zoom es solo de vídeo) y se copia sin re-codificar. Si aun así hicieran
   falta más tramos de zoom de los que caben en un único filter_complex,
   se encadenan varias pasadas (partición por presupuesto de caracteres,
   ver _plan_zoom_passes), cada una sobre la salida de la anterior.

_cut_video no tiene límite práctico de nº de tramos: cada uno es una
llamada de ffmpeg independiente, corta y sin filter_complex, así que ni el
nº de tramos ni sus caracteres pueden acercar la línea de comandos al
límite de Windows. _apply_zoom sí sigue particionando por presupuesto de
caracteres (_MAX_FILTER_COMPLEX_CHARS) porque su filter_complex crece con
el nº de tramos de zoom — a la escala real del proyecto (decenas) esto
siempre cabe en una única pasada.

Fuga de frames de vídeo en el filtro concat de ffmpeg (encontrada en
producción, 2026-08-08, investigada en dos rondas):

Primera ronda: una ejecución real contra `dinoblade_1` (diseño con shards
de corte particionados SOLO por presupuesto de caracteres, sin límite de
nº de tramos) produjo un `final.mp4` que se congelaba en el reproductor
sobre el minuto 10:29 y saltaba al minuto ~67, sin audio a partir de ahí.
`ffprobe` reveló un salto de PTS de ~3400s en mitad del vídeo, aterrizando
casi exactamente en la duración TOTAL del shard que lo contenía (133
tramos concatenados de un tirón en un único filter_complex). Reproducido
con un test sintético de 1h/400 cortes a 60fps
(tests/scale_test_edit_pipeline.py) pero NO con <=37 tramos de footage
real de dinoblade_1 (1080p60) ni con 133 tramos sintéticos a 30fps/640x360
— se interpretó (de forma incompleta, ver la segunda ronda) como sensible
a fan-in grande + 60fps, y el fix aplicado entonces fue limitar cada shard
a 40 tramos como máximo (_MAX_CONCAT_SEGMENTS_PER_SHARD, muy por debajo
del punto de fallo sintético observado).

Segunda ronda: al revalidar ese fix contra `dinoblade_1` completo (ya con
el límite de 40 tramos/shard), `ffprobe` sobre el `final.mp4` resultante
SEGUÍA mostrando 2 discontinuidades de PTS de vídeo (saltos de 25.6s y
519.5s) — el límite de 40 NO bastaba a la resolución real (1920x1080),
aunque sí bastaba en el test sintético a 640x360. Aislando el shard
problemático (los mismos 40 tramos y el mismo código de producción, sin
el resto del pipeline alrededor) se encontró la causa real: el archivo de
ESE SHARD, por sí solo, tenía 1077.5s de vídeo pero 1587.8s de audio — el
filtro `concat` de ffmpeg estaba perdiendo ~510s de FRAMES DE VÍDEO
silenciosamente dentro de su propio filter_complex (la pista de audio,
con el mismo fan-in, no se veía afectada en absoluto). Es decir: no es
(solo) "el concat pierde la cuenta del PTS acumulado" como se pensó en la
primera ronda, sino un fallo del propio filtro `concat` de ffmpeg al
reunir muchas ramas de vídeo trim+setpts, aparentemente sensible también a
la resolución (1080p reproduce el fallo con 40 tramos, algo que 640x360 no
reproducía ni con 133) — por lo que NINGÚN presupuesto de nº de tramos por
shard es una cota fiable mientras se siga usando el filtro `concat` para
el corte.

Fix definitivo: eliminar el filtro `concat` del paso de CORTE por
completo (no acotar su fan-in — evitarlo). _cut_video ya no arma shards
con filter_complex; corta cada tramo a un archivo aislado con un simple
trim de entrada (_cut_segment, sin ningún filtro) y los pega con el
concat DEMUXER (_glue_video_files) — el mismo mecanismo ya usado y
validado para pegar shards entre sí y para la vía rápida de append_outro,
que opera a nivel de contenedor (paquetes ya completos) y no pasa por el
filtro `concat`. Revalidado contra `dinoblade_1` completo tras el
rediseño: 0 discontinuidades de PTS en todo el archivo (ver status.md
para el detalle). La ruta del zoom (_apply_zoom) nunca usó `concat` (solo
scale/crop condicionados por `between()`) y no mostró el problema en
ningún test, así que no necesitaba ningún cambio.

Renderizado parcial sin pérdida (smart cut, 2026-08-09, idea tomada de
auto-editor de WyattBlue — investigado primero con un prototipo aislado
antes de tocar este módulo, ver status.md para el detalle de esa
investigación):

Cada tramo a conservar se recodificaba ENTERO a crf16/veryfast en
_cut_segment, aunque la inmensa mayoría de su duración no toca ningún
punto de corte — solo sus dos extremos importan para que el corte sea
preciso a nivel de frame. Midiendo contra los `cuts.json` reales de
`dinoblade_1` (147 cortes) e `icarus_1` (549 cortes, mucha más densidad
de cortes) qué fracción de cada tramo a conservar cae entre dos
keyframes reales del vídeo de entrada: 91.2% y 76.0% respectivamente
podría copiarse sin recodificar en vez de recodificarse — la fracción no
copiable se concentra en los tramos más cortos que un intervalo de
keyframe (frecuentes en `icarus_1`), no en el grueso de la duración.

Mecanismo (`_cut_segment_smart`, sustituye al antiguo `_cut_segment` que
solo recodificaba el tramo completo): antes de cortar, `_cut_video`
escanea UNA VEZ los timestamps de todos los keyframes del vídeo de
entrada (`_scan_keyframe_timestamps`, vía `ffprobe -show_entries
packet=pts_time,flags` — solo demuxea paquetes, no decodifica, así que es
barato incluso en grabaciones de 1-2h: ~15-35s medido contra
`dinoblade_1`/`icarus_1` reales, despreciable frente a los 25-55 min del
pipeline completo). Por cada tramo [start, end] a conservar:

1. Busca (bisección sobre la lista ordenada de keyframes) el primer
   keyframe >= start (`kf_start`) y el último keyframe <= end (`kf_end`).
2. Si `kf_end > kf_start` (hay un hueco interior real entre ambos):
   recodifica solo la cabeza [start, kf_start) y la cola [kf_end, end)
   (si no están vacías — con `start`/`end` ya alineados a keyframe no
   haría falta ninguna de las dos) igual que antes (crf16/veryfast), y
   copia SIN RECODIFICAR (`-c copy`) el interior [kf_start, kf_end) —
   válido porque `-ss`/`-to` antes de `-i` con estos timestamps caen
   EXACTAMENTE en keyframes reales, así que ffmpeg no necesita
   redondear al keyframe anterior para arrancar la copia (ver más abajo
   por qué esto SÍ es distinto del problema ya documentado del inpoint
   del concat demuxer).
3. Si no hay hueco útil (tramo más corto que un intervalo de keyframe):
   recodifica el tramo completo, exactamente el comportamiento de
   siempre — fallback sin pérdida de precisión ni de robustez.

Todos los fragmentos resultantes (1 a 3 por tramo) se pegan con el MISMO
concat DEMUXER (`_glue_video_files`) ya usado para pegar tramos
completos — no hace falta ningún mecanismo nuevo de unión.

Por qué esto NO es el mismo problema que el inpoint impreciso del concat
demuxer (documentado más arriba, el motivo por el que cada tramo se
corta con `-ss`/`-to` antes de `-i` en vez de con el demuxer): aquel
problema aparecía porque el inpoint NO coincidía con un keyframe real
(caía a mitad de GOP) y el demuxer redondeaba hacia atrás incluyendo de
más. Aquí `kf_start`/`kf_end` SON keyframes reales (leídos directamente
del vídeo, no arbitrarios), así que no hay nada que redondear — `-ss` cae
justo donde ya hay un keyframe. Verificado explícitamente con un test
sintético (ver tests/test_smart_cut_segments.py): el contenido copiado es
bit-idéntico (hash del frame decodificado) al del vídeo de origen en el
mismo instante.

Efecto secundario menor aceptado: dividir un tramo en cabeza/interior/
cola añade puntos de corte extra, y `-ss`/`-to` redondea cada uno a favor
de conservar un poco más de contenido, nunca menos (mismo tipo de
redondeo ya aceptado en producción — ver los ~2.9s de sobra en los 182
tramos de `dinoblade_1` más arriba) — esto escala con el Nº DE TRAMOS QUE
SE DIVIDEN, no con la duración total, así que es irrelevante en la
práctica (unas pocas fracciones de segundo repartidas en todo el vídeo).

Color range / pix_fmt: los `raw.mp4` reales de este proyecto son
`yuvj420p` (rango de color completo, propagado desde la fuente aunque
`ingest/run.py` solo pide `-pix_fmt yuv420p` sin más). Comprobado
explícitamente que recodificar cabeza/cola con los mismos flags de
siempre (sin ningún `-color_range` adicional) conserva ese mismo rango
completo automáticamente — no hay salto de niveles de negro/blanco en la
costura entre un fragmento copiado y uno recodificado.

Validado con un test sintético dedicado (tests/test_smart_cut_segments.py,
mismo patrón que scale_test_edit_pipeline.py: genera su propio vídeo,
sin tocar data/) que verifica bit-identidad del interior copiado,
continuidad de PTS/DTS (reutilizando check_pts_continuity/
check_av_duration_consistency) y consistencia de color_range/pix_fmt; y
contra un vídeo real (ver status.md para la medición de tiempo real
antes/después del paso de corte).
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from src.common import db
from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

# Objetivos de sonoridad para el paso de loudnorm (dos pasadas: medición +
# aplicación). -14 LUFS integrado / -1.5 dBTP de pico real / 11 LU de rango
# es un objetivo habitual para contenido hablado pensado para YouTube; no
# es una clave de config porque la tarea solo pide activar/desactivar
# loudnorm, no parametrizar el objetivo.
_LOUDNORM_TARGET_I = -14.0
_LOUDNORM_TARGET_TP = -1.5
_LOUDNORM_TARGET_LRA = 11.0

# Presupuesto de caracteres por filter_complex del paso de ZOOM (ver
# _plan_zoom_passes -- el corte ya no usa filter_complex en absoluto, ver
# "Fuga de frames de vídeo en el filtro concat de ffmpeg" en el docstring
# del módulo): Windows limita la línea de comandos de un proceso a ~32767
# caracteres (CreateProcess; por debajo de eso, WinError 206 "el nombre
# del archivo o la extensión es demasiado largo"). El resto de argumentos
# de la llamada a ffmpeg (rutas, flags de códec) apenas ocupan unos
# cientos de caracteres, así que dejar ~12000 de margen es de sobra.
_MAX_FILTER_COMPLEX_CHARS = 20000

# Calidad del vídeo intermedio de corte (_cut_segment_recode): más alta que la
# del vídeo final (_CUT_CRF < _FINAL_CRF) porque este archivo se vuelve a
# recodificar en el paso de zoom -- una calidad baja aquí compondría dos
# generaciones de pérdida en vez de una sola. veryfast porque es un
# intermedio que se borra enseguida, no hace falta optimizar su tamaño.
_CUT_SHARD_CRF = "16"
_CUT_SHARD_PRESET = "veryfast"
_FINAL_CRF = "20"
_FINAL_PRESET = "medium"


def _raw_video_path(video_id: str, config: dict) -> Path:
    raw_dir = (REPO_ROOT / config["paths"]["raw"]).resolve()
    path = raw_dir / f"{video_id}.mp4"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe el vídeo de entrada para '{video_id}': {path}. "
            "Ejecuta primero la etapa de ingesta "
            "(python -m src.ingest.run --file <ruta_al_mp4_de_obs>)."
        )
    return path


def _cuts_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["cuts"]).resolve() / video_id / "cuts.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No existen los cortes para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de detección de cortes (python -m src.detect_cuts.run --video-id {video_id})."
        )
    return path


def _transcript_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["transcripts"]).resolve() / f"{video_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe la transcripción para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de transcripción (python -m src.transcribe.run --video-id {video_id})."
        )
    return path


def _output_dir(video_id: str, config: dict) -> Path:
    out_dir = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló analizando {path}:\n{result.stderr[-2000:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe devolvió un JSON inválido para {path}: {exc}") from exc


def _parse_frame_rate(raw: str | None) -> float | None:
    if not raw:
        return None
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return None
        return num_f / den_f if den_f else None
    try:
        return float(raw)
    except ValueError:
        return None


def _video_info(path: Path) -> dict:
    """Devuelve {"duration": float, "width": int, "height": int, "fps": float} de un vídeo."""
    probe = _probe(path)
    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        raise ValueError(f"{path} no tiene ninguna pista de vídeo.")
    video_stream = video_streams[0]

    duration = None
    for candidate in (video_stream.get("duration"), probe.get("format", {}).get("duration")):
        if candidate is None:
            continue
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if duration is None:
        raise ValueError(f"No se pudo determinar la duración de {path}.")

    fps = _parse_frame_rate(video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")) or 30.0

    audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    audio_stream = audio_streams[0] if audio_streams else {}

    return {
        "duration": duration,
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": fps,
        "sample_rate": int(audio_stream["sample_rate"]) if audio_stream.get("sample_rate") else None,
        "channels": audio_stream.get("channels"),
    }


def _run_ffmpeg(cmd: list[str], *, description: str) -> None:
    logger.info("%s...", description)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló ({description}):\n{result.stderr[-4000:]}")


def _compute_keep_segments(cuts: list[dict], duration: float) -> list[tuple[float, float]]:
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


def _map_to_edited_timeline(t: float, sorted_cuts: list[dict]) -> float:
    """
    Convierte un timestamp del vídeo ORIGINAL a su equivalente en la línea
    de tiempo YA CORTADA, restando la duración acumulada de los cortes
    anteriores a t (o la porción de un corte que contenga a t) — el mismo
    remapeo que CLAUDE.md documenta como necesario para detect_chapters.

    `sorted_cuts` debe estar ordenado por "start".
    """
    removed = 0.0
    for c in sorted_cuts:
        if c["start"] >= t:
            break
        removed += min(float(c["end"]), t) - float(c["start"])
    return max(0.0, t - removed)


def detect_long_speech_segments(transcript: dict, cuts: list[dict], config: dict) -> list[dict]:
    """
    Agrupa palabras consecutivas de transcript['words'] cuyo hueco (gap)
    entre el final de una y el inicio de la siguiente es menor que
    config['edit']['long_speech_gap_seconds'], y devuelve los grupos
    resultantes cuya duración en el vídeo ORIGINAL es >=
    config['edit']['long_speech_min_seconds'].

    Los timestamps devueltos ya están remapeados a la línea de tiempo del
    vídeo EDITADO (después de aplicar `cuts`), listos para usarse
    directamente en el filtro de zoom sobre el clip ya cortado.

    Returns:
        [{"start": float, "end": float}, ...] en la línea de tiempo
        editada, ordenados por tiempo.
    """
    words = transcript.get("words", [])
    if not words:
        return []

    edit_config = config.get("edit", {})
    gap_threshold = float(edit_config.get("long_speech_gap_seconds", 1.2))
    min_seconds = float(edit_config.get("long_speech_min_seconds", 10.0))

    raw_runs: list[tuple[float, float]] = []
    run_start = float(words[0]["start"])
    run_end = float(words[0]["end"])
    for prev_word, word in zip(words, words[1:]):
        gap = float(word["start"]) - float(prev_word["end"])
        if gap <= gap_threshold:
            run_end = float(word["end"])
        else:
            raw_runs.append((run_start, run_end))
            run_start = float(word["start"])
            run_end = float(word["end"])
    raw_runs.append((run_start, run_end))

    sorted_cuts = sorted(cuts, key=lambda c: c["start"])

    long_runs: list[dict] = []
    for start, end in raw_runs:
        if end - start < min_seconds:
            continue
        edited_start = _map_to_edited_timeline(start, sorted_cuts)
        edited_end = _map_to_edited_timeline(end, sorted_cuts)
        if edited_end <= edited_start:
            continue
        long_runs.append({"start": edited_start, "end": edited_end})

    return long_runs


def _build_facecam_zoom_expr(speech_segments: list[dict], zoom_factor: float, ramp_seconds: float) -> str | None:
    """
    Expresión ffmpeg (evaluable con la variable `t`, en la línea de tiempo
    del clip ya cortado) para el efecto de zoom típico de streamer: durante
    la ventana [start, start+ramp_seconds] de cada tramo de speech_segments
    sube suavemente desde 1.0 hasta zoom_factor (curva coseno alzada, sin
    saltos), y CORTA SECO a 1.0 exactamente en t=start+ramp_seconds — no en
    el final del tramo de habla — porque `between(t,start,start+ramp)` deja
    de cumplirse instantáneamente ahí. El zoom NO se mantiene sostenido
    durante el resto del tramo de habla; su duración visible es siempre
    ramp_seconds. Vale 1.0 fuera de esa ventana.

    Si un tramo dura MENOS que ramp_seconds no da tiempo a completar la
    rampa antes de que el tramo termine, así que se descarta por completo
    (sin zoom en ese tramo) en vez de comprimir la rampa para que quepa —
    un zoom a medio completar que además no llega a mostrarse sostenido
    ni un instante sería más distracción que efecto.

    None si no hay tramos, el factor es <= 1.0, o ramp_seconds <= 0 (con
    corte en start+ramp_seconds, una rampa de 0s no llegaría a ser visible).
    """
    if not speech_segments or zoom_factor <= 1.0 or ramp_seconds <= 0:
        return None

    terms = []
    for seg in speech_segments:
        start, end = seg["start"], seg["end"]
        dur = end - start
        if dur < ramp_seconds:
            continue
        ramp_end = start + ramp_seconds
        level = f"0.5*(1-cos(PI*(t-{start:.6f})/{ramp_seconds:.6f}))"
        terms.append(f"if(between(t,{start:.6f},{ramp_end:.6f}),{level},0)")
    if not terms:
        return None

    nested = terms[0]
    for term in terms[1:]:
        nested = f"max({nested},{term})"
    return f"(1+({zoom_factor}-1)*({nested}))"


def _build_facecam_zoom_filters(
    zoom_expr: str, focus_x: float, focus_y: float, width: int, height: int
) -> tuple[str, str]:
    """
    Devuelve (scale_filter, crop_filter) que juntos implementan el zoom
    hacia la webcam: primero se agranda el frame ENTERO por zoom_expr(t)
    (filtro scale, que sí soporta expresiones dependientes de `t` vía
    eval=frame), y después se recorta una ventana de tamaño FIJO
    (width x height, el tamaño original) cuya posición sigue al punto
    (focus_x, focus_y) ya escalado — el filtro crop no tiene opción `eval`
    y sus parámetros w/h no aceptan `t` en las pruebas hechas contra este
    build de ffmpeg, pero x/y sí, así que el zoom en sí se hace con scale y
    solo el desplazamiento hacia la webcam con crop.

    A zoom=1.0 el frame escalado mide igual que el original, así que la
    ventana de recorte solo cabe en la posición (0,0): sin desplazamiento
    visible, tal y como se espera con zoom desactivado.
    """
    scale_filter = f"scale=w='trunc(iw*({zoom_expr})/2)*2':h='trunc(ih*({zoom_expr})/2)*2':eval=frame"
    crop_x = f"min(max({focus_x:.2f}*({zoom_expr})-{width}/2,0),in_w-{width})"
    crop_y = f"min(max({focus_y:.2f}*({zoom_expr})-{height}/2,0),in_h-{height})"
    crop_filter = f"crop=w={width}:h={height}:x='{crop_x}':y='{crop_y}'"
    return scale_filter, crop_filter


def _partition_by_length(
    items: list, build_filter_fn, max_chars: int, max_items: int | None = None
) -> list[list]:
    """
    Agrupa `items` en particiones consecutivas tal que
    len(build_filter_fn(partition)) no supere max_chars caracteres NI (si
    se indica max_items) el nº de items por partición supere max_items.
    Usado por _plan_zoom_passes -- el corte ya no arma ningún
    filter_complex (ver "Fuga de frames de vídeo en el filtro concat de
    ffmpeg" en el docstring del módulo), así que esta función solo
    particiona tramos de ZOOM hoy. Cada item aporta por sí solo un puñado
    de líneas de longitud acotada (nunca una fracción apreciable de
    max_chars), así que esto siempre progresa: ninguna partición queda
    vacía salvo que `items` lo esté.
    """
    partitions: list[list] = []
    current: list = []
    for item in items:
        candidate = current + [item]
        too_long = len(build_filter_fn(candidate)) > max_chars
        too_many = max_items is not None and len(candidate) > max_items
        if current and (too_long or too_many):
            partitions.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        partitions.append(current)
    return partitions


def _scan_keyframe_timestamps(path: Path) -> list[float]:
    """
    Lista ORDENADA de los timestamps (segundos, pts_time) de todos los
    keyframes de la pista de vídeo de `path`, vía ffprobe -- solo demuxea
    paquetes (lee sus flags), sin decodificar ningún frame, así que es
    barato incluso en grabaciones de 1-2h (ver "Renderizado parcial sin
    pérdida" en el docstring del módulo). Usado por _cut_video para
    decidir qué parte interior de cada tramo a conservar se puede copiar
    sin recodificar.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló escaneando keyframes de {path}:\n{result.stderr[-2000:]}")
    keyframes: list[float] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 2 or "K" not in parts[1]:
            continue
        try:
            keyframes.append(float(parts[0]))
        except ValueError:
            continue
    keyframes.sort()
    return keyframes


def _keyframe_at_or_after(keyframes: list[float], t: float) -> float | None:
    """Primer keyframe >= t de `keyframes` (ya ordenada), o None si no hay ninguno."""
    i = bisect.bisect_left(keyframes, t)
    return keyframes[i] if i < len(keyframes) else None


def _keyframe_at_or_before(keyframes: list[float], t: float) -> float | None:
    """Último keyframe <= t de `keyframes` (ya ordenada), o None si no hay ninguno."""
    i = bisect.bisect_right(keyframes, t) - 1
    return keyframes[i] if i >= 0 else None


def _cut_segment_recode(input_path: Path, start: float, end: float, out_path: Path, description: str) -> None:
    """
    Recodifica [start, end] (timestamps absolutos del vídeo de entrada) a
    out_path -- crf16/veryfast, con un simple trim de entrada (-ss/-to
    antes de -i, preciso a nivel de frame) -- SIN ningún filtro `concat`
    ni `filter_complex` (ver "Fuga de frames de vídeo en el filtro concat
    de ffmpeg" en el docstring del módulo). Usado tanto para el fallback
    de tramo completo como para la cabeza/cola del renderizado parcial
    sin pérdida (ver _cut_segment_smart).
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
        "-i", str(input_path),
        "-c:v", "libx264", "-crf", _CUT_SHARD_CRF, "-preset", _CUT_SHARD_PRESET, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=description)


def _cut_segment_copy(input_path: Path, start: float, end: float, out_path: Path, description: str) -> None:
    """
    Copia [start, end] SIN recodificar (-c copy, prácticamente gratis).
    Solo válido cuando `start` y `end` caen EXACTAMENTE en keyframes
    reales del vídeo de entrada (ver _cut_segment_smart y "Renderizado
    parcial sin pérdida" en el docstring del módulo) -- si no coincidieran
    con un keyframe, ffmpeg redondearía `start` hacia el keyframe anterior
    e incluiría de más contenido intermedio (el mismo problema ya
    documentado del inpoint del concat demuxer, evitado aquí porque los
    timestamps SÍ son keyframes reales).
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
        "-i", str(input_path),
        "-c", "copy",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=description)


def _cut_segment_smart(
    input_path: Path, start: float, end: float, keyframes: list[float],
    index: int, total: int, out_dir: Path,
) -> list[Path]:
    """
    Recorta el tramo [start, end] (timestamps absolutos del vídeo de
    entrada) a uno o más archivos aislados, aplicando "renderizado
    parcial sin pérdida" (ver docstring del módulo): busca el primer
    keyframe >= start (`kf_start`) y el último keyframe <= end
    (`kf_end`); si hay hueco entre ambos, recodifica solo la cabeza
    [start, kf_start) y la cola [kf_end, end) (omitidas si ya están
    vacías) y copia sin recodificar el interior [kf_start, kf_end) --
    mucho más barato que recodificar el tramo entero. Si el tramo es más
    corto que un intervalo de keyframe (no hay hueco útil), cae al
    comportamiento de siempre: recodificar el tramo completo.

    Returns:
        Lista de 1 a 3 fragmentos, EN ORDEN, listos para pegarse con el
        concat demuxer junto con los del resto de tramos.
    """
    kf_start = _keyframe_at_or_after(keyframes, start)
    kf_end = _keyframe_at_or_before(keyframes, end)
    useful_gap = kf_start is not None and kf_end is not None and kf_end > kf_start

    if not useful_gap:
        out_path = out_dir / f"_cut_seg_{index}_full.mp4"
        _cut_segment_recode(
            input_path, start, end, out_path,
            description=(
                f"Cortando tramo {index + 1}/{total} completo "
                f"({start:.2f}s-{end:.2f}s, sin hueco interior copiable)"
            ),
        )
        return [out_path]

    fragments: list[Path] = []
    if kf_start > start:
        head_path = out_dir / f"_cut_seg_{index}_head.mp4"
        _cut_segment_recode(
            input_path, start, kf_start, head_path,
            description=f"Cortando tramo {index + 1}/{total}, cabeza ({start:.2f}s-{kf_start:.2f}s)",
        )
        fragments.append(head_path)

    mid_path = out_dir / f"_cut_seg_{index}_mid.mp4"
    _cut_segment_copy(
        input_path, kf_start, kf_end, mid_path,
        description=(
            f"Copiando tramo {index + 1}/{total}, interior sin recodificar "
            f"({kf_start:.2f}s-{kf_end:.2f}s)"
        ),
    )
    fragments.append(mid_path)

    if end > kf_end:
        tail_path = out_dir / f"_cut_seg_{index}_tail.mp4"
        _cut_segment_recode(
            input_path, kf_end, end, tail_path,
            description=f"Cortando tramo {index + 1}/{total}, cola ({kf_end:.2f}s-{end:.2f}s)",
        )
        fragments.append(tail_path)

    return fragments


def _glue_video_files(paths: list[Path], out_path: Path) -> None:
    """
    Pega los archivos de `paths` (ya completos, cada uno un fragmento del
    vídeo final -- recodificado por _cut_segment_recode o copiado sin
    recodificar por _cut_segment_copy, ambos con los mismos parámetros de
    stream) con el concat DEMUXER, SIN inpoint/outpoint (solo
    `file '...'` por entrada) y `-c copy`: rápido y exacto porque no
    recorta dentro de ningún archivo a mitad de GOP (ver la nota del
    docstring del módulo sobre por qué NO se usa inpoint/outpoint para
    cortar), y no pasa por el filtro `concat` de ffmpeg -- mismo mecanismo
    ya validado en la vía rápida de append_outro.
    """
    list_path = out_path.with_suffix(".txt")
    list_lines = [f"file '{Path(p).resolve().as_posix()}'" for p in paths]
    list_path.write_text("\n".join(list_lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy",
        str(out_path),
    ]
    _run_ffmpeg(cmd, description=f"Uniendo {len(paths)} tramo(s) cortado(s)")
    list_path.unlink(missing_ok=True)


def _cut_video(input_path: Path, keep_segments: list[tuple[float, float]], out_dir: Path) -> Path:
    """
    Recorta `keep_segments` de `input_path` aplicando "renderizado
    parcial sin pérdida" (ver docstring del módulo): cada tramo se corta
    con _cut_segment_smart, que copia sin recodificar (-c copy) el
    interior de cada tramo entre dos keyframes reales y solo recodifica
    los bordes (o el tramo completo, como fallback, si es más corto que
    un intervalo de keyframe) -- sin `concat` ni `filter_complex` en
    ningún caso (ver "Fuga de frames de vídeo en el filtro concat de
    ffmpeg"). Todos los fragmentos resultantes (1 a 3 por tramo) se pegan
    después con el concat DEMUXER (_glue_video_files). Sin límite
    práctico de nº de tramos: cada fragmento es una llamada de ffmpeg
    independiente y corta, así que ni el nº de tramos ni sus caracteres
    pueden acercar la línea de comandos al límite de Windows.

    Returns:
        Ruta al vídeo ya cortado, sin zoom (data/output/<video_id>/_cuts.mp4).
    """
    t0 = time.monotonic()
    keyframes = _scan_keyframe_timestamps(input_path)
    logger.info(
        "%d keyframe(s) encontrados en %s (%.1fs, solo demux) para el renderizado parcial sin pérdida",
        len(keyframes), input_path.name, time.monotonic() - t0,
    )

    logger.info(
        "Cortando %d tramo(s) a conservar (interior copiado sin recodificar cuando hay hueco entre "
        "keyframes; recodificación completa como fallback)",
        len(keep_segments),
    )
    segment_paths: list[Path] = []
    n_partial = 0
    n_full_recode = 0
    for i, (start, end) in enumerate(keep_segments):
        fragments = _cut_segment_smart(input_path, start, end, keyframes, i, len(keep_segments), out_dir)
        segment_paths.extend(fragments)
        if len(fragments) == 1:
            n_full_recode += 1
        else:
            n_partial += 1
    logger.info(
        "%d tramo(s) con interior copiado sin recodificar, %d tramo(s) recodificados por completo "
        "(sin hueco interior útil)",
        n_partial, n_full_recode,
    )

    cut_path = out_dir / "_cuts.mp4"
    if len(segment_paths) == 1:
        shutil.move(str(segment_paths[0]), str(cut_path))
    else:
        _glue_video_files(segment_paths, cut_path)
        for p in segment_paths:
            Path(p).unlink(missing_ok=True)
    return cut_path


def _build_zoom_pass_filter_complex(
    speech_segments: list[dict], zoom_factor: float, ramp_seconds: float,
    focus_x: float, focus_y: float, width: int, height: int,
) -> str | None:
    """
    filter_complex que aplica el zoom hacia la webcam de `speech_segments`
    sobre [0:v] (un único vídeo de entrada -- el ya cortado por
    _cut_video), dejando el resultado en [vout]. Solo trata vídeo: el
    audio no pasa por aquí, se copia sin recodificar (ver _apply_zoom).
    None si _build_facecam_zoom_expr no genera ninguna expresión (ningún
    tramo de este subconjunto llega a completar la rampa).
    """
    zoom_expr = _build_facecam_zoom_expr(speech_segments, zoom_factor, ramp_seconds)
    if not zoom_expr:
        return None
    scale_filter, crop_filter = _build_facecam_zoom_filters(zoom_expr, focus_x, focus_y, width, height)
    return f"[0:v]{scale_filter}[vscaled];[vscaled]{crop_filter}[vout];"


def _plan_zoom_passes(
    speech_segments: list[dict], zoom_factor: float, ramp_seconds: float,
    focus_x: float, focus_y: float, width: int, height: int,
    max_chars: int = _MAX_FILTER_COMPLEX_CHARS,
) -> list[list[dict]]:
    """
    Reparte los tramos de speech_segments que SÍ producen zoom (duración
    >= ramp_seconds, ver _build_facecam_zoom_expr) en pasadas para
    _apply_zoom, tal que ninguna pasada supere max_chars (ver
    _partition_by_length). A la escala real (decenas de tramos) esto cabe
    siempre en una única pasada; la partición solo entraría en juego si
    algún día hubiera cientos de tramos de habla larga en un mismo vídeo.
    """
    if zoom_factor <= 1.0 or ramp_seconds <= 0:
        return []
    usable = [s for s in speech_segments if (s["end"] - s["start"]) >= ramp_seconds]
    if not usable:
        return []

    def build(segs: list[dict]) -> str:
        return (
            _build_zoom_pass_filter_complex(segs, zoom_factor, ramp_seconds, focus_x, focus_y, width, height)
            or ""
        )

    return _partition_by_length(usable, build, max_chars)


def _apply_zoom(cut_path: Path, speech_segments: list[dict], config: dict, width: int, height: int) -> Path:
    """
    Aplica el zoom hacia la webcam sobre `cut_path` (el vídeo ya cortado,
    sin zoom) en uno o más filter_complex APARTE del corte -- mucho más
    corto que el combinado anterior porque solo cubre los tramos de habla
    larga (p.ej. 47), no los cientos de cortes. Si hicieran falta más
    pasadas de las que caben en un único filter_complex (ver
    _plan_zoom_passes), se encadenan: cada pasada parte de la salida de la
    anterior y solo aplica zoom en su subconjunto de tramos (fuera de ellos
    el vídeo pasa sin cambios, ver _build_facecam_zoom_filters). Toma
    posesión de `cut_path`: lo consume (lo renombra o lo borra) en
    cualquier caso, el llamador no necesita limpiarlo aparte.

    Returns:
        Ruta a data/output/<video_id>/_cuts_zoom.mp4 (o `cut_path`
        renombrado sin cambios si no hay ningún tramo de zoom que aplicar).
    """
    edit_config = config.get("edit", {})
    zoom_factor = float(edit_config.get("long_speech_zoom_factor", 1.0))
    ramp_seconds = float(edit_config.get("zoom_in_duration_seconds", 4.5))
    facecam = config.get("facecam_region") or {}
    focus_x = float(facecam.get("x", 0)) + float(facecam.get("w", width)) / 2
    focus_y = float(facecam.get("y", 0)) + float(facecam.get("h", height)) / 2

    result_path = cut_path.with_name("_cuts_zoom.mp4")
    passes = _plan_zoom_passes(speech_segments, zoom_factor, ramp_seconds, focus_x, focus_y, width, height)
    if not passes:
        # Sin zoom que aplicar: remux barato (sin recodificar) en vez de un
        # simple rename, para que este archivo tenga +faststart igual que
        # si hubiera pasado por una pasada de zoom (ver más abajo) --
        # normalize_audio/append_outro solo lo aplican si de verdad
        # recodifican, y si loudnorm/outro estuvieran desactivados este
        # sería directamente el final.mp4 entregado.
        _run_ffmpeg(
            ["ffmpeg", "-y", "-i", str(cut_path), "-c", "copy", "-movflags", "+faststart", str(result_path)],
            description="Sin tramos de zoom que aplicar; remuxeando el vídeo ya cortado",
        )
        cut_path.unlink(missing_ok=True)
        return result_path

    logger.info(
        "Aplicando zoom hacia la webcam en %d tramo(s) de habla larga en %d pasada(s) de ffmpeg "
        "(factor=%s, rampa=%ss)",
        sum(len(p) for p in passes), len(passes), zoom_factor, ramp_seconds,
    )

    current_input = cut_path
    for i, segs in enumerate(passes):
        is_last = i == len(passes) - 1
        out_path = result_path if is_last else cut_path.with_name(f"_zoom_pass_{i}.mp4")
        filter_complex = _build_zoom_pass_filter_complex(
            segs, zoom_factor, ramp_seconds, focus_x, focus_y, width, height
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(current_input),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "0:a",
            "-c:v", "libx264", "-crf", _FINAL_CRF, "-preset", _FINAL_PRESET, "-pix_fmt", "yuv420p",
            "-c:a", "copy",
        ]
        if is_last:
            # Solo la ÚLTIMA pasada puede acabar siendo el resultado final
            # de apply_cuts_with_zoom (las intermedias se recodifican otra
            # vez enseguida), así que solo ella necesita +faststart.
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(out_path))
        _run_ffmpeg(
            cmd,
            description=f"Zoom hacia la webcam (pasada {i + 1}/{len(passes)}, {len(segs)} tramo(s))",
        )
        Path(current_input).unlink(missing_ok=True)
        current_input = out_path

    return result_path


def apply_cuts_with_zoom(video_id: str, cuts: list[dict], config: dict) -> str:
    """
    Corta los tramos marcados en `cuts` de data/raw/<video_id>.mp4
    (conservando el resto) y aplica el zoom típico de streamer durante los
    tramos de habla continua de config['edit']['long_speech_min_seconds']
    segundos o más (ver detect_long_speech_segments): sube lento hacia
    config['facecam_region'] durante los primeros
    config['edit']['zoom_in_duration_seconds'] del tramo, y corta seco a
    1.0 exactamente al completarse esa rampa — no al terminar el tramo de
    habla (ver _build_facecam_zoom_expr).

    En DOS pasos independientes (ver docstring del módulo para el
    porqué): _cut_video recorta primero (uno o más shards de ffmpeg, según
    haga falta), y _apply_zoom aplica el zoom después sobre el resultado,
    en su propio filter_complex -- mucho más pequeño porque solo cubre los
    tramos de habla larga, no los cortes.

    Returns:
        Ruta al vídeo con los cortes y el zoom ya aplicados
        (data/output/<video_id>/_cuts_zoom.mp4).
    """
    input_path = _raw_video_path(video_id, config)
    info = _video_info(input_path)
    duration, width, height = info["duration"], info["width"], info["height"]

    keep_segments = _compute_keep_segments(cuts, duration)
    if not keep_segments:
        raise ValueError(
            f"Los cortes de '{video_id}' eliminan el vídeo entero (duración {duration:.2f}s); "
            "no queda nada que conservar."
        )

    logger.info(
        "%d tramo(s) a conservar de %d corte(s) (duración original %.2fs)",
        len(keep_segments), len(cuts), duration,
    )

    transcript_path = _transcript_path(video_id, config)
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)
    speech_segments = detect_long_speech_segments(transcript, cuts, config)
    if speech_segments:
        logger.info(
            "%d tramo(s) de habla continua >= %.1fs detectado(s) (línea de tiempo editada): %s",
            len(speech_segments),
            float(config.get("edit", {}).get("long_speech_min_seconds", 10.0)),
            ", ".join(f"{s['start']:.2f}s-{s['end']:.2f}s" for s in speech_segments),
        )
    else:
        logger.info("No se ha detectado ningún tramo de habla continua; no se aplicará zoom.")

    out_dir = _output_dir(video_id, config)
    cut_path = _cut_video(input_path, keep_segments, out_dir)
    result_path = _apply_zoom(cut_path, speech_segments, config, width, height)

    return str(result_path)


def _measure_loudness(path: str) -> dict | None:
    """Primera pasada de loudnorm (solo análisis): devuelve las medidas en JSON, o None si no se pudieron parsear."""
    cmd = [
        "ffmpeg", "-i", path, "-vn",
        "-af",
        f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}:print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # El bloque JSON de loudnorm no es necesariamente lo último en stderr
    # (ffmpeg suele imprimir un resumen de muxing/tamaño después); se busca
    # el ÚLTIMO bloque {...} en todo el stderr en vez de anclarlo al final.
    matches = re.findall(r"\{[^{}]*\}", result.stderr)
    if not matches:
        logger.warning("No se pudo leer la medición de sonoridad de ffmpeg loudnorm; se omite la normalización.")
        return None
    match = matches[-1]
    try:
        return json.loads(match)
    except json.JSONDecodeError:
        logger.warning("La medición de sonoridad de ffmpeg loudnorm no es JSON válido; se omite la normalización.")
        return None


def normalize_audio(clip_path: str, config: dict) -> str:
    """
    Normaliza el audio de clip_path con ffmpeg loudnorm (dos pasadas: mide
    y luego aplica), si config['edit']['loudnorm'] es true. El vídeo no se
    re-codifica (-c:v copy); solo se transcodifica el audio.

    Returns:
        Ruta al clip con audio normalizado (mismo directorio que
        clip_path), o clip_path sin cambios si loudnorm está desactivado.
    """
    edit_config = config.get("edit", {})
    if not edit_config.get("loudnorm", True):
        logger.info("loudnorm desactivado en config; se omite la normalización de audio.")
        return clip_path

    measured = _measure_loudness(clip_path)
    output_path = str(Path(clip_path).with_name("_normalized.mp4"))

    if measured is None:
        loudnorm_filter = f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}"
    else:
        loudnorm_filter = (
            f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}:"
            f"measured_I={measured.get('input_i')}:measured_TP={measured.get('input_tp')}:"
            f"measured_LRA={measured.get('input_lra')}:measured_thresh={measured.get('input_thresh')}:"
            f"offset={measured.get('target_offset')}:linear=true:print_format=summary"
        )

    cmd = [
        "ffmpeg", "-y", "-i", clip_path,
        "-c:v", "copy",
        "-af", loudnorm_filter,
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd, description="Normalizando audio (loudnorm, dos pasadas)")

    return output_path


def _same_stream_params(clip_info: dict, outro_info: dict) -> bool:
    """
    True si clip_info y outro_info son lo bastante parecidos como para
    concatenar con el concat demuxer (-c copy) sin recodificar.

    IMPORTANTE: el concat demuxer con -c copy NO valida esto por su cuenta
    — si los streams no encajan, no falla con un código de error, produce
    un archivo "correcto" pero corrupto (fps/duración con valores absurdos,
    p.ej. un frame rate mezcla de los dos vídeos). Por eso aquí se
    comprueba explícitamente ANTES de elegir la vía rápida, en vez de
    intentarla y fiarse del returncode de ffmpeg.
    """
    if clip_info["width"] != outro_info["width"] or clip_info["height"] != outro_info["height"]:
        return False
    if abs(clip_info["fps"] - outro_info["fps"]) > 0.01:
        return False
    if clip_info["sample_rate"] != outro_info["sample_rate"]:
        return False
    if clip_info["channels"] != outro_info["channels"]:
        return False
    return True


def append_outro(clip_path: str, config: dict) -> str:
    """
    Concatena assets/outro/outro.mp4 al final de clip_path, si
    config['edit']['append_outro'] es true y el archivo existe.

    Usa una concatenación rápida sin recodificar (concat demuxer, -c copy)
    SOLO si se comprueba (vía ffprobe) que el outro tiene la misma
    resolución/fps/sample rate/canales que el clip principal — el outro ya
    debería prepararse así (ver README). Si no coinciden, recae en una
    concatenación más lenta pero robusta vía filter_complex que normaliza
    el outro a los parámetros del clip principal.

    Returns:
        Ruta al clip final con el outro añadido, o clip_path sin cambios
        si append_outro está desactivado o no existe el archivo de outro.
    """
    edit_config = config.get("edit", {})
    if not edit_config.get("append_outro", True):
        logger.info("append_outro desactivado en config; se omite el outro.")
        return clip_path

    outro_path = (REPO_ROOT / config["paths"]["outro"]).resolve()
    if not outro_path.exists() or outro_path.stat().st_size == 0:
        logger.warning(
            "append_outro está activado pero no existe (o está vacío) el archivo de outro en %s; "
            "se continúa sin añadir outro.",
            outro_path,
        )
        return clip_path

    output_path = str(Path(clip_path).with_name("_with_outro.mp4"))
    info = _video_info(Path(clip_path))
    outro_info = _video_info(outro_path)

    if _same_stream_params(info, outro_info):
        list_path = Path(clip_path).with_name("_outro_concat_list.txt")
        list_path.write_text(
            f"file '{Path(clip_path).resolve()}'\nfile '{outro_path.resolve()}'\n", encoding="utf-8"
        )
        fast_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart", output_path,
        ]
        logger.info("Añadiendo outro (concatenación rápida sin recodificar; mismos parámetros de stream)...")
        result = subprocess.run(fast_cmd, capture_output=True, text=True)
        list_path.unlink(missing_ok=True)
        if result.returncode == 0:
            return output_path
        logger.warning(
            "La concatenación rápida del outro falló pese a tener los mismos parámetros de stream; "
            "recodificando el outro para que encaje con el clip principal:\n%s",
            result.stderr[-1000:],
        )
    else:
        logger.info(
            "El outro no tiene la misma resolución/fps/audio que el clip principal "
            "(clip=%dx%d@%.2ffps/%sHz/%sch, outro=%dx%d@%.2ffps/%sHz/%sch); "
            "recodificando el outro para que encaje.",
            info["width"], info["height"], info["fps"], info["sample_rate"], info["channels"],
            outro_info["width"], outro_info["height"], outro_info["fps"],
            outro_info["sample_rate"], outro_info["channels"],
        )

    width, height, fps = info["width"], info["height"], info["fps"]
    sample_rate = info["sample_rate"] or 48000
    channel_layout = "stereo" if (info["channels"] or 2) >= 2 else "mono"

    slow_cmd = [
        "ffmpeg", "-y",
        "-i", clip_path, "-i", str(outro_path),
        "-filter_complex",
        (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[outro_v];"
            f"[1:a]aformat=sample_rates={sample_rate}:channel_layouts={channel_layout}[outro_a];"
            "[0:v][0:a][outro_v][outro_a]concat=n=2:v=1:a=1[vout][aout]"
        ),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(slow_cmd, description="Añadiendo outro (recodificando para compatibilidad)")

    return output_path


def run(video_id: str, config: dict) -> dict:
    """
    Orquesta apply_cuts_with_zoom -> normalize_audio -> append_outro y
    guarda el resultado en data/output/<video_id>/final.mp4.

    Returns:
        dict con {"video_id": str, "output_path": str}
    """
    cuts_path = _cuts_path(video_id, config)
    with open(cuts_path, "r", encoding="utf-8") as f:
        cuts = json.load(f)

    stage_paths: list[str] = []

    clip_path = apply_cuts_with_zoom(video_id, cuts, config)
    stage_paths.append(clip_path)

    normalized_path = normalize_audio(clip_path, config)
    if normalized_path != clip_path:
        stage_paths.append(normalized_path)

    final_stage_path = append_outro(normalized_path, config)
    if final_stage_path != normalized_path:
        stage_paths.append(final_stage_path)

    out_dir = _output_dir(video_id, config)
    output_path = out_dir / "final.mp4"
    shutil.move(final_stage_path, output_path)
    if final_stage_path in stage_paths:
        stage_paths.remove(final_stage_path)

    # Limpia los intermediarios (todo menos final.mp4, que ya se movió).
    for stage_path in stage_paths:
        Path(stage_path).unlink(missing_ok=True)

    logger.info("Vídeo final guardado en %s", output_path)

    db.set_status(video_id, "edited")

    return {"video_id": video_id, "output_path": str(output_path)}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Editar el vídeo final")
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    run(args.video_id, config)


if __name__ == "__main__":
    _cli()
