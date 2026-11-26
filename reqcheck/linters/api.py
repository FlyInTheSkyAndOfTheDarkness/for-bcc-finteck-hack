"""Проверки API-спецификации: семантика HTTP-метода, данные в строке запроса, коды ответа,
тело запроса, типы параметров запроса и ответа."""
from __future__ import annotations

import re
from typing import Optional

from reqcheck.linters.common import Ctx, cell_values, sentence_at, short
from reqcheck.models import Block, BlockKind, Criterion, Finding, Severity
from reqcheck.segment import PARAM_FMT_COL, PARAM_NAME_COL, STATUS_HEADING_RE, _status_entries

METHOD_KEY = re.compile(r"^(rest ?type|http ?method|method|метод( http)?|тип (rest|http|запроса|метода))$", re.I)
DESC_KEY = re.compile(r"^(description|описание|назначение|наименование( метода)?|summary)$", re.I)
URL_KEY = re.compile(r"^(request url|url( запроса| метода)?|endpoint|адрес( метода| запроса)?|path|curl)$", re.I)
HTTP_METHOD = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b")
METHOD_PATH = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/{}\-.]+)")
WRITE_RU = re.compile(r"(?<![а-яё])(сохранени|сохраня|сохранит|создани|созда[её]т|изменени|изменя|удалени|"
                      r"удаля|обновлени|обновля|регистраци|запис[ьиа]|замен[аяиу]|установк|отключени|"
                      r"включени|отмен[аяиу]|блокировк|перевод[аыу]? средств|списани)\w*", re.I)
READ_RU = re.compile(r"(?<![а-яё])(чтени|получени|просмотр|выгрузк|поиск|запрос данных)\w*", re.I)
WRITE_EN = {"save", "create", "update", "delete", "remove", "set", "add", "edit", "modify", "register",
            "cancel", "change", "write", "put", "post", "insert", "upsert", "block", "unblock", "enable",
            "disable", "reset", "submit", "confirm", "approve", "transfer"}
SENSITIVE = re.compile(r"card|pan\b|account|iban|cvv|cvc|token|password|passw|secret|\biin\b|phone|email|"
                       r"(?<![а-яё])(карт|сч[её]т|парол|телефон|паспорт|иин|токен)\w*", re.I)
QUERY_SECTION = re.compile(r"query|строк\w* запроса|параметры url|url-параметр|query string", re.I)
REQ_SECTION = re.compile(r"запрос|request|query|path|header|заголовк|body|тело|входн|input", re.I)
RESP_SECTION = re.compile(r"ответ|response|output|выходн|результат", re.I)
BODY_SECTION = re.compile(r"request body|тело запроса|^body$|^тело$", re.I)
BODY_UNUSED = re.compile(r"не используется|не передается|не передаётся|отсутствует|без тела|not used|"
                         r"\bnone\b|не требуется|пуст", re.I)
CODE_RE = re.compile(r"(?<![\d.,\-–])([1-5]\d{2})(?![\d])")
HTTP_CODES = {"100", "200", "201", "202", "204", "206", "301", "302", "303", "304", "307", "308", "400",
              "401", "402", "403", "404", "405", "406", "408", "409", "410", "412", "413", "415", "422",
              "423", "425", "428", "429", "500", "501", "502", "503", "504"}
REASONS = {
    "200": ["OK"], "201": ["Created"], "202": ["Accepted"], "204": ["No Content"],
    "301": ["Moved Permanently"], "302": ["Found"], "304": ["Not Modified"],
    "400": ["Bad Request"], "401": ["Unauthorized"], "403": ["Forbidden"], "404": ["Not Found"],
    "405": ["Method Not Allowed"], "409": ["Conflict"], "415": ["Unsupported Media Type"],
    "422": ["Unprocessable Entity", "Unprocessable Content"], "429": ["Too Many Requests"],
    "500": ["Internal Server Error"], "502": ["Bad Gateway"], "503": ["Service Unavailable"],
    "504": ["Gateway Timeout"],
}
RETURNS_OTHER = re.compile(r"(возвраща\w*|верн[уеё]\w*|отда[её]\w*|отвеча\w*|returns?|returned|responds?)"
                           r"[^.;]{0,25}?(?<![\d])([1-5]\d{2})(?!\d)", re.I)
ERROR_PAYLOAD = re.compile(r'"(result|status|state|outcome)"\s*:\s*"(error|fail\w*|err|declined|reject\w*)"'
                           r'|"(error|errorCode|error_code)"\s*:', re.I)


