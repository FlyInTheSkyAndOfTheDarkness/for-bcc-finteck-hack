"""Тесты модуля оценки eval/: gold-разметка, сопоставление, судья (FakeLLM), мутатор."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.evaluate import compute_metrics, evaluate, findings_from_dict, main, match_lexical
from eval.gold import DEFAULT_GOLD, load_gold
from eval.judge import JudgeResult, match_judge
from eval.mutate import CLEAN_SPEC, MUTANTS_DIR, OPERATORS, _sub, mutate
from eval.textnorm import norm, quote_sim
from reqcheck.llm import FakeLLM
from reqcheck.models import (Criterion, Finding, FindingSource, ReportData, RunPassport, Severity,
                             Verdict)

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "requirements.md"
GOLD = load_gold(DEFAULT_GOLD)


def _report(findings: list[Finding]) -> ReportData:
    return ReportData(
        passport=RunPassport(doc_name="test", doc_path="docs/requirements.md", doc_sha256="0",
                             doc_format="md", run_at="2026-01-01T00:00:00+05:00", provider="fake",
                             model="fake-llm", agent_version="test", prompt_version="test"),
        requirements=[], findings=findings,
        verdict=Verdict(ready_for_development=False, ready_for_testing=False, rationale="test"))


def _finding(quote: str, criterion: str, title: str = "", explanation: str = "",
             severity: str = "major", fid: str = "") -> Finding:
    return Finding(fid=fid, criterion=Criterion(criterion), severity=Severity(severity), title=title or "x",
                   quote=quote, explanation=explanation or "x", recommendation="x",
                   source=FindingSource.llm_requirement)


def _views(report: ReportData):
    return findings_from_dict(json.loads(report.model_dump_json()))


def _perfect() -> ReportData:
    return _report([_finding(g.quotes[0], g.criterion.value, g.title, g.description, g.severity.value,
                             fid=f"D-{i:03d}") for i, g in enumerate(GOLD, 1)])


# ------------------------------------------------------------------ gold ---
def test_gold_shape():
    assert 30 <= len(GOLD) <= 60
    assert len({g.id for g in GOLD}) == len(GOLD)
    assert sum(g.must_find for g in GOLD) >= 10
    assert any(g.severity == Severity.critical for g in GOLD)


def test_gold_quotes_found_in_document():
    doc = norm(DOC.read_text(encoding="utf-8"))
    missing = [(g.id, q) for g in GOLD for q in g.quotes if norm(q) not in doc]
    assert not missing, missing


# -------------------------------------------------------------- matching ---
def test_perfect_report_full_recall():
    fv = _views(_perfect())
    res = compute_metrics(fv, GOLD, match_lexical(fv, GOLD))["summary"]
    assert res["recall"] == 1.0
    assert res["must_find"]["recall"] == 1.0
    assert res["precision"] == 1.0
    assert res["severity_agreement"] == 1.0
    assert res["duplicates"] == 0


def test_empty_report_zero_recall():
    fv = _views(_report([]))
    res = compute_metrics(fv, GOLD, match_lexical(fv, GOLD))["summary"]
    assert res["recall"] == 0.0
    assert res["precision"] is None


def test_wrong_place_does_not_match():
    g01 = next(g for g in GOLD if g.id == "G-01")
    fv = _views(_report([
        # верная суть и критерий G-01, но цитата указывает на раздел UI
        _finding("Карты → Расходы → Уведомления. На экране видны переключатель", "set_consistent",
                 g01.title, g01.description),
        # слишком короткая цитата не должна совпадать с «REST type | GET»
        _finding("GET", "correct", "Изменение состояния через GET"),
    ]))
    assert match_lexical(fv, GOLD) == []


def test_criterion_gate():
    fv = _views(_report([_finding("| **REST type** | GET |", "complete", "GET")]))
    assert match_lexical(fv, GOLD) == []
    pairs = match_lexical(fv, GOLD, any_criterion=True)
    assert [p.g for p in pairs] == ["G-30"]
    fv = _views(_report([_finding("REST type | GET", "appropriate", "Сохранение через GET")]))
    assert [p.g for p in match_lexical(fv, GOLD)] == ["G-30"]


def test_paraphrased_quote_and_duplicates():
    # цитата с другой разметкой/кавычками/ё и две находки на один дефект
    fv = _views(_report([
        _finding("«Месяц определяется по дате совершения покупки в Алматы» — но «Февральский счетчик вырос на 4 000 ₸»",
                 "set_consistent", "Противоречие Ф-2 и критерия приёмки"),
        _finding("Покупка 31 января в 23:58 на 4 000 ₸", "set_consistent", "КП противоречит Ф-2 по месяцу"),
    ]))
    res = compute_metrics(fv, GOLD, match_lexical(fv, GOLD))
    hit = {g["id"] for g in res["details"]["gold"] if g["matched_by"]}
    assert hit == {"G-01"}
    assert res["summary"]["duplicates"] == 1
    assert res["summary"]["precision"] == 1.0


def test_quote_sim_normalization():
    assert quote_sim("Счётчик показывает 19 900 ₸", "| счетчик  показывает 19 900 ₸ |") == 100
    assert quote_sim("Сервис передаёт push на отправку в течение минуты после получения события.", "минуты") < 60


# ----------------------------------------------------------------- judge ---
def test_judge_with_fake_llm():
    fv = _views(_report([
        _finding("REST type | GET", "correct", fid="D-001"),
        _finding("что-то", "complete", fid="D-002"),
        _finding("200. Successful — Created", "set_consistent", fid="D-003"),
    ]))

    def handler(system, user, schema):
        assert schema is JudgeResult and "G-30" in user and "D-002" in user
        return {"links": [{"fid": "D-001", "gold_ids": ["G-30"]},
                          {"fid": "D-002", "gold_ids": []},
                          {"fid": "D-003", "gold_ids": ["G-38", "G-40", "G-999"]}]}

    llm = FakeLLM({"eval.judge": handler})
    pairs = match_judge(fv, GOLD, llm)
    assert llm.stats.calls == 1
    assert sorted((p.f, p.g) for p in pairs) == [(0, "G-30"), (2, "G-38"), (2, "G-40")]
    res = compute_metrics(fv, GOLD, pairs)["summary"]
    assert res["matched_findings"] == 2 and res["unmatched_findings"] == 1


def test_cli_single_and_compare(tmp_path):
    good, empty = tmp_path / "good" / "report.json", tmp_path / "empty" / "report.json"
    for p, rep in ((good, _perfect()), (empty, _report([]))):
        p.parent.mkdir()
        p.write_text(rep.model_dump_json(), encoding="utf-8")
    out = tmp_path / "res" / "good.md"
    assert main([str(good), "--out", str(out)]) == 0
    assert "Recall must_find" in out.read_text(encoding="utf-8")
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["summary"]["recall"] == 1.0
    cmp_out = tmp_path / "res" / "cmp.md"
    assert main(["--compare", str(good), str(empty), "--out", str(cmp_out)]) == 0
    text = cmp_out.read_text(encoding="utf-8")
    assert "good/report" in text and "empty/report" in text
    # judge через FakeLLM без обработчика: пустой ответ → recall 0, но без падения
    res = evaluate(good, judge=True, client=FakeLLM())
    assert res["summary"]["recall"] == 0.0


# --------------------------------------------------------------- mutator ---
def test_mutator_deterministic():
    clean = CLEAN_SPEC.read_text(encoding="utf-8")
    a, ga = mutate(clean, 7)
    b, gb = mutate(clean, 7)
    assert a == b and [g.model_dump() for g in ga] == [g.model_dump() for g in gb]
    c, _ = mutate(clean, 8)
    assert c != a and a != clean


@pytest.mark.parametrize("seed", range(1, 9))
def test_mutant_gold_quotes_present(seed):
    clean = CLEAN_SPEC.read_text(encoding="utf-8")
    text, gold = mutate(clean, seed)
    t, c = norm(text), norm(clean)
    assert len(gold) == 5
    for g in gold:
        assert all(norm(q) in t for q in g.quotes), g
        if g.mutation != "missing_branch":                  # удаление: цитата — оставшийся текст
            assert any(norm(q) not in c for q in g.quotes), g   # дефект действительно внесён
    assert t != c


def test_every_operator_variant_applies():
    clean = CLEAN_SPEC.read_text(encoding="utf-8")
    for op in OPERATORS:
        for v in op.variants:
            text = clean
            for old, new in v.edits:
                text = _sub(text, old, new)          # якорь ровно один раз
            assert all(norm(q) in norm(text) for q in v.quotes), (op.name, v.title)
        text, gold = mutate(clean, 1, ops=[op.name])
        assert [g.mutation for g in gold] == [op.name]


def test_ready_mutants_up_to_date():
    clean = CLEAN_SPEC.read_text(encoding="utf-8")
    covered = set()
    for seed in (1, 2):
        text, gold = mutate(clean, seed)
        assert (MUTANTS_DIR / f"{seed}.md").read_text(encoding="utf-8") == text
        assert [g.id for g in load_gold(MUTANTS_DIR / f"{seed}.gold.yaml")] == [g.id for g in gold]
        covered |= {g.mutation for g in gold}
    assert covered == {op.name for op in OPERATORS}


def test_perfect_report_on_mutant():
    gold = load_gold(MUTANTS_DIR / "1.gold.yaml")
    fv = _views(_report([_finding(g.quotes[-1], g.criterion.value, g.title, g.description, g.severity.value)
                         for g in gold]))
    assert compute_metrics(fv, gold, match_lexical(fv, gold))["summary"]["recall"] == 1.0
