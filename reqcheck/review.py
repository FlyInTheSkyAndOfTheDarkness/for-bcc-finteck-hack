"""Стадия 4. LLM-проверка каждого требования по ISO 29148 §5.2.5. Владелец: A7.

Требования режутся на пачки по 6–8 штук в порядке документа (пачка не смешивает meta с
остальными). Для каждой пачки — контекст (заголовки разделов, цель, глоссарий, соседние
требования вне пачки) и известные находки линтеров по требованиям пачки, чтобы модель не
дублировала их. Пачки идут параллельно (2–3 потока; лимитер провайдера потокобезопасен).
Meta-требования (версия, согласования, чек-листы) проверяются одной компактной пачкой.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Sequence

from reqcheck.engine import parallel
from reqcheck.engine.terms import TermIndex
from reqcheck.engine.prompting import (REQ_CRITERIA, call, criteria_text, draft_to_finding,
                                       known_lines, req_blocks)
from reqcheck.llm import LLMClient
from reqcheck.models import (Document, Finding, FindingSource, ReqKind, Requirement)
from reqcheck.schemas import FindingDrafts

log = logging.getLogger(__name__)

BATCH = 7
CONTEXT_CHARS = 3000
GLOSSARY_RE = re.compile(r"глоссари|термин|определени|сокращени|glossary", re.I)


def _section(doc_blocks: dict, r: Requirement) -> tuple[str, ...]:
    b = doc_blocks.get(r.block_ids[0]) if r.block_ids else None
    if b is not None and b.section_path:
        return tuple(b.section_path)
    return tuple(r.location.split(" (")[0].split(" › ")) if r.location else ()


def make_batches(doc: Document, reqs: Sequence[Requirement], size: int = BATCH) -> list[list[Requirement]]:
    """Пачки в порядке документа: раздел к разделу, большой раздел делится поровну."""
    blocks = {b.id: b for b in doc.blocks}
    groups: list[list[Requirement]] = []
    for r in reqs:
        if r.kind == ReqKind.meta:
            continue
        sec = _section(blocks, r)
        if groups and _section(blocks, groups[-1][-1]) == sec:
            groups[-1].append(r)
        else:
            groups.append([r])
    # большой раздел → равные куски ≤ size
    chunks: list[list[Requirement]] = []
    for g in groups:
        if len(g) <= size:
            chunks.append(g)
        else:
            n = -(-len(g) // size)
            k = -(-len(g) // n)
            chunks += [g[i:i + k] for i in range(0, len(g), k)]
    # мелкие соседние разделы склеиваем до size
    batches: list[list[Requirement]] = []
    for c in chunks:
        if batches and len(batches[-1]) + len(c) <= size:
            batches[-1].extend(c)
        else:
            batches.append(list(c))
    return batches


def build_context(doc: Document, reqs: Sequence[Requirement], batch: Sequence[Requirement],
                  limit: int = CONTEXT_CHARS, index: Optional[TermIndex] = None) -> str:
    blocks = {b.id: b for b in doc.blocks}
    in_batch = {r.rid for r in batch}
    parts: list[str] = []
    secs = []
    for r in batch:
        s = " › ".join(_section(blocks, r))
        if s and s not in secs:
            secs.append(s)
    if secs:
        parts.append("Разделы пачки: " + "; ".join(secs))
    goals = [r for r in reqs if r.kind == ReqKind.business and r.rid not in in_batch][:2]
    if goals:
        parts.append("Цель документа: " + " ".join(f"[{g.rid}] «{g.quote}»" for g in goals))
    gloss = [b.text for b in doc.blocks
             if b.kind.value != "heading" and any(GLOSSARY_RE.search(h) for h in b.section_path)]
    if gloss:
        parts.append("Глоссарий: " + " ; ".join(gloss)[:800])
    # соседние требования вне пачки: по одному до и после + тексты того же раздела
    order = [r.rid for r in reqs]
    idx = [order.index(r.rid) for r in batch]
    neigh: list[Requirement] = []
    for j in (min(idx) - 1, max(idx) + 1, max(idx) + 2):
        if 0 <= j < len(reqs) and reqs[j].rid not in in_batch and reqs[j].kind != ReqKind.meta:
            neigh.append(reqs[j])
    batch_secs = {_section(blocks, r) for r in batch}
    for r in reqs:
        if len(neigh) >= 4:
            break
        if r.rid not in in_batch and r not in neigh and _section(blocks, r) in batch_secs:
            neigh.append(r)
    if neigh:
        parts.append("Соседние требования (вне пачки, не проверять):\n" + "\n".join(
            f"[{r.rid}] {r.doc_id or '—'} | {r.kind.value} | «{r.quote[:300]}»" for r in neigh))
    # связанные по терминам требования из других разделов: ответ на «пропуск» может быть там
    if index is not None:
        rmap = {r.rid: r for r in reqs}
        rel: list[str] = []
        shown = in_batch | {r.rid for r in neigh}
        for r in batch:
            rel += index.related(r.rid, 2, exclude=shown | set(rel))
        rel = rel[:6]
        if rel:
            parts.append("Связанные требования из других разделов (не проверять, только для понимания):\n"
                         + "\n".join(f"[{x}] {rmap[x].doc_id or '—'} | {rmap[x].kind.value} | "
                                     f"«{rmap[x].quote[:300]}»" for x in rel))
    text = "\n".join(parts)
    return text[:limit]


def _linter_findings(doc: Document, reqs: Sequence[Requirement]) -> list[Finding]:
    try:
        from reqcheck import linters
        return linters.run_linters(doc, list(reqs))
    except Exception as e:  # noqa: BLE001
        log.warning("линтеры для known_findings недоступны: %s", e)
        return []


def _review_batch(doc: Document, reqs: Sequence[Requirement], batch: Sequence[Requirement],
                  llm: LLMClient, known: Sequence[Finding], criteria: str, tag: str,
                  index: Optional[TermIndex] = None) -> list[Finding]:
    rids = {r.rid for r in batch}
    kn = [f for f in known if set(f.rids) & rids]
    out = call(llm, "review_requirement", FindingDrafts, tag,
               criteria=criteria, doc_name=doc.name, context=build_context(doc, reqs, batch, index=index),
               known_findings=known_lines(kn), requirements=req_blocks(batch))
    res = []
    for d in out.findings:
        f = draft_to_finding(doc, reqs, d, source=FindingSource.llm_requirement, scope="requirement",
                             allowed_rids=list(rids))
        if f.criterion.value.startswith("set_") or f.criterion.value in ("ranked", "modifiable", "traceable"):
            f.scope = "set"
        res.append(f)
    return res


def review_requirements(doc: Document, reqs: list[Requirement], llm: LLMClient,
                        known: Optional[list[Finding]] = None) -> list[Finding]:
    """Находки source=llm_requirement по всем требованиям (кроме meta — отдельная компактная пачка)."""
    if not reqs:
        return []
    known = _linter_findings(doc, reqs) if known is None else known
    full = criteria_text(REQ_CRITERIA, language=True)
    compact = criteria_text(["conforming", "complete", "necessary"], compact=True)
    index = TermIndex(reqs)
    jobs = []
    for b in make_batches(doc, reqs):
        jobs.append(lambda b=b: _review_batch(doc, reqs, b, llm, known, full, "review", index))
    meta = [r for r in reqs if r.kind == ReqKind.meta]
    if meta:
        jobs.append(lambda: _review_batch(doc, reqs, meta, llm, known, compact, "review.meta"))
    results = parallel.run_all(jobs, max_workers=parallel.workers(3), label="review", default=[])
    found: list[Finding] = []
    for r in results:
        found += r or []
    return found
