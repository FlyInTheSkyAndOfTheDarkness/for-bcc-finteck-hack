"""Провайдеры LLM. Владелец: A2."""
from __future__ import annotations


def make_client(settings):
    raise NotImplementedError(f"провайдер {settings.provider} ещё не реализован")
