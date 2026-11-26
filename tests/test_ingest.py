"""Тесты стадии ingest на трёх версиях одного документа (MD, PDF, DOCX) + HTML/TXT."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from reqcheck.ingest import load_document
from reqcheck.ingest.mdparse import normalize_inline, split_row
from reqcheck.models import BlockKind

FIX = Path(__file__).parent / "fixtures"
FORMATS = ["md", "pdf", "docx"]


@pytest.fixture(scope="module", params=FORMATS)
def doc(request):
    return load_document(FIX / f"requirements.{request.param}")


def _tables(doc):
    """Таблицы как списки строк: [(header, [rows...])] в порядке документа."""
    out = []
    for b in doc.blocks:
        if b.kind is BlockKind.table_header:
            out.append((b.cells, []))
        elif b.kind is BlockKind.table_row:
            out[-1][1].append(b.cells)
    return out


def _table_by_header(doc, first_cell):
    for h, rows in _tables(doc):
        if h and h[0] == first_cell:
            return h, rows
    raise AssertionError(f"нет таблицы с заголовком {first_cell!r}")


def test_name_and_basics(doc):
    assert doc.name == "Уведомление о расходах по карте"
    assert doc.format in FORMATS
    assert len(doc.sha256) == 64
    assert [b.id for b in doc.blocks][:2] == ["B-0001", "B-0002"]
    assert len({b.id for b in doc.blocks}) == len(doc.blocks)


def test_functional_requirements(doc):
    items = [b for b in doc.blocks if b.kind is BlockKind.list_item and re.match(r"Ф-\d\.", b.text)]
    assert [b.text[:3] for b in items] == ["Ф-1", "Ф-2", "Ф-3", "Ф-4"]
    for b in items:
        assert b.section_path[-1] == "Функциональные требования"
        assert b.part and b.part.startswith("Часть A")
    assert "1 000 до 1 000 000 ₸" in items[0].text
    # Ф-4 на PDF переходит на новую страницу: должен быть склеен
    assert items[3].text.endswith("независимо от истории предыдущей настройки.")


def test_parts(doc):
    parts = {b.part for b in doc.blocks if b.part}
    assert len(parts) == 2
    assert any(p.startswith("Часть A") for p in parts)
    assert any(p.startswith("Часть B") for p in parts)
    api = [b for b in doc.blocks if b.part and b.part.startswith("Часть B")]
    assert api and all(b.section_path[0].startswith("Часть B") for b in api)


def test_acceptance_table(doc):
    h, rows = _table_by_header(doc, "Исходные условия")
    assert len(h) == 3 and len(rows) == 3
    assert all(len(r) == 3 for r in rows)
    assert rows[1][0] == "Покупка 31 января в 23:58 на 4 000 ₸"
    row_blocks = [b for b in doc.blocks if b.kind is BlockKind.table_row and b.table_header == h]
    assert row_blocks[0].text == " | ".join(rows[0])
    assert row_blocks[0].section_path[-1] == "Критерии приемки"


def test_query_params_table(doc):
    h, rows = _table_by_header(doc, "Parameter")
    tabs = [t for t in _tables(doc) if t[0][0] == "Parameter"]
    q = next(t for t in tabs if len(t[0]) == 7)
    assert len(q[1]) == 3
    assert [r[0] for r in q[1]] == ["cardId", "amount", "enabled"]
    assert q[1][1][6] == "limit_kzt"           # обратные кавычки сняты


def test_review_table_complete(doc):
    h, rows = _table_by_header(doc, "Проверка")
    assert len(rows) == 14                     # в т.ч. строки, перешедшие на другую страницу PDF
    assert all(len(r) == 4 for r in rows)


def test_code_blocks(doc):
    code = [b for b in doc.blocks if b.kind is BlockKind.code]
    texts = [b.text for b in code]
    assert any('"result":"OK"' in t for t in texts)
    assert any("Карта не найдена" in t for t in texts)
    assert any("AlertsController" in t and "→" in t for t in texts)
    assert len(code) >= 7


def test_every_block_has_locator(doc):
    for b in doc.blocks:
        assert b.text.strip()
        assert b.line_start is not None and b.line_end is not None
        assert b.locator().strip(), b
    f4 = next(b for b in doc.blocks if b.text.startswith("Ф-4"))
    loc = f4.locator()
    assert "Часть A" in loc and "Функциональные требования" in loc
    if doc.format == "pdf":
        assert "стр." in loc and f4.page is not None
    assert all(b.page for b in doc.blocks) if doc.format == "pdf" else all(b.page is None for b in doc.blocks)


def test_document_text_consistent(doc):
    for b in doc.blocks:
        assert b.text in doc.text


def test_emphasis_and_backticks_stripped(doc):
    assert "**" not in doc.text and "`" not in doc.text
    assert "«Порог, ₸»" in doc.text and "—" in doc.text


def test_formats_agree_on_structure():
    docs = {f: load_document(FIX / f"requirements.{f}") for f in FORMATS}
    md = docs["md"]
    for f in ("pdf", "docx"):
        d = docs[f]
        for kind in (BlockKind.heading, BlockKind.list_item, BlockKind.table_row, BlockKind.code):
            a = [b.text for b in md.blocks if b.kind is kind]
            b_ = [b.text for b in d.blocks if b.kind is kind]
            if kind is BlockKind.table_row and f == "pdf":
                continue
            assert len(a) == len(b_), (f, kind, len(a), len(b_))
        # заголовки и таблицы — слово в слово
        assert [b.text for b in md.blocks if b.kind is BlockKind.heading] == \
               [b.text for b in d.blocks if b.kind is BlockKind.heading]
        # строки таблиц: строка MD находится в целевом документе (PDF: с точностью до пробелов;
        # полностью пустые строки пустой таблицы в PDF не восстанавливаются — известное ограничение)
        norm = lambda cells: re.sub(r"\s+", "", " | ".join(cells))  # noqa: E731
        got = {norm(b.cells) for b in d.blocks if b.kind is BlockKind.table_row}
        for b in md.blocks:
            if b.kind is BlockKind.table_row and any(b.cells):
                assert norm(b.cells) in got, (f, b.cells)


def test_no_text_lost_md():
    """Каждая непустая нормализованная строка тела MD покрыта каким-либо блоком."""
    p = FIX / "requirements.md"
    d = load_document(p)
    flat = " ".join(d.text.split())
    in_fence = False
    missing = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if not s or re.fullmatch(r"-{3,}", s):
            continue
        if in_fence:
            assert s in d.text, s
            continue
        if s.startswith("|"):
            cells = split_row(s)
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c) and any(cells):
                continue
            parts = [c for c in cells if c]
        else:
            s = re.sub(r"^#+\s+", "", s)
            s = re.sub(r"^(?:[*\-]|\d+\.)\s+", "", s) if not re.match(r"^\d+\.", s) else s
            parts = [normalize_inline(s)]
        for part in parts:
            if part not in flat:
                missing.append(part)
    assert not missing, missing


def test_html_and_txt(tmp_path):
    h = tmp_path / "a.html"
    h.write_text("<html><body><h1>Док</h1><h2>Часть A. X</h2><h3>Раздел</h3><ul><li>Ф-1. Текст</li></ul>"
                 "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
                 "<pre>{\"a\": 1}</pre></body></html>", encoding="utf-8")
    d = load_document(h)
    assert d.name == "Док" and d.format == "html"
    kinds = [b.kind for b in d.blocks]
    assert kinds == [BlockKind.heading] * 3 + [BlockKind.list_item,
                     BlockKind.table_header, BlockKind.table_row, BlockKind.code]
    assert d.blocks[3].section_path == ["Часть A. X", "Раздел"] and d.blocks[3].part == "Часть A. X"
    t = tmp_path / "b.txt"
    t.write_text("# Т\n\nабзац\nпродолжение\n\n- пункт\n", encoding="utf-8")
    d = load_document(t)
    assert d.format == "txt" and [b.text for b in d.blocks][1:] == ["абзац продолжение", "пункт"]
    assert d.blocks[1].line_start == 3 and d.blocks[1].line_end == 4


def test_md_line_numbers():
    d = load_document(FIX / "requirements.md")
    f1 = next(b for b in d.blocks if b.text.startswith("Ф-1."))
    src = (FIX / "requirements.md").read_text(encoding="utf-8").splitlines()
    assert "Ф-1." in src[f1.line_start - 1]
    assert f1.locator().endswith(f"(строка {f1.line_start})")
