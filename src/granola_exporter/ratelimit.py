"""Client-side request pacing.

Both backends are rate limited, with different budgets: the public API documents
a 25-request burst per 5 seconds and a sustained 5 requests/second, while the
MCP server documents ~100 requests/minute across all tools -- and says in the
same breath that the real budget varies by plan and by tool. The limiter is
therefore generic: callers supply their own budget, one bucket per budget, and
a rejected request can push its own bucket slower for the rest of the run.
"""

from __future__ import annotations

import time

# A penalized bucket halves its rate. Without a floor, a run that keeps being
# rejected would converge on never issuing another request.
DEFAULT_MIN_RATE = 1.0 / 60.0


class RateLimiter:
    """Token bucket pacing requests to a documented burst and sustained rate."""

    def __init__(
        self, capacity: int, rate: float, *, min_rate: float | None = None
    ) -> None:
        """Initialize a full bucket.

        Args:
            capacity: Maximum burst size, in requests.
            rate: Sustained refill rate, in tokens per second.
            min_rate: Slowest rate :meth:`penalize` may back off to. Defaults
                to one request per minute, or ``rate`` when that is already
                slower.
        """
        self.capacity = capacity
        self.rate = rate
        self.min_rate = min(rate, DEFAULT_MIN_RATE if min_rate is None else min_rate)
        self._tokens = float(capacity)
        self._updated = time.monotonic()

    def acquire(self) -> None:
        """Block until a request token is available, then consume it."""
        while True:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._updated) * self.rate
            )
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            time.sleep((1.0 - self._tokens) / self.rate)

    def penalize(self) -> float:
        """Halve the sustained rate after a rejection and empty the bucket.

        The server never says how long its quota window is, so the working rate
        has to be discovered. Holding the penalty for the rest of the run is the
        point: without it every call rediscovers the limit from scratch, which
        is how a blocked backfill spends its whole life in ``time.sleep``.

        Returns:
            The new sustained rate, in tokens per second.
        """
        self._tokens = 0.0
        self._updated = time.monotonic()
        self.rate = max(self.min_rate, self.rate / 2.0)
        return self.rate
