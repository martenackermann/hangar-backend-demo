"""In-process sliding-window rate limiter (login throttling).

State lives in the API process: good enough for the single-process demo, and every
replica of a scaled deployment still throttles on its own. A reverse proxy or WAF is the
place for a shared limit. Client IPs come from ``request.client`` — uvicorn runs with
``proxy_headers=True``, so set ``FORWARDED_ALLOW_IPS`` to the proxy's address when the API
sits behind one.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Hashable

from app.core.errors import ApiError


class TooManyRequests(ApiError):
    status, code, title = 429, "too_many_requests", "Too many requests"


class SlidingWindowLimiter:
    """At most ``limit`` hits per ``window`` seconds and key; bounded number of keys."""

    def __init__(
        self,
        *,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hits: dict[Hashable, deque[float]] = {}
        self._max_keys = max_keys
        self._clock = clock

    def _recent(self, key: Hashable, window: float) -> deque[float] | None:
        hits = self._hits.get(key)
        if hits is None:
            return None
        cutoff = self._clock() - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def retry_after(self, key: Hashable, *, limit: int, window: float) -> int | None:
        """Seconds until ``key`` may try again, or ``None`` while it is under ``limit``."""
        hits = self._recent(key, window)
        if hits is None or len(hits) < limit:
            return None
        return max(1, math.ceil(hits[-limit] + window - self._clock()))

    def hit(self, key: Hashable, *, window: float) -> None:
        if key not in self._hits and len(self._hits) >= self._max_keys:
            self._prune(window)
            while len(self._hits) >= self._max_keys:  # still full: drop the oldest key
                del self._hits[next(iter(self._hits))]
        self._hits.setdefault(key, deque()).append(self._clock())

    def check(self, key: Hashable, *, limit: int, window: float, message: str) -> None:
        """``429 too_many_requests`` (with ``Retry-After``) when ``key`` is over ``limit``."""
        wait = self.retry_after(key, limit=limit, window=window)
        if wait is not None:
            raise TooManyRequests(message, headers={"Retry-After": str(wait)})

    def reset(self, key: Hashable | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)

    def _prune(self, window: float) -> None:
        for key in list(self._hits):
            self._recent(key, window)


#: Login throttling state shared by ``/auth/login`` and ``/auth/persona-login``.
login_limiter = SlidingWindowLimiter()
