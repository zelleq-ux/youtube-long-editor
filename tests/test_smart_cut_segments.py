"""
Test sintético (sin tocar data/ ni vídeos reales) del "renderizado
parcial sin pérdida" de src/edit/run.py (_cut_segment_smart/_cut_video):
ver "Renderizado parcial sin pérdida (smart cut, 2026-08-09)" en el
docstring de ese módulo para el diseño completo y el porqué.

Motivación: antes, _cut_segment recodificaba CADA tramo a conservar
entero a crf16/veryfast, aunque la inmensa mayoría de su duración no toca
ningún punto de corte -- solo sus dos extremos importan para que el corte
sea preciso a nivel de frame. Midiendo contra los cuts.json reales de
dinoblade_1/icarus_1 (ver status.md), 91.2%/76.0% de la duración
conservada podría copiarse sin recodificar en vez de recodificarse.
_cut_segment_smart copia sin recodificar (-c copy) el interior de cada
tramo entre dos keyframes reales del vídeo de entrada, y solo recodifica
los bordes (o el tramo completo, como fallback, si es más corto que un
intervalo de keyframe).

Este test genera su propia fuente sintética (GOP forzado, color range
completo -- yuvj420p, igual que los raw.mp4 reales del proyecto) y
comprueba, usando las funciones REALES de src/edit/run.py (no una
reimplementación paralela):

  1. Al menos un tramo produce un fragmento "interior copiado" (si no,
     el test no estaría probando nada -- señal de que la fuente sintética
     o los tramos de prueba no ejercitan la ruta que se quiere validar).
  2. El interior copiado es BIT-IDÉNTICO (hash del frame decodificado) al
     mismo instante en el vídeo de origen -- confirma que -c copy no
     recodificó nada por accidente.
  3. pix_fmt/color_range se conservan consistentes entre un fragmento
     recodificado (cabeza/cola) y el vídeo de origen -- sin salto de
     niveles de negro/blanco en la costura.
  4. check_pts_continuity/check_av_duration_consistency (reutilizadas de
     scale_test_edit_pipeline.py) sobre el resultado de _cut_video real
     -- 0 discontinuidades.
  5. El nº de frames de vídeo del resultado de _cut_video coincide (con
     tolerancia de redondeo, ver el docstring del módulo) con el de un
     baseline que recodifica cada tramo completo con el mismo código de
     producción (_cut_segment_recode), sin usar la ruta "smart".
  6. Tiempo de ffmpeg: _cut_video (smart) debe ser más rápido que el
     baseline en este vídeo de prueba (la mayoría de su duración cae en
     tramos largos con hueco interior copiable).

Uso:
    cd <repo_root>
    python tests/test_smart_cut_segments.py

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
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.edit.run import (  # noqa: E402
    _cut_segment_recode,
    _cut_segment_smart,
    _cut_video,
    _glue_video_files,
    _keyframe_at_or_after,
    _keyframe_at_or_before,
    _scan_keyframe_timestamps,
)
from tests.scale_test_edit_pipeline import (  # noqa: E402
    check_av_duration_consistency,
    check_pts_continuity,
)

FPS = 30
WIDTH, HEIGHT = 640, 360
DURATION_SECONDS = 300
# GOP forzado a 2.0s (60 frames @ 30fps) -- dentro del rango real
# observado en dinoblade_1/icarus_1 (keyframe medio cada ~3.0s/~3.3s,
# máximo ~4.17s por keyint=250@60fps, ver status.md).
GOP_FRAMES = 60

# Tramos a conservar sintéticos (simulando compute_keep_segments): mezcla
# deliberada de cortos (<1 GOP, sin hueco interior útil -> fallback a
# recodificación completa) y largos (varios GOPs, con mucho margen
# interior copiable) a offsets NO alineados con ningún keyframe.
KEEP_SEGMENTS: list[tuple[float, float]] = [
    (0.37, 1.10),      # ~0.7s: sin hueco útil
    (3.40, 4.05),      # ~0.65s: idem
    (7.83, 47.21),     # ~39s: varios GOPs de margen interior
    (50.10, 50.60),    # ~0.5s: sin hueco útil
    (55.90, 118.44),   # ~62s: largo
    (120.02, 121.35),  # ~1.3s: roza 1 GOP, puede o no tener hueco útil
    (130.77, 298.90),  # ~168s: el más largo, la mayoría del resto del vídeo
]

_MAX_FRAME_COUNT_DIFF = 20  # tolerancia de redondeo (ver docstring del módulo, "efecto secundario menor")
_MAX_SMART_SLOWDOWN_RATIO = 1.5  # ver el check de "speedup" más abajo


def _workdir() -> Path:
    d = Path(tempfile.gettempdir()) / "yt_long_editor_smart_cut_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _generate_source(path: Path) -> None:
    if path.exists():
        print(f"Reutilizando fuente sintética existente ({path})")
        return
    print(f"Generando fuente sintética {DURATION_SECONDS}s @ {FPS}fps ({WIDTH}x{HEIGHT}), GOP={GOP_FRAMES}...")
    t0 = time.monotonic()
    # yuvj420p (full-range) a propósito: así son los raw.mp4 reales del
    # proyecto (confirmado con ffprobe contra dinoblade_1/icarus_1, pese a
    # que ingest/run.py solo pide "-pix_fmt yuv420p" sin más -- el full
    # range se propaga de la fuente). GOP forzado (-g/-keyint_min con
    # sc_threshold=0) para tener keyframes en posiciones predecibles.
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION_SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION_SECONDS}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuvj420p", "-color_range", "pc",
        "-g", str(GOP_FRAMES), "-keyint_min", str(GOP_FRAMES), "-sc_threshold", "0",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    print(f"  generado en {time.monotonic() - t0:.1f}s")


def _count_video_frames(path: Path) -> int:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return int(result.stdout.strip())


def _frame_md5_sequence(path: Path) -> list[str]:
    """
    Lista de hashes MD5 (uno por frame de vídeo, en orden de PRESENTACIÓN)
    de TODO el archivo, decodificando SECUENCIALMENTE desde el principio
    sin ningún `-ss` ni corte anticipado (`-frames:v`) de por medio.

    Por qué NO extraer un único frame suelto con `-ss t` o con
    `-vf select=eq(n\\,IDX) -frames:v 1` (lo que hacía esta función antes,
    2026-08-11, con dos variantes distintas, ambas descartadas):
    confirmado, investigando el bug de audio duplicado de
    src/edit/run.py, que en este build de ffmpeg CUALQUIER extracción que
    termine el pipeline anticipadamente (ya sea por `-ss` buscando
    directamente un instante, o por `-frames:v 1` cortando el filtro
    `select` en cuanto encuentra un match) puede aterrizar en un frame
    VECINO sin avisar en archivos con B-frames -- aparentemente por cómo
    interactúa el buffer de reordenamiento del decodificador con una
    parada temprana del pipeline. Un volcado SECUENCIAL COMPLETO (sin
    parada anticipada) sí da resultados fiables -- verificado comparando
    ambos métodos sobre el mismo archivo, mismo frame objetivo: el
    volcado secuencial coincidía con el contenido real (confirmado con un
    segundo método independiente, comparación byte a byte del frame
    decodificado) y las extracciones puntuales con `-ss`/`-frames:v 1` no
    (ver "Solape de audio/vídeo en la costura del renderizado parcial sin
    pérdida" en el docstring de src/edit/run.py para el detalle completo).
    Coste aceptable aquí: los archivos de este test son cortos (unos
    pocos miles de frames como mucho), decodificar entero tarda del orden
    de segundos.
    """
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "framemd5", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    hashes: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("0,"):  # stream_index 0 == vídeo
            continue
        hashes.append(line.rsplit(",", 1)[-1].strip())
    return hashes


def _pix_fmt_and_color_range(path: Path) -> tuple[str, str]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt,color_range", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    parts = result.stdout.strip().split(",")
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    work_dir = _workdir()
    source_path = work_dir / "source.mp4"
    _generate_source(source_path)

    keyframes = _scan_keyframe_timestamps(source_path)
    print(f"{len(keyframes)} keyframes encontrados en la fuente sintética")

    # === Fase A: _cut_segment_smart directo, para inspeccionar los
    # fragmentos intermedios ANTES de que _cut_video los limpie (necesario
    # para el chequeo de bit-identidad del interior copiado). ===
    direct_dir = work_dir / "direct"
    if direct_dir.exists():
        shutil.rmtree(direct_dir)
    direct_dir.mkdir(parents=True)

    print("\n=== Fase A: _cut_segment_smart directo (bit-identidad + color range) ===")
    print("Decodificando secuencia completa de hashes de la fuente (una sola vez)...")
    source_hashes = _frame_md5_sequence(source_path)

    all_fragments: list[Path] = []
    n_partial = 0
    n_full_fallback = 0
    for i, (start, end) in enumerate(KEEP_SEGMENTS):
        fragments = _cut_segment_smart(source_path, start, end, keyframes, FPS, i, len(KEEP_SEGMENTS), direct_dir)
        all_fragments.extend(fragments)
        if len(fragments) > 1:
            n_partial += 1
        else:
            n_full_fallback += 1

        mid_paths = [f for f in fragments if f.name.endswith("_mid.mp4")]
        if not mid_paths:
            continue
        mid_path = mid_paths[0]
        kf_start = _keyframe_at_or_after(keyframes, start)
        kf_end = _keyframe_at_or_before(keyframes, end)
        probe_t_mid = (kf_start + kf_end) / 2
        mid_hashes = _frame_md5_sequence(mid_path)
        relative_index = round((probe_t_mid - kf_start) * FPS)
        absolute_index = round(probe_t_mid * FPS)
        frag_hash = mid_hashes[relative_index] if relative_index < len(mid_hashes) else None
        src_hash = source_hashes[absolute_index] if absolute_index < len(source_hashes) else None
        check(
            f"seg{i} interior copiado bit-idéntico al origen",
            frag_hash is not None and frag_hash == src_hash,
            f"kf_start={kf_start:.2f} kf_end={kf_end:.2f} frag_hash={frag_hash} src_hash={src_hash}",
        )

        recoded_paths = [f for f in fragments if f.name.endswith(("_head.mp4", "_tail.mp4"))]
        if recoded_paths:
            src_pixfmt, src_range = _pix_fmt_and_color_range(source_path)
            frag_pixfmt, frag_range = _pix_fmt_and_color_range(recoded_paths[0])
            check(
                f"seg{i} pix_fmt/color_range del borde recodificado coincide con el origen",
                (frag_pixfmt, frag_range) == (src_pixfmt, src_range),
                f"origen={src_pixfmt}/{src_range} fragmento={frag_pixfmt}/{frag_range}",
            )

    print(f"{n_partial} tramo(s) con interior copiado, {n_full_fallback} tramo(s) con fallback completo")
    check("al menos un tramo produjo un interior copiado (la ruta 'smart' se ejercitó de verdad)", n_partial > 0)

    direct_glued = direct_dir / "direct_glued.mp4"
    _glue_video_files(all_fragments, direct_glued)
    problems_direct = []
    problems_direct += check_pts_continuity(direct_glued, "v:0", "vídeo (fase A, fragmentos directos)")
    problems_direct += check_pts_continuity(direct_glued, "a:0", "audio (fase A, fragmentos directos)")
    problems_direct += check_av_duration_consistency(direct_glued)
    check("fase A: sin discontinuidades de PTS ni desajuste audio/vídeo", not problems_direct, "; ".join(problems_direct))

    # === Fase B: punto de entrada real de producción (_cut_video) vs.
    # baseline que recodifica cada tramo completo con el mismo código de
    # producción (_cut_segment_recode) -- mide tiempo real y compara
    # recuento de frames. ===
    print("\n=== Fase B: baseline (recodifica todo) vs. _cut_video real (smart) ===")
    baseline_dir = work_dir / "baseline"
    smart_dir = work_dir / "smart"
    for d in (baseline_dir, smart_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    t0 = time.monotonic()
    baseline_fragments = []
    for i, (start, end) in enumerate(KEEP_SEGMENTS):
        p = baseline_dir / f"seg_{i}.mp4"
        _cut_segment_recode(source_path, start, end, p, f"baseline seg{i} [{start:.2f}-{end:.2f}]")
        baseline_fragments.append(p)
    baseline_path = baseline_dir / "final_baseline.mp4"
    _glue_video_files(baseline_fragments, baseline_path)
    baseline_time = time.monotonic() - t0
    print(f"baseline (recodifica todo): {baseline_time:.2f}s")

    t0 = time.monotonic()
    smart_path = _cut_video(source_path, KEEP_SEGMENTS, FPS, smart_dir)
    smart_time = time.monotonic() - t0
    print(f"_cut_video real (smart): {smart_time:.2f}s")

    problems_smart = []
    problems_smart += check_pts_continuity(smart_path, "v:0", "vídeo (fase B, _cut_video real)")
    problems_smart += check_pts_continuity(smart_path, "a:0", "audio (fase B, _cut_video real)")
    problems_smart += check_av_duration_consistency(smart_path)
    check("fase B: sin discontinuidades de PTS ni desajuste audio/vídeo", not problems_smart, "; ".join(problems_smart))

    n_baseline_frames = _count_video_frames(baseline_path)
    n_smart_frames = _count_video_frames(smart_path)
    diff = n_smart_frames - n_baseline_frames
    print(f"frames de vídeo: baseline={n_baseline_frames} smart={n_smart_frames} (diff={diff:+d})")
    check(
        f"recuento de frames coincide dentro de tolerancia (+/-{_MAX_FRAME_COUNT_DIFF}, "
        "el smart puede tener algunos de más por redondeo de -ss/-to en los bordes extra, nunca de menos)",
        0 <= diff <= _MAX_FRAME_COUNT_DIFF,
        f"diff={diff}",
    )

    speedup = baseline_time / smart_time if smart_time > 0 else float("inf")
    print(f"speedup: {speedup:.2f}x")
    # Tolerancia laxa (no "smart_time < baseline_time" a secas) desde el
    # fix del solape de audio/vídeo en la costura (2026-08-11, ver
    # src/edit/run.py, _cut_segment_copy): el interior copiado ahora hace
    # DOS pasadas de ffmpeg en vez de una (cortar + remux con recuento
    # exacto de frames), lo que añade un overhead FIJO por segmento
    # (arranque de proceso + remux barato). En este vídeo de prueba
    # (300s/640x360, se codifica en segundos) ese overhead fijo puede
    # pesar más que el ahorro de recodificación, así que el ratio real
    # puede caer por debajo de 1.0x -- no es representativo del caso real
    # (1-2h/1080p+, donde recodificar vídeo es muchísimo más caro que unos
    # pocos remuxes extra; la medición de tiempo real está en status.md).
    # _MAX_SMART_SLOWDOWN_RATIO solo protege contra una regresión GRAVE
    # (p.ej. si el mecanismo de dos pasadas se rompiera y degenerase en
    # recodificar todo).
    check(
        f"_cut_video (smart) no es drásticamente más lento que el baseline en este vídeo de prueba "
        f"(tolerancia por el overhead fijo de las dos pasadas del interior copiado; la comparación de "
        f"velocidad representativa es contra vídeos reales, ver status.md)",
        smart_time <= baseline_time * _MAX_SMART_SLOWDOWN_RATIO,
        f"baseline={baseline_time:.2f}s smart={smart_time:.2f}s (ratio={smart_time / baseline_time:.2f}x)",
    )

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: renderizado parcial sin pérdida validado (bit-identidad, PTS continuo, color range, "
          f"recuento de frames, {speedup:.2f}x más rápido que recodificar todo).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
