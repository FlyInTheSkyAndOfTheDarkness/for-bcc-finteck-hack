"""Параллельный запуск независимых LLM-вызовов с сохранением порядка и изоляцией ошибок."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)


def workers(default: int = 3) -> int:
    try:
        return max(1, int(os.getenv("REQCHECK_WORKERS", default)))
    except ValueError:
        return default


def run_all(fns: Sequence[Callable[[], Any]], *, max_workers: int | None = None,
            label: str = "task", default: Any = None) -> list[Any]:
    """Выполнить функции без аргументов параллельно; результат — в исходном порядке.

    Упавшая функция не роняет остальные: в её позиции будет `default` (а ошибка — в логе).
    """
    if not fns:
        return []

    def safe(i: int, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.warning("%s #%d упал: %s: %s", label, i, type(e).__name__, e)
            return default

    n = min(len(fns), max_workers or workers())
    if n <= 1:
        return [safe(i, f) for i, f in enumerate(fns)]
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"rc-{label}") as ex:
        futs = [ex.submit(safe, i, f) for i, f in enumerate(fns)]
        return [f.result() for f in futs]
