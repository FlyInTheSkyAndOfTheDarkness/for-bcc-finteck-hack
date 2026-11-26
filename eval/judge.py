"""LLM-судья для сопоставления находок отчёта с gold: один пакетный вызов на отчёт."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from eval.gold import GoldItem

if TYPE_CHECKING:
    from eval.evaluate import FView, Pair


class JudgeLink(BaseModel):
    fid: str = Field(description="идентификатор находки из списка FINDINGS")
    gold_ids: list[str] = Field(default_factory=list,
                                description="ID дефектов GOLD, которые находка описывает; пусто — ни одного")
    note: str = Field(default="", description="кратко, почему так")


class JudgeResult(BaseModel):
    links: list[JudgeLink] = Field(default_factory=list)


SYSTEM = """Ты — эксперт по ревью требований (ISO/IEC/IEEE 29148, IEEE 830). Тебе дан эталонный список
дефектов документа (GOLD) и находки автоматического ревьюера (FINDINGS). Для каждой находки укажи,
какие дефекты GOLD она описывает.

Правила:
- Находка соответствует дефекту GOLD, только если она указывает на то же место документа И на ту же
  суть проблемы (то же противоречие, тот же пропуск, та же неоднозначность). Совпадения раздела мало.
- Формулировка и критерий могут отличаться, если суть та же.
- Одна находка может соответствовать нескольким GOLD, только если явно описывает каждую из проблем.
- Если находка не соответствует ни одному GOLD — gold_ids пустой (это не значит, что она ложная).
- Подсказки-кандидаты получены лексическим поиском и могут быть неверны.
- Верни запись для КАЖДОЙ находки. Используй только ID из списков."""


def build_user(findings: list["FView"], gold: list[GoldItem], hints: dict[int, list["Pair"]] | None) -> str:
    g = [{"id": x.id, "title": x.title, "section": x.section, "quotes": x.quotes,
          "criterion": x.criterion.value, "description": x.description} for x in gold]
    f = []
    for x in findings:
        item = {"fid": x.label, "title": x.title, "criterion": x.criterion, "quote": x.quote,
                "location": x.location, "explanation": x.explanation[:600]}
        if hints and hints.get(x.idx):
            item["candidates"] = [p.g for p in hints[x.idx][:3]]
        f.append(item)
    return ("GOLD:\n" + json.dumps(g, ensure_ascii=False, indent=1)
            + "\n\nFINDINGS:\n" + json.dumps(f, ensure_ascii=False, indent=1))


def match_judge(findings: list["FView"], gold: list[GoldItem], client,
                hints: dict[int, list["Pair"]] | None = None) -> list["Pair"]:
    from eval.evaluate import Pair

    if not findings:
        return []
    res = client.generate_json(SYSTEM, build_user(findings, gold, hints), JudgeResult,
                               tag="eval.judge", temperature=0.0)
    by_label = {f.label: f for f in findings}
    valid = {g.id for g in gold}
    pairs, seen = [], set()
    for link in res.links:
        f = by_label.get(link.fid.strip())
        if f is None:
            continue
        for gid in link.gold_ids:
            gid = gid.strip()
            if gid in valid and (f.idx, gid) not in seen:
                seen.add((f.idx, gid))
                pairs.append(Pair(f.idx, gid, 100.0, 100.0, "judge"))
    return pairs
