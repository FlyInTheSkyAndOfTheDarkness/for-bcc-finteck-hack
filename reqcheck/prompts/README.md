# Промпты reqcheck

Все промпты — на русском, в markdown. Каждый файл имеет одну и ту же структуру:
роль → входные данные → метод → правила → критичность → формат ответа (поле за полем по `schemas.py`) → few-shot пример из другой финтех-области → `## ДАННЫЕ` с плейсхолдерами.

## Файлы и стадии

| Файл | Стадия (`pipeline.py`) | Модуль | Схема ответа | tag |
| :--- | :--- | :--- | :--- | :--- |
| `_common.md` | все (префикс system) | — | — | — |
| `segment_refine.md` | segment | `segment.py` | `SegResult` | `segment` (владелец другой агент) |
| `review_requirement.md` | review | `review.py` | `FindingDrafts` | `review` |
| `set_contradictions.md` | set | `setreview.py` | `FindingDrafts` | `set.contradictions` |
| `set_scenarios.md` | set | `setreview.py` | `ScenarioChecks` | `set.scenarios` |
| `set_completeness.md` | set | `setreview.py` | `FindingDrafts` | `set.completeness` |
| `set_summary.md` | set (лучше — после critic, см. ниже) | `setreview.py` | `SetSummaryDraft` | `set.summary` |
| `critic.md` | critic | `critic.py` | `CriticResult` | `critic` |
| `clarify.md` | clarify | `clarify.py` | `QuestionDrafts` | `clarify` |
| `apply_answers.md` | clarify (после ответов) | `clarify.py` | `AnswerEffects` | `clarify.answers` |
| `verdict.md` | verdict | `clarify.py: decide` | `VerdictDraft` | `verdict` |

Каталоги: `criteria/iso29148.yaml` (9 критериев §5.2.5 + 6 критериев §5.2.6 + `language_guidance` §5.2.7), `criteria/ieee830.yaml` (`ranked`, `modifiable`, `traceable` + `cross_reference` §4.3.1–4.3.8), `checks/fintech.yaml` (45 предметных пунктов). Вместе каталоги покрывают все значения `models.Criterion`.

## Как собирать вызов

```python
from reqcheck.prompts import load

def render(name: str, **ctx) -> tuple[str, str]:
    text = load(name).format(**ctx)            # все плейсхолдеры обязательны
    head, tail = text.split("\n## ДАННЫЕ\n", 1)
    system = load("_common") + "\n\n" + head   # статично в пределах прогона -> кэшируется
    user = "## ДАННЫЕ\n" + tail
    return system, user

system, user = render("review_requirement", criteria=..., doc_name=..., context=..., known_findings=..., requirements=...)
out = llm.generate_json(system, user, FindingDrafts, tag="review")
```

- `_common.md` не содержит фигурных скобок и не форматируется.
- В шаблонах литеральные фигурные скобки JSON-примеров экранированы как `{{ }}`. Подставляемые ЗНАЧЕНИЯ экранировать не нужно: `str.format` не обрабатывает скобки внутри значений (текст документа с `{...}` безопасен).
- Маркер `\n## ДАННЫЕ\n` встречается в каждом промпте ровно один раз. Всё выше — инструкция (system), ниже — данные (user).
- Пустое значение передавайте строкой `нет` (а не пустой строкой), чтобы модель не думала, что данные потерялись.

## Плейсхолдеры по промптам

| Промпт | Плейсхолдеры |
| :--- | :--- |
| review_requirement | `criteria`, `doc_name`, `context`, `known_findings`, `requirements` |
| set_contradictions | `criteria`, `doc_name`, `requirements`, `candidates`, `known_findings` |
| set_scenarios | `doc_name`, `acceptance`, `rules`, `context` |
| set_completeness | `criteria`, `doc_name`, `fintech_checks`, `outline`, `requirements`, `known_findings` |
| set_summary | `criteria`, `doc_name`, `outline`, `requirements`, `findings`, `stats` |
| critic | `doc_name`, `candidates`, `requirements` |
| clarify | `doc_name`, `max_questions`, `findings`, `requirements` |
| apply_answers | `doc_name`, `findings`, `answers`, `requirements` |
| verdict | `doc_name`, `stats`, `findings`, `set_summary`, `questions` |

