"""Shared test fixtures/helpers for clankers.tasks tests."""

from __future__ import annotations

import asyncio


class FakeClock:
    """A controllable clock + async sleep for deterministic, instant-settling tests.

    `sleep(dt)` advances the virtual clock by `dt` and yields once to the event loop
    (`asyncio.sleep(0)`) so any synchronous callback triggered by the caller still runs
    inline before the coroutine resumes.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt

    async def sleep(self, dt: float) -> None:
        self.advance(dt)
        await asyncio.sleep(0)
