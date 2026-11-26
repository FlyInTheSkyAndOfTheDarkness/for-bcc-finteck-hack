"""Оркестратор: прогоняет стадии по очереди и собирает ReportData."""
from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from reqcheck import __version__
from reqcheck import clarify, critic, linters, review, segment, setreview
from reqcheck.config import ROOT, Settings
from reqcheck.grounding import GROUND_THRESHOLD, find_quote
from reqcheck.ingest import load_document
from reqcheck.llm import LLMClient, get_client
from reqcheck.models import Document, Finding, Question, ReportData, RunPassport, Severity
from reqcheck.prompts import prompt_version

Progress = Callable[[str, str], None]   # (стадия, сообщение)
AskFn = Callable[[Question], Optional[str]]

SEV_ORDER = {Severity.critical: 0, Severity.major: 1, Severity.minor: 2}


def agent_version() -> str:
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return f"{__version__} ({out.stdout.strip()})"
    except Exception:
        pass
    return __version__


def _ground(doc: Document, findings: list[Finding]) -> list[Finding]:
    """Проставить grounded/location по цитате. Неподтверждённые цитаты помечаются, но решает Critic."""
    for f in findings:
        score, block = find_quote(doc, f.quote) if f.quote else (0.0, None)
        f.grounded = score >= GROUND_THRESHOLD
        if block is not None and not f.location:
            f.location = block.locator()
    return findings


def _number(findings: list[Finding]) -> list[Finding]:
    findings.sort(key=lambda f: (SEV_ORDER[f.severity], f.scope != "requirement",
                                 min(f.rids) if f.rids else "R-99", -f.confidence))
    for i, f in enumerate(findings, 1):
        f.fid = f"D-{i:03d}"
    return findings


def run(input_path: str | Path, settings: Settings, *, llm: Optional[LLMClient] = None,
        progress: Optional[Progress] = None, ask: Optional[AskFn] = None) -> ReportData:
    """Полный прогон агента. ask — функция для уточняющих вопросов (интерактивный режим)."""
    say = progress or (lambda stage, msg: None)
    t0 = time.time()
    llm = llm or get_client(settings)

    say("ingest", f"Читаю {input_path}")
    doc = load_document(input_path)
    say("ingest", f"Блоков: {len(doc.blocks)}, формат: {doc.format}")

    say("segment", "Нарезаю требования")
    reqs = segment.segment(doc, llm)
    say("segment", f"Требований: {len(reqs)}")

    say("linters", "Детерминированные проверки")
    found = linters.run_linters(doc, reqs)
    say("linters", f"Находок линтеров: {len(found)}")

    say("review", "Проверяю каждое требование (ISO 29148 §5.2.5)")
    found += review.review_requirements(doc, reqs, llm)

    say("set", "Проверяю набор целиком (§5.2.6, IEEE 830 §4.3, fintech)")
    set_found, _ = setreview.review_set(doc, reqs, llm, found)
    found += set_found
    say("set", f"Всего кандидатов в дефекты: {len(found)}")

    say("critic", "Проверяю находки (Critic)")
    found = critic.verify(doc, reqs, _ground(doc, found), llm)
    found = _number(found)
    say("critic", f"Подтверждено: {len(found)}")

    say("set", "Оцениваю набор по критериям §5.2.6 / IEEE 830")
    summary = setreview.summarize(doc, reqs, found, llm)

    say("clarify", "Формулирую уточняющие вопросы")
    questions = clarify.make_questions(doc, reqs, found, llm)
    for i, q in enumerate(questions, 1):
        q.qid = q.qid or f"Q-{i:02d}"
    if ask and questions:
        for q in questions:
            q.answer = ask(q)
        if any(q.answer for q in questions):
            say("clarify", "Пересматриваю находки с учётом ответов")
            found = _number(clarify.apply_answers(doc, reqs, found, questions, llm))

    say("verdict", "Формирую итог")
    verdict = clarify.decide(reqs, found, summary, llm)

    passport = RunPassport(
        doc_name=doc.name, doc_path=Path(input_path).name, doc_sha256=doc.sha256, doc_format=doc.format,
        run_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        provider=llm.provider, model=llm.model, agent_version=agent_version(),
        prompt_version=prompt_version(),
        params={"temperature": settings.temperature, "cache": settings.use_cache,
                "interactive": bool(ask)},
        llm_calls=llm.stats.calls, tokens_total=llm.stats.tokens_total,
        duration_s=round(time.time() - t0, 1),
    )
    say("done", f"Готово за {passport.duration_s} с, вызовов LLM: {llm.stats.calls}")
    return ReportData(passport=passport, requirements=reqs, findings=found, questions=questions,
                      set_summary=summary, verdict=verdict)
