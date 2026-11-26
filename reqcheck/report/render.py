"""Построение контекста и рендер отчёта (Markdown / HTML) через Jinja2."""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from reqcheck.models import Criterion, Finding, ReportData, Severity

from . import labels

TEMPLATES = Path(__file__).parent / "templates"
QUOTE_LIMIT = 140
ID_RE = re.compile(r"\b([RDQ]-\d+)\b")
SEV_ORDER = [Severity.critical, Severity.major, Severity.minor]


# ------------------------------------------------------------------ filters ---
def _short(text, limit: int = QUOTE_LIMIT) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _angle(text) -> str:
    return str(text or "").replace("<", "&lt;").replace(">", "&gt;")


def _mdcell(text) -> str:
    """Ячейка MD-таблицы: экранируем «|», переводы строк -> <br>, «<» -> &lt;."""
    t = _angle(text).replace("\\", "\\\\").replace("|", "\\|")
    return re.sub(r"(\r\n|\r|\n)+", "<br>", t)


def _mdquote(text) -> str:
    lines = _angle(text).splitlines() or [""]
    return "\n".join("> " + ln if ln.strip() else ">" for ln in lines)


def _fmt_dur(sec) -> str:
    sec = float(sec or 0)
    if sec < 60:
        return f"{sec:.1f} с"
    return f"{int(sec // 60)} мин {int(sec % 60)} с"


def _fmt_params(p) -> str:
    return ", ".join(f"{k}={v}" for k, v in (p or {}).items()) or "—"


def _make_env(fmt: str, known: set) -> Environment:
    html = fmt == "html"
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=html,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )

    def link(i):
        if html:
            return Markup('<a href="#{0}">{0}</a>').format(i) if i in known else escape(i)
        return f"[{i}](#{i})" if i in known else str(i)

    def links(ids):
        parts = [link(i) for i in ids]
        return Markup(", ").join(parts) if html else ", ".join(parts)

    def autolink(text):
        t = str(text or "")
        if html:
            out, pos = [], 0
            for m in ID_RE.finditer(t):
                out.append(escape(t[pos:m.start()]))
                out.append(link(m.group(1)))
                pos = m.end()
            out.append(escape(t[pos:]))
            return Markup("").join(out)
        return ID_RE.sub(lambda m: link(m.group(1)), _angle(t))

    env.filters.update(
        short=_short, ref=link, refs=links, autolink=autolink, dur=_fmt_dur, params=_fmt_params,
        cell=_mdcell, mdq=_mdquote, mdtext=_angle,
        crit=labels.criterion_name, critref=labels.criterion_ref, sevname=labels.severity_name,
        badge=labels.severity_badge, kind=labels.kind_name, source=labels.source_name,
    )
    env.globals["SEV_ICON"] = {s.value: labels.SEVERITIES[s][1] for s in Severity}
    return env


# ------------------------------------------------------------------ context ---
def build_context(data: ReportData) -> dict:
    reqs = data.requirements
    findings = data.findings
    n = len(reqs)
    rid_set = {r.rid for r in reqs}

    by_primary = {r.rid: [] for r in reqs}
    secondary = {r.rid: [] for r in reqs}
    orphan: list = []
    set_findings: list = []
    affected: set = set()
    for f in findings:
        affected.update(x for x in f.rids if x in rid_set)
        if f.scope == "set":
            set_findings.append(f)
            continue
        own = [x for x in f.rids if x in rid_set]
        if not own:
            orphan.append(f)
            continue
        by_primary[own[0]].append(f)
        for x in own[1:]:
            secondary[x].append(f)

    def status(rid):
        fs = by_primary[rid] + secondary[rid]
        if any(f.severity == Severity.critical for f in fs):
            return "critical"
        return "warn" if fs else "ok"

    req_rows = [{
        "r": r, "status": status(r.rid),
        "findings": by_primary[r.rid], "also": secondary[r.rid],
        "set_refs": [f for f in set_findings if r.rid in f.rids],
    } for r in reqs]

    sev_count = Counter(f.severity for f in findings)
    crit_table: "OrderedDict[Criterion, Counter]" = OrderedDict()
    for f in findings:
        crit_table.setdefault(f.criterion, Counter())[f.severity] += 1
    crit_rows = sorted(
        ({"criterion": c, "total": sum(cnt.values()),
          "critical": cnt[Severity.critical], "major": cnt[Severity.major], "minor": cnt[Severity.minor]}
         for c, cnt in crit_table.items()),
        key=lambda x: (-x["critical"], -x["total"]))

    fam_order = [name for name, _ in labels.FAMILIES] + [labels.OTHER_FAMILY]
    families = OrderedDict((k, []) for k in fam_order)
    for f in findings:
        if f.severity == Severity.critical:
            families[labels.family_of(f.criterion)].append(f)
    n_critical = sev_count[Severity.critical]

    dev, test = data.verdict.ready_for_development, data.verdict.ready_for_testing
    verdict_phrase = (
        "разработку и тестирование начинать можно" if dev and test else
        "разработку начинать можно, тестирование нельзя" if dev else
        "тестирование начинать можно, разработку нельзя" if test else
        "разрабатывать и тестировать по документу в нынешнем виде нельзя")
    if n == 0:
        summary_text = f"В документе не выделено ни одного требования. Вывод: {verdict_phrase}."
    else:
        summary_text = (
            f"Выделено требований: {n}; найдено замечаний: {len(findings)} "
            f"(критичных {n_critical}, существенных {sev_count[Severity.major]}, "
            f"незначительных {sev_count[Severity.minor]}); "
            f"требований с замечаниями: {len(affected)} из {n}. Вывод: {verdict_phrase}.")

    known = rid_set | {f.fid for f in findings if f.fid} | {q.qid for q in data.questions if q.qid}
    q_for_f: dict = {}
    for q in data.questions:
        for fid in q.fids:
            q_for_f.setdefault(fid, []).append(q.qid)

    return {
        "d": data, "p": data.passport, "n": n,
        "req_rows": req_rows,
        "clean_rows": [x for x in req_rows if x["status"] == "ok"],
        "dirty_rows": [x for x in req_rows if x["status"] != "ok"],
        "orphan": orphan, "set_findings": set_findings,
        "n_findings": len(findings), "sev_count": {s.value: sev_count[s] for s in SEV_ORDER},
        "crit_rows": crit_rows, "families": families, "n_critical": n_critical,
        "affected": len(affected), "share": round(100 * len(affected) / n) if n else 0,
        "summary_text": summary_text, "verdict_phrase": verdict_phrase,
        "questions": data.questions, "q_for_f": q_for_f,
        "set_criteria": list(data.set_summary.criteria.items()),
        "sha_short": (data.passport.doc_sha256 or "")[:12],
        "known": known, "severities": labels.SEVERITIES, "criteria_catalog": labels.CRITERIA,
        "n_questions": len(data.questions),
        "n_answered": sum(1 for q in data.questions if q.answer),
    }


def _render(data: ReportData, fmt: str) -> str:
    ctx = build_context(data)
    env = _make_env(fmt, ctx["known"])
    return env.get_template("report.html.j2" if fmt == "html" else "report.md.j2").render(**ctx)


def render_markdown(data: ReportData) -> str:
    return _render(data, "md")


def render_html(data: ReportData) -> str:
    return _render(data, "html")
