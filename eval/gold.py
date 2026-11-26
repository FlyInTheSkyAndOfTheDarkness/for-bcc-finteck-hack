"""Загрузка и проверка gold-разметки (eval/gold.yaml, eval/mutants/*.gold.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from reqcheck.models import Criterion, Severity

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_GOLD = EVAL_DIR / "gold.yaml"

# Семейства критериев: находка с критерием из того же семейства считается совместимой.
FAMILIES: dict[str, set[str]] = {
    "ambiguity": {"unambiguous", "verifiable", "set_comprehensible", "set_validatable"},
    "consistency": {"set_consistent", "correct"},
    "completeness": {"complete", "set_complete"},
    "conformance": {"conforming", "traceable", "modifiable", "ranked"},
    "scope": {"appropriate", "necessary", "singular", "feasible", "set_feasible", "set_bounded"},
}


def family(criterion: str) -> str:
    for name, members in FAMILIES.items():
        if criterion in members:
            return name
    return "other"


class GoldItem(BaseModel):
    id: str
    title: str
    section: str = ""
    quotes: list[str]
    criterion: Criterion
    alt_criteria: list[Criterion] = Field(default_factory=list)
    severity: Severity
    must_find: bool = False
    description: str = ""
    mutation: Optional[str] = None   # для мутантов: тип внесённого дефекта

    @field_validator("quotes")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v or not all(q.strip() for q in v):
            raise ValueError("у gold-элемента должна быть хотя бы одна непустая цитата")
        return v

    def criterion_score(self, crit: str) -> float:
        """100 — тот же критерий, 80 — допустимый альтернативный, 60 — то же семейство, 0 — нет."""
        if crit == self.criterion.value:
            return 100.0
        if crit in {c.value for c in self.alt_criteria}:
            return 80.0
        fam = family(crit)
        if fam != "other" and (fam == family(self.criterion.value)
                               or fam in {family(c.value) for c in self.alt_criteria}):
            return 60.0
        return 0.0


def load_gold(path: str | Path = DEFAULT_GOLD) -> list[GoldItem]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    items = [GoldItem.model_validate(x) for x in raw]
    ids = [g.id for g in items]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"повторяющиеся id в gold: {sorted(dup)}")
    return items


def dump_gold(items: list[GoldItem], path: str | Path) -> None:
    data = [g.model_dump(mode="json", exclude_none=True) for g in items]
    Path(path).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
                          encoding="utf-8", newline="\n")
