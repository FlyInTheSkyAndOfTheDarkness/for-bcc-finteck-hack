"""Стадия 4. LLM-проверка каждого требования по ISO 29148 §5.2.5. Владелец: A7."""
from __future__ import annotations

from reqcheck.llm import LLMClient
from reqcheck.models import Document, Finding, Requirement


def review_requirements(doc: Document, reqs: list[Requirement], llm: LLMClient) -> list[Finding]:
    """ЗАГЛУШКА."""
    return []
