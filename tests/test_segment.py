import pytest

from reqcheck.grounding import GROUND_THRESHOLD, find_quote
from reqcheck.llm import FakeLLM, LLMError
from reqcheck.models import BlockKind, ReqKind
from reqcheck.segment import explicit_id, segment
from tests.helpers_blocks import doc_from_md, load_input


@pytest.fixture(scope="module")
def doc():
    return load_input()


@pytest.fixture(scope="module")
def reqs(doc):
    return segment(doc, FakeLLM())


def _part(doc, r):
    return doc.blocks[[b.id for b in doc.blocks].index(r.block_ids[0])].part or ""


def test_count_and_numbering(reqs):
    assert 30 <= len(reqs) <= 55
    assert [r.rid for r in reqs] == [f"R-{i:02d}" for i in range(1, len(reqs) + 1)]


def test_all_grounded_and_located(doc, reqs):
    for r in reqs:
        assert r.grounded, r
        assert find_quote(doc, r.quote)[0] >= GROUND_THRESHOLD
        assert r.location and r.block_ids
        assert r.summary and len(r.summary.rstrip("…").split()) <= 15


def test_both_parts_covered(doc, reqs):
    parts = {_part(doc, r)[:7] for r in reqs}
    assert "Часть A" in parts and "Часть B" in parts
    kinds = {r.kind for r in reqs}
    assert {ReqKind.functional, ReqKind.acceptance, ReqKind.api, ReqKind.meta, ReqKind.nonfunctional,
            ReqKind.data, ReqKind.integration} <= kinds


def test_explicit_ids_preserved_and_not_split(reqs):
    ids = [r.doc_id for r in reqs if r.doc_id]
    assert ids[:4] == ["Ф-1", "Ф-2", "Ф-3", "Ф-4"]
    f2 = next(r for r in reqs if r.doc_id == "Ф-2")
    # пункт не дробится на предложения: неатомарность — дефект для ревьюера
    assert f2.quote.count(".") >= 4 and f2.kind == ReqKind.functional


def test_acceptance_rows_separate(doc, reqs):
    acc = [r for r in reqs if r.kind == ReqKind.acceptance]
    assert len(acc) == 3
    by_id = {b.id: b for b in doc.blocks}
    assert all(len(r.block_ids) == 1 and by_id[r.block_ids[0]].kind == BlockKind.table_row for r in acc)
    assert len({r.block_ids[0] for r in acc}) == 3


def test_meta_tables_aggregated(doc, reqs):
    meta = [r for r in reqs if r.kind == ReqKind.meta]
    assert 2 <= len(meta) <= 6
    # строки чек-листа ревью не становятся отдельными требованиями
    by_id = {b.id: b for b in doc.blocks}
    review_rows = [b.id for b in doc.blocks if b.kind == BlockKind.table_row and "Да / Нет" in b.text]
    holders = {r.rid for r in reqs for bid in r.block_ids if bid in review_rows}
    assert len(holders) == 1
    assert all(by_id[b].kind != BlockKind.heading for r in reqs for b in r.block_ids)


def test_api_units(reqs):
    api = [r for r in reqs if r.kind == ReqKind.api]
    assert len(api) >= 15
    status = [r for r in api if r.summary.startswith("Код ")]
    assert len({r.summary.split(":")[0] for r in status}) >= 4   # каждый код — отдельное требование
    params = [r for r in api if r.summary.startswith("Параметр ")]
    assert len(params) >= 5


def test_explicit_id_regex():
    assert explicit_id("Ф-1. Текст") == "Ф-1"
    assert explicit_id("НФТ-12: Текст") == "НФТ-12"
    assert explicit_id("FR-3) text") == "FR-3"
    assert explicit_id("BR-1.2 Правило") == "BR-1.2"
    assert explicit_id("REQ-12 text") == "REQ-12"
    assert explicit_id("1. Проверить сессию") is None
    assert explicit_id("UTF-8 кодировка") is None
    assert explicit_id("Content-Type: application/json") is None


