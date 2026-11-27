"""Anthropic Claude (пакет `anthropic` >= 1).

- Модель по умолчанию — `claude-opus-5-5` (ANTHROPIC_MODEL переопределяет).
- Ответ строго по JSON-схеме: структурированный вывод (`output_format=<pydantic-класс>`); SDK сам
  приводит схему к поддерживаемому виду и проверяет ответ.
- Размышления у моделей Claude 5 всегда адаптивные; глубину задаёт `LLM_EFFORT`
  (low | medium | high | xhigh | max, по умолчанию medium — как у самой модели). Effort входит в ключ кэша.
- temperature не передаётся: у текущих моделей Claude параметры сэмплирования отключены
  (воспроизводимость даёт дисковый кэш).
- Стриминг (`get_final_message`) — длинные ответы с размышлениями не упираются в HTTP-таймаут.
- Отказ модели по политике безопасности: серверный запасной маршрут (`fallbacks="default"`).
"""
from __future__ import annotations

import os
from typing import Any

from reqcheck.llm import LLMError
from .base import BaseLLM, QuotaExhausted, RetryableError

DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 64000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM(BaseLLM):
    def __init__(self, settings: Any) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("Для провайдера anthropic установите пакет: pip install anthropic") from e
        if not settings.api_key:
            raise LLMError("Не задан ANTHROPIC_API_KEY (добавьте его в .env).")
        settings.model = settings.model or DEFAULT_MODEL
        super().__init__(settings)
        effort = (os.getenv("LLM_EFFORT") or "medium").strip().lower()
        if effort not in EFFORTS:
            raise LLMError(f"LLM_EFFORT={effort!r}: допустимо {', '.join(EFFORTS)}")
        self.thinking = effort            # входит в ключ кэша (см. BaseLLM._generate_json)
        self.model = settings.model
        self._sdk = anthropic
        timeout = float(os.getenv("LLM_REQUEST_TIMEOUT") or 900)
        self._client = anthropic.Anthropic(api_key=settings.api_key, timeout=timeout, max_retries=0)

    def _classify(self, exc):
        if isinstance(exc, LLMError):
            return exc
        a = self._sdk
        if isinstance(exc, a.APITimeoutError):
            return RetryableError(f"таймаут запроса к {self.model}", timeout=True)
        if isinstance(exc, a.RateLimitError):
            retry = exc.response.headers.get("retry-after") if getattr(exc, "response", None) else None
            return RetryableError(f"429 {self._safe(exc)[:200]}",
                                  retry_after=float(retry) if retry and retry.replace(".", "").isdigit() else None)
        if isinstance(exc, (a.APIConnectionError, a.InternalServerError)):
            return RetryableError(f"{type(exc).__name__}: {self._safe(exc)[:200]}")
        if isinstance(exc, a.APIStatusError) and exc.status_code in (529, 503, 502, 500):
            return RetryableError(f"{exc.status_code}: {self._safe(exc)[:200]}")
        if isinstance(exc, a.PermissionDeniedError) and "credit" in self._safe(exc).lower():
            return QuotaExhausted(self.model, "закончился баланс API")
        if isinstance(exc, a.BadRequestError) and "credit balance" in self._safe(exc).lower():
            return QuotaExhausted(self.model, "закончился баланс API")
        return exc

    def _complete_json(self, system, user, json_schema, schema, temperature):
        with self._client.beta.messages.stream(
            model=self.model, max_tokens=MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.thinking},
            output_format=schema,
            betas=[FALLBACK_BETA], fallbacks="default",
        ) as stream:
            r = stream.get_final_message()
        if r.stop_reason == "refusal":
            cat = getattr(getattr(r, "stop_details", None), "category", None)
            raise LLMError(f"{self.model} отказался отвечать (категория: {cat}).")
        if r.stop_reason == "max_tokens":
            raise RetryableError(f"{self.model}: ответ обрезан на max_tokens={MAX_TOKENS}")
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        u = r.usage
        return text, int((u.input_tokens or 0) + (u.output_tokens or 0))
