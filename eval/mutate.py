"""Мутационное тестирование агента: внесение известных дефектов в чистую спецификацию.

    python -m eval.mutate --seed 1 [--n 5] [--src eval/clean_spec.md] [--out-dir eval/mutants]
    python -m eval.mutate --seeds 1 2 3

Пишет eval/mutants/<seed>.md и eval/mutants/<seed>.gold.yaml (схема как у eval/gold.yaml).
Мутации детерминированы по seed: выбор типов дефектов и вариантов — через random.Random(seed).
Каждый оператор правит свои, непересекающиеся якорные фрагменты clean_spec.md, поэтому
любые комбинации совместимы. Оценка агента на мутанте:

    python -m reqcheck eval/mutants/1.md reports/mutant1.md
    python -m eval.evaluate reports/mutant1.json --gold eval/mutants/1.gold.yaml
"""
from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from eval.gold import EVAL_DIR, GoldItem, dump_gold

CLEAN_SPEC = EVAL_DIR / "clean_spec.md"
MUTANTS_DIR = EVAL_DIR / "mutants"


class AnchorError(RuntimeError):
    pass


def _sub(text: str, old: str, new: str) -> str:
    n = text.count(old)
    if n != 1:
        raise AnchorError(f"якорь встречается {n} раз(а), ожидался 1: {old[:60]!r}")
    return text.replace(old, new)


@dataclass
class Variant:
    edits: list[tuple[str, str]]     # (старый фрагмент, новый фрагмент)
    title: str
    section: str
    quotes: list[str]                # цитаты из ИЗМЕНЁННОГО текста
    description: str


@dataclass
class Operator:
    name: str
    criterion: str
    alt_criteria: list[str]
    severity: str
    variants: list[Variant]


