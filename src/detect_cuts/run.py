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

Además, INDEPENDIENTEMENTE de esas tres señales, recorta la intro del
vídeo (desde el instante 0 hasta que el usuario aparece en pantalla) si
config['detect_cuts']['trim_intro'] está activo (por defecto sí): ver
detect_intro_face_cut, que detecta la primera aparición fiable de una cara
dentro de config['facecam_region'] con el detector de caras ligero de
src.common.face_detection (cv2.FaceDetectorYN / "YuNet", ver ese módulo
para por qué no es un Haar cascade clásico). Este corte NO pasa por el
filtro de movimiento/silencio -- se aplica siempre que se detecte con
fiabilidad una intro sin cara, haya o no haya audio o movimiento en ese
tramo.

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

Exclusión de facecam_region del cálculo de movimiento (2026-08-09, idea
tomada de auto-editor de WyattBlue, cuya detección de movimiento soporta
restringir el análisis a una región del frame): sin esto, el propio
streamer moviéndose en su webcam (gestos, risas, hablar) cuenta como
"movimiento en pantalla" para score_motion_segment y puede evitar que se
corte un silencio real donde no pasa nada en el contenido (el juego) --
justo el caso contrario al que protege la regla de arriba ("silencio +
acción visual NO se corta": la acción tiene que ser del contenido, no del
propio streamer reaccionando en su recuadro). compute_motion_timeseries
excluye facecam_region del área sobre la que se calcula la magnitud media
de cada muestra (ver _build_motion_exclusion_mask) si
config['detect_cuts']['exclude_facecam_from_motion'] está activo (por
defecto sí); facecam_region está en píxeles del frame ORIGINAL y se
reescala proporcionalmente a la resolución de análisis (480p) antes de
aplicarse. Se excluye del promedio en vez de ponerse a cero manteniendo el
mismo denominador, para no diluir la sensibilidad al movimiento real del
resto del frame según lo grande que sea facecam_region. El checkpoint de
movimiento visual guarda una huella de la máscara usada
(_motion_mask_signature) y se invalida si no coincide con la de la
ejecución actual, para no mezclar en una misma serie muestras calculadas
con y sin exclusión si la config cambia entre una ejecución interrumpida y
su reanudación.
"""
from __future__ import annotations

import argparse
import hashlib
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
from src.common.face_detection import facecam_crop_box, frame_has_face, load_face_detector

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


def _motion_mask_signature(motion_mask: np.ndarray | None) -> str:
    """
    Huella de `motion_mask` para invalidar checkpoints calculados con una
    máscara de exclusión de facecam_region distinta (o sin máscara, o con
    otra región/config) -- ver _save_motion_checkpoint/_load_motion_checkpoint.
    "none" si no hay máscara (se analiza el frame completo); si no, la
    forma + hash del contenido, así que cualquier cambio en
    facecam_region, en exclude_facecam_from_motion, o en la resolución de
    análisis produce una huella distinta.
    """
    if motion_mask is None:
        return "none"
    return f"{motion_mask.shape[0]}x{motion_mask.shape[1]}:{hashlib.sha256(motion_mask.tobytes()).hexdigest()}"


def _save_motion_checkpoint(
    checkpoint_path: Path,
    times: list[float],
    magnitudes: list[float],
    frame_idx: int,
    next_sample_frame: int,
    video_stat: os.stat_result,
    motion_mask: np.ndarray | None,
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
        motion_mask_signature=np.asarray(_motion_mask_signature(motion_mask)),
    )
    os.replace(tmp_path, checkpoint_path)


def _load_motion_checkpoint(
    checkpoint_path: Path, video_stat: os.stat_result, motion_mask: np.ndarray | None
) -> dict | None:
    """
    Carga un checkpoint previo si existe y es válido para este vídeo (mismo
    tamaño/mtime, mismo intervalo de muestreo, misma máscara de exclusión
    de facecam_region -- ver _motion_mask_signature). None si no hay
    checkpoint o está obsoleto/corrupto, en cuyo caso se recalcula desde
    cero.
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
            # "motion_mask_signature" no existía antes de excluir
            # facecam_region del cálculo de movimiento; si falta, o no
            # coincide con la máscara de ESTA ejecución (config o región
            # cambiada, o exclude_facecam_from_motion activado/desactivado
            # desde el checkpoint anterior), se descarta -- mezclar
            # muestras calculadas con y sin exclusión en una misma serie
            # sería silenciosamente inconsistente.
            stored_signature = str(data["motion_mask_signature"]) if "motion_mask_signature" in data else None
            if stored_signature != _motion_mask_signature(motion_mask):
                logger.info(
                    "Checkpoint de movimiento visual calculado con otra máscara de exclusión de "
                    "facecam_region; se ignora y se recalcula desde cero."
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
    motion_mask: np.ndarray | None,
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

    motion_mask: máscara booleana del tamaño del frame de análisis (True =
    incluir ese píxel), o None para analizar el frame completo -- ver
    _build_motion_exclusion_mask. Si se da, la magnitud media de cada
    muestra se calcula SOLO sobre los píxeles con mask=True, para que
    excluir facecam_region no diluya la sensibilidad al movimiento real
    del resto del frame (si en vez de esto se promediara sobre el frame
    completo poniendo a cero los píxeles excluidos, un mismo movimiento en
    la zona de contenido puntuaría más bajo cuanto más grande fuera
    facecam_region, sin motivo).
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
                mag_mean = float(np.mean(magnitude[motion_mask])) if motion_mask is not None else float(np.mean(magnitude))
                mag_full_res_equiv = mag_mean * magnitude_scale_correction

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

    detect_cuts_config = config.get("detect_cuts", {})
    motion_mask = None
    if detect_cuts_config.get("exclude_facecam_from_motion", True):
        analysis_w, analysis_h = analysis_size if analysis_size else (orig_width, orig_height)
        motion_mask = _build_motion_exclusion_mask(
            config.get("facecam_region"), orig_width, orig_height, analysis_w, analysis_h
        )

    # El checkpoint solo es válido para reanudar si, además de tratarse del
    # mismo vídeo y el mismo muestreo/resolución de análisis (ver
    # _load_motion_checkpoint), se calculó con la MISMA máscara de exclusión
    # de facecam_region -- si no, mezclaría muestras antiguas (calculadas
    # sobre el frame completo) con muestras nuevas (excluyendo la webcam) en
    # una misma serie, silenciosamente inconsistente. Por eso el checkpoint
    # se carga aquí, ya con motion_mask calculado, en vez de al principio de
    # la función.
    resume = _load_motion_checkpoint(checkpoint_path, video_stat, motion_mask)
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

    interval_frames = max(1, round(_MOTION_SAMPLE_INTERVAL_SECONDS * fps))
    # CAP_PROP_FRAME_COUNT puede ser poco fiable según el contenedor; solo
    # se usa como estimación best-effort para el % de progreso y la
    # cadencia de checkpoints, nunca para lógica de corrección.
    expected_samples = int(total_frames / interval_frames) if total_frames and total_frames > 0 else None
    checkpoint_every = max(1, expected_samples // 10) if expected_samples else _MOTION_CHECKPOINT_FALLBACK_EVERY_SAMPLES

    logger.info(
        "Calculando serie de movimiento visual (optical flow, muestreo cada %.2fs, análisis a %s%s%s)...",
        _MOTION_SAMPLE_INTERVAL_SECONDS,
        f"{analysis_size[0]}x{analysis_size[1]}" if analysis_size else f"{orig_width}x{orig_height} (sin reescalar)",
        f", ~{expected_samples} muestra(s) esperada(s)" if expected_samples else "",
        ", excluyendo facecam_region" if motion_mask is not None else "",
    )

    out_queue: "queue.Queue" = queue.Queue()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=_motion_worker,
        args=(
            str(input_path), start_frame_idx, start_next_sample, interval_frames, fps,
            analysis_size, magnitude_scale_correction, motion_mask, out_queue, stop_event,
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
                _save_motion_checkpoint(
                    checkpoint_path, times, magnitudes, frame_idx, next_sample_frame, video_stat, motion_mask
                )
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


def _build_motion_exclusion_mask(
    facecam_region: dict | None, orig_width: int, orig_height: int, analysis_width: int, analysis_height: int
) -> np.ndarray | None:
    """
    Máscara booleana (True = incluir ese píxel al calcular movimiento) del
    tamaño del frame de ANÁLISIS (tras el reescalado a
    _MOTION_ANALYSIS_HEIGHT_PX, ver compute_motion_timeseries), con
    facecam_region excluida (False).

    Por qué: sin esto, el propio streamer moviéndose en su webcam (gestos,
    risas, hablar) cuenta como "movimiento en pantalla" y puede evitar que
    se corte un silencio real donde no pasa nada en el contenido (el
    juego/pantalla compartida) -- justo el caso contrario al que CLAUDE.md
    protege ("silencio + acción visual NO se corta": la acción tiene que
    ser del contenido, no del propio streamer reaccionando en su recuadro).

    facecam_region está en píxeles del frame ORIGINAL (orig_width x
    orig_height); se reescala proporcionalmente (mismo factor que el resto
    del frame al pasar a analysis_width x analysis_height, preservando
    aspect ratio) antes de aplicarla, reutilizando facecam_crop_box (de
    src.common.face_detection) para el recorte/clamp a los límites del
    frame de análisis.

    Returns:
        None si no hay facecam_region configurado, o si excluirla dejaría
        la máscara vacía (facecam_region cubre el frame de análisis
        entero -- caso límite de configuración; se prefiere analizar el
        frame completo antes que no analizar nada).
    """
    if not facecam_region or orig_height <= 0:
        return None
    scale = analysis_height / orig_height
    scaled_region = {
        "x": facecam_region.get("x", 0) * scale,
        "y": facecam_region.get("y", 0) * scale,
        "w": facecam_region.get("w", 0) * scale,
        "h": facecam_region.get("h", 0) * scale,
    }
    x0, y0, x1, y1 = facecam_crop_box(scaled_region, analysis_width, analysis_height)
    mask = np.ones((analysis_height, analysis_width), dtype=bool)
    mask[y0:y1, x0:x1] = False
    if not mask.any():
        logger.warning(
            "facecam_region cubre todo el frame de análisis; no se puede excluir del cálculo de "
            "movimiento visual, se analiza el frame completo."
        )
        return None
    return mask


def _confirm_window_at(
    samples: list[tuple[float, bool]],
    end_index: int,
    confirm_window_seconds: float,
    min_detection_ratio: float,
) -> float | None:
    """
    Mira hacia atrás desde samples[end_index] (ordenados por tiempo) una
    ventana de hasta confirm_window_seconds de duración. Si la ventana
    cubre al menos la mitad de ese tiempo (para no confirmar con una o dos
    muestras sueltas nada más empezar, antes de que la ventana tenga
    sentido estadístico) y al menos min_detection_ratio de sus muestras
    tienen cara detectada, devuelve el timestamp de la PRIMERA muestra CON
    detección dentro de esa ventana -- el instante en que la cara empezó a
    aparecer de verdad, no el de la muestra que dispara la confirmación.
    None si la ventana no cumple los requisitos.
    """
    t_end = samples[end_index][0]
    start_index = end_index
    while start_index > 0 and t_end - samples[start_index - 1][0] < confirm_window_seconds:
        start_index -= 1
    window = samples[start_index:end_index + 1]

    if t_end - window[0][0] < confirm_window_seconds / 2:
        return None

    ratio = sum(1 for _, detected in window if detected) / len(window)
    if ratio < min_detection_ratio:
        return None

    return next(t for t, detected in window if detected)


def _confirm_intro_end_time(
    samples: list[tuple[float, bool]],
    confirm_window_seconds: float,
    min_detection_ratio: float,
) -> float | None:
    """
    Primer instante (recorriendo `samples` en orden) en que
    _confirm_window_at confirma que la cara ya apareció de verdad. Envoltorio
    de conveniencia sobre una lista completa de muestras ya recogida --
    usado en tests para verificar la lógica de confirmación de forma
    aislada, sin tener que simular la lectura de vídeo.
    """
    for i in range(len(samples)):
        confirmed = _confirm_window_at(samples, i, confirm_window_seconds, min_detection_ratio)
        if confirmed is not None:
            return confirmed
    return None


def detect_intro_face_cut(video_id: str, config: dict, detector=frame_has_face) -> dict | None:
    """
    Muestrea data/raw/<video_id>.mp4 cada
    config['detect_cuts']['intro_face_sample_interval_seconds'] desde el
    inicio (recorrido secuencial con cap.grab() para saltar los frames no
    muestreados, sin seeks -- misma razón de rendimiento que
    compute_motion_timeseries: un seek por muestra sería carísimo con un
    GOP largo), buscando la primera vez que hay una cara real dentro de
    config['facecam_region'] (detector de caras ligero de
    src.common.face_detection -- cv2.FaceDetectorYN -- sobre el recorte
    pequeño de esa región, no el frame completo).

    Para evitar falsos negativos puntuales (un parpadeo, un frame raro) no
    basta una única detección: se exige que al menos
    intro_face_min_detection_ratio de las muestras dentro de una ventana de
    intro_face_confirm_window_seconds tengan cara detectada antes de
    considerar que "ya apareció de verdad" (ver _confirm_window_at). El
    instante devuelto es el de la PRIMERA muestra detectada dentro de esa
    ventana ya confirmada.

    Si config['detect_cuts']['trim_intro'] es false, o no hay
    facecam_region configurado, no se busca nada y se devuelve None sin
    abrir el vídeo. Si no se confirma ninguna aparición de cara dentro de
    los primeros intro_face_max_search_seconds (por defecto 15 min),
    TAMPOCO se recorta nada -- evita cortar el vídeo entero por error si
    el detector falla o el vídeo no tiene cara en facecam_region -- y se
    loguea un aviso claro.

    `detector` es inyectable (por defecto frame_has_face, el detector de
    caras real) para poder testear el resto de la lógica (muestreo
    secuencial, ventana de confirmación, límite de búsqueda) con un
    detector simulado, sin depender de que un modelo entrenado en caras
    reales reconozca marcadores sintéticos dibujados a mano.

    Returns:
        {"end": float} con el instante (segundos, línea de tiempo del
        vídeo ORIGINAL) en que se confirma la aparición de la cara, o None
        si trim_intro está desactivado, falta facecam_region, o no se
        confirma ninguna aparición dentro del límite de búsqueda.
    """
    detect_cuts_config = config.get("detect_cuts", {})
    if not detect_cuts_config.get("trim_intro", True):
        logger.info("trim_intro desactivado en config; no se recorta la intro.")
        return None

    facecam_region = config.get("facecam_region")
    if not facecam_region:
        logger.warning(
            "No hay facecam_region configurado; no se puede recortar la intro por detección de cara."
        )
        return None

    sample_interval = float(detect_cuts_config.get("intro_face_sample_interval_seconds", 1.5))
    confirm_window = float(detect_cuts_config.get("intro_face_confirm_window_seconds", 8.0))
    min_ratio = float(detect_cuts_config.get("intro_face_min_detection_ratio", 0.7))
    max_search_seconds = float(detect_cuts_config.get("intro_face_max_search_seconds", 900.0))

    input_path = _raw_video_path(video_id, config)
    cap = cv2.VideoCapture(str(input_path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        logger.warning("No se pudo abrir %s para detectar la intro; no se recorta.", input_path)
        return None

    logger.info(
        "Buscando primera aparición fiable de cara en facecam_region (muestreo cada %.1fs, "
        "ventana de confirmación %.1fs >= %.0f%%, máx. %.0fs de búsqueda)...",
        sample_interval, confirm_window, min_ratio * 100, max_search_seconds,
    )

    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0:
            fps = 30.0
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        crop_box = facecam_crop_box(facecam_region, frame_width, frame_height)
        crop_size = (crop_box[2] - crop_box[0], crop_box[3] - crop_box[1])
        face_detector = load_face_detector(crop_size)

        interval_frames = max(1, round(sample_interval * fps))
        max_search_frames = round(max_search_seconds * fps)

        samples: list[tuple[float, bool]] = []
        frame_idx = 0
        next_sample_frame = 0
        confirmed_time: float | None = None

        while frame_idx <= max_search_frames:
            if frame_idx >= next_sample_frame:
                ok, frame = cap.read()
                if not ok:
                    break
                t = frame_idx / fps
                detected = detector(frame, crop_box, face_detector)
                samples.append((t, detected))
                next_sample_frame = frame_idx + interval_frames
                frame_idx += 1

                confirmed_time = _confirm_window_at(samples, len(samples) - 1, confirm_window, min_ratio)
                if confirmed_time is not None:
                    break
            else:
                if not cap.grab():
                    break
                frame_idx += 1
    finally:
        cap.release()

    if confirmed_time is None:
        logger.warning(
            "No se detectó ninguna aparición fiable de cara en facecam_region dentro de los "
            "primeros %.0fs; no se recorta la intro (para no arriesgarse a cortar el vídeo "
            "entero por un fallo del detector).",
            max_search_seconds,
        )
        return None

    logger.info("Cara detectada de forma fiable en facecam_region a partir de %.2fs.", confirmed_time)
    return {"end": confirmed_time}


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


# Prioridad de `type` al fusionar cortes solapados (índice más bajo = gana
# la fusión, ver _merge_overlapping_cuts): "intro" es una señal de corte
# independiente de silencio+movimiento (ver detect_intro_face_cut) y vale
# la pena poder distinguirla después en cuts.json, así que nunca debe
# quedar enmascarada por un candidato de silencio/muletilla con el que
# solape -- de lo contrario el instante del corte sigue siendo correcto,
# pero se pierde la etiqueta que dice "esto es el recorte de intro". Entre
# "filler" y "silence" se mantiene el criterio ya existente (silencio
# domina) invertido a "filler domina", ya que una muletilla implica habla
# real (no silencio de verdad) y es la señal más específica de las dos.
_CUT_TYPE_PRIORITY = ["intro", "filler", "silence"]


def _cut_type_rank(cut_type: str) -> int:
    try:
        return _CUT_TYPE_PRIORITY.index(cut_type)
    except ValueError:
        return len(_CUT_TYPE_PRIORITY)


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


def run(video_id: str, config: dict) -> dict:
    """
    Combina las tres señales de silencio/muletilla+movimiento, aplica el
    filtro de contexto visual, añade el recorte de intro por detección de
    cara (independiente, ver detect_intro_face_cut), y produce la lista
    final de cortes.

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

    # Recorte de intro (independiente de silencio+movimiento: se aplica
    # siempre que se detecte con fiabilidad una cara en facecam_region,
    # sin necesidad de que el tramo previo también sea silencio -- ver
    # detect_intro_face_cut). Solo se margina el borde final (el corte ya
    # empieza en el instante 0 del vídeo, no hay "antes" que proteger).
    logger.info("Buscando intro sin cara para recortar...")
    intro_face_cut = detect_intro_face_cut(video_id, config)
    if intro_face_cut is not None:
        intro_end = max(0.0, intro_face_cut["end"] - cut_margin_seconds)
        if intro_end > 0:
            margined.append({
                "start": 0.0,
                "end": intro_end,
                "type": "intro",
                "reason": "intro sin cara detectada en facecam_region",
            })
            logger.info(
                "Recorte de intro: 0.00s-%.2fs (cara detectada de forma fiable a partir de %.2fs)",
                intro_end, intro_face_cut["end"],
            )

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
