"""Ollama: /api/chat с format=<JSON-схема>."""
from __future__ import annotations

from typing import Any

from reqcheck.llm import LLMError
from ._http import post_json
from .base import BaseLLM


class OllamaLLM(BaseLLM):
    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        if not settings.model:
            raise LLMError("Укажите OLLAMA_MODEL (например qwen2.5:14b).")
        self.base = (settings.base_url or "http://localhost:11434").rstrip("/")

    def _complete_json(self, system, user, json_schema, schema, temperature):
        payload = {"model": self.model, "stream": False, "format": json_schema,
                   "options": {"temperature": temperature},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        d = post_json(f"{self.base}/api/chat", payload, {}, timeout=600)
        return d.get("message", {}).get("content", ""), int(d.get("prompt_eval_count", 0) + d.get("eval_count", 0))
