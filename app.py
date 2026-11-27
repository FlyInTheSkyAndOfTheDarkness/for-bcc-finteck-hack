"""reqcheck — демо-интерфейс (Streamlit). Запуск: streamlit run app.py"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from reqcheck.config import ROOT, load_settings
from reqcheck.models import ReportData, Severity
from reqcheck.report import render_html, render_markdown
from reqcheck.report.labels import (criterion_name, criterion_ref, kind_name, severity_badge,
                                    severity_name, source_name)

st.set_page_config(page_title="reqcheck — агент проверки требований", page_icon="🧭", layout="wide")

STAGES = [("ingest", "Чтение документа"), ("segment", "Нарезка требований"),
          ("linters", "Детерминированные проверки"), ("review", "Проверка требований"),
          ("set", "Проверка набора"), ("critic", "Critic: проверка находок"),
          ("clarify", "Уточняющие вопросы"), ("verdict", "Вердикт"), ("done", "Готово")]
SAMPLES = ["docs/requirements.md", "docs/requirements.pdf"]
PROVIDERS = {"fake (демо без ключа)": "fake", "gemini": "gemini", "openai": "openai",
             "anthropic": "anthropic", "ollama": "ollama"}
SKIP, CUSTOM = "— пропустить —", "свой вариант…"

st.markdown("""<style>
.block-container{padding-top:2rem}
.badge{display:inline-block;padding:.55rem 1.1rem;border-radius:.6rem;font-size:1.15rem;font-weight:600;margin-right:.6rem}
.ok{background:#DFF3E4;color:#14532D}.bad{background:#FBE0E0;color:#7F1D1D}
.sub{color:#5B6B7B;margin-top:-.8rem;margin-bottom:1rem}
</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------------ helpers --
def _save_upload(up) -> str:
    d = Path(tempfile.mkdtemp(prefix="reqcheck_"))
    p = d / Path(up.name).name
    p.write_bytes(up.getvalue())
    return str(p)


def _stage_view(state: dict, box) -> None:
    lines = []
    for key, title in STAGES:
        if key in state:
            lines.append(f"{'✅' if state[key]['done'] else '⏳'} **{title}** — {state[key]['msg']}")
        else:
            lines.append(f"⬜ {title}")
    box.markdown("\n\n".join(lines))


def run_agent(path: str, provider: str, no_cache: bool, ask=None, tag: str = "report") -> tuple[ReportData, Path]:
    from reqcheck.pipeline import run
    out_dir = st.session_state.get("out_dir")
    if not out_dir:
        out_dir = ROOT / "reports" / "ui" / datetime.now().strftime("%Y%m%d-%H%M%S")
        st.session_state["out_dir"] = out_dir
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{tag}.md"
    settings = load_settings(provider=provider, use_cache=not no_cache, trace_path=out.with_suffix(".trace.jsonl"))
    if settings.provider not in ("fake", "ollama") and not settings.api_key:
        raise RuntimeError(f"Нет API-ключа для провайдера «{settings.provider}». Укажите ключ в .env или выберите fake.")
    state: dict = {}
    order = [k for k, _ in STAGES]
    with st.status("Агент работает…", expanded=True) as status:
        box = st.empty()
        _stage_view(state, box)

        def progress(stage: str, msg: str) -> None:
            for k in order:           # предыдущие стадии считаем завершёнными
                if k == stage:
                    break
                if k in state:
                    state[k]["done"] = True
            state[stage] = {"msg": msg, "done": stage == "done"}
            _stage_view(state, box)

        try:
            data = run(path, settings, progress=progress, ask=ask)
        except Exception as e:  # noqa: BLE001
            status.update(label=f"Ошибка: {e}", state="error")
            raise
        status.update(label=f"Готово за {data.passport.duration_s} с · вызовов LLM: {data.passport.llm_calls}",
                      state="complete", expanded=False)
    out.write_text(render_markdown(data), encoding="utf-8")
    out.with_suffix(".html").write_text(render_html(data), encoding="utf-8")
    out.with_suffix(".json").write_text(data.model_dump_json(indent=2), encoding="utf-8")
    return data, out


def _fkey(f) -> tuple:
    return (f.criterion.value, " ".join(f.quote.split())[:80] or f.title)


def diff_findings(before: ReportData, after: ReportData) -> dict:
    b = {_fkey(f): f for f in before.findings}
    a = {_fkey(f): f for f in after.findings}
    return {"dropped": [b[k] for k in b if k not in a],
            "new": [a[k] for k in a if k not in b],
            "changed": [(b[k], a[k]) for k in b if k in a and (
                b[k].severity != a[k].severity or b[k].recommendation != a[k].recommendation
                or b[k].explanation != a[k].explanation)]}


# ------------------------------------------------------------------ sidebar --
up = None
with st.sidebar:
    st.header("Настройки")
    _has_key = bool(load_settings(provider="gemini").api_key)   # есть ключ Gemini → он по умолчанию
    prov_label = st.selectbox("Провайдер LLM", list(PROVIDERS), key="provider",
                              index=list(PROVIDERS.values()).index("gemini") if _has_key else 0)
    provider = PROVIDERS[prov_label]
    cfg = load_settings(provider=provider)
    st.caption(f"Модель: `{cfg.model or 'авто'}`")
    if provider in ("fake", "ollama"):
        st.caption("Ключ не требуется ✓")
    else:
        st.caption("API-ключ: " + ("найден ✓" if cfg.api_key else "не найден ✗"))
    no_cache = st.checkbox("Не использовать кэш LLM", value=False, key="no_cache")
    st.divider()
    src = st.radio("Источник документа", ["Использовать пример", "Загрузить файл"], key="source")
    path_to_run = None
    if src == "Использовать пример":
        sample = st.selectbox("Пример", [f"Использовать пример: {k}" for k in SAMPLES], key="sample")
        rel = sample.split(": ", 1)[1]
        if (ROOT / rel).exists():
            path_to_run = str(ROOT / rel)
        else:
            st.warning(f"Файл {rel} не найден")
    else:
        up = st.file_uploader("Файл требований", type=["pdf", "docx", "md", "txt", "html"], key="upload")
        if up is not None:
            path_to_run = "__upload__"
    run_clicked = st.button("▶ Проверить требования", type="primary", width="stretch",
                            key="run_btn", disabled=path_to_run is None)

# ------------------------------------------------------------------- header --
st.title("reqcheck — агент проверки требований")
st.markdown('<div class="sub">ISO/IEC/IEEE 29148 · IEEE 830 · fintech-чек-лист</div>', unsafe_allow_html=True)

if run_clicked:
    st.session_state.pop("out_dir", None)
    for k in [k for k in st.session_state if str(k).startswith(("ans_", "cust_"))]:
        del st.session_state[k]
    real = _save_upload(up) if path_to_run == "__upload__" else path_to_run
    try:
        res, res_out = run_agent(real, provider, no_cache)
        st.session_state.update(data=res, out=res_out, doc_path=real, provider_used=provider,
                                no_cache_used=no_cache, prev=None, diff=None)
    except Exception as e:  # noqa: BLE001
        st.error(f"Не удалось выполнить проверку: {e}")

data: ReportData | None = st.session_state.get("data")
if data is None:
    st.info("Выберите документ слева и нажмите «Проверить требования». "
            "Для демо без ключа используйте провайдер fake.")
    st.stop()

out: Path = st.session_state["out"]
tabs = st.tabs(["Итог", "Нарезка", "Замечания", "Уточняющие вопросы", "Отчёт", "Трассировка"])

# -------------------------------------------------------------------- Итог --
with tabs[0]:
    v = data.verdict
    st.subheader(data.passport.doc_name)
    c1, c2 = st.columns(2)
    for col, label, ok in ((c1, "К разработке", v.ready_for_development),
                           (c2, "К тестированию", v.ready_for_testing)):
        col.markdown(f'<span class="badge {"ok" if ok else "bad"}">{"✅" if ok else "⛔"} {label}: '
                     f'{"готово" if ok else "не готово"}</span>', unsafe_allow_html=True)
    st.write(v.rationale)
    if v.top_fixes:
        st.markdown("**Что исправить в первую очередь**")
        for i, t in enumerate(v.top_fixes, 1):
            st.markdown(f"{i}. {t}")
    cnt = {s: sum(1 for f in data.findings if f.severity == s) for s in Severity}
    m = st.columns(6)
    m[0].metric("Требований", len(data.requirements))
    m[1].metric("Замечаний", len(data.findings))
    m[2].metric(severity_badge(Severity.critical), cnt[Severity.critical])
    m[3].metric(severity_badge(Severity.major), cnt[Severity.major])
    m[4].metric("Вызовов LLM", data.passport.llm_calls)
    m[5].metric("Время, с", data.passport.duration_s)
    if data.set_summary.criteria or data.set_summary.notes:
        with st.expander("Оценка набора требований целиком"):
            for k, val in data.set_summary.criteria.items():
                st.markdown(f"- **{criterion_name(k)}**: {val}")
            for n in data.set_summary.notes:
                st.markdown(f"- {n}")

# ----------------------------------------------------------------- Нарезка --
with tabs[1]:
    df = pd.DataFrame([{"R-ID": r.rid, "ID в документе": r.doc_id or "—", "Вид": kind_name(r.kind),
                        "Формулировка": r.summary, "Место": r.location} for r in data.requirements])
    st.caption(f"Выделено требований: {len(df)}")
    st.dataframe(df, width="stretch", hide_index=True)

# --------------------------------------------------------------- Замечания --
with tabs[2]:
    f1, f2, f3 = st.columns(3)
    sev_sel = f1.multiselect("Важность", [s.value for s in Severity], default=[s.value for s in Severity],
                             format_func=severity_name, key="f_sev")
    used_crit = sorted({f.criterion.value for f in data.findings})
    crit_sel = f2.multiselect("Критерий", used_crit, default=[], format_func=criterion_name, key="f_crit",
                              placeholder="все")
    scope_sel = f3.selectbox("Область", ["все", "requirement", "set"], key="f_scope",
                             format_func=lambda x: {"все": "все", "requirement": "отдельные требования",
                                                    "set": "набор в целом"}[x])
    shown = [f for f in data.findings if f.severity.value in sev_sel
             and (not crit_sel or f.criterion.value in crit_sel) and (scope_sel == "все" or f.scope == scope_sel)]
    st.caption(f"Показано {len(shown)} из {len(data.findings)}")
    for f in shown:
        rids = ", ".join(f.rids)
        with st.expander(f"{f.fid} · {severity_badge(f.severity)} · {criterion_name(f.criterion)} · {f.title}"
                         + (f"  [{rids}]" if rids else "")):
            if f.quote:
                st.markdown("> " + f.quote.replace("\n", "\n> "))
            st.caption(f"Место: {f.location or '—'}" + ("" if f.grounded else " · ⚠ цитата не подтверждена"))
            st.markdown(f"**Почему это дефект.** {f.explanation}")
            st.markdown(f"**Как исправить.** {f.recommendation}")
            st.caption(f"Критерий: {criterion_name(f.criterion)} ({criterion_ref(f.criterion)}) · "
                       f"уверенность {f.confidence:.0%} · источник: {source_name(f.source)}"
                       + (" · проверено Critic ✓" if f.verified else ""))

# ------------------------------------------------------ Уточняющие вопросы --
with tabs[3]:
    qs = data.questions
    if not qs:
        st.success("Уточняющие вопросы не потребовались.")
    else:
        st.write("Ответьте на вопросы, которые блокируют проверку, — агент пересмотрит замечания.")
        for q in qs:
            with st.container(border=True):
                st.markdown(f"**{q.qid}. {q.question}**")
                st.caption(f"Зачем: {q.why}" + (f" · замечания: {', '.join(q.fids)}" if q.fids else ""))
                opts = list(q.options) + [CUSTOM, SKIP]
                choice = st.radio("Ответ", opts, index=len(opts) - 1, key=f"ans_{q.qid}",
                                  label_visibility="collapsed")
                if choice == CUSTOM:
                    st.text_input("Ваш вариант", key=f"cust_{q.qid}")
        if st.button("Учесть ответы и пересчитать", type="primary", key="recalc_btn"):
            answers: dict[str, str] = {}
            for q in qs:
                ch = st.session_state.get(f"ans_{q.qid}", SKIP)
                if ch == CUSTOM:
                    ch = (st.session_state.get(f"cust_{q.qid}") or "").strip() or SKIP
                if ch != SKIP:
                    answers[q.question] = ch
            if not answers:
                st.warning("Ни одного ответа не выбрано.")
            else:
                try:
                    new, new_out = run_agent(st.session_state["doc_path"], st.session_state["provider_used"],
                                             st.session_state["no_cache_used"],
                                             ask=lambda q: answers.get(q.question), tag="report_answered")
                    st.session_state.update(prev=data, diff=diff_findings(data, new), data=new, out=new_out)
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"Пересчёт не удался: {e}")
        prev = st.session_state.get("prev")
        diff = st.session_state.get("diff")
        if prev is not None and diff is not None:
            st.divider()
            st.subheader("Что изменилось после ответов")
            a, b, c = st.columns(3)
            a.metric("Замечаний было → стало", f"{len(prev.findings)} → {len(data.findings)}",
                     delta=len(data.findings) - len(prev.findings), delta_color="inverse")
            b.metric("Снято", len(diff["dropped"]))
            c.metric("Изменено", len(diff["changed"]))
            for f in diff["dropped"]:
                st.markdown(f"- ➖ **{f.fid}** снято: {f.title}")
            for o, n in diff["changed"]:
                st.markdown(f"- ✏️ **{o.fid} → {n.fid}** изменено: {n.title}"
                            + (f" ({severity_name(o.severity)} → {severity_name(n.severity)})"
                               if o.severity != n.severity else ""))
            for f in diff["new"]:
                st.markdown(f"- ➕ **{f.fid}** новое: {f.title}")

# ------------------------------------------------------------------ Отчёт --
with tabs[4]:
    md = out.read_text(encoding="utf-8")
    d1, d2, d3, _ = st.columns([1, 1, 1, 3])
    d1.download_button("⬇ .md", md, file_name="report.md", mime="text/markdown", key="dl_md")
    d2.download_button("⬇ .html", out.with_suffix(".html").read_text(encoding="utf-8"),
                       file_name="report.html", mime="text/html", key="dl_html")
    d3.download_button("⬇ .json", out.with_suffix(".json").read_text(encoding="utf-8"),
                       file_name="report.json", mime="application/json", key="dl_json")
    st.caption(f"Файлы сохранены в {out.parent}")
    st.markdown(md)

# ------------------------------------------------------------- Трассировка --
with tabs[5]:
    tp = out.with_suffix(".trace.jsonl")
    rows = []
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rows.append({"Время": r.get("ts"), "Тег": r.get("tag"), "Модель": r.get("model"),
                         "Задержка, с": r.get("latency_s"), "Токены": r.get("tokens"),
                         "Кэш": "да" if r.get("cache_hit") else "нет", "Ошибка": r.get("error") or ""})
    if rows:
        tdf = pd.DataFrame(rows)
        st.caption(f"Вызовов: {len(tdf)} · из кэша: {(tdf['Кэш'] == 'да').sum()} · "
                   f"токенов: {int(tdf['Токены'].fillna(0).sum())}")
        st.dataframe(tdf, width="stretch", hide_index=True)
    else:
        st.info("Журнал вызовов LLM пуст (провайдер fake не пишет трассировку или вызовов не было).")
