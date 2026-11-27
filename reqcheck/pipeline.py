"""Оркестратор: прогоняет стадии по очереди и собирает ReportData."""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
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


def _rid_num(rids: list[str]) -> int:
    """Минимальный номер требования для сортировки (числом: R-100 после R-99)."""
    nums = [int(x.split("-")[-1]) for x in rids if x.split("-")[-1].isdigit()]
    return min(nums) if nums else 10 ** 6


def _number(findings: list[Finding]) -> list[Finding]:
    findings.sort(key=lambda f: (getattr(f, "doubtful", False), SEV_ORDER[f.severity], f.scope != "requirement",
                                 _rid_num(f.rids), -f.confidence))
    for i, f in enumerate(findings, 1):
        f.fid = f"D-{i:03d}"
    return findings


def run(input_path: str | Path, settings: Settings, *, llm: Optional[LLMClient] = None,
        progress: Optional[Progress] = None, ask: Optional[AskFn] = None) -> ReportData:
    """Полный прогон агента. ask — функция для уточняющих вопросов (интерактивный режим)."""
    say = progress or (lambda stage, msg: None)
    t0 = time.time()
    # REQCHECK_SKIP=review,set,critic,clarify — отключить стадии (абляция и отладка)
    skip = {x.strip() for x in os.getenv("REQCHECK_SKIP", "").split(",") if x.strip()}
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

    if "review" not in skip:
        say("review", "Проверяю каждое требование (ISO 29148 §5.2.5)")
        found += review.review_requirements(doc, reqs, llm, known=list(found))

    if "set" not in skip:
        say("set", "Проверяю набор целиком (§5.2.6, IEEE 830 §4.3, fintech)")
        set_found, _ = setreview.review_set(doc, reqs, llm, found)
        found += set_found
    say("set", f"Всего кандидатов в дефекты: {len(found)}")

    if "critic" not in skip:
        say("critic", "Проверяю находки (Critic)")
        found = critic.verify(doc, reqs, _ground(doc, found), llm)
    else:
        found = [f for f in _ground(doc, found) if f.grounded]
    found = _number(found)
    say("critic", f"Подтверждено: {len(found)}")

    say("set", "Оцениваю набор по критериям §5.2.6 / IEEE 830")
    say("clarify", "Формулирую уточняющие вопросы (параллельно с оценкой набора)")
    if "clarify" in skip:
        from reqcheck.models import SetSummary
        summary, questions = SetSummary(), []
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:   # обе стадии зависят только от пронумерованных D-NNN
            sure = [f for f in found if not f.doubtful]   # «под сомнением» не порождают вопросов и не влияют на итог
            fut_summary = pool.submit(setreview.summarize, doc, reqs, sure, llm)
            fut_questions = pool.submit(clarify.make_questions, doc, reqs, sure, llm)
            summary, questions = fut_summary.result(), fut_questions.result()
    for i, q in enumerate(questions, 1):
        q.qid = q.qid or f"Q-{i:02d}"
    if ask and questions:
        for q in questions:
            q.answer = ask(q)
        if any(q.answer for q in questions):
            say("clarify", "Пересматриваю находки с учётом ответов")
            found = _number(clarify.apply_answers(doc, reqs, found, questions, llm))

    say("verdict", "Формирую итог")
    sure = [f for f in found if not f.doubtful]
    verdict = clarify._fallback(sure) if "clarify" in skip else clarify.decide(reqs, sure, summary, llm, questions=questions, doc_name=doc.name)

    passport = RunPassport(
        doc_name=doc.name, doc_path=Path(input_path).name, doc_sha256=doc.sha256, doc_format=doc.format,
        run_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        provider=llm.provider, model=llm.model, agent_version=agent_version(),
        prompt_version=prompt_version(),
        params={"temperature": settings.temperature, "cache": settings.use_cache,
                "interactive": bool(ask),
                **({"skipped_stages": ",".join(sorted(skip))} if skip else {}),
                **({"models_used": ", ".join(f"{m} ×{n}" for m, n in sorted(llm.stats.models.items()))}
                   if getattr(llm.stats, "models", None) else {}),
                **({"cache_hits": llm.stats.cache_hits} if llm.stats.cache_hits else {})},
        llm_calls=llm.stats.calls, tokens_total=llm.stats.tokens_total,
        duration_s=round(time.time() - t0, 1),
    )
    errs = getattr(llm.stats, "errors", 0)
    say("done", f"Готово за {passport.duration_s} с, вызовов LLM: {llm.stats.calls}"
        + (f"; не удалось выполнить запросов к LLM: {errs} — часть проверок пропущена, "
           f"см. {settings.trace_path.name if settings.trace_path else 'журнал'}" if errs else ""))
    return ReportData(passport=passport, requirements=reqs, findings=found, questions=questions,
                      set_summary=summary, verdict=verdict)
