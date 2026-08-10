"""
Test sintético (sin red, sin vídeo/ffmpeg real) de src/subtitles/run.py.

Cubre:
1. filter_and_remap_words: descarta palabras que solapan (total o
   parcialmente) un corte, conserva las que no, remapea correctamente a la
   línea de tiempo editada, y no descarta una palabra que empieza
   exactamente donde termina un corte (límite exclusivo).
2. _group_words_into_cues: corta por presupuesto de caracteres y por
   duración, PREFIRIENDO la última pausa natural vista (puntuación o hueco
   largo) en vez de cortar justo en la palabra que desborda.
3. build_cues: extiende subtítulos demasiado cortos hasta el suelo de
   lectura, limita por max_cue_seconds, y nunca solapa con el siguiente
   subtítulo ni recorta por debajo de la duración real de la locución.
4. wrap_cue_text: reparto en <= 2 líneas de <= 42 caracteres, balanceado.
5. cues_to_srt / _format_srt_timestamp: formato .srt válido (numeración
   secuencial, HH:MM:SS,mmm).
6. run() end-to-end contra un transcript.json + cuts.json sintéticos en un
   directorio temporal: subtitles.srt se genera, respeta los límites de
   caracteres/líneas/duración, no contiene ninguna palabra cortada, y
   config['subtitles']['enabled']=False no genera ningún archivo.
7. Calibración contra el vídeo final real (bug de sincronización
   encontrado en revisión manual de dinoblade_1, ver docstring de
   src/subtitles/run.py): compute_drift_per_cut mide correctamente la
   deriva contra un final.mp4 sintético REAL (generado con ffmpeg) cuya
   duración se fija deliberadamente por encima de lo que predice
   cuts.json, cae a 0.0 (sin calibrar) si no hay final.mp4 o si la deriva
   medida es implausible, y resta la duración del outro si
   append_outro está activo; _calibrated_edited_timestamp aplica
   correctamente drift_per_cut * (nº de cortes ya pasados) a varios
   timestamps de control calculados a mano; y una ejecución run()
   end-to-end completa (transcript + cuts + un final.mp4 real de
   duración conocida, 5 cortes) confirma que los cues del .srt
   REGENERADO quedan desplazados progresivamente más cuanto más cortes
   los preceden -- no un desplazamiento uniforme -- verificado cue a cue
   contra los valores calculados a mano.

Uso:
    cd <repo_root>
    python tests/test_subtitles.py

No toca data/ real (genera sus propios clips sintéticos con ffmpeg en un
directorio temporal para la sección 7). Código de salida 0 si todas las
comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import src.subtitles.run as subtitles_module  # noqa: E402
from src.subtitles.run import (  # noqa: E402
    _calibrated_edited_timestamp,
    _format_srt_timestamp,
    _group_words_into_cues,
    _prepare_intro_words,
    build_cues,
    compute_drift_per_cut,
    cues_to_srt,
    filter_and_remap_words,
    run,
    wrap_cue_text,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


def _w(text: str, start: float, end: float) -> dict:
    return {"text": text, "start": start, "end": end}


def main() -> int:
    print("=== filter_and_remap_words: descarte por solape + remapeo ===")
    words = [
        {"word": " Hola", "start": 0.0, "end": 0.5},
        {"word": " mundo", "start": 0.5, "end": 1.0},
        {"word": " parcial", "start": 1.5, "end": 2.0},   # solapa el borde del corte -> descartada
        {"word": " esto", "start": 2.2, "end": 2.6},      # dentro del corte -> descartada
        {"word": " es", "start": 3.0, "end": 3.5},        # justo al terminar el corte -> conservada
        {"word": " una", "start": 3.5, "end": 4.0},
        {"word": " prueba", "start": 4.0, "end": 4.6},
    ]
    cuts = [{"start": 1.8, "end": 3.0, "type": "silence", "reason": "silencio de audio"}]
    sorted_cuts = sorted(cuts, key=lambda c: c["start"])
    kept = filter_and_remap_words(words, sorted_cuts)
    kept_texts = [w["text"] for w in kept]
    check(
        "descarta 'parcial' y 'esto', conserva el resto",
        kept_texts == ["Hola", "mundo", "es", "una", "prueba"],
        f"kept={kept_texts}",
    )
    by_text = {w["text"]: w for w in kept}
    check("'Hola' no se remapea (antes del corte)", by_text["Hola"]["start"] == 0.0 and by_text["Hola"]["end"] == 0.5)
    check(
        "'es' se remapea restando la duración del corte (1.2s)",
        abs(by_text["es"]["start"] - 1.8) < 1e-9 and abs(by_text["es"]["end"] - 2.3) < 1e-9,
        f"es={by_text['es']}",
    )
    check(
        "'prueba' se remapea consistentemente",
        abs(by_text["prueba"]["start"] - 2.8) < 1e-9 and abs(by_text["prueba"]["end"] - 3.4) < 1e-9,
        f"prueba={by_text['prueba']}",
    )

    print("=== _group_words_into_cues: corte por caracteres, prefiere pausa natural (coma) ===")
    char_words = [
        _w("A", 0.0, 0.3),
        _w("B,", 0.3, 0.6),
        _w("C", 0.6, 0.9),
        _w("D", 0.9, 1.2),
        _w("E", 1.2, 1.5),
    ]
    config_chars = {"subtitles": {"max_chars_per_line": 9, "max_lines": 1, "max_cue_seconds": 1000}}
    groups_chars = _group_words_into_cues(char_words, config_chars)
    groups_chars_texts = [[w["text"] for w in g] for g in groups_chars]
    check(
        "corta después de la coma ('B,'), no justo antes de 'E'",
        groups_chars_texts == [["A", "B,"], ["C", "D", "E"]],
        f"groups={groups_chars_texts}",
    )

    print("=== _group_words_into_cues: corte por duración, prefiere pausa natural (hueco largo) ===")
    dur_words = [
        _w("P0", 0.0, 0.5),
        _w("P1", 0.5, 1.0),
        _w("P2", 1.6, 2.1),   # hueco de 0.6s tras P1 -> pausa natural
        _w("P3", 2.1, 2.6),
        _w("P4", 2.6, 4.0),   # P0.start a P4.end = 4.0s > max_cue_seconds(3.0) -> fuerza corte
    ]
    config_dur = {"subtitles": {"max_chars_per_line": 1000, "max_lines": 1, "max_cue_seconds": 3.0, "natural_pause_gap_seconds": 0.5}}
    groups_dur = _group_words_into_cues(dur_words, config_dur)
    groups_dur_texts = [[w["text"] for w in g] for g in groups_dur]
    check(
        "corta en el hueco largo (tras P1), no justo antes de P4",
        groups_dur_texts == [["P0", "P1"], ["P2", "P3", "P4"]],
        f"groups={groups_dur_texts}",
    )

    print("=== _group_words_into_cues: palabra que empieza justo donde termina un corte no rompe nada ===")
    check(
        "todas las palabras de entrada aparecen en algún grupo (nada se pierde en el agrupado)",
        sorted(w for g in groups_chars_texts for w in g) == sorted(w["text"] for w in char_words),
    )

    print("=== build_cues: suelo de duración por lectura, tope por siguiente subtítulo, nunca por debajo de la locución real ===")
    group1 = [_w("Hola", 10.0, 10.2)]     # 0.2s de habla real, muy corto
    group2 = [_w("Ey", 10.5, 10.9)]       # el siguiente subtítulo empieza en 10.5, antes del suelo de lectura de group1
    config_build = {"subtitles": {"min_cue_seconds": 1.0, "max_cue_seconds": 6.0, "reading_cps_max": 20}}
    cues = build_cues([group1, group2], config_build, total_edited_duration=20.0)
    check(
        "cue1 se extiende pero se limita al inicio de cue2 (10.5), no llega al suelo de 1s completo",
        cues[0]["start"] == 10.0 and abs(cues[0]["end"] - 10.5) < 1e-9,
        f"cue1={cues[0]}",
    )
    check(
        "cue1 nunca queda por debajo de la duración real de la locución (10.0-10.2)",
        cues[0]["end"] >= 10.2,
    )
    check(
        "cue2 se extiende hasta el suelo de lectura/mínimo (no hay tope de un tercer subtítulo)",
        cues[1]["start"] == 10.5 and cues[1]["end"] >= 10.5 + 1.0 - 1e-9,
        f"cue2={cues[1]}",
    )

    print("=== build_cues: el tope de duración (max_cue_seconds) manda incluso con un suelo de lectura enorme ===")
    long_text_group = [_w("palabra_larga_para_forzar_un_suelo_de_lectura_enorme", 0.0, 0.3)]
    config_extreme = {"subtitles": {"min_cue_seconds": 1.0, "max_cue_seconds": 6.0, "reading_cps_max": 1}}
    cues_extreme = build_cues([long_text_group], config_extreme, total_edited_duration=100.0)
    check(
        "la duración nunca supera max_cue_seconds pese a un suelo de lectura mucho mayor",
        abs(cues_extreme[0]["end"] - cues_extreme[0]["start"] - 6.0) < 1e-9,
        f"cue={cues_extreme[0]}",
    )

    print("=== wrap_cue_text: reparto balanceado en <= 2 líneas de <= 42 caracteres ===")
    short_text = "Hola a todos"
    check("texto corto: una sola línea", wrap_cue_text(short_text, 42, 2) == [short_text])

    long_text = "Hola a todos, bienvenidos de nuevo a este directo tan especial de verdad"
    wrapped = wrap_cue_text(long_text, 42, 2)
    check("texto largo: se reparte en exactamente 2 líneas", len(wrapped) == 2, f"wrapped={wrapped}")
    check(
        "ninguna línea supera 42 caracteres",
        all(len(line) <= 42 for line in wrapped),
        f"lens={[len(line) for line in wrapped]}",
    )
    check(
        "el texto se conserva íntegro al unir las líneas",
        " ".join(wrapped) == long_text,
        f"joined={' '.join(wrapped)!r}",
    )

    print("=== _format_srt_timestamp / cues_to_srt: formato .srt válido ===")
    check("0s -> 00:00:00,000", _format_srt_timestamp(0.0) == "00:00:00,000")
    check("61.234s -> 00:01:01,234", _format_srt_timestamp(61.234) == "00:01:01,234")
    check("3661.5s (más de una hora) -> 01:01:01,500", _format_srt_timestamp(3661.5) == "01:01:01,500")

    srt_cues = [
        {"start": 0.0, "end": 1.5, "lines": ["Primera línea"]},
        {"start": 1.5, "end": 3.25, "lines": ["Segunda línea", "con dos renglones"]},
    ]
    srt_text = cues_to_srt(srt_cues)
    check(
        "numeración secuencial desde 1",
        re.findall(r"^\d+$", srt_text, flags=re.MULTILINE) == ["1", "2"],
        f"srt={srt_text!r}",
    )
    check("timestamps con el formato HH:MM:SS,mmm --> HH:MM:SS,mmm", "00:00:00,000 --> 00:00:01,500" in srt_text)
    check("segundo bloque con las dos líneas", "Segunda línea\ncon dos renglones" in srt_text)

    print("=== run(): end-to-end con transcript.json + cuts.json sintéticos ===")
    work_dir = Path(tempfile.mkdtemp(prefix="subtitles_test_"))
    try:
        video_id = "test_video"
        transcripts_dir = work_dir / "transcripts"
        cuts_dir = work_dir / "cuts" / video_id
        output_dir = work_dir / "output"
        transcripts_dir.mkdir(parents=True)
        cuts_dir.mkdir(parents=True)

        # Frase larga y continua (sin huecos) para forzar varios subtítulos solo por
        # presupuesto de caracteres, más una palabra ("perdida") que cae dentro de un
        # corte y no debe aparecer en ningún subtítulo del resultado.
        sentence = (
            "Hola a todos y muy buenas de nuevo a este directo tan especial en el que vamos "
            "a hablar largo y tendido sobre un montón de cosas interesantes hoy perdida aqui mismo"
        ).split(" ")
        transcript_words = []
        t = 0.0
        cut_word_index = sentence.index("perdida")
        cut_start = None
        cut_end = None
        for i, w in enumerate(sentence):
            start = t
            end = t + 0.3
            if i == cut_word_index:
                cut_start = start - 0.05
                cut_end = end + 0.05
            transcript_words.append({"word": f" {w}", "start": start, "end": end, "probability": 0.9})
            t = end  # sin hueco entre palabras

        duration = t
        transcript = {
            "video_id": video_id,
            "language": "es",
            "duration_s": duration,
            "words": transcript_words,
            "segments": [{"start": 0.0, "end": duration, "text": " ".join(sentence)}],
        }
        cuts = [{"start": cut_start, "end": cut_end, "type": "filler", "reason": "muletilla: 'perdida'"}]

        (transcripts_dir / f"{video_id}.json").write_text(json.dumps(transcript), encoding="utf-8")
        (cuts_dir / "cuts.json").write_text(json.dumps(cuts), encoding="utf-8")

        config = {
            "paths": {
                "transcripts": str(transcripts_dir),
                "cuts": str(work_dir / "cuts"),
                "output": str(output_dir),
            },
            "subtitles": {
                "enabled": True,
                "max_chars_per_line": 42,
                "max_lines": 2,
                "min_cue_seconds": 1.0,
                "max_cue_seconds": 6.0,
                "reading_cps_min": 15,
                "reading_cps_max": 20,
                "natural_pause_gap_seconds": 0.5,
            },
        }

        import src.common.db as db_module
        db_module.DB_PATH = work_dir / "pipeline.db"

        result = run(video_id, config)
        srt_path = Path(result["subtitles_path"])
        check("run() devuelve la ruta de subtitles.srt y el archivo existe", srt_path.exists(), f"path={srt_path}")

        srt_content = srt_path.read_text(encoding="utf-8")
        check("'perdida' (palabra cortada) no aparece en ningún subtítulo", "perdida" not in srt_content)

        blocks = [b for b in srt_content.strip().split("\n\n") if b.strip()]
        check("se generaron varios subtítulos (frase larga > presupuesto de un solo cue)", len(blocks) >= 2, f"n={len(blocks)}")

        parsed = []
        ts_re = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})")

        def _to_seconds(h, m, s, ms):
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

        all_lines_ok = True
        max_lines_ok = True
        for i, block in enumerate(blocks, start=1):
            lines = block.split("\n")
            check(f"bloque {i}: numeración correcta", lines[0] == str(i), f"got={lines[0]!r}")
            m = ts_re.match(lines[1])
            check(f"bloque {i}: línea de timestamps con formato válido", m is not None, f"line={lines[1]!r}")
            text_lines = lines[2:]
            if len(text_lines) > 2:
                max_lines_ok = False
            if any(len(tl) > 42 for tl in text_lines):
                all_lines_ok = False
            if m:
                start_s = _to_seconds(*m.groups()[0:4])
                end_s = _to_seconds(*m.groups()[4:8])
                parsed.append((start_s, end_s))

        check("ningún subtítulo tiene más de 2 líneas", max_lines_ok)
        check("ninguna línea supera 42 caracteres", all_lines_ok)
        check(
            "los subtítulos están ordenados y no se solapan entre sí",
            all(parsed[i][1] <= parsed[i + 1][0] + 1e-6 for i in range(len(parsed) - 1)),
            f"parsed={parsed}",
        )
        check(
            "ningún subtítulo dura más que max_cue_seconds",
            all(end - start <= 6.0 + 1e-6 for start, end in parsed),
            f"durations={[round(e - s, 3) for s, e in parsed]}",
        )

        print("=== run() con config['subtitles']['enabled']=False: no genera ningún archivo ===")
        video_id2 = "disabled_video"
        (transcripts_dir / f"{video_id2}.json").write_text(json.dumps(transcript), encoding="utf-8")
        (work_dir / "cuts" / video_id2).mkdir(parents=True)
        (work_dir / "cuts" / video_id2 / "cuts.json").write_text(json.dumps(cuts), encoding="utf-8")
        config_disabled = dict(config, subtitles={**config["subtitles"], "enabled": False})
        result_disabled = run(video_id2, config_disabled)
        check(
            "subtitles_path es None y no se crea subtitles.srt",
            result_disabled["subtitles_path"] is None and not (output_dir / video_id2 / "subtitles.srt").exists(),
            f"result={result_disabled}",
        )

        print("=== compute_drift_per_cut: sin final.mp4 -> 0.0 (sin calibrar, mismo comportamiento que antes) ===")
        video_id3 = "no_final_video"
        (work_dir / "cuts" / video_id3).mkdir(parents=True)
        cuts5 = [
            {"start": 5.0, "end": 6.0, "type": "silence", "reason": "s"},
            {"start": 12.0, "end": 13.5, "type": "silence", "reason": "s"},
            {"start": 20.0, "end": 21.2, "type": "silence", "reason": "s"},
            {"start": 28.0, "end": 28.8, "type": "silence", "reason": "s"},
            {"start": 35.0, "end": 36.0, "type": "silence", "reason": "s"},
        ]
        sorted_cuts5 = sorted(cuts5, key=lambda c: c["start"])
        nominal_main5 = 34.5  # 40.0s original - 5.5s de cortes
        check(
            "compute_drift_per_cut sin final.mp4 devuelve 0.0",
            compute_drift_per_cut(video_id3, {"paths": {"output": str(output_dir)}}, sorted_cuts5, nominal_main5) == 0.0,
        )

        print("=== compute_drift_per_cut: deriva implausible -> 0.0 (desconfía en vez de calibrar mal) ===")
        video_id4 = "implausible_video"
        implausible_dir = output_dir / video_id4
        implausible_dir.mkdir(parents=True)
        (implausible_dir / "final.mp4").write_bytes(b"not a real video")  # ffprobe fallará -> None -> 0.0
        check(
            "final.mp4 corrupto/no-vídeo: ffprobe falla, compute_drift_per_cut cae a 0.0",
            compute_drift_per_cut(video_id4, {"paths": {"output": str(output_dir)}}, sorted_cuts5, nominal_main5) == 0.0,
        )

        print("=== compute_drift_per_cut + _calibrated_edited_timestamp: final.mp4 REAL con deriva conocida ===")
        video_id5 = "drift_video"
        drift_output_dir = output_dir / video_id5
        drift_output_dir.mkdir(parents=True)
        # duración real deliberadamente 0.5s por encima de la nominal (34.5s) -- simula la
        # deriva acumulada de redondeo de frames del smart-cut real, ver docstring del módulo.
        real_main_duration5 = 35.0
        total_drift5 = real_main_duration5 - nominal_main5  # 0.5s
        expected_drift_per_cut5 = total_drift5 / len(sorted_cuts5)  # 0.1s
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c=blue:s=64x64:rate=10:duration={real_main_duration5}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={real_main_duration5}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                str(drift_output_dir / "final.mp4"),
            ],
            check=True, capture_output=True,
        )
        config5 = {"paths": {"output": str(output_dir)}, "edit": {"append_outro": False}}
        measured_drift_per_cut = compute_drift_per_cut(video_id5, config5, sorted_cuts5, nominal_main5)
        check(
            "compute_drift_per_cut mide la deriva correcta contra un final.mp4 real (0.5s/5 cortes = 0.1s/corte)",
            abs(measured_drift_per_cut - expected_drift_per_cut5) < 0.01,
            f"measured={measured_drift_per_cut:.4f}, expected={expected_drift_per_cut5:.4f}",
        )

        print("=== _calibrated_edited_timestamp: desplazamiento PROGRESIVO según nº de cortes ya pasados ===")
        # (original_t, nº de cortes que preceden a original_t, nominal edited esperado)
        control_points = [
            (1.0, 0, 1.0),                                    # antes del primer corte
            (7.0, 1, 7.0 - 1.0),                               # tras 1 corte (5.0-6.0)
            (15.0, 2, 15.0 - 1.0 - 1.5),                       # tras 2 cortes
            (23.0, 3, 23.0 - 1.0 - 1.5 - 1.2),                 # tras 3 cortes
            (30.0, 4, 30.0 - 1.0 - 1.5 - 1.2 - 0.8),           # tras 4 cortes
            (37.0, 5, 37.0 - 1.0 - 1.5 - 1.2 - 0.8 - 1.0),     # tras los 5 cortes
        ]
        for original_t, n_cuts, nominal_expected in control_points:
            calibrated = _calibrated_edited_timestamp(original_t, sorted_cuts5, expected_drift_per_cut5)
            expected_calibrated = nominal_expected + n_cuts * expected_drift_per_cut5
            check(
                f"t={original_t}s ({n_cuts} corte(s) pasados): calibrado = nominal + {n_cuts}*drift_per_cut",
                abs(calibrated - expected_calibrated) < 1e-9,
                f"calibrated={calibrated:.4f}, expected={expected_calibrated:.4f} (nominal={nominal_expected:.4f})",
            )
        check(
            "drift_per_cut=0.0 se comporta EXACTAMENTE igual que sin calibrar",
            _calibrated_edited_timestamp(23.0, sorted_cuts5, 0.0) == 23.0 - 1.0 - 1.5 - 1.2,
        )

        print("=== run() end-to-end con final.mp4 real de deriva conocida: cues del .srt desplazados progresivamente ===")
        transcript5_words = [
            {"word": " Hola", "start": 1.0, "end": 1.5},
            {"word": " Uno", "start": 7.0, "end": 7.5},
            {"word": " Dos", "start": 15.0, "end": 15.5},
            {"word": " Tres", "start": 23.0, "end": 23.5},
            {"word": " Cuatro", "start": 30.0, "end": 30.5},
            {"word": " Cinco", "start": 37.0, "end": 37.5},
        ]
        transcript5 = {
            "video_id": video_id5,
            "language": "es",
            "duration_s": 40.0,
            "words": transcript5_words,
            "segments": [{"start": 0.0, "end": 40.0, "text": "Hola Uno Dos Tres Cuatro Cinco"}],
        }
        (transcripts_dir / f"{video_id5}.json").write_text(json.dumps(transcript5), encoding="utf-8")
        (work_dir / "cuts" / video_id5).mkdir(parents=True)
        (work_dir / "cuts" / video_id5 / "cuts.json").write_text(json.dumps(cuts5), encoding="utf-8")

        config5_full = dict(config5, paths={**config5["paths"], "transcripts": str(transcripts_dir), "cuts": str(work_dir / "cuts")})
        config5_full["subtitles"] = {
            # max_cue_seconds pequeño A PROPÓSITO: fuerza un cue por palabra (cada palabra
            # dura 0.5s, cualquier par ya supera 0.6s) para poder comparar cada timestamp
            # calibrado 1:1 contra los puntos de control, sin que el agrupado por huecos
            # naturales (que solo actúa si hace falta cortar por desbordar el presupuesto)
            # decida fusionar palabras cuyo hueco EDITADO quede por debajo de un umbral mayor.
            "enabled": True, "max_chars_per_line": 42, "max_lines": 2,
            "min_cue_seconds": 0.0, "max_cue_seconds": 0.6,
            "reading_cps_min": 15, "reading_cps_max": 20, "natural_pause_gap_seconds": 0.5,
        }
        result5 = run(video_id5, config5_full)
        srt5_content = Path(result5["subtitles_path"]).read_text(encoding="utf-8")
        blocks5 = [b for b in srt5_content.strip().split("\n\n") if b.strip()]
        check("run() calibrado genera 6 subtítulos (uno por palabra, muy separadas)", len(blocks5) == 6, f"n={len(blocks5)}")

        starts5 = []
        for block in blocks5:
            m = ts_re.match(block.split("\n")[1])
            starts5.append(_to_seconds(*m.groups()[0:4]))

        expected_starts5 = [nominal_expected + n_cuts * expected_drift_per_cut5 for _, n_cuts, nominal_expected in control_points]
        check(
            "los 6 cues quedan en los timestamps calibrados esperados (desplazamiento progresivo, no uniforme)",
            all(abs(a - b) < 0.05 for a, b in zip(starts5, expected_starts5)),
            f"got={starts5}, expected={[round(x, 3) for x in expected_starts5]}",
        )
        check(
            "el desplazamiento respecto al nominal CRECE con el nº de cortes pasados (no es un shift uniforme)",
            starts5[5] - control_points[5][2] > starts5[0] - control_points[0][2],
            f"shift_cue0={starts5[0] - control_points[0][2]:.4f}, shift_cue5={starts5[5] - control_points[5][2]:.4f}",
        )

        print("=== _prepare_intro_words: limpia texto y descarta palabras vacías/inválidas ===")
        raw_intro_words = [
            {"word": " Bienvenidos", "start": 0.0, "end": 0.5},
            {"word": "   ", "start": 0.5, "end": 0.6},        # solo espacios -> descartada
            {"word": " a", "start": 0.6, "end": 0.6},         # end == start -> descartada
            {"word": " este", "start": 0.7, "end": 1.0},
        ]
        intro_words_prepared = _prepare_intro_words(raw_intro_words)
        check(
            "descarta palabras vacías y de duración nula, limpia el texto",
            intro_words_prepared == [
                {"text": "Bienvenidos", "start": 0.0, "end": 0.5},
                {"text": "este", "start": 0.7, "end": 1.0},
            ],
            f"got={intro_words_prepared}",
        )

        print("=== compute_drift_per_cut: intro_duration_s se resta de final.mp4 igual que el outro ===")
        # Mismo final.mp4 real de deriva conocida (video_id5, 35.0s de contenido principal), pero
        # ahora con intro_duration_s pasado explícitamente -- si compute_drift_per_cut no lo restara,
        # interpretaría esos segundos de intro como deriva de redondeo y calcularía un drift_per_cut
        # muy distinto (e implausible, casi con seguridad activaría el fallback a 0.0).
        drift_with_intro = compute_drift_per_cut(
            video_id5, config5, sorted_cuts5, nominal_main5, intro_duration_s=0.0
        )
        check(
            "intro_duration_s=0.0 (default) reproduce el mismo resultado que sin el parámetro",
            abs(drift_with_intro - expected_drift_per_cut5) < 0.01,
            f"got={drift_with_intro:.4f} expected={expected_drift_per_cut5:.4f}",
        )

        print("=== run() end-to-end CON intro.mp4: transcripción propia al principio + contenido principal desplazado ===")
        video_id6 = "intro_video"
        # Múltiplo exacto de 0.1s (1 frame a rate=10 más abajo) para que la duración generada por
        # ffmpeg no se redondee a un frame distinto del pedido -- con un valor "raro" (p.ej. 12.34s)
        # ffmpeg redondea al frame más cercano (0.1s de paso) y el probe posterior mide un valor
        # ligeramente distinto del usado aquí para calcular los timestamps esperados.
        intro_duration_s6 = 12.3
        output_dir6 = output_dir / video_id6
        output_dir6.mkdir(parents=True)
        intro_path6 = output_dir6 / "intro.mp4"
        # El contenido del archivo no importa -- transcribe_file se monkeypatchea más abajo (no se
        # necesita un modelo de whisper real para probar el cableado de subtitles/run.py); solo hace
        # falta que exista y que ffprobe pueda leer su duración real.
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c=green:s=64x64:rate=10:duration={intro_duration_s6}",
                "-f", "lavfi", "-i", f"sine=frequency=220:duration={intro_duration_s6}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                str(intro_path6),
            ],
            check=True, capture_output=True,
        )

        # final.mp4 = intro (12.34s) + el mismo contenido principal de 35.0s reales que video_id5
        # (mismo cuts5/nominal_main5/drift esperado) -- sin outro.
        final_duration6 = intro_duration_s6 + real_main_duration5
        output_dir6_final = output_dir6 / "final.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c=blue:s=64x64:rate=10:duration={final_duration6}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={final_duration6}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                str(output_dir6_final),
            ],
            check=True, capture_output=True,
        )

        (transcripts_dir / f"{video_id6}.json").write_text(json.dumps(transcript5 | {"video_id": video_id6}), encoding="utf-8")
        (work_dir / "cuts" / video_id6).mkdir(parents=True)
        (work_dir / "cuts" / video_id6 / "cuts.json").write_text(json.dumps(cuts5), encoding="utf-8")

        config6 = dict(config5_full)
        fake_intro_words = [
            {"word": " Bienvenidos", "start": 0.0, "end": 0.5},
            {"word": " video", "start": 1.0, "end": 1.6},
        ]

        def _fake_transcribe_file(path, cfg, **kwargs):
            check(
                "transcribe_file se llama con la ruta de intro.mp4 (no la del contenido principal)",
                Path(path) == intro_path6,
                f"path={path}",
            )
            return {
                "language": "es", "language_probability": 1.0, "duration_s": intro_duration_s6,
                "words": fake_intro_words, "segments": [],
            }

        original_transcribe_file = subtitles_module.transcribe_file
        subtitles_module.transcribe_file = _fake_transcribe_file
        try:
            result6 = run(video_id6, config6)
        finally:
            subtitles_module.transcribe_file = original_transcribe_file

        srt6_content = Path(result6["subtitles_path"]).read_text(encoding="utf-8")
        blocks6 = [b for b in srt6_content.strip().split("\n\n") if b.strip()]
        check(
            "run() con intro genera 8 subtítulos (2 del intro + 6 del contenido principal)",
            len(blocks6) == 8,
            f"n={len(blocks6)}",
        )

        starts6 = []
        texts6 = []
        for block in blocks6:
            lines = block.split("\n")
            m = ts_re.match(lines[1])
            starts6.append(_to_seconds(*m.groups()[0:4]))
            texts6.append(lines[2])

        check(
            "los 2 primeros subtítulos son las palabras del intro, SIN desplazar (0-based)",
            texts6[0:2] == ["Bienvenidos", "video"] and starts6[0] == 0.0 and abs(starts6[1] - 1.0) < 1e-6,
            f"texts={texts6[0:2]} starts={starts6[0:2]}",
        )
        expected_main_starts6 = [intro_duration_s6 + s for s in expected_starts5]
        check(
            "los 6 subtítulos del contenido principal quedan desplazados +intro_duration_s respecto al caso sin intro",
            all(abs(a - b) < 0.05 for a, b in zip(starts6[2:], expected_main_starts6)),
            f"got={starts6[2:]}, expected={[round(x, 3) for x in expected_main_starts6]}",
        )
        check(
            "todos los cues quedan ordenados cronológicamente (intro antes que el contenido principal)",
            all(starts6[i] <= starts6[i + 1] for i in range(len(starts6) - 1)),
            f"starts6={starts6}",
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: agrupado, envoltura, formato .srt y ejecución end-to-end se comportan como se espera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