## Формат значений (одинаковый во всех промптах)

**`doc_name`** — `Document.name`.

**`requirements`** — по одному блоку на требование (именно в таком виде оно показано в few-shot примерах):
```
[R-07] doc_id: Ф-3 | kind: functional | место: Часть A › Функциональные требования (строка 41)
Текст: «<Requirement.quote>»
```
`doc_id` отсутствует → `doc_id: —`. Для set_summary/verdict допустим краткий вид `[R-07] Ф-3 | functional | <summary>`.

**`acceptance`** / **`rules`** (set_scenarios) — тот же формат; `acceptance` = требования `kind == acceptance`, `rules` = `functional`, `api`, `data`, `nonfunctional`, `integration`.

**`context`** — для review: заголовки раздела пачки, цель документа, глоссарий (если есть), 2–3 соседних требования вне пачки; для set_scenarios: глоссарий и описание процесса (BPMN/sequence-блоки). До ~3000 символов.

**`criteria`** — текст из YAML-каталогов. Набор критериев по промптам:
- review_requirement: все `scope: requirement` из `iso29148.yaml` + `language_guidance`;
- set_contradictions: `set_consistent`, `correct`, `unambiguous` + `cross_reference` §4.3.4;
- set_completeness: `set_complete`, `complete` + `cross_reference` §4.3.3;
- set_summary: все `scope: set` из обоих файлов (9 штук).

Рекомендуемый рендер одного критерия:
```
### unambiguous — Однозначность (ISO/IEC/IEEE 29148:2018 §5.2.5)
<definition_ru>
Проверочные вопросы:
- <check_questions...>
Типичные дефекты:
- <typical_defects...>
Критичность: critical — <...>; major — <...>; minor — <...>
```
`language_guidance`: `<principle_ru>` и строки `- <class_ru>: <markers через запятую> → <fix_ru>`.

**`known_findings`** — уже найденные кандидаты (для review — находки линтеров по требованиям пачки; для set_* — все предыдущие), по строке:
```
- R-04 | unambiguous | «быстро» | <title>
```

**`candidates`**
- set_contradictions — группы требований с общими терминами/числами/полями (движок строит по пересечению лемм, чисел, имён полей; группа 2–6 требований):
  ```
  G-1 (общие термины: сумма, amount, 1 000): R-02, R-15, R-21
  ```
  Отдельным коротким вызовом идут пары «ограничение ↔ условие» (`terms.quantifier_pairs`: «максимум / не более / только / всегда / независимо …» в одном правиле и «если / при / повторно / после …» в другом, ≥ 2 общих слова):
  ```
  G-1 (ограничение «не более» в R-04 против условия «если» в R-09 — проверь вопрос (b); общие термины: …): R-04, R-09
  ```
- critic — пронумерованные кандидаты с 0 (индекс = позиция в списке, который движок передал):
  ```
  [#3] источник: llm_requirement | критерий: verifiable | критичность: major | уверенность: 0.70 | цитата найдена: да | R: R-04, R-09
  Заголовок: <title>
  Цитата: «<quote>»
  Пояснение: <explanation>
  Рекомендация: <recommendation>
  ```
  «цитата найдена» — из `Finding.grounded` (после `_ground`).
  Для находок-«пропусков» (в заголовке/пояснении «не указан / не определён / отсутствует …») движок дописывает строку `Проверь, не закрыт ли пропуск здесь: [R-NN] «…»` — 2 требования, связанные по терминам.

