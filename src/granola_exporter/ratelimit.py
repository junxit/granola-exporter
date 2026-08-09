"""Client-side request pacing.

Both backends are rate limited, with different budgets: the public API documents
a 25-request burst per 5 seconds and a sustained 5 requests/second, while the
MCP server averages around 100 requests/minute across all tools. The limiter is
therefore generic — callers supply their own budget.
"""

from __future__ import annotations

import time


class RateLimiter:
    """Token bucket pacing requests to a documented burst and sustained rate."""

    def __init__(self, capacity: int, rate: float) -> None:
        """Initialize a full bucket.

        Args:
            capacity: Maximum burst size, in requests.
            rate: Sustained refill rate, in tokens per second.
        """
        self.capacity = capacity
        self.rate = rate
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