def _kv(b: Block) -> Optional[tuple[str, str]]:
    cells = cell_values(b)
    if b.kind == BlockKind.table_row and len(cells) == 2:
        return cells[0], cells[1]
    return None


def _url_tokens(url: str) -> list[str]:
    path = re.sub(r"https?://[^/]+", "", url)
    path = path.split("?")[0]
    toks = []
    for seg in re.split(r"[/\s\"'$]+", path):
        for t in re.split(r"[_\-.]|(?<=[a-z])(?=[A-Z])", seg):
            if t:
                toks.append(t.lower())
    return toks


class _Api:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.blocks = ctx.api_blocks()
        self.method: Optional[tuple[str, Block]] = None
        self.desc: list[Block] = []
        self.urls: list[Block] = []
        for b in self.blocks:
            kv = _kv(b)
            if kv:
                k, v = kv
                if METHOD_KEY.match(k) and HTTP_METHOD.search(v) and not self.method:
                    self.method = (HTTP_METHOD.search(v).group(1), b)
                elif DESC_KEY.match(k):
                    self.desc.append(b)
                elif URL_KEY.match(k):
                    self.urls.append(b)
            elif b.kind != BlockKind.heading:
                m = METHOD_PATH.search(b.text)
                if m and not self.method:
                    self.method = (m.group(1), b)
                    self.urls.append(b)

    def write_evidence(self) -> list[tuple[Block, str]]:
        ev = []
        for b in self.desc:
            m = WRITE_RU.search(_kv(b)[1])
            if m:
                ev.append((b, f"описание «{short(_kv(b)[1], 60)}»"))
        for b in self.urls:
            val = _kv(b)[1] if _kv(b) else b.text
            hit = [t for t in _url_tokens(val) if t in WRITE_EN]
            if hit:
                ev.append((b, f"адрес содержит «{hit[0]}»"))
        return ev


# ---------------------------------------------------------------- d. HTTP --
def check_method(api: _Api) -> list[Finding]:
    ctx, out = api.ctx, []
    if not api.method:
        return out
    method, mb = api.method
    ev = api.write_evidence()
    if method == "GET" and ev:
        why = "; ".join(dict.fromkeys(e for _, e in ev))
        out.append(ctx.finding(
            criterion=Criterion.correct, severity=Severity.major,
            title="GET для операции, изменяющей состояние",
            quote=mb.text, block=mb, extra_blocks=[b for b, _ in ev],
            explanation=(f"Метод объявлен как GET, но операция изменяет данные ({why}). По RFC 9110 GET "
                         "должен быть безопасным и идемпотентным: его кэшируют прокси, повторяют браузеры "
                         "и предзагрузчики, параметры попадают в журналы — изменение настроек через GET "
                         "приводит к непреднамеренным повторным изменениям и утечке данных."),
            recommendation=("Используйте POST/PUT (или PATCH) с телом запроса application/json для "
                            "сохранения; GET оставьте только для чтения настроек."),
            confidence=0.93))
    # противоречивое описание: одновременно запись и чтение
    for b in api.desc:
        v = _kv(b)[1]
        w, r = WRITE_RU.search(v), READ_RU.search(v)
        if w and r:
            out.append(ctx.finding(
                criterion=Criterion.set_consistent, severity=Severity.minor,
                title="Описание метода совмещает запись и чтение",
                quote=b.text, block=b,
                explanation=(f"В описании метода одновременно указаны «{w.group(0)}» и «{r.group(0)}»: "
                             "неясно, что делает метод — сохраняет данные или возвращает их."),
                recommendation="Опишите назначение метода однозначно; чтение и сохранение — отдельные методы.",
                confidence=0.85))
    return out


