"""CLI: python -m reqcheck <файл требований> -o <файл отчёта>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reqcheck.config import load_settings
from reqcheck.models import Question


def _ask_console(q: Question) -> str | None:
    from rich.console import Console
    from rich.prompt import Prompt
    c = Console()
    c.print(f"\n[bold cyan]{q.qid}[/] {q.question}")
    c.print(f"[dim]Зачем: {q.why}[/]")
    for i, o in enumerate(q.options, 1):
        c.print(f"  {i}. {o}")
    ans = Prompt.ask("Ответ (номер, свой текст или Enter — пропустить)", default="", show_default=False)
    if ans.isdigit() and 1 <= int(ans) <= len(q.options):
        return q.options[int(ans) - 1]
    return ans or None


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="reqcheck", description="ИИ-агент проверки требований (ISO 29148 / IEEE 830)")
    ap.add_argument("input", help="файл требований: .pdf .docx .md .txt .html")
    ap.add_argument("-o", "--output", default="reports/report.md", help="файл отчёта (.md); рядом кладётся .html")
    ap.add_argument("--provider", help="gemini | openai | anthropic | ollama | fake")
    ap.add_argument("--model", help="имя модели (по умолчанию — из .env или автоматический выбор)")
    ap.add_argument("--interactive", action="store_true", help="задать уточняющие вопросы в терминале и учесть ответы")
    ap.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов LLM")
    ap.add_argument("--no-html", action="store_true", help="не создавать HTML-версию отчёта")
    args = ap.parse_args(argv)

    from rich.console import Console
    con = Console()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings(provider=args.provider, model=args.model,
                             use_cache=not args.no_cache, trace_path=out.with_suffix(".trace.jsonl"))
    if settings.provider not in ("fake", "ollama") and not settings.api_key:
        con.print(f"[red]Нет API-ключа для провайдера {settings.provider}. Скопируйте .env.example в .env и укажите ключ.[/]")
        return 2

    from reqcheck.pipeline import run
    from reqcheck.report import render_html, render_markdown

    data = run(args.input, settings,
               progress=lambda st, msg: con.print(f"[dim]{st:>8}[/] {msg}"),
               ask=_ask_console if args.interactive else None)
    out.write_text(render_markdown(data), encoding="utf-8")
    con.print(f"[green]Отчёт:[/] {out}")
    if not args.no_html:
        html = out.with_suffix(".html")
        html.write_text(render_html(data), encoding="utf-8")
        con.print(f"[green]HTML:[/] {html}")
    crit = sum(1 for f in data.findings if f.severity.value == "critical")
    con.print(f"Требований: {len(data.requirements)} · замечаний: {len(data.findings)} · критичных: {crit}")
    return 0
