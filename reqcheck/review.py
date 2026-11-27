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

BATCH = 4                  # развёрнутых правил в пачке (строк таблиц — до 8)
CONTEXT_CHARS = 3000
# Проходы ревью. По умолчанию один проход по всем девяти критериям. Эксперимент A9 с двумя
# сфокусированными проходами (TWO_PASSES: ясность / полнота) на эталонном документе дал тот же
# recall (61%), но +12 находок, precision 78% → 74% и +3 мин, поэтому выключен.
TWO_PASSES = [
    ("clarity", ["unambiguous", "verifiable", "singular", "appropriate", "necessary"], True),
    ("completeness", ["complete", "correct", "feasible", "conforming"], False),
]
PASSES = [("all", REQ_CRITERIA, True)]
PASS_FOCUS = {
    "clarity": "ФОКУС ЭТОГО ПРОХОДА: однозначность, проверяемость, единичность, уместность, необходимость — "
               "вопросы метода 1, 2, 3, 7, 9. Полноту веток и корректность проверяет другой проход; здесь их не выводи.",
    "completeness": "ФОКУС ЭТОГО ПРОХОДА: полнота и корректность — вопросы метода 4, 5, 6, 8 (ветки и граничные "
                    "случаи, полнота действия, числа, внутренние ошибки), а также незаполненные поля. "
                    "Расплывчатые слова и проверяемость проверяет другой проход; здесь их не выводи.",
}
GLOSSARY_RE = re.compile(r"глоссари|термин|определени|сокращени|glossary", re.I)


def _section(doc_blocks: dict, r: Requirement) -> tuple[str, ...]:
    b = doc_blocks.get(r.block_ids[0]) if r.block_ids else None
    if b is not None and b.section_path:
        return tuple(b.section_path)
    return tuple(r.location.split(" (")[0].split(" › ")) if r.location else ()


# Вес требования в пачке: развёрнутые правила (functional, nonfunctional, …) требуют больше
# внимания модели, чем строки таблиц API или критериев приёмки. Ёмкость пачки — 2·BATCH,
# т. е. до BATCH развёрнутых правил или до 2·BATCH строк таблиц (не больше MAX_ROWS).
DENSE_KINDS = {ReqKind.functional, ReqKind.nonfunctional, ReqKind.integration, ReqKind.data,
               ReqKind.business}
MAX_ROWS = 8


def _weight(r: Requirement) -> int:
    return 2 if r.kind in DENSE_KINDS else 1


def make_batches(doc: Document, reqs: Sequence[Requirement], size: int = BATCH) -> list[list[Requirement]]:
    """Пачки в порядке документа: раздел к разделу, большой раздел делится поровну.

    size — сколько развёрнутых правил помещается в пачку; строк таблиц помещается вдвое больше.
    """
    blocks = {b.id: b for b in doc.blocks}
    cap = 2 * size
    rows = min(MAX_ROWS, cap)
    groups: list[list[Requirement]] = []
    for r in reqs:
        if r.kind == ReqKind.meta:
            continue
        sec = _section(blocks, r)
        if groups and _section(blocks, groups[-1][-1]) == sec:
            groups[-1].append(r)
        else:
            groups.append([r])

    def fits(xs: Sequence[Requirement]) -> bool:
        return sum(_weight(x) for x in xs) <= cap and len(xs) <= rows

    # большой раздел → равные по весу куски
    chunks: list[list[Requirement]] = []
    for g in groups:
        if fits(g):
            chunks.append(g)
            continue
        total = sum(_weight(x) for x in g)
        n = max(-(-total // cap), -(-len(g) // rows))
        target = -(-total // n)
        cur: list[Requirement] = []
        for x in g:
            if cur and (sum(_weight(y) for y in cur) + _weight(x) > target or len(cur) >= rows):
                chunks.append(cur)
                cur = []
            cur.append(x)
        if cur:
            chunks.append(cur)
    # мелкие соседние разделы склеиваем, пока помещаются
    batches: list[list[Requirement]] = []
    for c in chunks:
        if batches and fits(batches[-1] + c):
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


def unwrap_quote(d):
    """Модель иногда оборачивает цитату в «…» или кавычки — снять обёртку до привязки к тексту."""
    q = (d.quote or "").strip()
    while len(q) > 2 and q[0] in "«\"'“" and q[-1] in "»\"'”":
        q = q[1:-1].strip()
    d.quote = q
    return d


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
        f = draft_to_finding(doc, reqs, unwrap_quote(d), source=FindingSource.llm_requirement, scope="requirement",
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
    compact = criteria_text(["conforming", "complete", "necessary"], compact=True)
    index = TermIndex(reqs)
    if len(PASSES) > 1:
        passes = [PASS_FOCUS[name] + "\n\n" + criteria_text(keys, language=lang) for name, keys, lang in PASSES]
    else:
        passes = [criteria_text(REQ_CRITERIA, language=True)]
    jobs = []
    for b in make_batches(doc, reqs):
        for crit in passes:
            jobs.append(lambda b=b, crit=crit: _review_batch(doc, reqs, b, llm, known, crit, "review", index))
    meta = [r for r in reqs if r.kind == ReqKind.meta]
    if meta:
        jobs.append(lambda: _review_batch(doc, reqs, meta, llm, known, compact, "review.meta"))
    results = parallel.run_all(jobs, max_workers=parallel.workers(3), label="review", default=[])
    found: list[Finding] = []
    for r in results:
        found += r or []
    return found
