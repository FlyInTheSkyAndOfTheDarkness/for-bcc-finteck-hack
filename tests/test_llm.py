"""Тесты LLM-инфраструктуры без сети + живой смоук (RUN_LIVE=1)."""
from __future__ import annotations

import json
import os
import time

import pytest

from reqcheck.config import Settings, load_settings
from reqcheck.llm import LLMError
from reqcheck.providers.base import BaseLLM, QuotaExhausted, RateLimiter, RetryableError, strip_fences
from reqcheck.providers.gemini import GeminiLLM, order_models, parse_retry_delay
from reqcheck.schemas import FindingDrafts, SegResult

KEY = "SECRET-KEY-123456"


class Fake(BaseLLM):
    def __init__(self, settings, script):
        super().__init__(settings)
        self.script = list(script)
        self.calls = []
        self._sleep = lambda s: self.slept.append(s)
        self.slept = []

    def _complete_json(self, system, user, json_schema, schema, temperature):
        self.calls.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, 10


def mk(tmp_path, **kw):
    d = dict(provider="fake", api_key=KEY, cache_dir=tmp_path / "c", rpm=0,
             trace_path=tmp_path / "t.jsonl", model="m1")
    d.update(kw)
    return Settings(**d)


OK = json.dumps({"requirements": []})


def test_cache_hit_and_miss(tmp_path):
    f = Fake(mk(tmp_path), [OK, OK])
    f.generate_json("s", "u", SegResult, tag="a")
    f.generate_json("s", "u", SegResult, tag="a")
    assert f.stats.calls == 1 and f.stats.cache_hits == 1 and f.stats.tokens_total == 10
    f.generate_json("s", "u2", SegResult, tag="a")  # другой ввод -> промах
    assert f.stats.calls == 2 and f.stats.by_tag["a"] == 2


def test_cache_disabled(tmp_path):
    f = Fake(mk(tmp_path, use_cache=False), [OK, OK])
    f.generate_json("s", "u", SegResult, tag="a")
    f.generate_json("s", "u", SegResult, tag="a")
    assert f.stats.calls == 2 and f.stats.cache_hits == 0


def test_retry_on_429_uses_retry_delay(tmp_path):
    f = Fake(mk(tmp_path), [RetryableError("429", retry_after=7.0), OK])
    f.generate_json("s", "u", SegResult, tag="a")
    assert len(f.calls) == 2 and 7.0 <= f.slept[0] <= 7.0 * 1.25 + 0.01


def test_backoff_exhausted(tmp_path):
    f = Fake(mk(tmp_path, max_retries=2), [RetryableError("503")] * 5)
    with pytest.raises(LLMError):
        f.generate_json("s", "u", SegResult, tag="a")
    assert len(f.calls) == 3 and f.slept[1] > f.slept[0] * 1.2


def test_quota_error_propagates_with_model_name(tmp_path):
    f = Fake(mk(tmp_path), [QuotaExhausted("gemini-x-flash")])
    with pytest.raises(QuotaExhausted, match="gemini-x-flash"):
        f.generate_json("s", "u", SegResult, tag="a")


def test_validation_retry_once(tmp_path):
    f = Fake(mk(tmp_path), ['{"wrong": 1}', "```json\n" + OK + "\n```"])
    out = f.generate_json("s", "u", SegResult, tag="a")
    assert out.requirements == [] and "не прошёл проверку" in f.calls[1] and f.calls[1].startswith("u")
    f2 = Fake(mk(tmp_path / "x"), ["bad", "bad"])
    with pytest.raises(LLMError):
        f2.generate_json("s", "u", SegResult, tag="a")
    assert len(f2.calls) == 2


def test_rate_limiter_timing():
    rl = RateLimiter(2, window=0.4)
    t0 = time.monotonic()
    for _ in range(3):
        rl.acquire()
    assert 0.35 <= time.monotonic() - t0 < 1.0


def test_trace_written_without_key(tmp_path):
    s = mk(tmp_path)
    f = Fake(s, [RetryableError("429 " + KEY, retry_after=0), OK])
    f.generate_json("sys", "user text", SegResult, tag="seg")
    f.generate_json("sys", "user text", SegResult, tag="seg")  # из кэша
    raw = s.trace_path.read_text(encoding="utf-8")
    assert KEY not in raw
    recs = [json.loads(l) for l in raw.splitlines()]
    assert len(recs) == 3 and recs[-1]["cache_hit"] is True and recs[0]["error"]
    assert {"ts", "tag", "provider", "model", "latency_s", "tokens", "attempt", "system_sha",
            "user_sha", "user", "response", "error"} <= set(recs[1])
    assert not any(KEY in str(f.stats) for _ in [0])


def test_strip_fences():
    assert strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_fences('Вот: {"a":1} готово') == '{"a":1}'


def test_model_order_and_retry_delay():
    names = ["models/gemini-2.5-flash", "models/gemini-3.5-flash-lite", "models/gemini-3.8-flash",
             "models/gemini-3.5-flash", "models/gemini-3.6-flash-preview", "models/gemini-2.5-pro",
             "models/gemini-3.5-flash-image", "models/gemini-3.1-flash-lite"]
    assert order_models(names) == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash",
                                   "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    det = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}
    assert parse_retry_delay("", det) == 34.0
    assert parse_retry_delay("Please retry in 12.5s.") == 12.5


