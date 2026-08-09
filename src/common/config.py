"""Carga config/settings.yaml + variables de entorno (.env)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_config(path: str | Path = REPO_ROOT / "config" / "settings.yaml") -> dict:
    load_dotenv(REPO_ROOT / ".env")
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["_env"] = {
        "anthropic_api_key": os.getenv("NEWCLIPS_ANTHROPIC_API_KEY"),
        "gemini_api_key": os.getenv("GEMINI_API_KEY"),
        "youtube_client_secret_path": os.getenv("YOUTUBE_CLIENT_SECRET_PATH"),
    }
    return config
