"""Мини-HTTP на urllib для провайдеров без SDK."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request

from .base import RetryableError
from reqcheck.llm import LLMError


def post_json(url: str, payload: dict, headers: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        ra = e.headers.get("Retry-After") if e.headers else None
        delay = float(ra) if ra and re.fullmatch(r"\d+(\.\d+)?", ra) else None
        if e.code == 429 or e.code >= 500:
            raise RetryableError(f"HTTP {e.code}: {body}", retry_after=delay)
        raise LLMError(f"HTTP {e.code}: {body}")
    except (socket.timeout, TimeoutError):
        raise RetryableError("таймаут запроса", timeout=True)
    except urllib.error.URLError as e:
        raise RetryableError(f"нет соединения с {url}: {e.reason}")