OPERATORS: list[Operator] = [
    Operator("vague_term", "unambiguous", ["verifiable"], "major", [
        Variant([("передаёт push провайдеру не позднее 60 секунд после получения события для 95 % событий "
                  "при нагрузке до 50 событий в секунду", "передаёт push провайдеру оперативно после получения события")],
                "Расплывчатый срок «оперативно» в НФ-1", "Часть A › Нефункциональные требования › НФ-1",
                ["Сервис уведомлений передаёт push провайдеру оперативно после получения события."],
                "Норматив времени заменён неизмеримым словом «оперативно»: нет числа, перцентиля и нагрузки."),
        Variant([("не позднее чем через 2 минуты после зачисления", "своевременно после зачисления")],
                "Расплывчатое «своевременно» в цели", "Часть A › Цели",
                ["Клиент узнаёт о зачислении заработной платы своевременно после зачисления средств на счёт."],
                "Цель не содержит измеримого срока: «своевременно» допускает любую задержку."),
        Variant([("Доступность сервиса уведомлений — не ниже 99,9 % за календарный месяц.",
                  "Сервис уведомлений обладает высокой доступностью.")],
                "Расплывчатое «высокая доступность» в НФ-2", "Часть A › Нефункциональные требования › НФ-2",
                ["Сервис уведомлений обладает высокой доступностью."],
                "Нет численного значения доступности и периода измерения — требование непроверяемо."),
    ]),
    Operator("numeric_contradiction", "set_consistent", ["correct", "verifiable"], "critical", [
        Variant([("| 3 повторные попытки с интервалом 30 секунд", "| 5 повторных попыток с интервалом 30 секунд")],
                "Число повторов в КП-6 противоречит ЗП-8", "Часть A › ЗП-8 ↔ Критерии приемки › КП-6",
                ["повторяет передачу до 3 раз с интервалом 30 секунд", "5 повторных попыток с интервалом 30 секунд"],
                "ЗП-8 допускает до 3 повторов, критерий приёмки КП-6 ожидает 5."),
        Variant([("повторяет передачу до 3 раз с интервалом 30 секунд", "повторяет передачу до 3 раз с интервалом 10 секунд")],
                "Интервал повторов в ЗП-8 противоречит КП-6", "Часть A › ЗП-8 ↔ Критерии приемки › КП-6",
                ["повторяет передачу до 3 раз с интервалом 10 секунд", "3 повторные попытки с интервалом 30 секунд"],
                "ЗП-8 задаёт интервал 10 секунд, критерий приёмки КП-6 — 30 секунд."),
        Variant([("подряд | Отправлено два push", "подряд | Отправлен один push")],
                "КП-5 противоречит ЗП-6 (push по каждому зачислению)", "Часть A › ЗП-6 ↔ Критерии приемки › КП-5",
                ["push отправляется по каждому зачислению", "Зачислить две выплаты с КНП 311 подряд | Отправлен один push"],
                "По ЗП-6 push отправляется по каждому зачислению, а КП-5 при двух зачислениях ожидает один push."),
    ]),
    Operator("unit_mismatch", "set_consistent", ["unambiguous", "correct"], "major", [
        Variant([("`amountTiyn` (integer, сумма в тиынах)", "`amount` (number, сумма в тенге)")],
                "Сумма в событии в тенге, а в журнале — в тиынах", "Часть A › Требования к интеграции ↔ Требования к данным",
                ["`amount` (number, сумма в тенге)", "сумма зачисления в тиынах (integer)"],
                "Событие передаёт сумму в тенге дробным числом, а данные хранят целые тиыны; правило пересчёта не задано."),
        Variant([("сумма зачисления в тиынах (integer)", "сумма зачисления в тенге (decimal)")],
                "Журнал хранит тенге, событие передаёт тиыны", "Часть A › Требования к данным ↔ Требования к интеграции",
                ["сумма зачисления в тенге (decimal)", "`amountTiyn` (integer, сумма в тиынах)"],
                "Данные хранят сумму в тенге, событие — в тиынах; единицы не согласованы, риск ошибки в 100 раз."),
    ]),
    Operator("state_changing_get", "correct", ["appropriate", "set_consistent", "conforming"], "critical", [
        Variant([("| **REST type** | PUT |", "| **REST type** | GET |"), ("curl -X PUT", "curl -X GET")],
                "Сохранение настройки через GET", "Часть B › Общие параметры › REST type",
                ["REST type | GET", "curl -X GET"],
                "Метод изменяет состояние, но объявлен как GET (небезопасно, кэшируется, совпадает с методом чтения)."),
    ]),
    Operator("status_phrase_mismatch", "set_consistent", ["correct", "conforming"], "major", [
        Variant([("#### 200. Successful — OK", "#### 200. Successful — Created")],
                "Код 200 подписан как Created", "Часть B › Статусы › 200",
                ["200. Successful — Created"], "Фраза Created соответствует 201, а не 200."),
        Variant([("#### 400. Unsuccessful — Bad request", "#### 400. Unsuccessful — Not found")],
                "Код 400 подписан как Not found", "Часть B › Статусы › 400",
                ["400. Unsuccessful — Not found"], "Фраза Not found соответствует 404, а не 400."),
    ]),
    Operator("missing_branch", "complete", ["set_complete"], "major", [
        Variant([("3. Проверить тело запроса; при ошибке вернуть 400.", "3. Проверить тело запроса.")],
                "Не описан исход при ошибке проверки тела запроса", "Часть B › Логика работы метода › п.3",
                ["Проверить тело запроса."],
                "Логика не говорит, что вернуть при некорректном теле; ветка ошибки отсутствует."),
        Variant([(" При ошибке сохранения переключатель возвращается в прежнее положение и показывается сообщение "
                  "«Не удалось сохранить настройку, попробуйте позже».", "")],
                "UI не описывает ошибку сохранения настройки", "Часть A › Дизайн процесса UI",
                ["Состояние переключателя сохраняется сразу при нажатии."],
                "Не описано поведение экрана, если сохранение настройки не удалось."),
        Variant([(" Если уведомление выключено — завершение без отправки.", "")],
                "BPMN без ветки «уведомление выключено»", "Часть A › BPMN схема процесса",
                ["если уведомление включено, формирование push → отправка push → завершение."],
                "Схема процесса не показывает, что происходит при выключенном уведомлении."),
    ]),
    Operator("unfilled_template_field", "conforming", ["traceable", "modifiable", "set_complete"], "minor", [
        Variant([("| **Задача в JIRA** | PAY-1024 |", "| **Задача в JIRA** | — |")],
                "Не заполнена задача в JIRA", "Часть A › Общие сведения",
                ["Задача в JIRA | —"], "Поле шаблона не заполнено — нет трассировки к задаче."),
        Variant([("Тестовые сценарии строятся по критериям приемки КП-1…КП-6; ответственный — QA-инженер команды уведомлений.",
                  "заполняются тестировщиками")],
                "Раздел «Тестирование требований» не заполнен", "Часть A › Требования к тестированию",
                ["заполняются тестировщиками"], "Раздел шаблона оставлен с заглушкой."),
        Variant([("Документ: версия 1.0 от 20.11.2026.", "Документ: версия —, дата —.")],
                "Не заполнены версия и дата документа", "Часть A › Ревью требований",
                ["Документ: версия —, дата —."], "Нет версии и даты документа."),
    ]),
    Operator("two_in_one", "singular", ["traceable", "verifiable"], "minor", [
        Variant([("push о зачислении заработной платы не отправляется.",
                  "push о зачислении заработной платы не отправляется, а история зачислений за последние "
                  "12 месяцев отображается в разделе «Уведомления».")],
                "ЗП-7 объединяет два разных требования", "Часть A › Функциональные требования › ЗП-7",
                ["история зачислений за последние 12 месяцев отображается в разделе «Уведомления»"],
                "К правилу о выключенном уведомлении присоединено независимое требование об истории зачислений."),
        Variant([("уведомление о зачислении заработной платы включено.",
                  "уведомление о зачислении заработной платы включено, и клиенту один раз показывается "
                  "экран-приглашение с описанием функции.")],
                "ЗП-2 объединяет два разных требования", "Часть A › Функциональные требования › ЗП-2",
                ["клиенту один раз показывается экран-приглашение с описанием функции"],
                "К правилу о значении по умолчанию присоединено независимое требование об экране-приглашении."),
    ]),
]
OPS_BY_NAME = {op.name: op for op in OPERATORS}


