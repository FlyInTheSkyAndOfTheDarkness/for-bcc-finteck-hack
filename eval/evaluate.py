"""Оценка отчёта reqcheck против gold-разметки.

    python -m eval.evaluate reports/report.json [--gold eval/gold.yaml] [--judge]
                            [--out eval/results/<name>.md] [--threshold 80] [--any-criterion]
    python -m eval.evaluate --compare a.json b.json ... [--out eval/results/ablation.md]

Сопоставление находка ↔ gold:
  1. Кандидаты — по похожести цитат (rapidfuzz.partial_ratio после нормализации eval.textnorm):
     цитата gold ↔ цитата находки; цитата gold ↔ пояснение находки (≥ 90, только для
     цитат ≥ 15 символов); цитата gold ↔ цитаты требований, на которые ссылается находка
     (вес 0.85, только при точном/альтернативном критерии).
  2. Лексическое правило: похожесть ≥ порога И совместимый критерий (тот же, из alt_criteria
     или того же семейства, см. eval.gold.FAMILIES). Каждая находка закрепляется за одним
     лучшим gold (score = 0.6·цитата + 0.25·похожесть текста + 0.15·критерий); несколько
     находок на один gold допускаются (дубликаты).
  3. --judge: один пакетный вызов LLM сопоставляет находки с gold ID (eval.judge); там
     находка может закрывать несколько gold, если явно говорит об обоих.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from eval.gold import DEFAULT_GOLD, GoldItem, family, load_gold
from eval.textnorm import norm, quote_sim

SEV_ORDER = {"minor": 0, "major": 1, "critical": 2}
DEFAULT_THRESHOLD = 80.0


# ------------------------------------------------------------------ input ---
@dataclass
class FView:
    """Находка отчёта в виде, независимом от точной версии схемы ReportData."""
    idx: int
    fid: str
    criterion: str
    severity: str
    title: str
    quote: str
    explanation: str
    location: str = ""
    rids: list[str] = field(default_factory=list)
    req_quotes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.fid or f"#{self.idx + 1}"


def _enum_val(v) -> str:
    return getattr(v, "value", v) or ""


def findings_from_dict(data: dict) -> list[FView]:
    reqs = {r.get("rid"): r for r in data.get("requirements", []) or []}
    out = []
    for i, f in enumerate(data.get("findings", []) or []):
        rids = list(f.get("rids") or [])
        out.append(FView(
            idx=i, fid=f.get("fid") or "", criterion=_enum_val(f.get("criterion")),
            severity=_enum_val(f.get("severity")), title=f.get("title") or "",
            quote=f.get("quote") or "", explanation=f.get("explanation") or "",
            location=f.get("location") or "", rids=rids,
            req_quotes=[reqs[r].get("quote") or "" for r in rids if r in reqs],
        ))
    return out


def load_report(path: str | Path) -> tuple[list[FView], dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    p = data.get("passport", {}) or {}
    meta = {
        "report": str(path),
        "doc_name": p.get("doc_name", ""),
        "provider": p.get("provider", ""),
        "model": p.get("model", ""),
        "agent_version": p.get("agent_version", ""),
        "prompt_version": p.get("prompt_version", ""),
        "requirements": len(data.get("requirements", []) or []),
    }
    return findings_from_dict(data), meta


# --------------------------------------------------------------- matching ---
@dataclass
class Pair:
    f: int            # индекс находки
    g: str            # gold id
    evidence: float   # похожесть по месту (0..100)
    score: float      # итоговый балл для выбора лучшего gold
    via: str          # quote | explanation | requirement | judge


@dataclass
class _GoldNorm:
    item: GoldItem
    quotes: list[str]
    text: str


def _evidence(f: FView, fq: str, fe: str, frq: list[str], g: _GoldNorm) -> tuple[float, str]:
    best, via = 0.0, "quote"
    for gq in g.quotes:
        s = quote_sim(gq, fq, normalized=True)
        if s > best:
            best, via = s, "quote"
        if len(gq) >= 15 and fe:
            se = fuzz.partial_ratio(gq, fe)
            if se >= 90 and se * 0.95 > best:
                best, via = se * 0.95, "explanation"
        for rq in frq:
            sr = quote_sim(gq, rq, normalized=True) * 0.85
            if sr > best:
                best, via = sr, "requirement"
    return best, via


def candidates(findings: list[FView], gold: list[GoldItem], *, threshold: float = DEFAULT_THRESHOLD,
               any_criterion: bool = False) -> dict[int, list[Pair]]:
    """Все допустимые пары (находка → gold), отсортированные по score."""
    gn = [_GoldNorm(g, [norm(q) for q in g.quotes], norm(f"{g.title} {g.description}"))
          for g in gold]
    out: dict[int, list[Pair]] = {}
    for f in findings:
        fq, fe = norm(f.quote), norm(f.explanation)
        frq = [norm(q) for q in f.req_quotes]
        ft = norm(f"{f.title} {f.explanation}")
        pairs = []
        for g in gn:
            ev, via = _evidence(f, fq, fe, frq, g)
            if ev < threshold:
                continue
            cs = g.item.criterion_score(f.criterion)
            if not any_criterion and (cs == 0 or (via == "requirement" and cs < 80)):
                continue
            sem = fuzz.token_set_ratio(ft, g.text) if ft else 0.0
            pairs.append(Pair(f.idx, g.item.id, round(ev, 1),
                              round(0.6 * ev + 0.25 * sem + 0.15 * cs, 2), via))
        out[f.idx] = sorted(pairs, key=lambda p: -p.score)
    return out


def match_lexical(findings: list[FView], gold: list[GoldItem], *, threshold: float = DEFAULT_THRESHOLD,
                  any_criterion: bool = False) -> list[Pair]:
    """Каждая находка закрепляется за лучшим допустимым gold (или ни за каким)."""
    cands = candidates(findings, gold, threshold=threshold, any_criterion=any_criterion)
    return [ps[0] for ps in cands.values() if ps]


# ---------------------------------------------------------------- metrics ---
def _ratio(a: int, b: int) -> Optional[float]:
    return round(a / b, 3) if b else None


def compute_metrics(findings: list[FView], gold: list[GoldItem], pairs: list[Pair]) -> dict:
    by_id = {g.id: g for g in gold}
    fmap = {f.idx: f for f in findings}
    per_gold: dict[str, list[Pair]] = {}
    for p in sorted(pairs, key=lambda p: -p.score):
        if p.g in by_id:
            per_gold.setdefault(p.g, []).append(p)
    matched_f = {p.f for p in pairs if p.g in by_id}
    found = set(per_gold)

    def rec(items: list[GoldItem]) -> dict:
        n, k = len(items), sum(1 for g in items if g.id in found)
        return {"n": n, "found": k, "recall": _ratio(k, n)}

    recall = _ratio(len(found), len(gold))
    precision = _ratio(len(matched_f), len(findings))
    f1 = (round(2 * precision * recall / (precision + recall), 3)
          if precision and recall else (0.0 if gold and findings else None))

    # согласие по критичности и критерию: лучшая находка для каждого найденного gold
    sev_cm: dict[str, dict[str, int]] = {}
    sev_agree = sev_over = sev_under = crit_exact = crit_compat = 0
    for gid, ps in per_gold.items():
        g, f = by_id[gid], fmap[ps[0].f]
        gs, fs = g.severity.value, f.severity
        sev_cm.setdefault(gs, {}).setdefault(fs, 0)
        sev_cm[gs][fs] += 1
        d = SEV_ORDER.get(fs, -9) - SEV_ORDER[gs]
        sev_agree += d == 0
        sev_over += d > 0
        sev_under += d < 0 and d > -9
        crit_exact += f.criterion == g.criterion.value
        crit_compat += g.criterion_score(f.criterion) > 0

    crits = sorted({g.criterion.value for g in gold})
    fams = sorted({family(g.criterion.value) for g in gold})
    summary = {
        "n_gold": len(gold),
        "n_findings": len(findings),
        "recall": recall,
        "must_find": rec([g for g in gold if g.must_find]),
        "by_severity": {s: rec([g for g in gold if g.severity.value == s])
                        for s in ("critical", "major", "minor")},
        "precision": precision,
        "f1": f1,
        "matched_findings": len(matched_f),
        "unmatched_findings": len(findings) - len(matched_f),
        "duplicates": sum(len(ps) - 1 for ps in per_gold.values()),
        "severity_agreement": _ratio(sev_agree, len(per_gold)),
        "severity_over": sev_over,
        "severity_under": sev_under,
        "severity_confusion": sev_cm,
        "criterion_exact": _ratio(crit_exact, len(per_gold)),
        "criterion_compatible": _ratio(crit_compat, len(per_gold)),
        "by_criterion": {c: rec([g for g in gold if g.criterion.value == c]) for c in crits},
        "by_family": {fa: rec([g for g in gold if family(g.criterion.value) == fa]) for fa in fams},
        "finding_criteria": _count(f.criterion for f in findings),
    }
    details = {
        "gold": [{
            "id": g.id, "title": g.title, "severity": g.severity.value, "criterion": g.criterion.value,
            "must_find": g.must_find,
            "matched_by": [{"finding": fmap[p.f].label, "title": fmap[p.f].title,
                            "criterion": fmap[p.f].criterion, "severity": fmap[p.f].severity,
                            "evidence": p.evidence, "score": p.score, "via": p.via}
                           for p in per_gold.get(g.id, [])],
        } for g in gold],
        "unmatched_findings": [{
            "finding": f.label, "criterion": f.criterion, "severity": f.severity,
            "title": f.title, "quote": f.quote[:200], "location": f.location,
        } for f in findings if f.idx not in matched_f],
    }
    return {"summary": summary, "details": details}


def _count(it) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------- evaluate ---
def evaluate(report_path: str | Path, gold_path: str | Path = DEFAULT_GOLD, *, judge: bool = False,
             threshold: float = DEFAULT_THRESHOLD, any_criterion: bool = False, client=None) -> dict:
    findings, meta = load_report(report_path)
    gold = load_gold(gold_path)
    if judge:
        from eval.judge import match_judge
        if client is None:
            from reqcheck.config import load_settings
            from reqcheck.llm import get_client
            client = get_client(load_settings())
        hints = candidates(findings, gold, threshold=min(threshold, 70.0), any_criterion=True)
        pairs = match_judge(findings, gold, client, hints=hints)
        mode = f"judge ({getattr(client, 'provider', '?')}/{getattr(client, 'model', '?')})"
    else:
        pairs = match_lexical(findings, gold, threshold=threshold, any_criterion=any_criterion)
        mode = f"lexical (порог {threshold:g}{', любой критерий' if any_criterion else ''})"
    res = compute_metrics(findings, gold, pairs)
    res["meta"] = {**meta, "gold": str(gold_path), "mode": mode}
    return res


# --------------------------------------------------------------- markdown ---
def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _rec_cell(r: dict) -> str:
    return f"{r['found']}/{r['n']} ({_pct(r['recall'])})"


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(res: dict) -> str:
    s, d, m = res["summary"], res["details"], res["meta"]
    L = [f"# Оценка отчёта reqcheck против gold", "",
         f"- Отчёт: `{m['report']}`", f"- Gold: `{m['gold']}` ({s['n_gold']} дефектов)",
         f"- Модель агента: {m.get('provider', '')}/{m.get('model', '')}, версия {m.get('agent_version', '')}",
         f"- Режим сопоставления: {m['mode']}", "",
         "## Итоговые метрики", "",
         "| Метрика | Значение |", "| :--- | :--- |",
         f"| Recall (все gold) | {_pct(s['recall'])} ({s['n_gold'] and sum(1 for g in d['gold'] if g['matched_by'])}/{s['n_gold']}) |",
         f"| Recall must_find | {_rec_cell(s['must_find'])} |"]
    for sev in ("critical", "major", "minor"):
        L.append(f"| Recall {sev} | {_rec_cell(s['by_severity'][sev])} |")
    L += [f"| Precision (сопоставленные находки / все) | {_pct(s['precision'])} ({s['matched_findings']}/{s['n_findings']}) |",
          f"| F1 | {_pct(s['f1'])} |",
          f"| Дубликаты (лишние находки на тот же gold) | {s['duplicates']} |",
          f"| Согласие по критичности | {_pct(s['severity_agreement'])} (завышено {s['severity_over']}, занижено {s['severity_under']}) |",
          f"| Критерий совпал точно / совместим | {_pct(s['criterion_exact'])} / {_pct(s['criterion_compatible'])} |",
          "", "## Recall по критериям gold", "", "| Критерий | Найдено |", "| :--- | :--- |"]
    L += [f"| {c} | {_rec_cell(r)} |" for c, r in s["by_criterion"].items()]
    L += ["", "| Семейство | Найдено |", "| :--- | :--- |"]
    L += [f"| {c} | {_rec_cell(r)} |" for c, r in s["by_family"].items()]
    L += ["", "## Матрица критичности (строки — gold, столбцы — агент)", "",
          "| gold \\ агент | critical | major | minor |", "| :--- | :--- | :--- | :--- |"]
    for gs in ("critical", "major", "minor"):
        row = s["severity_confusion"].get(gs, {})
        L.append(f"| {gs} | {row.get('critical', 0)} | {row.get('major', 0)} | {row.get('minor', 0)} |")
    L += ["", "## Gold-дефекты", "", "| ID | must | Крит. | Дефект | Найден | Находки |",
          "| :--- | :--- | :--- | :--- | :--- | :--- |"]
    for g in d["gold"]:
        mb = ", ".join(f"{x['finding']} ({x['via']}, {x['evidence']:.0f})" for x in g["matched_by"])
        L.append(f"| {g['id']} | {'да' if g['must_find'] else ''} | {g['severity']} | "
                 f"{_md_escape(g['title'])} | {'да' if g['matched_by'] else '**нет**'} | {mb} |")
    missed = [g for g in d["gold"] if g["must_find"] and not g["matched_by"]]
    if missed:
        L += ["", "### Пропущенные must_find", ""] + [f"- {g['id']}: {g['title']}" for g in missed]
    L += ["", "## Находки без пары в gold — возможные дополнительные истинные дефекты, проверить вручную", ""]
    if d["unmatched_findings"]:
        L += ["| Находка | Критерий | Крит. | Заголовок | Цитата |", "| :--- | :--- | :--- | :--- | :--- |"]
        L += [f"| {u['finding']} | {u['criterion']} | {u['severity']} | {_md_escape(u['title'])} | "
              f"{_md_escape(u['quote'][:120])} |" for u in d["unmatched_findings"]]
    else:
        L.append("Нет.")
    return "\n".join(L) + "\n"


def render_compare(rows: list[tuple[str, dict]]) -> str:
    L = ["# Сравнение прогонов (ablation)", "",
         "| Прогон | Модель | Находок | Recall | must_find | critical | major | minor | Precision | F1 | Дубл. | Согл. крит. |",
         "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, res in rows:
        s, m = res["summary"], res["meta"]
        b = s["by_severity"]
        L.append(f"| {_md_escape(name)} | {m.get('model', '')} | {s['n_findings']} | {_pct(s['recall'])} | "
                 f"{_pct(s['must_find']['recall'])} | {_pct(b['critical']['recall'])} | {_pct(b['major']['recall'])} | "
                 f"{_pct(b['minor']['recall'])} | {_pct(s['precision'])} | {_pct(s['f1'])} | {s['duplicates']} | "
                 f"{_pct(s['severity_agreement'])} |")
    if rows:
        L += ["", f"Режим: {rows[0][1]['meta']['mode']}; gold: `{rows[0][1]['meta']['gold']}`."]
    return "\n".join(L) + "\n"


def _run_name(path: str) -> str:
    p = Path(path)
    return f"{p.parent.name}/{p.stem}" if p.parent.name not in ("", ".") else p.stem


def _write(out: Optional[str], md: str, payload: dict) -> None:
    if out:
        o = Path(out)
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_text(md, encoding="utf-8")
        o.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"записано: {o} и {o.with_suffix('.json')}")
    else:
        print(md)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m eval.evaluate", description="Оценка report.json против gold")
    ap.add_argument("report", nargs="?", help="путь к report.json")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--judge", action="store_true", help="сопоставление одним пакетным вызовом LLM")
    ap.add_argument("--out", help="куда записать Markdown (рядом ляжет .json)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="порог похожести цитат (0..100)")
    ap.add_argument("--any-criterion", action="store_true", help="не требовать совместимости критерия")
    ap.add_argument("--compare", nargs="+", metavar="REPORT", help="несколько report.json → таблица ablation")
    a = ap.parse_args(argv)

    kw = dict(judge=a.judge, threshold=a.threshold, any_criterion=a.any_criterion)
    if a.compare:
        paths = ([a.report] if a.report else []) + a.compare
        rows = [(_run_name(p), evaluate(p, a.gold, **kw)) for p in paths]
        _write(a.out, render_compare(rows),
               {"runs": [{"name": n, "meta": r["meta"], "summary": r["summary"]} for n, r in rows]})
        return 0
    if not a.report:
        ap.error("укажите report.json или --compare")
    res = evaluate(a.report, a.gold, **kw)
    _write(a.out, render_markdown(res), res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
