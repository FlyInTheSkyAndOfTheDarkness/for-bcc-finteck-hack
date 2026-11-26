"""Привязка цитат к тексту документа (anti-hallucination). Владелец: A3.

find_quote(doc, quote) -> (оценка 0..100, лучший блок | None)

Обе стороны нормализуются (регистр, пробелы, md-разметка, кавычки, тире, ё→е, ₸/тг,
разряды чисел «1 000» → «1000»). Сначала ищется точное вхождение (в блоке или на стыке
2–3 соседних блоков), затем rapidfuzz partial_ratio по блокам и окнам из 2–3 блоков.
Цитату с многоточием («начало … конец») проверяем по частям.
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

from reqcheck.models import Block, Document

GROUND_THRESHOLD = 85  # rapidfuzz partial_ratio, 0..100

_QUOTES = str.maketrans({c: '"' for c in "«»„“”‟❝❞＂‘’‚‛`´"} | {"'": '"'})
_DASHES = str.maketrans({c: "-" for c in "—–‒―−‐‑"})
_MD = re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)|^#{1,6}\s+|^\s*[*+\-•●▪]\s+", re.M)
_TG = re.compile(r"(?<![а-яa-z])тг\.?(?![а-яa-z])")
_DIGIT_GROUP = re.compile(r"(?<=\d)[    ](?=\d{3}(?!\d))")
_SPACE_PUNCT = re.compile(r"\s+([.,;:!?)])")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.+|…)\s*")


def normalize(s: str) -> str:
    """Каноническая форма текста для сравнения цитат."""
    if not s:
        return ""
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = _MD.sub(" ", s)
    s = s.replace("|", " ")
    s = s.casefold().replace("ё", "е")
    s = s.translate(_QUOTES).translate(_DASHES)
    s = _TG.sub("₸", s)
    s = " ".join(s.split())
    s = _DIGIT_GROUP.sub("", s)
    s = _SPACE_PUNCT.sub(r"\1", s)
    return s.strip(" .;:,")


class _Index:
    """Нормализованные тексты блоков документа и окна из соседних блоков."""

    def __init__(self, doc: Document) -> None:
        self.blocks = [b for b in doc.blocks if b.text and b.text.strip()]
        self.norm = [normalize(b.text) for b in self.blocks]
        # окна: (текст, индекс первого блока, [смещения начала каждого блока в тексте])
        self.windows: list[tuple[str, int, list[int]]] = []
        n = len(self.blocks)
        for size in (1, 2, 3):
            for i in range(0, n - size + 1):
                parts = self.norm[i:i + size]
                if size > 1 and not all(parts):
                    continue
                offs, pos = [], 0
                for p in parts:
                    offs.append(pos)
                    pos += len(p) + 1
                self.windows.append((" ".join(parts), i, offs))
        self.joined = " ".join(self.norm)

    def block_at(self, win: tuple[str, int, list[int]], pos: int) -> Block:
        _, start, offs = win
        k = 0
        for j, o in enumerate(offs):
            if pos >= o:
                k = j
        return self.blocks[start + k]


_CACHE: dict[tuple, _Index] = {}


def _index(doc: Document) -> _Index:
    key = (id(doc), doc.sha256, len(doc.blocks), doc.blocks[0].id if doc.blocks else "")
    idx = _CACHE.get(key)
    if idx is None:
        if len(_CACHE) > 16:
            _CACHE.clear()
        idx = _CACHE[key] = _Index(doc)
    return idx


def _score_one(idx: _Index, q: str) -> tuple[float, Optional[Block]]:
    if not q:
        return 0.0, None
    # 1. точное вхождение: сначала одиночные блоки, затем окна
    for win in idx.windows:
        p = win[0].find(q)
        if p >= 0:
            return 100.0, idx.block_at(win, p)
    if len(q) < 6:               # короткие фрагменты — только точное совпадение
        return 0.0, None
    # 2. нечёткий поиск
    best, best_win = 0.0, None
    qlen = len(q)
    for win in idx.windows:
        text = win[0]
        if not text:
            continue
        if len(text) >= qlen:
            sc = fuzz.partial_ratio(q, text, score_cutoff=max(best, 50))
        else:
            # кандидат короче цитаты: partial_ratio нашёл бы блок внутри «раздутой» цитаты,
            # поэтому штрафуем за непокрытую часть цитаты.
            sc = fuzz.partial_ratio(q, text) * len(text) / qlen
        if sc > best:
            best, best_win = sc, win
            if best >= 99.9:
                break
    if best_win is None:
        return 0.0, None
    block = idx.blocks[best_win[1]]
    if len(best_win[2]) > 1 and len(best_win[0]) >= qlen:
        al = fuzz.partial_ratio_alignment(q, best_win[0])
        if al is not None:
            block = idx.block_at(best_win, al.dest_start)
    return round(float(best), 1), block


def find_quote(doc: Document, quote: str) -> tuple[float, Optional[Block]]:
    """Найти цитату в документе. Возвращает (оценка 0..100, лучший блок)."""
    if not quote or not doc.blocks:
        return 0.0, None
    idx = _index(doc)
    parts = [normalize(p) for p in _ELLIPSIS.split(quote)]
    parts = [p for p in parts if len(p) >= 3]
    if not parts:
        return 0.0, None
    if len(parts) == 1:
        return _score_one(idx, parts[0])
    scores = [_score_one(idx, p) for p in parts]
    score = min(s for s, _ in scores)
    return score, scores[0][1]


def locate_all(doc: Document, quote: str) -> list[Block]:
    """Все блоки, где цитата встречается точно (после нормализации)."""
    q = normalize(quote)
    if not q:
        return []
    idx = _index(doc)
    return [b for b, t in zip(idx.blocks, idx.norm) if q in t]
