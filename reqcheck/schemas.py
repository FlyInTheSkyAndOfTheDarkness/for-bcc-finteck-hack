"""Схемы ответов LLM (structured output). Общий контракт промптов (prompts/*.md) и движка.

Правила совместимости с Gemini response_schema: без dict-полей, без Union кроме Optional,
перечисления через Enum. Менять только через оркестратора.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from reqcheck.models import Criterion, ReqKind, Severity


# ---- segment_refine ---------------------------------------------------------
class SegItem(BaseModel):
    doc_id: Optional[str] = None      # номер из документа, если есть ("Ф-1"); иначе null
    kind: ReqKind
    summary: str                      # краткая формулировка, ≤ 15 слов
    quote: str                        # дословная цитата из документа


class SegResult(BaseModel):
    requirements: list[SegItem]


# ---- review_requirement / set_* / fintech -------------------------------------
class FindingDraft(BaseModel):
    rids: list[str]                   # к каким R-NN относится (для набора — все затронутые)
    criterion: Criterion
    severity: Severity
    title: str                        # ≤ 12 слов
    quote: str                        # дословная цитата из документа (самый показательный фрагмент)
    explanation: str                  # почему это дефект, со ссылкой на текст
    recommendation: str               # конкретная правка формулировки
    confidence: float = Field(ge=0, le=1)


class FindingDrafts(BaseModel):
    findings: list[FindingDraft]


# ---- set_scenarios: прогон критериев приёмки против правил --------------------
class ScenarioCheck(BaseModel):
    ac_rid: str                       # R-NN критерия приёмки
    rule_rids: list[str]              # правила, которые применимы
    expected_by_rules: str            # что должно получиться по правилам (с расчётом)
    stated_expected: str              # что написано в критерии приёмки
    consistent: bool
    explanation: str
    quote_ac: str
    quote_rule: str
    recommendation: str = ""


class ScenarioChecks(BaseModel):
    checks: list[ScenarioCheck]
    missing_scenarios: list[str] = Field(default_factory=list)  # правила/ветки без критериев приёмки


# ---- set_summary: оценка набора по критериям ----------------------------------
class SetStatus(str, Enum):
    ok = "соблюдено"
    partial = "частично"
    violated = "нарушено"


class SetCriterionAssessment(BaseModel):
    criterion: Criterion              # только set_* / ranked / modifiable / traceable
    status: SetStatus
    comment: str                      # с ссылками на R-NN / D-NNN


class SetSummaryDraft(BaseModel):
    assessments: list[SetCriterionAssessment]
    notes: list[str] = Field(default_factory=list)


# ---- critic ---------------------------------------------------------------
class CriticVerdict(BaseModel):
    index: int                        # номер кандидата во входном списке
    keep: bool
    duplicate_of: Optional[int] = None  # номер кандидата, с которым объединить
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    reason: str


class CriticResult(BaseModel):
    verdicts: list[CriticVerdict]


# ---- clarify --------------------------------------------------------------
class QuestionDraft(BaseModel):
    fids: list[str]
    rids: list[str]
    question: str
    why: str
    options: list[str]


class QuestionDrafts(BaseModel):
    questions: list[QuestionDraft]


class AnswerEffect(BaseModel):
    fid: str
    action: str                       # keep | drop | downgrade | update
    severity: Optional[Severity] = None
    note: str = ""                    # как ответ меняет оценку


class AnswerEffects(BaseModel):
    effects: list[AnswerEffect]
    new_findings: list[FindingDraft] = Field(default_factory=list)


# ---- verdict --------------------------------------------------------------
class VerdictDraft(BaseModel):
    ready_for_development: bool
    ready_for_testing: bool
    rationale: str
    top_fixes: list[str]
