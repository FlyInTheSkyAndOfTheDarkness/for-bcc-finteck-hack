"""Слабые / неоднозначные слова в формулировках требований (лексикон lexicon/vague_ru.yaml)."""
from __future__ import annotations

import re
from functools import lru_cache

from reqcheck.linters.common import Ctx, lexicon, sentence_at, short, wb
from reqcheck.models import BlockKind, Criterion, Finding, ReqKind, Severity

STRICT_KINDS = {ReqKind.functional, ReqKind.nonfunctional, ReqKind.acceptance, ReqKind.api,
                ReqKind.integration, ReqKind.data}


@lru_cache(maxsize=1)
def _terms() -> list[tuple[re.Pattern, dict]]:
    out = []
    for t in lexicon("vague_ru.yaml")["terms"]:
        out.append((re.compile(wb(t["pattern"]), re.I), t))
    return out


def run(ctx: Ctx) -> list[Finding]:
    out = []
    for b in ctx.req_blocks():
        if b.kind in (BlockKind.code, BlockKind.heading, BlockKind.table_header):
            continue
        text = b.text
        hits = []
        for rx, t in _terms():
            for m in rx.finditer(text):
                hits.append((m, t))
        if not hits:
            continue
        hits.sort(key=lambda h: h[0].start())
        m0, t0 = hits[0]
        quote = sentence_at(text, m0.start())
        rids = ctx.rids_for_quote(b, quote)
        kinds = {ctx.req_by_rid[r].kind for r in rids if r in ctx.req_by_rid}
        verif = any(t["class"] == "verifiable" for _, t in hits)
        crit = Criterion.verifiable if verif else Criterion.unambiguous
        sev = Severity.major if (verif and kinds & {ReqKind.functional, ReqKind.nonfunctional,
                                                    ReqKind.acceptance}) else Severity.minor
        words = []
        for m, t in hits:
            w = m.group(0)
            if w.casefold() not in [x.casefold() for x in words]:
                words.append(w)
        listed = ", ".join(f"«{w}»" for w in words)
        hints = "; ".join(dict.fromkeys(f"«{m.group(0)}» — {t['hint']}" for m, t in hits))
        out.append(ctx.finding(
            criterion=crit, severity=sev,
            title=f"Неточная формулировка: {listed}"[:90],
            quote=quote, block=b, rids=rids,
            explanation=(f"Формулировка «{short(quote, 120)}» содержит {listed} — "
                         + ("характеристику, которую нельзя объективно проверить без числовой границы"
                            if verif else "слова, допускающие разное толкование (открытый перечень, оговорка)")
                         + ". ISO/IEC/IEEE 29148 (§5.2.7) относит такие слова к недопустимым в требованиях."),
            recommendation=f"Переформулируйте: {hints}.",
            confidence=0.85 if not (kinds & STRICT_KINDS) else 0.88))
    return out
