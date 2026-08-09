"""
Publicación en YouTube (YouTube Data API v3).

Sube data/output/<video_id>/final.mp4 ya editado a un canal de YouTube vía
la YouTube Data API v3 (videos().insert, subida resumable), adjuntando:

- Título: el parámetro --title/title, o -- si no se da ninguno -- un
  placeholder CLARAMENTE marcado como pendiente (nunca se sube con un
  título vacío ni inventado por el módulo).
- Descripción: el parámetro --description/description si se da (con los
  capítulos de chapters.txt pegados DEBAJO si existen -- ver
  _build_description), o si no se da ninguna, solo el contenido de
  data/output/<video_id>/chapters.txt (ya en el formato de capítulos
  listo para YouTube, ver detect_chapters/run.py), o vacía si tampoco
  existe.
- Miniatura: data/output/<video_id>/thumbnail.png -- SIEMPRE obligatoria
  (2026-08-09: ya no se genera automáticamente, el usuario la crea a mano
  a partir de un frame candidato de src/thumbnail/run.py; ver
  _thumbnail_path), adjuntada con thumbnails().set() DESPUÉS de que el
  vídeo se haya subido (hace falta el video_id que devuelve YouTube, no
  el video_id interno del proyecto). run() lanza FileNotFoundError con un
  mensaje claro si no existe todavía, en vez de subir sin miniatura en
  silencio. Si pesa más de 2MB (límite real de thumbnails().set(), bug
  encontrado el 2026-08-10 en una subida real), se recomprime a JPEG en
  memoria antes de subirla -- ver _prepare_thumbnail_upload -- sin tocar
  nunca el archivo original en disco.
- Subtítulos: data/output/<video_id>/subtitles.srt si existe (ver
  src/subtitles/run.py), adjuntada con captions().insert() DESPUÉS de que
  el vídeo se haya subido -- mismo orden y misma razón que la miniatura
  (hace falta el video_id de YouTube). Se sube como pista NO borrador
  (isDraft=False) en el idioma de config/--caption-language (por defecto
  "es", el proyecto es para directos en español).
- privacy_status: SIEMPRE "private" por defecto -- nunca se sube como
  "public" ni "unlisted" salvo que se pida explícitamente por parámetro o
  con --privacy en el CLI.

Autenticación OAuth (flujo estándar de "instalar-app" de Google vía
google-auth-oauthlib): la primera vez abre el navegador para que el
usuario autorice el acceso (scopes youtube.upload -- videos().insert,
thumbnails().set() -- y youtube.readonly -- channels().list(), ver
verificación de canal más abajo) usando YOUTUBE_CLIENT_SECRET_PATH del
.env, y guarda el resultado en token.json (raíz del repo, gitignored)
para no repetir la autorización en cada ejecución; lo reutiliza y
refresca automáticamente mientras el refresh token siga siendo válido.

IMPORTANTE -- no se sube nada de verdad salvo que se pida explícitamente:
run() acepta `execute` (por defecto False). Con execute=False construye
la petición completa (credenciales OAuth, servicio de la API, body de
snippet/status, MediaFileUpload resumable) y se detiene ahí SIN llamar a
.execute() -- así se puede verificar que todo está bien construido sin
gastar cuota de subida real ni crear un vídeo de verdad en el canal. Solo
con execute=True (o --execute en el CLI) se ejecuta la subida real
(vídeo + miniatura + subtítulos, cada uno si existe). Con execute=False
NUNCA se llama a captions().insert() -- ni siquiera para construir la
petición sin ejecutarla, a diferencia del vídeo -- porque hace falta el
video_id que devuelve YouTube al insertar el vídeo, que solo existe tras
una subida real.

IMPORTANTE -- verificación de canal (2026-08-10, bug real: una subida de
dinoblade_1 acabó en un canal equivocado -- el token OAuth se vincula al
canal que esté activo en el NAVEGADOR en el momento de autorizar, no
necesariamente al que el usuario "quiere", y la API de YouTube no ofrece
ningún parámetro para elegir canal de destino en la subida; la única
defensa práctica es verificar ANTES de subir, no después de que el vídeo
ya esté en el canal equivocado). Por eso run() SIEMPRE consulta
channels().list(mine=True) nada más construir el servicio -- tanto con
execute=False como con execute=True, antes de construir ninguna petición
de subida -- y loguea claramente "Subiendo al canal: <nombre>". Si
config['youtube']['expected_channel_name'] está configurado (opcional),
_verify_channel lanza RuntimeError de inmediato si el canal del token
actual no coincide EXACTAMENTE, en vez de seguir adelante.
"""
from __future__ import annotations

