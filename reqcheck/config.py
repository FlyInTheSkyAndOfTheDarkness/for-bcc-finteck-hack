"""Настройки из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PKG = Path(__file__).resolve().parent


@dataclass
class Settings:
    provider: str = "gemini"
    model: str = ""                 # пусто → провайдер выбирает сам
    api_key: str = ""
    base_url: str = ""
    rpm: int = 8                    # запросов в минуту
    temperature: float = 0.0
    cache_dir: Path = field(default_factory=lambda: ROOT / ".cache" / "llm")
    use_cache: bool = True
    trace_path: Path | None = None  # куда писать JSONL-журнал вызовов
    max_retries: int = 5


def _num(kind, name: str, default):
    """Число из переменной окружения; пустое или нечисловое значение → значение по умолчанию."""
    raw = (os.getenv(name) or "").strip()
    try:
        return kind(raw) if raw else default
    except ValueError:
        return default


def _key(raw: str) -> str:
    """Ключ из окружения; заглушка из .env.example («your_…_here») считается отсутствующим ключом."""
    k = (raw or "").strip()
    return "" if k.lower().startswith("your_") or k.lower().endswith("_here") else k


def load_settings(**overrides) -> Settings:
    load_dotenv(ROOT / ".env")
    load_dotenv()  # .env в текущей папке, если запускают не из корня
    provider = (overrides.pop("provider", None) or os.getenv("LLM_PROVIDER") or "gemini").lower()
    env = {
        "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", None),
        "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"),
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", None),
        "ollama": (None, "OLLAMA_MODEL", "OLLAMA_BASE_URL"),
        "fake": (None, None, None),
    }.get(provider, (None, None, None))
    s = Settings(
        provider=provider,
        api_key=_key(os.getenv(env[0], "")) if env[0] else "",
        model=os.getenv(env[1], "") if env[1] else "",
        base_url=os.getenv(env[2], "") if env[2] else "",
        rpm=_num(int, "LLM_RPM", 8),
        temperature=_num(float, "LLM_TEMPERATURE", 0.0),
    )
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, v)
    return s
