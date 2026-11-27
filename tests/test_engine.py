"""Тесты LLM-стадий (review / set / critic / clarify) на FakeLLM. Владелец: A7."""
from __future__ import annotations

import re

import pytest

from reqcheck import clarify, critic, review, segment, setreview
from reqcheck.engine import prompting
from reqcheck.engine.terms import TermIndex, candidate_groups, render_groups, stem
from reqcheck.llm import FakeLLM
from reqcheck.models import (Criterion, Finding, FindingSource, ReqKind, Requirement, SetSummary,
                             Severity)
from reqcheck.schemas import (CriticResult, CriticVerdict, FindingDraft, ScenarioCheck)
from tests.helpers_blocks import doc_from_md, load_input

PROMPTS = ["review_requirement", "set_contradictions", "set_scenarios", "set_completeness",
           "set_summary", "critic", "clarify", "apply_answers", "verdict"]

MINI = """# Онлайн-депозит

## Функциональные требования
* **Д-1.** Сумма вклада от 50 000 до 10 000 000 ₸ включительно.
* **Д-2.** Проценты начисляются ежедневно по ставке 14% годовых.
* **Д-3.** Досрочное расторжение возможно в любой день.

## Нефункциональные требования
Расчёт процентов выполняется быстро.

## API
| Parameter | Format | Logic | Description |
| :--- | :--- | :--- | :--- |
| `depositAmount` | integer | 100–5 000 000 | Сумма вклада в тиынах |
| `rate` | string | 0–100 | Ставка в процентах |

## Критерии приемки
| Исходные условия | Действие | Ожидаемый результат |
| :--- | :--- | :--- |
| Вклад 60 000 ₸ | Открыть вклад | Вклад открыт, ставка 12% |
"""


@pytest.fixture(scope="module")
def doc():
    return load_input()


@pytest.fixture(scope="module")
def reqs(doc):
    return segment.segment(doc, FakeLLM())


@pytest.fixture(scope="module")
def mini():
    d = doc_from_md(MINI, name="mini.md")
    rows = [b for b in d.blocks if b.kind.value in ("list_item", "text", "table_row")]
    kinds = {"Д-1": ReqKind.functional, "Д-2": ReqKind.functional, "Д-3": ReqKind.functional}
    reqs = []
    for i, b in enumerate(rows, 1):
        m = re.match(r"(Д-\d)", b.text)
        sec = b.section_path[-1] if b.section_path else ""
        kind = (kinds.get(m.group(1)) if m else None) or (
            ReqKind.nonfunctional if "Нефункц" in sec else ReqKind.api if sec == "API"
            else ReqKind.acceptance if "приемки" in sec else ReqKind.business)
        reqs.append(Requirement(rid=f"R-{i:02d}", doc_id=m.group(1) if m else None, kind=kind,
                                summary=b.text[:60], quote=b.text, block_ids=[b.id], location=b.locator()))
    return d, reqs


# -------------------------------------------------------------- rendering ---
def _ctx_for(name, doc, reqs):
    common = dict(doc_name=doc.name, requirements=prompting.req_blocks(reqs[:3]))
    extra = {
        "review_requirement": dict(criteria=prompting.criteria_text(prompting.REQ_CRITERIA, language=True),
                                   context="", known_findings=""),
        "set_contradictions": dict(criteria=prompting.criteria_text(["set_consistent"], xref=["§4.3.4"]),
                                   candidates=render_groups(candidate_groups(reqs)), known_findings=""),
        "set_scenarios": dict(acceptance="", rules=prompting.req_blocks(reqs[:2]), context=""),
        "set_completeness": dict(criteria="x", fintech_checks=prompting.fintech_checks(doc),
                                 outline=prompting.outline(doc, reqs), known_findings=""),
        "set_summary": dict(criteria="x", outline="", findings="", stats=prompting.stats(reqs, [])),
        "critic": dict(candidates=""),
        "clarify": dict(max_questions=15, findings="- D-001 | critical | x"),
        "apply_answers": dict(findings="", answers=""),
        "verdict": dict(stats="", findings="", set_summary="", questions=""),
    }[name]
    ctx = {**common, **extra}
    if name in ("set_scenarios",):
        ctx.pop("requirements")
    if name == "verdict":
        ctx.pop("requirements")
    return ctx


