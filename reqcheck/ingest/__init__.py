"""Стадия 1. Чтение документа → Document (блоки с локаторами). Владелец: A1."""
from __future__ import annotations

import hashlib
from pathlib import Path

from reqcheck.ingest.mdparse import MdParser
from reqcheck.models import Document

SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx", ".html", ".htm"}


def _parse(md: str = "", pages: list[tuple[int, str]] | None = None) -> MdParser:
    p = MdParser()
    if pages is None:
        p.feed(md)
    else:
        off = 0
        for page, text in pages:
            off = p.feed(text, page=page, line_offset=off)
    return p


def load_document(path: str | Path) -> Document:
    """Прочитать PDF / DOCX / MD / TXT / HTML и вернуть Document."""
    p = Path(path)
    raw = p.read_bytes()
    ext = p.suffix.lower()
    if ext in (".md", ".markdown", ".txt"):
        parser = _parse(raw.decode("utf-8-sig", errors="replace"))
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
        parser = _parse(html_to_markdown(raw.decode("utf-8-sig", errors="replace")))
        fmt = "html"
    else:
        raise ValueError(f"Неподдерживаемый формат: {ext or p.name} (поддерживаются {', '.join(sorted(SUPPORTED))})")
    return Document(name=parser.title or p.stem, path=str(p), sha256=hashlib.sha256(raw).hexdigest(),
                    format=fmt, text="\n".join(b.text for b in parser.blocks), blocks=parser.blocks)
