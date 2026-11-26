"""Общая часть всех провайдеров: кэш, лимит запросов, повторы, разбор JSON, журнал."""
from __future__ import annotations

import abc
import hashlib
import json
import random
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from reqcheck.llm import LLMClient, LLMError, T


class RetryableError(LLMError):
    """Временная ошибка (429/5xx/таймаут/сеть). retry_after — задержка от сервера, сек."""

    def __init__(self, msg: str, retry_after: Optional[float] = None, timeout: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.timeout = timeout


class QuotaExhausted(LLMError):
    """Суточная квота модели исчерпана — повторять бессмысленно, нужна другая модель."""

    def __init__(self, model: str, msg: str = ""):
        super().__init__(f"Суточная квота модели {model} исчерпана. {msg}".strip())
        self.model = model


class ModelUnavailable(LLMError):
    """Модель не существует / недоступна ключу (404, 403) — пропустить модель."""

    def __init__(self, model: str, msg: str = ""):
        super().__init__(f"Модель {model} недоступна: {msg}".strip())
        self.model = model


class RateLimiter:
    """Скользящее окно: не более `rpm` запросов за `window` секунд. Потокобезопасно."""

    def __init__(self, rpm: int, window: float = 60.0):
        self.rpm = max(0, int(rpm))
        self.window = window
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Блокируется до свободного слота; возвращает, сколько секунд ждали."""
        if self.rpm <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window:
                    self._times.popleft()
                if len(self._times) < self.rpm:
                    self._times.append(now)
                    return waited
                delay = self.window - (now - self._times[0]) + 0.01
            time.sleep(delay)
            waited += delay


_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.S)


def strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = _FENCE.match(t)
    if m:
        return m.group(1).strip()
    if t and t[0] not in "{[":  # мусор вокруг JSON: берём от первой скобки до последней
        i = min([k for k in (t.find("{"), t.find("[")) if k >= 0], default=-1)
        j = max(t.rfind("}"), t.rfind("]"))
        if 0 <= i < j:
            return t[i:j + 1]
    return t


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class BaseLLM(LLMClient):
    def __init__(self, settings: Any) -> None:
        super().__init__()
        self.settings = settings
        self.provider = settings.provider
        self.model = settings.model or ""
        self._cache_model = settings.model or "auto"   # ключ кэша не зависит от выбранного позже имени
        self.limiter = RateLimiter(settings.rpm)
        self._trace_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.backoff_base = 2.0
        self.backoff_max = 60.0
        self._sleep = time.sleep

    @abc.abstractmethod
    def _complete_json(self, system: str, user: str, json_schema: dict, schema: type,
                       temperature: float) -> tuple[str, int]:
        """Вернуть (сырой JSON-текст, всего токенов). Ошибки — RetryableError/QuotaExhausted/LLMError."""

    def _classify(self, exc: Exception) -> Exception:
        """Преобразовать исключение SDK в RetryableError/QuotaExhausted/LLMError (по умолчанию — как есть)."""
        return exc

    def generate_json(self, system: str, user: str, schema: type[T], *, tag: str,
                      temperature: Optional[float] = None) -> T:
        try:
            return self._generate_json(system, user, schema, tag=tag, temperature=temperature)
        except LLMError:
            with self._stats_lock:   # стадии ловят ошибку и продолжают; счётчик виден в паспорте/CLI
                self.stats.errors += 1
            raise

    def _generate_json(self, system: str, user: str, schema: type[T], *, tag: str,
                       temperature: Optional[float] = None) -> T:
        s = self.settings
        temp = s.temperature if temperature is None else temperature
        js = schema.model_json_schema()
        key = _sha(json.dumps([self.provider, self._cache_model, system, user, js, temp],
                              ensure_ascii=False, sort_keys=True))
        cache_file = Path(s.cache_dir) / f"{key}.json"
        if s.use_cache and cache_file.exists():
            try:
                obj = schema.model_validate_json(cache_file.read_text(encoding="utf-8"))
                with self._stats_lock:
                    self.stats.cache_hits += 1
                self._trace(tag, system, user, 0.0, 0, True, 0, obj.model_dump_json(), None)
                return obj
            except Exception:  # noqa: BLE001  битый кэш игнорируем
                pass

        cur_user = user
        validation_retried = False
        attempt = 0
        net_attempts = 0
        while True:
            attempt += 1
            t0 = time.monotonic()
            raw, tokens = "", 0
            try:
                self.limiter.acquire()
                t0 = time.monotonic()
                raw, tokens = self._complete_json(system, cur_user, js, schema, temp)
            except Exception as e:  # noqa: BLE001
                err = self._classify(e)
                lat = time.monotonic() - t0
                with self._stats_lock:
                    self.stats.calls += 1
                    self.stats.by_tag[tag] = self.stats.by_tag.get(tag, 0) + 1
                self._trace(tag, system, user, lat, 0, False, attempt, "", self._safe(err))
                if isinstance(err, RetryableError):
                    net_attempts += 1
                    if net_attempts > s.max_retries:
                        raise LLMError(f"[{tag}] {self.model}: исчерпаны повторы: {self._safe(err)}") from e
                    delay = err.retry_after if err.retry_after is not None else \
                        min(self.backoff_max, self.backoff_base * 2 ** (net_attempts - 1))
                    delay = delay + random.uniform(0, 0.25 * max(delay, 1.0))
                    self._sleep(delay)
                    continue
                if isinstance(err, LLMError):
                    raise err from e
                raise LLMError(f"[{tag}] {self.model}: {type(err).__name__}: {self._safe(err)}") from e

            lat = time.monotonic() - t0
            with self._stats_lock:
                self.stats.calls += 1
                self.stats.tokens_total += int(tokens or 0)
                self.stats.by_tag[tag] = self.stats.by_tag.get(tag, 0) + 1
            try:
                obj = schema.model_validate(json.loads(strip_fences(raw)))
            except (ValueError, ValidationError) as e:
                last_err = str(e)[:1500]
                self._trace(tag, system, user, lat, tokens, False, attempt, raw, "parse: " + last_err[:500])
                if validation_retried:
                    raise LLMError(f"[{tag}] ответ модели не соответствует схеме {schema.__name__}: "
                                   f"{last_err[:300]}") from e
                validation_retried = True
                cur_user = (user + "\n\n---\nТвой предыдущий ответ не прошёл проверку JSON-схемы:\n"
                            f"{last_err}\nИсправь и верни ТОЛЬКО корректный JSON по схеме.")
                continue
            self._trace(tag, system, user, lat, tokens, False, attempt, raw, None)
            if s.use_cache:
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache_file.with_suffix(".tmp")
                    tmp.write_text(obj.model_dump_json(), encoding="utf-8")
                    tmp.replace(cache_file)
                except OSError:
                    pass
            return obj

    def _safe(self, e: Any) -> str:
        """Текст ошибки без ключа."""
        t = str(e)
        k = getattr(self.settings, "api_key", "") or ""
        if k:
            t = t.replace(k, "***")
        return t[:1000]

    def _trace(self, tag, system, user, latency, tokens, cache_hit, attempt, response, error) -> None:
        p = self.settings.trace_path
        if not p:
            return
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": tag, "provider": self.provider,
            "model": self.model or self._cache_model, "latency_s": round(latency, 3),
            "tokens": tokens, "cache_hit": cache_hit, "attempt": attempt,
            "system_sha": _sha(system)[:12], "user_sha": _sha(user)[:12],
            "user": user[:2000], "response": (response or "")[:4000], "error": error,
        }
        line = json.dumps(rec, ensure_ascii=False)
        k = getattr(self.settings, "api_key", "") or ""
        if k:
            line = line.replace(k, "***")
        try:
            with self._trace_lock:
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass
