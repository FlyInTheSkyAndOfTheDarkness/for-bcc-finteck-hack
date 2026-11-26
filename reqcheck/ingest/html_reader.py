"""HTML → Markdown-строки (h1–h6, p, li, table, pre) для MdParser."""
from __future__ import annotations

from html.parser import HTMLParser

_SKIP = {"script", "style", "head", "title"}
_HEAD = ("h1", "h2", "h3", "h4", "h5", "h6")


class _H2M(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.buf: list[str] = []
        self.mode: str | None = None     # h1..h6 | p | li | cell | pre
        self.lists: list[list] = []      # [ordered, counter]
        self.row: list[str] | None = None
        self.rows_in_table = 0
        self.skip = 0
        self.pre = False

    def _end_block(self) -> None:
        text = "".join(self.buf)
        if self.mode in _HEAD:
            self.buf = []
            t = " ".join(text.split())
            if t:
                self.out += ["", "#" * int(self.mode[1]) + " " + t, ""]
        elif self.mode == "li":
            self.buf = []
            t = " ".join(text.split())
            if t:
                if self.lists and self.lists[-1][0]:
                    self.lists[-1][1] += 1
                    self.out.append(f"{self.lists[-1][1]}. {t}")
                else:
                    self.out.append(f"* {t}")
        elif self.mode == "p":
            self.buf = []
            t = " ".join(text.split())
            if t:
                self.out += ["", t, ""]
        self.mode = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        if self.skip:
            return
        if tag in _HEAD or tag in ("p", "li", "div"):
            self._end_block()
            self.mode = "p" if tag == "div" else tag
        elif tag in ("ul", "ol"):
            self._end_block()
            self.lists.append([tag == "ol", 0])
        elif tag == "table":
            self._end_block()
            self.out.append("")
            self.rows_in_table = 0
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.buf = []
            self.mode = "cell"
        elif tag == "pre":
            self._end_block()
            self.pre, self.mode, self.buf = True, "pre", []
        elif tag == "br":
            self.buf.append("\n" if self.pre else " ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _HEAD or tag in ("p", "li", "div"):
            self._end_block()
        elif tag in ("ul", "ol"):
            self._end_block()
            if self.lists:
                self.lists.pop()
            self.out.append("")
        elif tag in ("td", "th"):
            cell = " ".join("".join(self.buf).split()).replace("|", "\\|")
            if self.row is not None:
                self.row.append(cell)
            self.buf, self.mode = [], None
        elif tag == "tr" and self.row is not None:
            self.out.append("| " + " | ".join(self.row) + " |")
            if self.rows_in_table == 0:
                self.out.append("|" + " --- |" * len(self.row))
            self.rows_in_table += 1
            self.row = None
        elif tag == "table":
            self.out.append("")
        elif tag == "pre":
            code = "".join(self.buf).strip("\n")
            self.out += ["", "```", code, "```", ""]
            self.buf, self.mode, self.pre = [], None, False

    def handle_data(self, data):
        if self.skip:
            return
        if self.mode is None and data.strip():
            self.mode = "p"
        self.buf.append(data)


def html_to_markdown(html: str) -> str:
    p = _H2M()
    p.feed(html)
    p.close()
    p._end_block()
    return "\n".join(p.out)
