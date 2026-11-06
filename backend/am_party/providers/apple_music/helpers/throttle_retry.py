"""Request throttling with retries and server-imposed cooldowns.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/helpers/throttle_retry.py, which itself embeds asyncio_throttle by
hallazzang, MIT).
Modifications: uses the local error classes instead of music_assistant_models.errors,
drops the BYPASS_THROTTLER contextvar / set_rate_limit and MA logger/translation plumbing.
The rate limit, cooldown and backoff logic is unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Concatenate, Protocol

from ..errors import RateLimited, ResourceTemporarilyUnavailable, RetriesExhausted

# Cap exponential backoff to prevent absurd wait times
MAX_BACKOFF = 120

# Cap a server-provided Retry-After, in case it is absurd or hostile
MAX_RETRY_AFTER = 86400

# Longest single wait a server can ask of a call, a call asked to wait longer fails instead
MAX_WAIT_TIME = 60


def parse_retry_after(value: str | None) -> int:
    """Parse a Retry-After header value (delay-seconds or HTTP-date), 0 if absent/invalid."""
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except ValueError, TypeError:
        pass
    try:
        target = parsedate_to_datetime(value)
        delta = (target - datetime.now(UTC)).total_seconds()
        return max(0, int(delta))
    except ValueError, TypeError:
        return 0


class Throttler:
    """Sliding-window rate limiter (asyncio_throttle with accurate sleeps)."""

    def __init__(self, rate_limit: int, period: float = 1.0) -> None:
        self.rate_limit = rate_limit
        self.period = period
        self._task_logs: deque[float] = deque()

    async def acquire(self) -> float:
        """Acquire a free slot, return the time spent waiting for it."""
        cur_time = time.monotonic()
        start_time = cur_time
        while True:
            self._flush()
            if len(self._task_logs) < self.rate_limit:
                break
            time_to_release = self._task_logs[0] + self.period - cur_time
            await asyncio.sleep(time_to_release)
            cur_time = time.monotonic()
        self._task_logs.append(cur_time)
        return cur_time - start_time

    def _flush(self) -> None:
        now = time.monotonic()
        while self._task_logs:
            if now - self._task_logs[0] > self.period:
                self._task_logs.popleft()
            else:
                break


class ThrottlerManager:
    """Throttler plus retry settings and a shared server-imposed cooldown."""

    def __init__(
        self, rate_limit: int, period: float = 1, retry_attempts: int = 5, initial_backoff: int = 5
    ) -> None:
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff
        self.throttler = Throttler(rate_limit, period)
        self._cooldown_until: float = 0.0

    @property
    def cooldown_remaining(self) -> float:
        """Seconds a server-imposed rate limit still holds every caller back."""
        return max(0.0, self._cooldown_until - time.monotonic())

    @asynccontextmanager
    async def acquire(self, honored_until: float = 0.0) -> AsyncGenerator[float]:
        """Wait out any cooldown and take a rate-limit slot; yield the delay."""
        delay = 0.0
        honored = honored_until
        while True:
            while (target := self._cooldown_until) > honored:
                if (remaining := self.cooldown_remaining) > MAX_WAIT_TIME:
                    raise RateLimited(
                        f"Rate limited for another {remaining:.0f} seconds",
                        backoff_time=round(remaining),
                    )
                delay += await self._wait_until(target)
                honored = target
            delay += await self.throttler.acquire()
            if self._cooldown_until <= honored:
                break
        yield delay

    def set_cooldown(self, seconds: float) -> None:
        """Hold back every caller of this throttler for the given number of seconds."""
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    async def _wait_until(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        await asyncio.sleep(remaining)
        return remaining


class _Throttleable(Protocol):
    @property
    def logger(self) -> logging.Logger: ...

    @property
    def throttler(self) -> ThrottlerManager: ...


def throttle_with_retries[T: _Throttleable, **P, R](
    func: Callable[Concatenate[T, P], Awaitable[R]],
) -> Callable[Concatenate[T, P], Coroutine[Any, Any, R]]:
    """Call an async method through ``self.throttler``, retrying transient failures."""

    @functools.wraps(func)
    async def wrapper(self: T, *args: P.args, **kwargs: P.kwargs) -> R:
        throttler = self.throttler
        exp_backoff = throttler.initial_backoff
        honored_until = 0.0
        for attempt in range(throttler.retry_attempts):
            try:
                async with throttler.acquire(honored_until) as delay:
                    if delay != 0:
                        self.logger.debug(
                            "%s was delayed for %.3f secs due to throttling", func.__name__, delay
                        )
                    try:
                        return await func(self, *args, **kwargs)
                    except ResourceTemporarilyUnavailable as e:
                        self.logger.info(
                            "Attempt %d/%d failed: %s", attempt + 1, throttler.retry_attempts, e
                        )
                        server_wait = min(max(float(e.backoff_time), 0.0), MAX_RETRY_AFTER)
                        if server_wait > MAX_WAIT_TIME:
                            if isinstance(e, RateLimited):
                                throttler.set_cooldown(server_wait)
                            raise RetriesExhausted(
                                f"Not retrying, asked to wait {server_wait:.0f} seconds"
                            ) from e
                        if attempt < throttler.retry_attempts - 1:
                            if isinstance(e, RateLimited):
                                # Retry-After is a floor: escalate above it, jitter upwards
                                base = max(server_wait, min(exp_backoff, MAX_BACKOFF))
                                sleep_time = base * random.uniform(1.0, 1.1)
                                exp_backoff = min(exp_backoff * 2, MAX_BACKOFF)
                            elif server_wait:
                                sleep_time = server_wait * random.uniform(1.0, 1.1)
                            else:
                                sleep_time = min(
                                    exp_backoff * random.uniform(0.75, 1.25), MAX_BACKOFF
                                )
                                exp_backoff = min(exp_backoff * 2, MAX_BACKOFF)
                            if isinstance(e, RateLimited):
                                # a rate limit applies to the whole account: hold everyone back
                                throttler.set_cooldown(sleep_time)
                            self.logger.info("Retrying in %.1f seconds...", sleep_time)
                            honored_until = time.monotonic() + sleep_time
                            await asyncio.sleep(sleep_time)
                        elif isinstance(e, RateLimited):
                            throttler.set_cooldown(max(server_wait, min(exp_backoff, MAX_BACKOFF)))
            except RateLimited as e:
                # raised by the gate (cooldown longer than MAX_WAIT_TIME)
                raise RetriesExhausted(f"Not retrying, asked to wait {e.backoff_time} seconds") from e
        raise RetriesExhausted(f"Retries exhausted, failed after {throttler.retry_attempts} attempts")

    return wrapper
