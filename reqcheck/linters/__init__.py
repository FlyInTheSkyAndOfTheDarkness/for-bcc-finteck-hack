"""Стадия 3. Детерминированные проверки без LLM. Владелец: A3."""
from __future__ import annotations

from reqcheck.models import Document, Finding, Requirement


def run_linters(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    """Вернуть находки source=linter, с точной цитатой и локатором. ЗАГЛУШКА."""
    return []
