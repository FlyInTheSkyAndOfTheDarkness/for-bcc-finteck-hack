"""Стадия 2. Нарезка документа на требования. Владелец: A3.

1. Детерминированный проход по блокам (работает и без LLM):
   - явные идентификаторы (Ф-1, ФТ-2, НФТ-1, FR-3, REQ-12, BR-1.2…) сохраняются как doc_id;
   - вид требования определяется по заголовкам раздела (типовой шаблон банка:
     «Цели», «Функциональные требования», «Требования к данным», «Критерии приемки»,
     «Логика работы метода», «Параметры запроса», «Статусы»…);
   - единица требования — единица документа: пункт списка, абзац, строка таблицы,
     описание кода ответа. Пункты НЕ дробятся на предложения (неатомарность — дефект,
     о котором должен сообщить ревьюер);
   - служебные разделы шаблона (чек-лист ревью, согласующие, «заполняются …») —
     одно meta-требование на раздел.
2. Необязательное уточнение LLM (schemas.SegResult): улучшает summary/kind и добавляет
   пропущенные правилами обязательства. Добавления принимаются только с подтверждённой
   цитатой и без пересечения с уже найденными; удалять требования LLM не может.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from reqcheck.grounding import GROUND_THRESHOLD, find_quote, normalize
from reqcheck.llm import LLMClient
from reqcheck.models import Block, BlockKind, Document, ReqKind, Requirement

log = logging.getLogger(__name__)

MAX_SUMMARY_WORDS = 15
LLM_CHUNK_CHARS = 40000

# ------------------------------------------------------------------ patterns ---
# Явный идентификатор требования в начале пункта: «Ф-1.», «НФТ-2:», «FR-3)», «BR-1.2», «REQ-12».
ID_RE = re.compile(
    r"^\s*(?:\d+[.)]\s+)?[\[(]?(?P<id>[A-ZА-ЯЁ][A-ZА-ЯЁa-zа-яё]{0,6}[-‑–]\d+(?:\.\d+)*)[\])]?"
    r"(?=[.:)\s]|$)[.:)]?\s*")
NUM_RE = re.compile(r"^\s*\d+(?:\.\d+)*[.)]\s+")
MODAL_RE = re.compile(r"(?<![а-яё])(долж(?:ен|н[аоы]?)|обязан\w*|необходимо|требуется|следует|"
                      r"не допускается|запрещ\w+)(?![а-яё])|\b(shall|must|should)\b", re.I)
BOILERPLATE_RE = re.compile(r"^(не используют?ся|не используется|не применяется|не применимо|"
                            r"нет|отсутству\w+|не требуется|n/?a|tbd|—|-|или|и|либо)\.?$", re.I)
STATUS_ENTRY_RE = re.compile(r"(?:^|(?<=[.;:!?]\s)|(?<=\s\s))(?P<code>[1-5]\d{2})\.\s+(?=\S)")
STATUS_HEADING_RE = re.compile(r"^\s*(?:http\s*)?(?P<code>[1-5]\d{2})\b")

# Заголовок раздела → режим. Проверяется от самого глубокого заголовка к верхнему.
_H = lambda p: re.compile(p, re.I)  # noqa: E731
SECTION_RULES: list[tuple[re.Pattern, str]] = [
    (_H(r"критери\w*\s+при[её]мк|acceptance|приемочн|при[её]мочн"), "acceptance"),
    (_H(r"ревью|чек-?лист|согласующ|согласовани|история изменени|изменения документа|"
        r"версии документа|change ?log|тестирование требований|требования к тестированию"), "meta"),
    (_H(r"^общие сведения|^паспорт|^о документе|^сведения о документе|^реквизиты"), "meta"),
    (_H(r"^цел[иь]|бизнес-?требовани|business requirement|^goals?\b|^назначение"), "business"),
    (_H(r"дизайн|\bui\b|\bux\b|интерфейс|макет|экранн?"), "ui"),
    (_H(r"нефункциональн|non-?functional|\bnfr\b|производительност|надежност|надёжност|безопасност"),
     "nonfunctional"),
    (_H(r"требовани\w* к данным|модел\w* данных|^данные|\bdata\b|хранени\w* данных"), "data"),
    (_H(r"интеграци|integration"), "integration"),
    (_H(r"архитектур|bpmn|sequence|диаграмм|^схема|бизнес-?процесс|процесс"), "process"),
    (_H(r"функциональн|functional|use ?case|сценари|пользовательск\w+ истори|user stor"), "functional"),
]
API_SECTION_RULES: list[tuple[re.Pattern, str]] = [
    (_H(r"^схема|диаграмм|sequence|bpmn|архитектур"), "process"),
    (_H(r"статус|коды? ответ|http[- ]?(коды|status)|^status|^[1-5]\d{2}\b"), "api_status"),
    (_H(r"общие параметры|general|основные параметры|описание метода|сведения о методе"), "api_general"),
    (_H(r"логика|правил|алгоритм|обработк|валидаци|параметры|query|path|header|заголовк|request|"
        r"тело|body|ответ|response|ошибк|error|пример|метод"), "api"),
]
# Признак API-спецификации в пути раздела.
API_CTX_RE = _H(r"\bapi\b|\brest\b|\bsoap\b|grpc|swagger|openapi|endpoint|логика работы метода|"
                r"параметры запроса|^статусы|коды ответа|общие параметры")
# Строки таблицы «Общие параметры», которые несут требование (метод, URL, авторизация, описание).
API_GENERAL_KEYS = _H(r"^(description|описание|назначение|rest ?type|http ?method|method|метод|"
                      r"тип (rest|http|запроса|метода)|authorization|authentication|auth|"
                      r"авторизаци\w*|аутентификаци\w*|request url|url( запроса| метода)?|endpoint|"
                      r"адрес( метода| запроса)?|content-type|timeout|тайм-?аут|идемпотентн\w*|"
                      r"rate ?limit|ограничени\w*)$")
PARAM_NAME_COL = _H(r"^(parameter|param|параметр|поле|field|name|наименование|имя|атрибут)\b")
PARAM_FMT_COL = _H(r"^(format|type|тип|формат)\b")
PARAM_DESC_COL = _H(r"^(description|описание|назначение)\b")

# Номер раздела в начале заголовка: «2.», «3.1», «IV.», «A)», «Раздел 2.».
HEADING_NUM_RE = re.compile(r"^\s*(?:(?:раздел|глава|section|part)\s+)?"
                            r"(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[А-ЯЁA-Z][.)])\s+", re.I)
# Несколько требований с ID в одном абзаце (PDF без интервалов между строками):
# «FR-1. … FR-2. …» — ID в начале предложения.
INLINE_ID_RE = re.compile(r"(?:^|(?<=[.;!?])\s+)(?P<id>[A-ZА-ЯЁ][A-ZА-ЯЁa-zа-яё]{0,6}[-‑–]\d+(?:\.\d+)*)"
                          r"[.:)]\s+(?=\S)")

FLEX_KINDS = {ReqKind.functional, ReqKind.nonfunctional, ReqKind.data, ReqKind.integration,
              ReqKind.ui, ReqKind.process, ReqKind.business}


# --------------------------------------------------------------- helpers ---
@dataclass
class _Cand:
    kind: ReqKind
    quote: str
    blocks: list[Block]
    doc_id: Optional[str] = None
    summary: str = ""
    order: tuple = field(default_factory=tuple)


def _headings(b: Block) -> list[str]:
    """Заголовки, под которыми стоит блок (без заголовка части и названия документа)."""
    path = list(b.section_path or [])
    if b.kind == BlockKind.heading and path and path[-1] == b.text:
        path = path[:-1]
    return path


def section_mode(b: Block) -> tuple[Optional[str], bool]:
    """(режим раздела | None, в API-спецификации?)."""
    path = _headings(b)
    api_ctx = any(API_CTX_RE.search(h) for h in path) or bool(b.part and API_CTX_RE.search(b.part))
    for h in reversed(path):
        hh = h.strip()
        bare = HEADING_NUM_RE.sub("", hh, count=1)
        # сначала заголовок как есть, затем без номера («2. Цели проекта» → «Цели проекта»)
        for cand in ((hh, bare) if bare and bare != hh else (hh,)):
            if api_ctx:
                for rx, mode in API_SECTION_RULES:
                    if rx.search(cand):
                        return mode, True
            for rx, mode in SECTION_RULES:
                if rx.search(cand):
                    return (mode if not (api_ctx and mode in ("functional",)) else "api"), api_ctx
    return None, api_ctx


def explicit_id(text: str) -> Optional[str]:
    m = ID_RE.match(text or "")
    if not m:
        return None
    rid = m.group("id").replace("‑", "-").replace("–", "-")
    # отсекаем коды вида «HTTP-200», «UTF-8», «ISO-29148»
    prefix = rid.split("-")[0]
    if prefix.upper() in {"HTTP", "UTF", "ISO", "IEC", "IEEE", "ГОСТ", "SHA", "TLS", "RFC", "UTC", "GMT"}:
        return None
    return rid


def _strip_prefix(text: str) -> str:
    m = ID_RE.match(text)
    if m:
        text = text[m.end():]
    text = NUM_RE.sub("", text, count=1)
    return text.strip()


def _words_cut(s: str, n: int = MAX_SUMMARY_WORDS) -> str:
    w = s.split()
    out = " ".join(w[:n]).rstrip(" ,;:—-")
    return out + "…" if len(w) > n else out


def summarize(text: str) -> str:
    """Первая фраза без номера, ≤ 15 слов."""
    t = _strip_prefix(" ".join((text or "").split()))
    first = re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z0-9«\"])", t, maxsplit=1)[0]
    first = first.rstrip(".;: ")
    if len(first.split()) > MAX_SUMMARY_WORDS:
        clause = re.split(r"[;:]\s+", first, maxsplit=1)[0]
        first = clause if 6 <= len(clause.split()) <= MAX_SUMMARY_WORDS else first
    return _words_cut(first)


def _col(header: list[str], rx: re.Pattern) -> Optional[int]:
    for i, h in enumerate(header or []):
        if rx.search((h or "").strip()):
            return i
    return None


def _row_summary(b: Block, mode: str) -> str:
    cells = [c.strip() for c in (b.cells or b.text.split("|"))]
    hdr = b.table_header or []
    nonempty = [c for c in cells if c]
    if not nonempty:
        return ""
    if mode == "acceptance" and len(cells) >= 3:
        c0, c1, c2 = cells[0], cells[1], cells[2]
        if explicit_id(c0) and len(cells) >= 4:
            c0, c1, c2 = cells[1], cells[2], cells[3]
        return _words_cut(f"{c1} при условии «{c0}» → {c2}")
    ni, fi, di = _col(hdr, PARAM_NAME_COL), _col(hdr, PARAM_FMT_COL), _col(hdr, PARAM_DESC_COL)
    if ni is not None and ni < len(cells) and (di is not None or fi is not None) and len(cells) > 2:
        parts = [f"Параметр {cells[ni]}"]
        if fi is not None and fi < len(cells) and cells[fi]:
            parts[0] += f" ({cells[fi]})"
        if di is not None and di < len(cells) and cells[di]:
            parts.append(cells[di])
        return _words_cut(": ".join(parts))
    if len(cells) == 2:
        return _words_cut(f"{cells[0]}: {cells[1]}")
    return _words_cut(" — ".join(nonempty))


def _is_trivial(text: str) -> bool:
    t = " ".join((text or "").split()).strip()
    if not t:
        return True
    if BOILERPLATE_RE.match(t):
        return True
    return len(re.findall(r"\w+", t)) < 2


def _status_entries(text: str) -> list[tuple[str, str]]:
    """Разбить абзац «200. Ответ … 400. Ответ …» на (код, фрагмент дословно)."""
    ms = list(STATUS_ENTRY_RE.finditer(text))
    if not ms:
        return []
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        frag = text[m.start():end].strip()
        out.append((m.group("code"), frag))
    if ms[0].start() > 0 and text[:ms[0].start()].strip():
        return []   # абзац начинается не с кода — это не перечень статусов
    return out


def _id_entries(text: str) -> list[tuple[str, str]]:
    """Разбить абзац «FR-1. … FR-2. …» на (ID, фрагмент дословно), если в нём ≥ 2 ID одного префикса."""
    ms = [m for m in INLINE_ID_RE.finditer(text) if explicit_id(m.group("id") + " ")]
    if len(ms) < 2 or ms[0].start("id") != len(text) - len(text.lstrip()):
        return []
    prefixes = {m.group("id").replace("‑", "-").replace("–", "-").split("-")[0] for m in ms}
    if len(prefixes) != 1:
        return []
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start("id") if i + 1 < len(ms) else len(text)
        frag = text[m.start("id"):end].strip()
        out.append((explicit_id(frag) or m.group("id"), frag))
    return out


def _mode_kind(mode: Optional[str], api_ctx: bool) -> ReqKind:
    if mode is None:
        return ReqKind.api if api_ctx else ReqKind.functional
    if mode in ("api", "api_general", "api_status"):
        return ReqKind.api
    return ReqKind(mode)


def _meta_summary(heading: str, blocks: list[Block]) -> str:
    rows = sum(1 for b in blocks if b.kind == BlockKind.table_row)
    tail = f" (таблица, {rows} стр.)" if rows else ""
    return _words_cut(f"Служебный раздел шаблона «{heading}»{tail}")


# ------------------------------------------------------------- rule pass ---
def rule_segment(doc: Document) -> list[_Cand]:
    cands: list[_Cand] = []
    meta_groups: dict[tuple, list[Block]] = {}
    pos = {b.id: i for i, b in enumerate(doc.blocks)}

    for b in doc.blocks:
        if b.kind == BlockKind.heading or not (b.text or "").strip():
            continue
        mode, api_ctx = section_mode(b)
        text = b.text.strip()
        did = explicit_id(text) if b.kind in (BlockKind.list_item, BlockKind.text) else None
        if b.kind == BlockKind.table_row and b.cells:
            did = explicit_id(b.cells[0].strip()) if b.cells[0].strip() else None
            if did and normalize(did) != normalize(b.cells[0].strip().rstrip(".")):
                did = None
        order = (pos[b.id], 0)

        if mode == "meta":
            key = tuple(_headings(b))
            meta_groups.setdefault(key, []).append(b)
            continue
        if b.kind == BlockKind.table_header:
            continue
        if b.kind == BlockKind.table_row and not any((c or "").strip() for c in (b.cells or [text])):
            continue

        if b.kind in (BlockKind.text, BlockKind.list_item) and mode != "api_status":
            entries = _id_entries(text)
            if entries:
                kind = _mode_kind(mode, api_ctx)
                for k, (eid, frag) in enumerate(entries):
                    cands.append(_Cand(kind, frag, [b], eid, summarize(frag), (pos[b.id], k)))
                continue

        if mode is None:
            # раздел без «требовательного» заголовка: берём только явно пронумерованные
            # требования и фразы с модальностью обязательства
            if b.kind == BlockKind.code or not (did or MODAL_RE.search(text)):
                continue
            kind = ReqKind.api if api_ctx else ReqKind.functional
            cands.append(_Cand(kind, text, [b], did, summarize(text), order))
            continue

        if b.kind == BlockKind.code:
            if mode == "process":
                cands.append(_Cand(ReqKind.process, text, [b], None, summarize(text), order))
            continue   # примеры JSON и т.п. не являются требованиями

        if mode == "acceptance":
            if b.kind == BlockKind.table_row:
                cands.append(_Cand(ReqKind.acceptance, text, [b], did, _row_summary(b, mode), order))
            elif not _is_trivial(text):
                cands.append(_Cand(ReqKind.acceptance, text, [b], did, summarize(text), order))
            continue

        if mode == "api_general":
            if b.kind == BlockKind.table_row:
                key = (b.cells[0] if b.cells else text.split("|")[0]).strip()
                if API_GENERAL_KEYS.search(key):
                    cands.append(_Cand(ReqKind.api, text, [b], did, _row_summary(b, mode), order))
            elif not _is_trivial(text):
                cands.append(_Cand(ReqKind.api, text, [b], did, summarize(text), order))
            continue

        if mode == "api_status":
            if b.kind == BlockKind.text:
                entries = _status_entries(text)
                if entries:
                    for k, (code, frag) in enumerate(entries):
                        body = frag[len(code) + 1:].strip()
                        cands.append(_Cand(ReqKind.api, frag, [b], None,
                                           _words_cut(f"Код {code}: {summarize(body)}"), (pos[b.id], k)))
                    continue
            if b.kind == BlockKind.table_row:
                cands.append(_Cand(ReqKind.api, text, [b], did, _row_summary(b, mode), order))
            elif not _is_trivial(text):
                cands.append(_Cand(ReqKind.api, text, [b], did, summarize(text), order))
            continue

        kind = ReqKind.api if mode == "api" else ReqKind(mode)
        if b.kind == BlockKind.table_row:
            cands.append(_Cand(kind, text, [b], did, _row_summary(b, mode), order))
        elif not _is_trivial(text):
            cands.append(_Cand(kind, text, [b], did, summarize(text), order))

    for key, blocks in meta_groups.items():
        heading = key[-1] if key else "служебный раздел"
        first = next((x for x in blocks if x.kind == BlockKind.text or
                      (x.kind == BlockKind.table_row and any(c.strip() for c in x.cells or []))), blocks[0])
        quote = first.text.strip()
        if not quote.replace("|", "").strip():
            quote = heading
        cands.append(_Cand(ReqKind.meta, quote, blocks, None, _meta_summary(heading, blocks),
                           (pos[blocks[0].id], 0)))
    cands.sort(key=lambda c: c.order)
    return cands


# ------------------------------------------------------------ LLM refine ---
def _doc_text(blocks: list[Block]) -> str:
    out, last_path = [], None
    for b in blocks:
        if b.kind == BlockKind.heading:
            out.append(f"\n## {b.text}")
            continue
        p = " › ".join(b.section_path)
        if p != last_path and not out:
            out.append(f"## {p}")
        last_path = p
        line = f"[строка {b.line_start}] " if b.line_start else ""
        out.append(f"{line}{b.text}")
    return "\n".join(out).strip()


def _cand_payload(cands: list[_Cand]) -> str:
    items = [{"n": i + 1, "doc_id": c.doc_id, "kind": c.kind.value, "draft_summary": c.summary,
              "quote": c.quote if len(c.quote) <= 400 else c.quote[:400]} for i, c in enumerate(cands)]
    return json.dumps(items, ensure_ascii=False, indent=0)


def _match_cand(item, cands: list[_Cand]) -> Optional[_Cand]:
    if item.doc_id:
        for c in cands:
            if c.doc_id and normalize(c.doc_id) == normalize(item.doc_id):
                return c
    q = normalize(item.quote)
    if not q:
        return None
    best, best_len = None, 0
    for c in cands:
        cq = normalize(c.quote)
        if not cq:
            continue
        if q == cq or (len(q) >= 12 and (q in cq or cq in q)):
            overlap = min(len(q), len(cq))
            if overlap > best_len:
                best, best_len = c, overlap
    return best


def _clean_summary(s: str) -> Optional[str]:
    s = " ".join((s or "").split()).strip(" .")
    if not s or len(s.split()) > MAX_SUMMARY_WORDS + 5:
        return None
    return _words_cut(s)


def _refine_chunk(doc: Document, blocks: list[Block], cands: list[_Cand], llm: LLMClient,
                  covered: set[str]) -> list[_Cand]:
    from reqcheck.prompts import load
    from reqcheck.schemas import SegResult

    try:
        system = load("_common") + "\n\n" + load("segment_refine")
    except FileNotFoundError:
        system = load("segment_refine")
    user = ("## Кандидаты, найденные правилами (не удалять, сохранить порядок)\n"
            f"{_cand_payload(cands)}\n\n## Текст документа\n{_doc_text(blocks)}")
    res = llm.generate_json(system, user, SegResult, tag="segment")
    added: list[_Cand] = []
    pos = {b.id: i for i, b in enumerate(doc.blocks)}
    updated: set[int] = set()
    for item in res.requirements:
        c = _match_cand(item, cands)
        if c is not None:
            if id(c) in updated:
                continue          # дубль уже сопоставленного кандидата
            updated.add(id(c))
            s = _clean_summary(item.summary)
            if s:
                c.summary = s
            if c.kind in FLEX_KINDS and item.kind in FLEX_KINDS:
                c.kind = item.kind
            continue
        # добавление: только с подтверждённой цитатой и вне уже покрытых блоков
        quote = " ".join((item.quote or "").split())
        if len(quote) < 15:
            continue
        score, blk = find_quote(doc, quote)
        if score < GROUND_THRESHOLD or blk is None or blk.id in covered:
            continue
        if blk.kind in (BlockKind.heading, BlockKind.table_header):
            continue
        kind = item.kind if item.kind not in (ReqKind.meta,) else ReqKind.meta
        s = _clean_summary(item.summary) or summarize(quote)
        did = item.doc_id if item.doc_id and normalize(item.doc_id) in normalize(blk.text) else None
        nc = _Cand(kind, quote, [blk], did, s, (pos[blk.id], 1))
        added.append(nc)
        covered.add(blk.id)
    return added


def llm_refine(doc: Document, cands: list[_Cand], llm: LLMClient) -> list[_Cand]:
    """Уточнить кандидатов через LLM; при любой ошибке вернуть кандидатов как есть."""
    covered = {b.id for c in cands for b in c.blocks}
    total = sum(len(b.text) for b in doc.blocks)
    if total <= LLM_CHUNK_CHARS:
        chunks = [(doc.blocks, cands)]
    else:
        parts: dict[Optional[str], list[Block]] = {}
        for b in doc.blocks:
            parts.setdefault(b.part, []).append(b)
        chunks = []
        for part, blocks in parts.items():
            ids = {b.id for b in blocks}
            chunks.append((blocks, [c for c in cands if c.blocks[0].id in ids]))
    added: list[_Cand] = []
    for blocks, part_cands in chunks:
        try:
            added += _refine_chunk(doc, blocks, part_cands, llm, covered)
        except Exception as e:  # noqa: BLE001 — LLM не должна ломать нарезку
            log.warning("segment: LLM-уточнение пропущено: %s", e)
    out = cands + added
    out.sort(key=lambda c: c.order)
    return out


# ----------------------------------------------------------------- public ---
def _to_requirements(doc: Document, cands: list[_Cand]) -> list[Requirement]:
    reqs = []
    width = 2 if len(cands) < 100 else 3
    for i, c in enumerate(cands, 1):
        score, _ = find_quote(doc, c.quote)
        reqs.append(Requirement(
            rid=f"R-{i:0{width}d}", doc_id=c.doc_id, kind=c.kind,
            summary=c.summary or summarize(c.quote), quote=c.quote,
            block_ids=[b.id for b in c.blocks], location=c.blocks[0].locator(),
            grounded=score >= GROUND_THRESHOLD))
    return reqs


def segment(doc: Document, llm: Optional[LLMClient]) -> list[Requirement]:
    """Правила + LLM → список требований с R-NN, doc_id, цитатой и локатором."""
    cands = rule_segment(doc)
    if llm is not None:
        cands = llm_refine(doc, cands, llm)
    return _to_requirements(doc, cands)
