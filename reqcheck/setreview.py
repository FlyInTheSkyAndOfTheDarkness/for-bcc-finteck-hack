"""Стадия 5. Проверка набора целиком (§5.2.6, IEEE 830 §4.3, fintech-чек-лист). Владелец: A7.

Три независимых LLM-вызова идут параллельно:
  set.contradictions — группы-кандидаты из индекса терминов (engine/terms.py) + весь набор;
  set.scenarios      — прогон критериев приёмки против правил, маркеры → критерий/критичность;
  set.completeness   — пропуски набора с предметным чек-листом (метка [FT-…] → source=fintech).
summarize (set.summary) вызывается после critic и нумерации D-NNN.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional, Sequence

from reqcheck.engine import parallel
from reqcheck.engine.prompting import (SET_CRITERIA, call, criteria_text, draft_to_finding,
                                       fintech_checks, fintech_tag, finding_lines, known_lines,
                                       outline, repair_quote, req_blocks, req_short, rid_map,
                                       scope_criteria, stats)
from reqcheck.engine.terms import TermIndex, candidate_groups, render_groups
from reqcheck.grounding import find_quote
from reqcheck.llm import LLMClient
from reqcheck.models import (BlockKind, Criterion, Document, Finding, FindingSource, ReqKind,
                             Requirement, SetSummary, Severity)
from reqcheck.schemas import FindingDrafts, ScenarioCheck, ScenarioChecks, SetSummaryDraft

log = logging.getLogger(__name__)

RULE_KINDS = {ReqKind.functional, ReqKind.api, ReqKind.data, ReqKind.nonfunctional, ReqKind.integration}
SMALL_SET = 24             # до стольких требований set.contradictions — один вызов со всеми
GROUPS_PER_CALL = 6
MAX_SHOWN = 28

MARKERS = [  # (маркер, критерий, критичность, уверенность, заголовок)
    ("ПРОТИВОРЕЧИЕ:", Criterion.set_consistent, Severity.critical, 0.85,
     "Ожидаемый результат критерия приёмки {ac} расходится с правилом {rule}"),
    ("НЕОДНОЗНАЧНО:", Criterion.unambiguous, Severity.major, 0.75,
     "Результат критерия приёмки {ac} зависит от толкования {rule}"),
    ("НЕ ХВАТАЕТ ДАННЫХ:", Criterion.complete, Severity.major, 0.75,
     "Правила не позволяют вычислить результат критерия {ac}"),
    ("НЕПРОВЕРЯЕМО:", Criterion.verifiable, Severity.major, 0.75,
     "Ожидаемый результат критерия приёмки {ac} нельзя наблюдать"),
]
AGREED = "СОГЛАСОВАНО:"


_NO_AC = re.compile(r"\s*[—–-]\s*(?:нет|отсутству\w+)\s+(?:ни одного\s+)?(?:критери\w*|сценари\w*)(?:\s+при[её]мки)?\.?$", re.I)


def _sentence(s: str) -> str:
    s = " ".join((s or "").split()).rstrip()
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?»" else s + "."


def _label(r: Optional[Requirement]) -> str:
    if r is None:
        return "—"
    return f"{r.rid} ({r.doc_id})" if r.doc_id else r.rid


# ------------------------------------------------------------ contradictions ---
def contradiction_jobs(reqs: Sequence[Requirement], max_groups: int = 12,
                       per_call: int = GROUPS_PER_CALL) -> list[tuple[list, list[Requirement]]]:
    """Разбить группы-кандидаты на вызовы: (группы, показываемые требования).

    Небольшой набор (≤ SMALL_SET требований) — один вызов со всеми требованиями. Иначе группы
    режутся по per_call, а модель видит требования своих групп и связанные с ними по терминам
    (так один вызов сильной модели укладывается в таймаут и вызовы идут параллельно).
    """
    groups = candidate_groups(reqs, max_groups=max_groups)
    body = [r for r in reqs if r.kind != ReqKind.meta]
    if len(body) <= SMALL_SET or not groups:
        return [(groups, body)]
    index = TermIndex(reqs)
    jobs = []
    for i in range(0, len(groups), per_call):
        chunk = groups[i:i + per_call]
        want: list[str] = []
        for g in chunk:
            for r in g.rids:
                if r not in want:
                    want.append(r)
        for r in list(want):
            for x in index.related(r, 1, exclude=set(want)):
                if len(want) < MAX_SHOWN:
                    want.append(x)
        shown = [r for r in reqs if r.rid in set(want)]
        jobs.append((chunk, shown))
    return jobs


def _contradictions_call(doc: Document, reqs: Sequence[Requirement], llm: LLMClient,
                         prior: Sequence[Finding], groups: list, shown: list[Requirement]) -> list[Finding]:
    rids = {r.rid for r in shown}
    kn = [f for f in prior if not f.rids or set(f.rids) & rids]
    out = call(llm, "set_contradictions", FindingDrafts, "set.contradictions",
               criteria=criteria_text(["set_consistent", "correct", "unambiguous"], language=False,
                                      xref=["§4.3.4"]),
               doc_name=doc.name, requirements=req_blocks(shown), candidates=render_groups(groups),
               known_findings=known_lines(kn))
    return [draft_to_finding(doc, reqs, d, source=FindingSource.llm_set, scope="set") for d in out.findings]


# ----------------------------------------------------------------- scenarios ---
def _context_for_scenarios(doc: Document, reqs: Sequence[Requirement]) -> str:
    parts = []
    goals = [r for r in reqs if r.kind == ReqKind.business]
    if goals:
        parts.append("Цель: " + " ".join(f"«{g.quote}»" for g in goals[:2]))
    proc = [r for r in reqs if r.kind in (ReqKind.process, ReqKind.ui)]
    if proc:
        parts.append("Процесс и интерфейс:\n" + "\n".join(f"[{r.rid}] «{r.quote[:400]}»" for r in proc[:6]))
    gloss = [b.text for b in doc.blocks if b.kind != BlockKind.heading
             and any(re.search(r"глоссари|термин|определени", h, re.I) for h in b.section_path)]
    if gloss:
        parts.append("Глоссарий: " + " ; ".join(gloss)[:800])
    return "\n".join(parts)[:3000]


def _strip_marker(text: str) -> tuple[Optional[tuple], str]:
    t = (text or "").strip()
    up = t.upper()
    for m in MARKERS:
        if up.startswith(m[0]):
            return m, t[len(m[0]):].strip()
    if up.startswith(AGREED):
        return None, t[len(AGREED):].strip()
    return (), t


def scenario_finding(doc: Document, reqs: Sequence[Requirement], c: ScenarioCheck) -> Optional[Finding]:
    """ScenarioCheck → Finding по маркеру в начале explanation (None — согласовано)."""
    marker, body = _strip_marker(c.explanation)
    if marker is None:                         # СОГЛАСОВАНО
        return None
    if marker == ():                           # без маркера: решает флаг consistent
        if c.consistent:
            return None
        marker = ("", Criterion.set_consistent, Severity.major, 0.7,
                  "Критерий приёмки {ac} не согласован с правилом {rule}")
    _, criterion, severity, conf, title_t = marker
    rmap = rid_map(reqs)
    ac = rmap.get(c.ac_rid)
    rule_rids = [r for r in c.rule_rids if r in rmap and r != c.ac_rid]
    rule = rmap.get(rule_rids[0]) if rule_rids else None
    quote, grounded = repair_quote(doc, c.quote_ac, [ac] if ac else list(reqs))
    rule_quote, _ = repair_quote(doc, c.quote_rule, [rmap[r] for r in rule_rids] or list(reqs))
    expl = body.rstrip()
    if expl:
        expl = expl[0].upper() + expl[1:]
    if expl and expl[-1] not in ".!?»":
        expl += "."
    if c.expected_by_rules:
        expl += f" Расчёт по правилам: {_sentence(c.expected_by_rules)}"
    if c.stated_expected:
        expl += f" В критерии: «{c.stated_expected.strip().strip('«»')}»."
    if rule_quote:
        expl += f" Правило {_label(rule)}: «{rule_quote.strip()}»"
    rec = (c.recommendation or "").strip() or (
        "Привести ожидаемый результат критерия в соответствие с правилом или исправить правило"
        + (f". По правилам ожидается: {_sentence(c.expected_by_rules)}" if c.expected_by_rules else "."))
    loc = ""
    if grounded:
        _, blk = find_quote(doc, quote)
        loc = blk.locator() if blk else ""
    rids = ([c.ac_rid] if ac else []) + rule_rids
    return Finding(scope="set", rids=rids, criterion=criterion, severity=severity,
                   title=title_t.format(ac=_label(ac), rule=_label(rule) if rule else "правил"),
                   quote=quote, location=loc or (ac.location if ac else ""), explanation=expl,
                   recommendation=rec, source=FindingSource.scenario, confidence=conf, grounded=grounded)


def missing_finding(doc: Document, reqs: Sequence[Requirement], missing: Sequence[str]) -> Optional[Finding]:
    """missing_scenarios → одна агрегированная находка set_validatable с цитатой раздела критериев."""
    items = [_NO_AC.sub("", " ".join(m.split())).rstrip(" .;") for m in missing if m and m.strip()]
    items = [m for m in items if m]
    if not items:
        return None
    rmap = rid_map(reqs)
    acs = [r for r in reqs if r.kind == ReqKind.acceptance]
    rids: list[str] = []
    for m in items:
        for x in re.findall(r"R-\d+", m):
            if x in rmap and x not in rids:
                rids.append(x)
    quote, loc = "", ""
    blocks = {b.id: b for b in doc.blocks}
    if acs and acs[0].block_ids and acs[0].block_ids[0] in blocks:
        b = blocks[acs[0].block_ids[0]]
        head = next((h for h in doc.blocks if h.kind == BlockKind.heading
                     and h.section_path == b.section_path), None)
        quote = head.text if head else acs[0].quote
        loc = (head or b).locator()
    elif rids:
        quote, loc = rmap[rids[0]].quote, rmap[rids[0]].location
    if not quote:
        return None
    criterion = Criterion.set_validatable if acs else Criterion.set_complete
    severity = Severity.major if len(items) >= 3 else Severity.minor
    lst = "; ".join(f"{i}) {m}" for i, m in enumerate(items, 1))
    expl = (f"Критерии приёмки ({len(acs)} шт.) не покрывают ветки правил, поэтому приёмка по ним "
            f"не подтвердит поведение системы в этих случаях: {lst}" if acs else
            f"В документе нет критериев приёмки; ветки правил, для которых нужны проверочные сценарии: {lst}")
    rec = ("Добавить в раздел критериев приёмки по строке «исходные условия | действие | ожидаемый результат» "
           "на каждую ветку: " + "; ".join(items[:8]) + ".")
    score, _ = find_quote(doc, quote)
    return Finding(scope="set", rids=rids or [a.rid for a in acs],
                   criterion=criterion, severity=severity,
                   title=f"Ветки правил без критериев приёмки: {len(items)}", quote=quote, location=loc,
                   explanation=expl + ".", recommendation=rec, source=FindingSource.scenario,
                   confidence=0.75, grounded=score >= 85)


# Ветки без критериев приёмки попадают в оценку набора (summarize) независимо от решения critic.
_MISSING: dict[int, list[str]] = {}       # id(reqs) → ветки (вызов идёт в рабочем потоке)
_LOCK = threading.Lock()


def _remember_missing(reqs: Sequence[Requirement], missing: Sequence[str]) -> None:
    items = [_NO_AC.sub("", " ".join(m.split())).rstrip(" .;") for m in missing if m and m.strip()]
    with _LOCK:
        if len(_MISSING) > 32:
            _MISSING.clear()
        _MISSING[id(reqs)] = [m for m in items if m]


def _recall_missing(reqs: Sequence[Requirement]) -> list[str]:
    with _LOCK:
        return list(_MISSING.get(id(reqs), []))


def _scenarios(doc: Document, reqs: Sequence[Requirement], llm: LLMClient) -> list[Finding]:
    acs = [r for r in reqs if r.kind == ReqKind.acceptance]
    rules = [r for r in reqs if r.kind in RULE_KINDS]
    if not rules:
        return []
    out = call(llm, "set_scenarios", ScenarioChecks, "set.scenarios", doc_name=doc.name,
               acceptance=req_blocks(acs), rules=req_blocks(rules),
               context=_context_for_scenarios(doc, reqs))
    found = [f for f in (scenario_finding(doc, reqs, c) for c in out.checks) if f]
    _remember_missing(reqs, out.missing_scenarios)
    mf = missing_finding(doc, reqs, out.missing_scenarios)
    if mf:
        found.append(mf)
    return found


# -------------------------------------------------------------- completeness ---
def _completeness(doc: Document, reqs: Sequence[Requirement], llm: LLMClient,
                  prior: Sequence[Finding]) -> list[Finding]:
    out = call(llm, "set_completeness", FindingDrafts, "set.completeness",
               criteria=criteria_text(["set_complete", "complete"], xref=["§4.3.3"]),
               doc_name=doc.name, fintech_checks=fintech_checks(doc, cap=25),
               outline=outline(doc, reqs), requirements=req_blocks(reqs),
               known_findings=known_lines(prior))
    res = []
    for d in out.findings:
        f = draft_to_finding(doc, reqs, d, source=FindingSource.llm_set, scope="set")
        if fintech_tag(f):
            f.source = FindingSource.fintech
        res.append(f)
    return res


def review_set(doc: Document, reqs: list[Requirement], llm: LLMClient,
               prior: list[Finding]) -> tuple[list[Finding], SetSummary]:
    if not reqs:
        return [], SetSummary()
    cjobs = contradiction_jobs(reqs)
    jobs = [lambda: _scenarios(doc, reqs, llm), lambda: _completeness(doc, reqs, llm, prior)]
    names = ["set.scenarios", "set.completeness"]
    for i, (g, shown) in enumerate(cjobs, 1):
        jobs.append(lambda g=g, shown=shown: _contradictions_call(doc, reqs, llm, prior, g, shown))
        names.append(f"set.contradictions ({i}/{len(cjobs)})")
    results = parallel.run_all(jobs, max_workers=parallel.workers(4), label="set", default=None)
    found: list[Finding] = []
    notes = []
    for name, r in zip(names, results):
        if r is None:
            notes.append(f"Стадия {name} не выполнена (ошибка LLM); её проверки в отчёт не вошли.")
            continue
        found += r
    return found, SetSummary(notes=notes)


# ------------------------------------------------------------------- summary ---
def _fallback_status(crit: str, findings: Sequence[Finding]) -> str:
    fs = [f for f in findings if f.criterion.value == crit]
    if not fs:
        return "нет оценки: модель не вернула оценку, находок по критерию нет"
    ids = ", ".join(f.fid or f.title for f in fs[:6])
    st = "нарушено" if any(f.severity == Severity.critical for f in fs) or len(fs) > 2 else "частично"
    return f"{st}: находки {ids}"


def summarize(doc: Document, reqs: list[Requirement], findings: list[Finding], llm: LLMClient) -> SetSummary:
    """Оценка набора по set_* / ranked / modifiable / traceable. Вызывается ПОСЛЕ critic и нумерации D-NNN."""
    keys = [k for k in SET_CRITERIA if k in set(scope_criteria("set"))] or SET_CRITERIA
    notes: list[str] = []
    crit: dict[str, str] = {}
    try:
        out = call(llm, "set_summary", SetSummaryDraft, "set.summary",
                   criteria=criteria_text(keys, compact=True), doc_name=doc.name,
                   outline=outline(doc, reqs), requirements=req_short(reqs),
                   findings=finding_lines(findings), stats=stats(reqs, findings))
        for a in out.assessments:
            k = a.criterion.value
            if k in keys and k not in crit:
                crit[k] = f"{a.status.value}: {' '.join(a.comment.split())}"
        notes = [" ".join(n.split()) for n in out.notes if n and n.strip()]
    except Exception as e:  # noqa: BLE001
        log.warning("set.summary упал: %s", e)
        notes.append("Оценка набора сформирована по правилам без LLM (модель недоступна).")
    missing = _recall_missing(reqs)
    if missing:
        notes.append("Ветки правил без критериев приёмки (прогон сценариев): " + "; ".join(missing) + ".")
    ordered = {k: crit.get(k) or _fallback_status(k, findings) for k in keys}
    return SetSummary(criteria=ordered, notes=notes)
