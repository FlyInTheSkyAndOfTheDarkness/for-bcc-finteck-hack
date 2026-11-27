import pytest

from reqcheck.grounding import GROUND_THRESHOLD, find_quote
from reqcheck.linters import run_linters
from reqcheck.models import Criterion, FindingSource, Severity
from reqcheck.segment import segment
from tests.helpers_blocks import doc_from_md, load_input


@pytest.fixture(scope="module")
def doc():
    return load_input()


@pytest.fixture(scope="module")
def findings(doc):
    return run_linters(doc, segment(doc, None))


def _has(findings, crit, *words):
    return [f for f in findings if f.criterion == crit and all(w.lower() in (f.title + f.explanation).lower()
                                                                for w in words)]


def test_count_and_grounding(doc, findings):
    assert len({f.title for f in findings}) >= 10
    for f in findings:
        assert f.source == FindingSource.linter
        assert f.quote and find_quote(doc, f.quote)[0] >= GROUND_THRESHOLD, f.quote
        assert f.location and f.explanation and f.recommendation
        assert 0.8 <= f.confidence <= 0.99
        assert any("а" <= ch <= "я" for ch in f.title.lower())          # русский текст


def test_expected_findings_on_input(findings):
    assert _has(findings, Criterion.correct, "GET")                                   # GET для сохранения
    assert _has(findings, Criterion.appropriate, "строке")                            # данные карты в query
    assert _has(findings, Criterion.set_consistent, "тиын")                           # единицы денег
    assert _has(findings, Criterion.correct, "Created")                               # 200 ≠ Created
    assert _has(findings, Criterion.set_consistent, "403", "404")                     # 403 → 404
    assert _has(findings, Criterion.set_complete, "201")                              # 201 без описания
    assert _has(findings, Criterion.set_consistent, "пример", "ошибк")                # ERROR под 200
    assert _has(findings, Criterion.set_consistent, "Content-Type")
    assert len(_has(findings, Criterion.set_consistent, "в запросе", "в ответе")) == 2  # amount, enabled
    assert _has(findings, Criterion.conforming, "Да / Нет")                           # чек-лист шаблона
    assert _has(findings, Criterion.conforming, "согласован")                         # пустые согласующие
    assert _has(findings, Criterion.modifiable, "верси")
    assert _has(findings, Criterion.traceable, "идентификатор")
    # одна находка на таблицу, а не на ячейку
    assert len([f for f in findings if "Да / Нет" in f.title]) == 1


def test_no_false_positive_on_precise_bounds(findings):
    quotes = " ".join(f.quote for f in findings)
    assert "максимум один" not in quotes
    assert not any("Некорректный" in f.quote for f in findings)
    assert not any(f.title.startswith("Неточная") and "Dev / Prod" in f.quote for f in findings)


CLEAN = """# Блокировка карты

Версия 1.2 от 01.03.2025.

## Функциональные требования
* **ФТ-1.** Клиент блокирует карту в приложении; статус карты меняется на «Заблокирована» не позднее 2 секунд.
* **ФТ-2.** Сумма комиссии хранится в тиынах как целое число.

## Нефункциональные требования
* **НФТ-1.** Время ответа метода не более 500 мс для 95% запросов.

## Критерии приемки
| ID | Условие | Действие | Результат |
| --- | --- | --- | --- |
| КП-1 | Карта активна | Заблокировать | Статус «Заблокирована» |

## Блок согласующих
| Подразделение | ФИО | Подпись |
| --- | --- | --- |
| ИБ | Иванов И. И. | согласовано |

## Часть B. REST API

### Общие параметры
| Поле | Значение |
| --- | --- |
| Description | Блокировка карты |
| REST type | POST |
| Request URL | /cards/block |
| Authorization | Bearer |

### Параметры запроса
| Parameter | Format | Description |
| --- | --- | --- |
| cardToken | string | Токен карты |

### Ответ
| Parameter | Format | Description |
| --- | --- | --- |
| status | string | Статус карты |

### Статусы
**200.** Ответ в случае успешной блокировки.
**400.** Ответ в случае неверных параметров.

#### 200. OK
```json
{"status":"BLOCKED"}
```

#### 400. Bad Request
```json
{"message":"Некорректный запрос"}
```
"""


def test_clean_document_has_no_findings():
    d = doc_from_md(CLEAN)
    fs = run_linters(d, segment(d, None))
    assert fs == [], [f.title for f in fs]


DEFECTS = """# Удаление шаблона платежа

## Функциональные требования
* **ПТ-1.** Сервис быстро удаляет шаблон платежа и т.д.
* **ПТ-3.** Сумма платежа хранится в копейках.
* **ПТ-3.** Лимит отображается в рублях.

## Часть B. REST API

### Общие параметры
| Поле | Значение |
| --- | --- |
| Description | Удаление шаблона |
| Method | GET |
| URL | /templates/removeTemplate |
| Swagger | — |

### Query-параметры
| Parameter | Format | Description |
| --- | --- | --- |
| accountNumber | string | Номер счёта клиента |
| amount | number | Сумма |

### Ответ
| Parameter | Format | Description |
| --- | --- | --- |
| amount | string | Сумма |

### Статусы
**200.** Успех.

#### 200. Successful — No Content
```json
{"status":"error"}
```
"""


def test_generic_defects_other_domain():
    d = doc_from_md(DEFECTS)
    fs = run_linters(d, segment(d, None))
    titles = " | ".join(f.title for f in fs)
    for f in fs:
        assert find_quote(d, f.quote)[0] >= GROUND_THRESHOLD
    assert "GET" in titles                                   # GET + remove
    assert "строке запроса" in titles                        # номер счёта в query
    assert "быстро" in titles and "и т.д." in titles         # слабые слова одним замечанием
    assert "копейках" in titles                              # копейки vs рубли
    assert "No Content" in titles                            # 200 ≠ No Content (204)
    assert "Пример ответа 200" in titles
    assert "ПТ-3 повторяется" in titles and "ПТ-2" in titles  # дубль и пропуск нумерации
    assert "amount" in titles                                # number vs string
    assert "Swagger" in " ".join(f.explanation for f in fs)
    sev = {f.title: f.severity for f in fs}
    assert sev[next(t for t in sev if t.startswith("GET"))] == Severity.major


def test_tracker_field_without_issue_key():
    from reqcheck.linters import placeholders
    from reqcheck.linters.common import Ctx
    from tests.helpers_blocks import doc_from_md
    md = ("# Д\n\n| Поле | Значение |\n| :--- | :--- |\n| **Задача в JIRA** | Практикум без задачи |\n"
          "| **Продукт** | Банк |\n| **Тип** | Новый |\n")
    doc = doc_from_md(md)
    fs = placeholders.check_tracker(Ctx(doc, []))
    assert len(fs) == 1 and "ключа задачи" in fs[0].title and fs[0].grounded
    ok = doc_from_md(md.replace("Практикум без задачи", "PAY-1234"))
    assert placeholders.check_tracker(Ctx(ok, [])) == []
