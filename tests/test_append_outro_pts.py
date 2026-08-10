"""
Test de regresión para el bug de discontinuidad de PTS en append_outro
(src/edit/run.py), encontrado el 2026-08-09 contra una ejecución real
completa de dinoblade_1 (la primera vez que ese camino se ejercía contra
un vídeo completo -- ver el docstring de append_outro para el detalle).

Bug original: cuando el outro no coincide en resolución/fps/audio con el
clip principal, la versión anterior de append_outro pasaba el clip
principal COMPLETO junto con el outro por un único filtro `concat` de
ffmpeg para poder recodificar el outro y unirlos en una sola pasada. Eso
reproducía el mismo bug ya documentado para el paso de corte ("el filtro
concat de ffmpeg pierde frames de vídeo en 1080p"): un salto de PTS de
vídeo de ~3448s en mitad del archivo real (audio intacto). El fix
normaliza ÚNICAMENTE el outro (archivo corto) a un fichero aparte y une
ambos con el concat DEMUXER (-c copy), sin tocar nunca el clip principal.

Este test genera dos clips sintéticos pequeños con parámetros
DELIBERADAMENTE distintos (resolución/fps/sample rate/canales, como el
caso real que disparó el bug), llama a append_outro con ellos, y verifica:

1. La duración del resultado es la suma de ambos clips (con tolerancia).
2. El resultado no tiene discontinuidades de PTS ni de vídeo ni de audio
   (reutilizando check_pts_continuity de tests/scale_test_edit_pipeline.py
   -- la misma comprobación que detectó el bug original).
3. El clip principal NO se recodificó: un frame decodificado del clip
   principal original y el mismo instante en el resultado son
   BIT-IDÉNTICOS (mismo MD5 del contenido decodificado) -- si el clip
   principal hubiera pasado por cualquier filtro/recodificación, esto
   fallaría.
4. La resolución/fps del resultado son las del clip principal (el outro
   se escaló/normalizó a esos parámetros, no al revés).

Uso:
    cd <repo_root>
    python tests/test_append_outro_pts.py

No toca data/ real -- genera sus propios clips sintéticos en un directorio
temporal. Código de salida 0 si todas las comprobaciones pasan, 1 si
alguna falla.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.edit.run import append_outro  # noqa: E402
from tests.scale_test_edit_pipeline import check_av_duration_consistency, check_pts_continuity  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


# Clip principal: 640x360@30fps, 6s, audio 48kHz estéreo -- parámetros
# "objetivo" a los que append_outro debe normalizar el outro.
_MAIN_WIDTH, _MAIN_HEIGHT, _MAIN_FPS, _MAIN_DURATION = 640, 360, 30, 6.0
# Outro: resolución/fps/sample_rate/canales DISTINTOS a propósito (mismo
# tipo de desajuste que outro.mp4 real vs. dinoblade_1: 1080p30/44.1kHz
# mono/estéreo distinto de 1080p60/48kHz).
_OUTRO_WIDTH, _OUTRO_HEIGHT, _OUTRO_FPS, _OUTRO_DURATION = 320, 240, 25, 2.0
_OUTRO_SAMPLE_RATE = 44100


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló:\n{result.stderr[-2000:]}")


def _generate_main_clip(path: Path) -> None:
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={_MAIN_WIDTH}x{_MAIN_HEIGHT}:rate={_MAIN_FPS}:duration={_MAIN_DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={_MAIN_DURATION}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(path),
    ])


def _generate_outro_clip(path: Path) -> None:
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={_OUTRO_WIDTH}x{_OUTRO_HEIGHT}:rate={_OUTRO_FPS}:duration={_OUTRO_DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={_OUTRO_DURATION}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", str(_OUTRO_SAMPLE_RATE), "-ac", "1",
        str(path),
    ])


def _extract_video_es(path: Path) -> bytes:
    """
    Extrae el elementary stream de vídeo H.264 (Annex B) de `path` con
    -c copy -- los bytes reales del bitstream codificado, sin decodificar
    ni recodificar nada. Comparar estos bytes directamente es la prueba
    más directa de "no se recodificó": si -c copy realmente no toca el
    vídeo, el ES del resultado debe EMPEZAR exactamente con el ES del
    clip principal original, byte a byte.

    (Se probó primero comparar hashes de frames DECODIFICADOS con un
    filtro `select=eq(n,IDX)`, pero dio falsos negativos: el propio
    proceso de decodificación con reordenamiento de B-frames + hilos
    resultó no ser determinista frame-a-frame entre invocaciones con
    duraciones de stream distintas, aunque el ES subyacente fuera
    idéntico -- confirmado extrayendo el ES crudo y comparándolo byte a
    byte, que si coincidía. Comparar el ES evita ese problema por
    completo.)
    """
    with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(tmp_path),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def _probe_video(path: Path) -> tuple[int, int, float]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    width_s, height_s, rate_s = result.stdout.strip().split(",")
    num, den = rate_s.split("/")
    return int(width_s), int(height_s), float(num) / float(den)


def _probe_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def main() -> int:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe no están en PATH; no se puede ejecutar este test.")
        return 1

    work_dir = Path(tempfile.mkdtemp(prefix="append_outro_pts_test_"))
    try:
        main_path = work_dir / "main_clip.mp4"
        outro_path = work_dir / "outro.mp4"
        print("Generando clip principal y outro sintéticos con parámetros deliberadamente distintos...")
        _generate_main_clip(main_path)
        _generate_outro_clip(outro_path)

        config = {
            "paths": {"outro": str(outro_path)},
            "edit": {"append_outro": True},
        }

        print("=== append_outro() con clip principal y outro de formatos distintos ===")
        result_path = Path(append_outro(str(main_path), config))
        check("append_outro devuelve un archivo que existe", result_path.exists(), f"path={result_path}")

        result_duration = _probe_duration(result_path)
        expected_duration = _MAIN_DURATION + _OUTRO_DURATION
        check(
            "la duración del resultado es la suma del clip principal + outro (± 0.5s)",
            abs(result_duration - expected_duration) < 0.5,
            f"result={result_duration:.3f}s, expected~{expected_duration:.1f}s",
        )

        width, height, fps = _probe_video(result_path)
        check(
            "la resolución/fps del resultado son las del clip principal (el outro se normalizó, no al revés)",
            width == _MAIN_WIDTH and height == _MAIN_HEIGHT and abs(fps - _MAIN_FPS) < 0.01,
            f"got={width}x{height}@{fps}, expected={_MAIN_WIDTH}x{_MAIN_HEIGHT}@{_MAIN_FPS}",
        )

        print("=== sin discontinuidades de PTS (la comprobación que detectó el bug original) ===")
        problems = []
        problems += check_pts_continuity(result_path, "v:0", "video")
        problems += check_pts_continuity(result_path, "a:0", "audio")
        problems += check_av_duration_consistency(result_path)
        check("check_pts_continuity/check_av_duration_consistency no reportan ningún problema", not problems, f"problems={problems}")

        print("=== el clip principal NUNCA se recodifica (elementary stream bit-idéntico al original) ===")
        main_es = _extract_video_es(main_path)
        result_es = _extract_video_es(result_path)
        check(
            "el ES de vídeo del resultado EMPIEZA exactamente con el ES del clip principal original (mismos bytes, -c copy real)",
            result_es[: len(main_es)] == main_es,
            f"main_es_size={len(main_es)}, result_es_size={len(result_es)}, "
            f"coincide={result_es[: len(main_es)] == main_es}",
        )
        check(
            "el ES del resultado es más largo que el del clip principal (el outro normalizado se añadió detrás)",
            len(result_es) > len(main_es),
            f"main_es_size={len(main_es)}, result_es_size={len(result_es)}",
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: append_outro une clips de formato distinto sin recodificar el clip principal y sin discontinuidades de PTS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