def check_query_sensitive(api: _Api) -> list[Finding]:
    ctx = api.ctx
    method = api.method[0] if api.method else None
    rows = []
    for b in api.blocks:
        if b.kind != BlockKind.table_row or not b.table_header:
            continue
        in_query = any(QUERY_SECTION.search(h) for h in b.section_path)
        ni = next((i for i, h in enumerate(b.table_header) if PARAM_NAME_COL.search(h.strip())), None)
        if not in_query or ni is None:
            continue
        cells = cell_values(b)
        name = cells[ni] if ni < len(cells) else ""
        if SENSITIVE.search(name) or SENSITIVE.search(" ".join(cells[ni + 1:])):
            rows.append((b, name))
    if not rows:
        return []
    b0, n0 = rows[0]
    names = ", ".join(f"«{n}»" for _, n in rows)
    writes = bool(api.write_evidence())
    return [ctx.finding(
        criterion=Criterion.appropriate, severity=Severity.major,
        title="Чувствительные данные в строке запроса (query)",
        quote=b0.text, block=b0, extra_blocks=[b for b, _ in rows[1:]],
        explanation=(f"Параметры {names} передаются в строке URL" +
                     (f" метода {method}" if method else "") +
                     (", который к тому же изменяет состояние" if writes and method == "GET" else "") +
                     ". URL целиком сохраняется в журналах веб-серверов, прокси, балансировщиков и истории, "
                     "поэтому идентификаторы карт/счетов и изменяемые значения не должны в нём передаваться "
                     "(PCI DSS, требования ИБ банка)."),
        recommendation=("Передавайте идентификатор карты и изменяемые значения в теле POST/PUT-запроса "
                        "или используйте непрозрачный токен карты; маскируйте значения в журналах."),
        confidence=0.88)]


