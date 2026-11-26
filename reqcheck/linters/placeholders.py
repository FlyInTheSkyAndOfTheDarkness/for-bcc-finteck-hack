"""Незаполненный шаблон: альтернативы «Да / Нет», прочерки в обязательных полях, пустые таблицы,
фразы-заглушки («заполняются …», TBD, TODO, уточнить) и отсутствие версии/даты документа."""
from __future__ import annotations

import re

from reqcheck.linters.common import Ctx, cell_values, short
from reqcheck.models import Block, BlockKind, Criterion, Finding, Severity

_CHOICE = r"(да|нет|yes|no|не требуется|не применимо|n/?a|выполнено|не выполнено)"
CHOICE_RE = re.compile(rf"^\s*{_CHOICE}(\s*/\s*{_CHOICE})+\s*$", re.I)
DASH_VALUES = {"—", "–", "-", "--", "?", "…", "...", "tbd", "todo", "n/a"}
LABEL_DASH_RE = re.compile(r"(?P<label>[\wА-Яа-яЁё][\w\sА-Яа-яЁё/-]{0,30}):\s*[—–-](?=\s*(?:$|[.;,|]))")
STUB_RE = re.compile(
    r"(?<![а-яё])(заполня(ется|ются|ет)\s+\w+|будет\s+(определ|уточн|описан|добавл|заполн)\w*|"
    r"уточнить|уточняется|требует уточнения|не заполнен\w*|в разработке|на согласовании)(?![а-яё])"
    r"|\b(TBD|TBA|TODO|FIXME|XXX)\b|\?\?\?|<\s*(указать|заполнить|описать)[^>]*>", re.I)
CRITICAL_KEYS = re.compile(r"url|endpoint|адрес|метод|method|rest ?type|authorization|авторизац|"
                           r"версия|version|дата|date|swagger|контракт", re.I)
APPROVAL_HDR = re.compile(r"согласу|подпис|утвержд|фио|approv|sign", re.I)
VERSION_RE = re.compile(r"(верси[яи]|version|ревизи[яи]|редакци[яи])\s*(документа)?\s*[:№#]?\s*v?\d+(\.\d+)+"
                        r"|\bv\d+\.\d+\b", re.I)
DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./](\d{2}|\d{4})\b|\b\d{4}-\d{2}-\d{2}\b|"
                     r"\b\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|"
                     r"ноября|декабря)\s+\d{4}\b", re.I)
VERSION_MENTION = re.compile(r"(верси|дата)\w*.{0,40}(не заполнен|не указан|отсутству)|"
                             r"(не заполнен|не указан|отсутству)\w*.{0,40}(верси|дата)", re.I)


def _table_title(table: list[Block]) -> str:
    sp = table[0].section_path
    return sp[-1] if sp else "таблица"


def check_choices(ctx: Ctx) -> list[Finding]:
    out = []
    for table in ctx.tables():
        rows = [b for b in table if b.kind == BlockKind.table_row]
        hits = [(b, c) for b in rows for c in cell_values(b) if CHOICE_RE.match(c)]
        if not hits:
            continue
        rows_hit = sorted({b.id for b, _ in hits})
        first = hits[0][0]
        title = _table_title(table)
        out.append(ctx.finding(
            criterion=Criterion.conforming, severity=Severity.minor,
            title=f"Не выбраны значения «{hits[0][1]}» в таблице «{short(title, 40)}»",
            quote=first.text, block=first, scope="set",
            explanation=(f"В таблице «{title}» {len(hits)} ячеек в {len(rows_hit)} строках содержат "
                         f"неразрешённые альтернативы шаблона (например, «{hits[0][1]}»): отметки не "
                         f"проставлены, раздел оставлен в виде заготовки. Документ не соответствует "
                         f"собственному шаблону, а заявленные проверки фактически не выполнены."),
            recommendation=("Выберите одно значение в каждой ячейке (Да / Нет / Не требуется), "
                            "заполните примечания для ответов «Нет» или удалите неприменимые строки."),
            confidence=0.95))
    return out


def check_empty_tables(ctx: Ctx) -> list[Finding]:
    out = []
    for table in ctx.tables():
        header = table[0] if table[0].kind == BlockKind.table_header else None
        rows = [b for b in table if b.kind == BlockKind.table_row]
        empty = [b for b in rows if not any(cell_values(b))]
        title = _table_title(table)
        hdr_text = header.text if header else title
        approval = bool(APPROVAL_HDR.search(hdr_text) or APPROVAL_HDR.search(title))
        if rows and len(empty) != len(rows):
            continue
        if not rows and not (header and approval):
            continue   # таблица из одного заголовка — только для листа согласования
        anchor = header or rows[0]
        out.append(ctx.finding(
            criterion=Criterion.conforming, severity=Severity.major if approval else Severity.minor,
            title=(f"Не заполнен лист согласования «{short(title, 40)}»" if approval
                   else f"Пустая таблица «{short(title, 40)}»"),
            quote=anchor.text, block=anchor, scope="set",
            explanation=(f"Таблица «{title}» содержит только заголовок «{short(hdr_text, 80)}»" +
                         (f" и пустые строки ({len(rows)})." if rows else ", строк нет.") +
                         (" Нет ни одного согласующего: документ формально не согласован, "
                          "ответственные за решения не определены." if approval else "")),
            recommendation=("Укажите подразделения, ФИО ответственных и отметки о согласовании "
                            "до передачи документа в разработку." if approval
                            else "Заполните таблицу или удалите её из документа."),
            confidence=0.93))
    return out


