import json
from pathlib import Path

from reqcheck.clarify import guard
from reqcheck.models import ReportData, Severity
from reqcheck.report import render_html, render_markdown

FIX = Path(__file__).parent / "fixtures" / "report_data.json"


def _data(n_doubtful=2):
    d = ReportData.model_validate(json.loads(FIX.read_text(encoding="utf-8")))
    for f in (d.findings[-n_doubtful:] if n_doubtful else []):
        f.doubtful = True
    return d


def test_doubtful_findings_go_to_appendix_not_counts():
    d = _data()
    doubt = [f for f in d.findings if f.doubtful]
    md, html = render_markdown(d), render_html(d)
    assert "Приложение А. Замечания под сомнением" in md and 'id="doubtful"' in html
    for f in doubt:
        assert f.fid in md.split("Приложение А. Замечания под сомнением", 1)[1]
    sure = len(d.findings) - len(doubt)
    assert f"найдено замечаний: {sure}" in md
    assert f"ещё {len(doubt)} замечаний под сомнением" in md
    assert "Critic сомневается" in md


def test_no_appendix_without_doubtful():
    d = _data(0)
    assert "под сомнением" not in render_markdown(d)


def test_guard_ignores_doubtful_when_pipeline_filters_them():
    d = _data(0)
    crit = [f for f in d.findings if f.severity == Severity.critical]
    for f in crit:
        f.doubtful = True
    sure = [f for f in d.findings if not f.doubtful]
    dev_ok, _, blockers, _ = guard(sure)
    assert dev_ok and not blockers