def test_gemini_classify_and_fallback(tmp_path):
    class APIError(Exception):
        def __init__(self, code, msg, details=None):
            super().__init__(msg)
            self.code, self.details = code, details

    s = mk(tmp_path, provider="gemini", model="")
    g = GeminiLLM(s)
    g.model = "gemini-a-flash"
    assert isinstance(g._classify(APIError(404, "no longer available")), Exception)
    e = g._classify(APIError(429, "quota", {"error": {"details": [{"retryDelay": "9s"}]}}))
    assert isinstance(e, RetryableError) and e.retry_after == 9.0
    q = g._classify(APIError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel"))
    assert isinstance(q, QuotaExhausted) and "gemini-a-flash" in str(q)
    assert isinstance(g._classify(APIError(503, "UNAVAILABLE")), RetryableError)

    # рантайм-фолбэк: суточная квота первой модели -> вторая
    g._ready, g._chain = True, ["gemini-a-flash", "gemini-b-flash-lite"]
    used = []

    def fake_generate(model, *a, **k):
        used.append(model)
        if model == "gemini-a-flash":
            raise APIError(429, "RESOURCE_EXHAUSTED PerDay")
        return OK, 5

    g._generate = fake_generate
    out = g.generate_json("s", "u", SegResult, tag="t")
    assert out.requirements == [] and used == ["gemini-a-flash", "gemini-b-flash-lite"]
    assert g.model == "gemini-b-flash-lite"


def test_gemini_chain_wraps_skips_dead_and_recovers(tmp_path):
    class APIError(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code, self.details = code, None

    s = mk(tmp_path, provider="gemini", model="")
    g = GeminiLLM(s)
    g._ready, g._chain = True, ["gemini-p-flash-lite", "gemini-x-flash", "gemini-old-flash"]
    g.model = "gemini-x-flash"                     # уже ушли с основной из-за 503
    g._left_primary = 1.0                          # давно
    g.recover_s = 0
    state = {"primary_up": False}
    used = []

    def fake_generate(model, *a, **k):
        used.append(model)
        if model == "gemini-old-flash":
            raise APIError(404, "model is no longer available")
        if model == "gemini-p-flash-lite" and not state["primary_up"]:
            raise APIError(429, "RESOURCE_EXHAUSTED PerDay")
        if model == "gemini-x-flash":
            raise APIError(404, "not found")
        return OK, 1

    g._generate = fake_generate
    state["primary_up"] = True
    out = g.generate_json("s", "u", SegResult, tag="t1")      # возврат на основную модель
    assert out.requirements == [] and used == ["gemini-p-flash-lite"] and g.model == "gemini-p-flash-lite"

    # основная исчерпала квоту, x и old мертвы → понятная ошибка, без зацикливания
    used.clear()
    state["primary_up"] = False
    with pytest.raises(Exception):
        g.generate_json("s", "u2", SegResult, tag="t2")
    assert len(used) == len(set(used)) == 3                    # каждая модель — один раз
    assert g._dead == {"gemini-p-flash-lite", "gemini-x-flash", "gemini-old-flash"}


def test_gemini_transient_model_retried_after_last_fallback_dies(tmp_path):
    """503 на живой модели, затем 404 на последней запасной → не падать, а повторить живую с паузой."""
    class APIError(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code, self.details = code, None

    s = mk(tmp_path, provider="gemini", model="")
    g = GeminiLLM(s)
    g._ready, g._chain = True, ["gemini-p-flash-lite", "gemini-x-flash-lite", "gemini-old-flash"]
    g._dead = {"gemini-p-flash-lite"}                 # основная исчерпала квоту
    g.model = "gemini-x-flash-lite"
    g._unavail["gemini-x-flash-lite"] = 1             # второй 503 подряд → уход на следующую
    calls = []

    def fake_generate(model, *a, **k):
        calls.append(model)
        if model == "gemini-old-flash":
            raise APIError(404, "model is no longer available")
        if calls.count(model) == 1:
            raise APIError(503, "UNAVAILABLE")
        return OK, 1

    g._generate = fake_generate
    g._sleep = lambda *_: None
    out = g.generate_json("s", "u", SegResult, tag="t")
    assert out.requirements == []
    assert calls == ["gemini-x-flash-lite", "gemini-old-flash", "gemini-x-flash-lite"]
    assert "gemini-old-flash" in g._dead and g.model == "gemini-x-flash-lite"


@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="живой тест: RUN_LIVE=1")
def test_live_gemini_findingdrafts(tmp_path):
    from reqcheck.providers import make_client
    s = load_settings()
    if not s.api_key:
        pytest.skip("нет GEMINI_API_KEY")
    s.cache_dir, s.use_cache, s.trace_path = tmp_path / "c", False, tmp_path / "t.jsonl"
    c = make_client(s)
    out = c.generate_json(
        "Ты аналитик требований. Отвечай по-русски. rids — идентификаторы вида R-01.",
        "R-01: Система должна быстро обрабатывать платежи. Найди дефекты формулировки.",
        FindingDrafts, tag="live")
    assert isinstance(out, FindingDrafts)
    assert s.api_key not in s.trace_path.read_text(encoding="utf-8")
    print("model:", c.model, "tokens:", c.stats.tokens_total)
