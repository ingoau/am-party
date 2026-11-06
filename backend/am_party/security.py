"""Input validation and small in-memory rate-limiting helpers with bounded memory.

Everything here is keyed by attacker-controlled values (guest ids, client IPs), so
every map is a TTL + size bounded LRU: idle keys expire and the oldest keys are
evicted once ``maxsize`` is reached.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

# Apple Music ids: catalog (digits) or library/playlist-ish ids like "i.xxx", "l.xxx".
TRACK_ID_RE = re.compile(r"(?:[0-9]{1,20}|[ailp]\.[A-Za-z0-9]{1,64})", re.ASCII)


def valid_track_id(value: object) -> bool:
    return isinstance(value, str) and TRACK_ID_RE.fullmatch(value) is not None


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """LRU dict whose entries expire ``ttl`` seconds after their last write/touch."""

    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def __len__(self) -> int:
        self.prune()
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return self.get(key) is not None  # type: ignore[arg-type]

    def prune(self) -> None:
        cutoff = self._clock() - self.ttl
        while self._data:
            key, (ts, _) = next(iter(self._data.items()))
            if ts >= cutoff:
                break
            self._data.popitem(last=False)

    def get(self, key: K, default: V | None = None) -> V | None:
        item = self._data.get(key)
        if item is None:
            return default
        if item[0] < self._clock() - self.ttl:
            self._data.pop(key, None)
            return default
        return item[1]

    def set(self, key: K, value: V) -> None:
        self._data[key] = (self._clock(), value)
        self._data.move_to_end(key)
        self.prune()
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def pop(self, key: K, default: V | None = None) -> V | None:
        item = self._data.pop(key, None)
        return default if item is None else item[1]


class SlidingWindow(Generic[K]):
    """At most ``limit`` hits per ``window`` seconds per key."""

    def __init__(
        self,
        limit: int,
        window: float,
        maxkeys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: TTLCache[K, deque[float]] = TTLCache(maxkeys, window, clock)

    def _window(self, key: K) -> deque[float]:
        win = self._hits.get(key)
        if win is None:
            win = deque()
        t = self._clock()
        while win and t - win[0] >= self.window:
            win.popleft()
        return win

    def allowed(self, key: K) -> bool:
        """Whether one more hit would be within the limit (does not record it)."""
        return len(self._window(key)) < self.limit

    def hit(self, key: K) -> None:
        win = self._window(key)
        win.append(self._clock())
        while len(win) > self.limit:
            win.popleft()
        self._hits.set(key, win)

    def check_and_hit(self, key: K) -> bool:
        """Record a hit if allowed; return False (and record nothing) if over the limit."""
        if not self.allowed(key):
            return False
        self.hit(key)
        return True


class ConnectionCounter:
    """Concurrent connection caps, per key (e.g. client IP) and in total."""

    def __init__(self, per_key: int, total: int) -> None:
        self.per_key = per_key
        self.total = total
        self._counts: dict[str, int] = {}
        self._sum = 0

    @property
    def active(self) -> int:
        return self._sum

    def acquire(self, key: str) -> bool:
        if self._sum >= self.total or self._counts.get(key, 0) >= self.per_key:
            return False
        self._counts[key] = self._counts.get(key, 0) + 1
        self._sum += 1
        return True

    def release(self, key: str) -> None:
        n = self._counts.get(key, 0)
        if n <= 0:
            return
        self._sum -= 1
        if n == 1:
            del self._counts[key]
        else:
            self._counts[key] = n - 1
