"""TaskRunner tests against FakeRobot with a fast, injectable clock."""

from __future__ import annotations

from typing import Any

import pytest
from clankers.config import load_robots
from clankers.tasks.library import load_library
from clankers.tasks.robot_adapter import FakeRobot
from clankers.tasks.runner import TaskEvent, TaskRunner
from conftest import FakeClock

CFG = load_robots()
LIB = load_library(CFG)


class CountingAdapter:
    """Wraps a RobotAdapter, counting `send_targets` calls for stop() tests."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.send_count = 0

    def connect(self) -> None:
        self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def get_positions(self) -> dict[str, float]:
        return self._inner.get_positions()

    def send_targets(self, targets: dict[str, float]) -> dict[str, float]:
        self.send_count += 1
        return self._inner.send_targets(targets)


def _make_runner() -> tuple[TaskRunner, list[TaskEvent], FakeRobot, FakeClock]:
    clock = FakeClock()
    adapter = FakeRobot(CFG, clock=clock.now)
    events: list[TaskEvent] = []
    runner = TaskRunner(adapter, LIB, on_event=events.append, clock=clock.now, sleep=clock.sleep)
    return runner, events, adapter, clock


async def test_run_pose_emits_accepted_progress_done_in_order() -> None:
    runner, events, _adapter, _clock = _make_runner()
    await runner.run_pose("hand_curl", duration_s=2.0)

    assert events[0].event == "accepted"
    assert events[-1].event == "done"
    kinds = [e.event for e in events]
    assert kinds.count("accepted") == 1
    assert kinds.count("done") == 1
    assert all(k == "progress" for k in kinds[1:-1])


async def test_run_pose_progress_fraction_is_non_decreasing_and_ends_at_one() -> None:
    runner, events, _adapter, _clock = _make_runner()
    await runner.run_pose("hand_curl", duration_s=1.0)

    fracs = [e.frac for e in events if e.event == "progress"]
    assert fracs == sorted(fracs)
    assert fracs[-1] == pytest.approx(1.0)


async def test_run_pose_reaches_goal_within_tolerance() -> None:
    runner, _events, adapter, _clock = _make_runner()
    await runner.run_pose("hand_curl", duration_s=2.0)

    goal = LIB.pose("hand_curl")
    positions = adapter.get_positions()
    for name, value in goal.items():
        assert positions[name] == pytest.approx(value, abs=0.05)


async def test_stop_mid_skill_emits_stopped_and_sends_no_further_frames() -> None:
    clock = FakeClock()
    counting = CountingAdapter(FakeRobot(CFG, clock=clock.now))
    events: list[TaskEvent] = []
    runner: TaskRunner | None = None

    def on_event(event: TaskEvent) -> None:
        events.append(event)
        if event.event == "progress" and sum(1 for e in events if e.event == "progress") == 3:
            assert runner is not None
            runner.stop()

    runner = TaskRunner(counting, LIB, on_event=on_event, clock=clock.now, sleep=clock.sleep)

    await runner.run_skill("wave_fingers")

    assert events[-1].event == "stopped"
    assert "done" not in [e.event for e in events]
    sends_at_stop = sum(1 for e in events if e.event == "progress")
    assert counting.send_count == sends_at_stop
    # A full run of wave_fingers (3 steps x 1.5s @ 20Hz) sends far more frames than 3.
    assert counting.send_count < 3 * (int(1.5 * 20) + 1)


async def test_stop_when_idle_is_a_harmless_noop() -> None:
    runner, events, _adapter, _clock = _make_runner()
    runner.stop()  # nothing running yet
    await runner.run_pose("home", duration_s=0.5)
    assert events[-1].event == "done"


async def test_cannot_run_two_tasks_concurrently() -> None:
    runner, _events, _adapter, _clock = _make_runner()
    runner._running = True  # simulate a task already in flight, without racing real ones
    with pytest.raises(RuntimeError):
        await runner.run_pose("home", duration_s=0.5)
