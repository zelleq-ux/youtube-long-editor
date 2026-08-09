"""
Publicación en YouTube (YouTube Data API v3).

Sube data/output/<video_id>/final.mp4 ya editado a un canal de YouTube vía
la YouTube Data API v3 (videos().insert, subida resumable), adjuntando:

- Título: el parámetro --title/title, o -- si no se da ninguno -- un
  placeholder CLARAMENTE marcado como pendiente (nunca se sube con un
  título vacío ni inventado por el módulo).
- Descripción: el contenido de data/output/<video_id>/chapters.txt si
  existe (ya en el formato de capítulos listo para YouTube, ver
  detect_chapters/run.py), o vacía si no existe todavía.
- Miniatura: data/output/<video_id>/thumbnail.png si existe, adjuntada
  con thumbnails().set() DESPUÉS de que el vídeo se haya subido (hace
  falta el video_id que devuelve YouTube, no el video_id interno del
  proyecto).
- privacy_status: SIEMPRE "private" por defecto -- nunca se sube como
  "public" ni "unlisted" salvo que se pida explícitamente por parámetro o
  con --privacy en el CLI.

Autenticación OAuth (flujo estándar de "instalar-app" de Google vía
google-auth-oauthlib): la primera vez abre el navegador para que el
usuario autorice el acceso (scope youtube.upload, que cubre tanto
videos().insert como thumbnails().set() -- no hace falta un scope más
amplio) usando YOUTUBE_CLIENT_SECRET_PATH del .env, y guarda el resultado
en token.json (raíz del repo, gitignored) para no repetir la autorización
en cada ejecución; lo reutiliza y refresca automáticamente mientras el
refresh token siga siendo válido.

IMPORTANTE -- no se sube nada de verdad salvo que se pida explícitamente:
run() acepta `execute` (por defecto False). Con execute=False construye
la petición completa (credenciales OAuth, servicio de la API, body de
snippet/status, MediaFileUpload resumable) y se detiene ahí SIN llamar a
.execute() -- así se puede verificar que todo está bien construido sin
gastar cuota de subida real ni crear un vídeo de verdad en el canal. Solo
con execute=True (o --execute en el CLI) se ejecuta la subida real
(vídeo + miniatura si existe).
"""
from __future__ import annotations

import argparse
import logging
import random
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaFileUpload

from src.common.config import REPO_ROOT, load_config

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_TOKEN_PATH = REPO_ROOT / "token.json"

_DEFAULT_PRIVACY_STATUS = "private"
_TITLE_PLACEHOLDER_PREFIX = "[TÍTULO PENDIENTE]"
_DEFAULT_CATEGORY_ID = "20"  # "Gaming" en la taxonomía de categorías de vídeo de YouTube

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


def _thumbnail_path(video_id: str, config: dict) -> Path | None:
    """data/output/<video_id>/thumbnail.png si existe, o None (no bloquea la subida)."""
    path = _output_dir(video_id, config) / "thumbnail.png"
    return path if path.exists() else None


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
    execute: bool = False,
    youtube_service: Resource | None = None,
) -> dict:
    """
    Sube data/output/<video_id>/final.mp4 a YouTube.

    IMPORTANTE: con execute=False (por defecto) construye la petición
    completa -- credenciales OAuth, servicio de la API, body de
    snippet/status, MediaFileUpload resumable -- pero NO llama a
    .execute() en ninguna parte: no se sube nada de verdad, no se gasta
    cuota, no se crea ningún vídeo en el canal. Solo con execute=True se
    ejecuta la subida real (y, si hay miniatura, thumbnails().set()
    justo después).

    `youtube_service` es inyectable (por defecto None, construye el
    servicio real vía OAuth con _build_youtube_service) -- mismo patrón
    `client` de detect_chapters/thumbnail, para poder testear la
    construcción de la petición (título/descripción/privacidad/orden de
    llamadas) con un servicio simulado, sin credenciales ni red reales.

    Returns:
        dict con {"video_id", "youtube_video_id" (None si execute=False
        o si no se llegó a completar la subida), "title", "description",
        "privacy_status", "thumbnail_attached": bool, "executed": bool}
    """
    video_path = _final_video_path(video_id, config)
    description = _description_from_chapters(video_id, config)
    thumbnail_path = _thumbnail_path(video_id, config)
    body = _build_video_body(video_id, title, description, privacy_status)

    logger.info(
        "Preparando subida de '%s' a YouTube: título=%r, privacidad=%s, miniatura=%s, ejecutar_subida_real=%s",
        video_id, body["snippet"]["title"], privacy_status, thumbnail_path is not None, execute,
    )

    youtube = youtube_service if youtube_service is not None else _build_youtube_service(config)
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=-1, resumable=True)
    insert_request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    result = {
        "video_id": video_id,
        "youtube_video_id": None,
        "title": body["snippet"]["title"],
        "description": description,
        "privacy_status": privacy_status,
        "thumbnail_attached": thumbnail_path is not None,
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

    if thumbnail_path is not None:
        logger.info("Subiendo miniatura para %s...", youtube_video_id)
        youtube.thumbnails().set(
            videoId=youtube_video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png"),
        ).execute()

    return result


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Publicar un vídeo editado en YouTube")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--title", default=None, help="Título del vídeo (si se omite, se usa un placeholder marcado como pendiente)")
    parser.add_argument("--privacy", default=_DEFAULT_PRIVACY_STATUS, choices=["private", "unlisted", "public"])
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Ejecuta la subida real. Sin esta flag solo se construye y verifica la petición, sin subir nada.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config()
    run(args.video_id, config, privacy_status=args.privacy, title=args.title, execute=args.execute)


if __name__ == "__main__":
    _cli()
