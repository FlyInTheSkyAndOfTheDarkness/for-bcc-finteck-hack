"""Anthropic (необязательный пакет `anthropic`)."""
from __future__ import annotations

import json
from typing import Any

from reqcheck.llm import LLMError
from .base import BaseLLM, RetryableError


class AnthropicLLM(BaseLLM):
    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("Для провайдера anthropic установите пакет: pip install anthropic") from e
        if not settings.api_key:
            raise LLMError("Не задан ANTHROPIC_API_KEY.")
        if not settings.model:
            raise LLMError("Укажите ANTHROPIC_MODEL.")
        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=settings.api_key, timeout=300, max_retries=0)

    def _classify(self, exc):
        if isinstance(exc, LLMError):
            return exc
        a = self._sdk
        if isinstance(exc, (a.RateLimitError, a.APIConnectionError, a.InternalServerError)):
            return RetryableError(f"{type(exc).__name__}: {self._safe(exc)[:200]}",
                                  timeout=isinstance(exc, a.APITimeoutError))
        return exc

    def _complete_json(self, system, user, json_schema, schema, temperature):
        sys_msg = system + "\n\nВерни ТОЛЬКО JSON по схеме:\n" + json.dumps(json_schema, ensure_ascii=False)
        r = self._client.messages.create(
            model=self.model, max_tokens=8192, temperature=temperature, system=sys_msg,
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return text, int(r.usage.input_tokens + r.usage.output_tokens)
