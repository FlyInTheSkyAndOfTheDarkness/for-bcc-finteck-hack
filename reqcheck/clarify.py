"""Стадия 7. Уточняющие вопросы автору ТЗ и итоговый вердикт. Владелец: A7."""
from __future__ import annotations

from reqcheck.llm import LLMClient
from reqcheck.models import Document, Finding, Question, Requirement, SetSummary, Verdict


def make_questions(doc: Document, reqs: list[Requirement], findings: list[Finding],
                   llm: LLMClient) -> list[Question]:
    """ЗАГЛУШКА."""
    return []


def apply_answers(doc: Document, reqs: list[Requirement], findings: list[Finding],
                  questions: list[Question], llm: LLMClient) -> list[Finding]:
    """Пересмотреть находки с учётом ответов (Question.answer). ЗАГЛУШКА."""
    return findings


def decide(reqs: list[Requirement], findings: list[Finding], summary: SetSummary,
           llm: LLMClient) -> Verdict:
    """Можно ли начинать разработку и тестирование; что исправить первым. ЗАГЛУШКА."""
    crit = [f for f in findings if f.severity.value == "critical"]
    return Verdict(ready_for_development=not crit, ready_for_testing=not crit,
                   rationale="заглушка", top_fixes=[f.title for f in crit[:5]])
