"""
Extracción de frames candidatos a miniatura de YouTube.

Simplificado drásticamente (2026-08-09, tercera versión de este módulo --
las dos anteriores componían la miniatura entera de forma automática:
primero con paneles con borde + titular vía Claude + mejora con Gemini,
después con recorte de sujeto vía rembg + fondo desenfocado + título
quemado. Ambos diseños quedan retirados por completo -- ver
`status.md`/historial de git si hace falta el detalle). ESTE módulo YA NO
COMPONE NADA: se limita a extraer 4-5 frames reales, en la resolución
COMPLETA del vídeo original (sin recortar, sin excluir ninguna zona del
frame, sin redimensionar), de los momentos de mayor movimiento/acción del
directo, y los guarda tal cual como:

    data/output/<video_id>/thumbnail_candidate_1.png
    data/output/<video_id>/thumbnail_candidate_2.png
    ...

El usuario elige uno de esos candidatos, lo compone a mano (título,
recortes, lo que quiera) con su propio editor/canvas, y guarda el
resultado final como data/output/<video_id>/thumbnail.png -- la ruta que
consume src/publish/youtube.py al subir el vídeo (esa ruta/nombre NO
cambia; ver _thumbnail_path en publish/youtube.py, que ahora lanza un
error claro si ese archivo no existe todavía en vez de subir sin
miniatura o fallar de forma confusa).

Selección de candidatos (_select_candidate_frames): muestrea
config['thumbnail']['candidate_sample_count'] puntos uniformemente
repartidos por el vídeo (evitando el primer/último 10%, que suele ser
intro/despedida, y cualquier tramo ya marcado en
data/cuts/<video_id>/cuts.json si existe -- normalmente pantallas de
carga o silencios sin acción real, poco interesantes para una miniatura)
y puntúa cada uno por MOVIMIENTO: diferencia media de píxeles en gris
respecto al frame ~0.5s antes -- la misma señal ligera (diferencia de
píxeles, no optical flow denso) que ya usaban las versiones anteriores de
este módulo para elegir el frame de gameplay, y el mismo concepto de
"movimiento visual" que usa detect_cuts (allí con optical flow Farneback,
mucho más caro -- aquí se mantiene la versión ligera a propósito: extraer
candidatos debe ser cosa de segundos, no los minutos que tarda el análisis
de movimiento completo de detect_cuts sobre una grabación de 1-2h).
A diferencia de las versiones anteriores, la puntuación se calcula sobre
el FRAME COMPLETO -- sin excluir facecam_region ni recortar nada: el
streamer y el juego deben poder aparecer juntos en el candidato, tal cual
se grabó. De los candidatos con más movimiento se eligen los
`num_candidates` de mayor puntuación que queden separados entre sí por al
menos `min_gap_seconds` (criterio greedy: se recorren de mayor a menor
puntuación, descartando cualquiera que quede demasiado cerca en el tiempo
de uno ya elegido) -- evita quedarse con varios frames casi idénticos del
mismo instante de acción.

config['thumbnail']['enabled'] (por defecto true) permite desactivar el
módulo entero sin tocar código: si es false, run() no hace nada (ni abre
el vídeo) y lo deja claro en el log.

Intro grabado aparte (2026-08-10, ver CLAUDE.md "Intro grabado aparte"):
este módulo extrae candidatos ÚNICAMENTE de data/raw/<video_id>.mp4 (el
contenido principal del stream); data/output/<video_id>/intro.mp4, si
existe, nunca se abre aquí -- queda excluido de los candidatos por
construcción, sin necesidad de ningún filtro adicional (igual que ya
queda excluido el outro, que tampoco es data/raw/<video_id>.mp4).
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import cv2
import numpy as np

from src.common import db
from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

_SAMPLE_MARGIN_RATIO = 0.1  # evita el primer/último 10% del vídeo (intro/despedida)
_MOTION_PROBE_GAP_SECONDS = 0.5  # separación entre el frame candidato y el frame "anterior" para medir movimiento

_DEFAULT_NUM_CANDIDATES = 5
_DEFAULT_MIN_GAP_SECONDS = 60.0
_DEFAULT_CANDIDATE_SAMPLE_COUNT = 60  # nº de puntos muestreados por movimiento ANTES de aplicar min_gap_seconds


def _raw_video_path(video_id: str, config: dict) -> Path:
    path = (REPO_ROOT / config["paths"]["raw"]).resolve() / f"{video_id}.mp4"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe el vídeo de entrada para '{video_id}': {path}. "
            "Ejecuta primero la etapa de ingesta (python -m src.ingest.run --file <ruta_al_mp4_de_obs>)."
        )
    return path


def _output_dir(video_id: str, config: dict) -> Path:
    out_dir = (REPO_ROOT / config["paths"]["output"]).resolve() / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _load_cut_intervals(video_id: str, config: dict) -> list[tuple[float, float]]:
    """
    Tramos ya marcados para eliminar en data/cuts/<video_id>/cuts.json
    (silencio, muletilla, o la intro sin cara -- ver CLAUDE.md), si esa
    etapa ya se ha ejecutado para este vídeo. Se usan para descartar
    candidatos que caigan ahí -- típicamente pantallas de carga o
    silencios sin acción real, poco interesantes para una miniatura.
    Lista vacía si detect_cuts no se ha ejecutado todavía: no bloquea la
    extracción de candidatos, solo no filtra nada.
    """
    cuts_dir = config.get("paths", {}).get("cuts")
    if not cuts_dir:
        return []
    path = (REPO_ROOT / cuts_dir).resolve() / video_id / "cuts.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        cuts = json.load(f)
    return [(float(c["start"]), float(c["end"])) for c in cuts]


def _in_any_interval(t: float, intervals: list[tuple[float, float]]) -> bool:
    return any(start <= t < end for start, end in intervals)


def _select_candidate_frames(
    video_id: str, config: dict, num_candidates: int, min_gap_seconds: float
) -> list[tuple[float, np.ndarray]]:
    """
    Escanea data/raw/<video_id>.mp4 y devuelve hasta `num_candidates`
    frames (BGR, resolución COMPLETA, SIN recortar ni excluir ninguna
    zona) de los momentos de mayor movimiento, separados entre sí por al
    menos `min_gap_seconds` -- ver el docstring del módulo para el
    porqué de cada decisión de diseño.

    Returns:
        [(timestamp_s, frame_bgr), ...] ordenados CRONOLÓGICAMENTE (no
        por puntuación de movimiento).
    """
    input_path = _raw_video_path(video_id, config)
    cap = cv2.VideoCapture(str(input_path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir {input_path} para extraer frames candidatos.")

    try:
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = total_frames / fps if fps > 0 else 0.0

        sample_count = max(
            num_candidates,
            int(config.get("thumbnail", {}).get("candidate_sample_count", _DEFAULT_CANDIDATE_SAMPLE_COUNT)),
        )
        lo = duration * _SAMPLE_MARGIN_RATIO
        hi = duration * (1 - _SAMPLE_MARGIN_RATIO)
        if duration <= 0 or hi <= lo:
            sample_times = [0.0]
        elif sample_count == 1:
            sample_times = [(lo + hi) / 2]
        else:
            sample_times = [lo + (hi - lo) * i / (sample_count - 1) for i in range(sample_count)]

        cut_intervals = _load_cut_intervals(video_id, config)
        if cut_intervals:
            filtered = [t for t in sample_times if not _in_any_interval(t, cut_intervals)]
            sample_times = filtered or sample_times

        scored: list[tuple[float, float, np.ndarray]] = []  # (motion, t, frame)
        for t in sample_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, (t - _MOTION_PROBE_GAP_SECONDS)) * 1000)
            ok_a, frame_a = cap.read()
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok_b, frame_b = cap.read()
            if not (ok_a and ok_b):
                continue

            gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
            gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
            if gray_a.shape != gray_b.shape:
                continue

            motion = float(cv2.absdiff(gray_a, gray_b).mean())
            scored.append((motion, t, frame_b))

        # Mayor movimiento primero; se van aceptando mientras no queden a
        # menos de min_gap_seconds de un candidato ya elegido.
        scored.sort(key=lambda item: -item[0])
        selected: list[tuple[float, np.ndarray]] = []
        for _motion, t, frame in scored:
            if all(abs(t - chosen_t) >= min_gap_seconds for chosen_t, _ in selected):
                selected.append((t, frame))
            if len(selected) >= num_candidates:
                break

        selected.sort(key=lambda item: item[0])  # orden cronológico para guardar/mostrar
        return selected
    finally:
        cap.release()


def run(
    video_id: str,
    config: dict,
    num_candidates: int = _DEFAULT_NUM_CANDIDATES,
    min_gap_seconds: float = _DEFAULT_MIN_GAP_SECONDS,
) -> dict:
    """
    Extrae hasta `num_candidates` frames reales (resolución completa, sin
    recortar) de momentos de alto movimiento en data/raw/<video_id>.mp4,
    separados entre sí por al menos `min_gap_seconds`, y los guarda como
    data/output/<video_id>/thumbnail_candidate_<i>.png (1-indexado, orden
    cronológico).

    IMPORTANTE: este módulo NO genera ni toca
    data/output/<video_id>/thumbnail.png -- esa imagen la crea el usuario
    a mano a partir de uno de estos candidatos (ver docstring del
    módulo). publish/youtube.py sigue leyendo esa ruta sin cambios, y
    ahora falla con un mensaje claro si no existe todavía.

    Returns:
        dict con {"video_id", "candidate_paths": [str, ...]} (lista
        vacía si config['thumbnail']['enabled'] es false).
    """
    thumbnail_config = config.get("thumbnail", {})
    if not thumbnail_config.get("enabled", True):
        logger.info(
            "config['thumbnail']['enabled'] es false; no se extrae ningún candidato para '%s'.", video_id
        )
        return {"video_id": video_id, "candidate_paths": []}

    logger.info(
        "Buscando hasta %d frame(s) candidato(s) de alto movimiento para '%s' (separados >= %.0fs)...",
        num_candidates, video_id, min_gap_seconds,
    )
    candidates = _select_candidate_frames(video_id, config, num_candidates, min_gap_seconds)

    output_dir = _output_dir(video_id, config)
    for stale in output_dir.glob("thumbnail_candidate_*.png"):
        stale.unlink()

    candidate_paths: list[str] = []
    for i, (t, frame) in enumerate(candidates, start=1):
        path = output_dir / f"thumbnail_candidate_{i}.png"
        cv2.imwrite(str(path), frame)
        candidate_paths.append(str(path))
        logger.info("Candidato %d/%d guardado (t=%.2fs): %s", i, len(candidates), t, path)

    if not candidate_paths:
        logger.warning("No se pudo extraer ningún frame candidato para '%s'.", video_id)
    else:
        logger.info(
            "%d candidato(s) guardado(s) en %s. Elige uno, edítalo si quieres, y guárdalo como "
            "thumbnail.png antes de publicar.",
            len(candidate_paths), output_dir,
        )

    db.set_status(video_id, "thumbnail_candidates_generated")

    return {"video_id": video_id, "candidate_paths": candidate_paths}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Extraer frames candidatos a miniatura de un vídeo")
    parser.add_argument("--video-id", required=True)
    parser.add_argument(
        "--num-candidates", type=int, default=_DEFAULT_NUM_CANDIDATES,
        help=f"Nº de frames candidatos a extraer (default {_DEFAULT_NUM_CANDIDATES}).",
    )
    parser.add_argument(
        "--min-gap-seconds", type=float, default=_DEFAULT_MIN_GAP_SECONDS,
        help=f"Separación mínima en segundos entre candidatos elegidos (default {_DEFAULT_MIN_GAP_SECONDS:.0f}s).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    run(args.video_id, config, num_candidates=args.num_candidates, min_gap_seconds=args.min_gap_seconds)


if __name__ == "__main__":
    _cli()
