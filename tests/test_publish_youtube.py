"""
Test sintético (sin red, sin OAuth real, sin subir nada de verdad) de
src/publish/youtube.py.

Cubre:
1. _build_video_body: título/placeholder, descripción, privacidad --
   función pura.
2. _description_from_chapters / _thumbnail_path / _final_video_path:
   helpers de filesystem (chapters.txt/thumbnail.png opcionales,
   final.mp4 obligatorio).
3. run(execute=False) con un servicio de YouTube FALSO inyectado (mismo
   patrón `client` que thumbnail/detect_chapters): confirma que la
   petición (videos().insert) se construye con el body correcto y que
   NUNCA se llama a next_chunk()/execute() -- no se sube nada.
4. run(execute=True) con el mismo servicio falso: confirma que sí se
   agota next_chunk() hasta la respuesta final, que se propaga el
   youtube_video_id devuelto, y que thumbnails().set() se llama (con ese
   mismo id) solo si hay thumbnail.png.
5. _execute_resumable_upload: reintenta ante HttpError 503 (transitorio)
   y no ante un HttpError 404 (no debe reintentar, debe propagar).

Uso:
    cd <repo_root>
    python tests/test_publish_youtube.py

No toca data/ real, no abre ningún navegador, no llama a Google. Código
de salida 0 si todas las comprobaciones pasan, 1 si alguna falla.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import httplib2
from googleapiclient.errors import HttpError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.publish.youtube import (  # noqa: E402
    _build_video_body,
    _description_from_chapters,
    _execute_resumable_upload,
    _final_video_path,
    _thumbnail_path,
    run,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FALLO"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        failures.append(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# Servicio de YouTube falso (mismo patrón que los fakes de Claude/Gemini en
# tests/test_thumbnail.py): captura las llamadas, nunca toca la red.
# ---------------------------------------------------------------------------

class _FakeInsertRequest:
    def __init__(self, kwargs: dict, final_response: dict, chunks_before_done: int):
        self.kwargs = kwargs
        self._final_response = final_response
        self._chunks_before_done = chunks_before_done
        self.next_chunk_calls = 0

    def next_chunk(self):
        self.next_chunk_calls += 1
        if self.next_chunk_calls < self._chunks_before_done:
            return {"progress": self.next_chunk_calls}, None
        return {"progress": 1.0}, self._final_response


class _FakeThumbnailSetRequest:
    def __init__(self, kwargs: dict):
        self.kwargs = kwargs
        self.executed = False

    def execute(self):
        self.executed = True
        return {}


class _FakeVideosResource:
    def __init__(self, final_response: dict, chunks_before_done: int = 1):
        self.insert_calls: list[dict] = []
        self._final_response = final_response
        self._chunks_before_done = chunks_before_done
        self.last_request: "_FakeInsertRequest | None" = None

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        request = _FakeInsertRequest(kwargs, self._final_response, self._chunks_before_done)
        self.last_request = request
        return request


class _FakeThumbnailsResource:
    def __init__(self):
        self.set_calls: list[dict] = []

    def set(self, **kwargs):
        self.set_calls.append(kwargs)
        return _FakeThumbnailSetRequest(kwargs)


class _FakeYoutubeService:
    def __init__(self, final_response: dict, chunks_before_done: int = 1):
        self.videos_resource = _FakeVideosResource(final_response, chunks_before_done)
        self.thumbnails_resource = _FakeThumbnailsResource()

    def videos(self):
        return self.videos_resource

    def thumbnails(self):
        return self.thumbnails_resource


def _write_dummy_file(path: Path, content: bytes = b"dummy") -> None:
    path.write_bytes(content)


def main() -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="publish_youtube_test_"))
    try:
        config = {"paths": {"output": str(work_dir)}}
        video_id = "test_video"
        output_dir = work_dir / video_id
        output_dir.mkdir(parents=True)

        print("=== _build_video_body (función pura) ===")
        body_no_title = _build_video_body(video_id, None, "desc", "private")
        check(
            "sin título: usa un placeholder que incluye el video_id",
            body_no_title["snippet"]["title"].startswith("[TÍTULO PENDIENTE]") and video_id in body_no_title["snippet"]["title"],
            f"title={body_no_title['snippet']['title']!r}",
        )
        body_with_title = _build_video_body(video_id, "  Mi Directo Épico  ", "desc", "unlisted")
        check(
            "con título: se usa tal cual (recortado de espacios)",
            body_with_title["snippet"]["title"] == "Mi Directo Épico",
            f"title={body_with_title['snippet']['title']!r}",
        )
        check("la descripción pasa tal cual", body_with_title["snippet"]["description"] == "desc")
        check("la privacidad pasa tal cual", body_with_title["status"]["privacyStatus"] == "unlisted")

        print("=== helpers de filesystem ===")
        try:
            _final_video_path(video_id, config)
            check("_final_video_path sin final.mp4 lanza FileNotFoundError", False, "no lanzó excepción")
        except FileNotFoundError:
            check("_final_video_path sin final.mp4 lanza FileNotFoundError", True, "")

        _write_dummy_file(output_dir / "final.mp4")
        resolved_video_path = _final_video_path(video_id, config)
        check("_final_video_path con final.mp4 devuelve su ruta", resolved_video_path.exists(), f"path={resolved_video_path}")

        check("_description_from_chapters sin chapters.txt devuelve cadena vacía", _description_from_chapters(video_id, config) == "")
        (output_dir / "chapters.txt").write_text("00:00 Introducción\n05:00 Boss final\n", encoding="utf-8")
        check(
            "_description_from_chapters con chapters.txt devuelve su contenido",
            _description_from_chapters(video_id, config) == "00:00 Introducción\n05:00 Boss final",
            f"desc={_description_from_chapters(video_id, config)!r}",
        )

        check("_thumbnail_path sin thumbnail.png devuelve None", _thumbnail_path(video_id, config) is None)
        _write_dummy_file(output_dir / "thumbnail.png")
        check("_thumbnail_path con thumbnail.png devuelve su ruta", _thumbnail_path(video_id, config) is not None)

        print("=== run(execute=False): construye la petición pero no sube nada ===")
        fake_service_dry = _FakeYoutubeService(final_response={"id": "should_not_be_used"})
        result_dry = run(video_id, config, title="Mi Directo", youtube_service=fake_service_dry, execute=False)
        check(
            "execute=False: se construyó exactamente 1 petición videos().insert",
            len(fake_service_dry.videos_resource.insert_calls) == 1,
            f"insert_calls={len(fake_service_dry.videos_resource.insert_calls)}",
        )
        insert_kwargs = fake_service_dry.videos_resource.insert_calls[0]
        check("execute=False: part incluye snippet y status", insert_kwargs.get("part") == "snippet,status", f"part={insert_kwargs.get('part')!r}")
        check(
            "execute=False: body.snippet.title es el título dado",
            insert_kwargs["body"]["snippet"]["title"] == "Mi Directo",
            f"title={insert_kwargs['body']['snippet']['title']!r}",
        )
        check(
            "execute=False: body.snippet.description es el contenido de chapters.txt",
            insert_kwargs["body"]["snippet"]["description"] == "00:00 Introducción\n05:00 Boss final",
        )
        check(
            "execute=False: body.status.privacyStatus es 'private' por defecto",
            insert_kwargs["body"]["status"]["privacyStatus"] == "private",
            f"privacyStatus={insert_kwargs['body']['status']['privacyStatus']!r}",
        )
        check(
            "execute=False: NUNCA se llamó a next_chunk (no se sube nada de verdad)",
            fake_service_dry.videos_resource.last_request.next_chunk_calls == 0,
        )
        check(
            "execute=False: thumbnails().set() nunca se llama",
            len(fake_service_dry.thumbnails_resource.set_calls) == 0,
        )
        check(
            "execute=False: el resultado refleja executed=False y youtube_video_id=None",
            result_dry["executed"] is False and result_dry["youtube_video_id"] is None and result_dry["thumbnail_attached"] is True,
            f"result={result_dry}",
        )

        print("=== run(execute=True): sube de verdad (contra el servicio falso) y adjunta miniatura ===")
        fake_service_real = _FakeYoutubeService(final_response={"id": "yt_abc123"}, chunks_before_done=3)
        result_real = run(video_id, config, title="Mi Directo", youtube_service=fake_service_real, execute=True)
        check(
            "execute=True: se agotó next_chunk hasta la respuesta final",
            fake_service_real.videos_resource.last_request.next_chunk_calls == 3,
            f"next_chunk_calls={fake_service_real.videos_resource.last_request.next_chunk_calls}",
        )
        check(
            "execute=True: el resultado trae el youtube_video_id devuelto",
            result_real["executed"] is True and result_real["youtube_video_id"] == "yt_abc123",
            f"result={result_real}",
        )
        check(
            "execute=True: thumbnails().set() se llamó una vez con el video_id de YouTube",
            len(fake_service_real.thumbnails_resource.set_calls) == 1
            and fake_service_real.thumbnails_resource.set_calls[0]["videoId"] == "yt_abc123",
            f"set_calls={fake_service_real.thumbnails_resource.set_calls}",
        )

        print("=== run(execute=True) sin thumbnail.png: no llama a thumbnails().set() ===")
        video_id_no_thumb = "no_thumb_video"
        output_dir2 = work_dir / video_id_no_thumb
        output_dir2.mkdir(parents=True)
        _write_dummy_file(output_dir2 / "final.mp4")
        fake_service_no_thumb = _FakeYoutubeService(final_response={"id": "yt_no_thumb"})
        run(video_id_no_thumb, config, youtube_service=fake_service_no_thumb, execute=True)
        check(
            "sin thumbnail.png: thumbnails().set() no se llama",
            len(fake_service_no_thumb.thumbnails_resource.set_calls) == 0,
        )

        print("=== _execute_resumable_upload: reintentos ante errores transitorios ===")
        real_sleep = time.sleep
        time.sleep = lambda _seconds: None  # evita esperar de verdad el backoff exponencial en el test
        try:
            attempts = {"count": 0}

            class _FlakyRequest:
                def next_chunk(self):
                    attempts["count"] += 1
                    if attempts["count"] < 3:
                        raise HttpError(httplib2.Response({"status": 503}), b"transient")
                    return {"progress": 1.0}, {"id": "recovered_after_retries"}

            response = _execute_resumable_upload(_FlakyRequest())
            check(
                "reintenta ante 503 y termina devolviendo la respuesta final",
                response == {"id": "recovered_after_retries"} and attempts["count"] == 3,
                f"response={response}, attempts={attempts['count']}",
            )

            class _PermanentlyBrokenRequest:
                def next_chunk(self):
                    raise HttpError(httplib2.Response({"status": 404}), b"not found")

            try:
                _execute_resumable_upload(_PermanentlyBrokenRequest())
                check("un HttpError no transitorio (404) se propaga sin reintentar", False, "no se lanzó ninguna excepción")
            except HttpError as exc:
                check("un HttpError no transitorio (404) se propaga sin reintentar", exc.resp.status == 404, f"status={exc.resp.status}")
        finally:
            time.sleep = real_sleep

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        print(f"\nFALLO: {len(failures)} comprobación(es) fallida(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: construcción de la petición, ejecución (con servicio falso) y reintentos se comportan como se espera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