import argparse
import logging
import random
import time
from pathlib import Path

import cv2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaFileUpload, MediaInMemoryUpload

from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # necesario para channels().list(mine=True), ver _verify_channel
]
_TOKEN_PATH = REPO_ROOT / "token.json"

_DEFAULT_PRIVACY_STATUS = "private"
_TITLE_PLACEHOLDER_PREFIX = "[TÍTULO PENDIENTE]"
_DEFAULT_CATEGORY_ID = "20"  # "Gaming" en la taxonomía de categorías de vídeo de YouTube

_DEFAULT_CAPTION_LANGUAGE = "es"
_CAPTION_TRACK_NAME = "Español"
_CAPTION_MIMETYPE = "application/octet-stream"  # tipo genérico aceptado por captions().insert() para .srt

# thumbnails().set() de la API de YouTube rechaza imágenes de más de 2MB
# (bug real, 2026-08-10: MediaUploadSizeError a mitad de una subida real,
# que además abortó captions().insert() al ser una excepción sin capturar
# -- ver _prepare_thumbnail_upload). Calidades JPEG a probar en orden,
# de mayor a menor -- la primera que quepa gana; JPEG comprime mucho mejor
# que PNG para una imagen fotográfica como una miniatura, así que es la
# vía natural antes de plantearse tocar la resolución.
_YOUTUBE_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
_THUMBNAIL_JPEG_QUALITY_STEPS = (95, 90, 85, 80, 70, 60, 50, 40, 30)

_UPLOAD_RETRIABLE_STATUS_CODES = (500, 502, 503, 504)
_UPLOAD_MAX_RETRIES = 10


def _output_dir(video_id: str, config: dict) -> Path:
    return (REPO_ROOT / config["paths"]["output"]).resolve() / video_id


def _final_video_path(video_id: str, config: dict) -> Path:
    path = _output_dir(video_id, config) / "final.mp4"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe el vídeo editado para '{video_id}': {path}. "
            f"Ejecuta primero la etapa de edición (python -m src.edit.run --video-id {video_id})."
        )
    return path


def _description_from_chapters(video_id: str, config: dict) -> str:
    """Pega data/output/<video_id>/chapters.txt si existe; descripción vacía si no (no bloquea la subida)."""
    chapters_path = _output_dir(video_id, config) / "chapters.txt"
    if not chapters_path.exists():
        logger.info(
            "No existe chapters.txt para '%s'; se sube con la descripción vacía.", video_id
        )
        return ""
    return chapters_path.read_text(encoding="utf-8").strip()


def _build_description(video_id: str, config: dict, custom_description: str | None) -> str:
    """
    Descripción final a subir. Sin `custom_description` (--description),
    comportamiento de siempre: solo el contenido de chapters.txt (o vacía
    si no existe). Con `custom_description`, se usa ese texto tal cual
    (recortado de espacios) y se le pegan los capítulos DEBAJO, separados
    por una línea en blanco, si chapters.txt existe -- chapters.txt se
    genera precisamente para pegarse en la descripción (ver CLAUDE.md):
    los timestamps ahí son lo que permite a YouTube detectar los
    capítulos automáticamente, así que un --description personalizado no
    debe hacer que se pierdan en silencio.
    """
    chapters_content = _description_from_chapters(video_id, config)
    if custom_description is None:
        return chapters_content
    custom = custom_description.strip()
    if chapters_content:
        return f"{custom}\n\n{chapters_content}"
    return custom