def check_dashes(ctx: Ctx) -> list[Finding]:
    """Прочерки и пустые значения в таблицах «поле — значение» (2 столбца)."""
    out = []
    for table in ctx.tables():
        rows = [b for b in table if b.kind == BlockKind.table_row]
        kv = [b for b in rows if len(cell_values(b)) == 2]
        if not rows or len(kv) != len(rows) or len(rows) < 3:
            continue
        missing: list[tuple[Block, str]] = []
        for b in rows:
            key, val = cell_values(b)
            if not key:
                continue
            if val.casefold() in DASH_VALUES or not val:
                missing.append((b, key))
                continue
            for m in LABEL_DASH_RE.finditer(val):
                missing.append((b, f"{key} ({m.group('label').strip()})"))
        if not missing:
            continue
        crit = [k for b, k in missing if CRITICAL_KEYS.search(k)]
        first = next((b for b, k in missing if CRITICAL_KEYS.search(k)), missing[0][0])
        title = _table_title(table)
        names = ", ".join(f"«{k}»" for _, k in missing)
        out.append(ctx.finding(
            criterion=Criterion.set_complete, severity=Severity.major if crit else Severity.minor,
            title=f"Не заполнены поля таблицы «{short(title, 40)}»",
            quote=first.text, block=first, extra_blocks=[b for b, _ in missing],
            explanation=(f"В таблице «{title}» вместо значений стоят прочерки или пусто: {names}. "
                         "Набор требований неполон: без этих данных нельзя однозначно реализовать "
                         "и проверить решение."),
            recommendation=("Заполните поля (для неприменимых — явно «не применимо» с причиной); "
                            "для адресов укажите значения для всех сред."),
            confidence=0.9))
    return out


def check_stub_phrases(ctx: Ctx, skip: set[str]) -> list[Finding]:
    out = []
    for b in ctx.doc.blocks:
        if b.id in skip or b.kind in (BlockKind.heading, BlockKind.code):
            continue
        m = STUB_RE.search(b.text)
        if not m:
            continue
        section = b.section_path[-1] if b.section_path else "раздел"
        out.append(ctx.finding(
            criterion=Criterion.set_complete, severity=Severity.minor,
            title=f"Раздел «{short(section, 40)}» не заполнен (заглушка)",
            quote=b.text if len(b.text) <= 160 else m.group(0), block=b,
            explanation=(f"В разделе «{section}» вместо содержания стоит текст-заглушка «{m.group(0)}». "
                         "Раздел шаблона оставлен незаполненным, набор требований неполон."),
            recommendation="Заполните раздел содержательно или удалите его, если он неприменим.",
            confidence=0.9))
    return out


def check_metadata(ctx: Ctx) -> tuple[list[Finding], set[str]]:
    """Нет версии и даты документа → modifiable (IEEE 830 §4.3.8) / conforming."""
    texts = [b.text for b in ctx.doc.blocks]
    has_version = any(VERSION_RE.search(t) for t in texts)
    has_date = any(DATE_RE.search(t) for t in texts)
    mention = next((b for b in ctx.doc.blocks if VERSION_MENTION.search(b.text)), None)
    if has_version and has_date and mention is None:
        return [], set()
    if mention is not None:
        anchor, quote = mention, next((s for s in re.split(r"(?<=[.!?])\s+", mention.text)
                                       if VERSION_MENTION.search(s)), mention.text)
    else:
        anchor = next((b for b in ctx.doc.blocks if b.kind == BlockKind.heading), ctx.doc.blocks[0])
        quote = anchor.text
    missing = [n for n, ok in (("версия", has_version), ("дата", has_date)) if not ok]
    what = " и ".join(missing) if missing else "версия/дата"
    f = ctx.finding(
        criterion=Criterion.modifiable, severity=Severity.minor,
        title=f"Не указаны {what} документа",
        quote=quote, block=anchor, scope="set", rids=[],
        explanation=(f"В документе не зафиксированы {what}" +
                     (" — это прямо отмечено в тексте" if mention is not None else "") +
                     ". Без версии нельзя управлять изменениями, ссылаться на согласованную редакцию "
                     "и сопоставлять отчёт ревью с конкретным состоянием документа."),
        recommendation="Добавьте номер версии, дату, автора и историю изменений документа.",
        confidence=0.9)
    return [f], ({mention.id} if mention is not None else set())


def run(ctx: Ctx) -> list[Finding]:
    meta, used = check_metadata(ctx)
    return check_choices(ctx) + check_empty_tables(ctx) + check_dashes(ctx) + \
        check_stub_phrases(ctx, used) + meta
