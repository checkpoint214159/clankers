"""Quintic min-jerk profile tests (clankers.tasks.profiles)."""

from __future__ import annotations

import itertools

import pytest
from clankers.tasks.profiles import min_jerk, plan_move


def test_min_jerk_endpoints() -> None:
    assert min_jerk(0.0) == 0.0
    assert min_jerk(1.0) == 1.0


def test_min_jerk_clamps_outside_unit_interval() -> None:
    assert min_jerk(-0.5) == 0.0
    assert min_jerk(1.5) == 1.0


def test_min_jerk_is_monotonically_non_decreasing() -> None:
    samples = [min_jerk(i / 100) for i in range(101)]
    assert all(b >= a for a, b in itertools.pairwise(samples))


def test_min_jerk_midpoint_is_half() -> None:
    assert min_jerk(0.5) == pytest.approx(0.5)


def test_plan_move_first_and_last_frame_match_endpoints() -> None:
    start = {"a": 0.0, "b": 1.0}
    goal = {"a": 1.0, "b": -1.0}
    frames = plan_move(start, goal, duration_s=1.0, hz=20)
    t0, first = frames[0]
    tn, last = frames[-1]
    assert t0 == 0.0
    assert first == pytest.approx(start)
    assert tn == pytest.approx(1.0)
    assert last == pytest.approx(goal)


def test_plan_move_frame_count_matches_duration_times_hz() -> None:
    frames = plan_move({"a": 0.0}, {"a": 1.0}, duration_s=2.0, hz=20)
    assert len(frames) == 2 * 20 + 1


def test_plan_move_time_offsets_are_strictly_increasing() -> None:
    frames = plan_move({"a": 0.0}, {"a": 1.0}, duration_s=1.0, hz=10)
    offsets = [t for t, _ in frames]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)


def test_plan_move_missing_start_joint_treated_as_already_at_goal() -> None:
    frames = plan_move({}, {"a": 5.0}, duration_s=1.0, hz=10)
    values = [f["a"] for _, f in frames]
    assert all(v == pytest.approx(5.0) for v in values)


def test_plan_move_zero_duration_returns_goal_only() -> None:
    frames = plan_move({"a": 0.0}, {"a": 1.0}, duration_s=0.0)
    assert frames == [(0.0, {"a": 1.0})]


def test_plan_move_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        plan_move({"a": 0.0}, {"a": 1.0}, duration_s=-1.0)
    with pytest.raises(ValueError):
        plan_move({"a": 0.0}, {"a": 1.0}, duration_s=1.0, hz=0)