@pytest.mark.parametrize("name", PROMPTS)
def test_render_every_prompt(name, doc, reqs):
    system, user = prompting.render(name, **_ctx_for(name, doc, reqs))
    assert user.startswith("## ДАННЫЕ\n")
    assert "## ДАННЫЕ" not in system
    assert system.startswith("# Общие правила reqcheck")
    assert doc.name in user
    assert "{" not in re.sub(r"\{\{|\}\}|\{\"|\{'", "", user.split("\n", 2)[1])  # плейсхолдеры подставлены
    # пустые значения → «нет»
    if name in ("review_requirement", "set_contradictions", "set_completeness"):
        assert "\nнет" in user


def test_requirement_block_format(reqs):
    r = next(x for x in reqs if x.doc_id)
    s = prompting.req_block(r)
    assert s.startswith(f"[{r.rid}] doc_id: {r.doc_id} | kind: {r.kind.value} | место: ")
    assert f"Текст: «{r.quote}»" in s


def test_criteria_compact_and_full():
    full = prompting.criteria_text(["unambiguous"], language=True)
    compact = prompting.criteria_text(["unambiguous"], compact=True)
    assert "Типичные дефекты" in full and "Типичные дефекты" not in compact
    assert full.startswith("### unambiguous — Однозначность")
    assert "§5.2.7" in full
    assert "Критичность: critical" in compact
    assert len(prompting.scope_criteria("set")) == 9


def test_fintech_checks_ranked_and_capped(doc):
    lines = prompting.fintech_checks(doc, cap=10).splitlines()
    assert 1 <= len(lines) <= 10
    assert all(re.match(r"- \[FT-[A-Z]+-\d+\] \(", x) for x in lines)


def test_outline(doc, reqs):
    out = prompting.outline(doc, reqs)
    assert "требований:" in out
    assert out.startswith("- ")


# ------------------------------------------------------ draft → finding ---
def _draft(**kw):
    base = dict(rids=["R-01"], criterion="unambiguous", severity="major", title="t", quote="q",
                explanation="e", recommendation="r", confidence=0.8)
    base.update(kw)
    return FindingDraft.model_validate(base)


def test_draft_to_finding_grounded(doc, reqs):
    r = next(x for x in reqs if x.kind == ReqKind.functional)
    frag = " ".join(r.quote.split()[2:8])
    f = prompting.draft_to_finding(doc, reqs, _draft(rids=[r.rid, "R-999"], quote=frag),
                                   source=FindingSource.llm_requirement, scope="requirement")
    assert f.grounded and f.rids == [r.rid] and f.location
    assert f.source == FindingSource.llm_requirement


def test_draft_quote_repair(doc, reqs):
    r = next(x for x in reqs if x.kind == ReqKind.functional)
    words = r.quote.split()
    garbled = " ".join(words[1:9]).replace("о", "а", 1) + " ВЫДУМАННЫЙ ХВОСТ ТЕКСТА"
    f = prompting.draft_to_finding(doc, reqs, _draft(rids=[r.rid], quote=garbled),
                                   source=FindingSource.llm_requirement, scope="requirement")
    assert f.grounded
    assert f.quote != garbled and f.quote in r.quote


def test_draft_not_grounded_kept_marked(doc, reqs):
    f = prompting.draft_to_finding(doc, reqs, _draft(rids=["R-01"], quote="совершенно посторонний текст про ипотеку и страхование"),
                                   source=FindingSource.llm_set, scope="set")
    assert not f.grounded
    assert f.rids == ["R-01"]


# ------------------------------------------------------- contradictions ---
def test_contradiction_groups_generic(mini):
    d, reqs = mini
    groups = candidate_groups(reqs)
    rid = {r.doc_id: r.rid for r in reqs if r.doc_id}
    api_amount = next(r.rid for r in reqs if "depositAmount" in r.quote)
    ac = next(r.rid for r in reqs if r.kind == ReqKind.acceptance)
    # сумма вклада: бизнес-правило ↔ API
    assert any({rid["Д-1"], api_amount} <= set(g.rids) for g in groups), render_groups(groups)
    # ставка: правило ↔ критерий приёмки
    assert any({rid["Д-2"], ac} <= set(g.rids) for g in groups), render_groups(groups)
    assert all(2 <= len(g.rids) <= 6 for g in groups)
    assert render_groups(groups).startswith("G-1 (общие термины: ")


def test_stem_and_common_terms(reqs):
    assert stem("порога") == stem("порог")
    ti = TermIndex(reqs)
    assert all(not k.startswith("#") for k in ti.common)


