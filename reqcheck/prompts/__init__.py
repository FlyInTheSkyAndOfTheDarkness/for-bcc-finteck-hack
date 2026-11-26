"""Загрузка промптов и каталогов. Промпты — prompts/*.md, каталоги — criteria/*.yaml, checks/*.yaml."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parent.parent
PROMPTS = PKG / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Текст промпта prompts/<name>.md."""
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def load_yaml(relpath: str):
    """YAML из пакета, например 'criteria/iso29148.yaml'."""
    return yaml.safe_load((PKG / relpath).read_text(encoding="utf-8"))


def prompt_version() -> str:
    """Короткий хэш всех промптов и каталогов — фиксируется в паспорте прогона."""
    h = hashlib.sha256()
    for d in ("prompts", "criteria", "checks", "lexicon"):
        for f in sorted((PKG / d).glob("**/*")):
            if f.is_file() and f.suffix in (".md", ".yaml", ".yml"):
                h.update(f.relative_to(PKG).as_posix().encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:10]
