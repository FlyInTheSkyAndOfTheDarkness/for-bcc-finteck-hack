"""Помощники тестов A3: Document из Markdown-строки или файла.

Используется разборщик A1 (reqcheck.ingest.mdparse.MdParser). Если его нет или он сломан,
работает минимальный встроенный разбор по тому же контракту Block
(heading / text / list_item / table_header / table_row / code, section_path, part, cells).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from reqcheck.models import Block, BlockKind, Document

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "docs" / "requirements.md"


def _fallback_blocks(md: str) -> tuple[str, list[Block]]:
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    title = None
    header = None
    lines = md.splitlines()
    i = 0
    strip = lambda s: " ".join(re.sub(r"\*\*|__|`", "", s).split())  # noqa: E731

    def emit(kind, text, ls, le, **kw):
        path = [t for _, t in stack]
        part = next((t for t in path if t.startswith("Часть")), None)
        blocks.append(Block(id=f"B-{len(blocks) + 1:04d}", kind=kind, text=text, part=part,
                            section_path=path, line_start=ls, line_end=le, **kw))

    para: list[tuple[str, int]] = []

    def flush():
        if para:
            emit(BlockKind.text, strip(" ".join(t for t, _ in para)), para[0][1], para[-1][1])
            para.clear()

    while i < len(lines):
        raw, ln = lines[i].rstrip(), i + 1
        i += 1
        if raw.strip().startswith("```"):
            flush()
            body = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            emit(BlockKind.code, "\n".join(body).strip(), ln, i)
            continue
        if not raw.strip():
            flush()
            header = None
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if m:
            flush()
            header = None
            lvl, t = len(m.group(1)), strip(m.group(2))
            if lvl == 1 and title is None:
                title = t
            else:
                while stack and stack[-1][0] >= lvl:
                    stack.pop()
                stack.append((lvl, t))
            emit(BlockKind.heading, t, ln, ln)
            continue
        if raw.lstrip().startswith("|"):
            flush()
            cells = [strip(c) for c in raw.strip().strip("|").split("|")]
            if all(re.match(r"^:?-+:?$", c) for c in cells if c) and any(cells):
                continue
            if header is None:
                header = cells
                emit(BlockKind.table_header, " | ".join(cells), ln, ln, cells=cells)
            else:
                emit(BlockKind.table_row, " | ".join(cells), ln, ln, cells=cells, table_header=header)
            continue
        m = re.match(r"^\s*([*+-]|\d+[.)])\s+(.*)$", raw)
        if m:
            flush()
            t = f"{m.group(1)} {m.group(2)}" if m.group(1)[0].isdigit() else m.group(2)
            emit(BlockKind.list_item, strip(t), ln, ln)
            continue
        para.append((raw.strip(), ln))
    flush()
    return title or "document", blocks


def doc_from_md(md: str, name: str = "synthetic.md") -> Document:
    try:
        from reqcheck.ingest.mdparse import MdParser
        p = MdParser()
        p.feed(md)
        title, blocks = p.title or name, p.blocks
    except Exception:  # noqa: BLE001
        title, blocks = _fallback_blocks(md)
    return Document(name=title, path=name, sha256=hashlib.sha256(md.encode()).hexdigest(),
                    format="md", text="\n".join(b.text for b in blocks), blocks=blocks)


def load_input() -> Document:
    """docs/requirements.md через ingest A1 (с запасным вариантом)."""
    try:
        from reqcheck.ingest import load_document
        doc = load_document(INPUT)
        if any(b.kind == BlockKind.table_row for b in doc.blocks):
            return doc
    except Exception:  # noqa: BLE001
        pass
    return doc_from_md(INPUT.read_text(encoding="utf-8"), name=str(INPUT))
