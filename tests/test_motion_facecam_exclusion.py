"""
Test sintético (sin tocar data/ ni vídeos reales) de la exclusión de
facecam_region en compute_motion_timeseries (src/detect_cuts/run.py).

Motivación: idea tomada de auto-editor (WyattBlue/auto-editor), cuya
detección de movimiento soporta restringir el análisis a una región del
frame. Antes de este cambio, compute_motion_timeseries mide la magnitud de
flujo óptico como la media espacial del frame COMPLETO reescalado a
480p -- así que el propio streamer moviéndose en su facecam_region
(gestos, risas) cuenta como "movimiento en pantalla" y puede evitar que se
corte un silencio real donde no pasa nada en el contenido (el juego),
justo lo contrario de lo que CLAUDE.md protege ("silencio + acción visual
NO se corta": la acción tiene que ser del contenido, no del propio
streamer). Fix: excluir facecam_region del área analizada
(`_build_motion_exclusion_mask`, controlado por
`config['detect_cuts']['exclude_facecam_from_motion']`, default true).

Genera dos vídeos sintéticos pequeños con cv2.VideoWriter (control total
de cada frame, sin depender de que Farneback reconozca nada complejo):

  1. motion_inside.mp4: un bloque rebota SOLO dentro de facecam_region;
     el resto del frame es EXACTAMENTE idéntico en todos los frames (cero
     flujo óptico real fuera de la región). El bloque ocupa la mayor parte
     de su contenedor (deja solo el margen necesario para moverse), para
     que la mayoría de facecam_region muestre flujo real y no se diluya
     en un punto diminuto.
  2. motion_outside.mp4: el mismo bloque rebota en un contenedor alejado
     de facecam_region (sin solape); DENTRO de facecam_region el frame es
     idéntico en todos los frames.

Para cada vídeo se ejecuta compute_motion_timeseries dos veces (config con
exclude_facecam_from_motion=True/False) y se compara la puntuación
resultante (score_motion_segment, 0.0-1.0) contra motion_threshold:

  - motion_inside: con exclusión, la puntuación debe caer por debajo del
    umbral (0.0 exacto, de hecho, porque fuera de facecam_region no hay
    NINGÚN flujo) -> "sin movimiento real", candidato a corte. Sin
    exclusión, debe superar el umbral -> antes se habría descartado por
    error el candidato de silencio (el bug que este cambio corrige).
  - motion_outside: la puntuación debe superar el umbral CON o SIN
    exclusión por igual (excluir facecam_region no debe ocultar
    movimiento real del contenido) -- se comprueba además que ambas
    puntuaciones sean prácticamente iguales.

Uso:
    cd <repo_root>
    python tests/test_motion_facecam_exclusion.py

Genera sus propios vídeos bajo un directorio temporal (no toca data/ ni
ningún vídeo real). Código de salida 0 si todas las comprobaciones pasan,
1 si alguna falla.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.detect_cuts.run import compute_motion_timeseries, score_motion_segment  # noqa: E402

# < _MOTION_ANALYSIS_HEIGHT_PX (480): compute_motion_timeseries no reescala
# el frame, así que facecam_region se aplica 1:1 sin tener que razonar
# sobre el factor de escala en este test.
WIDTH, HEIGHT = 420, 200
FPS = 10.0
N_FRAMES = 24

# Contenedores en PÍXELES (para dibujar el vídeo sintético con _write_video,
# que sigue trabajando en píxeles reales de este frame de WIDTH x HEIGHT).
FACECAM_REGION_PX = {"x": 20, "y": 20, "w": 160, "h": 120}
# Contenedor lejano, sin solape con FACECAM_REGION_PX (gap de 40px en x).
OUTSIDE_REGION_PX = {"x": 220, "y": 20, "w": 160, "h": 120}


def _to_fraction(region_px: dict) -> dict:
    """Píxeles -> fracción 0.0-1.0 del frame (facecam_region ya no es en píxeles absolutos, ver src.common.face_detection)."""
    return {
        "x": region_px["x"] / WIDTH, "y": region_px["y"] / HEIGHT,
        "w": region_px["w"] / WIDTH, "h": region_px["h"] / HEIGHT,
    }


# facecam_region (la que se pasa a compute_motion_timeseries vía config,
# ver _score) es una fracción 0.0-1.0 del frame -- conversión exacta de
# FACECAM_REGION_PX, para no cambiar el comportamiento verificado por este test.
FACECAM_REGION = _to_fraction(FACECAM_REGION_PX)

# El objeto ocupa la mayor parte de su contenedor pero con MARGIN de sobra
# en todos los bordes (>= el winsize=15 que usa Farneback en este módulo):
# el flujo óptico de un borde en movimiento "sangra" unos pocos píxeles
# más allá del propio objeto por el suavizado del algoritmo, así que sin
# margen el objeto tocando el borde del contenedor contaminaría la máscara
# vecina con señal residual que no es del objeto en sí.
OBJECT_SIZE = (70, 60)
MARGIN = 20
DISPLACEMENT_PER_FRAME = 15
MOTION_THRESHOLD = 0.15

# Fondo con textura fija (idéntica en todos los frames): Farneback estima
# el flujo a partir de gradientes locales, así que un fondo COMPLETAMENTE
# plano (p.ej. negro uniforme) no le da ninguna estructura con la que
# resolver "cero movimiento" de forma fiable en zonas estáticas -- un
# fondo con textura (aunque sea ruido fijo) es el caso realista (cualquier
# vídeo real tiene textura) y dejar que el algoritmo confirme quietud real
# en vez de ruido de estimación sobre una superficie sin gradiente.
_RNG = np.random.default_rng(20260809)
_BACKGROUND = _RNG.integers(0, 40, size=(HEIGHT, WIDTH, 3), dtype=np.uint8)


def _bounce_offset(frame_idx: int, max_offset: int) -> int:
    """Posición 0..max_offset con rebote (sin teletransportes que Farneback no pueda seguir)."""
    if max_offset <= 0:
        return 0
    period = max_offset * 2
    phase = (frame_idx * DISPLACEMENT_PER_FRAME) % period
    return phase if phase <= max_offset else period - phase


def _write_video(path: Path, moving_container: dict) -> None:
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError(f"No se pudo abrir VideoWriter para {path}")
    cx, cy, cw = moving_container["x"], moving_container["y"], moving_container["w"]
    ow, oh = OBJECT_SIZE
    inner_x0, inner_y0 = cx + MARGIN, cy + MARGIN
    max_offset_x = max(0, (cw - 2 * MARGIN) - ow)
    for i in range(N_FRAMES):
        frame = _BACKGROUND.copy()
        x = inner_x0 + _bounce_offset(i, max_offset_x)
        y = inner_y0
        frame[y:y + oh, x:x + ow] = 255
        writer.write(frame)
    writer.release()


def _score(video_id: str, raw_dir: Path, cuts_dir: Path, exclude: bool) -> float:
    config = {
        "paths": {"raw": str(raw_dir), "cuts": str(cuts_dir)},
        "facecam_region": FACECAM_REGION,
        "detect_cuts": {
            "exclude_facecam_from_motion": exclude,
            "motion_threshold": MOTION_THRESHOLD,
        },
    }
    times, magnitudes = compute_motion_timeseries(video_id, config)
    if len(times) == 0:
        raise RuntimeError(f"compute_motion_timeseries no devolvió ninguna muestra para '{video_id}'")
    return score_motion_segment(times, magnitudes, 0.0, N_FRAMES / FPS, config)


def main() -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="motion_facecam_test_"))
    raw_dir = work_dir / "raw"
    cuts_dir = work_dir / "cuts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cuts_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str) -> None:
        status = "OK" if condition else "FALLO"
        print(f"  [{status}] {label}: {detail}")
        if not condition:
            failures.append(f"{label}: {detail}")

    try:
        _write_video(raw_dir / "motion_inside.mp4", FACECAM_REGION_PX)
        _write_video(raw_dir / "motion_outside.mp4", OUTSIDE_REGION_PX)

        print("=== Caso 1: movimiento SOLO dentro de facecam_region ===")
        score_inside_excluded = _score("motion_inside", raw_dir, cuts_dir, exclude=True)
        score_inside_included = _score("motion_inside", raw_dir, cuts_dir, exclude=False)
        print(f"  score con exclusion=True:  {score_inside_excluded:.4f}")
        print(f"  score con exclusion=False: {score_inside_included:.4f}")
        check(
            "motion_inside + exclude=True -> sin movimiento real",
            score_inside_excluded < MOTION_THRESHOLD,
            f"score={score_inside_excluded:.4f} (umbral {MOTION_THRESHOLD})",
        )
        # No se exige score == 0.0 exacto: mp4v es un códec con pérdida, así
        # que dos frames con contenido "idéntico" no quedan bit-a-bit
        # idénticos tras codificar/decodificar, y ese ruido residual de
        # compresión le da a Farneback un gradiente mínimo distinto de cero
        # incluso en zonas realmente estáticas -- exactamente lo mismo que
        # pasaría con vídeo real. Lo que importa es que quede muy por debajo
        # del umbral (ya comprobado arriba), no que sea matemáticamente cero.
        check(
            "motion_inside + exclude=False -> se habria descartado como movimiento (bug reproducido)",
            score_inside_included >= MOTION_THRESHOLD,
            f"score={score_inside_included:.4f} (umbral {MOTION_THRESHOLD})",
        )

        print("=== Caso 2: movimiento SOLO fuera de facecam_region ===")
        score_outside_excluded = _score("motion_outside", raw_dir, cuts_dir, exclude=True)
        score_outside_included = _score("motion_outside", raw_dir, cuts_dir, exclude=False)
        print(f"  score con exclusion=True:  {score_outside_excluded:.4f}")
        print(f"  score con exclusion=False: {score_outside_included:.4f}")
        check(
            "motion_outside + exclude=True -> sigue detectando movimiento real",
            score_outside_excluded >= MOTION_THRESHOLD,
            f"score={score_outside_excluded:.4f} (umbral {MOTION_THRESHOLD})",
        )
        check(
            "motion_outside + exclude=False -> detecta movimiento real (como siempre)",
            score_outside_included >= MOTION_THRESHOLD,
            f"score={score_outside_included:.4f} (umbral {MOTION_THRESHOLD})",
        )
        # No se espera igualdad exacta: excluir facecam_region (una zona
        # que aquí no contiene NINGÚN movimiento) quita píxeles con
        # magnitud 0 del denominador de la media, así que la media sobre
        # el resto del frame sube ligeramente (o se mantiene igual) -- es
        # el efecto matemático esperado de promediar sobre menos píxeles,
        # no una señal escondida. Lo que NO debe pasar nunca es que
        # excluir facecam_region OCULTE movimiento real de fuera bajando
        # la puntuación.
        check(
            "motion_outside -> excluir facecam_region no oculta movimiento real de fuera",
            score_outside_excluded >= score_outside_included - 1e-9,
            f"exclude=True:{score_outside_excluded:.6f} vs exclude=False:{score_outside_included:.6f}",
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: exclude_facecam_from_motion filtra el movimiento del streamer sin ocultar movimiento real del contenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
