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


def get_client(settings: "Any") -> LLMClient:  # noqa: F821
    """Фабрика клиента по настройкам. Реализацию даёт reqcheck/providers/__init__.py."""
    if settings.provider == "fake":
        return FakeLLM()
    from reqcheck.providers import make_client
    return make_client(settings)
