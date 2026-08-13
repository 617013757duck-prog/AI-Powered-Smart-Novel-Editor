from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
NOVELS_DIR = DATA_DIR / "novels"
CHROMA_DIR = DATA_DIR / "chroma_db"
CONFIG_FILE = DATA_DIR / "config.json"
PROMPTS_FILE = DATA_DIR / "prompts.json"

for d in [DATA_DIR, NOVELS_DIR, CHROMA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "model": "deepseek",
        "timeout": 300,
        "temperature": 0.7
    },
    "external_api": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": 120,
        "temperature": 0.7
    },
    "ai_provider": {
        "provider": "local"
    },
    "embedding": {
        "model_name": "default",
        "chunk_size": 500,
        "chunk_overlap": 50
    },
    "tri_ai": {
        "writer_temperature": 0.8,
        "reviewer_temperature": 0.3,
        "chat_temperature": 0.5,
        "retrieve_top_k": 8,
        "allow_ai_rename_chapter": False
    }
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            merged = _deep_merge(DEFAULT_CONFIG.copy(), user_cfg)
            return merged
        except Exception:
            pass
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
