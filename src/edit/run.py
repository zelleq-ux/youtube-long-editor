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
   defecto 4.5s), dirigido hacia config['edit']['facecam_region']
   (posición aproximada de la webcam sobre el frame original); y CORTA
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

Rendimiento: apply_cuts_with_zoom procesa el vídeo con UN solo
filter_complex (trim de cada tramo a conservar + concat + zoom), en UNA
sola pasada de ffmpeg — nada de re-abrir/buscar en el archivo de entrada
por cada corte (ver la lección de detect_cuts en status.md: un seek por
candidato dispara el tiempo de proceso a decenas de minutos). El grafo de
filtros va como argumento -filter_complex inline (el build de ffmpeg usado
en desarrollo no soporta -filter_complex_script ni el indirect @archivo);
con una grabación muy larga y muchos cientos de cortes esto podría
acercarse al límite de longitud de línea de comandos de Windows (se
loguea un aviso si el filtro generado supera un tamaño razonable).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
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


def apply_cuts_with_zoom(video_id: str, cuts: list[dict], config: dict) -> str:
    """
    Corta los tramos marcados en `cuts` de data/raw/<video_id>.mp4
    (conservando el resto) y aplica el zoom típico de streamer durante los
    tramos de habla continua de config['edit']['long_speech_min_seconds']
    segundos o más (ver detect_long_speech_segments): sube lento hacia
    config['edit']['facecam_region'] durante los primeros
    config['edit']['zoom_in_duration_seconds'] del tramo, y corta seco a
    1.0 exactamente al completarse esa rampa — no al terminar el tramo de
    habla (ver _build_facecam_zoom_expr). Todo en una única pasada de
    ffmpeg (un solo filter_complex: trim de cada tramo a conservar + concat
    + zoom).

    Returns:
        Ruta al vídeo con los cortes y el zoom ya aplicados
        (data/output/<video_id>/_cuts_zoom.mp4).
    """
    edit_config = config.get("edit", {})
    zoom_factor = float(edit_config.get("long_speech_zoom_factor", 1.0))
    ramp_seconds = float(edit_config.get("zoom_in_duration_seconds", 4.5))

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
            float(edit_config.get("long_speech_min_seconds", 10.0)),
            ", ".join(f"{s['start']:.2f}s-{s['end']:.2f}s" for s in speech_segments),
        )
    else:
        logger.info("No se ha detectado ningún tramo de habla continua; no se aplicará zoom.")

    lines: list[str] = []
    for i, (start, end) in enumerate(keep_segments):
        lines.append(f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}];")
        lines.append(f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}];")

    n = len(keep_segments)
    if n > 1:
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
        lines.append(f"{concat_inputs}concat=n={n}:v=1:a=1[vcat][acat];")
        video_label, audio_label = "vcat", "acat"
    else:
        video_label, audio_label = "v0", "a0"

    zoom_expr = _build_facecam_zoom_expr(speech_segments, zoom_factor, ramp_seconds)
    if zoom_expr:
        facecam = edit_config.get("facecam_region") or {}
        focus_x = float(facecam.get("x", 0)) + float(facecam.get("w", width)) / 2
        focus_y = float(facecam.get("y", 0)) + float(facecam.get("h", height)) / 2
        scale_filter, crop_filter = _build_facecam_zoom_filters(zoom_expr, focus_x, focus_y, width, height)
        lines.append(f"[{video_label}]{scale_filter}[vscaled];")
        lines.append(f"[vscaled]{crop_filter}[vout];")
    else:
        lines.append(f"[{video_label}]null[vout];")
    lines.append(f"[{audio_label}]anull[aout];")

    filter_complex = "".join(lines)
    if len(filter_complex) > 20000:
        # El build de ffmpeg usado en desarrollo no soporta -filter_complex_script
        # (ni el indirect @file para -filter_complex), así que el grafo va
        # inline en la línea de comandos. Windows soporta hasta ~32767
        # caracteres de línea de comandos (subprocess.run con lista de
        # argumentos, sin shell de por medio); con muchos cientos de cortes
        # en una grabación larga esto podría llegar a ese límite.
        logger.warning(
            "El filtro de ffmpeg generado es muy largo (%d caracteres); con muchos "
            "cientos de cortes esto podría acercarse al límite de línea de comandos de Windows.",
            len(filter_complex),
        )

    out_dir = _output_dir(video_id, config)
    output_path = out_dir / "_cuts_zoom.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    _run_ffmpeg(
        cmd,
        description=(
            f"Aplicando {len(cuts)} corte(s) y zoom hacia la webcam en {len(speech_segments)} "
            f"tramo(s) de habla larga (factor={zoom_factor}, rampa={ramp_seconds}s) en una pasada"
        ),
    )

    return str(output_path)


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
