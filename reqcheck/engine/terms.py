"""Индекс содержательных терминов требований и группы кандидатов на противоречия.

Полностью generic: никаких слов из конкретного документа. Термины — грубые основы слов
(русский суффиксный стеммер), латинские идентификаторы (имена полей), числа и единицы.
Сверхчастые в документе термины отбрасываются (df > 40% требований).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from reqcheck.models import ReqKind, Requirement

STOP = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до
его ее ей ему если есть еще же за здесь и из или им их к как ко когда кто ли либо между меня мне
может мы на над надо наш не него нее нет ни них но ну о об однако он она они оно от очень по под
при про с со так также такой там те тем то того тоже той только том ту ты у уже хотя чего чей чем
что чтобы чье эта эти это этого этой этом этот я либо каждый каждая каждое любой иной иные
должен должна должно должны необходимо следует требуется может могут быть является являются
система системы системе систему сервис сервиса сервисе требование требования требований
случае случай случаев если иначе который которая которое которые которых также т д п др
the a an of to in on for and or is are be by with from as at this that it shall must should
""".split())

UNITS = {"₸", "тенге", "тг", "тиын", "тиынах", "тиыны", "kzt", "usd", "%", "процент", "секунд",
         "секунды", "сек", "минут", "минуты", "минуту", "минуты", "час", "часа", "часов", "день",
         "дня", "дней", "сутки", "суток", "месяц", "месяца", "месяцев", "год", "года", "лет",
         "мс", "ms", "rps", "tps", "bit", "бит", "байт", "символов", "знак", "знака", "знаков"}

_SUFFIXES = sorted("""
иями ями ами ией ией ого его ому ему ыми ими ых их ая яя ое ее ые ие ую юю ой ей ий ый ом ем ам ям ах ях
ов ев ия ья ие ье ию ью ти ть ешь ет ют ут ит ят ем им ете ите ал ала али ало ил ила или ило
ость ости ение ения ению ением ении ание ания анию анием ании а я о е у ю ы и ь
""".split(), key=len, reverse=True)

_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*|[а-яА-ЯёЁ]+|\d[\d\s  ]*(?:[.,]\d+)?|[₸%]")
_CAMEL = re.compile(r"[a-z][A-Z]|_")

# Связи типов требований, при которых совпадение даже одного термина значимо.
KIND_LINKS = {
    frozenset({ReqKind.acceptance, ReqKind.functional}),
    frozenset({ReqKind.acceptance, ReqKind.api}),
    frozenset({ReqKind.data, ReqKind.functional}),
    frozenset({ReqKind.data, ReqKind.api}),
    frozenset({ReqKind.integration, ReqKind.api}),
    frozenset({ReqKind.integration, ReqKind.functional}),
    frozenset({ReqKind.business, ReqKind.nonfunctional}),
    frozenset({ReqKind.functional, ReqKind.api}),
    frozenset({ReqKind.nonfunctional, ReqKind.functional}),
    frozenset({ReqKind.ui, ReqKind.functional}),
    frozenset({ReqKind.ui, ReqKind.api}),
}
SKIP_KINDS = {ReqKind.meta}


def stem(w: str) -> str:
    w = w.lower().replace("ё", "е")
    if not re.match(r"[а-я]", w):
        return w
    if len(w) <= 4:
        return w
    for s in _SUFFIXES:
        if w.endswith(s) and len(w) - len(s) >= 4:
            w = w[: -len(s)]
            break
    return w[:7]


@dataclass
class Term:
    key: str          # ключ: основа, «#число», «$единица», «@идентификатор»
    surface: str      # как встретилось впервые
    kind: str         # word | num | unit | ident


def terms_of(text: str) -> dict[str, Term]:
    out: dict[str, Term] = {}
    for m in _TOKEN.finditer(text or ""):
        tok = m.group(0).strip()
        if not tok:
            continue
        low = tok.lower().replace("ё", "е")
        if tok[0].isdigit():
            digits = re.sub(r"[\s  ]", "", tok).replace(",", ".")
            if len(digits.replace(".", "")) < 2:      # одиночные цифры — нумерация пунктов
                continue
            key = f"#{digits}"
            out.setdefault(key, Term(key, tok, "num"))
        elif low in UNITS or tok in {"₸", "%"}:
            u = {"тг": "₸", "тенге": "₸", "kzt": "₸"}.get(low, low)
            key = f"${stem(u) if u not in {'₸', '%'} else u}"
            out.setdefault(key, Term(key, tok, "unit"))
        elif re.match(r"[a-zA-Z]", tok):
            if low in STOP or len(low) < 3:
                continue
            kind = "ident" if (_CAMEL.search(tok) or len(tok) >= 4) else "word"
            out.setdefault(f"@{low}", Term(f"@{low}", tok, kind))
        else:
            if low in STOP or len(low) < 3:
                continue
            key = stem(low)
            if key in STOP:
                continue
            out.setdefault(key, Term(key, tok, "word"))
    return out


