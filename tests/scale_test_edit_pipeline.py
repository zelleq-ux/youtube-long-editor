"""
Test de escala del pipeline completo de src/edit/run.py
(apply_cuts_with_zoom + normalize_audio) contra un vídeo sintético de 1h a
60fps con ~400 cortes y ~50 tramos de zoom -- la misma escala usada para
validar el rediseño en dos pasos (shards) del 2026-08-05 (ver status.md),
pero con una comprobación EXPLÍCITA de monotonía/continuidad de PTS que
aquel test NO tenía.

Motivación (bug real encontrado en producción, 2026-08-08): una ejecución
completa contra dinoblade_1 (grabación real de 1h39m) produjo un
`final.mp4` que se congelaba en el reproductor alrededor del minuto 10:29
y saltaba al minuto ~67, sin audio a partir de ahí. El test de escala
anterior NO detectó nada porque solo comprobaba la DURACIÓN total del
vídeo de salida (que puede seguir siendo correcta incluso con timestamps
internos rotos: un salto de PTS hacia delante en mitad del archivo no
cambia cuántos frames hay en total, así que el contador de frames/duración
declarada en el contenedor puede seguir "cuadrando"). Solo una inspección
paquete a paquete (ffprobe) de PTS/DTS revela el problema.

Causa raíz encontrada (investigada en dos rondas, ver el docstring de
src/edit/run.py para el detalle completo): `_cut_video` particionaba los
tramos a conservar en "shards" (una llamada de ffmpeg por shard, con un
filtro `concat` interno) usando primero solo un presupuesto de CARACTERES
del filter_complex (`_MAX_FILTER_COMPLEX_CHARS`, 20000) -- con tramos
cortos esto permitía shards de ciento y pico de tramos concatenados en un
único filtro `concat`, lo que producía una discontinuidad de PTS a mitad
de la salida de ese shard. Un primer fix (limitar cada shard a 40 tramos
como máximo) se validó contra este mismo test sintético (640x360/60fps) y
parecía suficiente -- pero al revalidarlo contra `dinoblade_1` completo
(1920x1080 real) el problema SEGUÍA apareciendo con esos mismos 40 tramos:
aislando el shard se confirmó que el filtro `concat` de ffmpeg pierde
FRAMES DE VÍDEO (no solo "la cuenta del PTS") al reunir muchas ramas
trim+setpts, y es sensible también a la resolución -- 640x360 no
reproduce el fallo ni con 133 tramos, 1920x1080 sí con solo 40. Este test
sintético a baja resolución NO puede reproducir esa sensibilidad a
resolución, así que NO sirve para validar ningún límite de nº de
tramos/shard basado en `concat` -- la validación real de ese aspecto se
hizo directamente contra `dinoblade_1` (ver status.md).

Fix definitivo (no solo acotar el fan-in, sino evitarlo): `_cut_video` ya
no usa el filtro `concat` en absoluto para el corte -- cada tramo se
recorta a su propio archivo aislado (_cut_segment, un simple trim de
entrada sin filtros) y se pegan con el concat DEMUXER (_glue_video_files),
que opera a nivel de contenedor y no pasa por el filtro `concat`. Este
test sigue sirviendo como regresión de ese mecanismo (demuxer + tramos
aislados, con muchos cortes y tramos de zoom) aunque no pueda, por su
resolución reducida, reproducir por sí solo el bug original del filtro
`concat`.

Uso:
    cd <repo_root>
    python tests/scale_test_edit_pipeline.py

Genera sus propios ficheros de trabajo bajo un directorio temporal del
sistema (no toca data/ ni ningún vídeo real) y los reutiliza entre
ejecuciones si ya existen (el vídeo sintético de 1h tarda un rato en
generarse). Termina con código de salida 0 si todas las comprobaciones
pasan, 1 si alguna falla.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.edit.run import apply_cuts_with_zoom, normalize_audio  # noqa: E402

VIDEO_ID = "scale_test_1h"
DURATION_SECONDS = 3600
FPS = 60  # igual que las grabaciones reales del proyecto -- el bug es sensible a esto (ver docstring)
WIDTH, HEIGHT = 640, 360  # resolución reducida a propósito: solo por velocidad, el bug no depende de ella

# Cada cuántos segundos hay un corte sintético (y cuánto dura cada uno) --
# ~400 cortes en total, mismo orden de magnitud que el caso real que falló
# (181 cortes) y que el test de escala anterior (400 cortes).
_CUT_PERIOD_SECONDS = 9.0
_CUT_DURATION_SECONDS = 0.8

# Cada cuántos segundos hay una tanda de "habla" (palabras cada 0.3s
# durante WORD_RUN_SECONDS, luego silencio) -- genera ~48 tramos de habla
# larga tras agrupar por hueco, mismo orden de magnitud que los 47 reales.
_WORD_RUN_SECONDS = 15.0
_WORD_GAP_SECONDS = 60.0

# Umbral (segundos) por encima del cual un hueco entre PTS consecutivos
# (ordenados) se considera una discontinuidad anómala, no jitter normal de
# reordenamiento por B-frames. A 60fps un frame dura ~0.0167s; cualquier
# hueco de más de 1s es varias decenas de frames y no tiene explicación
# normal en un vídeo CFR.
_PTS_GAP_ANOMALY_THRESHOLD_SECONDS = 1.0

# Diferencia máxima tolerada entre la duración del stream de vídeo y el de
# audio del archivo final -- más que esto indica que uno de los dos se
# cortó/truncó antes que el otro (el síntoma de "sin audio a partir de
# cierto punto" del bug real).
_MAX_AV_DURATION_DIFF_SECONDS = 2.0


def _workdir() -> Path:
    d = Path(tempfile.gettempdir()) / "yt_long_editor_scale_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _generate_synthetic_video(path: Path) -> None:
    if path.exists():
        print(f"Reutilizando vídeo sintético existente ({path})")
        return
    print(f"Generando vídeo sintético de {DURATION_SECONDS}s @ {FPS}fps ({WIDTH}x{HEIGHT})...")
    t0 = time.monotonic()
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION_SECONDS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION_SECONDS}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    print(f"  generado en {time.monotonic() - t0:.1f}s")


def _generate_cuts() -> list[dict]:
    cuts = []
    t = 3.0
    while t < DURATION_SECONDS - 5:
        cuts.append({
            "start": round(t, 3), "end": round(t + _CUT_DURATION_SECONDS, 3),
            "type": "silence", "reason": "scale_test sintético",
        })
        t += _CUT_PERIOD_SECONDS
    return cuts


def _generate_transcript_words() -> list[dict]:
    words = []
    t = 5.0
    idx = 0
    while t < DURATION_SECONDS - 20:
        run_end = t + _WORD_RUN_SECONDS
        wt = t
        while wt < run_end:
            words.append({"word": f"w{idx}", "start": round(wt, 3), "end": round(wt + 0.25, 3)})
            wt += 0.3
            idx += 1
        t = run_end + _WORD_GAP_SECONDS
    return words


def _dump_packets(path: Path, select_stream: str) -> list[tuple[float, float]]:
    """[(pts_time, dts_time), ...] EN EL ORDEN EN QUE APARECEN EN EL ARCHIVO (orden de decodificación)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", select_stream,
        "-show_entries", "packet=pts_time,dts_time",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    rows: list[tuple[float, float]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(",")
        if len(parts) != 2:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return rows


def check_pts_continuity(path: Path, select_stream: str, label: str) -> list[str]:
    """
    Comprueba, para el stream `select_stream` (p.ej. "v:0" o "a:0") de
    `path`, que:

    1. El DTS (orden de decodificación) es monótono no decreciente en el
       orden en que los paquetes aparecen en el archivo -- una violación
       aquí es un stream objetivamente corrupto (no es un tema de
       jitter/reordenamiento, DTS SIEMPRE debe ser no decreciente).
    2. Ordenando los PTS de todos los paquetes, ningún hueco entre valores
       consecutivos supera _PTS_GAP_ANOMALY_THRESHOLD_SECONDS -- esto es
       lo que detecta el bug real (un salto de cientos/miles de segundos
       en mitad del archivo). Se usa el orden ORDENADO (no el de
       aparición) porque el reordenamiento normal por B-frames hace que,
       en el orden de aparición, el PTS "vaya y venga" unos pocos frames
       sin que eso sea un problema.

    Devuelve una lista de descripciones de problemas encontrados (vacía si
    todo está bien).
    """
    rows = _dump_packets(path, select_stream)
    problems: list[str] = []
    if not rows:
        return [f"[{label}] no se encontró ningún paquete"]

    dts_values = [d for _, d in rows]
    dts_violations = [
        (i, dts_values[i], dts_values[i + 1])
        for i in range(len(dts_values) - 1)
        if dts_values[i + 1] < dts_values[i]
    ]
    if dts_violations:
        first = dts_violations[0]
        problems.append(
            f"[{label}] DTS no monótono: {len(dts_violations)} violación(es), "
            f"primera en el paquete #{first[0]} ({first[1]:.3f}s -> {first[2]:.3f}s)"
        )

    pts_sorted = sorted(p for p, _ in rows)
    for i in range(len(pts_sorted) - 1):
        gap = pts_sorted[i + 1] - pts_sorted[i]
        if gap > _PTS_GAP_ANOMALY_THRESHOLD_SECONDS:
            problems.append(
                f"[{label}] salto anómalo de PTS: {gap:.3f}s entre {pts_sorted[i]:.3f}s y "
                f"{pts_sorted[i + 1]:.3f}s (umbral {_PTS_GAP_ANOMALY_THRESHOLD_SECONDS}s)"
            )

    return problems


def check_av_duration_consistency(path: Path) -> list[str]:
    """
    Comprueba que la duración del stream de vídeo y la del de audio de
    `path` no difieren en más de _MAX_AV_DURATION_DIFF_SECONDS -- detecta
    el síntoma de "un stream se corta antes que el otro" (en el bug real,
    el audio de final.mp4 terminaba a los 614s con un vídeo de 5533s).
    """
    problems: list[str] = []
    durations: dict[str, float | None] = {}
    for select_stream, key in (("v:0", "video"), ("a:0", "audio")):
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", select_stream,
            "-show_entries", "stream=duration", "-of", "csv=p=0",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        try:
            durations[key] = float(result.stdout.strip())
        except ValueError:
            durations[key] = None
            problems.append(f"no se pudo leer la duración del stream de {key}")

    if durations.get("video") is not None and durations.get("audio") is not None:
        diff = abs(durations["video"] - durations["audio"])
        if diff > _MAX_AV_DURATION_DIFF_SECONDS:
            problems.append(
                f"duración de vídeo ({durations['video']:.2f}s) y audio ({durations['audio']:.2f}s) "
                f"difieren {diff:.2f}s (> {_MAX_AV_DURATION_DIFF_SECONDS}s) -- posible truncamiento de un stream"
            )
    return problems


def main() -> int:
    work_dir = _workdir()
    raw_dir = work_dir / "raw"
    transcripts_dir = work_dir / "transcripts"
    output_dir = work_dir / "output"
    for d in (raw_dir, transcripts_dir, output_dir):
        d.mkdir(parents=True, exist_ok=True)

    input_path = raw_dir / f"{VIDEO_ID}.mp4"
    _generate_synthetic_video(input_path)

    cuts = _generate_cuts()
    print(f"{len(cuts)} cortes sintéticos")

    words = _generate_transcript_words()
    with open(transcripts_dir / f"{VIDEO_ID}.json", "w", encoding="utf-8") as f:
        json.dump({"words": words, "segments": []}, f)
    print(f"{len(words)} palabras sintéticas ({len(words) and 'generadas' or 'ninguna'})")

    config = {
        "paths": {
            "raw": str(raw_dir),
            "transcripts": str(transcripts_dir),
            "output": str(output_dir),
            "outro": str(work_dir / "no_outro.mp4"),  # no existe -> append_outro se omite
        },
        # Fracción 0.0-1.0 del frame (cambiado de píxeles absolutos el 2026-08-10,
        # ver src.common.face_detection) -- conversión exacta de x=15,y=60,w=200,h=120
        # sobre WIDTH=640,HEIGHT=360, para no cambiar el comportamiento de este test.
        "facecam_region": {"x": 15 / WIDTH, "y": 60 / HEIGHT, "w": 200 / WIDTH, "h": 120 / HEIGHT},
        "edit": {
            "long_speech_min_seconds": 10,
            "long_speech_gap_seconds": 1.2,
            "long_speech_zoom_factor": 1.13,
            "zoom_in_duration_seconds": 4.5,
            "loudnorm": True,
            "append_outro": False,
        },
    }

    print("\n=== apply_cuts_with_zoom (corte + zoom, pipeline real) ===")
    t0 = time.monotonic()
    clip_path = apply_cuts_with_zoom(VIDEO_ID, cuts, config)
    print(f"completado en {time.monotonic() - t0:.1f}s -> {clip_path}")

    print("\n=== normalize_audio (loudnorm, 2 pasadas) ===")
    t0 = time.monotonic()
    normalized_path = normalize_audio(clip_path, config)
    print(f"completado en {time.monotonic() - t0:.1f}s -> {normalized_path}")

    final_path = output_dir / f"{VIDEO_ID}_final.mp4"
    Path(normalized_path).replace(final_path)
    if Path(clip_path) != Path(normalized_path) and Path(clip_path).exists():
        Path(clip_path).unlink(missing_ok=True)

    print(f"\n=== Verificando {final_path} ===")
    problems: list[str] = []
    problems += check_pts_continuity(final_path, "v:0", "vídeo")
    problems += check_pts_continuity(final_path, "a:0", "audio")
    problems += check_av_duration_consistency(final_path)

    if problems:
        print(f"\nFALLO: {len(problems)} problema(s) encontrado(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nOK: sin discontinuidades de PTS ni desajuste de duración audio/vídeo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