def mutate(text: str, seed: int, n: int = 5, ops: Optional[list[str]] = None) -> tuple[str, list[GoldItem]]:
    """Внести n дефектов (или явно заданные ops). Детерминировано по seed."""
    rng = random.Random(seed)
    if ops:
        chosen = [OPS_BY_NAME[o] for o in ops]
    else:
        # Операторы берутся по кругу из фиксированной перестановки: соседние seed'ы покрывают все
        # типы дефектов (seed 1 и 2 при n=5 — все 8), вариант внутри типа выбирает rng(seed).
        order = OPERATORS[:]
        random.Random(0).shuffle(order)
        k = min(n, len(order))
        start = ((seed - 1) * k) % len(order)
        chosen = [order[(start + j) % len(order)] for j in range(k)]
    chosen = sorted(chosen, key=OPERATORS.index)   # стабильный порядок применения и нумерации
    gold: list[GoldItem] = []
    for i, op in enumerate(chosen, 1):
        v = rng.choice(op.variants)
        for old, new in v.edits:
            text = _sub(text, old, new)
        gold.append(GoldItem(id=f"M-{i:02d}", title=v.title, section=v.section, quotes=v.quotes,
                             criterion=op.criterion, alt_criteria=op.alt_criteria, severity=op.severity,
                             must_find=True, description=v.description, mutation=op.name))
    return text, gold


def write_mutant(seed: int, n: int = 5, src: Path = CLEAN_SPEC, out_dir: Path = MUTANTS_DIR) -> tuple[Path, Path]:
    text, gold = mutate(src.read_text(encoding="utf-8"), seed, n)
    out_dir.mkdir(parents=True, exist_ok=True)
    md, gy = out_dir / f"{seed}.md", out_dir / f"{seed}.gold.yaml"
    md.write_text(text, encoding="utf-8", newline="\n")
    dump_gold(gold, gy)
    return md, gy


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m eval.mutate")
    ap.add_argument("--seed", type=int, action="append", dest="seeds")
    ap.add_argument("--seeds", type=int, nargs="+", dest="seeds_list")
    ap.add_argument("--n", type=int, default=5, help="сколько типов дефектов внести (≤ 8)")
    ap.add_argument("--src", type=Path, default=CLEAN_SPEC)
    ap.add_argument("--out-dir", type=Path, default=MUTANTS_DIR)
    a = ap.parse_args(argv)
    seeds = (a.seeds or []) + (a.seeds_list or []) or [1, 2]
    for s in seeds:
        md, gy = write_mutant(s, a.n, a.src, a.out_dir)
        print(f"seed {s}: {md} , {gy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