# ------------------------------------------------------------- critic ---
def _f(rids, crit, quote, src=FindingSource.llm_requirement, conf=0.7, sev=Severity.major, expl="пояснение"):
    return Finding(rids=rids, criterion=crit, severity=sev, title=f"{crit.value} {quote[:10]}", quote=quote,
                   explanation=expl, recommendation="r", source=src, confidence=conf)


def test_pre_dedupe_merges_family_overlap():
    a = _f(["R-03"], Criterion.unambiguous, "быстро", src=FindingSource.linter, conf=0.6)
    b = _f(["R-03", "R-04"], Criterion.verifiable, "Расчёт выполняется быстро", conf=0.8,
           expl="Слово «быстро» не задаёт срок, тест нельзя написать.")
    c = _f(["R-03"], Criterion.appropriate, "Расчёт выполняется быстро")   # другая семья — не дубль
    d = _f(["R-09"], Criterion.unambiguous, "быстро")                       # другие R — не дубль
    out = critic.pre_dedupe([a, b, c, d])
    assert len(out) == 3
    merged = out[0]
    assert merged.source == FindingSource.llm_requirement and merged.rids == ["R-03", "R-04"]


def test_apply_verdicts_dup_and_drop():
    batch = [_f(["R-01"], Criterion.unambiguous, "aaaaaa1"), _f(["R-02"], Criterion.verifiable, "bbbbbb2"),
             _f(["R-03"], Criterion.complete, "cccccc3"),
             _f(["R-04"], Criterion.conforming, "dddddd4", src=FindingSource.linter, conf=0.95)]
    res = CriticResult(verdicts=[
        CriticVerdict(index=0, keep=False, duplicate_of=1, severity="major", confidence=0.8, reason="дубль"),
        CriticVerdict(index=1, keep=True, severity="critical", confidence=0.9, reason="ok"),
        CriticVerdict(index=2, keep=False, severity="minor", confidence=0.2, reason="нет дефекта"),
        CriticVerdict(index=3, keep=False, severity="minor", confidence=0.3, reason="спорно"),
    ])
    out = critic.apply_verdicts(batch, res)
    # дубль слит; отклонённая находка с подтверждённой цитатой не удалена, а помечена doubtful
    assert [f.quote for f in out] == ["bbbbbb2", "cccccc3", "dddddd4"]
    assert out[0].severity == Severity.critical and out[0].verified and out[0].rids == ["R-02", "R-01"]
    assert not out[0].doubtful
    assert out[1].doubtful and out[1].severity == Severity.minor
    assert not out[2].verified and not out[2].doubtful   # защищённый линтер остался, но не подтверждён


def test_verify_drops_ungrounded_unconfirmed(doc, reqs):
    ok = _f([reqs[5].rid], Criterion.unambiguous, reqs[5].quote[:40])
    bad = _f([reqs[5].rid], Criterion.complete, "нет такого текста")
    bad.grounded = False
    out = critic.verify(doc, reqs, [ok, bad], FakeLLM())
    assert [f.quote for f in out] == [ok.quote]


def test_verify_with_llm_handler(doc, reqs):
    fs = [_f([reqs[5].rid], Criterion.unambiguous, reqs[5].quote[:30]),
          _f([reqs[6].rid], Criterion.complete, reqs[6].quote[:30])]

    def h(system, user, schema):
        n = len(re.findall(r"^\[#\d+\]", user, re.M))
        return {"verdicts": [{"index": i, "keep": i == 0, "duplicate_of": None, "severity": "critical",
                              "confidence": 0.9, "reason": "r"} for i in range(n)]}
    out = critic.verify(doc, reqs, fs, FakeLLM({"critic": h}))
    sure = [f for f in out if not f.doubtful]
    assert len(sure) == 1 and sure[0].verified and sure[0].severity == Severity.critical
    assert len(out) == 2 and out[1].doubtful


# ------------------------------------------------------------ scenarios ---
def _check(expl, consistent=False):
    return ScenarioCheck(ac_rid="R-15", rule_rids=["R-08", "R-77"], expected_by_rules="50 000 ≥ 50 000 → push",
                         stated_expected="один push", consistent=consistent, explanation=expl,
                         quote_ac="Итог 50 000 ₸ и один push", quote_rule="равной порогу или выше")


