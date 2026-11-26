"""Гигиена идентификаторов: дубли и пропуски в нумерации, требования без идентификаторов."""
from __future__ import annotations

import re
from collections import defaultdict

from reqcheck.linters.common import Ctx, short
from reqcheck.models import Criterion, Finding, ReqKind, Severity

ID_PARTS = re.compile(r"^(?P<prefix>.+?)[-‑–](?P<num>\d+(?:\.\d+)*)$")
NEEDS_ID = {ReqKind.functional, ReqKind.nonfunctional, ReqKind.data, ReqKind.integration,
            ReqKind.acceptance, ReqKind.ui, ReqKind.business}


def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    groups: dict[str, list] = defaultdict(list)
    for r in ctx.reqs:
        if r.doc_id:
            m = ID_PARTS.match(r.doc_id)
            if m:
                groups[m.group("prefix")].append((m.group("num"), r))
    for prefix, items in groups.items():
        seen: dict[str, object] = {}
        for num, r in items:
            if num in seen:
                first = seen[num]
                b = ctx.by_id[r.block_ids[0]]
                out.append(ctx.finding(
                    criterion=Criterion.traceable, severity=Severity.major, scope="set",
                    title=f"Идентификатор {r.doc_id} повторяется",
                    quote=r.quote, block=b, rids=sorted([first.rid, r.rid]),
                    explanation=(f"Идентификатор {r.doc_id} присвоен двум разным требованиям "
                                 f"({first.rid}: «{short(first.summary, 50)}» и {r.rid}: «{short(r.summary, 50)}»). "
                                 "Ссылки из тестов, задач и других документов станут неоднозначными."),
                    recommendation="Присвойте каждому требованию уникальный идентификатор.",
                    confidence=0.95))
            else:
                seen[num] = r
        simple = sorted({int(n) for n, _ in items if n.isdigit()})
        if len(simple) >= 2:
            gaps = [n for n in range(simple[0], simple[-1] + 1) if n not in simple]
            # «КП-1 … КП-2024» — это не нумерация с пропусками, а разные коды; не шумим
            if gaps and len(gaps) <= max(10, len(simple)):
                after = next(r for n, r in items if n.isdigit() and int(n) > gaps[0])
                b = ctx.by_id[after.block_ids[0]]
                miss = ", ".join(f"{prefix}-{g}" for g in gaps)
                out.append(ctx.finding(
                    criterion=Criterion.traceable, severity=Severity.minor, scope="set",
                    title=f"Пропуск в нумерации: нет {miss}"[:90],
                    quote=after.quote, block=b, rids=[after.rid],
                    explanation=(f"В нумерации требований с префиксом «{prefix}» пропущены {miss}. "
                                 "Неясно, удалено ли требование или потеряно при редактировании."),
                    recommendation="Восстановите пропущенные пункты или отметьте их как удалённые.",
                    confidence=0.85))

    unnumbered = [r for r in ctx.reqs if r.kind in NEEDS_ID and not r.doc_id]
    if unnumbered:
        has_ids = any(r.doc_id for r in ctx.reqs)
        sections = []
        for r in unnumbered:
            b = ctx.by_id[r.block_ids[0]]
            s = b.section_path[-1] if b.section_path else "—"
            if s not in sections:
                sections.append(s)
        first = unnumbered[0]
        b = ctx.by_id[first.block_ids[0]]
        body = re.sub(r"^\s*\d+(?:\.\d+)*[.)]\s+", "", first.quote, count=1) or first.quote
        quote = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0]
        listed = ", ".join(f"«{s}»" for s in sections[:8])
        out.append(ctx.finding(
            criterion=Criterion.traceable, severity=Severity.minor, scope="set",
            title=("Требования без идентификаторов" if has_ids
                   else "В документе нет идентификаторов требований"),
            quote=quote, block=b, rids=[r.rid for r in unnumbered],
            explanation=((f"Нумерация есть только у части требований, а {len(unnumbered)} требований "
                          if has_ids else f"Ни одно из {len(unnumbered)} требований ") +
                         f"в разделах {listed} не имеют собственных идентификаторов. На них нельзя "
                         "сослаться из тест-кейсов, задач и критериев приёмки (IEEE 830 §4.3.8, трассируемость)."),
            recommendation=("Присвойте уникальные идентификаторы всем требованиям (например, НФТ-1, ДАН-1, "
                            "ИНТ-1, КП-1) и используйте их в критериях приёмки и API-спецификации."),
            confidence=0.85))
    return out
