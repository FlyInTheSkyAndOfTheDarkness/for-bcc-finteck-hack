"""Стадия 1. Чтение документа → Document (блоки с локаторами). Владелец: A1."""
from __future__ import annotations

import hashlib
from pathlib import Path

from reqcheck.models import Block, BlockKind, Document


def load_document(path: str | Path) -> Document:
    """Прочитать PDF / DOCX / MD / TXT / HTML и вернуть Document. ЗАГЛУШКА: каждая строка — блок."""
    p = Path(path)
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    blocks = [Block(id=f"B-{i:04d}", kind=BlockKind.text, text=line.strip(), line_start=i, line_end=i)
              for i, line in enumerate(text.splitlines(), 1) if line.strip()]
    return Document(name=p.stem, path=str(p), sha256=hashlib.sha256(raw).hexdigest(),
                    format=p.suffix.lstrip(".").lower(), text=text, blocks=blocks)
