"""
Test sintético (sin llamar a la API real de Claude) de
src/detect_chapters/run.py.

Dos partes:

1. detect_chapters_with_claude con un cliente FALSO inyectado (mismo
   patrón de inyección que `detector` en detect_intro_face_cut de
   detect_cuts/run.py) -- confirma que una transcripción con temas
   claramente diferenciados (cambio de juego A a juego B) se traduce
   correctamente en la lista {timestamp_original_s, title} en bruto, y
   que la llamada a Claude se construye con los parámetros esperados
   (modelo de la config, output_format estructurado). No prueba que
   Claude "sepa" detectar temas -- eso se valida por separado contra
   vídeos reales (ver status.md) -- solo el cableado transcript -> prompt
   -> respuesta parseada.

2. remap_chapters_to_edited_timeline de forma aislada (sin pasar por
   Claude en absoluto), cubriendo el caso pedido explícitamente: un
   capítulo cuyo timestamp ORIGINAL cae DENTRO de un tramo cortado, más
   los casos de la intro forzada a 0.0 y la separación mínima aplicada
   sobre la línea de tiempo YA EDITADA.

Uso:
    cd <repo_root>
    python tests/test_detect_chapters.py

Sin red ni vídeo: termina en menos de un segundo. Código de salida 0 si
todas las comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.detect_chapters.run import (  # noqa: E402
    _ChapterModel,
    _ChaptersResponseModel,
    detect_chapters_with_claude,
    remap_chapters_to_edited_timeline,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


class _FakeResponse:
    def __init__(self, stop_reason: str, parsed_output):
        self.stop_reason = stop_reason
        self.parsed_output = parsed_output


class _FakeMessages:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.messages = _FakeMessages(response)


def _make_transcript_with_two_themes() -> dict:
    """
    Segmentos claramente separados en dos temas (juego A hasta ~t=490s,
    juego B desde t=500s) -- el propio texto no se analiza en este test
    (el cliente es falso), pero mantiene la forma real de
    data/transcripts/<video_id>.json que consume _format_transcript_for_prompt.
    """
    segments = [
        {"id": 1, "start": 0.0, "end": 8.0, "text": "hola a todos bienvenidos al directo de hoy"},
        {"id": 2, "start": 60.0, "end": 68.0, "text": "vamos a jugar al juego A durante un rato"},
        {"id": 3, "start": 300.0, "end": 308.0, "text": "seguimos con el juego A, esto va muy bien"},
        {"id": 4, "start": 500.0, "end": 508.0, "text": "cambiamos ahora al juego B, un juego totalmente distinto"},
        {"id": 5, "start": 1200.0, "end": 1208.0, "text": "seguimos con el juego B hasta el final del directo"},
    ]
    return {"duration_s": 1800.0, "segments": segments}


def main() -> int:
    config = {
        "detect_chapters": {"claude_model": "claude-sonnet-5", "min_chapter_seconds": 120},
        "_env": {"anthropic_api_key": "fake-key-not-used"},
    }

    print("=== Parte 1: detect_chapters_with_claude con cliente falso ===")
    transcript = _make_transcript_with_two_themes()
    fake_output = _ChaptersResponseModel(
        chapters=[
            _ChapterModel(timestamp_original_s=0.0, title="Introducción"),
            _ChapterModel(timestamp_original_s=500.0, title="Empieza el juego B"),
            _ChapterModel(timestamp_original_s=1200.0, title="Recta final del juego B"),
        ]
    )
    fake_client = _FakeClient(_FakeResponse(stop_reason="end_turn", parsed_output=fake_output))

    raw_chapters = detect_chapters_with_claude(transcript, config, client=fake_client)

    check(
        "detect_chapters_with_claude devuelve 3 capítulos en bruto",
        len(raw_chapters) == 3,
        f"raw_chapters={raw_chapters}",
    )
    check(
        "cada capítulo en bruto conserva timestamp_original_s y title",
        raw_chapters == [
            {"timestamp_original_s": 0.0, "title": "Introducción"},
            {"timestamp_original_s": 500.0, "title": "Empieza el juego B"},
            {"timestamp_original_s": 1200.0, "title": "Recta final del juego B"},
        ],
        f"raw_chapters={raw_chapters}",
    )
    check(
        "se llamó a client.messages.parse exactamente una vez",
        len(fake_client.messages.calls) == 1,
        f"n llamadas={len(fake_client.messages.calls)}",
    )
    call = fake_client.messages.calls[0] if fake_client.messages.calls else {}
    check(
        "la llamada usa el modelo de la config",
        call.get("model") == "claude-sonnet-5",
        f"model={call.get('model')!r}",
    )
    check(
        "la llamada pide output estructurado (_ChaptersResponseModel)",
        call.get("output_format") is _ChaptersResponseModel,
        f"output_format={call.get('output_format')!r}",
    )
    prompt_text = call.get("messages", [{}])[0].get("content", "")
    check(
        "el prompt incluye el texto de los segmentos de la transcripción",
        "juego A" in prompt_text and "juego B" in prompt_text,
        "el prompt no contiene el texto esperado de los dos temas",
    )

    # --- Refusal: debe propagarse como error, no como lista vacía silenciosa ---
    refusal_client = _FakeClient(_FakeResponse(stop_reason="refusal", parsed_output=None))
    try:
        detect_chapters_with_claude(transcript, config, client=refusal_client)
        check("un refusal de Claude lanza RuntimeError", False, "no se lanzó ninguna excepción")
    except RuntimeError:
        check("un refusal de Claude lanza RuntimeError", True, "")

    print("\n=== Parte 2: remap_chapters_to_edited_timeline (sin Claude) ===")

    # Caso principal pedido: un capítulo cae DENTRO de un tramo cortado.
    # duration=1800s, un único corte [490, 505] (15s) -- el capítulo en
    # t=500 cae dentro del corte, más cerca del final (505, distancia 5)
    # que del inicio del tramo conservado anterior (490, distancia 10),
    # así que debe ajustarse a 505 antes de remapear.
    duration = 1800.0
    cuts = [{"start": 490.0, "end": 505.0, "type": "silence", "reason": "test"}]
    raw = [
        {"timestamp_original_s": 0.0, "title": "Introducción"},
        {"timestamp_original_s": 500.0, "title": "Empieza el juego B"},
        {"timestamp_original_s": 1200.0, "title": "Recta final del juego B"},
    ]
    result = remap_chapters_to_edited_timeline(raw, cuts, duration, config)
    expected = [
        {"timestamp_s": 0.0, "title": "Introducción"},
        {"timestamp_s": 490.0, "title": "Empieza el juego B"},
        {"timestamp_s": 1185.0, "title": "Recta final del juego B"},
    ]
    check(
        "capítulo dentro de un corte: se ajusta al tramo conservado más cercano y remapea bien",
        result == expected,
        f"result={result} expected={expected}",
    )

    # Caso simétrico de control: el capítulo cae en un corte donde el
    # tramo conservado ANTERIOR está más cerca. Dos cortes [490,495] y
    # [500,700] dejan un tramo conservado corto (495,500) justo antes de un
    # hueco grande -- keep_segments = (0,490), (495,500), (700,duration).
    # Un capítulo en t=550 (dentro del segundo corte) está a distancia 55
    # del inicio del tramo anterior (495) pero a 150 del inicio del
    # siguiente (700), así que debe ajustarse hacia ATRÁS, a 495.
    duration2 = 2000.0
    cuts_prev_closer = [
        {"start": 490.0, "end": 495.0, "type": "silence", "reason": "test"},
        {"start": 500.0, "end": 700.0, "type": "silence", "reason": "corte grande"},
    ]
    result_prev = remap_chapters_to_edited_timeline(
        [{"timestamp_original_s": 550.0, "title": "X"}], cuts_prev_closer, duration2, config
    )
    # 550 esta dentro de [500,700) -> se ajusta a 495 (inicio del tramo
    # conservado anterior, (495,500), más cercano que el siguiente en 700)
    # -> remapeado: 495 - 5 (único corte antes de 495: 490-495) = 490.0.
    # No es el primer capítulo real (no hay ninguno en 0) -> se antepone
    # la intro genérica en 0.0.
    check(
        "capítulo dentro de un corte con el tramo anterior más cerca: se ajusta hacia atrás",
        result_prev == [
            {"timestamp_s": 0.0, "title": "Introducción"},
            {"timestamp_s": 490.0, "title": "X"},
        ],
        f"result_prev={result_prev}",
    )

    # Primer capítulo lejos de 0 -> se antepone una intro genérica.
    result_no_intro = remap_chapters_to_edited_timeline(
        [{"timestamp_original_s": 300.0, "title": "Primer tema real"}], [], 1000.0, config
    )
    check(
        "sin capítulo cerca de 0: se antepone 'Introducción' en 0.0",
        result_no_intro[0] == {"timestamp_s": 0.0, "title": "Introducción"}
        and result_no_intro[1] == {"timestamp_s": 300.0, "title": "Primer tema real"},
        f"result_no_intro={result_no_intro}",
    )

    # Primer capítulo YA prácticamente en 0 -> NO se antepone una intro
    # duplicada, solo se fuerza el valor exacto a 0.0.
    result_already_zero = remap_chapters_to_edited_timeline(
        [{"timestamp_original_s": 0.2, "title": "Ya es la intro"}], [], 1000.0, config
    )
    check(
        "capítulo ya casi en 0: se fuerza a 0.0 exacto sin duplicar",
        result_already_zero == [{"timestamp_s": 0.0, "title": "Ya es la intro"}],
        f"result_already_zero={result_already_zero}",
    )

    # Separación mínima sobre la línea de tiempo EDITADA, no la original:
    # un corte enorme [10, 900] hace que un capítulo a t=950 (940s después
    # de la intro en el ORIGINAL, de sobra) quede a solo 60s de la intro
    # en el vídeo YA EDITADO (950 - 890 de corte = 60 < 120) -> debe
    # descartarse. Un capítulo posterior a t=1800 sí queda lo bastante
    # lejos tras el remapeo (1800 - 890 = 910 >= 120) -> ese sí se
    # conserva, para contrastar ambos casos en el mismo resultado.
    cuts_big = [{"start": 10.0, "end": 900.0, "type": "silence", "reason": "corte enorme"}]
    raw_close_after_cut = [
        {"timestamp_original_s": 0.0, "title": "Introducción"},
        {"timestamp_original_s": 950.0, "title": "Demasiado cerca tras el corte"},
        {"timestamp_original_s": 1800.0, "title": "Suficientemente lejos tras el corte"},
    ]
    result_min_sep = remap_chapters_to_edited_timeline(raw_close_after_cut, cuts_big, 2000.0, config)
    check(
        "separacion minima se aplica sobre la linea YA editada, no la original",
        result_min_sep == [
            {"timestamp_s": 0.0, "title": "Introducción"},
            {"timestamp_s": 910.0, "title": "Suficientemente lejos tras el corte"},
        ],
        f"result_min_sep={result_min_sep}",
    )

    print("\n=== Parte 3: remap_chapters_to_edited_timeline con intro_duration_s (2026-08-10) ===")

    # intro_duration_s=0.0 (default) debe reproducir EXACTAMENTE el resultado sin intro (Parte 2, caso 1).
    result_no_shift = remap_chapters_to_edited_timeline(raw, cuts, duration, config, intro_duration_s=0.0)
    check(
        "intro_duration_s=0.0 es idéntico al comportamiento sin el parámetro (retrocompatible)",
        result_no_shift == expected,
        f"result_no_shift={result_no_shift}",
    )

    # Con un intro real de 90s: SIEMPRE 'Introducción' en 0.0 (representando el intro, no un
    # capítulo genérico condicional) y el resto desplazado exactamente +90s.
    result_with_intro = remap_chapters_to_edited_timeline(raw, cuts, duration, config, intro_duration_s=90.0)
    check(
        "con intro real: 'Introducción' fija en 0.0 y el resto desplazado +intro_duration_s",
        result_with_intro == [
            {"timestamp_s": 0.0, "title": "Introducción"},
            {"timestamp_s": 580.0, "title": "Empieza el juego B"},
            {"timestamp_s": 1275.0, "title": "Recta final del juego B"},
        ],
        f"result_with_intro={result_with_intro}",
    )

    # Un capítulo detectado por Claude cerca del inicio del vídeo original NO colapsa con la
    # intro real: se desplaza +intro_duration_s como cualquier otro (a diferencia del caso SIN
    # intro real, donde un capítulo casi en 0 se fusiona con el "Introducción" genérico) --
    # siempre que la separación resultante siga cumpliendo min_chapter_seconds (120s aquí).
    result_near_zero_with_intro = remap_chapters_to_edited_timeline(
        [{"timestamp_original_s": 50.0, "title": "Primer tema real"}], [], 1000.0, config, intro_duration_s=90.0
    )
    check(
        "con intro real: un capítulo detectado cerca de 0 se desplaza +90s en vez de colapsar con la intro",
        result_near_zero_with_intro == [
            {"timestamp_s": 0.0, "title": "Introducción"},
            {"timestamp_s": 140.0, "title": "Primer tema real"},
        ],
        f"result={result_near_zero_with_intro}",
    )

    # Separación mínima re-aplicada tras el desplazamiento: un intro MÁS CORTO que
    # min_chapter_seconds (120s) deja el primer capítulo real demasiado cerca de la intro -> se
    # descarta (mismo criterio que ya se aplica entre capítulos consecutivos).
    result_intro_too_close = remap_chapters_to_edited_timeline(
        [{"timestamp_original_s": 10.0, "title": "Primer tema real"}], [], 1000.0, config, intro_duration_s=30.0
    )
    check(
        "intro corta: un capítulo real que queda a <120s de la intro tras desplazarse se descarta",
        result_intro_too_close == [{"timestamp_s": 0.0, "title": "Introducción"}],
        f"result={result_intro_too_close}",
    )

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: detect_chapters_with_claude y remap_chapters_to_edited_timeline se comportan como se espera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
