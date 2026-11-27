"""CLI: python -m reqcheck <файл требований> -o <файл отчёта>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reqcheck.config import load_settings
from reqcheck.models import Question


def _ask_console(q: Question) -> str | None:
    from rich.console import Console
    from rich.markup import escape
    from rich.prompt import Prompt
    c = Console()
    c.print(f"\n[bold cyan]{escape(str(q.qid))}[/] {escape(q.question)}")   # текст от LLM: «[…]» — не разметка
    c.print(f"[dim]Зачем: {escape(q.why or '')}[/]")
    for i, o in enumerate(q.options, 1):
        c.print(f"  {i}. {escape(o)}")
    ans = Prompt.ask("Ответ (номер, свой текст или Enter — пропустить)", default="", show_default=False)
    if ans.isdigit() and 1 <= int(ans) <= len(q.options):
        return q.options[int(ans) - 1]
    return ans or None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # русские сообщения и журнал провайдера в консоли Windows
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
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
    from rich.markup import escape
    con = Console()
    from reqcheck.ingest import SUPPORTED
    src = Path(args.input)
    if not src.is_file():
        con.print(f"[red]Файл не найден: {escape(str(src))}[/]")
        return 2
    if src.suffix.lower() not in SUPPORTED:
        con.print(f"[red]Неподдерживаемый формат {escape(src.suffix or src.name)}: {', '.join(sorted(SUPPORTED))}[/]")
        return 2
    out = Path(args.output)
    if out.suffix.lower() in (".html", ".htm", ".json") or \
            src.resolve() in {out.resolve(), out.with_suffix(".html").resolve(), out.with_suffix(".json").resolve()}:
        con.print("[red]Укажите в -o отдельный файл отчёта .md (например reports/report.md); "
                  "входной документ не должен перезаписываться.[/]")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings(provider=args.provider, model=args.model,
                             use_cache=not args.no_cache, trace_path=out.with_suffix(".trace.jsonl"))
    if settings.provider not in ("fake", "ollama") and not settings.api_key:
        con.print(f"[red]Нет API-ключа для провайдера {settings.provider}. Скопируйте .env.example в .env и укажите ключ.[/]")
        return 2

    from reqcheck.pipeline import run
    from reqcheck.report import render_html, render_markdown

    try:   # журнал относится к этому прогону, а не копится от прошлых
        settings.trace_path.unlink(missing_ok=True)
    except OSError:
        pass

    data = run(args.input, settings,
               progress=lambda st, msg: con.print(f"[dim]{st:>8}[/] {escape(str(msg))}"),
               ask=_ask_console if args.interactive else None)
    if settings.trace_path and not settings.trace_path.exists():
        settings.trace_path.write_text("", encoding="utf-8")   # fake / всё из кэша без вызовов — пустой журнал
    # сначала данные: если рендер упадёт, результат прогона (и затраченные вызовы LLM) не теряется
    out.with_suffix(".json").write_text(data.model_dump_json(indent=2), encoding="utf-8")
    try:
        out.write_text(render_markdown(data), encoding="utf-8")
        con.print(f"[green]Отчёт:[/] {escape(str(out))}  (+ {escape(out.with_suffix('.json').name)})")
        if not args.no_html:
            html = out.with_suffix(".html")
            html.write_text(render_html(data), encoding="utf-8")
            con.print(f"[green]HTML:[/] {escape(str(html))}")
    except Exception as e:  # noqa: BLE001
        con.print(f"[red]Не удалось сформировать отчёт: {escape(str(e))}. "
                  f"Данные прогона сохранены в {escape(out.with_suffix('.json').name)}.[/]")
        return 1
    sure = [f for f in data.findings if not f.doubtful]
    crit = sum(1 for f in sure if f.severity.value == "critical")
    con.print(f"Требований: {len(data.requirements)} · замечаний: {len(sure)} · критичных: {crit}"
              + (f" · под сомнением: {len(data.findings) - len(sure)}" if len(sure) < len(data.findings) else ""))
    return 0