**`fintech_checks`** — пункты `checks/fintech.yaml`, отобранные по `applies_when` (подстрока в `doc.text.lower()`; пустой список = всегда). Если пунктов больше ~25, брать с наибольшим числом совпавших ключей.
```
- [FT-TIME-02] (Время и часовые пояса; criterion: unambiguous; по умолчанию: critical) <question_ru> Почему важно: <why_ru>
```

**`outline`** — разделы документа по `Block.section_path` заголовков:
```
- Часть A › Требования к изменениям › Функциональные требования — требований: 4
- Часть A › Требования к тестированию › Тестирование требований — пусто / шаблон
```

**`findings`** — проверенные находки (после critic и `_number`):
```
- D-003 | critical | set_consistent | R-04, R-15 | <title> | цитата: «<quote>»
```
Для clarify и apply_answers добавить строки `  Пояснение: …` и `  Рекомендация: …`. Для verdict: critical и major полностью, minor — только строка.

**`stats`** — несколько строк: `Требований: 34 (functional 6, api 12, …)`, `Находок: 27 (critical 5, major 12, minor 10)`, `По критериям: unambiguous 6, set_consistent 4, …`.

**`set_summary`** (verdict) — строки `- set_consistent: нарушено — <comment>`.

**`questions`** (verdict) / **`answers`** (apply_answers):
```
- Q-02 | D-001, D-004 | R-02, R-15 | <question>
  Варианты: <opt1> / <opt2> / <opt3>
  Ответ: <answer или «нет ответа»>
```

**`max_questions`** — целое, по умолчанию 15.

## Что движку делать с ответами

- **FindingDraft → Finding**: `scope` = `set` для set_*, иначе `requirement`; `source` — по стадии (`llm_requirement`, `llm_set`, `scenario`); для set_completeness: если в `explanation` есть метка `[FT-…]`, ставить `source = fintech`. `location` проставляет `_ground`.
- **ScenarioCheck** с `consistent = false` → Finding (`scope=set`, `source=scenario`, `rids = [ac_rid] + rule_rids`, `quote = quote_ac`, `explanation = explanation + " Правило: «" + quote_rule + "»"`). Критерий и критичность — по маркеру в начале `explanation`:
  - `ПРОТИВОРЕЧИЕ:` → `set_consistent`, critical;
  - `НЕОДНОЗНАЧНО:` → `unambiguous`, major (critical, если правило ключевое — решит critic);
  - `НЕ ХВАТАЕТ ДАННЫХ:` → `complete`, major;
  - `НЕПРОВЕРЯЕМО:` → `verifiable`, major.
  `recommendation` модель заполняет сама (конкретная правка ячейки критерия или правила); если поле пустое — движок формирует: «Привести ожидаемый результат критерия в соответствие с правилом или исправить правило» + вычисленный `expected_by_rules`.
  `missing_scenarios` — строки вида `R-NN: <сценарий>`; выводить в отчёте отдельным списком «Ветки без критериев приёмки» и/или одной находкой `set_complete` (minor/major) с цитатой правила R-NN.
- **CriticResult**: оставить `keep = true`, проставить `severity`, `confidence`, `verified = True`. Для `duplicate_of` объединить `rids` дубля в оставленную находку. Если вердикт для индекса не пришёл — оставить находку как есть с `verified = False`. При большом числе кандидатов (> ~40) делить на пачки, сортируя по `rids`, чтобы дубли попали в одну пачку.
- **AnswerEffects**: `drop` → удалить; `downgrade`/`update` → `severity`, а `note` дописать к `recommendation` (или `explanation`); `new_findings` — как FindingDraft, `source = llm_set`, затем `_ground`.
- **VerdictDraft** → `Verdict` один к одному.

## Рекомендации по параметрам

- temperature: 0.1–0.2 для review, set_*, critic, verdict; 0.3 для clarify.
- review_requirement: пачки по 6–10 требований одного раздела.
- set_contradictions: до ~15 групп кандидатов за вызов.
