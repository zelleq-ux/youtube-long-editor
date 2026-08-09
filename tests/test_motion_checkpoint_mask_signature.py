"""
Test unitario (sin vídeo) de la invalidación de checkpoints de movimiento
visual cuando cambia la máscara de exclusión de facecam_region.

Motivación: compute_motion_timeseries (src/detect_cuts/run.py) puede
reanudar un cálculo interrumpido desde
data/cuts/<video_id>/_motion_checkpoint.npz. Antes de añadir
exclude_facecam_from_motion, el checkpoint ya validaba que el vídeo, el
intervalo de muestreo y la resolución de análisis coincidieran -- pero no
sabía nada sobre si las muestras se calcularon con facecam_region excluida
o no. Sin esa comprobación, activar/desactivar exclude_facecam_from_motion
(o cambiar facecam_region) entre una ejecución interrumpida y su reanudación
mezclaría en una misma serie temporal muestras calculadas con máscaras
distintas, de forma silenciosa. Fix: _motion_mask_signature codifica la
máscara (o su ausencia) en el checkpoint, y _load_motion_checkpoint la
compara contra la máscara de la ejecución actual antes de reanudar.

Uso:
    cd <repo_root>
    python tests/test_motion_checkpoint_mask_signature.py

Sin dependencias de vídeo/ffmpeg: termina en menos de un segundo. Código
de salida 0 si todas las comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.detect_cuts.run import _load_motion_checkpoint, _save_motion_checkpoint  # noqa: E402


class _FakeStat:
    st_mtime = 1000.0
    st_size = 12345


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


work_dir = Path(tempfile.mkdtemp(prefix="motion_checkpoint_sig_test_"))
try:
    checkpoint_path = work_dir / "_motion_checkpoint.npz"
    video_stat = _FakeStat()

    mask_a = np.ones((10, 10), dtype=bool)
    mask_a[2:5, 2:5] = False  # excluye un bloque 3x3

    mask_b = np.ones((10, 10), dtype=bool)
    mask_b[0:3, 0:3] = False  # excluye un bloque distinto (misma forma, otra región)

    _save_motion_checkpoint(checkpoint_path, [1.0, 2.0], [0.1, 0.2], 10, 20, video_stat, mask_a)

    resume_same_mask = _load_motion_checkpoint(checkpoint_path, video_stat, mask_a)
    check(
        "checkpoint con la MISMA máscara se reanuda",
        resume_same_mask is not None and resume_same_mask["times"] == [1.0, 2.0],
        f"resume={resume_same_mask}",
    )

    resume_different_region = _load_motion_checkpoint(checkpoint_path, video_stat, mask_b)
    check(
        "checkpoint con una máscara DISTINTA (otra región excluida) se invalida",
        resume_different_region is None,
        f"resume={resume_different_region}",
    )

    resume_no_mask = _load_motion_checkpoint(checkpoint_path, video_stat, None)
    check(
        "checkpoint calculado CON máscara se invalida si ahora no hay máscara (exclude desactivado)",
        resume_no_mask is None,
        f"resume={resume_no_mask}",
    )

    # Caso inverso: checkpoint guardado SIN máscara (exclude_facecam_from_motion
    # desactivado, o sin facecam_region) no debe reanudarse si ahora SÍ hay máscara.
    checkpoint_path_no_mask = work_dir / "_motion_checkpoint_no_mask.npz"
    _save_motion_checkpoint(checkpoint_path_no_mask, [5.0], [0.3], 30, 40, video_stat, None)

    resume_none_to_none = _load_motion_checkpoint(checkpoint_path_no_mask, video_stat, None)
    check(
        "checkpoint sin máscara se reanuda si ahora tampoco hay máscara",
        resume_none_to_none is not None and resume_none_to_none["times"] == [5.0],
        f"resume={resume_none_to_none}",
    )

    resume_none_to_mask = _load_motion_checkpoint(checkpoint_path_no_mask, video_stat, mask_a)
    check(
        "checkpoint sin máscara se invalida si ahora SÍ hay máscara",
        resume_none_to_mask is None,
        f"resume={resume_none_to_mask}",
    )
finally:
    shutil.rmtree(work_dir, ignore_errors=True)

if failures:
    print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nOK: los checkpoints de movimiento visual se invalidan correctamente al cambiar la máscara de exclusión.")
sys.exit(0)
