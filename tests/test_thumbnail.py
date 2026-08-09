"""
Test sintético (sin red, sin llamar a Claude/Gemini de verdad) de
src/thumbnail/run.py.

Cubre:
1. _group_speech_runs / _wrap_headline: funciones puras.
2. _select_face_frame contra un vídeo sintético pequeño, con
   detect_faces_at INYECTADO (mismo patrón que el parámetro `detector` de
   detect_intro_face_cut) -- confirma que se elige el candidato con cara
   detectada cuando solo uno de varios la tiene, y que cae a un frame de
   respaldo sin lanzar excepción si ninguno la tiene.
3. _select_gameplay_frame contra un vídeo sintético con movimiento SOLO en
   una ventana de tiempo conocida fuera de facecam_region -- confirma que
   elige un frame de esa ventana.
4. _extract_headline con un cliente de Claude FALSO inyectado (mismo
   patrón que detect_chapters_with_claude).
5. _enhance_with_gemini con un cliente de Gemini FALSO inyectado --
   confirma el camino de éxito y los tres caminos de fallback (excepción,
   sin output_image, datos indecodificables) caen todos de vuelta a la
   imagen compuesta sin modificar.
6. _compose_thumbnail -- tamaño de canvas exacto y que quemar el texto
   cambia píxeles respecto a no quemarlo.

Uso:
    cd <repo_root>
    python tests/test_thumbnail.py

Genera sus propios vídeos/imágenes en un directorio temporal (no toca
data/ ni llama a ninguna API real). Código de salida 0 si todas las
comprobaciones pasan, 1 si alguna falla.
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

from src.thumbnail.run import (  # noqa: E402
    _HeadlineModel,
    _CANVAS_HEIGHT,
    _CANVAS_WIDTH,
    _compose_thumbnail,
    _enhance_with_gemini,
    _extract_headline,
    _group_speech_runs,
    _select_face_frame,
    _select_gameplay_frame,
    _wrap_headline,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# Fakes para Claude y Gemini (mismo patrón que test_detect_chapters.py)
# ---------------------------------------------------------------------------

class _FakeAnthropicResponse:
    def __init__(self, stop_reason: str, parsed_output):
        self.stop_reason = stop_reason
        self.parsed_output = parsed_output


class _FakeAnthropicMessages:
    def __init__(self, response: _FakeAnthropicResponse):
        self._response = response
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response: _FakeAnthropicResponse):
        self.messages = _FakeAnthropicMessages(response)


class _FakeImagePart:
    def __init__(self, data):
        self.data = data


class _FakeGeminiInteraction:
    def __init__(self, output_image):
        self.output_image = output_image


class _FakeGeminiInteractions:
    def __init__(self, result):
        self._result = result  # una excepción para simular fallo, o una _FakeGeminiInteraction
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeGeminiClient:
    def __init__(self, result):
        self.interactions = _FakeGeminiInteractions(result)


def _png_bytes_of(color: tuple[int, int, int]) -> bytes:
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (8, 8), color=color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Vídeos sintéticos
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 420, 200
FPS = 10.0
FACECAM_REGION = {"x": 20, "y": 20, "w": 160, "h": 120}
_RNG = np.random.default_rng(20260809)
_BACKGROUND = _RNG.integers(0, 40, size=(HEIGHT, WIDTH, 3), dtype=np.uint8)


def _write_simple_video(path: Path, duration_s: float) -> None:
    """Vídeo con textura fija, sin movimiento -- para el test de _select_face_frame (la detección va inyectada)."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    n_frames = int(duration_s * FPS)
    for _ in range(n_frames):
        writer.write(_BACKGROUND.copy())
    writer.release()


