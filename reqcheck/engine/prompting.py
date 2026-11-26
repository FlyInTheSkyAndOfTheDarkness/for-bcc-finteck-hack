"""Рендер промптов и данных по контракту prompts/README.md, вызов LLM, FindingDraft → Finding.

Все значения плейсхолдеров рендерятся здесь, чтобы формат был одинаковым во всех стадиях.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Iterable, Optional, Sequence, TypeVar

from pydantic import BaseModel
from rapidfuzz import fuzz

from reqcheck.grounding import GROUND_THRESHOLD, find_quote, normalize
from reqcheck.llm import LLMClient
from reqcheck.models import (Block, BlockKind, Criterion, Document, Finding, FindingSource,
                             Requirement, Severity)
from reqcheck.prompts import load, load_yaml
from reqcheck.schemas import FindingDraft

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

SPLIT = "\n## ДАННЫЕ\n"
NONE = "нет"
SEV_RANK = {Severity.critical: 0, Severity.major: 1, Severity.minor: 2}

SET_CRITERIA = ["set_complete", "set_consistent", "set_feasible", "set_comprehensible",
                "set_validatable", "set_bounded", "ranked", "modifiable", "traceable"]
REQ_CRITERIA = ["necessary", "appropriate", "unambiguous", "complete", "singular", "feasible",
                "verifiable", "correct", "conforming"]


# ------------------------------------------------------------------ render ---
def _val(v) -> str:
    if v is None:
        return NONE
    s = str(v)
    return s if s.strip() else NONE


def render(name: str, **ctx) -> tuple[str, str]:
    """(system, user) для prompts/<name>.md. Пустые значения → «нет»."""
    text = load(name).format(**{k: _val(v) for k, v in ctx.items()})
    head, tail = text.split(SPLIT, 1)
    system = load("_common") + "\n\n" + head
    user = "## ДАННЫЕ\n" + tail
    return system, user


def call(llm: LLMClient, name: str, schema: type[T], tag: str, *,
         temperature: Optional[float] = None, **ctx) -> T:
    system, user = render(name, **ctx)
    return llm.generate_json(system, user, schema, tag=tag, temperature=temperature)


# ---------------------------------------------------------------- criteria ---
def _catalog() -> dict:
    iso = load_yaml("criteria/iso29148.yaml")
    ieee = load_yaml("criteria/ieee830.yaml")
    crit = dict(iso.get("criteria", {}))
    crit.update(ieee.get("criteria", {}))
    return {"criteria": crit, "language": iso.get("language_guidance") or {},
            "xref": ieee.get("cross_reference") or []}


def render_criterion(key: str, c: dict, *, compact: bool = False) -> str:
    lines = [f"### {key} — {c.get('name_ru', key)} ({c.get('standard', '')})".rstrip(" ()")]
    if c.get("definition_ru"):
        lines.append(" ".join(str(c["definition_ru"]).split()))
    qs = c.get("check_questions") or []
    if qs:
        lines.append("Проверочные вопросы:")
        lines += [f"- {q}" for q in qs]
    if not compact and c.get("typical_defects"):
        lines.append("Типичные дефекты:")
        lines += [f"- {d}" for d in c["typical_defects"]]
    sg = c.get("severity_guide") or {}
    if sg:
        lines.append("Критичность: " + "; ".join(f"{k} — {v}" for k, v in sg.items()))
    return "\n".join(lines)


def render_language(lg: dict) -> str:
    if not lg:
        return ""
    lines = [f"### Языковые признаки неоднозначности ({lg.get('standard', 'ISO/IEC/IEEE 29148:2018 §5.2.7')})"]
    if lg.get("principle_ru"):
        lines.append(" ".join(str(lg["principle_ru"]).split()))
    for tc in lg.get("term_classes") or []:
        markers = ", ".join(tc.get("markers") or [])
        lines.append(f"- {tc.get('class_ru', '')}: {markers} → {tc.get('fix_ru', '')}")
    return "\n".join(lines)


def render_xref(sections: Iterable[str]) -> str:
    want = tuple(sections)
    out = []
    for x in _catalog()["xref"]:
        name = str(x.get("ieee830", ""))
        if any(name.startswith(s) for s in want):
            maps = ", ".join(x.get("maps_to") or [])
            lines = [f"### IEEE 830-1998 {name} (соответствует: {maps})"]
            lines += [f"- {e}" for e in x.get("extra_checks") or []]
            out.append("\n".join(lines))
    return "\n\n".join(out)


def criteria_text(keys: Sequence[str], *, compact: bool = False, language: bool = False,
                  xref: Sequence[str] = ()) -> str:
    cat = _catalog()
    parts = [render_criterion(k, cat["criteria"][k], compact=compact)
             for k in keys if k in cat["criteria"]]
    if language:
        parts.append(render_language(cat["language"]))
    if xref:
        parts.append(render_xref(xref))
    return "\n\n".join(p for p in parts if p)


def scope_criteria(scope: str) -> list[str]:
    return [k for k, c in _catalog()["criteria"].items() if c.get("scope") == scope]


# ------------------------------------------------------------ requirements ---
def _loc(r: Requirement) -> str:
    return r.location or "—"


def req_block(r: Requirement) -> str:
    return (f"[{r.rid}] doc_id: {r.doc_id or '—'} | kind: {r.kind.value} | место: {_loc(r)}\n"
            f"Текст: «{r.quote}»")


def req_blocks(reqs: Iterable[Requirement]) -> str:
    return "\n".join(req_block(r) for r in reqs)


def req_short(reqs: Iterable[Requirement]) -> str:
    return "\n".join(f"[{r.rid}] {r.doc_id or '—'} | {r.kind.value} | {r.summary}" for r in reqs)


def rid_map(reqs: Iterable[Requirement]) -> dict[str, Requirement]:
    return {r.rid: r for r in reqs}


# ---------------------------------------------------------------- findings ---
def _short_quote(q: str, n: int = 160) -> str:
    q = " ".join((q or "").split())
    return q if len(q) <= n else q[: n - 1].rstrip() + "…"


def known_lines(findings: Iterable[Finding]) -> str:
    """`- R-04 | unambiguous | «быстро» | <title>` — для known_findings."""
    out = []
    for f in findings:
        rids = ", ".join(f.rids) if f.rids else "набор"
        out.append(f"- {rids} | {f.criterion.value} | «{_short_quote(f.quote, 120)}» | {f.title}")
    return "\n".join(out)


def finding_lines(findings: Iterable[Finding], *, details: bool = False,
                  details_for: Optional[set] = None) -> str:
    """`- D-003 | critical | set_consistent | R-04, R-15 | <title> | цитата: «…»` (+ пояснение)."""
    out = []
    for f in findings:
        rids = ", ".join(f.rids) if f.rids else "—"
        out.append(f"- {f.fid or '?'} | {f.severity.value} | {f.criterion.value} | {rids} | "
                   f"{f.title} | цитата: «{_short_quote(f.quote)}»")
        if details or (details_for and f.severity in details_for):
            out.append(f"  Пояснение: {' '.join(f.explanation.split())}")
            out.append(f"  Рекомендация: {' '.join(f.recommendation.split())}")
    return "\n".join(out)


def stats(reqs: Sequence[Requirement], findings: Sequence[Finding]) -> str:
    kinds = Counter(r.kind.value for r in reqs)
    sev = Counter(f.severity.value for f in findings)
    crit = Counter(f.criterion.value for f in findings)
    lines = [f"Требований: {len(reqs)} (" + ", ".join(f"{k} {v}" for k, v in kinds.most_common()) + ")",
             f"Находок: {len(findings)} (critical {sev.get('critical', 0)}, major {sev.get('major', 0)}, "
             f"minor {sev.get('minor', 0)})"]
    if crit:
        lines.append("По критериям: " + ", ".join(f"{k} {v}" for k, v in crit.most_common()))
    return "\n".join(lines)


# ------------------------------------------------------------------ outline ---
def outline(doc: Document, reqs: Sequence[Requirement]) -> str:
    """Разделы документа по заголовкам с числом требований (прямо в разделе)."""
    by_id = {b.id: b for b in doc.blocks}
    count: Counter = Counter()
    for r in reqs:
        b = by_id.get(r.block_ids[0]) if r.block_ids else None
        if b is not None:
            count[tuple(b.section_path)] += 1
    content: Counter = Counter()
    for b in doc.blocks:
        if b.kind != BlockKind.heading and b.text.strip():
            content[tuple(b.section_path)] += 1
    lines = []
    for b in doc.blocks:
        if b.kind != BlockKind.heading or not b.section_path:
            continue
        path = tuple(b.section_path)
        n = count.get(path, 0)
        if n:
            tail = f"требований: {n}"
        elif content.get(path, 0):
            tail = "требований нет (пояснительный текст)"
        else:
            sub = any(p[:len(path)] == path and len(p) > len(path) for p in content)
            tail = "только подразделы" if sub else "пусто / шаблон"
        lines.append(f"- {' › '.join(path)} — {tail}")
    return "\n".join(lines)


# ---------------------------------------------------------------- fintech ---
def fintech_checks(doc: Document, cap: int = 25) -> str:
    data = load_yaml("checks/fintech.yaml") or {}
    areas = data.get("areas") or {}
    text = (doc.text or " ".join(b.text for b in doc.blocks)).lower().replace("ё", "е")
    scored = []
    for i, it in enumerate(data.get("items") or []):
        keys = [k.lower().replace("ё", "е") for k in (it.get("applies_when") or [])]
        if keys:
            hits = sum(1 for k in keys if k in text)
            if not hits:
                continue
        else:
            hits = 1   # всегда применим; ранжируется как один совпавший ключ
        sev_bonus = {"critical": 0.5, "major": 0.25}.get(str(it.get("default_severity")), 0)
        scored.append((-(hits + sev_bonus), i, it))
    scored.sort(key=lambda t: (t[0], t[1]))
    chosen = sorted(scored[:cap], key=lambda t: t[1])      # исходный порядок каталога
    lines = []
    for _, _, it in chosen:
        area = areas.get(it.get("area"), it.get("area", ""))
        lines.append(f"- [{it['id']}] ({area}; criterion: {it.get('criterion')}; по умолчанию: "
                     f"{it.get('default_severity')}) {it.get('question_ru', '').strip()} "
                     f"Почему важно: {it.get('why_ru', '').strip()}")
    return "\n".join(lines)


# -------------------------------------------------------- draft → finding ---
_WORD = re.compile(r"\S+")


def _expand_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def best_span(quote: str, text: str) -> tuple[float, str]:
    """Лучший нечёткий фрагмент `text`, соответствующий `quote` (по словам)."""
    if not quote or not text:
        return 0.0, ""
    q, t = quote.strip(), text
    if len(q) >= len(t):
        return float(fuzz.partial_ratio(q.casefold(), t.casefold())), t.strip()
    al = fuzz.partial_ratio_alignment(q.casefold(), t.casefold())
    if al is None:
        return 0.0, ""
    s, e = _expand_to_words(t, al.dest_start, al.dest_end)
    span = t[s:e].strip(" ,;:«»\"")
    return float(al.score), span


def repair_quote(doc: Document, quote: str, candidates: Sequence[Requirement]) -> tuple[str, bool]:
    """Вернуть (цитата, grounded). Негроундированную цитату ищем в тексте требований."""
    if quote:
        score, _ = find_quote(doc, quote)
        if score >= GROUND_THRESHOLD:
            return quote, True
    best = (0.0, "")
    parts = [p for p in re.split(r"\s*(?:\.\.\.+|…)\s*", quote or "") if len(p.strip()) >= 6] or [quote]
    for part in parts:
        for r in candidates:
            sc, span = best_span(part, r.quote)
            if sc > best[0] and span:
                best = (sc, span)
    if best[0] >= 70 and best[1]:
        score, _ = find_quote(doc, best[1])
        if score >= GROUND_THRESHOLD:
            return best[1], True
    return quote, False


def _block_to_rid(reqs: Sequence[Requirement]) -> dict[str, str]:
    m: dict[str, str] = {}
    for r in reqs:
        for b in r.block_ids:
            m.setdefault(b, r.rid)
    return m


def clamp_conf(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.7


def draft_to_finding(doc: Document, reqs: Sequence[Requirement], d: FindingDraft, *,
                     source: FindingSource, scope: str,
                     allowed_rids: Optional[Sequence[str]] = None) -> Finding:
    """FindingDraft → Finding: фильтр rids, ремонт цитаты, location, grounded."""
    rmap = rid_map(reqs)
    rids = []
    for x in d.rids or []:
        x = (x or "").strip()
        if x in rmap and x not in rids:
            rids.append(x)
    if allowed_rids is not None and scope == "requirement":
        inside = [x for x in rids if x in set(allowed_rids)]
        rids = inside or rids
    cands = [rmap[x] for x in rids] or list(reqs)
    quote, grounded = repair_quote(doc, d.quote, cands)
    if not grounded and rids:   # цитата могла прийти из другого требования
        quote, grounded = repair_quote(doc, d.quote, list(reqs))
    location = ""
    block: Optional[Block] = None
    if grounded:
        _, block = find_quote(doc, quote)
        if block is not None:
            location = block.locator()
            if not rids:
                rid = _block_to_rid(reqs).get(block.id)
                if rid:
                    rids = [rid]
    if not location and rids:
        location = rmap[rids[0]].location
    return Finding(scope=scope, rids=rids, criterion=d.criterion, severity=d.severity,
                   title=" ".join(d.title.split()), quote=quote, location=location,
                   explanation=d.explanation.strip(), recommendation=d.recommendation.strip(),
                   source=source, confidence=clamp_conf(d.confidence), grounded=grounded)


def fintech_tag(f: Finding) -> bool:
    return any("[FT-" in s for s in (f.explanation, f.title, f.recommendation))


def is_set_criterion(c: Criterion) -> bool:
    return c.value in SET_CRITERIA


def norm(s: str) -> str:
    return normalize(s or "")
