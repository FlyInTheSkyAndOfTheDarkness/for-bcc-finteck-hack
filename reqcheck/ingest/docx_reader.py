"""DOCX → Markdown-строки (абзацы и таблицы в порядке следования) для MdParser."""
from __future__ import annotations

import re
from pathlib import Path

_CODE_STYLES = ("code", "source code", "preformatted", "html preformatted")
_CODE_FONTS = ("courier", "consolas", "mono")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _is_code(p) -> bool:
    if p.style is not None and p.style.name.lower() in _CODE_STYLES:
        return True
    runs = [r for r in p.runs if r.text.strip()]
    return bool(runs) and all(r.font.name and any(f in r.font.name.lower() for f in _CODE_FONTS) for r in runs)


def docx_to_markdown(path: Path) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    out: list[str] = []
    code: list[str] = []
    counter = 0

    def flush_code():
        nonlocal code
        if code:
            out.extend(["", "```", *code, "```", ""])
            code = []

    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, d)
            if _is_code(p):
                code.extend(p.text.split("\n"))
                continue
            flush_code()
            text = " ".join(p.text.split())
            if not text:
                counter = 0
                out.append("")
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"(?i)heading\s*(\d)", style)
            if m or style.lower() == "title":
                counter = 0
                out += ["", "#" * (int(m.group(1)) if m else 1) + " " + text, ""]
            elif "list number" in style.lower():
                counter += 1
                out.append(f"{counter}. {text}")
            elif "list" in style.lower() or el.find(".//{*}numPr") is not None:
                counter = 0
                out.append(f"* {text}")
            else:
                counter = 0
                if re.match(r"^(\d+[.)]|[*+\-•#|>`])", text):
                    text = "\\" + text              # не даём обычному абзацу стать списком/заголовком
                out += ["", text, ""]
        elif tag == "tbl":
            flush_code()
            counter = 0
            t = Table(el, d)
            out.append("")
            for i, row in enumerate(t.rows):
                cells, prev = [], None
                for c in row.cells:                   # объединённые ячейки python-docx повторяет
                    if c._tc is not prev:
                        cells.append(_cell(c.text))
                    prev = c._tc
                out.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    out.append("|" + " --- |" * len(cells))
            out.append("")
    flush_code()
    return "\n".join(out)