# ------------------------------------------------------- e. status codes --
def check_statuses(api: _Api) -> list[Finding]:
    ctx, out = api.ctx, []
    described: dict[str, Block] = {}
    frags: dict[str, str] = {}
    headings: dict[str, Block] = {}
    entry_spans: dict[str, list[tuple[int, int]]] = {}
    for b in api.blocks:
        if b.kind == BlockKind.heading:
            m = STATUS_HEADING_RE.match(b.text)
            if m and m.group("code") in HTTP_CODES:
                headings.setdefault(m.group("code"), b)
        elif b.kind == BlockKind.text and ctx.mode(b) == "api_status":
            for code, frag in _status_entries(b.text):
                described.setdefault(code, b)
                frags.setdefault(code, frag)
                st = b.text.find(frag)
                entry_spans.setdefault(b.id, []).append((st, st + len(code)))
    if not described and not headings:
        return out
    documented = set(described) | set(headings)

    def own_heading_code(b: Block) -> Optional[str]:
        for h in reversed(b.section_path):
            m = STATUS_HEADING_RE.match(h)
            if m and m.group("code") in HTTP_CODES:
                return m.group("code")
        return None

    # 1. фраза причины не соответствует коду
    for code, hb in headings.items():
        for other, phrases in REASONS.items():
            if other == code:
                continue
            for ph in phrases:
                rx = re.compile(rf"\b{re.escape(ph)}\b" if ph in ("OK", "Found") else rf"\b{re.escape(ph)}\b", 0 if ph in ("OK", "Found") else re.I)
                if rx.search(hb.text) and not any(re.search(rf"\b{re.escape(p)}\b", hb.text, re.I)
                                                  for p in REASONS.get(code, [])):
                    out.append(ctx.finding(
                        criterion=Criterion.correct, severity=Severity.major,
                        title=f"Код {code} подписан как «{ph}» (это код {other})",
                        quote=hb.text, block=hb, extra_blocks=[described[code]] if code in described else [],
                        rids=ctx.rids_for_quote(described[code], frags[code]) if code in described else [],
                        explanation=(f"Заголовок «{hb.text}» сочетает код {code} с фразой «{ph}», которая по "
                                     f"RFC 9110 соответствует коду {other}. Разработчик и тестировщик не "
                                     f"смогут определить, какой код ожидать."),
                        recommendation=(f"Выберите один код: «{code} {REASONS.get(code, [''])[0]}» или "
                                        f"«{other} {ph}» — и приведите заголовок, описание и примеры к нему."),
                        confidence=0.92))
                    break
        # класс: «Successful» у 4xx/5xx или «Unsuccessful/Error» у 2xx
        low = hb.text.casefold()
        if code.startswith("2") and re.search(r"unsuccessful|\berror\b|ошибк|неуспешн", low):
            out.append(ctx.finding(
                criterion=Criterion.correct, severity=Severity.minor,
                title=f"Код {code} помечен как неуспешный", quote=hb.text, block=hb, rids=[],
                explanation=f"Код {code} относится к успешным (2xx), но заголовок «{hb.text}» называет его ошибкой.",
                recommendation="Согласуйте класс кода и описание.", confidence=0.85))
        if code[0] in "45" and re.search(r"(?<!un)successful|(?<!не)успешн", low):
            out.append(ctx.finding(
                criterion=Criterion.correct, severity=Severity.minor,
                title=f"Код {code} помечен как успешный", quote=hb.text, block=hb, rids=[],
                explanation=f"Код {code} — ошибка ({code[0]}xx), но заголовок «{hb.text}» называет его успешным.",
                recommendation="Согласуйте класс кода и описание.", confidence=0.85))

    # 2. в разделе кода X сказано, что возвращается другой код
    flagged_other: set[str] = set()
    for b in api.blocks:
        if b.kind not in (BlockKind.text, BlockKind.list_item):
            continue
        x = own_heading_code(b)
        if not x:
            continue
        for m in RETURNS_OTHER.finditer(b.text):
            y = m.group(2)
            if y in HTTP_CODES and y != x:
                flagged_other.add(y)
                quote = sentence_at(b.text, m.start())
                out.append(ctx.finding(
                    criterion=Criterion.set_consistent, severity=Severity.major,
                    title=f"В описании кода {x} указано, что возвращается {y}",
                    quote=quote, block=b, extra_blocks=[headings[x]],
                    explanation=(f"Раздел «{headings[x].text}» описывает код {x}, но в нём сказано: "
                                 f"«{short(quote, 120)}». Неясно, какой код вернёт сервис в этой ситуации; "
                                 f"описание кода {x} фактически противоречит самому себе."),
                    recommendation=(f"Определите один код для ситуации и опишите его в собственном разделе; "
                                    f"для {x} оставьте только случаи, когда он действительно возвращается."),
                    confidence=0.9))
                break

    # 3. ошибочный результат в примере успешного кода
    for b in api.blocks:
        if b.kind != BlockKind.code:
            continue
        x = own_heading_code(b)
        if not x or not x.startswith("2"):
            continue
        m = ERROR_PAYLOAD.search(b.text)
        if m:
            out.append(ctx.finding(
                criterion=Criterion.set_consistent, severity=Severity.major,
                title=f"Пример ответа {x} содержит ошибку",
                quote=b.text, block=b,
                rids=sorted(set(ctx.rids_for([b]) + (ctx.rids_for_quote(described[x], frags[x])
                                                     if x in described else []))),
                explanation=(f"Среди примеров успешного кода {x} («{headings[x].text}») приведён ответ с "
                             f"признаком ошибки ({m.group(0)}). Клиент не сможет различать успех и отказ по "
                             f"коду HTTP; неясно, когда возвращается {x} с ошибкой, а когда 4xx."),
                recommendation=(f"Для отказа по бизнес-правилу используйте код 4xx (например, 400 или 422) с "
                                f"телом ошибки; в примерах {x} оставьте только успешный результат."),
                confidence=0.9))

    # 4. упомянутые, но не описанные коды
    mentions: dict[str, tuple[Block, int]] = {}
    for b in api.blocks:
        if b.kind in (BlockKind.code, BlockKind.heading, BlockKind.table_header):
            continue
        spans = entry_spans.get(b.id, [])
        for m in CODE_RE.finditer(b.text):
            c = m.group(1)
            if c not in HTTP_CODES or any(s <= m.start() < e for s, e in spans):
                continue
            if b.kind == BlockKind.table_row:
                continue  # значения в таблицах параметров — не упоминания кодов
            mentions.setdefault(c, (b, m.start()))
    for c, (b, p) in sorted(mentions.items()):
        if c in documented:
            continue
        quote = sentence_at(b.text, p)
        success = c.startswith("2") and re.search(r"успешн|success", quote, re.I)
        sev = Severity.major if c in flagged_other or not c.startswith("2") else Severity.minor
        out.append(ctx.finding(
            criterion=Criterion.set_complete, severity=sev,
            title=f"Код {c} упоминается, но не описан",
            quote=quote, block=b,
            explanation=(f"Код {c} упомянут («{short(quote, 100)}»)" +
                         (" и объявлен успешным" if success else "") +
                         f", но в перечне статусов для него нет описания (условие возврата, тело ответа). "
                         f"Описаны только: {', '.join(sorted(documented))}."),
            recommendation=(f"Добавьте описание кода {c} (когда возвращается, пример тела) или уберите "
                            f"упоминание, если код не используется."),
            confidence=0.88))
    return out


