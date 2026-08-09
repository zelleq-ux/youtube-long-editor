"""
Test sintético (sin red, sin OAuth real, sin subir nada de verdad) de
src/publish/youtube.py.

Cubre:
1. _build_video_body: título/placeholder, descripción, privacidad --
   función pura.
2. _description_from_chapters / _thumbnail_path / _final_video_path:
   helpers de filesystem (chapters.txt opcional; final.mp4 y, desde
   2026-08-09, thumbnail.png también SIEMPRE obligatorios -- ver más
   abajo el porqué del cambio).
3. run(execute=False) con un servicio de YouTube FALSO inyectado (mismo
   patrón `client` que thumbnail/detect_chapters): confirma que la
   petición (videos().insert) se construye con el body correcto y que
   NUNCA se llama a next_chunk()/execute() -- no se sube nada.
4. run(execute=True) con el mismo servicio falso: confirma que sí se
   agota next_chunk() hasta la respuesta final, que se propaga el
   youtube_video_id devuelto, y que thumbnails().set() se llama con ese
   mismo id.
5. _execute_resumable_upload: reintenta ante HttpError 503 (transitorio)
   y no ante un HttpError 404 (no debe reintentar, debe propagar).
6. captions().insert(): con execute=False NUNCA se llama (ni se construye
   la petición) aunque exista subtitles.srt; con execute=True se llama una
   vez con el videoId/idioma correctos y el contenido del .srt solo si
   subtitles.srt existe -- y no se llama en absoluto si no existe (esto
   SÍ sigue siendo opcional, a diferencia de thumbnail.png).
7. thumbnail.png ya NO es opcional (2026-08-09: src/thumbnail/run.py dejó
   de generarlo automáticamente, solo extrae frames candidatos; el
   usuario lo crea a mano) -- run() debe lanzar FileNotFoundError con un
   mensaje claro si no existe, ANTES de construir ninguna petición,
   tanto con execute=False como con execute=True.
8. _get_channel_name / _verify_channel (2026-08-10, bug real: una subida
   acabó en un canal equivocado porque el token OAuth se vinculó al canal
   activo en el navegador, no al que se quería): run() SIEMPRE consulta
   channels().list(mine=True) antes de construir la petición de subida
   (tanto execute=False como execute=True) y expone el nombre del canal
   en el resultado; si config['youtube']['expected_channel_name'] no
   coincide, lanza RuntimeError ANTES de llamar a videos().insert().
9. _prepare_thumbnail_upload (2026-08-10, bug real: MediaUploadSizeError
   a mitad de una subida real por una miniatura de más de 2MB, que además
   abortó la subida de subtítulos al ser una excepción sin capturar):
   una imagen ya por debajo del límite se sube tal cual (PNG); una por
   encima se recomprime a JPEG en memoria (probado con una imagen de
   ruido aleatoria, deliberadamente incompresible en PNG, para forzar el
   camino de recompresión de verdad) sin tocar el archivo original en
   disco; si ni la calidad JPEG mínima basta, lanza RuntimeError con un
   mensaje claro en vez de dejar que reviente a mitad de la subida.

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

import cv2
import httplib2
import numpy as np
from googleapiclient.errors import HttpError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.publish.youtube import (  # noqa: E402
    _build_video_body,
    _description_from_chapters,
    _execute_resumable_upload,
    _final_video_path,
    _get_channel_name,
    _prepare_thumbnail_upload,
    _subtitles_path,
    _thumbnail_path,
    _verify_channel,
    _YOUTUBE_THUMBNAIL_MAX_BYTES,
    run,
)
import src.publish.youtube as publish_youtube  # noqa: E402

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


class _FakeCaptionsInsertRequest:
    def __init__(self, kwargs: dict):
        self.kwargs = kwargs
        self.executed = False

    def execute(self):
        self.executed = True
        return {"id": "caption_abc"}


class _FakeCaptionsResource:
    def __init__(self):
        self.insert_calls: list[dict] = []

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        return _FakeCaptionsInsertRequest(kwargs)


class _FakeChannelsListRequest:
    def __init__(self, channel_name: "str | None"):
        self._channel_name = channel_name

    def execute(self):
        if self._channel_name is None:
            return {"items": []}
        return {"items": [{"snippet": {"title": self._channel_name}}]}


class _FakeChannelsResource:
    def __init__(self, channel_name: "str | None" = "Fake Channel"):
        self.channel_name = channel_name
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _FakeChannelsListRequest(self.channel_name)


class _FakeYoutubeService:
    def __init__(self, final_response: dict, chunks_before_done: int = 1, channel_name: "str | None" = "Fake Channel"):
        self.videos_resource = _FakeVideosResource(final_response, chunks_before_done)
        self.thumbnails_resource = _FakeThumbnailsResource()
        self.captions_resource = _FakeCaptionsResource()
        self.channels_resource = _FakeChannelsResource(channel_name)

    def videos(self):
        return self.videos_resource

    def thumbnails(self):
        return self.thumbnails_resource

    def captions(self):
        return self.captions_resource

    def channels(self):
        return self.channels_resource


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

        try:
            _thumbnail_path(video_id, config)
            check("_thumbnail_path sin thumbnail.png lanza FileNotFoundError", False, "no lanzó excepción")
        except FileNotFoundError as exc:
            check(
                "_thumbnail_path sin thumbnail.png lanza FileNotFoundError con el mensaje esperado",
                "thumbnail.png no encontrado" in str(exc),
                f"mensaje={exc}",
            )
        _write_dummy_file(output_dir / "thumbnail.png")
        check("_thumbnail_path con thumbnail.png devuelve su ruta", _thumbnail_path(video_id, config).exists())

        check("_subtitles_path sin subtitles.srt devuelve None", _subtitles_path(video_id, config) is None)
        _write_dummy_file(output_dir / "subtitles.srt", content=b"1\n00:00:00,000 --> 00:00:01,000\nHola\n")
        check("_subtitles_path con subtitles.srt devuelve su ruta", _subtitles_path(video_id, config) is not None)

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
            "execute=False: captions().insert() nunca se llama, aunque exista subtitles.srt",
            len(fake_service_dry.captions_resource.insert_calls) == 0,
        )
        check(
            "execute=False: el resultado refleja executed=False, youtube_video_id=None y subtitles_attached=False",
            result_dry["executed"] is False and result_dry["youtube_video_id"] is None
            and result_dry["thumbnail_attached"] is True and result_dry["subtitles_attached"] is False,
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
        check(
            "execute=True: captions().insert() se llamó una vez con el videoId/idioma correctos",
            len(fake_service_real.captions_resource.insert_calls) == 1
            and fake_service_real.captions_resource.insert_calls[0]["body"]["snippet"]["videoId"] == "yt_abc123"
            and fake_service_real.captions_resource.insert_calls[0]["body"]["snippet"]["language"] == "es"
            and fake_service_real.captions_resource.insert_calls[0]["body"]["snippet"]["isDraft"] is False,
            f"insert_calls={fake_service_real.captions_resource.insert_calls}",
        )
        check(
            "execute=True: el resultado refleja subtitles_attached=True",
            result_real["subtitles_attached"] is True,
            f"result={result_real}",
        )

        print("=== run(execute=True) con --caption-language distinto: se propaga al body ===")
        fake_service_lang = _FakeYoutubeService(final_response={"id": "yt_lang"})
        run(video_id, config, youtube_service=fake_service_lang, execute=True, caption_language="en")
        check(
            "el idioma pasado se refleja en el body de captions().insert()",
            fake_service_lang.captions_resource.insert_calls[0]["body"]["snippet"]["language"] == "en",
            f"insert_calls={fake_service_lang.captions_resource.insert_calls}",
        )

        print("=== run(): sin thumbnail.png, falla con FileNotFoundError en vez de subir sin miniatura (execute=False y execute=True) ===")
        video_id_no_thumb = "no_thumb_video"
        output_dir_no_thumb = work_dir / video_id_no_thumb
        output_dir_no_thumb.mkdir(parents=True)
        _write_dummy_file(output_dir_no_thumb / "final.mp4")
        fake_service_no_thumb_dry = _FakeYoutubeService(final_response={"id": "should_not_be_used"})
        try:
            run(video_id_no_thumb, config, youtube_service=fake_service_no_thumb_dry, execute=False)
            check("sin thumbnail.png (execute=False): run() lanza FileNotFoundError", False, "no lanzó excepción")
        except FileNotFoundError as exc:
            check(
                "sin thumbnail.png (execute=False): run() lanza FileNotFoundError con el mensaje esperado",
                "thumbnail.png no encontrado" in str(exc),
                f"mensaje={exc}",
            )
        check(
            "sin thumbnail.png: nunca se llega a construir ninguna petición videos().insert()",
            len(fake_service_no_thumb_dry.videos_resource.insert_calls) == 0,
        )
        fake_service_no_thumb_real = _FakeYoutubeService(final_response={"id": "should_not_be_used"})
        try:
            run(video_id_no_thumb, config, youtube_service=fake_service_no_thumb_real, execute=True)
            check("sin thumbnail.png (execute=True): run() lanza FileNotFoundError", False, "no lanzó excepción")
        except FileNotFoundError:
            check("sin thumbnail.png (execute=True): run() lanza FileNotFoundError", True, "")

        print("=== run(execute=True) con thumbnail.png pero SIN subtitles.srt: sigue subiendo, solo omite captions().insert() ===")
        video_id_no_subs = "no_subs_video"
        output_dir_no_subs = work_dir / video_id_no_subs
        output_dir_no_subs.mkdir(parents=True)
        _write_dummy_file(output_dir_no_subs / "final.mp4")
        _write_dummy_file(output_dir_no_subs / "thumbnail.png")
        fake_service_no_subs = _FakeYoutubeService(final_response={"id": "yt_no_subs"})
        result_no_subs = run(video_id_no_subs, config, youtube_service=fake_service_no_subs, execute=True)
        check(
            "con thumbnail.png pero sin subtitles.srt: la subida sigue funcionando y adjunta la miniatura",
            len(fake_service_no_subs.thumbnails_resource.set_calls) == 1,
        )
        check(
            "sin subtitles.srt: captions().insert() no se llama y subtitles_attached=False",
            len(fake_service_no_subs.captions_resource.insert_calls) == 0
            and result_no_subs["subtitles_attached"] is False,
            f"result={result_no_subs}",
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

        print("=== _get_channel_name / _verify_channel ===")
        fake_service_channel = _FakeYoutubeService(final_response={"id": "unused"}, channel_name="Zelleq")
        check(
            "_get_channel_name devuelve el título del canal del token actual",
            _get_channel_name(fake_service_channel) == "Zelleq",
        )

        fake_service_no_channel = _FakeYoutubeService(final_response={"id": "unused"}, channel_name=None)
        try:
            _get_channel_name(fake_service_no_channel)
            check("_get_channel_name sin ningún canal devuelto lanza RuntimeError", False, "no lanzó excepción")
        except RuntimeError:
            check("_get_channel_name sin ningún canal devuelto lanza RuntimeError", True, "")

        check(
            "_verify_channel sin expected_channel_name configurado: devuelve el canal sin comprobar nada",
            _verify_channel(fake_service_channel, {}) == "Zelleq",
        )
        check(
            "_verify_channel con expected_channel_name que SÍ coincide: devuelve el canal sin lanzar",
            _verify_channel(fake_service_channel, {"youtube": {"expected_channel_name": "Zelleq"}}) == "Zelleq",
        )
        try:
            _verify_channel(fake_service_channel, {"youtube": {"expected_channel_name": "Canal de VODs"}})
            check("_verify_channel con expected_channel_name que NO coincide lanza RuntimeError", False, "no lanzó excepción")
        except RuntimeError as exc:
            check(
                "_verify_channel con expected_channel_name que NO coincide lanza RuntimeError con ambos nombres en el mensaje",
                "Zelleq" in str(exc) and "Canal de VODs" in str(exc),
                f"mensaje={exc}",
            )

        print("=== run(): con canal equivocado, falla ANTES de construir videos().insert() ===")
        video_id_wrong_channel = "wrong_channel_video"
        output_dir_wrong_channel = work_dir / video_id_wrong_channel
        output_dir_wrong_channel.mkdir(parents=True)
        _write_dummy_file(output_dir_wrong_channel / "final.mp4")
        _write_dummy_file(output_dir_wrong_channel / "thumbnail.png")
        config_wrong_channel = {**config, "youtube": {"expected_channel_name": "Canal Principal"}}
        fake_service_wrong_channel = _FakeYoutubeService(final_response={"id": "should_not_be_used"}, channel_name="Canal de VODs")
        try:
            run(video_id_wrong_channel, config_wrong_channel, youtube_service=fake_service_wrong_channel, execute=False)
            check("run() con canal equivocado lanza RuntimeError", False, "no lanzó excepción")
        except RuntimeError:
            check("run() con canal equivocado lanza RuntimeError", True, "")
        check(
            "con canal equivocado, NUNCA se construye ninguna petición videos().insert()",
            len(fake_service_wrong_channel.videos_resource.insert_calls) == 0,
        )

        print("=== run() con canal correcto: channel_name viaja en el resultado ===")
        config_right_channel = {**config, "youtube": {"expected_channel_name": "Zelleq"}}
        result_right_channel = run(video_id, config_right_channel, youtube_service=fake_service_channel, execute=False)
        check(
            "run() con canal correcto devuelve channel_name en el resultado y no lanza",
            result_right_channel.get("channel_name") == "Zelleq",
            f"result={result_right_channel}",
        )

        print("=== _prepare_thumbnail_upload: imagen por debajo del límite se sube tal cual (PNG) ===")
        small_thumb_path = work_dir / "small_thumb.png"
        small_image = np.zeros((100, 100, 3), dtype=np.uint8)
        small_image[:, :] = (30, 60, 90)
        cv2.imwrite(str(small_thumb_path), small_image)
        small_bytes_before = small_thumb_path.read_bytes()
        check(
            "la imagen pequeña de prueba está, en efecto, por debajo del límite",
            len(small_bytes_before) <= _YOUTUBE_THUMBNAIL_MAX_BYTES,
            f"size={len(small_bytes_before)}",
        )
        result_data, result_mimetype = _prepare_thumbnail_upload(small_thumb_path)
        check(
            "por debajo del límite: se devuelven los bytes ORIGINALES tal cual, mimetype image/png",
            result_data == small_bytes_before and result_mimetype == "image/png",
        )

        print("=== _prepare_thumbnail_upload: imagen por ENCIMA del límite se recomprime a JPEG y cabe ===")
        large_thumb_path = work_dir / "large_thumb.png"
        # ruido aleatorio: deliberadamente incompresible con PNG (sin patrones repetidos),
        # para garantizar de verdad que supera el límite y ejercitar el camino de recompresión real.
        rng = np.random.default_rng(20260810)
        noisy_image = rng.integers(0, 256, size=(1080, 1920, 3), dtype=np.uint8)
        cv2.imwrite(str(large_thumb_path), noisy_image, [cv2.IMWRITE_PNG_COMPRESSION, 0])
        large_size_before = large_thumb_path.stat().st_size
        check(
            "la imagen de ruido de prueba SÍ supera el límite de 2MB (para ejercitar la recompresión real)",
            large_size_before > _YOUTUBE_THUMBNAIL_MAX_BYTES,
            f"size={large_size_before}",
        )
        compressed_data, compressed_mimetype = _prepare_thumbnail_upload(large_thumb_path)
        check(
            "por encima del límite: el resultado recomprimido cabe bajo el límite y es JPEG",
            len(compressed_data) <= _YOUTUBE_THUMBNAIL_MAX_BYTES and compressed_mimetype == "image/jpeg",
            f"size={len(compressed_data)}",
        )
        decoded = cv2.imdecode(np.frombuffer(compressed_data, dtype=np.uint8), cv2.IMREAD_COLOR)
        check(
            "la recompresión NO cambia la resolución de la imagen",
            decoded is not None and decoded.shape[:2] == (1080, 1920),
            f"shape={None if decoded is None else decoded.shape}",
        )
        check(
            "el archivo original en disco NUNCA se toca (mismo tamaño después de comprimir para la subida)",
            large_thumb_path.stat().st_size == large_size_before,
        )

        print("=== _prepare_thumbnail_upload: si ni la calidad mínima cabe, lanza RuntimeError claro ===")
        original_max_bytes = publish_youtube._YOUTUBE_THUMBNAIL_MAX_BYTES
        publish_youtube._YOUTUBE_THUMBNAIL_MAX_BYTES = 100  # imposible de cumplir incluso a la mínima calidad JPEG
        try:
            try:
                _prepare_thumbnail_upload(large_thumb_path)
                check("límite imposible: _prepare_thumbnail_upload lanza RuntimeError", False, "no lanzó excepción")
            except RuntimeError as exc:
                check(
                    "límite imposible: _prepare_thumbnail_upload lanza RuntimeError con mensaje claro",
                    "MB" in str(exc) or "calidad" in str(exc),
                    f"mensaje={exc}",
                )
        finally:
            publish_youtube._YOUTUBE_THUMBNAIL_MAX_BYTES = original_max_bytes

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