@pytest.mark.parametrize("marker,crit,sev", [
    ("ПРОТИВОРЕЧИЕ:", Criterion.set_consistent, Severity.critical),
    ("НЕОДНОЗНАЧНО:", Criterion.unambiguous, Severity.major),
    ("НЕ ХВАТАЕТ ДАННЫХ:", Criterion.complete, Severity.major),
    ("НЕПРОВЕРЯЕМО:", Criterion.verifiable, Severity.major),
])
def test_scenario_marker_mapping(doc, reqs, marker, crit, sev):
    f = setreview.scenario_finding(doc, reqs, _check(f"{marker} расчёт расходится."))
    assert f is not None and f.criterion == crit and f.severity == sev
    assert f.source == FindingSource.scenario and f.scope == "set"
    assert f.rids == ["R-15", "R-08"]
    assert marker not in f.explanation and "Правило" in f.explanation
    assert f.recommendation


def test_scenario_agreed_skipped(doc, reqs):
    assert setreview.scenario_finding(doc, reqs, _check("СОГЛАСОВАНО: всё сходится", True)) is None


def test_missing_scenarios_aggregated(doc, reqs):
    f = setreview.missing_finding(doc, reqs, ["R-06: граница 1 000 ₸ — нет критерия",
                                              "R-07: возврат покупки — нет критерия",
                                              "R-09: смена порога — нет критерия"])
    assert f.criterion == Criterion.set_validatable and f.severity == Severity.major
    assert f.grounded and set(f.rids) == {"R-06", "R-07", "R-09"}


# --------------------------------------------------------------- verdict ---
def test_decide_guard_overrides_llm(reqs):
    crit = _f(["R-06"], Criterion.set_consistent, "x", sev=Severity.critical)
    crit.fid = "D-001"
    llm = FakeLLM({"verdict": lambda s, u, sc: {"ready_for_development": True, "ready_for_testing": True,
                                                  "rationale": "Всё хорошо.", "top_fixes": ["D-001: исправить"]}})
    v = clarify.decide(reqs, [crit], SetSummary(), llm)
    assert not v.ready_for_development and not v.ready_for_testing
    assert "D-001" in v.rationale


def test_decide_guard_scenario_contradiction(reqs):
    f = _f(["R-15", "R-08"], Criterion.set_consistent, "x", src=FindingSource.scenario, sev=Severity.major)
    f.fid = "D-001"
    llm = FakeLLM({"verdict": lambda s, u, sc: {"ready_for_development": True, "ready_for_testing": True,
                                                  "rationale": "ok", "top_fixes": []}})
    v = clarify.decide(reqs, [f], SetSummary(), llm)
    assert v.ready_for_development and not v.ready_for_testing


def test_decide_fallback_on_error(reqs):
    def boom(*a):
        raise RuntimeError("503")
    f = _f(["R-06"], Criterion.verifiable, "x", sev=Severity.critical)
    f.fid = "D-001"
    v = clarify.decide(reqs, [f], SetSummary(), FakeLLM({"verdict": boom}))
    assert not v.ready_for_development and not v.ready_for_testing and v.top_fixes


