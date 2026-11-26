"""Русские подписи и ссылки на стандарты для отчёта."""
from __future__ import annotations

from reqcheck.models import Criterion, FindingSource, ReqKind, Severity

ISO = "ISO/IEC/IEEE 29148:2018"
IEEE = "IEEE 830-1998"

# criterion -> (название, ссылка на стандарт, краткое определение, семейство для сводки)
CRITERIA: dict[Criterion, tuple[str, str, str]] = {
    Criterion.necessary: ("Необходимость", f"{ISO} §5.2.5",
                          "требование нужно: без него не достигается цель, и оно не дублирует другое"),
    Criterion.appropriate: ("Уместность уровня", f"{ISO} §5.2.5",
                            "уровень абстракции соответствует уровню документа, нет лишних проектных решений"),
    Criterion.unambiguous: ("Однозначность", f"{ISO} §5.2.5; {IEEE} §4.3.2",
                            "формулировку нельзя понять двумя способами"),
    Criterion.complete: ("Полнота", f"{ISO} §5.2.5; {IEEE} §4.3.3",
                         "требование содержит всё нужное для реализации: условия, значения, исключения"),
    Criterion.singular: ("Атомарность", f"{ISO} §5.2.5",
                         "в требовании одна мысль, его можно проверить отдельно"),
    Criterion.feasible: ("Реализуемость", f"{ISO} §5.2.5",
                         "требование выполнимо в рамках ограничений проекта"),
    Criterion.verifiable: ("Проверяемость", f"{ISO} §5.2.5; {IEEE} §4.3.6",
                           "есть измеримый способ убедиться, что требование выполнено"),
    Criterion.correct: ("Корректность", f"{ISO} §5.2.5; {IEEE} §4.3.1",
                        "требование верно отражает потребность заинтересованных сторон и не искажает факты"),
    Criterion.conforming: ("Соответствие шаблону", f"{ISO} §5.2.5",
                           "оформление соответствует принятому шаблону и стилю"),
    Criterion.set_complete: ("Полнота набора", f"{ISO} §5.2.6; {IEEE} §4.3.3",
                             "в наборе нет пропущенных требований и неопределённых мест"),
    Criterion.set_consistent: ("Согласованность набора", f"{ISO} §5.2.6; {IEEE} §4.3.4",
                               "требования не противоречат друг другу и терминология едина"),
    Criterion.set_feasible: ("Реализуемость набора", f"{ISO} §5.2.6",
                             "набор можно реализовать целиком в заданных ограничениях"),
    Criterion.set_comprehensible: ("Понятность набора", f"{ISO} §5.2.6",
                                   "набор структурирован и понятен читателю"),
    Criterion.set_validatable: ("Валидируемость набора", f"{ISO} §5.2.6",
                                "набор можно проверить на соответствие потребностям"),
    Criterion.set_bounded: ("Ограниченность набора", f"{ISO} §5.2.6",
                            "границы системы и охвата заданы"),
    Criterion.ranked: ("Ранжирование по важности", f"{IEEE} §4.3.5",
                       "приоритет и стабильность требований обозначены"),
    Criterion.modifiable: ("Модифицируемость", f"{IEEE} §4.3.7",
                           "структура позволяет менять требования без каскадных правок"),
    Criterion.traceable: ("Трассируемость", f"{IEEE} §4.3.8; {ISO} §5.2.5",
                          "у требования есть идентификатор и связь с источником и тестами"),
}

SEVERITIES: dict[Severity, tuple[str, str, str]] = {
    # (название, значок, расшифровка)
    Severity.critical: ("критично", "🔴", "блокирует разработку или тестирование: противоречие, непроверяемость, ключевой пропуск"),
    Severity.major: ("существенно", "🟠", "заметно повлияет на реализацию или приёмку"),
    Severity.minor: ("незначительно", "🟡", "оформление, стиль, мелкие неточности"),
}

KINDS: dict[ReqKind, str] = {
    ReqKind.functional: "функциональное",
    ReqKind.nonfunctional: "нефункциональное",
    ReqKind.data: "данные",
    ReqKind.integration: "интеграция",
    ReqKind.ui: "UI",
    ReqKind.process: "процесс",
    ReqKind.acceptance: "критерий приёмки",
    ReqKind.api: "API",
    ReqKind.business: "бизнес-цель",
    ReqKind.meta: "мета/шаблон",
}

SOURCES: dict[FindingSource, str] = {
    FindingSource.linter: "правило/линтер",
    FindingSource.llm_requirement: "LLM: требование",
    FindingSource.llm_set: "LLM: набор",
    FindingSource.scenario: "прогон сценариев",
    FindingSource.fintech: "fintech-чек-лист",
}

# семейства критичных дефектов для сводки
FAMILIES: list[tuple[str, set[Criterion]]] = [
    ("Непроверяемые", {Criterion.verifiable, Criterion.set_validatable}),
    ("Противоречивые", {Criterion.set_consistent, Criterion.correct}),
    ("Неполные", {Criterion.complete, Criterion.set_complete}),
]
OTHER_FAMILY = "Прочие критичные"


def family_of(c: Criterion) -> str:
    for name, members in FAMILIES:
        if c in members:
            return name
    return OTHER_FAMILY


def criterion_name(c) -> str:
    try:
        return CRITERIA[Criterion(c)][0]
    except (ValueError, KeyError):
        return str(c)


def criterion_ref(c) -> str:
    try:
        return CRITERIA[Criterion(c)][1]
    except (ValueError, KeyError):
        return ""


def severity_name(s) -> str:
    return SEVERITIES[Severity(s)][0]


def severity_badge(s) -> str:
    sv = SEVERITIES[Severity(s)]
    return f"{sv[1]} {sv[0]}"


def kind_name(k) -> str:
    return KINDS.get(ReqKind(k), str(k))


def source_name(s) -> str:
    return SOURCES.get(FindingSource(s), str(s))