def _write_gameplay_video(path: Path, duration_s: float, active_window: tuple[float, float]) -> None:
    """
    Vídeo con un marcador que rebota SOLO dentro de `active_window`
    (segundos), en un contenedor FUERA de FACECAM_REGION; el resto del
    tiempo el marcador se queda fijo en su posición de reposo -- mismo
    patrón de margen que tests/test_motion_facecam_exclusion.py.
    """
    container = {"x": 250, "y": 50, "w": 100, "h": 100}
    object_size = (40, 40)
    margin = 15
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    n_frames = int(duration_s * FPS)
    inner_x0 = container["x"] + margin
    inner_y0 = container["y"] + margin
    max_offset = max(0, (container["w"] - 2 * margin) - object_size[0])
    win_start, win_end = active_window
    for i in range(n_frames):
        t = i / FPS
        frame = _BACKGROUND.copy()
        if win_start <= t < win_end and max_offset > 0:
            local = (t - win_start) / (win_end - win_start)
            period = 2.0
            phase = (local * period) % period
            frac = phase if phase <= 1.0 else 2.0 - phase
            offset = round(frac * max_offset)
        else:
            offset = 0
        x, y = inner_x0 + offset, inner_y0
        frame[y:y + object_size[1], x:x + object_size[0]] = 255
        writer.write(frame)
    writer.release()


def _make_speech_transcript() -> dict:
    """Dos tramos de habla larga (>=10s, separados por un hueco de 5s > 1.2s) -- 4 candidatos en orden."""
    words = []
    idx = 0
    for run_start, run_end in [(0.0, 15.0), (20.0, 35.0)]:
        t = run_start
        while t < run_end:
            words.append({"word": f"w{idx}", "start": round(t, 3), "end": round(t + 0.3, 3)})
            t += 0.4
            idx += 1
    return {"duration_s": 40.0, "segments": [], "words": words}