# --------------------------------------------------------- full pipeline ---
def _handlers(doc_text_quote):
    def review_h(system, user, schema):
        m = re.search(r"^\[(R-\d+)\][^\n]*\nТекст: «(.+?)»", user, re.M | re.S)
        if not m:
            return {"findings": []}
        rid, text = m.group(1), m.group(2)
        return {"findings": [{"rids": [rid], "criterion": "unambiguous", "severity": "major",
                              "title": "Тестовая находка", "quote": " ".join(text.split()[:6]),
                              "explanation": "Пояснение со ссылкой на текст.", "recommendation": "Заменить на: «…».",
                              "confidence": 0.8}]}

    def contr_h(system, user, schema):
        return {"findings": [{"rids": ["R-34", "R-40"], "criterion": "set_consistent", "severity": "critical",
                              "title": "Тип amount расходится", "quote": "Порог в тенге",
                              "explanation": "R-40 указывает «string».", "recommendation": "Выбрать integer.",
                              "confidence": 0.9}]}

    def scen_h(system, user, schema):
        return {"checks": [{"ac_rid": "R-17", "rule_rids": ["R-07"], "expected_by_rules": "переводы исключаются",
                            "stated_expected": "19 900", "consistent": True,
                            "explanation": "СОГЛАСОВАНО: перевод не учитывается", "quote_ac": "Перевести 500 ₸",
                            "quote_rule": "Переводы и снятие наличных исключаются."},
                           {"ac_rid": "R-16", "rule_rids": ["R-07"], "expected_by_rules": "январь",
                            "stated_expected": "февраль", "consistent": False,
                            "explanation": "ПРОТИВОРЕЧИЕ: месяц по дате покупки — январь",
                            "quote_ac": "Февральский счётчик вырос на 4 000 ₸",
                            "quote_rule": "Месяц определяется по дате совершения покупки в Алматы."}],
                "missing_scenarios": ["R-06: граница 1 000 ₸"]}

    def compl_h(system, user, schema):
        return {"findings": [{"rids": ["R-08"], "criterion": "set_complete", "severity": "major",
                              "title": "Не задан часовой пояс отправки", "quote": "клиент получает push",
                              "explanation": "[FT-TIME-01] документ не определяет …", "recommendation": "Добавить.",
                              "confidence": 0.7}]}

    def critic_h(system, user, schema):
        n = len(re.findall(r"^\[#\d+\]", user, re.M))
        return {"verdicts": [{"index": i, "keep": True, "duplicate_of": None, "severity": "major",
                              "confidence": 0.8, "reason": "ok"} for i in range(n)]}

    def summary_h(system, user, schema):
        return {"assessments": [{"criterion": c, "status": "частично", "comment": "см. D-001"}
                                for c in prompting.SET_CRITERIA], "notes": ["Наблюдение."]}

    def clarify_h(system, user, schema):
        return {"questions": [{"fids": ["D-001"], "rids": ["R-34"], "question": "Какой тип у amount?",
                               "why": "Блокирует API.", "options": ["integer", "string"]}]}

    def verdict_h(system, user, schema):
        assert "Какой тип у amount?" in user      # вопросы доходят до verdict
        return {"ready_for_development": True, "ready_for_testing": True, "rationale": "Можно.",
                "top_fixes": ["D-001: тип"]}

    return {"review": review_h, "set.contradictions": contr_h, "set.scenarios": scen_h,
            "set.completeness": compl_h, "critic": critic_h, "set.summary": summary_h,
            "clarify": clarify_h, "verdict": verdict_h}


def test_full_pipeline_fake(tmp_path):
    from reqcheck.config import Settings
    from reqcheck.pipeline import run
    from reqcheck.report import render_markdown
    s = Settings(provider="fake", cache_dir=tmp_path, use_cache=False)
    llm = FakeLLM(_handlers(None))
    data = run("docs/requirements.md", s, llm=llm)
    assert data.requirements and data.findings
    tags = llm.stats.by_tag
    for t in ("review", "set.contradictions", "set.scenarios", "set.completeness", "critic",
              "set.summary", "clarify", "verdict"):
        assert tags.get(t), t
    srcs = {f.source for f in data.findings}
    assert {FindingSource.llm_requirement, FindingSource.scenario, FindingSource.fintech, FindingSource.linter} <= srcs
    assert all(f.fid for f in data.findings)
    assert all(f.grounded or f.verified for f in data.findings)
    assert len(data.set_summary.criteria) == 9
    assert data.questions and data.questions[0].qid == "Q-01"
    assert not data.verdict.ready_for_testing      # guard: критерий приёмки противоречит правилу
    md = render_markdown(data)
    assert "D-001" in md


def test_full_pipeline_empty_llm_degrades(tmp_path):
    from reqcheck.config import Settings
    from reqcheck.pipeline import run
    data = run("docs/requirements.md", Settings(provider="fake", use_cache=False), llm=FakeLLM())
    assert data.verdict.rationale
    assert len(data.set_summary.criteria) == 9


def test_aggregate_repeats_same_defect_in_neighbour_items():
    mk = lambda rid, code: Finding(rids=[rid], criterion=Criterion.correct, severity=Severity.major,
                                   title=f"Код {code} описан в разделе кода 200", quote=f"{code}. Ответ",
                                   location="Часть B › Статусы (строки 174–176)", explanation="e",
                                   recommendation="r", source=FindingSource.llm_requirement)
    other = mk("R-50", 401)
    other.location = "Часть B › Другое (строка 1)"
    out = critic.aggregate_repeats([mk("R-45", 401), mk("R-46", 403), mk("R-47", 500), other])
    assert len(out) == 2
    assert out[0].rids == ["R-45", "R-46", "R-47"] and "(+2" in out[0].title


def test_missing_scenarios_reach_summary(doc, reqs):
    setreview._remember_missing(reqs, ["R-07: возврат — нет критерия приёмки."])
    s = setreview.summarize(doc, reqs, [], FakeLLM())
    assert any("R-07: возврат" in n for n in s.notes)
