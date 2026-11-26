"""Провайдеры LLM. Владелец: A2."""
from __future__ import annotations


def make_client(settings):
    p = (settings.provider or "").lower()
    if p == "gemini":
        from .gemini import GeminiLLM
        return GeminiLLM(settings)
    if p in ("openai", "openai_compat", "openai-compatible"):
        from .openai_compat import OpenAICompatLLM
        return OpenAICompatLLM(settings)
    if p == "anthropic":
        from .anthropic import AnthropicLLM
        return AnthropicLLM(settings)
    if p == "ollama":
        from .ollama import OllamaLLM
        return OllamaLLM(settings)
    if p == "fake":
        from reqcheck.llm import FakeLLM
        return FakeLLM()
    from reqcheck.llm import LLMError
    raise LLMError(f"Неизвестный провайдер: {settings.provider!r} (gemini|openai|anthropic|ollama|fake)")
