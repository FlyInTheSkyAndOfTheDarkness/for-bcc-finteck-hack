"""Стадия 7. Уточняющие вопросы автору ТЗ и итоговый вердикт. Владелец: A7.

make_questions (clarify) — вопросы-решения по проверенным находкам, ≤ 15, детерминированы кэшем;
apply_answers (answers)  — пересмотр находок по ответам: keep/drop/downgrade/update + новые находки;
decide (verdict)         — итог LLM + правило-страховка: critical → разработку не начинать;
                           critical по set_consistent/verifiable или противоречащий критерий приёмки →
                           тестирование не начинать.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional, Sequence

from reqcheck.engine.prompting import (call, draft_to_finding, finding_lines, req_blocks, rid_map,
                                       stats)
from reqcheck.grounding import GROUND_THRESHOLD, find_quote
from reqcheck.llm import LLMClient
from reqcheck.models import (Criterion, Document, Finding, FindingSource, Question, Requirement,
                             SetSummary, Severity, Verdict)
from reqcheck.schemas import AnswerEffects, QuestionDrafts, VerdictDraft

log = logging.getLogger(__name__)

MAX_QUESTIONS = 15
# Вариант «оставить как есть» не решает дефект: убираем, если остаётся ≥ 2 вариантов.
_STATUS_QUO = re.compile(r"^\s*(?:оставить|сохранить текущ|не (?:менять|изменять)|на усмотрение)", re.I)
MAX_DETAILED = 40

# pipeline.decide() не получает документ и вопросы; make_questions запоминает их для того же
# прогона (тот же поток, тот же список reqs), чтобы verdict видел вопросы и ответы.
_STATE = threading.local()


def _remember(doc: Document, reqs: Sequence[Requirement], questions: list[Question]) -> None:
    _STATE.reqs_id, _STATE.doc_name, _STATE.questions = id(reqs), doc.name, questions


def _recall(reqs: Sequence[Requirement]) -> tuple[str, list[Question]]:
    if getattr(_STATE, "reqs_id", None) == id(reqs):
        return _STATE.doc_name, _STATE.questions
    return "", []


def _referenced(reqs: Sequence[Requirement], findings: Sequence[Finding]) -> list[Requirement]:
    used = {r for f in findings for r in f.rids}
    return [r for r in reqs if r.rid in used]


def _clean_list(xs: Sequence[str], allowed: set) -> list[str]:
    out = []
    for x in xs or []:
        x = (x or "").strip()
        if x in allowed and x not in out:
            out.append(x)
    return out


def make_questions(doc: Document, reqs: list[Requirement], findings: list[Finding],
                   llm: LLMClient, max_questions: int = MAX_QUESTIONS) -> list[Question]:
    _remember(doc, reqs, [])
    if not findings:
        return []
    detailed = [f for f in findings if f.severity != Severity.minor]
    if len(detailed) > MAX_DETAILED:
        detailed = [f for f in findings if f.severity == Severity.critical] + \
                   [f for f in findings if f.severity == Severity.major][:MAX_DETAILED]
    ids = {f.fid for f in detailed}
    lines = finding_lines(findings, details_for={Severity.critical, Severity.major}) \
        if len(findings) <= MAX_DETAILED else \
        "\n".join(finding_lines([f], details=f.fid in ids) for f in findings)
    try:
        out = call(llm, "clarify", QuestionDrafts, "clarify", doc_name=doc.name,
                   max_questions=max_questions, findings=lines,
                   requirements=req_blocks(_referenced(reqs, findings)))
    except Exception as e:  # noqa: BLE001
        log.warning("clarify упал: %s", e)
        return []
    fids = {f.fid for f in findings}
    rids = {r.rid for r in reqs}
    by_fid = {f.fid: f for f in findings}
    qs: list[Question] = []
    seen = set()
    for d in out.questions:
        text = " ".join((d.question or "").split())
        if not text or text in seen:
            continue
        qf = _clean_list(d.fids, fids)
        qr = _clean_list(d.rids, rids)
        if not qf and not qr:
            continue
        for fid in qf:                     # rids вопроса ⊇ rids его находок
            for r in by_fid[fid].rids:
                if r not in qr:
                    qr.append(r)
        opts = [" ".join(o.split()) for o in d.options if o and o.strip()]
        keep = [o for o in opts if not _STATUS_QUO.match(o)]
        opts = (keep if len(keep) >= 2 else opts)[:4]
        seen.add(text)
        qs.append(Question(fids=qf, rids=qr, question=text, why=" ".join(d.why.split()), options=opts))
        if len(qs) >= max_questions:
            break
    _remember(doc, reqs, qs)
    return qs


def _answers_lines(questions: Sequence[Question]) -> str:
    out = []
    for q in questions:
        out.append(f"- {q.qid or '?'} | {', '.join(q.fids) or '—'} | {', '.join(q.rids) or '—'} | {q.question}")
        if q.options:
            out.append(f"  Варианты: {' / '.join(q.options)}")
        out.append(f"  Ответ: {q.answer if q.answer else 'нет ответа'}")
    return "\n".join(out)


def apply_answers(doc: Document, reqs: list[Requirement], findings: list[Finding],
                  questions: list[Question], llm: LLMClient) -> list[Finding]:
    """Пересмотреть находки с учётом ответов (Question.answer)."""
    answered = [q for q in questions if q.answer]
    if not answered:
        return findings
    try:
        out = call(llm, "apply_answers", AnswerEffects, "answers", doc_name=doc.name,
                   findings=finding_lines(findings, details=True), answers=_answers_lines(questions),
                   requirements=req_blocks(_referenced(reqs, findings)))
    except Exception as e:  # noqa: BLE001
        log.warning("answers упал: %s", e)
        return findings
    by_fid = {f.fid: f for f in findings}
    drop = set()
    for e in out.effects:
        f = by_fid.get(e.fid)
        if f is None:
            continue
        act = (e.action or "").strip().lower()
        note = " ".join((e.note or "").split())
        if act == "drop":
            drop.add(e.fid)
        elif act in ("downgrade", "update"):
            if e.severity is not None:
                f.severity = e.severity
            if note:
                f.recommendation = (f.recommendation.rstrip() + f" По ответу автора: {note}").strip()
        elif act == "keep" and note:
            f.explanation = (f.explanation.rstrip() + f" (Ответ автора не снимает дефект: {note})").strip()
    result = [f for f in findings if f.fid not in drop]
    for d in out.new_findings:
        nf = draft_to_finding(doc, reqs, d, source=FindingSource.llm_set, scope="set")
        if nf.grounded:
            nf.verified = True
            result.append(nf)
    return result


# ------------------------------------------------------------------- verdict ---
def _ac_contradicted(findings: Sequence[Finding]) -> list[Finding]:
    return [f for f in findings if f.source == FindingSource.scenario
            and f.criterion == Criterion.set_consistent]


def guard(findings: Sequence[Finding]) -> tuple[bool, bool, list[Finding], list[Finding]]:
    """Правило-страховка: (dev_allowed, test_allowed, блокеры разработки, блокеры тестирования)."""
    crit = [f for f in findings if f.severity == Severity.critical]
    test_block = [f for f in crit if f.criterion in (Criterion.set_consistent, Criterion.verifiable)]
    test_block += [f for f in _ac_contradicted(findings) if f not in test_block]
    return (not crit, not test_block, crit, test_block)


def _ref(f: Finding) -> str:
    return f"{f.fid} ({', '.join(f.rids)})" if f.rids else f.fid


def _fallback(findings: Sequence[Finding]) -> Verdict:
    dev, test, crit, tb = guard(findings)
    parts = []
    parts.append("Разработку начинать нельзя: " + "; ".join(f"{_ref(f)} — {f.title}" for f in crit[:5]) + "."
                 if crit else "Критичных находок нет, разработку можно начинать.")
    parts.append("Тестирование начинать нельзя: " + "; ".join(f"{_ref(f)} — {f.title}" for f in tb[:5]) + "."
                 if tb else "Блокеров тестирования среди находок нет.")
    parts.append("Итог сформирован по правилам без LLM (модель недоступна).")
    top = [f for f in findings if f.severity == Severity.critical] + \
          [f for f in findings if f.severity == Severity.major]
    fixes = [f"{_ref(f)}: {f.recommendation.split('. ')[0].strip()}" for f in top[:7]]
    return Verdict(ready_for_development=dev, ready_for_testing=test, rationale=" ".join(parts),
                   top_fixes=fixes)


def _summary_lines(summary: SetSummary) -> str:
    out = []
    for k, v in summary.criteria.items():
        st, _, comment = v.partition(":")
        out.append(f"- {k}: {st.strip()} — {comment.strip()}")
    return "\n".join(out)


def _questions_lines(questions: Sequence[Question]) -> str:
    """Для verdict: варианты нужны только у отвеченных вопросов (короче вход сильной модели)."""
    out = []
    for q in questions:
        out.append(f"- {q.qid or '?'} | {', '.join(q.fids) or '—'} | {', '.join(q.rids) or '—'} | {q.question}")
        if q.answer and q.options:
            out.append(f"  Варианты: {' / '.join(q.options)}")
        out.append(f"  Ответ: {q.answer if q.answer else 'нет ответа'}")
    return "\n".join(out)


def decide(reqs: list[Requirement], findings: list[Finding], summary: SetSummary,
           llm: LLMClient, *, questions: Optional[list[Question]] = None,
           doc_name: str = "") -> Verdict:
    """Можно ли начинать разработку и тестирование; что исправить первым."""
    dev_ok, test_ok, crit, tb = guard(findings)
    if questions is None or not doc_name:
        dn, qs = _recall(reqs)
        doc_name = doc_name or dn
        questions = questions if questions is not None else qs
    sev_full = [f for f in findings if f.severity != Severity.minor]
    minor = [f for f in findings if f.severity == Severity.minor]
    lines = finding_lines(sev_full)
    if minor:
        lines += ("\n" if lines else "") + finding_lines(minor)
    name = doc_name or (reqs[0].location.split(" › ")[0] if reqs and reqs[0].location else "документ")
    try:
        d = call(llm, "verdict", VerdictDraft, "verdict", doc_name=name,
                 stats=stats(reqs, findings), findings=lines, set_summary=_summary_lines(summary),
                 questions=_questions_lines(questions or []))
    except Exception as e:  # noqa: BLE001
        log.warning("verdict упал: %s", e)
        return _fallback(findings)
    if not " ".join(d.rationale.split()):
        # модель не дала обоснования → флаги и текст берём из правил целиком, чтобы они не противоречили
        fb = _fallback(findings)
        fb.rationale = fb.rationale.replace("(модель недоступна)", "(модель не дала обоснования)")
        return fb
    dev, test = d.ready_for_development, d.ready_for_testing
    notes = []
    if dev and not dev_ok:
        dev = False
        notes.append("Правило отчёта: при critical-находках (" + ", ".join(f.fid for f in crit[:6])
                     + ") разработку не начинают.")
    if test and not test_ok:
        test = False
        notes.append("Правило отчёта: critical-противоречия или непроверяемость, либо критерий приёмки, "
                     "противоречащий правилам (" + ", ".join(f.fid for f in tb[:6]) + "), блокируют тестирование.")
    fixes = [" ".join(x.split()) for x in d.top_fixes if x and x.strip()][:7]
    if not fixes:
        fixes = _fallback(findings).top_fixes
    rationale = " ".join(d.rationale.split()) or _fallback(findings).rationale.replace(
        "Итог сформирован по правилам без LLM (модель недоступна).", "").strip()
    if notes:
        rationale = (rationale + " " + " ".join(notes)).strip()
    return Verdict(ready_for_development=dev, ready_for_testing=test, rationale=rationale, top_fixes=fixes)
