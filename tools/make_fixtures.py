"""Сгенерировать docs/requirements.pdf и docs/requirements.docx из docs/requirements.md.

Запуск: python tools/make_fixtures.py   (копии кладутся и в tests/fixtures/)
"""
from __future__ import annotations

import shutil
import sys
import warnings
from html.parser import HTMLParser
from pathlib import Path

warnings.filterwarnings("ignore")
import markdown  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
FIX = ROOT / "tests" / "fixtures"
FONTS = Path("C:/Windows/Fonts")

CSS = """
@font-face { font-family: ArialC; src: url(arial.ttf); }
@font-face { font-family: ArialC; font-weight: bold; src: url(arialbd.ttf); }
@font-face { font-family: ArialC; font-style: italic; src: url(ariali.ttf); }
@font-face { font-family: MonoC; src: url(consola.ttf); }
body { font-family: ArialC; font-size: 10pt; line-height: 1.3; }
h1 { font-size: 22pt; margin-top: 6pt; }
h2 { font-size: 18pt; margin-top: 14pt; }
h3 { font-size: 15pt; margin-top: 10pt; }
h4 { font-size: 13pt; margin-top: 8pt; }
code, pre { font-family: MonoC; font-size: 9pt; }
pre { margin-top: 4pt; margin-bottom: 4pt; }
table { border-collapse: collapse; width: 100%; margin-top: 4pt; margin-bottom: 6pt; }
th, td { border: 1px solid #555555; padding: 3pt; font-size: 9pt; vertical-align: top; }
th { background-color: #e6e6e6; font-weight: bold; }
hr { color: #999999; }
"""


def md_to_html(md_text: str) -> str:
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])
    body = body.replace("<hr />", "")
    return f"<html><body>{body}</body></html>"


def make_pdf(html: str, out: Path) -> None:
    import pymupdf as fitz
    story = fitz.Story(html=html, user_css=CSS, archive=fitz.Archive(str(FONTS)))
    writer = fitz.DocumentWriter(str(out))
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (50, 50, -50, -50)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


class _DocxBuilder(HTMLParser):
    def __init__(self, doc) -> None:
        super().__init__(convert_charrefs=True)
        self.d = doc
        self.para = None
        self.bold = 0
        self.code_inline = 0
        self.lists: list[str] = []
        self.pre = False
        self.table = None
        self.row_cells: list[str] | None = None
        self.cell_buf: list[str] = []
        self.in_cell = False
        self.cur_th = False

    def _add(self, text: str) -> None:
        if self.para is None or not text:
            return
        run = self.para.add_run(text)
        run.bold = bool(self.bold) or None
        if self.code_inline:
            run.font.name = "Consolas"

    def handle_starttag(self, tag, attrs):
        d = self.d
        if tag in ("h1", "h2", "h3", "h4"):
            self.para = d.add_paragraph(style=f"Heading {tag[1]}")
        elif tag == "p":
            self.para = d.add_paragraph()
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
        elif tag == "li":
            self.para = d.add_paragraph(style="List Bullet" if self.lists[-1] == "ul" else "List Number")
        elif tag in ("strong", "b"):
            self.bold += 1
        elif tag == "code" and not self.pre:
            self.code_inline += 1
        elif tag == "br":
            if self.in_cell:
                self.cell_buf.append(" ")
            elif self.para is not None:
                self.para.add_run().add_break()
        elif tag == "pre":
            self.pre = True
        elif tag == "table":
            self.table = []
        elif tag == "tr":
            self.row_cells = []
        elif tag in ("td", "th"):
            self.in_cell, self.cell_buf = True, []
            self.cur_th = tag == "th"

    def handle_endtag(self, tag):
        if tag in ("strong", "b"):
            self.bold -= 1
        elif tag == "code" and not self.pre:
            self.code_inline -= 1
        elif tag in ("ul", "ol"):
            self.lists.pop()
        elif tag in ("h1", "h2", "h3", "h4", "p", "li"):
            self.para = None
        elif tag in ("td", "th"):
            self.row_cells.append("".join(self.cell_buf).strip())
            self.in_cell = False
        elif tag == "tr":
            self.table.append(self.row_cells)
            self.row_cells = None
        elif tag == "table":
            self._flush_table()
            self.table = None
        elif tag == "pre":
            self.pre = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell_buf.append(data)
        elif self.pre:
            code = data.strip("\n")
            for line in code.split("\n"):
                p = self.d.add_paragraph(style="Code")
                r = p.add_run(line)
                r.font.name = "Consolas"
        elif self.para is not None:
            self._add(data.replace("\n", " "))

    def _flush_table(self) -> None:
        rows = self.table
        ncols = max(len(r) for r in rows)
        t = self.d.add_table(rows=len(rows), cols=ncols)
        t.style = "Table Grid"
        for i, r in enumerate(rows):
            for j in range(ncols):
                cell = t.cell(i, j)
                cell.text = r[j] if j < len(r) else ""
                if i == 0:
                    for run in cell.paragraphs[0].runs:
                        run.bold = True
        self.d.add_paragraph()


def make_docx(html: str, out: Path) -> None:
    import docx
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Pt
    d = docx.Document()
    st = d.styles.add_style("Code", WD_STYLE_TYPE.PARAGRAPH)
    st.base_style = d.styles["Normal"]
    st.font.name = "Consolas"
    st.font.size = Pt(9)
    _DocxBuilder(d).feed(html)
    d.save(str(out))


def main() -> None:
    md_text = (DOCS / "requirements.md").read_text(encoding="utf-8")
    html = md_to_html(md_text)
    make_pdf(html, DOCS / "requirements.pdf")
    make_docx(html, DOCS / "requirements.docx")
    FIX.mkdir(parents=True, exist_ok=True)
    for ext in ("md", "pdf", "docx"):
        shutil.copy(DOCS / f"requirements.{ext}", FIX / f"requirements.{ext}")
    print("ok:", ", ".join(f"requirements.{e}" for e in ("pdf", "docx")))


if __name__ == "__main__":
    sys.exit(main())
