"""Общие контракты данных reqcheck. Все стадии конвейера обмениваются только этими моделями.

Менять поля можно только через оркестратора: от них зависят все стадии.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- ingest ---
class BlockKind(str, Enum):
    heading = "heading"
    text = "text"
    list_item = "list_item"
    table_row = "table_row"
    table_header = "table_header"
    code = "code"


class Block(BaseModel):
    """Минимальная адресуемая единица исходного документа."""
    id: str                                   # "B-0001"
    kind: BlockKind
    text: str                                 # нормализованный текст (для таблиц — «кол1 | кол2 | …»)
    part: Optional[str] = None                # «Часть A. Новый функционал»
    section_path: list[str] = Field(default_factory=list)  # ["Часть A…", "Требования к изменениям", "Функциональные требования"]
    line_start: Optional[int] = None          # 1-based строки исходника (MD/TXT) или извлечённого текста (PDF/DOCX)
    line_end: Optional[int] = None
    page: Optional[int] = None                # PDF: 1-based
    cells: Optional[list[str]] = None         # table_row/table_header: ячейки
    table_header: Optional[list[str]] = None  # table_row: заголовки столбцов своей таблицы

    def locator(self) -> str:
        """Человекочитаемое место в документе для отчёта."""
        loc = " › ".join(self.section_path) if self.section_path else (self.part or "")
        pos = []
        if self.page:
            pos.append(f"стр. {self.page}")
        if self.line_start:
            pos.append(f"стр. текста {self.line_start}" if self.page else
                       (f"строка {self.line_start}" if self.line_end in (None, self.line_start)
                        else f"строки {self.line_start}–{self.line_end}"))
        return f"{loc} ({', '.join(pos)})" if pos else loc


class Document(BaseModel):
    name: str              # заголовок документа (первый H1) или имя файла
    path: str
    sha256: str
    format: str            # md | txt | pdf | docx | html
    text: str              # полный нормализованный текст (для grounding цитат)
    blocks: list[Block]


# ---------------------------------------------------------- requirements ---
class ReqKind(str, Enum):
    functional = "functional"
    nonfunctional = "nonfunctional"
    data = "data"
    integration = "integration"
    ui = "ui"
    process = "process"          # BPMN, sequence, архитектура как требование
    acceptance = "acceptance"    # критерий приёмки
    api = "api"                  # требование из API-спецификации (метод, параметр, статус, правило)
    business = "business"        # цели / бизнес-требование
    meta = "meta"                # версия, согласования, чек-лист ревью


class Requirement(BaseModel):
    rid: str                       # ID в отчёте: "R-01"
    doc_id: Optional[str] = None   # ID в документе: "Ф-1", "КП-2" (если в документе нет — None)
    kind: ReqKind
    summary: str                   # краткая формулировка (≤ 15 слов)
    quote: str                     # точная цитата из документа
    block_ids: list[str] = Field(default_factory=list)
    location: str = ""             # Block.locator() первого блока
    grounded: bool = True          # цитата найдена в документе (fuzzy ≥ порога)


# --------------------------------------------------------------- findings ---
class Criterion(str, Enum):
    # ISO/IEC/IEEE 29148:2018 §5.2.5 — свойства отдельного требования
    necessary = "necessary"
    appropriate = "appropriate"          # уровень абстракции; без лишних проектных решений
    unambiguous = "unambiguous"
    complete = "complete"
    singular = "singular"
    feasible = "feasible"
    verifiable = "verifiable"
    correct = "correct"
    conforming = "conforming"
    # ISO 29148 §5.2.6 — свойства набора требований (+ IEEE 830 §4.3)
    set_complete = "set_complete"
    set_consistent = "set_consistent"
    set_feasible = "set_feasible"
    set_comprehensible = "set_comprehensible"
    set_validatable = "set_validatable"
    set_bounded = "set_bounded"
    ranked = "ranked"                    # IEEE 830: ранжирование по важности/стабильности
    modifiable = "modifiable"            # IEEE 830
    traceable = "traceable"              # IEEE 830


class Severity(str, Enum):
    critical = "critical"   # блокирует разработку/тестирование: противоречие, непроверяемость, ключевой пропуск
    major = "major"         # заметно повлияет на реализацию или приёмку
    minor = "minor"         # оформление, стиль, мелкие неточности


class FindingSource(str, Enum):
    linter = "linter"
    llm_requirement = "llm_requirement"
    llm_set = "llm_set"
    scenario = "scenario"        # прогон критериев приёмки против правил
    fintech = "fintech"          # предметный чек-лист


class Finding(BaseModel):
    fid: str = ""                          # присваивает конвейер: "D-001"
    scope: str = "requirement"             # requirement | set
    rids: list[str] = Field(default_factory=list)
    criterion: Criterion
    severity: Severity
    title: str                             # кратко, ≤ 12 слов
    quote: str                             # точная цитата из документа, на которую ссылается замечание
    location: str = ""                     # место в документе
    explanation: str                       # почему это дефект, со ссылкой на текст
    recommendation: str                    # как исправить
    source: FindingSource
    confidence: float = 0.7                # 0..1
    verified: bool = False                 # прошло Critic
    grounded: bool = True                  # цитата найдена в документе


# -------------------------------------------------------------- questions ---
class Question(BaseModel):
    qid: str = ""                          # "Q-01"
    fids: list[str] = Field(default_factory=list)
    rids: list[str] = Field(default_factory=list)
    question: str
    why: str                               # что блокирует без ответа
    options: list[str] = Field(default_factory=list)  # варианты ответа
    answer: Optional[str] = None           # заполняется в --interactive / UI


# ----------------------------------------------------------------- report ---
class RunPassport(BaseModel):
    doc_name: str
    doc_path: str
    doc_sha256: str
    doc_format: str
    run_at: str                            # ISO-8601, локальное время с зоной
    provider: str
    model: str
    agent_version: str                     # git describe / коммит
    prompt_version: str                    # хэш каталога prompts/
    params: dict = Field(default_factory=dict)
    llm_calls: int = 0
    tokens_total: int = 0
    duration_s: float = 0.0


class SetSummary(BaseModel):
    """Оценка набора требований целиком по критериям §5.2.6 / IEEE 830 §4.3."""
    criteria: dict[str, str] = Field(default_factory=dict)  # criterion -> «соблюдено / нарушено: …»
    notes: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    ready_for_development: bool
    ready_for_testing: bool
    rationale: str
    top_fixes: list[str] = Field(default_factory=list)   # что исправить в первую очередь (со ссылками на D-/R-)


class ReportData(BaseModel):
    passport: RunPassport
    requirements: list[Requirement]
    findings: list[Finding]
    questions: list[Question] = Field(default_factory=list)
    set_summary: SetSummary = Field(default_factory=SetSummary)
    verdict: Verdict