@dataclass
class Group:
    rids: list[str]
    shared: list[str] = field(default_factory=list)   # поверхностные формы общих терминов
    weight: float = 0.0
    note: str = ""                                    # подсказка о виде конфликта (пары «ограничение ↔ условие»)

    def line(self, i: int) -> str:
        terms = ", ".join(self.shared[:6]) or "связанные типы требований"
        if self.note:
            return f"G-{i} ({self.note}; общие термины: {terms}): {', '.join(self.rids)}"
        return f"G-{i} (общие термины: {terms}): {', '.join(self.rids)}"


# Ограничение или квантор всеобщности в одном правиле и условие/особый случай в другом —
# самый частый скрытый конфликт набора («не более N за период» ↔ «если …, снова выполняется»).
CAP_RE = re.compile(r"(?<![а-яё])(максимум\w*|не более|не больше|не чаще|не превыша\w*|только|всегда|никогда|"
                    r"независимо|один раз|один такой|одн[аоу] так\w*|единственн\w*|исключительно|ровно|"
                    r"не меня\w*|не изменя\w*)(?![а-яё])", re.I)
COND_RE = re.compile(r"(?<![а-яё])(если|при|кроме|за исключением|повторн\w*|снова|следующ\w*|иначе|"
                     r"в случае|после|изменени\w*|возврат\w*|отмен\w*)(?![а-яё])", re.I)
RULE_KINDS = {ReqKind.functional, ReqKind.api, ReqKind.nonfunctional, ReqKind.data, ReqKind.integration}


