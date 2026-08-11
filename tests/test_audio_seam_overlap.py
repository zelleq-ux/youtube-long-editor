"""
Test de regresión del bug de audio duplicado/"tartamudeo" en la costura
cabeza->interior del renderizado parcial sin pérdida de src/edit/run.py
(ver "Solape de audio/vídeo en la costura del renderizado parcial sin
pérdida" en el docstring de ese módulo para el análisis completo de la
causa raíz -- investigado y arreglado 2026-08-11).

Síntoma real reportado (vídeos ya publicados: dinoblade_1, icarus_1,
shift_at_midnight_1, how_many_dudes_1): un glitch esporádico donde la voz
repite/tartamudea la última sílaba justo en un punto de corte -- p.ej.
"Hey, dónde cojones-nes está la pistola?!".

Causa raíz: `_cut_segment_smart` calcula `kf_start`/`kf_end` a partir de
keyframes de VÍDEO únicamente. Antes del fix, `_cut_segment_copy` copiaba
vídeo Y AUDIO sin recodificar (-c copy) en esos mismos timestamps -- válido
para vídeo (kf_start/kf_end SON keyframes reales), pero el audio (paquetes
AAC en su propia rejilla temporal, independiente del GOP de vídeo) no
puede recortarse con precisión de muestra en modo -c copy puro: el primer
paquete de audio del interior copiado terminaba siendo el que YA sonaba
antes de kf_start, duplicando contenido que el fragmento de cabeza
(recodificado con seek preciso) ya incluía.

Este test reproduce el mecanismo EXACTO con las funciones REALES de
producción (_cut_segment_smart, no una reimplementación paralela) sobre
varios tramos con offsets de inicio distintos (no alineados a keyframe,
para forzar la división cabeza+interior), y mide el solape de audio real
en la costura vía ffprobe -- packet pts_time, no duración declarada del
contenedor (poco fiable para fragmentos recortados).

De regalo (mismo mecanismo, hallazgo secundario de la misma
investigación): también comprueba que el VÍDEO del interior tiene el
recuento EXACTO de frames esperado -- antes del fix, `-to` en modo -c copy
podía "gotear" varios frames del GOP siguiente por el reordenamiento de
B-frames (que ingest/run.py deja activadas por defecto), produciendo un
frame congelado/repetido en la costura interior->cola -- el equivalente
visual del tartamudeo de audio.

Con el código ANTERIOR al fix (auditado: `_cut_segment_copy` con `-c copy`
puro para vídeo+audio en kf_start/kf_end), este test falla con un solape
medido de ~30-50ms por costura. Con el fix (vídeo `-c:v copy` + `-frames:v`
exacto, audio recodificado con seek preciso), el solape medido es 0.0ms
(o una fracción despreciable por redondeo de punto flotante) y el
recuento de frames de vídeo es exacto.

Uso:
    cd <repo_root>
    python tests/test_audio_seam_overlap.py

Genera sus propios ficheros de trabajo bajo un directorio temporal del
sistema (no toca data/ ni ningún vídeo real) y reutiliza la fuente
sintética entre ejecuciones si ya existe. Código de salida 0 si todas las
comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.edit.run import (  # noqa: E402
    _cut_segment_smart,
    _keyframe_at_or_after,
    _keyframe_at_or_before,
    _scan_keyframe_timestamps,
)

FPS = 30
WIDTH, HEIGHT = 640, 360
DURATION_SECONDS = 120
# GOP forzado a 2.0s -- mismo orden de magnitud que dinoblade_1/icarus_1
# reales (ver status.md), y B-frames DEJADAS ACTIVAS a propósito (sin
# -bf 0): así es como ingest/run.py codifica los raw.mp4 reales (libx264
# preset "medium" sin desactivar B-frames), y el hallazgo de vídeo de este
# bug depende de que existan.
GOP_FRAMES = 60

# Varios tramos con offsets de arranque DISTINTOS respecto al keyframe más
# cercano (para no depender de una coincidencia de un único offset) --
# todos suficientemente largos para producir cabeza+interior+cola.
KEEP_SEGMENTS: list[tuple[float, float]] = [
    (3.37, 40.21),
    (3.90, 40.21),
    (10.55, 60.02),
    (20.99, 70.50),
    (44.10, 80.80),
]

# Solape de audio tolerado en la costura cabeza->interior: por encima de
# esto se considera contenido duplicado real (el bug), no ruido de
# redondeo de punto flotante. El bug real medía 30-50ms; 5ms es
# generoso para redondeo pero muy por debajo de cualquier duplicación
# perceptible.
_MAX_AUDIO_OVERLAP_MS = 5.0

# AAC: 1024 muestras / 48kHz.
_AAC_FRAME_SECONDS = 1024 / 48000


def _workdir() -> Path:
    d = Path(tempfile.gettempdir()) / "yt_long_editor_audio_seam_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _generate_source(path: Path) -> None:
    if path.exists():
        print(f"Reutilizando fuente sintética existente ({path})")
        return
    print(f"Generando fuente sintética {DURATION_SECONDS}s @ {FPS}fps ({WIDTH}x{HEIGHT}), GOP={GOP_FRAMES}...")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION_SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION_SECONDS}",
        # Mismos crf/preset que ingest/run.py (sin -bf 0: B-frames activas
        # por defecto en el preset "medium", igual que los raw.mp4 reales).
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuvj420p", "-color_range", "pc",
        "-g", str(GOP_FRAMES), "-keyint_min", str(GOP_FRAMES), "-sc_threshold", "0",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def _packet_pts_times(path: Path, select_stream: str) -> list[float]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", select_stream,
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    times: list[float] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line or line == "N/A":
            continue
        try:
            times.append(float(line))
        except ValueError:
            continue
    return times


def _video_frame_count(path: Path) -> int:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return int(result.stdout.strip())


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        status = "OK" if condition else "FALLO"
        print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
        if not condition:
            failures.append(f"{label}: {detail}")

    work_dir = _workdir()
    source_path = work_dir / "source.mp4"
    _generate_source(source_path)
    keyframes = _scan_keyframe_timestamps(source_path)
    print(f"{len(keyframes)} keyframes de vídeo encontrados en la fuente\n")

    out_dir = work_dir / "frags"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    for i, (start, end) in enumerate(KEEP_SEGMENTS):
        kf_start = _keyframe_at_or_after(keyframes, start)
        kf_end = _keyframe_at_or_before(keyframes, end)
        print(f"segmento {i} [{start:.2f}s-{end:.2f}s] (kf_start={kf_start:.3f}, kf_end={kf_end:.3f}):")

        fragments = _cut_segment_smart(source_path, start, end, keyframes, FPS, i, len(KEEP_SEGMENTS), out_dir)
        head = next((f for f in fragments if f.name.endswith("_head.mp4")), None)
        mid = next((f for f in fragments if f.name.endswith("_mid.mp4")), None)

        if head is None or mid is None:
            check(f"seg{i}: produce cabeza+interior (para poder medir la costura)", False,
                  f"fragmentos={[f.name for f in fragments]}")
            continue

        # --- Costura cabeza -> interior: solape de AUDIO ---
        head_pts = _packet_pts_times(head, "a:0")
        mid_pts = _packet_pts_times(mid, "a:0")
        check(f"seg{i}: cabeza y interior tienen paquetes de audio", bool(head_pts) and bool(mid_pts))
        if head_pts and mid_pts:
            head_audio_end_abs = start + head_pts[-1] + _AAC_FRAME_SECONDS
            mid_first_abs = kf_start + mid_pts[0]
            overlap_ms = (head_audio_end_abs - mid_first_abs) * 1000
            check(
                f"seg{i}: sin solape de audio apreciable en la costura cabeza->interior "
                f"(<= {_MAX_AUDIO_OVERLAP_MS}ms)",
                overlap_ms <= _MAX_AUDIO_OVERLAP_MS,
                f"solape medido={overlap_ms:.1f}ms (cabeza termina en {head_audio_end_abs:.4f}s, "
                f"interior arranca en {mid_first_abs:.4f}s)",
            )

        # --- Interior: recuento EXACTO de frames de vídeo (sin goteo de B-frames) ---
        expected_frames = round((kf_end - kf_start) * FPS)
        actual_frames = _video_frame_count(mid)
        check(
            f"seg{i}: interior tiene el recuento exacto de frames de vídeo (sin goteo de B-frames)",
            actual_frames == expected_frames,
            f"esperado={expected_frames} real={actual_frames} (diff={actual_frames - expected_frames:+d})",
        )
        print()

    if failures:
        print(f"FALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("OK: sin solape de audio en ninguna costura cabeza->interior, recuento de vídeo exacto en todas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
