"""Стадия 3. Детерминированные проверки без LLM. Владелец: A3.

Модули:
  placeholders — незаполненный шаблон, прочерки, пустые таблицы, заглушки, версия/дата;
  vague        — слабые/неоднозначные слова (lexicon/vague_ru.yaml);
  units        — денежные единицы (lexicon/units.yaml);
  api          — HTTP-метод, данные в query, коды ответа, тело запроса, типы параметров;
  ids          — дубли/пропуски нумерации, требования без идентификаторов.
Каждая находка — source=linter, с дословной цитатой из блока и локатором.
"""
from __future__ import annotations

import logging

from reqcheck.models import Document, Finding, Requirement

log = logging.getLogger(__name__)


def run_linters(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    """Вернуть находки source=linter, с точной цитатой и локатором."""
    from reqcheck.linters import api, ids, placeholders, units, vague
    from reqcheck.linters.common import Ctx

    if not doc.blocks:
        return []
    ctx = Ctx(doc, reqs)
    found: list[Finding] = []
    for mod in (placeholders, vague, units, api, ids):
        try:
            found += mod.run(ctx)
        except Exception as e:  # noqa: BLE001 — один сломанный линтер не должен ронять прогон
            log.warning("linter %s упал: %s", mod.__name__, e)
    out, seen = [], set()
    for f in found:
        key = (f.title, f.quote)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