# --------------------------------------------------------- f. request body --
def check_body(api: _Api) -> list[Finding]:
    ctx, out = api.ctx, []
    body_blocks = [b for b in api.blocks if b.kind != BlockKind.heading and b.section_path
                   and BODY_SECTION.search(b.section_path[-1].strip())]
    if not body_blocks:
        return out
    ct = next((b for b in body_blocks if re.search(r"content-type", b.text, re.I)), None)
    unused = next((b for b in body_blocks if BODY_UNUSED.search(b.text) and b is not ct), None)
    if ct and unused:
        out.append(ctx.finding(
            criterion=Criterion.set_consistent, severity=Severity.minor,
            title="Указан Content-Type, но тело запроса не используется",
            quote=ct.text, block=ct, extra_blocks=[unused],
            explanation=(f"В разделе тела запроса указано «{short(ct.text, 60)}» и одновременно "
                         f"«{short(unused.text, 60)}». Content-Type описывает тело; без тела он не имеет "
                         f"смысла, и неясно, ожидает ли сервис JSON."),
            recommendation="Уберите Content-Type либо опишите тело запроса (поля, формат, пример).",
            confidence=0.88))
    method = api.method[0] if api.method else None
    has_body = any(b.kind in (BlockKind.table_row, BlockKind.code) for b in body_blocks)
    if method == "GET" and has_body and not unused:
        b0 = next(b for b in body_blocks if b.kind in (BlockKind.table_row, BlockKind.code))
        out.append(ctx.finding(
            criterion=Criterion.correct, severity=Severity.major,
            title="Тело запроса у метода GET", quote=b0.text, block=b0, extra_blocks=[api.method[1]],
            explanation="Для метода GET описано тело запроса; по RFC 9110 у GET тело не имеет определённой "
                        "семантики и часто отбрасывается прокси и клиентами.",
            recommendation="Используйте POST/PUT для передачи тела или перенесите параметры в query.",
            confidence=0.85))
    return out


# ----------------------------------------------------------- g. type mismatch --
def _type_class(t: str) -> Optional[str]:
    t = t.casefold().strip()
    if not t or t in {"—", "-"}:
        return None
    if re.match(r"(int|integer|long|short|int32|int64|bigint|целое)", t):
        return "integer"
    if re.match(r"(number|decimal|float|double|numeric|money|число)", t):
        return "number"
    if re.match(r"(string|str|text|varchar|char|строка)", t):
        return "string"
    if re.match(r"(bool|boolean|логическ)", t):
        return "boolean"
    if re.match(r"(date|datetime|timestamp|дата)", t):
        return "date"
    if re.match(r"(object|json|array|list|объект|массив)", t):
        return "object"
    return t


def check_types(api: _Api) -> list[Finding]:
    ctx, out = api.ctx, []
    req: dict[str, tuple[str, Block]] = {}
    resp: dict[str, tuple[str, Block]] = {}
    for b in api.blocks:
        if b.kind != BlockKind.table_row or not b.table_header:
            continue
        hdr = b.table_header
        ni = next((i for i, h in enumerate(hdr) if PARAM_NAME_COL.search(h.strip())), None)
        fi = next((i for i, h in enumerate(hdr) if PARAM_FMT_COL.search(h.strip())), None)
        if ni is None or fi is None:
            continue
        cells = cell_values(b)
        if max(ni, fi) >= len(cells) or not cells[ni]:
            continue
        name, fmt = cells[ni].strip(), cells[fi].strip()
        path = " / ".join(b.section_path)
        if any(RESP_SECTION.search(h) for h in b.section_path):
            resp.setdefault(name.casefold(), (fmt, b))
        elif any(REQ_SECTION.search(h) for h in b.section_path):
            req.setdefault(name.casefold(), (fmt, b))
        del path
    for name, (fq, bq) in req.items():
        if name not in resp:
            continue
        fr, br = resp[name]
        cq, cr = _type_class(fq), _type_class(fr)
        if cq and cr and cq != cr:
            pname = cell_values(br)[0] if cell_values(br) else name
            out.append(ctx.finding(
                criterion=Criterion.set_consistent, severity=Severity.major,
                title=f"Параметр {pname}: {fq} в запросе, {fr} в ответе",
                quote=br.text, block=br, extra_blocks=[bq],
                explanation=(f"Параметр «{pname}» в запросе имеет формат «{fq}» ({bq.locator()}), а в ответе — "
                             f"«{fr}» ({br.locator()}). Одно и то же значение описано разными типами; клиент "
                             f"и тесты не смогут однозначно сравнить отправленное и полученное значение."),
                recommendation=f"Приведите формат «{pname}» к одному типу в запросе, ответе и примерах.",
                confidence=0.9))
    return out


def run(ctx: Ctx) -> list[Finding]:
    api = _Api(ctx)
    if not api.blocks:
        return []
    return (check_method(api) + check_query_sensitive(api) + check_statuses(api) +
            check_body(api) + check_types(api))
