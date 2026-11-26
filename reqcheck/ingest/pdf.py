"""PDF → список (номер страницы, markdown страницы).

Основной путь — pymupdf4llm (классический движок на find_tables: надёжен для таблиц с линиями),
запасные: layout-движок pymupdf4llm, затем простое fitz-извлечение текста.
"""
from __future__ import annotations

import re
import warnings
from collections import Counter
from pathlib import Path

_PAGE_NO = re.compile(r"^\W*(стр\.?|page|страница)?\s*\d{1,4}(\s*(/|из|of)\s*\d{1,4})?\W*$", re.I)


def _layout_pages(path: Path):
    import pymupdf4llm
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    return [(c["metadata"].get("page_number") or c["metadata"].get("page") or i + 1, c["text"])
            for i, c in enumerate(chunks)]


def _rag_pages(path: Path):
    from pymupdf4llm.helpers import pymupdf_rag
    chunks = pymupdf_rag.to_markdown(str(path), page_chunks=True)
    return [(c["metadata"].get("page") or c["metadata"].get("page_number") or i + 1, c["text"])
            for i, c in enumerate(chunks)]


def _table_runs(text: str) -> list[list[str]]:
    runs, cur = [], []
    for ln in text.splitlines():
        if ln.lstrip().startswith("|"):
            cur.append(ln)
        elif cur and not ln.strip():
            continue
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def _swap_tables(layout: str, rag: str) -> str:
    """Layout-движок лучше с кодом и порядком чтения, классический — с таблицами (переносы в ячейках)."""
    a, b = _table_runs(layout), _table_runs(rag)
    if not a or len(a) != len(b):
        return layout
    out, k, in_tab = [], 0, False
    for ln in layout.splitlines():
        if ln.lstrip().startswith("|"):
            if not in_tab:
                out.extend(b[k])
                k += 1
                in_tab = True
        elif ln.strip() and in_tab:
            in_tab = False
            out.append(ln)
        elif not in_tab:
            out.append(ln)
    return "\n".join(out)


def _extract(path: Path) -> list[tuple[int, str]]:
    warnings.filterwarnings("ignore")
    layout = rag = None
    try:
        layout = _layout_pages(path)
    except Exception:  # noqa: BLE001
        pass
    try:
        rag = _rag_pages(path)
    except Exception:  # noqa: BLE001
        pass
    if layout and any(t.strip() for _, t in layout):
        if rag and len(rag) == len(layout):
            return [(p, _swap_tables(t, r[1])) for (p, t), r in zip(layout, rag)]
        return layout
    if rag and any(t.strip() for _, t in rag):
        return rag
    import pymupdf
    with pymupdf.open(str(path)) as doc:
        return [(i + 1, pg.get_text("text")) for i, pg in enumerate(doc)]


def _strip_running(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Убрать номера страниц и колонтитулы (одинаковые короткие строки на большинстве страниц)."""
    n = len(pages)
    cnt: Counter[str] = Counter()
    for _, t in pages:
        seen = {ln.strip() for ln in t.splitlines() if 0 < len(ln.strip()) < 90}
        cnt.update(seen)
    running = {s for s, c in cnt.items() if n >= 3 and c >= max(3, 0.6 * n)
               and not s.startswith(("|", "#", "*", "-", "```"))}
    out = []
    for p, t in pages:
        keep = [ln for ln in t.splitlines()
                if ln.strip() not in running and not _PAGE_NO.match(ln.strip() or "x")]
        out.append((p, "\n".join(keep)))
    return out


def clean_text(t: str) -> str:
    t = t.replace(chr(0x2010), "-").replace(chr(0x2011), "-").replace(" ", " ")
    t = re.sub(r"/[ 	]*<br>[ 	]*", "/", t)
    t = t.replace("­\n", "").replace("­", "-")      # мягкий перенос: в конце строки — склеить, иначе дефис
    t = re.sub(r"(?<=[а-яёa-z])-[ \t]*\n(?=[а-яёa-z])", "", t)  # перенос слова «…сло-\nво»
    # «заголовок» со строчной буквы («или», «и далее…») — ложное срабатывание определения уровней
    t = re.sub(r"(?m)^#{1,6}[ 	]+(?=\**[а-яёa-z])", "", t)
    return t


def pdf_pages(path: Path) -> list[tuple[int, str]]:
    pages = _extract(path)
    pages = _strip_running(pages)
    return [(p, clean_text(t)) for p, t in pages]
