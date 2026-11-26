"""DOCX → Markdown-строки (абзацы и таблицы в порядке следования) для MdParser."""
from __future__ import annotations

import re
from pathlib import Path

_CODE_STYLES = ("code", "source code", "preformatted", "html preformatted")
_CODE_FONTS = ("courier", "consolas", "mono")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _style_name(p) -> str:
    try:
        return (p.style.name if p.style is not None else "") or ""
    except Exception:  # noqa: BLE001  битые/нестандартные стили
        return ""


def _is_bold_heading(p, text: str) -> bool:
    """Абзац-«заголовок» без стиля Heading: целиком жирный, короткий, без точки в конце."""
    runs = [r for r in p.runs if r.text.strip()]
    if not runs or len(text.split()) > 12 or len(text) > 120 or text[-1] in ".;:,!?":
        return False
    style_bold = False
    try:
        style_bold = bool(p.style is not None and p.style.font.bold)
    except Exception:  # noqa: BLE001
        pass
    return all(r.bold or (r.bold is None and style_bold) for r in runs)


_NUM_HEADING = re.compile(r"^\d+(?:\.\d+)*\.?\s+(?=[А-ЯЁA-Z])")
_GENERIC_HEADINGS = re.compile(r"введени|глоссари|термин|сокращени|область применени|приложени|"
                               r"требовани|сведени|цел[иь]\b|назначени", re.I)


def _is_numbered_heading(text: str) -> bool:
    """Обычный абзац «3. Функциональные требования» — заголовок раздела, набранный вручную."""
    m = _NUM_HEADING.match(text)
    if not m or len(text.split()) > 8 or text[-1] in ".;:,!?":
        return False
    from reqcheck.segment import SECTION_RULES
    bare = text[m.end():]
    return bool(_GENERIC_HEADINGS.search(bare) or any(rx.search(bare) for rx, _ in SECTION_RULES))


def _is_code(p) -> bool:
    if _style_name(p).lower() in _CODE_STYLES:
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
    seen_heading = False

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
            style = _style_name(p)
            m = re.match(r"(?i)(?:heading|заголовок)\s*(\d)", style)
            if m or style.lower() in ("title", "название", "заголовок"):
                counter = 0
                out += ["", "#" * (int(m.group(1)) if m else 1) + " " + text, ""]
                seen_heading = True
            elif (_is_bold_heading(p, text) or _is_numbered_heading(text)) \
                    and "list" not in style.lower() and el.find(".//{*}numPr") is None:
                # заголовок, оформленный жирным шрифтом: уровень по номеру «2.1 …», первый — название
                counter = 0
                num = re.match(r"^(\d+(?:\.\d+)*)\.?\s", text)
                level = (2 + num.group(1).count(".")) if num else (1 if not seen_heading else 2)
                out += ["", "#" * min(level, 6) + " " + text, ""]
                seen_heading = True
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