def main() -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="thumbnail_test_"))
    try:
        config = {
            "paths": {"raw": str(work_dir), "transcripts": str(work_dir), "output": str(work_dir)},
            "facecam_region": FACECAM_REGION,
            "edit": {"long_speech_min_seconds": 10.0, "long_speech_gap_seconds": 1.2},
            "thumbnail": {"face_candidate_segments": 3, "gameplay_candidate_count": 5},
        }

        print("=== Funciones puras ===")
        transcript = _make_speech_transcript()
        runs = _group_speech_runs(transcript, config)
        check(
            "_group_speech_runs detecta exactamente 2 tramos, empezando en 0.0 y 20.0, ambos >= 10s",
            len(runs) == 2 and runs[0][0] == 0.0 and runs[1][0] == 20.0
            and (runs[0][1] - runs[0][0]) >= 10.0 and (runs[1][1] - runs[1][0]) >= 10.0,
            f"runs={runs}",
        )
        check("_wrap_headline deja 1-2 palabras en una línea", _wrap_headline("GANA") == ["GANA"])
        check(
            "_wrap_headline parte titulares largos en 2 líneas",
            _wrap_headline("ESTO NO ME LO ESPERABA NUNCA") == ["ESTO NO ME", "LO ESPERABA NUNCA"],
            f"={_wrap_headline('ESTO NO ME LO ESPERABA NUNCA')}",
        )

        print("=== _select_face_frame (vídeo sintético + detector inyectado) ===")
        face_video_id = "face_test"
        _write_simple_video(work_dir / f"{face_video_id}.mp4", duration_s=40.0)
        with open(work_dir / f"{face_video_id}.json", "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(transcript, f)

        # Candidatos = 35%/70% de cada tramo, en el mismo orden que
        # _select_face_frame -- derivado de `runs` en vez de calculado a
        # mano, para no arrastrar errores de aritmética al test.
        expected_candidate_times = []
        for start, end in runs:
            dur = end - start
            expected_candidate_times.append(start + dur * 0.35)
            expected_candidate_times.append(start + dur * 0.7)
        third_candidate_t = expected_candidate_times[2]

        call_state = {"count": 0}

        def detect_only_third_call(frame, crop_box):
            i = call_state["count"]
            call_state["count"] += 1
            if i == 2:
                return np.array([[0, 0, 10, 10, *([0.0] * 10), 0.95]])
            return None

        chosen_frame = _select_face_frame(face_video_id, config, detect_faces_at=detect_only_third_call)

        cap = cv2.VideoCapture(str(work_dir / f"{face_video_id}.mp4"), cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_POS_MSEC, third_candidate_t * 1000)
        ok, expected_frame = cap.read()
        cap.release()

        check(
            "se llamó al detector inyectado 4 veces (una por candidato)",
            call_state["count"] == 4,
            f"count={call_state['count']}",
        )
        check(
            f"el candidato con cara detectada (3º, t={third_candidate_t:.3f}s) es el elegido",
            ok and np.array_equal(chosen_frame, expected_frame),
            f"chosen == frame at t={third_candidate_t:.3f}s: {ok and np.array_equal(chosen_frame, expected_frame)}",
        )

        # Ningún candidato con cara -> no debe lanzar excepción, cae al de respaldo.
        fallback_frame = _select_face_frame(face_video_id, config, detect_faces_at=lambda frame, box: None)
        check(
            "sin ningún candidato con cara detectada, cae a un frame de respaldo sin fallar",
            isinstance(fallback_frame, np.ndarray) and fallback_frame.shape == (HEIGHT, WIDTH, 3),
            f"fallback_frame={type(fallback_frame)}",
        )

        print("=== _select_face_frame ignora tramos dentro de un corte ya detectado (p.ej. intro) ===")
        # cuts.json marca [0, 18) como corte de intro -- cubre el primer tramo
        # de habla (0.0-15.1s) por completo, pero no el segundo (20.0-35.1s).
        # Encontrado con una generación real contra dinoblade_1: sin este
        # filtro, face_candidate_segments=3 podía elegir sus 3 tramos
        # exclusivamente dentro de la intro (~17 min sin facecam_region en su
        # disposición normal), produciendo un recorte de cara en blanco.
        cuts_dir = work_dir / face_video_id
        cuts_dir.mkdir(exist_ok=True)
        with open(cuts_dir / "cuts.json", "w", encoding="utf-8") as f:
            import json as _json
            _json.dump([{"start": 0.0, "end": 18.0, "type": "intro", "reason": "intro sin cara"}], f)
        config_with_cuts = {**config, "paths": {**config["paths"], "cuts": str(work_dir)}}

        run2_start, run2_end = runs[1]
        run2_dur = run2_end - run2_start
        run2_candidate_times = [run2_start + run2_dur * 0.35, run2_start + run2_dur * 0.7]
        run2_frames = []
        cap = cv2.VideoCapture(str(work_dir / f"{face_video_id}.mp4"), cv2.CAP_FFMPEG)
        for t in run2_candidate_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, f_ = cap.read()
            run2_frames.append(f_ if ok else None)
        cap.release()

        chosen_after_cut_filter = _select_face_frame(
            face_video_id, config_with_cuts, detect_faces_at=lambda frame, box: np.array([[0, 0, 10, 10, *([0.0] * 10), 0.9]])
        )
        check(
            "el candidato elegido viene del 2º tramo (el 1º cae dentro del corte de intro)",
            any(f_ is not None and np.array_equal(chosen_after_cut_filter, f_) for f_ in run2_frames),
            "chosen_after_cut_filter coincide con alguno de los frames candidatos del 2º tramo",
        )

        print("=== _select_gameplay_frame (vídeo sintético, movimiento en ventana conocida) ===")
        gameplay_video_id = "gameplay_test"
        # gameplay_candidate_count=5 sobre duración 10s, margen 10% -> candidatos en
        # [1.0, 3.0, 5.0, 7.0, 9.0]; movimiento SOLO en [4.0, 6.0) -> solo el candidato
        # t=5.0 (comparando 4.5 vs 5.0) debería mostrar variación no nula.
        _write_gameplay_video(work_dir / f"{gameplay_video_id}.mp4", duration_s=10.0, active_window=(4.0, 6.0))
        chosen_gameplay = _select_gameplay_frame(gameplay_video_id, config)

        cap = cv2.VideoCapture(str(work_dir / f"{gameplay_video_id}.mp4"), cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_POS_MSEC, 5.0 * 1000)
        ok, expected_gameplay_frame = cap.read()
        cap.release()

        check(
            "se elige un frame de la ventana de movimiento esperada (t=5.0s)",
            ok and np.array_equal(chosen_gameplay, expected_gameplay_frame),
            f"chosen == frame at t=5.0s: {ok and np.array_equal(chosen_gameplay, expected_gameplay_frame)}",
        )

        print("=== _extract_headline (cliente de Claude falso) ===")
        fake_output = _HeadlineModel(headline="NO ME LO ESPERABA")
        fake_client = _FakeAnthropicClient(_FakeAnthropicResponse(stop_reason="end_turn", parsed_output=fake_output))
        transcript_with_segments = {
            "segments": [{"start": 12.0, "text": "no me lo esperaba para nada"}],
        }
        headline = _extract_headline(transcript_with_segments, {"detect_chapters": {"claude_model": "claude-sonnet-5"}}, client=fake_client)
        check("_extract_headline devuelve el titular del cliente falso", headline == "NO ME LO ESPERABA", f"headline={headline!r}")
        check(
            "la llamada usa el modelo de la config",
            fake_client.messages.calls[0].get("model") == "claude-sonnet-5",
            f"model={fake_client.messages.calls[0].get('model')!r}",
        )

        refusal_client = _FakeAnthropicClient(_FakeAnthropicResponse(stop_reason="refusal", parsed_output=None))
        try:
            _extract_headline(transcript_with_segments, {}, client=refusal_client)
            check("un refusal de Claude lanza RuntimeError", False, "no se lanzó ninguna excepción")
        except RuntimeError:
            check("un refusal de Claude lanza RuntimeError", True, "")

        print("=== _compose_thumbnail ===")
        face_frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        face_frame[:, :] = (200, 100, 50)
        gameplay_frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        gameplay_frame[:, :] = (10, 10, 10)

        composed_no_text = _compose_thumbnail(face_frame, gameplay_frame, FACECAM_REGION, "TITULO", burn_text=False)
        composed_with_text = _compose_thumbnail(face_frame, gameplay_frame, FACECAM_REGION, "TITULO", burn_text=True)
        check(
            "_compose_thumbnail produce un canvas de exactamente 1280x720",
            composed_no_text.size == (_CANVAS_WIDTH, _CANVAS_HEIGHT) == (1280, 720),
            f"size={composed_no_text.size}",
        )
        diff_pixels = np.array(composed_no_text) != np.array(composed_with_text)
        check(
            "quemar el texto cambia píxeles respecto a no quemarlo",
            bool(diff_pixels.any()),
            "las dos composiciones son idénticas",
        )

        print("=== _enhance_with_gemini (cliente de Gemini falso) ===")
        success_bytes = _png_bytes_of((1, 2, 3))
        import base64 as _base64
        success_client = _FakeGeminiClient(
            _FakeGeminiInteraction(output_image=_FakeImagePart(data=_base64.b64encode(success_bytes).decode("utf-8")))
        )
        result_img, enhanced = _enhance_with_gemini(composed_no_text, "TITULO", {"thumbnail": {}}, client=success_client)
        check("camino de éxito: enhanced=True", enhanced is True)
        check("camino de éxito: la imagen resultante viene de Gemini (no la original)", result_img.size == (8, 8), f"size={result_img.size}")

        exception_client = _FakeGeminiClient(RuntimeError("fallo simulado de red"))
        result_img2, enhanced2 = _enhance_with_gemini(composed_no_text, "TITULO", {"thumbnail": {}}, client=exception_client)
        check(
            "camino de fallo (excepción): cae a la composición original",
            enhanced2 is False and result_img2 is composed_no_text,
        )

        no_image_client = _FakeGeminiClient(_FakeGeminiInteraction(output_image=None))
        result_img3, enhanced3 = _enhance_with_gemini(composed_no_text, "TITULO", {"thumbnail": {}}, client=no_image_client)
        check(
            "camino de fallo (sin output_image): cae a la composición original",
            enhanced3 is False and result_img3 is composed_no_text,
        )

        bad_data_client = _FakeGeminiClient(
            _FakeGeminiInteraction(output_image=_FakeImagePart(data="esto no es base64 de una imagen valida!!"))
        )
        result_img4, enhanced4 = _enhance_with_gemini(composed_no_text, "TITULO", {"thumbnail": {}}, client=bad_data_client)
        check(
            "camino de fallo (datos indecodificables): cae a la composición original",
            enhanced4 is False and result_img4 is composed_no_text,
        )

        no_key_result, no_key_enhanced = _enhance_with_gemini(
            composed_no_text, "TITULO", {"thumbnail": {}, "_env": {"gemini_api_key": None}}, client=None
        )
        check(
            "sin GEMINI_API_KEY configurada: cae a la composición original sin intentar llamar",
            no_key_enhanced is False and no_key_result is composed_no_text,
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: selección de frames, titular y mejora con Gemini se comportan como se espera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
