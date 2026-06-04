import json
import os
from pathlib import Path
from copy import deepcopy

SETTINGS_PATH = Path("data/settings.json")

DEFAULT_SETTINGS = {
    "active_provider": "openai",
    "providers": {
        "openai": {
            "api_key": "",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1"
        },
        "claude": {
            "api_key": "",
            "model": "claude-opus-4-5"
        },
        "gemini": {
            "api_key": "",
            "model": "gemini-2.0-flash"
        },
        "openrouter": {
            "api_key": "",
            "model": "openai/gpt-4o",
            "base_url": "https://openrouter.ai/api/v1"
        },
        "local": {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3",
            "api_key": "ollama"
        }
    },
    "playwright": {
        "headless": False,
        "timeout": 30000,
        "download_dir": "data/downloads"
    },
    "ui": {
        "theme": "dark",
        "stream": True
    }
}


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r") as f:
                data = json.load(f)
            # Merge with defaults (fill missing keys)
            merged = deepcopy(DEFAULT_SETTINGS)
            _deep_merge(merged, data)
            return merged
        except Exception:
            pass
    return deepcopy(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> dict:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    return settings


def get_provider_config(settings: dict, provider: str = None) -> dict:
    provider = provider or settings.get("active_provider", "openai")
    return settings.get("providers", {}).get(provider, {})


def _deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
