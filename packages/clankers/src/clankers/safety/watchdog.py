"""Client-heartbeat watchdog (ADR-0004 layer 3).

Any process commanding motors must feed the watchdog; on expiry the owner torques off.
Timeout is watchdog_multiplier x expected interval (3-5x convention: catches real hangs
without false-triggering on jitter).
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Watchdog:
    def __init__(
        self,
        interval_s: float,
        multiplier: float,
        on_trip: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_s <= 0 or multiplier <= 0:
            raise ValueError("interval_s and multiplier must be positive")
        self.timeout_s = interval_s * multiplier
        self._on_trip = on_trip
        self._clock = clock
        self._last_feed: float | None = None
        self.tripped = False

    def start(self) -> None:
        self._last_feed = self._clock()
        self.tripped = False

    def disarm(self) -> None:
        """Stop supervising (operator intentionally torqued everything off). Keeps a trip latched."""
        self._last_feed = None

    @property
    def armed(self) -> bool:
        return self._last_feed is not None and not self.tripped

    def feed(self) -> None:
        if self.tripped:
            return  # a tripped watchdog stays tripped until explicitly re-armed via start()
        self._last_feed = self._clock()

    def check(self) -> bool:
        """Poll from the owner's control loop. Returns True if tripped (now or earlier)."""
        if self.tripped:
            return True
        if self._last_feed is None:
            return False  # not armed
        if self._clock() - self._last_feed > self.timeout_s:
            self.tripped = True
            self._on_trip()
        return self.tripped
