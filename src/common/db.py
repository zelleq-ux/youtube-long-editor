"""
Acceso mínimo al estado del pipeline (SQLite, stdlib).

No hay ningún esquema previo definido para esto en el proyecto hermano, así
que aquí se define uno deliberadamente pequeño: una única tabla `videos` que
guarda el último estado conocido de cada video_id (p.ej. "ingested",
"transcribed"...). Cada etapa puede llamar a `set_status` al terminar con
éxito; no se pretende sustituir a los archivos JSON intermedios, que siguen
siendo el contrato de datos real entre etapas.

La base de datos vive en data/pipeline.db (ver DB_PATH), fuera del control de
versiones.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from src.common.config import REPO_ROOT

DB_PATH = REPO_ROOT / "data" / "pipeline.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """
    Abre una conexión a data/pipeline.db, creando el directorio contenedor y
    la tabla `videos` si aún no existen (seguro de llamar repetidamente).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(_CREATE_TABLE_SQL)
        yield conn
        conn.commit()
    finally:
        conn.close()


def set_status(video_id: str, status: str) -> None:
    """
    Registra el estado de un vídeo (upsert: inserta si no existe, actualiza
    si ya había un registro), con `updated_at` en UTC ISO-8601.
    """
    updated_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO videos (video_id, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (video_id, status, updated_at),
        )


def get_status(video_id: str) -> str | None:
    """Devuelve el último estado registrado para `video_id`, o None si no existe."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    return row[0] if row else None
