"""
Test sintético (sin red, sin dependencias externas) de
src/thumbnail/run.py -- versión drásticamente simplificada (2026-08-09):
el módulo YA NO COMPONE NADA, solo extrae frames candidatos reales, a
resolución completa, de los momentos de mayor movimiento del vídeo.

Cubre:
1. _select_candidate_frames detecta ráfagas de movimiento conocidas
   dispersas por un vídeo sintético, incluyendo una ráfaga dentro de lo
   que en las versiones anteriores de este módulo habría sido
   `facecam_region` -- confirma que la puntuación ahora se calcula sobre
   el FRAME COMPLETO, sin excluir ninguna zona (a diferencia de
   _select_gameplay_frame de las versiones anteriores).
2. --min-gap-seconds: dos ráfagas de movimiento cercanas en el tiempo
   (más cerca entre sí que min_gap_seconds) colapsan en un solo
   candidato (el de mayor movimiento de las dos); una tercera ráfaga
   lejana se conserva como candidato aparte.
3. Los candidatos dentro de un tramo ya marcado en
   data/cuts/<video_id>/cuts.json se descartan.
4. run(): guarda thumbnail_candidate_1.png..._N.png (1-indexado, orden
   cronológico) a la resolución COMPLETA del vídeo de origen (sin
   redimensionar); limpia candidatos de una ejecución anterior con más
   candidatos que la actual; NUNCA crea ni toca thumbnail.png;
   config['thumbnail']['enabled']=False no genera ningún archivo.

Uso:
    cd <repo_root>
    python tests/test_thumbnail.py

Genera sus propios vídeos sintéticos en un directorio temporal (no toca
data/ ni llama a ninguna API o red). Código de salida 0 si todas las
comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.thumbnail.run import _select_candidate_frames, run  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


WIDTH, HEIGHT = 420, 200
FPS = 10.0
# Región que en las versiones anteriores de este módulo era `facecam_region` --
# usada aquí SOLO para comprobar que ya no se excluye de nada.
_OLD_FACECAM_REGION_BOX = (20, 20, 180, 140)  # x0, y0, x1, y1
_RNG = np.random.default_rng(20260809)
_BACKGROUND = _RNG.integers(0, 40, size=(HEIGHT, WIDTH, 3), dtype=np.uint8)


def _write_motion_video(path: Path, duration_s: float, bursts: list[dict]) -> None:
    """
    Vídeo con textura de fondo fija y, dentro de cada ráfaga de `bursts`
    (dict con "window": (start, end), "box": (x0,y0,x1,y1) contenedor del
    marcador que rebota, "max_offset": amplitud del rebote), un marcador
    blanco que se mueve SOLO durante esa ventana -- fuera de todas las
    ráfagas, el frame es la textura de fondo sin cambios (movimiento ~0).
    """
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    n_frames = int(duration_s * FPS)
    object_size = (24, 24)

    def _offset_at(t: float, burst: dict) -> int | None:
        start, end = burst["window"]
        if not (start <= t < end):
            return None
        local = (t - start) / (end - start)
        period = 2.0
        phase = (local * period) % period
        frac = phase if phase <= 1.0 else 2.0 - phase
        return round(frac * burst["max_offset"])

    for i in range(n_frames):
        t = i / FPS
        frame = _BACKGROUND.copy()
        for burst in bursts:
            offset = _offset_at(t, burst)
            if offset is None:
                continue
            x0, y0, x1, y1 = burst["box"]
            margin = 6
            max_extent = max(0, (x1 - x0 - 2 * margin) - object_size[0])
            actual_offset = min(offset, max_extent)
            x, y = x0 + margin + actual_offset, y0 + margin
            frame[y:y + object_size[1], x:x + object_size[0]] = 255
        writer.write(frame)
    writer.release()


def main() -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="thumbnail_test_"))
    try:
        config = {
            "paths": {"raw": str(work_dir), "output": str(work_dir)},
            "thumbnail": {"candidate_sample_count": 40},
        }

        print("=== _select_candidate_frames: detecta ráfagas dispersas, incluida una dentro de 'facecam_region' ===")
        video_id = "motion_test"
        # A: dentro de la región que antes era facecam_region (x0-x1: 20-180, y0-y1: 20-140).
        # B: fuera, más fuerte que C.
        # C: fuera, cerca de B en el tiempo (< min_gap_seconds de diferencia) pero más débil.
        # D: fuera, lejos de todas las demás.
        bursts = [
            {"name": "A", "window": (10.0, 12.0), "box": _OLD_FACECAM_REGION_BOX, "max_offset": 60},
            {"name": "B", "window": (30.0, 32.0), "box": (250, 30, 350, 130), "max_offset": 60},
            {"name": "C", "window": (33.0, 35.0), "box": (250, 30, 350, 130), "max_offset": 20},
            {"name": "D", "window": (50.0, 52.0), "box": (250, 30, 350, 130), "max_offset": 60},
        ]
        _write_motion_video(work_dir / f"{video_id}.mp4", duration_s=60.0, bursts=bursts)

        candidates = _select_candidate_frames(video_id, config, num_candidates=3, min_gap_seconds=10.0)
        candidate_times = [t for t, _ in candidates]

        check(
            "se devuelven como mucho 3 candidatos, en orden cronológico",
            len(candidates) <= 3 and candidate_times == sorted(candidate_times),
            f"times={candidate_times}",
        )
        near_a = any(9.0 <= t <= 13.0 for t in candidate_times)
        check(
            "se detecta la ráfaga A, DENTRO de lo que antes era facecam_region (ya no se excluye nada)",
            near_a,
            f"times={candidate_times}",
        )
        near_b = any(29.0 <= t <= 33.0 for t in candidate_times)
        near_c = any(32.0 <= t <= 36.0 for t in candidate_times)
        check(
            "de B y C (a < 10s de diferencia, min_gap_seconds=10), solo sobrevive UNO -- el más fuerte (B)",
            near_b and not near_c,
            f"near_b={near_b}, near_c={near_c}, times={candidate_times}",
        )
        near_d = any(49.0 <= t <= 53.0 for t in candidate_times)
        check(
            "se detecta la ráfaga D, lejos de las demás",
            near_d,
            f"times={candidate_times}",
        )

        print("=== _select_candidate_frames: descarta candidatos dentro de un tramo ya cortado (cuts.json) ===")
        video_id2 = "motion_cuts_test"
        bursts2 = [
            {"name": "cut", "window": (10.0, 12.0), "box": (250, 30, 350, 130), "max_offset": 60},
            {"name": "keep", "window": (40.0, 42.0), "box": (250, 30, 350, 130), "max_offset": 40},
        ]
        _write_motion_video(work_dir / f"{video_id2}.mp4", duration_s=60.0, bursts=bursts2)
        cuts_dir = work_dir / video_id2
        cuts_dir.mkdir(exist_ok=True)
        with open(cuts_dir / "cuts.json", "w", encoding="utf-8") as f:
            json.dump([{"start": 8.0, "end": 14.0, "type": "silence", "reason": "s"}], f)
        config_with_cuts = {**config, "paths": {**config["paths"], "cuts": str(work_dir)}}

        candidates2 = _select_candidate_frames(video_id2, config_with_cuts, num_candidates=1, min_gap_seconds=5.0)
        check(
            "el único candidato devuelto viene del tramo NO cortado (~40-42s), no del cortado (~10-12s)",
            len(candidates2) == 1 and 38.0 <= candidates2[0][0] <= 44.0,
            f"candidates={[t for t, _ in candidates2]}",
        )

        print("=== run(): guarda thumbnail_candidate_N.png a resolución completa, sin tocar thumbnail.png ===")
        output_dir = work_dir / video_id
        result = run(video_id, config, num_candidates=3, min_gap_seconds=10.0)
        check(
            "run() devuelve exactamente las rutas de los candidatos guardados",
            len(result["candidate_paths"]) == len(candidates) and len(result["candidate_paths"]) > 0,
            f"paths={result['candidate_paths']}",
        )
        for i, path_str in enumerate(result["candidate_paths"], start=1):
            path = Path(path_str)
            check(f"candidato {i}: nombre de archivo esperado", path.name == f"thumbnail_candidate_{i}.png")
            check(f"candidato {i}: el archivo existe", path.exists())
            img = cv2.imread(str(path))
            check(
                f"candidato {i}: guardado a la resolución COMPLETA del vídeo de origen (sin redimensionar)",
                img is not None and img.shape[:2] == (HEIGHT, WIDTH),
                f"shape={None if img is None else img.shape}",
            )
        check(
            "run() NUNCA crea ni toca data/output/<video_id>/thumbnail.png",
            not (output_dir / "thumbnail.png").exists(),
        )

        print("=== run(): limpia candidatos de una ejecución anterior con más candidatos que la actual ===")
        run(video_id, config, num_candidates=1, min_gap_seconds=10.0)
        remaining = sorted(output_dir.glob("thumbnail_candidate_*.png"))
        check(
            "tras pedir 1 candidato, no quedan restos de la ejecución anterior con más candidatos",
            [p.name for p in remaining] == ["thumbnail_candidate_1.png"],
            f"remaining={[p.name for p in remaining]}",
        )

        print("=== run() con config['thumbnail']['enabled']=False: no genera ningún archivo ===")
        video_id3 = "disabled_test"
        _write_motion_video(work_dir / f"{video_id3}.mp4", duration_s=20.0, bursts=[])
        config_disabled = {**config, "thumbnail": {**config["thumbnail"], "enabled": False}}
        result_disabled = run(video_id3, config_disabled)
        check(
            "candidate_paths vacío y no se crea ningún archivo",
            result_disabled["candidate_paths"] == []
            and not list((work_dir / video_id3).glob("thumbnail_candidate_*.png")) if (work_dir / video_id3).exists() else True,
            f"result={result_disabled}",
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: extracción de frames candidatos por movimiento, deduplicado por tiempo, filtro de cortes y guardado se comportan como se espera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
