"""Интерфейс к LLM. Стадии конвейера знают только `LLMClient.generate_json`.

Контракт (менять только через оркестратора):
    client.generate_json(system, user, schema, tag=...) -> экземпляр `schema` (pydantic BaseModel)

Реализация провайдеров — в reqcheck/providers/*. Базовый класс берёт на себя
кэш, ограничение запросов в минуту, повторы и журнал вызовов, так что провайдер
реализует только `_complete_json`.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMStats:
    calls: int = 0           # реальные обращения к API
    cache_hits: int = 0
    tokens_total: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class LLMClient(abc.ABC):
    provider: str = "base"
    model: str = ""

    def __init__(self) -> None:
        self.stats = LLMStats()

    @abc.abstractmethod
    def generate_json(self, system: str, user: str, schema: type[T], *, tag: str,
                      temperature: Optional[float] = None) -> T:
        """Вернуть объект `schema`, сгенерированный моделью.

        system — роль и правила; user — данные задачи; tag — имя стадии для журнала
        и статистики ("segment", "review", "set.contradictions", "critic", "clarify"…).
        Обязан бросить LLMError, если после повторов ответ не удалось разобрать.
        """


class FakeLLM(LLMClient):
    """Детерминированная заглушка для тестов и работы без ключа.

    handlers: {tag_prefix: callable(system, user, schema) -> dict | BaseModel}
    Без подходящего обработчика возвращает «пустой» объект схемы (все списки пустые).
    """
    provider = "fake"
    model = "fake-llm"

    def __init__(self, handlers: Optional[dict[str, Callable[[str, str, type], Any]]] = None):
        super().__init__()
        self.handlers = handlers or {}
        self.log: list[dict] = []

    def generate_json(self, system, user, schema, *, tag, temperature=None):
        self.stats.calls += 1
        self.stats.by_tag[tag] = self.stats.by_tag.get(tag, 0) + 1
        self.log.append({"tag": tag, "system": system, "user": user, "schema": schema.__name__})
        for prefix, fn in sorted(self.handlers.items(), key=lambda kv: -len(kv[0])):
            if tag.startswith(prefix):
                out = fn(system, user, schema)
                return out if isinstance(out, schema) else schema.model_validate(out)
        return _empty(schema)


def _empty(schema: type[T]) -> T:
    data: dict[str, Any] = {}
    for name, f in schema.model_fields.items():
        if f.is_required():
            ann = str(f.annotation)
            if "list" in ann:
                data[name] = []
            elif "bool" in ann:
                data[name] = False
            elif "int" in ann or "float" in ann:
                data[name] = 0
            elif "dict" in ann:
                data[name] = {}
            else:
                data[name] = ""
    return schema.model_validate(data)


DEFAULT_STRONG_TAGS = ("set.", "critic", "clarify", "verdict", "answers")


class RoutedLLM(LLMClient):
    """Маршрутизация по стадиям: массовые проверки — быстрой модели, рассуждения — сильной.

    Стадия определяется по префиксу tag. Обе модели работают через один и тот же контракт.
    """

    def __init__(self, fast: LLMClient, strong: LLMClient, strong_tags=DEFAULT_STRONG_TAGS):
        super().__init__()
        self.fast, self.strong, self.strong_tags = fast, strong, tuple(strong_tags)
        self.provider = fast.provider

    @property
    def model(self) -> str:  # type: ignore[override]
        if self.fast.model == self.strong.model:
            return self.fast.model
        return f"{self.fast.model} (массовые проверки) + {self.strong.model} ({', '.join(self.strong_tags)})"

    def _pick(self, tag: str) -> LLMClient:
        return self.strong if tag.startswith(self.strong_tags) else self.fast

    def generate_json(self, system, user, schema, *, tag, temperature=None):
        client = self._pick(tag)
        try:
            return client.generate_json(system, user, schema, tag=tag, temperature=temperature)
        finally:
            self._sync()

    def _sync(self) -> None:
        a, b = self.fast.stats, self.strong.stats
        self.stats.calls = a.calls + b.calls
        self.stats.cache_hits = a.cache_hits + b.cache_hits
        self.stats.tokens_total = a.tokens_total + b.tokens_total
        self.stats.by_tag = {k: a.by_tag.get(k, 0) + b.by_tag.get(k, 0) for k in {*a.by_tag, *b.by_tag}}


def get_client(settings: "Any") -> LLMClient:  # noqa: F821
    """Фабрика клиента по настройкам. Реализацию даёт reqcheck/providers/__init__.py.

    LLM_STRONG_MODEL (env) включает маршрутизацию: стадии из LLM_STRONG_TAGS идут в сильную модель.
    """
    import copy
    import os

    if settings.provider == "fake":
        return FakeLLM()
    from reqcheck.providers import make_client
    fast = make_client(settings)
    strong_model = os.getenv("LLM_STRONG_MODEL", "").strip()
    if not strong_model or strong_model == getattr(fast, "model", None):
        return fast
    s2 = copy.copy(settings)
    s2.model = strong_model
    tags = [t.strip() for t in os.getenv("LLM_STRONG_TAGS", "").split(",") if t.strip()] or DEFAULT_STRONG_TAGS
    return RoutedLLM(fast, make_client(s2), tags)
