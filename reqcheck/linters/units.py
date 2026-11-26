"""Согласованность денежных единиц: одна и та же денежная величина в минорных и основных единицах."""
from __future__ import annotations

import re
from functools import lru_cache

from reqcheck.linters.common import Ctx, lexicon, short
from reqcheck.models import Block, BlockKind, Criterion, Finding, Severity


@lru_cache(maxsize=1)
def _patterns() -> tuple[re.Pattern, re.Pattern]:
    m = lexicon("units.yaml")["money"]
    minor = "|".join(m["minor"])
    major = "|".join(m["major"])
    # явное указание единицы: «в тиынах», «в тенге», «(₸)», «, ₸» в заголовке поля, «сумма в KZT»
    lead = r"(?<![а-яёa-z])(?:в|во|in)\s+"
    rx_minor = re.compile(rf"{lead}(?:{minor})(?![а-яёa-z])", re.I)
    rx_major = re.compile(rf"(?:{lead}(?:{major})(?![а-яёa-z])|[(,]\s*(?:₸|kzt|тенге)\s*[)»\"])", re.I)
    return rx_minor, rx_major


def _scan(b: Block, rx: re.Pattern) -> list[re.Match]:
    return list(rx.finditer(b.text))


def run(ctx: Ctx) -> list[Finding]:
    rx_minor, rx_major = _patterns()
    minor_hits: list[tuple[Block, re.Match]] = []
    major_hits: list[tuple[Block, re.Match]] = []
    for b in ctx.req_blocks():
        if b.kind in (BlockKind.code, BlockKind.heading):
            continue
        minor_hits += [(b, m) for m in _scan(b, rx_minor)]
        major_hits += [(b, m) for m in _scan(b, rx_major)]
    if not minor_hits or not major_hits:
        return []
    mb, mm = minor_hits[0]
    same = [(b, m) for b, m in major_hits if b.id == mb.id]
    if same:
        bm = same[0][1]
        s, e = min(mm.start(), bm.start()), max(mm.end(), bm.end())
        # расширяем до начала слова-объекта слева (например, «сумму покупки в тиынах»)
        left = mb.text.rfind(",", 0, s)
        left = 0 if left < 0 else left + 1
        if s - left > 60:
            left = s - 25
            sp = mb.text.find(" ", left)
            left = sp + 1 if 0 <= sp < s else left
        quote = mb.text[left:e].strip()
    else:
        quote = mb.text[max(0, mm.start() - 40):mm.end()].strip()
        sp = quote.find(" ")
        if mm.start() >= 40 and sp > 0:
            quote = quote[sp + 1:]
    others = []
    for b, m in major_hits:
        if b.id != mb.id and b.id not in [x.id for x in others]:
            others.append(b)
    where = "; ".join(f"{b.locator()}: «{short(b.text, 70)}»" for b in others[:4])
    minor_u = mm.group(0).strip()
    major_u = (same[0][1] if same else major_hits[0][1]).group(0).strip(" ,()")
    return [ctx.finding(
        criterion=Criterion.set_consistent, severity=Severity.major, scope="set",
        title=f"Денежные суммы в разных единицах: «{minor_u}» и «{major_u}»",
        quote=quote, block=mb, extra_blocks=others,
        explanation=(f"Денежные величины задаются в разных единицах: «{minor_u}» (минорные единицы) "
                     f"и «{major_u}» (основные)" + (f"; основные единицы также в: {where}" if where else "") +
                     ". Сравнение накопленной суммы с порогом и передача значений между системами "
                     "требуют явного правила пересчёта; без него возможна ошибка в 100 раз."),
        recommendation=("Выберите единую единицу хранения и передачи денежных сумм (как правило, минорные "
                        "единицы — целое число) и укажите её во всех требованиях, таблицах данных и API; "
                        "опишите правило пересчёта и округления для отображения."),
        confidence=0.9)]
