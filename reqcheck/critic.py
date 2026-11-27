"""Стадия 6. Проверка находок: привязка, реальность, дубли, критичность. Владелец: A7.

1. Детерминированная предварительная дедупликация: одно семейство критериев + пересекающиеся
   цитаты + общие R-NN → одна находка (берётся более конкретная/уверенная, rids объединяются).
2. LLM-критик пачками ≤ 25 кандидатов, отсортированных по R-NN (дубли попадают в одну пачку).
   keep/duplicate_of/severity/confidence применяются, оценённые получают verified=True.
   Отклонённые Critic находки с подтверждённой цитатой не удаляются, а помечаются doubtful=True
   (отдельный блок отчёта, не влияют на итог); удаляются только дубли и неподтверждённые цитаты.
   Находки линтера с confidence ≥ 0.9 «защищены»: критик может слить их как дубль, но не выбросить.
3. Отбрасываются находки, у которых цитата не найдена в документе И которые критик не подтвердил.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Sequence

from rapidfuzz import fuzz

from reqcheck.engine import parallel
from reqcheck.engine.prompting import SEV_RANK, call, norm, req_blocks
from reqcheck.engine.terms import TermIndex
from reqcheck.llm import LLMClient
from reqcheck.models import Criterion, Document, Finding, FindingSource, ReqKind, Requirement
from reqcheck.schemas import CriticResult

log = logging.getLogger(__name__)

BATCH = 15
FULL_REQS = 60
PROTECT_CONF = 0.9
MIN_KEEP_CONF = 0.4
TERM_WORDS = 3        # цитата-термин («быстро») — дубль по одному пересечению цитат
SPAN_RATIO = 0.5      # короче половины длинной цитаты — фрагмент составного пункта
TITLE_SIM = 70        # тогда дубль, только если заголовки совпадают по сути (token_set_ratio)

FAMILY = {
    Criterion.unambiguous: "clarity", Criterion.verifiable: "clarity",
    Criterion.set_comprehensible: "clarity",
    Criterion.complete: "completeness", Criterion.set_complete: "completeness",
    Criterion.set_validatable: "completeness", Criterion.set_bounded: "completeness",
    Criterion.set_consistent: "consistency", Criterion.correct: "consistency",
    Criterion.feasible: "feasibility", Criterion.set_feasible: "feasibility",
    Criterion.conforming: "form", Criterion.modifiable: "form", Criterion.traceable: "form",
    Criterion.ranked: "form",
    Criterion.necessary: "necessary", Criterion.appropriate: "appropriate",
    Criterion.singular: "singular",
}


def family(c: Criterion) -> str:
    return FAMILY.get(c, c.value)


def _rid_key(f: Finding) -> tuple:
    nums = sorted(int(m) for r in f.rids for m in re.findall(r"\d+", r)) or [999]
    return (nums[0], family(f.criterion), f.scope)


def quotes_overlap(a: str, b: str) -> bool:
    qa, qb = norm(a), norm(b)
    if not qa or not qb:
        return False
    short, long_ = sorted((qa, qb), key=len)
    if len(short) >= 6 and short in long_:
        return True
    if len(short) >= 15 and fuzz.partial_ratio(short, long_) >= 90:
        return True
    return False


def same_problem(a: Finding, b: Finding) -> bool:
    if family(a.criterion) != family(b.criterion):
        return False
    ra, rb = set(a.rids), set(b.rids)
    if ra and rb and not (ra & rb):
        return False
    if not ra and not rb and a.scope != b.scope:
        return False
    if not quotes_overlap(a.quote, b.quote):
        return False
    # Составной пункт («Месяц определяется… В расходы входят… Возврат…») несёт несколько разных
    # дефектов: цитата-предложение внутри длинной цитаты — дубль, только если совпадает и суть.
    qa, qb = sorted((norm(a.quote), norm(b.quote)), key=len)
    if len(qa.split()) <= TERM_WORDS or len(qa) >= SPAN_RATIO * len(qb):
        return True
    return fuzz.token_set_ratio(_title_key(a.title), _title_key(b.title)) >= TITLE_SIM


def _merit(f: Finding) -> float:
    return (f.confidence + (0.15 if f.source != FindingSource.linter else 0.0)
            + min(len(f.explanation), 400) / 4000 + (0.05 if f.grounded else 0.0)
            + (0.02 if f.verified else 0.0))


def _union_rids(a: list[str], b: list[str]) -> list[str]:
    out = list(a)
    for r in b:
        if r not in out:
            out.append(r)
    return out


def merge_into(keep: Finding, dup: Finding) -> Finding:
    keep.rids = _union_rids(keep.rids, dup.rids)
    if SEV_RANK[dup.severity] < SEV_RANK[keep.severity] and not keep.verified:
        keep.severity = dup.severity
    keep.grounded = keep.grounded or (dup.grounded and quotes_overlap(keep.quote, dup.quote))
    return keep


def pre_dedupe(findings: Sequence[Finding]) -> list[Finding]:
    """Детерминированное слияние очевидных дублей (порядок сохраняется по победителю)."""
    out: list[Finding] = []
    for f in findings:
        f = f.model_copy(deep=True)
        hit = next((i for i, g in enumerate(out) if same_problem(g, f)), None)
        if hit is None:
            out.append(f)
            continue
        g = out[hit]
        if _merit(f) > _merit(g):
            out[hit] = merge_into(f, g)
        else:
            out[hit] = merge_into(g, f)
    return out


def _protected(f: Finding) -> bool:
    return f.source == FindingSource.linter and f.confidence >= PROTECT_CONF and f.grounded


RELATED_PER_CANDIDATE = 2
_GAP = re.compile(r"не\s+(?:указан|задан|определ|описан|приведен|перечислен|заполнен)|отсутству|нет\s", re.I)


def render_candidates(batch: Sequence[Finding], reqs: Optional[Sequence[Requirement]] = None,
                      index: Optional[TermIndex] = None) -> str:
    """Кандидаты для критика. К находкам-«пропускам» добавляются связанные по терминам
    требования: быстрая модель надёжнее замечает, что «пропуск» закрыт в другом месте,
    если улика лежит рядом с кандидатом."""
    rmap = {r.rid: r for r in reqs or []}
    out = []
    for i, f in enumerate(batch):
        block = (
            f"[#{i}] источник: {f.source.value} | критерий: {f.criterion.value} | критичность: "
            f"{f.severity.value} | уверенность: {f.confidence:.2f} | цитата найдена: "
            f"{'да' if f.grounded else 'нет'} | R: {', '.join(f.rids) or '—'}\n"
            f"Заголовок: {f.title}\nЦитата: «{f.quote}»\n"
            f"Пояснение: {' '.join(f.explanation.split())}\n"
            f"Рекомендация: {' '.join(f.recommendation.split())}")
        if index is not None and rmap and f.rids and _GAP.search(f.title + " " + f.explanation[:200]):
            rel: list[str] = []
            for rid in f.rids[:2]:
                rel += index.related(rid, RELATED_PER_CANDIDATE, exclude=set(f.rids) | set(rel))
            rel = [x for x in rel if x in rmap][:RELATED_PER_CANDIDATE]
            if rel:
                block += "\nПроверь, не закрыт ли пропуск здесь: " + " ".join(
                    f"[{x}] «{' '.join(rmap[x].quote.split())[:220]}»" for x in rel)
        out.append(block)
    return "\n".join(out)


def make_batches(findings: Sequence[Finding], size: int = BATCH) -> list[list[int]]:
    order = sorted(range(len(findings)), key=lambda i: (_rid_key(findings[i]), i))
    n = -(-len(order) // size) if order else 0
    if n == 0:
        return []
    k = -(-len(order) // n)
    return [order[i:i + k] for i in range(0, len(order), k)]


def critic_requirements(reqs: Sequence[Requirement], batch: Sequence[Finding],
                        full_limit: int = FULL_REQS) -> list[Requirement]:
    """Требования для критика: упомянутые в кандидатах + связанные по терминам.

    Небольшой документ передаётся целиком (без meta, кроме упомянутых): «пропуск» часто
    закрыт другим разделом, и критик должен это видеть.
    """
    used = {r for f in batch for r in f.rids}
    if len(reqs) <= full_limit:
        return [r for r in reqs if r.kind != ReqKind.meta or r.rid in used]
    index = TermIndex(reqs)
    want = set(used)
    for rid in sorted(used):
        want.update(index.related(rid, 2))
    return [r for r in reqs if r.rid in want]


def _judge(doc: Document, reqs: Sequence[Requirement], llm: LLMClient,
           batch: Sequence[Finding]) -> Optional[CriticResult]:
    ref = critic_requirements(reqs, batch)
    return call(llm, "critic", CriticResult, "critic", doc_name=doc.name,
                candidates=render_candidates(batch, reqs, TermIndex(reqs)), requirements=req_blocks(ref))


def apply_verdicts(batch: list[Finding], result: Optional[CriticResult]) -> list[Finding]:
    """Применить вердикты к пачке; вернуть оставшиеся находки (исходный порядок пачки)."""
    if result is None:
        return list(batch)
    n = len(batch)
    verdicts = {}
    for v in result.verdicts:
        if 0 <= v.index < n and v.index not in verdicts:
            verdicts[v.index] = v
    kept = {i for i, v in verdicts.items() if v.keep}
    # защищённые линтерные находки не выбрасываются (кроме слияния как дубль)
    for i, v in verdicts.items():
        if not v.keep and v.duplicate_of is None and _protected(batch[i]):
            kept.add(i)
    target: dict[int, int] = {}
    for i, v in verdicts.items():
        if i in kept or v.duplicate_of is None:
            continue
        j, seen = v.duplicate_of, {i}
        while j is not None and 0 <= j < n and j not in kept and j not in seen:
            seen.add(j)
            nxt = verdicts.get(j)
            j = nxt.duplicate_of if nxt else None
        if j is not None and 0 <= j < n and j in kept and j != i:
            target[i] = j
        elif _protected(batch[i]):
            kept.add(i)
    out_idx = set()
    for i in range(n):
        f = batch[i]
        v = verdicts.get(i)
        if v is None:
            f.verified = False
            out_idx.add(i)
            continue
        if i in kept:
            if v.keep:
                f.severity = v.severity
                f.confidence = max(0.0, min(1.0, v.confidence))
                f.verified = True
                if f.confidence < MIN_KEEP_CONF and not _protected(f):
                    f.doubtful = True
            out_idx.add(i)
        elif i not in target and f.grounded:
            # Critic отклонил, но цитата подтверждена: замер показал, что на Lite-модели отклонённые
            # находки в большинстве верные — не удаляем, а показываем отдельно как «под сомнением».
            f.severity = v.severity
            f.confidence = max(0.0, min(1.0, v.confidence))
            f.verified = True
            f.doubtful = True
            out_idx.add(i)
    for i, j in target.items():
        merge_into(batch[j], batch[i])
    return [batch[i] for i in range(n) if i in out_idx]


def verify(doc: Document, reqs: list[Requirement], findings: list[Finding], llm: LLMClient) -> list[Finding]:
    """Вернуть отфильтрованные и объединённые находки с verified/confidence."""
    if not findings:
        return []
    cands = pre_dedupe(findings)
    batches = make_batches(cands)
    jobs = [lambda b=b: _judge(doc, reqs, llm, [cands[i] for i in b]) for b in batches]
    results = parallel.run_all(jobs, max_workers=parallel.workers(4), label="critic", default=None)
    survivors: list[Finding] = []
    for b, res in zip(batches, results):
        survivors += apply_verdicts([cands[i] for i in b], res)
    survivors = [f for f in survivors if f.grounded or f.verified]
    # дубли между пачками; один и тот же дефект, повторённый в соседних пунктах, — одна находка
    return aggregate_repeats(pre_dedupe(survivors))


_DIGITS = re.compile(r"\d+")


def _section_of(loc: str) -> str:
    return re.sub(r"\s*\((?:стр|строк)[^)]*\)\s*$", "", loc or "").strip()


def _title_key(t: str) -> str:
    return " ".join(_DIGITS.sub("#", (t or "").lower().replace("ё", "е")).split())


def is_repeat(a: Finding, b: Finding) -> bool:
    """Тот же дефект в соседнем пункте того же раздела (заголовки отличаются только числами)."""
    if a.source == FindingSource.linter or b.source == FindingSource.linter:
        return False
    if a.criterion != b.criterion or a.scope != b.scope:
        return False
    if _section_of(a.location) != _section_of(b.location) or not _section_of(a.location):
        return False
    ka, kb = _title_key(a.title), _title_key(b.title)
    return ka == kb or (min(len(ka), len(kb)) >= 20 and fuzz.ratio(ka, kb) >= 92)


def aggregate_repeats(findings: Sequence[Finding]) -> list[Finding]:
    out: list[Finding] = []
    extra: dict[int, list[Finding]] = {}
    for f in findings:
        hit = next((i for i, g in enumerate(out) if is_repeat(g, f)), None)
        if hit is None:
            out.append(f)
        else:
            extra.setdefault(hit, []).append(f)
    for i, more in extra.items():
        g = out[i]
        for m in more:
            g.rids = _union_rids(g.rids, m.rids)
            if SEV_RANK[m.severity] < SEV_RANK[g.severity]:
                g.severity = m.severity
            g.confidence = max(g.confidence, m.confidence)
            g.verified = g.verified or m.verified
        refs = "; ".join(f"{', '.join(m.rids) or '—'} («{m.quote[:120]}»)" for m in more)
        g.explanation = g.explanation.rstrip() + f" Тот же дефект повторяется: {refs}."
        g.recommendation = g.recommendation.rstrip() + " Аналогично исправить: " +             ", ".join(r for m in more for r in m.rids) + "."
        g.title = f"{g.title} (+{len(more)} аналогичн.)"
    return out
