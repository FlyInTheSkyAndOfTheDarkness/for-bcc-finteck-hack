"""Ядро разбора Markdown/TXT в блоки. Остальные форматы сводятся к Markdown и идут сюда."""
from __future__ import annotations

import re
from typing import Optional

from reqcheck.models import Block, BlockKind

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([*+\-•●▪–]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SEP_CELL = re.compile(r"^:?-+:?$")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITAL = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITAL_U = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_PART = re.compile(r"^Часть\b")


def _emph(s: str) -> str:
    for _ in range(2):
        s = _BOLD.sub(r"\2", s)
    s = _ITAL.sub(r"\1", s)
    s = _ITAL_U.sub(r"\1", s)
    return s


def normalize_inline(s: str) -> str:
    """Снять md-выделение и обратные кавычки, схлопнуть пробелы; остальное оставить дословно."""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</?(mark|b|i|u|em|strong|sub|sup|span)(\s[^>]*)?>", "", s, flags=re.I)
    out, pos = [], 0
    for m in _CODE_SPAN.finditer(s):
        out.append(_emph(s[pos:m.start()]))
        out.append(m.group(2).strip())
        pos = m.end()
    out.append(_emph(s[pos:]))
    return " ".join("".join(out).replace("\\|", "|").split())


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, in_code, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("\\|")
            i += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur))
    return [normalize_inline(c) for c in cells]


def _structural(line: str) -> bool:
    return bool(_HEADING.match(line) or _LIST.match(line) or _FENCE.match(line)
                or _HR.match(line) or line.startswith("|"))


class MdParser:
    """Потоковый разбор; feed() можно вызывать по страницам, section_path сохраняется."""

    def __init__(self, line_paragraphs: bool = False) -> None:
        # line_paragraphs (TXT): строка на .!?; и следующая с заглавной — разные абзацы
        self.line_paragraphs = line_paragraphs
        self.blocks: list[Block] = []
        self.stack: list[tuple[int, str]] = []
        self.title: Optional[str] = None
        self._n = 0
        self._table_header: Optional[list[str]] = None
        self._last_table_header: Optional[list[str]] = None
        self._para: list[tuple[str, int]] = []
        self._item: Optional[dict] = None

    def _path(self) -> list[str]:
        return [t for _, t in self.stack]

    def _part(self) -> Optional[str]:
        for t in self._path():
            if _PART.match(t):
                return t
        return None

    def _emit(self, kind: BlockKind, text: str, ls: int, le: int, page: Optional[int], **kw) -> Block:
        self._n += 1
        b = Block(id=f"B-{self._n:04d}", kind=kind, text=text, part=self._part(),
                  section_path=self._path(), line_start=ls, line_end=le, page=page, **kw)
        self.blocks.append(b)
        return b

    def _flush(self, page: Optional[int]) -> None:
        if self._para:
            text = normalize_inline(" ".join(t for t, _ in self._para))
            if text:
                self._emit(BlockKind.text, text, self._para[0][1], self._para[-1][1], page)
            self._para = []
        if self._item:
            it = self._item
            text = normalize_inline(" ".join(it["parts"]))
            if text:
                self._emit(BlockKind.list_item, text, it["ls"], it["le"], page)
            self._item = None

    def feed(self, text: str, page: Optional[int] = None, line_offset: int = 0) -> int:
        lines = text.splitlines()
        i = 0
        first = next((ln.strip() for ln in lines if ln.strip()), "")
        prev = self.blocks[-1] if self.blocks else None
        continues = prev is not None and prev.page is not None and page is not None and prev.page == page - 1
        carry_table = (continues and first.startswith("|") and self._last_table_header is not None
                       and prev.kind in (BlockKind.table_row, BlockKind.table_header))
        self._table_header = self._last_table_header if carry_table else None
        if (continues and prev.kind in (BlockKind.text, BlockKind.list_item) and first
                and not _structural(first) and first[0].islower()
                and not re.search(r"[.!?:;»)\]]$", prev.text)):
            # абзац/пункт списка продолжается на новой странице: вернуть блок в «открытое» состояние
            self.blocks.pop()
            self._n -= 1
            if prev.kind is BlockKind.list_item:
                self._item = {"parts": [prev.text], "ls": prev.line_start, "le": prev.line_end}
            else:
                self._para = [(prev.text, prev.line_start)]
            while i < len(lines) and not lines[i].strip():
                i += 1
        while i < len(lines):
            raw = lines[i].rstrip()
            ln = line_offset + i + 1
            i += 1
            fm = _FENCE.match(raw)
            if fm:
                self._flush(page)
                self._table_header = None
                fence = fm.group(1)
                body, start = [], ln
                while i < len(lines) and not lines[i].strip().startswith(fence):
                    body.append(lines[i].rstrip())
                    i += 1
                end = line_offset + min(i + 1, len(lines))
                i += 1
                code = "\n".join(body).strip("\n")
                if code.strip():
                    self._emit(BlockKind.code, code, start, end, page)
                continue
            if not raw.strip():
                self._flush(page)
                self._table_header = None
                continue
            if raw.startswith("\\") and len(raw) > 1 and raw[1] in "#*+-•0123456789|`>":
                self._table_header = None          # экранированное начало абзаца: «. Текст» — не список
                if self._item is None:
                    self._para.append((raw[1:].strip(), ln))
                else:
                    self._item["parts"].append(raw[1:].strip())
                    self._item["le"] = ln
                continue
            m = _HEADING.match(raw)
            if m:
                self._flush(page)
                self._table_header = None
                level, htext = len(m.group(1)), normalize_inline(m.group(2))
                if not htext:
                    continue
                if level == 1 and self.title is None:
                    self.title = htext
                else:
                    while self.stack and self.stack[-1][0] >= level:
                        self.stack.pop()
                    self.stack.append((level, htext))
                self._emit(BlockKind.heading, htext, ln, ln, page)
                continue
            if _HR.match(raw):
                self._flush(page)
                self._table_header = None
                continue
            if raw.lstrip().startswith("|"):
                self._flush(page)
                cells = split_row(raw)
                if cells and any(cells) and all(_SEP_CELL.match(c) for c in cells if c):
                    continue
                if self._table_header is not None and cells == self._table_header:
                    continue                       # повтор шапки таблицы на новой странице
                if self._table_header is None:
                    self._table_header = self._last_table_header = cells
                    self._emit(BlockKind.table_header, " | ".join(cells), ln, ln, page, cells=cells)
                else:
                    self._emit(BlockKind.table_row, " | ".join(cells), ln, ln, page,
                               cells=cells, table_header=self._table_header)
                continue
            self._table_header = None
            m = _LIST.match(raw)
            if m:
                self._flush(page)
                marker, body = m.group(2), m.group(3)
                first = f"{marker} {body}" if marker[0].isdigit() else body
                self._item = {"parts": [first], "ls": ln, "le": ln}
                continue
            if self._item is not None:           # продолжение пункта списка
                self._item["parts"].append(raw.strip())
                self._item["le"] = ln
                continue
            if (self.line_paragraphs and self._para and self._para[-1][0][-1:] in ".!?;"
                    and re.match(r"[A-ZА-ЯЁ0-9«\"]", raw.strip())):
                self._flush(page)
            self._para.append((raw.strip(), ln))
        self._flush(page)
        return line_offset + len(lines)
