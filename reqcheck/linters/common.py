"""Общие помощники линтеров: контекст документа, построение Finding с точной цитатой."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import yaml

from reqcheck.models import Block, BlockKind, Criterion, Document, Finding, FindingSource, Requirement, Severity
from reqcheck.segment import section_mode

LEXICON = Path(__file__).resolve().parent.parent / "lexicon"


@lru_cache(maxsize=None)
def lexicon(name: str):
    return yaml.safe_load((LEXICON / name).read_text(encoding="utf-8"))


def wb(pattern: str) -> str:
    """Обернуть шаблон границами слова, работающими для кириллицы."""
    return rf"(?<![а-яёa-z0-9])(?:{pattern})(?![а-яёa-z0-9])"


_SENT_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)")


def sentence_at(text: str, pos: int) -> str:
    """Предложение (дословно), содержащее позицию pos. Для строки таблицы — ячейка."""
    if " | " in text:
        start = text.rfind(" | ", 0, pos)
        start = 0 if start < 0 else start + 3
        end = text.find(" | ", pos)
        end = len(text) if end < 0 else end
        return text[start:end].strip()
    for m in _SENT_RE.finditer(text):
        if m.start() <= pos < max(m.end(), m.start() + 1):
            return m.group(0).strip()
    return text.strip()


def cell_values(b: Block) -> list[str]:
    if b.cells is not None:
        return [c.strip() for c in b.cells]
    return [c.strip() for c in b.text.split("|")]


class Ctx:
    def __init__(self, doc: Document, reqs: list[Requirement]):
        self.doc = doc
        self.reqs = reqs
        self.by_id = {b.id: b for b in doc.blocks}
        self.pos = {b.id: i for i, b in enumerate(doc.blocks)}
        self.block_rids: dict[str, list[str]] = {}
        for r in reqs:
            for bid in r.block_ids:
                self.block_rids.setdefault(bid, []).append(r.rid)
        self.req_by_rid = {r.rid: r for r in reqs}
        self._modes = {b.id: section_mode(b) for b in doc.blocks}

    # ---- разделы
    def mode(self, b: Block) -> Optional[str]:
        return self._modes[b.id][0]

    def api_ctx(self, b: Block) -> bool:
        return self._modes[b.id][1]

    def api_blocks(self) -> list[Block]:
        return [b for b in self.doc.blocks if self.api_ctx(b)]

    def req_blocks(self) -> list[Block]:
        """Блоки, вошедшие в требования (кроме служебных meta)."""
        meta = {bid for r in self.reqs if r.kind.value == "meta" for bid in r.block_ids}
        ids = {bid for r in self.reqs for bid in r.block_ids} - meta
        return [b for b in self.doc.blocks if b.id in ids]

    def tables(self) -> list[list[Block]]:
        """Таблицы документа: [header, row, row, …] (header может отсутствовать)."""
        out: list[list[Block]] = []
        cur: list[Block] = []
        for b in self.doc.blocks:
            if b.kind == BlockKind.table_header:
                if cur:
                    out.append(cur)
                cur = [b]
            elif b.kind == BlockKind.table_row:
                if cur and (cur[-1].kind in (BlockKind.table_header, BlockKind.table_row)) \
                        and cur[-1].section_path == b.section_path:
                    cur.append(b)
                else:
                    if cur:
                        out.append(cur)
                    cur = [b]
            else:
                if cur:
                    out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return out

    def rids_for(self, blocks: Iterable[Block]) -> list[str]:
        seen: list[str] = []
        for b in blocks:
            for r in self.block_rids.get(b.id, []):
                if r not in seen:
                    seen.append(r)
        return sorted(seen)

    def rids_for_quote(self, b: Block, quote: str) -> list[str]:
        """Требования блока b, чья цитата пересекается с quote (для блоков с несколькими требованиями)."""
        rids = self.block_rids.get(b.id, [])
        if len(rids) <= 1:
            return list(rids)
        q = " ".join(quote.split())
        hit = [r for r in rids if q in " ".join(self.req_by_rid[r].quote.split())
               or " ".join(self.req_by_rid[r].quote.split()) in q]
        return hit or list(rids)

    # ---- находка
    def finding(self, *, criterion: Criterion, severity: Severity, title: str, quote: str, block: Block,
                explanation: str, recommendation: str, confidence: float = 0.9, scope: str = "requirement",
                rids: Optional[list[str]] = None, extra_blocks: Iterable[Block] = ()) -> Finding:
        q = " ".join(quote.split())
        if q not in " ".join(block.text.split()):
            q = " ".join(block.text.split())       # цитата обязана быть из текста блока
        if rids is None:
            rids = self.rids_for_quote(block, q)
            for eb in extra_blocks:
                for r in self.block_rids.get(eb.id, []):
                    if r not in rids:
                        rids.append(r)
            rids = sorted(rids)
        return Finding(scope=scope, rids=rids, criterion=criterion, severity=severity, title=title,
                       quote=q, location=block.locator(), explanation=explanation,
                       recommendation=recommendation, source=FindingSource.linter,
                       confidence=confidence, verified=False, grounded=True)


def loc(b: Block) -> str:
    return b.locator()


def short(s: str, n: int = 90) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"
