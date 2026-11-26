"""Привязка цитат к тексту документа (anti-hallucination). Владелец: A3."""
from __future__ import annotations

from typing import Optional

from reqcheck.models import Block, Document

GROUND_THRESHOLD = 85  # rapidfuzz partial_ratio, 0..100


def find_quote(doc: Document, quote: str) -> tuple[float, Optional[Block]]:
    """Найти цитату в документе. Возвращает (оценка 0..100, лучший блок). ЗАГЛУШКА: точное вхождение."""
    q = " ".join(quote.split())
    for b in doc.blocks:
        if q and q in " ".join(b.text.split()):
            return 100.0, b
    return 0.0, None
