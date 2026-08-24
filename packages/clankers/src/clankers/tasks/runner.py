"""TaskRunner: drives a RobotAdapter through poses/skills from the library.

Frames are paced against wall-clock time (or an injectable clock/sleep pair, for fast
deterministic tests) so a skill takes its declared duration regardless of how fast the
event loop can iterate. `stop()` is cooperative: it's checked once a frame has finished
sending, never mid-frame, so a partial command is never left in flight.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .library import PoseLibrary
from .profiles import plan_move
from .robot_adapter import RobotAdapter

EventCallback = Callable[["TaskEvent"], None]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class TaskEvent:
    """One runner lifecycle event, in order: accepted, progress* , (done | stopped | error)."""

    event: str  # "accepted" | "progress" | "done" | "stopped" | "error"
    name: str
    frac: float | None = None
    message: str | None = None


class TaskRunner:
    """Owns one `RobotAdapter` and one `PoseLibrary`; runs one task at a time."""

    def __init__(
        self,
        adapter: RobotAdapter,
        library: PoseLibrary,
        on_event: EventCallback | None = None,
        hz: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._adapter = adapter
        self._library = library
        self._on_event = on_event or (lambda event: None)
        self._hz = hz
        self._clock = clock
        self._sleep = sleep
        self._connected = False
        self._running = False
        self._stop_requested = False

    @property
    def running(self) -> bool:
        return self._running

    def current_positions(self) -> dict[str, float]:
        self._ensure_connected()
        return self._adapter.get_positions()

    def stop(self) -> None:
        """Request the running task stop after its in-flight frame finishes. No-op if idle."""
        self._stop_requested = True

    async def run_pose(self, name: str, duration_s: float = 2.0) -> None:
        pose = self._library.pose(name)
        await self._execute(name, [(pose, duration_s)])

    async def run_skill(self, name: str) -> None:
        skill = self._library.skill(name)
        steps = [(self._library.pose(step.pose), step.duration_s) for step in skill.steps]
        await self._execute(name, steps)

    def _ensure_connected(self) -> None:
        if not self._connected:
            self._adapter.connect()
            self._connected = True

    def _emit(self, event: str, name: str, frac: float | None = None, message: str | None = None) -> None:
        self._on_event(TaskEvent(event=event, name=name, frac=frac, message=message))

    async def _execute(self, name: str, steps: list[tuple[dict[str, float], float]]) -> None:
        if self._running:
            raise RuntimeError(f"cannot run {name!r}: a task is already running")
        self._running = True
        self._stop_requested = False
        try:
            self._ensure_connected()
            self._emit("accepted", name)
            total = sum(duration for _, duration in steps) or 1.0
            elapsed = 0.0
            current = self._adapter.get_positions()
            for pose_partial, duration_s in steps:
                goal = {**current, **pose_partial}
                frames = plan_move(current, goal, duration_s, hz=self._hz)
                stopped = await self._run_frames(name, frames, elapsed, total)
                if stopped:
                    return
                current = self._adapter.get_positions()
                elapsed += duration_s
            self._emit("done", name)
        except Exception as exc:
            # ADR-0004: never leave the hardware holding an arbitrary mid-task target
            # after a fault — hand off to the adapter's safe-stop before surfacing.
            stop = getattr(self._adapter, "stop_safe", None)
            if stop is not None:
                stop()
            self._emit("error", name, message=str(exc))
            raise
        finally:
            self._running = False

    async def _run_frames(
        self,
        name: str,
        frames: list[tuple[float, dict[str, float]]],
        elapsed_before: float,
        total: float,
    ) -> bool:
        """Send `frames` paced by `self._clock`/`self._sleep`. Returns True if stopped."""
        start_wall = self._clock()
        for t_offset, targets in frames:
            now = self._clock()
            remaining = start_wall + t_offset - now
            if remaining > 0:
                await self._sleep(remaining)
            self._adapter.send_targets(targets)
            frac = min(1.0, (elapsed_before + t_offset) / total)
            self._emit("progress", name, frac=frac)
            if self._stop_requested:
                self._emit("stopped", name)
                return True
        return False
