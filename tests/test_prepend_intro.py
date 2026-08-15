"""
Test de regresión para prepend_intro / _glue_extra_clip (src/edit/run.py),
la generalización del mecanismo de append_outro para poder anteponer Y
posponer clips extra (intro/outro), añadida el 2026-08-10 con soporte de
intro grabado aparte -- ver CLAUDE.md ("Intro grabado aparte") y el
docstring del módulo ("Unión de clips extra (intro/outro), tres niveles").

_glue_extra_clip generaliza el _same_stream_params binario original del
append_outro (fast/full-recode) a los TRES niveles ya validados de forma
manual en data/work/shift_at_midnight_1/run_pipeline.py (_smart_concat):
mismos parámetros de vídeo Y audio (fast_identical), solo el AUDIO difiere
(audio_only_match), o el vídeo difiere (full_recode, escalando SIEMPRE el
clip extra al principal). Este test cubre los tres niveles con
prepend_intro (position="before"), más la retrocompatibilidad (sin
intro.mp4, o con config['edit']['prepend_intro']=False) y la comprobación
de que el clip PRINCIPAL nunca se recodifica en ninguno de los tres casos
(mismo tipo de comprobación bit-exacta que tests/test_append_outro_pts.py,
pero comparando el FINAL del elementary stream -- el clip principal va
SEGUNDO en la lista del concat demuxer cuando position="before"). También
cubre el fix de "Sonoridad del intro" (2026-08-15, ver docstring de
src/edit/run.py): un intro grabado mucho más flojo que el objetivo de
sonoridad debe quedar nivelado tras prepend_intro, no conservar su nivel
de grabación original.

Uso:
    cd <repo_root>
    python tests/test_prepend_intro.py

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

from src.edit.run import _LOUDNORM_TARGET_I, _measure_loudness, prepend_intro  # noqa: E402
from tests.scale_test_edit_pipeline import check_av_duration_consistency, check_pts_continuity  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


_MAIN_WIDTH, _MAIN_HEIGHT, _MAIN_FPS, _MAIN_DURATION = 640, 360, 30, 6.0
_MAIN_SAMPLE_RATE, _MAIN_CHANNELS = 48000, 2


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló:\n{result.stderr[-2000:]}")


def _generate_clip(
    path: Path, width: int, height: int, fps: float, duration: float,
    sample_rate: int, channels: int, freq: int = 440, amplitude: float = 1.0,
) -> None:
    channel_layout = "stereo" if channels >= 2 else "mono"
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}:sample_rate={sample_rate}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", str(sample_rate), "-ac", str(channels),
        "-af", f"volume={amplitude},aformat=channel_layouts={channel_layout}",
        "-movflags", "+faststart",
        str(path),
    ])


def _extract_video_es(path: Path) -> bytes:
    """Elementary stream H.264 crudo (Annex B), sin decodificar -- ver test_append_outro_pts.py para el porqué."""
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


def _check_prepend(
    label: str, work_dir: Path, main_path: Path, intro_width: int, intro_height: int,
    intro_fps: float, intro_duration: float, intro_sample_rate: int, intro_channels: int,
) -> None:
    print(f"=== prepend_intro(): {label} ===")
    video_id = f"video_{label}"
    output_dir = work_dir / "output" / video_id
    output_dir.mkdir(parents=True)
    intro_path = output_dir / "intro.mp4"
    _generate_clip(
        intro_path, intro_width, intro_height, intro_fps, intro_duration,
        intro_sample_rate, intro_channels, freq=880,
    )

    config = {"paths": {"output": str(work_dir / "output")}, "edit": {"prepend_intro": True}}
    result_path = Path(prepend_intro(str(main_path), video_id, config))
    check(f"[{label}] prepend_intro devuelve un archivo que existe", result_path.exists(), f"path={result_path}")

    result_duration = _probe_duration(result_path)
    expected_duration = _MAIN_DURATION + intro_duration
    check(
        f"[{label}] duración del resultado = intro + principal (± 0.5s)",
        abs(result_duration - expected_duration) < 0.5,
        f"result={result_duration:.3f}s expected~{expected_duration:.1f}s",
    )

    width, height, fps = _probe_video(result_path)
    check(
        f"[{label}] resolución/fps del resultado son las del clip PRINCIPAL (el intro se adaptó, no al revés)",
        width == _MAIN_WIDTH and height == _MAIN_HEIGHT and abs(fps - _MAIN_FPS) < 0.01,
        f"got={width}x{height}@{fps}, expected={_MAIN_WIDTH}x{_MAIN_HEIGHT}@{_MAIN_FPS}",
    )

    problems = []
    problems += check_pts_continuity(result_path, "v:0", "video")
    problems += check_pts_continuity(result_path, "a:0", "audio")
    problems += check_av_duration_consistency(result_path)
    check(f"[{label}] sin discontinuidades de PTS", not problems, f"problems={problems}")

    main_es = _extract_video_es(main_path)
    result_es = _extract_video_es(result_path)
    check(
        f"[{label}] el ES del clip PRINCIPAL nunca se recodifica (el resultado TERMINA con sus bytes exactos)",
        result_es[-len(main_es):] == main_es,
        f"main_es_size={len(main_es)} result_es_size={len(result_es)}",
    )
    check(
        f"[{label}] el ES del resultado es más largo que el del clip principal (el intro se añadió delante)",
        len(result_es) > len(main_es),
        f"main_es_size={len(main_es)} result_es_size={len(result_es)}",
    )


def _extract_clip(src: Path, duration: float, out_path: Path) -> None:
    _run(["ffmpeg", "-y", "-i", str(src), "-t", f"{duration:.6f}", "-c", "copy", str(out_path)])


def _check_intro_loudness_matched(work_dir: Path, main_path: Path) -> None:
    """
    Regresión de "Sonoridad del intro" (ver docstring de src/edit/run.py):
    un intro grabado mucho más FLOJO que el objetivo de sonoridad debe
    quedar nivelado al mismo objetivo que el contenido principal tras
    prepend_intro -- antes del fix, el intro nunca pasaba por loudnorm en
    absoluto (se unía DESPUÉS de normalize_audio) y conservaba su nivel
    de grabación original.
    """
    print("=== prepend_intro(): nivelado de sonoridad del intro ===")
    video_id = "video_quiet_intro"
    output_dir = work_dir / "output" / video_id
    output_dir.mkdir(parents=True)
    intro_path = output_dir / "intro.mp4"
    intro_duration = 3.0
    # Resolución distinta a la del clip principal (fuerza el nivel de
    # recodificación completa, el caso REAL de este proyecto -- el intro
    # casi siempre viene de una sesión de OBS aparte con otra config) y
    # amplitud MUY floja (0.03 ≈ -30dB) para que el nivelado sea inequívoco.
    _generate_clip(
        intro_path, 1280, 720, 25, intro_duration, 44100, 1, freq=880, amplitude=0.03,
    )
    quiet_measured = _measure_loudness(str(intro_path))
    quiet_lufs = quiet_measured.get("input_i") if quiet_measured else None

    config = {"paths": {"output": str(work_dir / "output")}, "edit": {"prepend_intro": True, "loudnorm": True}}
    result_path = Path(prepend_intro(str(main_path), video_id, config))

    intro_only_path = work_dir / "result_intro_only.mp4"
    _extract_clip(result_path, intro_duration - 0.2, intro_only_path)  # margen para no rozar la costura
    result_measured = _measure_loudness(str(intro_only_path))
    result_lufs = float(result_measured["input_i"]) if result_measured else None

    check(
        "el intro original es MUCHO más flojo que el objetivo de sonoridad "
        "(confirma que el caso de prueba no es degenerado)",
        quiet_lufs is not None and float(quiet_lufs) < _LOUDNORM_TARGET_I - 5,
        f"intro_original_LUFS={quiet_lufs}",
    )
    check(
        "el intro YA UNIDO queda nivelado cerca del objetivo de sonoridad del contenido "
        "principal (no conserva su nivel de grabación original)",
        result_lufs is not None and abs(result_lufs - _LOUDNORM_TARGET_I) < 2.0,
        f"intro_original_LUFS={quiet_lufs} intro_ya_unido_LUFS={result_lufs} objetivo={_LOUDNORM_TARGET_I}",
    )


def main() -> int:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe no están en PATH; no se puede ejecutar este test.")
        return 1

    work_dir = Path(tempfile.mkdtemp(prefix="prepend_intro_test_"))
    try:
        main_path = work_dir / "main_clip.mp4"
        print("Generando clip principal sintético (640x360@30fps, 6s, 48kHz estéreo)...")
        _generate_clip(main_path, _MAIN_WIDTH, _MAIN_HEIGHT, _MAIN_FPS, _MAIN_DURATION, _MAIN_SAMPLE_RATE, _MAIN_CHANNELS)

        # Nivel 1: parámetros de vídeo Y audio idénticos -> concatenación rápida sin recodificar nada.
        _check_prepend(
            "fast_identical", work_dir, main_path,
            intro_width=_MAIN_WIDTH, intro_height=_MAIN_HEIGHT, intro_fps=_MAIN_FPS,
            intro_duration=2.0, intro_sample_rate=_MAIN_SAMPLE_RATE, intro_channels=_MAIN_CHANNELS,
        )

        # Nivel 2: mismo vídeo (resolución/fps), SOLO difiere el audio -> ajusta solo el audio del intro.
        _check_prepend(
            "audio_only_match", work_dir, main_path,
            intro_width=_MAIN_WIDTH, intro_height=_MAIN_HEIGHT, intro_fps=_MAIN_FPS,
            intro_duration=2.0, intro_sample_rate=44100, intro_channels=1,
        )

        # Nivel 3: el vídeo difiere (resolución/fps distintos, como una intro grabada con otra
        # configuración de OBS) -> recodifica el intro COMPLETO, escalado a la resolución del principal.
        _check_prepend(
            "full_recode", work_dir, main_path,
            intro_width=1280, intro_height=720, intro_fps=25,
            intro_duration=2.0, intro_sample_rate=44100, intro_channels=1,
        )

        _check_intro_loudness_matched(work_dir, main_path)

        print("=== retrocompatibilidad: sin data/output/<video_id>/intro.mp4 -> clip sin cambios ===")
        video_id_no_intro = "video_no_intro"
        output_dir_no_intro = work_dir / "output" / video_id_no_intro
        output_dir_no_intro.mkdir(parents=True)
        config_no_intro = {"paths": {"output": str(work_dir / "output")}, "edit": {"prepend_intro": True}}
        result_no_intro = prepend_intro(str(main_path), video_id_no_intro, config_no_intro)
        check(
            "sin intro.mp4: prepend_intro devuelve exactamente clip_path sin tocarlo",
            result_no_intro == str(main_path),
            f"result={result_no_intro}",
        )

        print("=== config['edit']['prepend_intro']=False: se omite aunque exista intro.mp4 ===")
        video_id_disabled = "video_disabled"
        output_dir_disabled = work_dir / "output" / video_id_disabled
        output_dir_disabled.mkdir(parents=True)
        _generate_clip(
            output_dir_disabled / "intro.mp4", _MAIN_WIDTH, _MAIN_HEIGHT, _MAIN_FPS, 2.0,
            _MAIN_SAMPLE_RATE, _MAIN_CHANNELS,
        )
        config_disabled = {"paths": {"output": str(work_dir / "output")}, "edit": {"prepend_intro": False}}
        result_disabled = prepend_intro(str(main_path), video_id_disabled, config_disabled)
        check(
            "prepend_intro desactivado: devuelve clip_path sin tocarlo pese a existir intro.mp4",
            result_disabled == str(main_path),
            f"result={result_disabled}",
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: prepend_intro une intro+clip principal en los tres niveles sin recodificar el clip principal, y respeta la retrocompatibilidad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
