"""
Etapa 3: Detección de cortes.

Combina TRES señales para decidir qué tramos recortar:

1. Silencio de audio: energía RMS por debajo de
   config['detect_cuts']['silence_db_threshold'] durante al menos
   config['detect_cuts']['silence_min_seconds'].
2. Movimiento visual: optical flow denso (Farneback), mismo enfoque que
   score_motion_segment en newclips-viral-pipeline/src/detect/run.py. Un
   tramo de silencio SOLO se marca para corte si el movimiento visual está
   también por debajo de config['detect_cuts']['motion_threshold']
   (silencio + quietud). Silencio con movimiento alto (acción en pantalla
   sin hablar) NUNCA se corta.
3. Muletillas en la transcripción (config['detect_cuts']['filler_words']),
   pasadas por el mismo filtro de contexto visual antes de marcarse.

Guarda el resultado en data/cuts/<video_id>/cuts.json y loguea un resumen
(nº de cortes, duración total eliminada) antes de que edit/ los aplique.

Nota: el código de newclips-viral-pipeline no está disponible en este
repo, así que el filtro de movimiento reimplementa aquí el enfoque descrito
en CLAUDE.md (Farneback + magnitud de flujo normalizada 0.0-1.0) en vez de
importarlo directamente.

Rendimiento del filtro de movimiento: compute_motion_timeseries recorre
data/raw/<video_id>.mp4 UNA sola vez de principio a fin (sin seeks) y
guarda la magnitud de flujo óptico muestreada cada
_MOTION_SAMPLE_INTERVAL_SECONDS en una serie temporal; score_motion_segment
solo consulta esa serie ya calculada (percentil 90 en el rango del
candidato) en vez de reabrir/buscar en el vídeo por cada candidato. La
primera versión llamaba a cv2.VideoCapture.set(CAP_PROP_POS_FRAMES, ...)
una vez por candidato, y con un GOP largo (keyint por defecto de x264,
~250 frames) cada seek forzaba redecodificar desde el keyframe anterior:
con 137 candidatos eso se tradujo en ~58 minutos sobre un vídeo de 9
minutos. El recorrido único es O(duración del vídeo) una sola vez.

Fiabilidad del filtro de movimiento en grabaciones de 1-2h (progreso
visible, checkpointing reanudable, y detección de cuelgues silenciosos de
cv2.VideoCapture en Windows vía un watchdog con timeout): ver el docstring
de compute_motion_timeseries.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

import cv2
import librosa
import numpy as np

from src.common import db
from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

# Parámetros de análisis de energía de audio (no configurables hoy en
# settings.yaml): tamaño de ventana y salto para librosa.feature.rms.
_SILENCE_FRAME_LENGTH = 2048
_SILENCE_HOP_LENGTH = 512

# compute_motion_timeseries: cada cuántos segundos se muestrea un par de
# frames consecutivos para calcular optical flow a lo largo de todo el
# vídeo. No hace falta analizar frame a frame (a 30fps serían ~200k pares
# en una grabación de 2h); un muestreo cada ~0.2-0.5s ya captura si hay
# acción sostenida en un tramo.
_MOTION_SAMPLE_INTERVAL_SECONDS = 0.3

# Magnitud de flujo óptico (px/frame equivalente a resolución original,
# percentil 90 entre frames muestreados) a partir de la cual se considera
# "movimiento máximo" (score -> 1.0). Es una cota heurística (no hay
# referencia exacta del proyecto hermano en este repo) elegida para que
# quietud real de cámara/escritorio caiga muy por debajo de
# motion_threshold (0.15 por defecto) y cualquier acción visible en
# pantalla lo supere con margen.
_MOTION_NORM_MAGNITUDE_PX = 4.0

# Altura (px) a la que se reescala cada frame ANTES de calcular optical
# flow (preservando el aspect ratio; nunca se hace upscale si el vídeo ya
# es más pequeño que esto). Para "hay movimiento sí/no" no hace falta
# precisión a resolución completa, y el coste de Farneback escala con el
# nº de píxeles: un 1920x1080 reescalado a 480p analiza ~1/5 de los
# píxeles (1080/480 al cuadrado ≈ 5.06x menos trabajo). La magnitud medida
# a la resolución reducida se reescala de vuelta a equivalente-resolución-
# original (dividiendo por el mismo factor de escala) antes de guardarla,
# así que _MOTION_NORM_MAGNITUDE_PX y score_motion_segment no necesitan
# ningún cambio.
_MOTION_ANALYSIS_HEIGHT_PX = 480

# Cada cuántas muestras (o segundos de pared, lo que ocurra antes) se
# loguea el progreso del cálculo de movimiento visual. Mismo patrón que
# transcribe/run.py: en una grabación de 1-2h un proceso silencioso durante
# horas sería inaceptable.
_MOTION_PROGRESS_EVERY_SAMPLES = 50
_MOTION_PROGRESS_EVERY_SECONDS = 30.0

# Si no llega ninguna muestra nueva durante este tiempo, se asume que
# cv2.VideoCapture se ha quedado colgado (ver docstring de
# compute_motion_timeseries) y se aborta con un error claro en vez de
# esperar indefinidamente. Una muestra normal tarda del orden de
# milisegundos a un par de segundos; 120s es ya ~100x ese margen.
_MOTION_STALL_TIMEOUT_SECONDS = 120.0

# Checkpointing: cada cuántas muestras se guarda el progreso parcial a
# disco. Si no se puede estimar el nº total de muestras (p.ej.
# CAP_PROP_FRAME_COUNT poco fiable para el contenedor), se usa este valor
# fijo como fallback (≈500 muestras * 0.3s ≈ 150s de vídeo entre
# checkpoints).
_MOTION_CHECKPOINT_FALLBACK_EVERY_SAMPLES = 500

_WORD_CLEAN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalize_word(text: str) -> str:
    """Minúsculas y sin puntuación/espacios, para comparar contra filler_words."""
    return _WORD_CLEAN_RE.sub("", text.strip().lower())


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


def detect_silence_segments(video_id: str, config: dict) -> list[dict]:
    """
    Detecta tramos de silencio de audio en data/raw/<video_id>.mp4 mediante
    energía RMS por ventana, en dB (referencia amplitud=1.0, es decir
    dBFS), comparada contra config['detect_cuts']['silence_db_threshold'].
    Solo se devuelven tramos con duración >= config['detect_cuts']['silence_min_seconds'].

    Dos detalles pensados para no comerse el arranque de una voz real:

    1. Canales: se carga el audio SIN mezclar a mono (mono=False) y la
       energía de cada frame es el MÁXIMO de la RMS entre canales, no la
       media. Si el audio de un compañero de stream está paneado
       predominantemente a un canal (p.ej. mic propio a la izquierda,
       Discord del compañero a la derecha), una mezcla a mono diluye esa
       voz (mono ≈ canal_activo / 2, unos -6dB de más) y puede mantenerla
       por debajo de silence_db_threshold más tiempo del real.
    2. Frontera de fin de silencio: además del umbral principal
       (silence_db_threshold) que define los tramos candidatos, se aplica
       un umbral más estricto (silence_db_threshold -
       config['detect_cuts']['silence_onset_margin_db']) para recortar los
       bordes de cada candidato hasta el último punto de silencio
       "profundo" real. Una voz que empieza floja y va subiendo de volumen
       puede tardar más de un segundo en cruzar el umbral principal; sin
       este recorte, todo ese tramo de subida (que ya es voz real) queda
       marcado como silencio recortable.

    Returns:
        [{"start": float, "end": float}, ...] ordenado por tiempo.
    """
    input_path = _raw_video_path(video_id, config)

    detect_cuts_config = config.get("detect_cuts", {})
    db_threshold = detect_cuts_config.get("silence_db_threshold", -35)
    min_seconds = detect_cuts_config.get("silence_min_seconds", 0.8)
    onset_margin_db = detect_cuts_config.get("silence_onset_margin_db", 10.0)
    strict_threshold = db_threshold - onset_margin_db

    logger.info("Cargando audio de %s para análisis de silencios...", input_path)
    y, sr = librosa.load(str(input_path), sr=None, mono=False)

    channels = y if y.ndim > 1 else y[np.newaxis, :]
    channel_rms = [
        librosa.feature.rms(y=ch, frame_length=_SILENCE_FRAME_LENGTH, hop_length=_SILENCE_HOP_LENGTH)[0]
        for ch in channels
    ]
    rms = np.maximum.reduce(channel_rms)
    rms_db = librosa.amplitude_to_db(rms, ref=1.0)
    frame_times = librosa.frames_to_time(
        np.arange(len(rms_db)), sr=sr, hop_length=_SILENCE_HOP_LENGTH
    )

    is_silent = rms_db < db_threshold

    # 1. Tramos candidatos "en bruto", con el umbral principal (como antes).
    raw_runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for i, silent in enumerate(is_silent):
        if silent and run_start is None:
            run_start = i
        elif not silent and run_start is not None:
            raw_runs.append((run_start, i))
            run_start = None
    if run_start is not None:
        raw_runs.append((run_start, len(is_silent)))

    # 2. Recorta cada candidato a su núcleo de silencio "profundo" (umbral
    #    estricto), descartando colas donde la energía ya está subiendo
    #    hacia una voz real aunque todavía no haya cruzado el umbral
    #    principal. Nunca alarga un tramo, solo lo acorta o lo descarta.
    segments: list[dict] = []
    for i0, i1 in raw_runs:
        start_idx = i0
        while start_idx < i1 and rms_db[start_idx] >= strict_threshold:
            start_idx += 1
        end_idx = i1
        while end_idx > start_idx and rms_db[end_idx - 1] >= strict_threshold:
            end_idx -= 1

        if end_idx <= start_idx:
            continue  # ningún núcleo de silencio profundo dentro del candidato

        seg_start = float(frame_times[start_idx])
        if end_idx < len(frame_times):
            seg_end = float(frame_times[end_idx])
        else:
            seg_end = float(frame_times[-1] + _SILENCE_HOP_LENGTH / sr) if len(frame_times) else seg_start

        if seg_end - seg_start >= min_seconds:
            segments.append({"start": seg_start, "end": seg_end})

    return segments


def _motion_checkpoint_path(video_id: str, config: dict) -> Path:
    return (REPO_ROOT / config["paths"]["cuts"]).resolve() / video_id / "_motion_checkpoint.npz"


def _save_motion_checkpoint(
    checkpoint_path: Path,
    times: list[float],
    magnitudes: list[float],
    frame_idx: int,
    next_sample_frame: int,
    video_stat: os.stat_result,
) -> None:
    """
    Guarda el progreso parcial del cálculo de movimiento visual, para poder
    reanudar sin recalcular desde cero si el proceso se interrumpe (Ctrl+C,
    kill, cuelgue detectado por el watchdog...). Escribe a un archivo
    temporal y hace un rename atómico, para no dejar un checkpoint a medio
    escribir si el proceso muere justo durante el guardado.
    """
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = checkpoint_path.with_name(checkpoint_path.stem + ".tmp.npz")
    np.savez(
        tmp_path,
        times=np.asarray(times, dtype=np.float64),
        magnitudes=np.asarray(magnitudes, dtype=np.float64),
        frame_idx=np.asarray(frame_idx),
        next_sample_frame=np.asarray(next_sample_frame),
        video_mtime=np.asarray(video_stat.st_mtime),
        video_size=np.asarray(video_stat.st_size),
        sample_interval_seconds=np.asarray(_MOTION_SAMPLE_INTERVAL_SECONDS),
        analysis_height_px=np.asarray(_MOTION_ANALYSIS_HEIGHT_PX),
    )
    os.replace(tmp_path, checkpoint_path)


def _load_motion_checkpoint(checkpoint_path: Path, video_stat: os.stat_result) -> dict | None:
    """
    Carga un checkpoint previo si existe y es válido para este vídeo (mismo
    tamaño/mtime, mismo intervalo de muestreo). None si no hay checkpoint o
    está obsoleto/corrupto, en cuyo caso se recalcula desde cero.
    """
    if not checkpoint_path.exists():
        return None
    try:
        # np.load sobre un .npz devuelve un NpzFile que mantiene el archivo
        # zip abierto de forma perezosa hasta que se cierra explícitamente;
        # sin el `with`, ese handle queda abierto en Windows y el próximo
        # os.replace() al guardar un checkpoint nuevo sobre esta misma ruta
        # falla con PermissionError ("Acceso denegado") porque el archivo
        # sigue bloqueado por esta lectura.
        with np.load(checkpoint_path) as data:
            if (
                float(data["video_mtime"]) != video_stat.st_mtime
                or int(data["video_size"]) != video_stat.st_size
            ):
                logger.info(
                    "Checkpoint de movimiento visual obsoleto (el vídeo de entrada cambió); "
                    "se ignora y se recalcula desde cero."
                )
                return None
            if float(data["sample_interval_seconds"]) != _MOTION_SAMPLE_INTERVAL_SECONDS:
                logger.info(
                    "Checkpoint de movimiento visual con un intervalo de muestreo distinto al actual; "
                    "se ignora y se recalcula desde cero."
                )
                return None
            # "analysis_height_px" no existía en checkpoints de versiones
            # anteriores a la reducción de resolución para optical flow;
            # si falta, es un checkpoint viejo y se descarta (las
            # magnitudes que contendría no serían comparables con las que
            # se calcularían ahora a resolución reducida).
            if "analysis_height_px" not in data or int(data["analysis_height_px"]) != _MOTION_ANALYSIS_HEIGHT_PX:
                logger.info(
                    "Checkpoint de movimiento visual de una versión anterior (resolución de análisis "
                    "distinta); se ignora y se recalcula desde cero."
                )
                return None
            return {
                "times": list(data["times"]),
                "magnitudes": list(data["magnitudes"]),
                "frame_idx": int(data["frame_idx"]),
                "next_sample_frame": int(data["next_sample_frame"]),
            }
    except Exception as exc:  # noqa: BLE001 - un checkpoint corrupto no debe tumbar el análisis
        logger.warning(
            "No se pudo leer el checkpoint de movimiento visual (%s); se recalcula desde cero.", exc
        )
        return None


def _motion_worker(
    video_path: str,
    frame_idx: int,
    next_sample_frame: int,
    interval_frames: int,
    fps: float,
    analysis_size: tuple[int, int] | None,
    magnitude_scale_correction: float,
    out_queue: "queue.Queue",
    stop_event: threading.Event,
) -> None:
    """
    Recorre el vídeo en un hilo aparte, publicando cada muestra calculada
    en out_queue, para que el hilo principal pueda aplicar un timeout de
    "sin progreso" sin bloquearse en la llamada nativa de cv2 (ver
    docstring de compute_motion_timeseries: cv2.VideoCapture puede
    colgarse en Windows sin usar CPU/disco de forma visible, y un hilo
    bloqueado en una llamada nativa no se puede interrumpir de forma
    segura desde Python — lo único que puede hacer el hilo principal es
    dejar de esperarlo y fallar con un diagnóstico claro).

    analysis_size: (ancho, alto) al que se reescala cada frame antes de
    calcular optical flow (None si no hace falta reescalar, p.ej. el vídeo
    ya es más pequeño que _MOTION_ANALYSIS_HEIGHT_PX). Farneback escala con
    el nº de píxeles, así que reescalar antes reduce mucho el coste; la
    magnitud resultante se multiplica por magnitude_scale_correction
    (1/factor_de_escala) para que quede en unidades equivalentes a la
    resolución original y _MOTION_NORM_MAGNITUDE_PX no necesite cambiar.
    """
    try:
        # Backend FFMPEG explícito: usa libavcodec/libavformat directamente
        # (el mismo motor que ya usa el propio pipeline de ingesta), en vez
        # de fiarse de la auto-detección de backend de OpenCV. En Windows,
        # backends alternativos como Media Foundation (MSMF) tienen cuelgues
        # y fugas de recursos documentados en grabaciones largas; fijar
        # FFMPEG explícitamente es una defensa barata aunque en este dev
        # environment ya sea el backend por defecto.
        cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            out_queue.put(("error", RuntimeError(f"No se pudo abrir {video_path} con el backend FFMPEG.")))
            return
    except Exception as exc:  # noqa: BLE001 - se reenvía al hilo principal
        out_queue.put(("error", exc))
        return

    try:
        if frame_idx > 0:
            if not cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx):
                out_queue.put(("error", RuntimeError(f"No se pudo reanudar en el frame {frame_idx}.")))
                return

        while not stop_event.is_set():
            if frame_idx >= next_sample_frame:
                ok_a, frame_a = cap.read()
                if not ok_a:
                    break
                idx_a = frame_idx
                frame_idx += 1

                ok_b, frame_b = cap.read()
                if not ok_b:
                    break
                frame_idx += 1

                gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
                gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
                if analysis_size is not None:
                    gray_a = cv2.resize(gray_a, analysis_size, interpolation=cv2.INTER_AREA)
                    gray_b = cv2.resize(gray_b, analysis_size, interpolation=cv2.INTER_AREA)
                flow = cv2.calcOpticalFlowFarneback(
                    gray_a, gray_b, None, 0.5, 3, 15, 3, 5, 1.2, 0
                )
                magnitude, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
                mag_full_res_equiv = float(np.mean(magnitude)) * magnitude_scale_correction

                next_sample_frame = idx_a + interval_frames
                out_queue.put(("sample", (idx_a / fps, mag_full_res_equiv, frame_idx, next_sample_frame)))
            else:
                if not cap.grab():
                    break
                frame_idx += 1
        out_queue.put(("done", None))
    except Exception as exc:  # noqa: BLE001 - se reenvía al hilo principal
        out_queue.put(("error", exc))
    finally:
        cap.release()


def compute_motion_timeseries(video_id: str, config: dict) -> tuple[np.ndarray, np.ndarray]:
    """
    Recorre data/raw/<video_id>.mp4 UNA sola vez, de principio a fin y sin
    seeks, calculando la magnitud (sin normalizar) de optical flow denso
    (Farneback) entre pares de frames consecutivos muestreados cada
    _MOTION_SAMPLE_INTERVAL_SECONDS. Los frames que no forman parte de un
    par muestreado se saltan con cap.grab() (sin decodificar/copiar su
    imagen), así que el coste total es un único paso secuencial sobre el
    vídeo en vez de un seek por candidato.

    Cada frame se reescala a _MOTION_ANALYSIS_HEIGHT_PX de alto (preservando
    aspect ratio, sin upscale) antes de calcular optical flow: Farneback
    escala con el nº de píxeles, y para clasificar "hay movimiento sí/no"
    no hace falta la resolución completa. La magnitud se reescala de vuelta
    a equivalente-resolución-original antes de guardarse, así que
    _MOTION_NORM_MAGNITUDE_PX sigue siendo válido sin cambios.

    Fiabilidad en grabaciones largas (1-2h):

    - Progreso visible: loguea cada _MOTION_PROGRESS_EVERY_SAMPLES muestras
      o _MOTION_PROGRESS_EVERY_SECONDS de reloj (lo que ocurra antes),
      igual que transcribe/run.py.
    - Checkpointing: guarda el progreso parcial a
      data/cuts/<video_id>/_motion_checkpoint.npz aproximadamente cada 10%
      del total estimado de muestras (o cada
      _MOTION_CHECKPOINT_FALLBACK_EVERY_SAMPLES si no se puede estimar el
      total). Si el proceso se interrumpe, la siguiente ejecución reanuda
      desde ahí en vez de recalcular desde cero. El checkpoint se borra al
      terminar con éxito.
    - Cuelgues silenciosos: cv2.VideoCapture es conocido por colgarse en
      Windows sin actividad visible de CPU/disco — no es solo un problema
      del backend Media Foundation (MSMF), que tiene fugas/cuelgues
      documentados en grabaciones largas; también se ha visto (y no se
      puede descartar aquí) con el propio backend FFMPEG si su pool de
      hilos interno de decodificación se bloquea entre sí, o si un
      antivirus/EDR intercepta la lectura secuencial de un archivo grande
      y la va pausando. Ninguna de estas causas es solucionable desde
      Python puro (no hay forma segura de matar un hilo bloqueado en una
      llamada nativa). Por eso el recorrido del vídeo se hace en un hilo
      aparte (_motion_worker) que va publicando cada muestra en una cola;
      el hilo principal aplica un timeout de _MOTION_STALL_TIMEOUT_SECONDS
      a la espera de la siguiente muestra — si se agota, se asume un
      cuelgue real (una muestra normal tarda milisegundos, no minutos) y
      se levanta un error con diagnóstico claro en vez de esperar en
      silencio para siempre; el checkpoint ya guardado permite reanudar.
      (Si el antivirus resulta ser la causa, excluir la carpeta data/raw
      del escaneo en tiempo real es la mitigación fuera de este código.)

    Returns:
        (times, magnitudes): dos np.ndarray 1D del mismo tamaño (vacíos si
        el vídeo no se pudo abrir o leer). times[i] es el instante en
        segundos del primer frame del par i; magnitudes[i] es la magnitud
        media (spatial mean, sin normalizar) del flujo óptico entre ese
        frame y el siguiente.
    """
    input_path = _raw_video_path(video_id, config)
    video_stat = input_path.stat()
    checkpoint_path = _motion_checkpoint_path(video_id, config)

    resume = _load_motion_checkpoint(checkpoint_path, video_stat)
    if resume:
        times, magnitudes = resume["times"], resume["magnitudes"]
        start_frame_idx, start_next_sample = resume["frame_idx"], resume["next_sample_frame"]
        logger.info(
            "Reanudando cálculo de movimiento visual desde checkpoint: %d muestra(s) ya calculada(s) "
            "(hasta %.1fs de vídeo).",
            len(times), times[-1] if times else 0.0,
        )
    else:
        times, magnitudes = [], []
        start_frame_idx, start_next_sample = 0, 0

    probe_cap = cv2.VideoCapture(str(input_path), cv2.CAP_FFMPEG)
    if not probe_cap.isOpened():
        probe_cap.release()
        logger.warning("No se pudo abrir %s para optical flow; serie de movimiento vacía.", input_path)
        return np.array([]), np.array([])
    fps = probe_cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 30.0
    total_frames = probe_cap.get(cv2.CAP_PROP_FRAME_COUNT)
    orig_width = int(probe_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_height = int(probe_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    probe_cap.release()

    if orig_height > _MOTION_ANALYSIS_HEIGHT_PX > 0:
        magnitude_scale_correction = orig_height / _MOTION_ANALYSIS_HEIGHT_PX
        analysis_width = max(1, round(orig_width / magnitude_scale_correction))
        analysis_size = (analysis_width, _MOTION_ANALYSIS_HEIGHT_PX)
    else:
        magnitude_scale_correction = 1.0
        analysis_size = None

    interval_frames = max(1, round(_MOTION_SAMPLE_INTERVAL_SECONDS * fps))
    # CAP_PROP_FRAME_COUNT puede ser poco fiable según el contenedor; solo
    # se usa como estimación best-effort para el % de progreso y la
    # cadencia de checkpoints, nunca para lógica de corrección.
    expected_samples = int(total_frames / interval_frames) if total_frames and total_frames > 0 else None
    checkpoint_every = max(1, expected_samples // 10) if expected_samples else _MOTION_CHECKPOINT_FALLBACK_EVERY_SAMPLES

    logger.info(
        "Calculando serie de movimiento visual (optical flow, muestreo cada %.2fs, análisis a %s%s)...",
        _MOTION_SAMPLE_INTERVAL_SECONDS,
        f"{analysis_size[0]}x{analysis_size[1]}" if analysis_size else f"{orig_width}x{orig_height} (sin reescalar)",
        f", ~{expected_samples} muestra(s) esperada(s)" if expected_samples else "",
    )

    out_queue: "queue.Queue" = queue.Queue()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=_motion_worker,
        args=(
            str(input_path), start_frame_idx, start_next_sample, interval_frames, fps,
            analysis_size, magnitude_scale_correction, out_queue, stop_event,
        ),
        daemon=True,
    )
    worker.start()

    inicio = time.monotonic()
    ultimo_log = inicio
    checkpoint_baseline = len(times)

    try:
        while True:
            try:
                kind, payload = out_queue.get(timeout=_MOTION_STALL_TIMEOUT_SECONDS)
            except queue.Empty:
                stop_event.set()
                elapsed = time.monotonic() - inicio
                raise RuntimeError(
                    f"compute_motion_timeseries: sin progreso durante {_MOTION_STALL_TIMEOUT_SECONDS:.0f}s "
                    f"({len(times)} muestra(s) calculada(s), última en {times[-1] if times else 0.0:.1f}s "
                    f"de vídeo, {elapsed:.0f}s transcurridos en esta ejecución). Probable cuelgue de "
                    "cv2.VideoCapture (ver docstring del módulo para causas conocidas en Windows). "
                    f"El progreso está guardado en {checkpoint_path}; vuelve a ejecutar para reanudar "
                    "desde ahí en vez de recalcular desde cero."
                )

            if kind == "error":
                raise RuntimeError(f"Error calculando movimiento visual: {payload}") from payload
            if kind == "done":
                break

            t, magnitude, frame_idx, next_sample_frame = payload
            times.append(t)
            magnitudes.append(magnitude)

            ahora = time.monotonic()
            if len(times) % _MOTION_PROGRESS_EVERY_SAMPLES == 0 or (ahora - ultimo_log) >= _MOTION_PROGRESS_EVERY_SECONDS:
                pct = f" ({100 * len(times) / expected_samples:.1f}%)" if expected_samples else ""
                logger.info(
                    "Progreso movimiento visual: %d muestra(s)%s, última marca %.1fs de vídeo, "
                    "%.1fs de proceso transcurridos",
                    len(times), pct, t, ahora - inicio,
                )
                ultimo_log = ahora

            if len(times) - checkpoint_baseline >= checkpoint_every:
                _save_motion_checkpoint(checkpoint_path, times, magnitudes, frame_idx, next_sample_frame, video_stat)
                checkpoint_baseline = len(times)
                logger.debug("Checkpoint de movimiento visual guardado (%d muestras).", len(times))
    finally:
        stop_event.set()
        worker.join(timeout=5.0)

    checkpoint_path.unlink(missing_ok=True)
    logger.info("Serie de movimiento visual calculada: %d muestra(s)", len(times))
    return np.array(times), np.array(magnitudes)


def score_motion_segment(
    motion_times: np.ndarray, motion_magnitudes: np.ndarray, start: float, end: float, config: dict
) -> float:
    """
    Consulta la serie temporal ya calculada por compute_motion_timeseries y
    devuelve, normalizado 0.0 (quietud total) - 1.0 (movimiento máximo), el
    percentil 90 de las magnitudes de flujo muestreadas dentro de
    [start, end).

    Se usa el percentil 90 (no la media) de las magnitudes: detect_silence_segments
    agrupa en un único tramo cualquier silencio de audio continuo, que puede
    abarcar tanto quietud real como un momento de acción en pantalla (p.ej.
    5s de silencio donde el usuario está quieto los 3 primeros segundos y
    luego mueve el ratón). Promediar diluiría esa acción por debajo del
    umbral y el tramo completo se cortaría igual, violando la regla de
    CLAUDE.md ("silencio con movimiento alto... se conserva siempre"). Con
    el percentil 90 basta con que una parte apreciable del tramo tenga
    movimiento real para conservar el tramo completo (comportamiento seguro
    por defecto), sin que una única muestra con un pico espurio (glitch de
    decodificación, frame duplicado) dispare un falso "hay movimiento" como
    pasaría con el máximo estricto.

    Si el candidato es más corto que _MOTION_SAMPLE_INTERVAL_SECONDS y no
    contiene ninguna muestra, se amplía la búsqueda con un margen de
    tolerancia de _MOTION_SAMPLE_INTERVAL_SECONDS a cada lado.

    Si la serie está vacía (vídeo no se pudo analizar) o no hay ninguna
    muestra ni siquiera con el margen de tolerancia, se devuelve 1.0
    (movimiento máximo) por seguridad: ante la duda, no se corta un tramo
    que no se ha podido evaluar.
    """
    if end <= start:
        return 0.0
    if len(motion_times) == 0:
        return 1.0

    mask = (motion_times >= start) & (motion_times < end)
    if not mask.any():
        tol = _MOTION_SAMPLE_INTERVAL_SECONDS
        mask = (motion_times >= start - tol) & (motion_times < end + tol)

    if not mask.any():
        logger.warning(
            "No hay muestras de movimiento cerca de [%.2fs, %.2fs); se asume movimiento máximo.",
            start, end,
        )
        return 1.0

    peak_magnitude = float(np.percentile(motion_magnitudes[mask], 90))
    return float(min(1.0, peak_magnitude / _MOTION_NORM_MAGNITUDE_PX))


def detect_filler_segments(video_id: str, transcript: dict, config: dict) -> list[dict]:
    """
    Busca config['detect_cuts']['filler_words'] (palabras o frases de varias
    palabras, p.ej. "o sea") como subsecuencias exactas de
    transcript['words'], comparando texto normalizado (minúsculas, sin
    puntuación).

    Returns:
        [{"start": float, "end": float, "word": str}, ...] ordenado por tiempo,
        una entrada por cada aparición encontrada.
    """
    filler_words = config.get("detect_cuts", {}).get("filler_words", [])
    words = transcript.get("words", [])
    normalized = [_normalize_word(w["word"]) for w in words]

    segments: list[dict] = []
    for phrase in filler_words:
        phrase_tokens = [_normalize_word(t) for t in phrase.split()]
        phrase_tokens = [t for t in phrase_tokens if t]
        if not phrase_tokens:
            continue

        n = len(phrase_tokens)
        i = 0
        while i <= len(words) - n:
            if normalized[i:i + n] == phrase_tokens:
                segments.append({
                    "start": words[i]["start"],
                    "end": words[i + n - 1]["end"],
                    "word": phrase,
                })
                i += n  # no solapar la misma aparición con la siguiente búsqueda
            else:
                i += 1

    segments.sort(key=lambda s: s["start"])
    return segments


def _merge_overlapping_cuts(cuts: list[dict]) -> list[dict]:
    """Fusiona cortes solapados/contiguos tras aplicar el margen de seguridad."""
    if not cuts:
        return []

    ordered = sorted(cuts, key=lambda c: c["start"])
    merged = [dict(ordered[0])]
    for c in ordered[1:]:
        last = merged[-1]
        if c["start"] <= last["end"]:
            last["end"] = max(last["end"], c["end"])
            if c["type"] == "silence":
                last["type"] = "silence"
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


def run(video_id: str, config: dict) -> dict:
    """
    Combina las tres señales, aplica el filtro de contexto visual, y
    produce la lista final de cortes.

    Returns:
        dict con {"video_id", "cuts_path", "cuts": [{"start", "end", "type", "reason"}, ...],
                  "total_cut_seconds": float}
    """
    detect_cuts_config = config.get("detect_cuts", {})
    motion_threshold = detect_cuts_config.get("motion_threshold", 0.15)
    cut_margin_seconds = detect_cuts_config.get("cut_margin_seconds", 0.2)

    transcripts_dir = (REPO_ROOT / config["paths"]["transcripts"]).resolve()
    transcript_path = transcripts_dir / f"{video_id}.json"
    if not transcript_path.exists():
        raise FileNotFoundError(
            f"No existe la transcripción para '{video_id}': {transcript_path}. "
            f"Ejecuta primero la etapa de transcripción (python -m src.transcribe.run --video-id {video_id})."
        )
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    logger.info("Detectando silencios de audio...")
    silence_segments = detect_silence_segments(video_id, config)
    logger.info("%d tramo(s) de silencio candidato(s) detectado(s)", len(silence_segments))

    logger.info("Detectando muletillas en la transcripción...")
    filler_segments = detect_filler_segments(video_id, transcript, config)
    logger.info("%d muletilla(s) candidata(s) detectada(s)", len(filler_segments))

    candidates = [
        {"start": s["start"], "end": s["end"], "type": "silence", "reason": "silencio de audio"}
        for s in silence_segments
    ] + [
        {"start": s["start"], "end": s["end"], "type": "filler", "reason": f"muletilla: '{s['word']}'"}
        for s in filler_segments
    ]
    candidates.sort(key=lambda c: c["start"])

    if candidates:
        motion_times, motion_magnitudes = compute_motion_timeseries(video_id, config)
    else:
        motion_times, motion_magnitudes = np.array([]), np.array([])

    logger.info(
        "Aplicando filtro de movimiento visual (motion_threshold=%.2f) a %d candidato(s)...",
        motion_threshold, len(candidates),
    )
    accepted: list[dict] = []
    rejected_by_motion = 0
    for c in candidates:
        motion_score = score_motion_segment(motion_times, motion_magnitudes, c["start"], c["end"], config)
        if motion_score < motion_threshold:
            accepted.append(c)
        else:
            rejected_by_motion += 1
            logger.debug(
                "Descartado (%s, %.2fs-%.2fs): motion_score=%.3f >= %.3f (acción en pantalla)",
                c["type"], c["start"], c["end"], motion_score, motion_threshold,
            )

    if rejected_by_motion:
        logger.info(
            "%d tramo(s) descartado(s) por movimiento visual (silencio/muletilla con acción en pantalla)",
            rejected_by_motion,
        )

    # Margen de seguridad: se deja cut_margin_seconds SIN cortar a cada lado
    # del tramo detectado, para no comerse el arranque/final real del
    # silencio o la muletilla.
    margined: list[dict] = []
    for c in accepted:
        new_start = c["start"] + cut_margin_seconds
        new_end = c["end"] - cut_margin_seconds
        if new_end <= new_start:
            logger.debug(
                "Tramo (%s, %.2fs-%.2fs) descartado tras aplicar margen de %.2fs (queda vacío)",
                c["type"], c["start"], c["end"], cut_margin_seconds,
            )
            continue
        margined.append({**c, "start": new_start, "end": new_end})

    cuts = _merge_overlapping_cuts(margined)
    total_cut_seconds = sum(c["end"] - c["start"] for c in cuts)

    logger.info(
        "Resumen de cortes: %d corte(s), %.2fs de duración total eliminada",
        len(cuts), total_cut_seconds,
    )

    cuts_dir = (REPO_ROOT / config["paths"]["cuts"]).resolve() / video_id
    cuts_dir.mkdir(parents=True, exist_ok=True)
    cuts_path = cuts_dir / "cuts.json"
    with open(cuts_path, "w", encoding="utf-8") as f:
        json.dump(cuts, f, ensure_ascii=False, indent=2)

    logger.info("Cortes guardados en %s", cuts_path)

    # Solo se marca como "cuts_detected" una vez el JSON está escrito con éxito.
    db.set_status(video_id, "cuts_detected")

    return {
        "video_id": video_id,
        "cuts_path": str(cuts_path),
        "cuts": cuts,
        "total_cut_seconds": total_cut_seconds,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Detectar cortes de un vídeo transcrito")
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
