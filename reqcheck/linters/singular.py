"""Единичность (ISO/IEC/IEEE 29148 §5.2.5 «singular»): один пронумерованный пункт — одно требование.

Пункт с собственным идентификатором, в котором несколько самостоятельных утверждений
(предложений со своим сказуемым), трудно трассировать и принимать по частям: тест-кейс и
задача ссылаются на «Ф-2», а внутри него четыре разных правила. Все такие пункты
собираются в ОДНО замечание по набору, чтобы не засорять отчёт.
"""
from __future__ import annotations

import re

from reqcheck.linters.common import Ctx
from reqcheck.models import Criterion, ReqKind, Severity

KINDS = {ReqKind.functional, ReqKind.nonfunctional, ReqKind.data, ReqKind.integration, ReqKind.business}
# граница предложения: точка/!/? + пробел + заглавная буква (не сокращения вида «т. е.», «1 000 ₸.»)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ«\"])")
ID_HEAD = re.compile(r"^\s*[A-Za-zА-Яа-яЁё]{1,6}[-‑–]?\d+(?:\.\d+)*[.)]?\s+")
MIN_WORDS = 3


def _statements(text: str) -> list[str]:
    body = ID_HEAD.sub("", text.strip())
    parts = [p.strip() for p in SENT_SPLIT.split(body) if p.strip()]
    return [p for p in parts if len(p.split()) >= MIN_WORDS]


def run(ctx: Ctx) -> list:
    hits = []
    for r in ctx.reqs:
        if r.kind in KINDS and r.doc_id and r.block_ids:
            n = len(_statements(r.quote))
            if n >= 2:
                hits.append((r, n))
    if not hits:
        return []
    hits.sort(key=lambda t: -t[1])
    first, n_max = hits[0]
    listing = "; ".join(f"{r.doc_id} — {n} утв." for r, n in hits)
    return [ctx.finding(
        criterion=Criterion.singular, severity=Severity.minor, scope="set",
        title="Составные требования: несколько утверждений в одном пункте",
        quote=first.quote, block=ctx.by_id[first.block_ids[0]], rids=[r.rid for r, _ in hits],
        explanation=(f"Пункты с одним идентификатором содержат по нескольку самостоятельных утверждений "
                     f"({listing}). По ISO/IEC/IEEE 29148 §5.2.5 требование должно быть единичным: "
                     "иначе тест-кейс, задача и решение о приёмке ссылаются на пункт целиком, а не на "
                     "конкретное правило, и частичное выполнение нельзя зафиксировать."),
        recommendation=(f"Разбейте пункты на атомарные требования с собственными номерами "
                        f"(например, {first.doc_id}.1, {first.doc_id}.2 …), по одному утверждению в каждом."),
        confidence=0.9)]
