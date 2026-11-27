"""Стадия set.checklist: выбор пунктов, пачки, ответ по пункту → находка."""
from __future__ import annotations

import re

from reqcheck.engine import prompting
from reqcheck.llm import FakeLLM
from reqcheck.models import Criterion, FindingSource, Severity
from reqcheck.schemas import ChecklistCheck, ChecklistStatus
from reqcheck.setreview import _checklist_call, checklist_chunks, checklist_finding
from tests.helpers_blocks import doc_from_md

MD = """# Кешбэк
## Требования
* **К-1.** Кешбэк начисляется за покупки по итогам месяца.
* **К-2.** Повторная доставка события не меняет итог.
"""


def _doc_reqs():
    from reqcheck.models import ReqKind, Requirement
    doc = doc_from_md(MD)
    reqs = []
    for i, b in enumerate([b for b in doc.blocks if b.text.startswith("**К-") or "К-" in b.text[:6]], 1):
        reqs.append(Requirement(rid=f"R-{i:02d}", doc_id=f"К-{i}", kind=ReqKind.functional,
                                summary=b.text[:40], quote=b.text, block_ids=[b.id], location=b.locator()))
    return doc, reqs


def test_items_cover_both_catalogs_and_respect_applies_when():
    doc, _ = _doc_reqs()
    ids = [it["id"] for it in prompting.checklist_items(doc)]
    assert any(i.startswith("FT-") for i in ids) and any(i.startswith("RQ-") for i in ids)
    assert "RQ-TERM-01" in ids                         # applies_when: [] — всегда
    assert "FT-SEC-03" not in ids or "get" in doc.text.lower()
    assert len(ids) == len(set(ids))


def test_chunks_balanced():
    items = [{"id": f"X-{i}"} for i in range(25)]
    sizes = [len(c) for c in checklist_chunks(items, 12)]
    assert sum(sizes) == 25 and max(sizes) - min(sizes) <= 1 and len(sizes) == 3
    assert checklist_chunks([], 12) == []


def test_finding_from_missing_and_skip_other_statuses():
    doc, reqs = _doc_reqs()
    item = {"id": "FT-TIME-01", "criterion": "unambiguous", "default_severity": "major"}
    c = ChecklistCheck(id="FT-TIME-01", status=ChecklistStatus.missing, rids=["R-01"],
                       title="Не указан часовой пояс месяца",
                       quote="Кешбэк начисляется за покупки по итогам месяца.",
                       explanation="Граница месяца неоднозначна.", recommendation="Указать UTC+5.")
    f = checklist_finding(doc, reqs, c, item)
    assert f is not None and f.grounded and f.source == FindingSource.fintech
    assert f.criterion == Criterion.unambiguous and f.severity == Severity.major
    assert f.explanation.endswith("[FT-TIME-01]")
    for st in (ChecklistStatus.covered, ChecklistStatus.known, ChecklistStatus.not_applicable):
        assert checklist_finding(doc, reqs, c.model_copy(update={"status": st}), item) is None
    assert checklist_finding(doc, reqs, c.model_copy(update={"quote": ""}), item) is None


def test_partial_without_severity_is_downgraded_and_rq_is_not_fintech():
    doc, reqs = _doc_reqs()
    item = {"id": "RQ-TERM-03", "criterion": "verifiable", "default_severity": "major"}
    c = ChecklistCheck(id="RQ-TERM-03", status=ChecklistStatus.partial, title="Не задан ключ повтора",
                       quote="Повторная доставка события не меняет итог.")
    f = checklist_finding(doc, reqs, c, item)
    assert f.severity == Severity.minor and f.source == FindingSource.llm_set


def test_call_ignores_unknown_and_duplicate_ids():
    doc, reqs = _doc_reqs()
    items = [{"id": "FT-TIME-01", "criterion": "unambiguous", "default_severity": "major",
              "question_ru": "Часовой пояс?", "why_ru": "Граница.", "area_ru": "Время"}]
    good = {"id": "FT-TIME-01", "status": "missing", "title": "Нет часового пояса",
            "quote": "Кешбэк начисляется за покупки по итогам месяца."}

    def h(system, user, schema):
        assert "[FT-TIME-01]" in user
        return {"checks": [good, dict(good, title="Дубль"), dict(good, id="FT-XXX-99")]}

    llm = FakeLLM({"set.checklist": h})
    out = _checklist_call(doc, reqs, llm, [], items)
    assert [f.title for f in out] == ["Нет часового пояса"]
    assert re.search(r"\[FT-TIME-01\]", out[0].explanation)