def _thumbnail_path(video_id: str, config: dict) -> Path:
    """
    data/output/<video_id>/thumbnail.png -- SIEMPRE obligatoria para
    publicar (2026-08-09: src/thumbnail/run.py ya no genera esta imagen
    automáticamente, solo extrae frames candidatos; thumbnail.png lo crea
    el usuario a mano a partir de uno de ellos, con el título/composición
    que quiera, y lo guarda en esta misma ruta). Lanza FileNotFoundError
    con un mensaje claro si no existe todavía -- mismo patrón que
    _final_video_path -- en vez de subir sin miniatura en silencio o
    fallar de forma confusa más adelante.
    """
    path = _output_dir(video_id, config) / "thumbnail.png"
    if not path.exists():
        raise FileNotFoundError(
            f"thumbnail.png no encontrado, generar/elegir uno primero: no existe {path}. "
            f"Ejecuta python -m src.thumbnail.run --video-id {video_id} para extraer frames candidatos "
            f"(data/output/{video_id}/thumbnail_candidate_*.png), elige uno, edítalo si quieres, y "
            f"guárdalo como {path} antes de publicar."
        )
    return path


def _subtitles_path(video_id: str, config: dict) -> Path | None:
    """data/output/<video_id>/subtitles.srt si existe (ver src/subtitles/run.py), o None (no bloquea la subida)."""
    path = _output_dir(video_id, config) / "subtitles.srt"
    return path if path.exists() else None


def _build_caption_body(youtube_video_id: str, language: str) -> dict:
    """
    Construye el `body` de captions().insert() -- función pura, sin red ni
    credenciales, mismo patrón que _build_video_body. isDraft=False: la
    pista queda publicada de inmediato, no como borrador pendiente de
    revisión manual (coherente con "corte automático sin revisión manual"
    de CLAUDE.md).
    """
    return {
        "snippet": {
            "videoId": youtube_video_id,
            "language": language,
            "name": _CAPTION_TRACK_NAME,
            "isDraft": False,
        }
    }


