"""Стадия 8. Рендер отчёта. Владелец: A6."""
from __future__ import annotations

from reqcheck.models import ReportData


def render_markdown(data: ReportData) -> str:
    """ЗАГЛУШКА."""
    return f"# Отчёт о проверке требований\n\nДокумент: {data.passport.doc_name}\n\n```json\n{data.model_dump_json(indent=2)}\n```\n"


def render_html(data: ReportData) -> str:
    """ЗАГЛУШКА."""
    return "<pre>" + render_markdown(data) + "</pre>"
