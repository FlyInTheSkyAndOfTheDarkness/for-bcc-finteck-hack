import time

from reqcheck.grounding import GROUND_THRESHOLD, find_quote, normalize
from tests.helpers_blocks import doc_from_md, load_input

MD = """# Документ

## Часть A. Тест

### Функциональные требования
* **Ф-1.** Держатель карты задаёт порог от 1 000 до 5 000 ₸ «включительно» — по умолчанию выключено.
* **Ф-2.** Ёмкость очереди не ограничена.

| Поле | Значение |
| :--- | :--- |
| **Метод** | POST |
"""


def test_normalize_variants():
    assert normalize("**Ф-1.**  Порог —  1 000 тг") == normalize("ф-1. порог - 1000 ₸")
    assert normalize("«ёлка»") == normalize('"елка"')


def test_exact_and_normalized():
    doc = doc_from_md(MD)
    s, b = find_quote(doc, "Держатель карты задаёт порог")
    assert s == 100 and b is not None and b.text.startswith("Ф-1.")
    # разметка, кавычки, тире, ё, неразрывные пробелы, разряды
    s, b = find_quote(doc, '**Ф-1.** держатель карты задает порог от 1000 до 5 000 тг "включительно" - по')
    assert s == 100 and b.text.startswith("Ф-1.")
    s, b = find_quote(doc, "Емкость очереди не ограничена")
    assert s == 100 and b.text.startswith("Ф-2.")


def test_table_row_and_cross_block():
    doc = doc_from_md(MD)
    s, b = find_quote(doc, "Метод | POST")
    assert s == 100 and b.kind.value == "table_row"
    s, b = find_quote(doc, "по умолчанию выключено. Ф-2. Ёмкость очереди")
    assert s >= GROUND_THRESHOLD and b.text.startswith("Ф-1.")


def test_fuzzy_and_rejects():
    doc = doc_from_md(MD)
    s, _ = find_quote(doc, "Держатель карты задает порог от 1 000 до 5 000 ₸ включительно, по умолчанию выключено")
    assert s >= GROUND_THRESHOLD
    s, _ = find_quote(doc, "Система отправляет SMS клиенту каждые десять минут")
    assert s < GROUND_THRESHOLD
    # реальный блок + выдуманный хвост не должен считаться подтверждённым
    s, _ = find_quote(doc, "Метод | POST. Дополнительно сервис обязан шифровать все поля алгоритмом ГОСТ")
    assert s < GROUND_THRESHOLD
    assert find_quote(doc, "") == (0.0, None)


def test_ellipsis_quote():
    doc = doc_from_md(MD)
    s, b = find_quote(doc, "Держатель карты задаёт порог … по умолчанию выключено")
    assert s == 100 and b.text.startswith("Ф-1.")
    s, _ = find_quote(doc, "Держатель карты задаёт порог … выдуманное продолжение текста")
    assert s < GROUND_THRESHOLD


def test_speed_on_input():
    doc = load_input()
    quotes = [b.text[3:60] for b in doc.blocks if len(b.text) > 20][:60]
    quotes += ["Выдуманная цитата номер %d про лимиты и комиссии" % i for i in range(40)]
    t = time.time()
    res = [find_quote(doc, q) for q in quotes]
    assert time.time() - t < 10
    assert all(s >= GROUND_THRESHOLD for s, _ in res[:60])
    assert sum(s >= GROUND_THRESHOLD for s, _ in res[60:]) == 0
