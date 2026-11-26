"""Регрессия: документы, не похожие на выданный (без частей, ID, стилей заголовков; cp1251; HTML из Word)."""
from __future__ import annotations

from reqcheck import linters
from reqcheck.ingest import load_document
from reqcheck.models import BlockKind, ReqKind
from reqcheck.segment import segment


def _run(path):
    doc = load_document(path)
    reqs = segment(doc, None)
    return doc, reqs, linters.run_linters(doc, reqs)


def test_txt_cp1251_and_line_paragraphs(tmp_path):
    p = tmp_path / "spec.txt"
    p.write_bytes("Модуль уведомлений\n\nСистема должна отправлять SMS.\nСервис должен хранить журнал.\n"
                  .encode("cp1251"))
    doc, reqs, _ = _run(p)
    assert [r.quote for r in reqs] == ["Система должна отправлять SMS.", "Сервис должен хранить журнал."]


def test_masculine_modal_and_numbered_headings(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("# Док\n\n## 1. Общие сведения\n\nОписание.\n\n## 2. Цели проекта\n\nСократить время выдачи.\n\n"
                 "## 5. Прочее\n\nШлюз должен принимать платежи.\n", encoding="utf-8")
    _, reqs, _ = _run(p)
    kinds = {r.quote: r.kind for r in reqs}
    assert kinds["Сократить время выдачи."] == ReqKind.business
    assert kinds["Описание."] == ReqKind.meta
    assert "Шлюз должен принимать платежи." in kinds


def test_inline_ids_split(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("# Док\n\n## Требования\n\nFR-1. Система должна A. FR-2. Система должна B. См. п. 3.\n",
                 encoding="utf-8")
    _, reqs, _ = _run(p)
    assert [(r.doc_id, r.quote) for r in reqs] == [("FR-1", "FR-1. Система должна A."),
                                                    ("FR-2", "FR-2. Система должна B. См. п. 3.")]


def test_stub_phrases_no_false_positives(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("# Док\n\n## Функциональные требования\n\n1. Пользователь заполняет анкету.\n"
                 "2. Если поле не заполнено, система возвращает ошибку.\n3. Клиент может уточнить сумму.\n\n"
                 "## Тестирование\n\nзаполняются тестировщиками\n\n## Вопросы\n\nTBD\n", encoding="utf-8")
    _, _, found = _run(p)
    stubs = [f.quote for f in found if "заглушка" in f.title]
    assert sorted(stubs) == ["TBD", "заполняются тестировщиками"]


def test_html_block_tags_inside_cells(tmp_path):
    p = tmp_path / "spec.html"
    p.write_text("<h1>Спец</h1><h2>Функциональные требования</h2><table>"
                 "<tr><th><p>ID</p></th><th><p>Требование</p></th></tr>"
                 "<tr><td><p>FR-1</p></td><td><div>Система должна блокировать карту.</div></td></tr>"
                 "<tr><td>FR-2<td>Система должна разблокировать карту.</tr></table>", encoding="utf-8")
    doc, reqs, _ = _run(p)
    rows = [b for b in doc.blocks if b.kind == BlockKind.table_row]
    assert [b.cells for b in rows] == [["FR-1", "Система должна блокировать карту."],
                                       ["FR-2", "Система должна разблокировать карту."]]
    assert [r.doc_id for r in reqs] == ["FR-1", "FR-2"]


def test_docx_without_heading_styles(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph().add_run("Требования к шлюзу").bold = True
    d.add_paragraph("1. Функциональные требования")
    d.add_paragraph("Шлюз должен принимать платежи.")
    p = tmp_path / "spec.docx"
    d.save(p)
    doc, reqs, _ = _run(p)
    assert doc.name == "Требования к шлюзу"
    assert [r.quote for r in reqs] == ["Шлюз должен принимать платежи."]
    assert doc.blocks[-1].section_path == ["1. Функциональные требования"]


def test_id_gap_not_reported_for_unrelated_codes(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("# Док\n\n## Функциональные требования\n\n- КП-1. Система должна A.\n"
                 "- КП-2024. Система должна B.\n", encoding="utf-8")
    _, _, found = _run(p)
    assert not [f for f in found if "Пропуск в нумерации" in f.title]