class TermIndex:
    def __init__(self, reqs: Sequence[Requirement], *, max_df: float = 0.4):
        self.reqs = [r for r in reqs if r.kind not in SKIP_KINDS]
        self.by_rid = {r.rid: r for r in self.reqs}
        self.terms: dict[str, dict[str, Term]] = {r.rid: terms_of(r.quote) for r in self.reqs}
        df: dict[str, int] = defaultdict(int)
        for t in self.terms.values():
            for k in t:
                df[k] += 1
        n = max(1, len(self.reqs))
        limit = max(3, int(max_df * n))
        self.df = dict(df)
        self.idf = {k: math.log(1 + n / v) for k, v in df.items()}
        self.common = {k for k, v in df.items() if v > limit and not k.startswith(("#", "@"))}
        # единицы и сами по себе слишком часты, но в паре с числом информативны
        self.post: dict[str, set[str]] = defaultdict(set)
        for rid, t in self.terms.items():
            for k in t:
                if k not in self.common and df[k] >= 2:
                    self.post[k].add(rid)

    def informative(self, rid: str) -> dict[str, Term]:
        return {k: v for k, v in self.terms[rid].items() if k not in self.common}

    def pair_score(self, a: str, b: str) -> tuple[float, list[str]]:
        ta, tb = self.informative(a), self.informative(b)
        shared = [k for k in ta if k in tb]
        if not shared:
            return 0.0, []
        nums = [k for k in shared if k.startswith("#")]
        units = [k for k in shared if k.startswith("$")]
        idents = [k for k in shared if k.startswith("@") and ta[k].kind == "ident"]
        words = [k for k in shared if not k.startswith(("#", "$"))]
        ra, rb = self.by_rid[a], self.by_rid[b]
        linked = frozenset({ra.kind, rb.kind}) in KIND_LINKS
        cross = (ra.location.split(" › ")[0] != rb.location.split(" › ")[0]) if ra.location and rb.location else False
        ok = (len(words) + len(idents) >= 2) or bool(nums) or bool(idents) or \
             (bool(units) and len(words) >= 1) or (linked and len(shared) >= 1 and (words or units))
        if not ok:
            return 0.0, []
        w = sum(self.idf.get(k, 1.0) for k in shared)
        w += 1.5 * len(nums) + 1.0 * len(idents) + 0.5 * len(units)
        if linked:
            w *= 1.3
        if ra.kind != rb.kind:
            w *= 1.15
        if cross:
            w *= 1.2
        order = sorted(shared, key=lambda k: (-(k.startswith("#") or k.startswith("@")), -self.idf.get(k, 1)))
        return w, order

    def related(self, rid: str, k: int = 3, *, exclude: set | None = None,
                min_weight: float = 2.0) -> list[str]:
        """k требований, сильнее всего связанных с rid общими терминами."""
        if rid not in self.by_rid:
            return []
        ex = exclude or set()
        scored = []
        for other in self.by_rid:
            if other == rid or other in ex:
                continue
            w, _ = self.pair_score(rid, other)
            if w >= min_weight:
                scored.append((-w, other))
        scored.sort()
        return [o for _, o in scored[:k]]

    def groups(self, *, max_groups: int = 12, max_size: int = 6, min_weight: float = 2.0) -> list[Group]:
        rids = [r.rid for r in self.reqs]
        edges: dict[str, list[tuple[float, str, list[str]]]] = defaultdict(list)
        for i, a in enumerate(rids):
            for b in rids[i + 1:]:
                w, shared = self.pair_score(a, b)
                if w >= min_weight:
                    edges[a].append((w, b, shared))
                    edges[b].append((w, a, shared))
        cands: list[Group] = []
        for seed in rids:
            nb = sorted(edges.get(seed, []), key=lambda t: (-t[0], t[1]))[: max_size - 1]
            if not nb:
                continue
            members = [seed] + [b for _, b, _ in nb]
            shared_c: dict[str, float] = defaultdict(float)
            surface: dict[str, str] = {}
            for w, b, sh in nb:
                for k in sh[:4]:
                    shared_c[k] += w
                    surface.setdefault(k, (self.terms[seed].get(k) or self.terms[b][k]).surface)
            weight = sum(w for w, _, _ in nb)
            order = {r: i for i, r in enumerate(rids)}
            members.sort(key=lambda r: order[r])
            cands.append(Group(members, [surface[k] for k, _ in sorted(shared_c.items(), key=lambda kv: -kv[1])],
                               round(weight, 2)))
        cands.sort(key=lambda g: (-g.weight, g.rids))
        chosen: list[Group] = []
        covered_pairs: set[frozenset] = set()
        for g in cands:
            pairs = {frozenset({a, b}) for i, a in enumerate(g.rids) for b in g.rids[i + 1:]}
            if any(set(g.rids) <= set(c.rids) for c in chosen):
                continue
            new = pairs - covered_pairs
            if chosen and len(new) < max(1, len(pairs) // 3):
                continue
            chosen.append(g)
            covered_pairs |= pairs
            if len(chosen) >= max_groups:
                break
        return chosen


def quantifier_pairs(index: TermIndex, *, max_pairs: int = 4, min_weight: float = 1.5) -> list[Group]:
    """Пары правил «ограничение/квантор ↔ условие/особый случай» с общими содержательными словами."""
    rules = [r for r in index.reqs if r.kind in RULE_KINDS]
    out: list[Group] = []
    for a in rules:
        ma = CAP_RE.search(a.quote or "")
        if not ma:
            continue
        for b in rules:
            if b.rid == a.rid:
                continue
            mb = COND_RE.search(b.quote or "")
            if not mb:
                continue
            w, shared = index.pair_score(a.rid, b.rid)
            words = [k for k in shared if not k.startswith(("#", "$"))]
            if w < min_weight or len(words) < 2:
                continue
            surf = [(index.terms[a.rid].get(k) or index.terms[b.rid][k]).surface for k in shared[:5]]
            out.append(Group(sorted([a.rid, b.rid], key=lambda x: int(re.sub(r"\D", "", x) or 0)), surf, round(w, 2),
                             note=f"ограничение «{ma.group(0)}» в {a.rid} против условия «{mb.group(0)}» в {b.rid} — "
                                  f"проверь вопрос (b)"))
    out.sort(key=lambda g: (-g.weight, g.rids))
    seen: set[frozenset] = set()
    chosen = []
    for g in out:
        k = frozenset(g.rids)
        if k in seen:
            continue
        seen.add(k)
        chosen.append(g)
        if len(chosen) >= max_pairs:
            break
    return chosen


def candidate_groups(reqs: Sequence[Requirement], max_groups: int = 12) -> list[Group]:
    index = TermIndex(reqs)
    pairs = quantifier_pairs(index)
    return pairs + index.groups(max_groups=max_groups)


def render_groups(groups: Sequence[Group]) -> str:
    return "\n".join(g.line(i) for i, g in enumerate(groups, 1))
