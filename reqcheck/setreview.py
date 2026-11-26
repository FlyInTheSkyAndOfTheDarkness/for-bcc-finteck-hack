"""Стадия 5. Проверка набора целиком (§5.2.6, IEEE 830 §4.3, fintech-чек-лист). Владелец: A7."""
from __future__ import annotations

from reqcheck.llm import LLMClient
from reqcheck.models import Document, Finding, Requirement, SetSummary


def review_set(doc: Document, reqs: list[Requirement], llm: LLMClient,
               prior: list[Finding]) -> tuple[list[Finding], SetSummary]:
    """ЗАГЛУШКА."""
    return [], SetSummary()