GENERIC = """# Перевод между своими счетами

## Бизнес-требования
Клиент переводит деньги между своими счетами без комиссии.

## Функциональные требования
1. ФТ-1. Система проверяет остаток на счёте списания.
2. ФТ-2. Перевод исполняется в течение 5 секунд.

## Нефункциональные требования
- НФТ-1. Доступность сервиса не ниже 99,9% в месяц.

## Критерии приемки
| ID | Условие | Действие | Результат |
| --- | --- | --- | --- |
| КП-1 | Остаток 100 | Перевести 50 | Остаток 50 |
| КП-2 | Остаток 10 | Перевести 50 | Отказ |

## Прочее
Сервис обязан вести журнал всех операций.

Просто текст без обязательств.
"""


def test_generic_template():
    d = doc_from_md(GENERIC)
    rs = segment(d, None)
    ids = [r.doc_id for r in rs if r.doc_id]
    assert ids == ["ФТ-1", "ФТ-2", "НФТ-1", "КП-1", "КП-2"]
    kinds = [r.kind for r in rs]
    assert kinds.count(ReqKind.acceptance) == 2 and ReqKind.business in kinds
    assert any("журнал" in r.quote for r in rs)                  # модальное «обязан» в прочем разделе
    assert not any("Просто текст" in r.quote for r in rs)
    assert all(r.grounded for r in rs)


def test_llm_refine_improves_and_adds():
    md = GENERIC.replace("Просто текст без обязательств.", "\n\nПри сбое перевод откатывается целиком.")
    d = doc_from_md(md)

    def handler(system, user, schema):
        assert "Кандидаты" in user and "ФТ-1" in user
        return {"requirements": [
            {"doc_id": "ФТ-1", "kind": "functional", "summary": "Проверка остатка счёта списания", "quote": "ФТ-1. Система проверяет остаток на счёте списания."},
            {"doc_id": None, "kind": "functional", "summary": "Откат перевода при сбое", "quote": "При сбое перевод откатывается целиком."},
            {"doc_id": None, "kind": "functional", "summary": "Выдумка", "quote": "Сервис начисляет кэшбэк 5% на все переводы клиентам."},
            {"doc_id": None, "kind": "meta", "summary": "Дубль", "quote": "Система проверяет остаток на счёте списания"},
        ]}

    llm = FakeLLM({"segment": handler})
    base = segment(d, None)
    rs = segment(d, llm)
    assert llm.stats.calls == 1
    assert next(r for r in rs if r.doc_id == "ФТ-1").summary == "Проверка остатка счёта списания"
    assert any(r.quote.startswith("При сбое") for r in rs)           # добавлено с подтверждённой цитатой
    assert not any("кэшбэк" in r.quote for r in rs)                  # выдумка отброшена
    assert len(rs) == len(base) + 1                                  # ничего не удалено, дубль не добавлен
    assert all(r.grounded for r in rs)
    assert [r.rid for r in rs] == [f"R-{i:02d}" for i in range(1, len(rs) + 1)]


def test_llm_failure_keeps_rules():
    d = doc_from_md(GENERIC)

    def boom(system, user, schema):
        raise LLMError("quota")

    rs = segment(d, FakeLLM({"segment": boom}))
    assert [r.quote for r in rs] == [r.quote for r in segment(d, None)]


def test_numbered_headings_and_paragraph_ids():
    md = """# ТЗ

## Раздел 3. Функциональные требования
FR-1. The system shall lock the card.

FR-2. Система отправляет SMS о блокировке.

## 4. Требования к данным
Хранить дату блокировки.
"""
    rs = segment(doc_from_md(md), None)
    assert [(r.doc_id, r.kind) for r in rs] == [("FR-1", ReqKind.functional), ("FR-2", ReqKind.functional),
                                                (None, ReqKind.data)]