def _build_video_body(video_id: str, title: str | None, description: str, privacy_status: str) -> dict:
    """
    Construye el `body` de videos().insert -- función pura, sin red ni
    credenciales, para poder testear la forma exacta de la petición
    (título/placeholder, descripción, privacidad) de forma aislada.
    """
    resolved_title = title.strip() if title and title.strip() else f"{_TITLE_PLACEHOLDER_PREFIX} {video_id}"
    return {
        "snippet": {
            "title": resolved_title,
            "description": description,
            "categoryId": _DEFAULT_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }


def _get_credentials(config: dict) -> Credentials:
    """
    Credenciales OAuth válidas: reutiliza y refresca token.json si existe
    y sigue siendo (o puede volver a ser) válido; si no, lanza el flujo de
    autorización en el navegador (InstalledAppFlow.run_local_server) y
    guarda el resultado en token.json para no repetirlo en la siguiente
    ejecución.
    """
    client_secret_path = config.get("_env", {}).get("youtube_client_secret_path")
    if not client_secret_path:
        raise RuntimeError(
            "YOUTUBE_CLIENT_SECRET_PATH no está configurado en .env; hace falta para autenticar con la "
            "YouTube Data API v3 (ver CLAUDE.md / status.md)."
        )
    if not Path(client_secret_path).exists():
        raise FileNotFoundError(f"No existe el archivo de credenciales de cliente OAuth: {client_secret_path}")

    creds: Credentials | None = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("El token de YouTube guardado ha caducado; refrescando...")
            creds.refresh(Request())
        else:
            logger.info(
                "No hay ningún token de YouTube válido guardado; abriendo el navegador para autorizar "
                "el acceso (una sola vez -- se guardará en %s para las próximas ejecuciones).",
                _TOKEN_PATH,
            )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, _SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _build_youtube_service(config: dict) -> Resource:
    creds = _get_credentials(config)
    return build("youtube", "v3", credentials=creds)


def _get_channel_name(youtube: Resource) -> str:
    """
    Nombre del canal de YouTube al que está vinculado el token actual,
    vía channels().list(mine=True) -- ver "verificación de canal" en el
    docstring del módulo para el porqué (bug real: el flujo OAuth se
    vincula al canal que esté activo en el navegador al autorizar, no
    necesariamente al que se quiere, y la API no deja elegir canal de
    destino por parámetro en la subida).
    """
    response = youtube.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(
            "channels().list(mine=True) no devolvió ningún canal para el token actual; no se puede "
            "verificar a qué canal se subiría. Revisa la autorización OAuth (borra token.json y vuelve "
            "a autorizar si hace falta) antes de subir nada."
        )
    return items[0]["snippet"]["title"]


def _verify_channel(youtube: Resource, config: dict) -> str:
    """
    Consulta el canal vinculado al token actual (_get_channel_name) y lo
    loguea SIEMPRE, tanto en dry-run como en subida real -- para que se
    note ANTES de subir, no después de que el vídeo ya esté en el canal
    equivocado (ver docstring del módulo). Si
    config['youtube']['expected_channel_name'] está configurado (opcional,
    ver settings.yaml), compara EXACTAMENTE (sensible a mayúsculas y
    espacios -- mejor un falso positivo molesto que dejar pasar un canal
    distinto por una comparación demasiado laxa) y lanza RuntimeError de
    inmediato si no coincide, ANTES de construir ninguna petición de
    subida.

    Returns:
        El nombre del canal verificado.
    """
    channel_name = _get_channel_name(youtube)
    logger.info("Subiendo al canal: %s", channel_name)

    expected = config.get("youtube", {}).get("expected_channel_name")
    if expected and channel_name != expected:
        raise RuntimeError(
            f"El token de YouTube actual está vinculado al canal '{channel_name}', pero "
            f"config['youtube']['expected_channel_name'] espera '{expected}'. Verifica que el "
            "navegador tenía la cuenta/canal correcto activo al autorizar -- borra token.json y "
            "vuelve a autorizar con el canal correcto antes de subir nada."
        )
    return channel_name


def _prepare_thumbnail_upload(thumbnail_path: Path) -> tuple[bytes, str]:
    """
    Devuelve (bytes, mimetype) listos para subir con thumbnails().set().

    Si `thumbnail_path` ya pesa <= _YOUTUBE_THUMBNAIL_MAX_BYTES, se sube
    tal cual (los bytes originales del PNG, sin tocar). Si pesa más, se
    recodifica a JPEG bajando la calidad progresivamente
    (_THUMBNAIL_JPEG_QUALITY_STEPS) -- SIN cambiar la resolución -- hasta
    encontrar la primera que quepa bajo el límite. NUNCA se sobrescribe
    `thumbnail_path` en disco -- la recompresión es solo para la subida,
    el archivo del usuario se queda exactamente como lo dejó.

    Si ni con la calidad JPEG mínima cabe (imagen extremadamente densa,
    caso límite no visto en la práctica), lanza RuntimeError ANTES de
    intentar la subida -- en vez de dejar que thumbnails().set() reviente
    con MediaUploadSizeError a mitad de camino, que además abortaría
    captions().insert() al ser una excepción sin capturar (ocurre
    después, en el mismo run()).
    """
    raw_bytes = thumbnail_path.read_bytes()
    if len(raw_bytes) <= _YOUTUBE_THUMBNAIL_MAX_BYTES:
        return raw_bytes, "image/png"

    logger.info(
        "%s pesa %.2fMB, por encima del límite de la API de YouTube para miniaturas (%.0fMB); "
        "recomprimiendo a JPEG...",
        thumbnail_path.name, len(raw_bytes) / 1024 / 1024, _YOUTUBE_THUMBNAIL_MAX_BYTES / 1024 / 1024,
    )
    image = cv2.imread(str(thumbnail_path))
    if image is None:
        raise RuntimeError(f"No se pudo leer {thumbnail_path} para comprimirla (¿es una imagen válida?).")

    for quality in _THUMBNAIL_JPEG_QUALITY_STEPS:
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            continue
        size = len(encoded)
        if size <= _YOUTUBE_THUMBNAIL_MAX_BYTES:
            logger.info(
                "Miniatura recomprimida a JPEG calidad=%d: %.2fMB (por debajo del límite, misma resolución).",
                quality, size / 1024 / 1024,
            )
            return encoded.tobytes(), "image/jpeg"

    raise RuntimeError(
        f"No se pudo comprimir {thumbnail_path} por debajo de {_YOUTUBE_THUMBNAIL_MAX_BYTES / 1024 / 1024:.0f}MB "
        f"(límite de la API de YouTube para miniaturas) ni con la calidad JPEG mínima probada "
        f"({_THUMBNAIL_JPEG_QUALITY_STEPS[-1]}). Reduce manualmente la resolución de la imagen antes de publicar."
    )


def _execute_resumable_upload(insert_request: HttpRequest) -> dict:
    """
    Bucle de subida resumable con reintentos ante errores transitorios del
    servidor (5xx) -- patrón estándar documentado por Google para
    videos().insert con MediaFileUpload(resumable=True): next_chunk() hay
    que llamarlo repetidamente hasta que devuelva la respuesta final.
    """
    response = None
    retry = 0
    while response is None:
        try:
            _status, response = insert_request.next_chunk()
        except HttpError as exc:
            if exc.resp.status not in _UPLOAD_RETRIABLE_STATUS_CODES:
                raise
            retry += 1
            if retry > _UPLOAD_MAX_RETRIES:
                raise
            sleep_seconds = min(2**retry, 60) + random.random()
            logger.warning(
                "Error transitorio subiendo a YouTube (%s); reintentando en %.1fs (intento %d/%d)...",
                exc, sleep_seconds, retry, _UPLOAD_MAX_RETRIES,
            )
            time.sleep(sleep_seconds)
    return response


def run(
    video_id: str,
    config: dict,
    privacy_status: str = _DEFAULT_PRIVACY_STATUS,
    title: str | None = None,
    description: str | None = None,
    execute: bool = False,
    youtube_service: Resource | None = None,
    caption_language: str = _DEFAULT_CAPTION_LANGUAGE,
) -> dict:
    """
    Sube data/output/<video_id>/final.mp4 a YouTube.

    `description` (opcional, ver --description en el CLI): si se da, se
    usa como descripción en vez del contenido plano de chapters.txt --
    pero los capítulos se le siguen pegando DEBAJO si chapters.txt existe
    (ver _build_description), para no perder la detección automática de
    capítulos de YouTube por descuido.

    IMPORTANTE: con execute=False (por defecto) construye la petición
    completa -- credenciales OAuth, servicio de la API, body de
    snippet/status, MediaFileUpload resumable -- pero NO llama a
    .execute() en ninguna parte: no se sube nada de verdad, no se gasta
    cuota, no se crea ningún vídeo en el canal. Solo con execute=True se
    ejecuta la subida real (y, si hay miniatura/subtítulos,
    thumbnails().set()/captions().insert() justo después -- en ese orden,
    ambos necesitan el video_id que devuelve YouTube al subir el vídeo).
    Con execute=False, captions().insert() no se llama ni se construye en
    absoluto (a diferencia de videos().insert(), que sí se construye
    siempre) -- no hay ningún video_id de YouTube todavía sobre el que
    construir esa petición.

    `youtube_service` es inyectable (por defecto None, construye el
    servicio real vía OAuth con _build_youtube_service) -- mismo patrón
    `client` de detect_chapters/thumbnail, para poder testear la
    construcción de la petición (título/descripción/privacidad/orden de
    llamadas) con un servicio simulado, sin credenciales ni red reales.

    Returns:
        dict con {"video_id", "youtube_video_id" (None si execute=False
        o si no se llegó a completar la subida), "channel_name", "title",
        "description", "privacy_status", "thumbnail_attached": bool,
        "subtitles_attached": bool, "executed": bool}
    """
    video_path = _final_video_path(video_id, config)
    final_description = _build_description(video_id, config, description)
    thumbnail_path = _thumbnail_path(video_id, config)
    subtitles_path = _subtitles_path(video_id, config)
    body = _build_video_body(video_id, title, final_description, privacy_status)

    logger.info(
        "Preparando subida de '%s' a YouTube: título=%r, privacidad=%s, miniatura=%s, subtítulos=%s, "
        "ejecutar_subida_real=%s",
        video_id, body["snippet"]["title"], privacy_status,
        thumbnail_path, subtitles_path is not None, execute,
    )

    youtube = youtube_service if youtube_service is not None else _build_youtube_service(config)
    channel_name = _verify_channel(youtube, config)

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=-1, resumable=True)
    insert_request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    result = {
        "video_id": video_id,
        "youtube_video_id": None,
        "channel_name": channel_name,
        "title": body["snippet"]["title"],
        "description": final_description,
        "privacy_status": privacy_status,
        "thumbnail_attached": True,
        "subtitles_attached": False,
        "executed": False,
    }

    if not execute:
        logger.info(
            "execute=False: la petición de subida se ha construido correctamente pero NO se ha subido "
            "nada de verdad. Llama a run(..., execute=True) (o --execute en el CLI) para confirmar la "
            "subida real."
        )
        return result

    logger.warning("Subiendo '%s' a YouTube de verdad (privacidad=%s)...", video_id, privacy_status)
    response = _execute_resumable_upload(insert_request)
    youtube_video_id = response["id"]
    result["youtube_video_id"] = youtube_video_id
    result["executed"] = True
    logger.info("Vídeo subido: https://youtu.be/%s", youtube_video_id)

    logger.info("Subiendo miniatura para %s...", youtube_video_id)
    thumbnail_bytes, thumbnail_mimetype = _prepare_thumbnail_upload(thumbnail_path)
    youtube.thumbnails().set(
        videoId=youtube_video_id,
        media_body=MediaInMemoryUpload(thumbnail_bytes, mimetype=thumbnail_mimetype),
    ).execute()

    if subtitles_path is not None:
        logger.info("Subiendo subtítulos (%s) para %s...", caption_language, youtube_video_id)
        youtube.captions().insert(
            part="snippet",
            body=_build_caption_body(youtube_video_id, caption_language),
            media_body=MediaFileUpload(str(subtitles_path), mimetype=_CAPTION_MIMETYPE),
        ).execute()
        result["subtitles_attached"] = True

    return result


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Publicar un vídeo editado en YouTube")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--title", default=None, help="Título del vídeo (si se omite, se usa un placeholder marcado como pendiente)")
    parser.add_argument(
        "--description", default=None,
        help=(
            "Descripción del vídeo (si se omite, se usa solo el contenido de chapters.txt). Si se da, "
            "se le pegan los capítulos de chapters.txt DEBAJO, si existen -- para no perder la "
            "detección automática de capítulos de YouTube por descuido."
        ),
    )
    parser.add_argument("--privacy", default=_DEFAULT_PRIVACY_STATUS, choices=["private", "unlisted", "public"])
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Ejecuta la subida real. Sin esta flag solo se construye y verifica la petición, sin subir nada.",
    )
    parser.add_argument(
        "--caption-language",
        default=_DEFAULT_CAPTION_LANGUAGE,
        help="Idioma de la pista de subtítulos, si data/output/<video_id>/subtitles.srt existe (por defecto 'es').",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config()
    run(
        args.video_id, config, privacy_status=args.privacy, title=args.title, description=args.description,
        execute=args.execute, caption_language=args.caption_language,
    )


if __name__ == "__main__":
    _cli()
