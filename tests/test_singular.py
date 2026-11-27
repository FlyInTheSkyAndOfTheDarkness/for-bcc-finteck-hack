from reqcheck.linters import singular
from reqcheck.linters.common import Ctx
from reqcheck.models import Block, BlockKind, Document, ReqKind, Requirement


def _doc(lines):
    blocks = [Block(id=f"B-{i:04d}", kind=BlockKind.list_item, text=t, section_path=["Функциональные требования"],
                    line_start=i, line_end=i) for i, t in enumerate(lines, 1)]
    doc = Document(name="t", path="t.md", sha256="0", format="md", text="\n".join(lines), blocks=blocks)
    reqs = [Requirement(rid=f"R-{i:02d}", doc_id=t.split(".")[0], kind=ReqKind.functional, summary=t[:30],
                        quote=t, block_ids=[b.id], location=b.locator())
            for i, (t, b) in enumerate(zip(lines, blocks), 1)]
    return doc, reqs


def test_compound_requirements_aggregated_into_one_finding():
    doc, reqs = _doc(["FR-1. Клиент задаёт лимит перевода. Лимит действует до конца суток.",
                      "FR-2. Система отклоняет перевод сверх лимита.",
                      "FR-3. Комиссия списывается со счёта списания. Возврат комиссии не производится. Чек отправляется на e-mail."])
    out = singular.run(Ctx(doc, reqs))
    assert len(out) == 1
    f = out[0]
    assert f.criterion.value == "singular" and set(f.rids) == {"R-01", "R-03"}
    assert "FR-3 — 3 утв." in f.explanation


def test_single_statement_and_short_fragments_not_flagged():
    doc, reqs = _doc(["FR-1. Система отклоняет перевод сверх лимита, установленного клиентом.",
                      "FR-2. Сумма в тенге. Да."])
    assert singular.run(Ctx(doc, reqs)) == []
