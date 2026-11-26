"""Стадия 6. Проверка находок: привязка, реальность, дубли, критичность. Владелец: A7."""
from __future__ import annotations

from reqcheck.llm import LLMClient
from reqcheck.models import Document, Finding, Requirement


def verify(doc: Document, reqs: list[Requirement], findings: list[Finding], llm: LLMClient) -> list[Finding]:
    """Вернуть отфильтрованные и объединённые находки с verified/confidence. ЗАГЛУШКА."""
    return findings
