"""OpenAI и совместимые API (base_url). Если пакет openai не установлен — чистый HTTP."""
from __future__ import annotations

import json
from typing import Any

from reqcheck.llm import LLMError
from ._http import post_json
from .base import BaseLLM, RetryableError


class OpenAICompatLLM(BaseLLM):
    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        if not settings.model:
            self.model = "gpt-4o-mini"
        if not settings.api_key and not settings.base_url:
            raise LLMError("Не задан OPENAI_API_KEY (или OPENAI_BASE_URL для локального сервера).")
        self.base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")

    def _complete_json(self, system, user, json_schema, schema, temperature):
        sys_msg = system + "\n\nВерни ТОЛЬКО JSON по схеме:\n" + json.dumps(json_schema, ensure_ascii=False)
        payload = {
            "model": self.model, "temperature": temperature,
            "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        d = post_json(f"{self.base}/chat/completions", payload,
                      {"Authorization": f"Bearer {self.settings.api_key}"})
        try:
            return d["choices"][0]["message"]["content"] or "", int(d.get("usage", {}).get("total_tokens", 0))
        except (KeyError, IndexError, TypeError) as e:
            raise RetryableError(f"неожиданный ответ API: {str(d)[:200]}") from e
