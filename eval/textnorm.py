"""Нормализация текста и нечёткое сравнение цитат для оценки (eval).

Одна и та же нормализация используется при сопоставлении находок с gold и в тесте,
который проверяет, что все цитаты gold.yaml действительно есть в документе.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_QUOTES = dict.fromkeys(map(ord, "«»„“”‟\"'‘’‚`´"), None)
_DASHES = {ord(c): "-" for c in "—–‑‒−―"}
_SPACES = {ord(c): " " for c in "    ​\t\r\n"}


def norm(text: str | None) -> str:
    """casefold, ё→е, без кавычек и markdown-разметки, единые тире и пробелы."""
    if not text:
        return ""
    t = text.translate(_SPACES).translate(_DASHES)
    t = t.replace("ё", "е").replace("Ё", "Е")
    t = re.sub(r"(\*\*|__|~~)", "", t)        # жирный/курсив/зачёркнутый
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)  # заголовки
    t = re.sub(r"^\s*[*+]\s+", "", t, flags=re.M)    # маркеры списка
    t = t.replace("|", " ")                    # ячейки таблиц
    t = re.sub(r"^\s*:?-{3,}:?\s*$", " ", t, flags=re.M)
    t = t.translate(_QUOTES)
    t = t.casefold()
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)     # «текст .» → «текст.»
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def quote_sim(gold_quote: str, text: str, *, normalized: bool = False) -> float:
    """Похожесть цитаты gold на цитату находки (0..100).

    partial_ratio ищет лучшее выравнивание короткой строки внутри длинной. Если короче
    оказалась цитата находки, она должна покрывать хотя бы половину цитаты gold
    (иначе «GET» совпадал бы с любым местом, где встречается GET).
    """
    g = gold_quote if normalized else norm(gold_quote)
    f = text if normalized else norm(text)
    if not g or not f:
        return 0.0
    s = fuzz.partial_ratio(g, f)
    if len(f) < len(g):
        s *= min(1.0, len(f) / max(12.0, 0.5 * len(g)))
    return float(s)


def contains(doc_norm: str, quote: str, min_score: float = 97.0) -> bool:
    """Есть ли цитата в нормализованном документе (подстрока или почти подстрока)."""
    q = norm(quote)
    if not q:
        return False
    return q in doc_norm or fuzz.partial_ratio(q, doc_norm) >= min_score
