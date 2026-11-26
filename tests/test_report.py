"""Тесты рендера отчёта (A6)."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from reqcheck.models import ReportData, Verdict, RunPassport
from reqcheck.report import render_html, render_markdown

FIX = Path(__file__).parent / "fixtures" / "report_data.json"

SECTIONS = ["Паспорт прогона", "Нарезка требований", "Проверка каждого требования",
            "Проверка набора требований целиком", "Сводка", "Уточняющие вопросы", "Итог",
            "Методика"]


@pytest.fixture(scope="module")
def data() -> ReportData:
    return ReportData.model_validate(json.loads(FIX.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def md(data) -> str:
    return render_markdown(data)


@pytest.fixture(scope="module")
def html(data) -> str:
    return render_html(data)


@pytest.mark.parametrize("section", SECTIONS)
def test_sections_present(md, html, section):
    assert section in md
    assert section in html


def test_title_and_summary(md, html, data):
    assert md.startswith("# Отчёт о проверке требований: " + data.passport.doc_name)
    assert "разрабатывать и тестировать по документу в нынешнем виде нельзя" in md


def test_passport_fields(md, data):
    p = data.passport
    for v in (p.model, p.agent_version, p.prompt_version, p.doc_sha256[:12], p.run_at):
        assert v in md


def test_findings_quote_location_id(md, html, data):
    import html as h
    for f in data.findings:
        assert f.fid in md and f"id=\"{f.fid}\"" in md and f"id=\"{f.fid}\"" in html
        assert f.quote in md
        assert f.location in md
        assert h.escape(f.quote) in html
        assert h.escape(f.location) in html


def test_requirement_table_one_row_per_req(md, data):
    sec = md.split('<a id="slicing"></a>')[1].split('<a id="requirements"></a>')[0]
    rows = [ln for ln in sec.splitlines() if re.match(r"\| \[R-\d+\]", ln)]
    assert len(rows) == len(data.requirements)
    assert "— (присвоен агентом)" in sec


def test_pipes_do_not_break_tables(md):
    sec = md.split('<a id="slicing"></a>')[1].split('<a id="requirements"></a>')[0]
    row = next(ln for ln in sec.splitlines() if "[R-02]" in ln)
    # 6 столбцов -> 7 неэкранированных разделителей
    assert len(re.findall(r"(?<!\\)\|", row)) == 7
    assert "\\|" in row


def test_newlines_in_cells_become_br(data):
    d = data.model_copy(deep=True)
    d.requirements[5].summary = "строка один" + chr(10) + "строка два"
    out = render_markdown(d)
    sec = out.split('<a id="slicing"></a>')[1].split('<a id="requirements"></a>')[0]
    row = next(ln for ln in sec.splitlines() if "[R-06]" in ln)
    assert "строка один<br>строка два" in row


def test_quote_truncated_in_table(md, data):
    long = data.model_copy(deep=True)
    long.requirements[0].quote = "слово " * 100
    out = render_markdown(long)
    sec = out.split('<a id="slicing"></a>')[1].split('<a id="requirements"></a>')[0]
    row = next(ln for ln in sec.splitlines() if "[R-01]" in ln)
    assert "…" in row and len(row) < 600
    assert ("слово " * 100).strip() in out  # полный текст ниже


def test_questions_prominent(md, html, data):
    for q in data.questions:
        assert q.qid in md and q.question in md
        assert q.qid in html
    assert "ответ не получен" in md
    assert "Принять КП-1 для всех переводов" in md
    assert "1. Всегда push-код" in md


def test_anchors_and_links(md, html):
    assert '<a id="R-01"></a>' in md and '(#D-003)' in md and '(#Q-01)' in md
    assert 'id="R-01"' in html and 'href="#D-003"' in html and 'href="#Q-01"' in html


def test_clean_requirement_compact(md):
    # все требования с замечаниями — своё; без замечаний нет в этом наборе только Ф-2.. проверим формат
    assert re.search(r"### <a id=\"R-01\"></a>❌ R-01", md)
    assert re.search(r"### <a id=\"R-08\"></a>⚠️ R-08", md)


def test_critical_groups(md):
    sec = md.split('<a id="summary"></a>')[1].split('<a id="questions"></a>')[0]
    assert "**Непроверяемые**" in sec and "**Противоречивые**" in sec and "**Неполные**" in sec


class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.ids = [], [], []
    VOID = {"meta", "link", "br", "hr", "input", "img"}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.ids.append(d["id"])
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append((tag, list(self.stack[-3:])))
        else:
            self.stack.pop()


def test_html_wellformed(html):
    p = _P()
    p.feed(html)
    assert not p.errors, p.errors[:3]
    assert not p.stack
    assert len(p.ids) == len(set(p.ids)), "дубли id"


def test_html_no_external_resources(html):
    assert not re.search(r"https?://", html)
    assert "<script src" not in html and "<link " not in html
    assert "prefers-color-scheme" in html


def test_html_escapes():
    d = ReportData.model_validate(json.loads(FIX.read_text(encoding="utf-8")))
    d.requirements[0].summary = "<script>alert(1)</script>"
    out = render_html(d)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_empty_report_renders():
    d = ReportData(
        passport=RunPassport(doc_name="Пусто", doc_path="x.md", doc_sha256="ab", doc_format="md",
                             run_at="2026-01-01T00:00:00+00:00", provider="p", model="m",
                             agent_version="v", prompt_version="1"),
        requirements=[], findings=[], questions=[],
        verdict=Verdict(ready_for_development=True, ready_for_testing=True, rationale="Нет замечаний."))
    for out in (render_markdown(d), render_html(d)):
        assert "Паспорт прогона" in out and "Уточняющих вопросов нет" in out
        assert "Критичных дефектов не найдено" in out
    assert "ни одного требования" in render_markdown(d)
    p = _P()
    p.feed(render_html(d))
    assert not p.errors


def test_no_findings_with_requirements(data):
    d = data.model_copy(deep=True)
    d.findings, d.questions = [], []
    out = render_markdown(d)
    assert "✅ Требования без замечаний (8)" in out
    assert "Замечаний уровня набора нет" in out
