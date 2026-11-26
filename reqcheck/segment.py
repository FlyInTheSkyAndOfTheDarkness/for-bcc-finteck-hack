"""Стадия 2. Нарезка документа на атомарные требования. Владелец: A3."""
from __future__ import annotations

from reqcheck.llm import LLMClient
from reqcheck.models import Document, Requirement


def segment(doc: Document, llm: LLMClient) -> list[Requirement]:
    """Правила + LLM → список требований с R-NN, doc_id, цитатой и локатором. ЗАГЛУШКА."""
    return []
