"""Gemini через google-genai.

Структурированный вывод: response_mime_type=application/json + response_schema=<pydantic-класс>
(проверено на схемах reqcheck.schemas). Если SDK/модель отвергает схему (400 про schema),
переходим на JSON-режим со схемой в тексте промпта.

Выбор модели при пустом settings.model: все `gemini-X.Y-flash` (без lite/preview/image/tts/live…)
по убыванию версии, затем `gemini-X.Y-flash-lite` по убыванию; каждая проверяется крошечным
запросом (таймаут LLM_PROBE_TIMEOUT, по умолчанию 30 с); 404/403/квота/503/таймаут -> следующая.
Во время работы то же правило: суточная квота, 404, таймаут (если есть следующая модель) или
два подряд 503 -> переход на следующую модель цепочки. Модели с 404/403/суточной квотой исключаются
до конца прогона; цепочка обходится по кругу, но за один вызов каждая модель пробуется один раз.
После временного ухода с основной модели (503/таймаут) клиент через LLM_RECOVER_S секунд
(по умолчанию 60) снова пробует основную: результаты прогона не «расползаются» по запасным моделям.

Размышления: переменная LLM_THINKING = off|minimal|low|medium|high (по умолчанию low).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Any, Optional

from reqcheck.llm import LLMError
from reqcheck.providers.base import BaseLLM, ModelUnavailable, QuotaExhausted, RetryableError

_STABLE = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-flash(-lite)?$")
_LEVELS = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


def order_models(names: list[str]) -> list[str]:
    """Предпочтительный порядок: flash по убыванию версии, затем flash-lite по убыванию."""
    flash, lite = [], []
    for n in names:
        n = n.split("/")[-1]
        m = _STABLE.match(n)
        if not m:
            continue
        ver = (int(m.group(1)), int(m.group(2) or 0))
        (lite if m.group(3) else flash).append((ver, n))
    key = lambda t: t[0]  # noqa: E731
    out = [n for _, n in sorted(flash, key=key, reverse=True)]
    out += [n for _, n in sorted(lite, key=key, reverse=True)]
    return out


def parse_retry_delay(text: str, details: Any = None) -> Optional[float]:
    """Достаёт задержку из RetryInfo (retryDelay: '34s') или из текста 'retry in 34.5s'."""
    try:
        for d in (details or {}).get("error", {}).get("details", []):
            rd = d.get("retryDelay")
            if rd:
                return float(str(rd).rstrip("s"))
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text) or \
        re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I)
    return float(m.group(1)) if m else None


class GeminiLLM(BaseLLM):
    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        if not settings.api_key:
            raise LLMError("Не задан GEMINI_API_KEY (см. .env.example).")
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:  # pragma: no cover
            raise LLMError("Не установлен пакет google-genai: pip install google-genai") from e
        self._genai, self._types = genai, types
        self._client = genai.Client(api_key=settings.api_key)
        self._lock = threading.RLock()
        self._chain: list[str] = []
        self._ready = False
        self._unavail: dict[str, int] = {}
        self._dead: set[str] = set()           # 404/403/суточная квота — до конца прогона
        self._left_primary = 0.0               # когда ушли с основной модели из-за 503/таймаута
        self.recover_s = float(os.getenv("LLM_RECOVER_S") or 60)
        self._no_schema: set[str] = set()      # модели, где response_schema не принят
        self._no_thinking: set[str] = set()    # модели, где thinking_config не принят
        self.thinking = (os.getenv("LLM_THINKING") or "low").strip().lower()
        self.probe_timeout = float(os.getenv("LLM_PROBE_TIMEOUT") or 30)

    # ------------------------------------------------------------ модели ---
    def _timeout_s(self, model: str) -> float:
        return float(os.getenv("LLM_REQUEST_TIMEOUT") or (120 if "lite" in model else 300))

    def _list_models(self) -> list[str]:
        try:
            return order_models([m.name for m in self._client.models.list()])
        except Exception as e:  # noqa: BLE001
            raise self._classify(e)

    def _ensure_model(self) -> None:
        with self._lock:
            if self._ready:
                return
            listed = self._list_models()
            if self.settings.model:
                self._chain = [self.settings.model] + self._fallbacks(listed)
                self.model = self.settings.model
            else:
                if not listed:
                    raise LLMError("Ключу не доступна ни одна модель gemini-*-flash (проверьте GEMINI_API_KEY).")
                self._chain = listed
                self.model = self._probe_select()
            self._ready = True

    def _fallbacks(self, listed: list[str]) -> list[str]:
        """Запасные модели для явно заданной. Порядок — из env (GEMINI_FALLBACK_MODELS для быстрой,
        LLM_STRONG_FALLBACK_MODELS для сильной), иначе: Flash той же или более ранней версии
        (новые Flash на бесплатном тарифе часто уходят в таймаут), затем Lite."""
        own = self.settings.model
        env_name = getattr(self.settings, "fallback_env", None) or "GEMINI_FALLBACK_MODELS"
        raw = os.getenv(env_name, "").strip()
        if raw:
            want = [m.strip() for m in raw.split(",") if m.strip() and m.strip() != own]
            return [m for m in want if not listed or m in listed]
        m = _STABLE.match(own)
        if not m:
            return [x for x in listed if x != own]
        ver = (int(m.group(1)), int(m.group(2) or 0))
        older, lite = [], []
        for x in listed:
            mm = _STABLE.match(x)
            if not mm or x == own:
                continue
            v = (int(mm.group(1)), int(mm.group(2) or 0))
            if mm.group(3):
                lite.append(x)
            elif not m.group(3) and v <= ver:
                older.append(x)
        newer = [x for x in listed if x not in older and x not in lite and x != own]
        return older + lite + newer

    def _probe_select(self) -> str:
        skipped = []
        for name in self._chain:
            t0 = time.monotonic()
            try:
                self._generate(name, "Ответь одним словом.", "Скажи: ок", None, 0.0,
                               timeout=self.probe_timeout, max_tokens=64, probe=True)
                self._log(f"выбрана модель {name} (проверка {time.monotonic() - t0:.1f} с)")
                return name
            except Exception as e:  # noqa: BLE001
                err = self._classify(e)
                if isinstance(err, (ModelUnavailable, QuotaExhausted, RetryableError)):
                    skipped.append(f"{name}: {self._safe(err)[:80]}")
                    continue
                raise err
        raise LLMError("Ни одна модель Gemini не прошла проверку: " + "; ".join(skipped))

    def _next_model(self, current: str, tried: Optional[set] = None) -> Optional[str]:
        """Следующая живая модель по кругу после current, не пробованная в этом вызове."""
        with self._lock:
            chain = self._chain
            if not chain:
                return None
            i = chain.index(current) if current in chain else -1
            skip = self._dead | (tried or set()) | {current}
            for k in range(1, len(chain) + 1):
                cand = chain[(i + k) % len(chain)]
                if cand not in skip:
                    return cand
            return None

    def _switch(self, frm: str, to: str, why: str) -> None:
        with self._lock:
            if self.model == frm:
                if self._chain and frm == self._chain[0]:
                    self._left_primary = time.monotonic()
                self.model = to
                self._log(f"переключение модели {frm} -> {to} ({why})")

    def _maybe_recover(self) -> None:
        """Вернуться на основную модель, если ушли с неё из-за временной ошибки давно."""
        with self._lock:
            if not self._chain:
                return
            primary = self._chain[0]
            if (self.model != primary and primary not in self._dead and self._left_primary
                    and time.monotonic() - self._left_primary >= self.recover_s):
                self._log(f"возврат на основную модель {primary}")
                self.model = primary
                self._left_primary = 0.0

    @staticmethod
    def _log(msg: str) -> None:
        print(f"[reqcheck.gemini] {msg}", file=sys.stderr)

    # ------------------------------------------------------------ вызов ----
    def _thinking_cfg(self, model: str) -> Any:
        t = self._types
        if model in self._no_thinking or self.thinking in ("", "default"):
            return None
        try:
            if self.thinking == "off":
                return t.ThinkingConfig(thinking_budget=0)
            lvl = _LEVELS.get(self.thinking)
            return t.ThinkingConfig(thinking_level=lvl) if lvl else None
        except Exception:  # noqa: BLE001  старый google-genai без thinking_level — работаем без него
            self._no_thinking.add(model)
            return None

    def _generate(self, model: str, system: str, user: str, schema: Optional[type], temperature: float,
                  *, timeout: float, max_tokens: Optional[int] = None, probe: bool = False,
                  json_schema: Optional[dict] = None) -> tuple[str, int]:
        t = self._types
        use_schema = schema is not None and model not in self._no_schema
        contents = user
        if schema is not None and not use_schema:
            contents = (user + "\n\nВерни ТОЛЬКО JSON, соответствующий этой JSON-схеме:\n"
                        + json.dumps(json_schema or schema.model_json_schema(), ensure_ascii=False))
        for _ in range(3):  # до двух откатов (thinking, schema)
            kw: dict[str, Any] = dict(
                system_instruction=system, temperature=temperature,
                http_options=t.HttpOptions(timeout=int(timeout * 1000)),
            )
            if schema is not None:
                kw["response_mime_type"] = "application/json"
                if use_schema:
                    kw["response_schema"] = schema
            if max_tokens:
                kw["max_output_tokens"] = max_tokens
            tc = None if probe else self._thinking_cfg(model)
            if tc is not None:
                kw["thinking_config"] = tc
            try:
                r = self._client.models.generate_content(
                    model=model, contents=contents, config=t.GenerateContentConfig(**kw))
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if getattr(e, "code", None) == 400 or "invalid_argument" in msg:
                    if tc is not None and "thinking" in msg:
                        self._no_thinking.add(model)
                        continue
                    if use_schema and ("schema" in msg or "response_" in msg):
                        self._no_schema.add(model)
                        use_schema = False
                        contents = (user + "\n\nВерни ТОЛЬКО JSON, соответствующий этой JSON-схеме:\n"
                                    + json.dumps(json_schema or schema.model_json_schema(), ensure_ascii=False))
                        continue
                raise
            u = getattr(r, "usage_metadata", None)
            tokens = int(getattr(u, "total_token_count", 0) or 0) if u else 0
            text = r.text if r is not None else None
            if not text:
                if probe:
                    return "", tokens
                raise RetryableError("Пустой ответ модели (возможно, блокировка или обрыв).")
            return text, tokens
        raise LLMError(f"Не удалось выполнить запрос к {model}.")

    def _complete_json(self, system, user, json_schema, schema, temperature):
        self._ensure_model()
        self._maybe_recover()
        tried: set[str] = set()
        while True:
            model = self.model
            if model in self._dead:                    # другой поток уже исключил модель
                nxt = self._next_model(model, tried)
                if not nxt:
                    raise LLMError("Нет доступных моделей Gemini (все исключены: 404/квота).")
                self._switch(model, nxt, "модель исключена")
                continue
            try:
                out = self._generate(model, system, user, schema, temperature,
                                     timeout=self._timeout_s(model), json_schema=json_schema)
                self._unavail[model] = 0
                return out
            except Exception as e:  # noqa: BLE001
                err = self._classify(e)
                tried.add(model)
                if isinstance(err, (QuotaExhausted, ModelUnavailable)):
                    with self._lock:
                        self._dead.add(model)
                nxt = self._next_model(model, tried)
                if isinstance(err, (QuotaExhausted, ModelUnavailable)) and nxt:
                    self._switch(model, nxt, str(err)[:120])
                    continue
                if isinstance(err, (QuotaExhausted, ModelUnavailable)):
                    # Непробованных нет, но живы модели, давшие в этом вызове временный сбой (503/таймаут):
                    # переходим на одну из них и отдаём временную ошибку — внешний цикл повторит с паузой.
                    alive = self._next_model(model)
                    if alive:
                        self._switch(model, alive, str(err)[:120])
                        raise RetryableError(f"{err}; повтор на {alive}") from err
                if isinstance(err, RetryableError) and nxt:
                    if err.timeout:
                        self._switch(model, nxt, "таймаут")
                        continue
                    if "503" in str(err) or "UNAVAILABLE" in str(err):
                        self._unavail[model] = self._unavail.get(model, 0) + 1
                        if self._unavail[model] >= 2:
                            self._unavail[model] = 0
                            self._switch(model, nxt, "503 перегрузка")
                            continue
                raise err

    # ------------------------------------------------------ ошибки ---------
    def _classify(self, exc: Exception) -> Exception:
        if isinstance(exc, LLMError):
            return exc
        model = self.model or self.settings.model or "?"
        text = self._safe(exc)
        low = text.lower()
        code = getattr(exc, "code", None)
        details = getattr(exc, "details", None)
        name = type(exc).__name__
        if name in ("ReadTimeout", "ConnectTimeout", "TimeoutException", "WriteTimeout", "PoolTimeout",
                    "TimeoutError", "DeadlineExceeded") or "timed out" in low or "timeout" in low and code is None:
            return RetryableError(f"таймаут запроса к {model}", timeout=True)
        if code == 429 or "resource_exhausted" in low:
            if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low or "daily" in low:
                return QuotaExhausted(model, "лимит запросов в сутки")
            return RetryableError(f"429 {text[:200]}", retry_after=parse_retry_delay(text, details))
        if code in (404, 403) or "not_found" in low or "permission_denied" in low:
            return ModelUnavailable(model, text[:200])
        if isinstance(code, int) and code >= 500 or "unavailable" in low or "internal" in low and code is None:
            return RetryableError(f"{code or ''} {text[:200]}", retry_after=parse_retry_delay(text, details))
        if name in ("ConnectError", "ConnectionError", "RemoteProtocolError", "ReadError", "ConnectTimeout") \
                or isinstance(exc, (ConnectionError, OSError)):
            return RetryableError(f"сеть: {name}: {text[:200]}")
        return exc
