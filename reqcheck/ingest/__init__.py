"""Стадия 1. Чтение документа → Document (блоки с локаторами). Владелец: A1."""
from __future__ import annotations

import hashlib
from pathlib import Path

from reqcheck.ingest.mdparse import MdParser
from reqcheck.models import Document

SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx", ".html", ".htm"}


def _parse(md: str = "", pages: list[tuple[int, str]] | None = None, line_paragraphs: bool = False) -> MdParser:
    p = MdParser(line_paragraphs=line_paragraphs)
    if pages is None:
        p.feed(md)
    else:
        off = 0
        for page, text in pages:
            off = p.feed(text, page=page, line_offset=off)
    return p


def _decode(raw: bytes) -> str:
    """UTF-8 (с BOM или без), UTF-16 с BOM; иначе cp1251 — типичная кодировка русских TXT/HTML в Windows."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1251")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def load_document(path: str | Path) -> Document:
    """Прочитать PDF / DOCX / MD / TXT / HTML и вернуть Document."""
    p = Path(path)
    raw = p.read_bytes()
    ext = p.suffix.lower()
    if ext in (".md", ".markdown", ".txt"):
        # в TXT строка, законченная точкой, — конец абзаца (не склеиваем соседние требования)
        parser = _parse(_decode(raw), line_paragraphs=ext == ".txt")
        fmt = "txt" if ext == ".txt" else "md"
    elif ext == ".pdf":
        from reqcheck.ingest.pdf import pdf_pages
        parser = _parse(pages=pdf_pages(p))
        fmt = "pdf"
    elif ext == ".docx":
        from reqcheck.ingest.docx_reader import docx_to_markdown
        parser = _parse(docx_to_markdown(p))
        fmt = "docx"
    elif ext in (".html", ".htm"):
        from reqcheck.ingest.html_reader import html_to_markdown
        parser = _parse(html_to_markdown(_decode(raw)))
        fmt = "html"
    else:
        raise ValueError(f"Неподдерживаемый формат: {ext or p.name} (поддерживаются {', '.join(sorted(SUPPORTED))})")
    return Document(name=parser.title or p.stem, path=str(p), sha256=hashlib.sha256(raw).hexdigest(),
                    format=fmt, text="\n".join(b.text for b in parser.blocks), blocks=parser.blocks)
