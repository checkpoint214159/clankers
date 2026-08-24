"""FakeRobot behavior tests (clankers.tasks.robot_adapter)."""

from __future__ import annotations

import pytest
from clankers.config import load_robots
from clankers.tasks.robot_adapter import FakeRobot, joint_names
from conftest import FakeClock

CFG = load_robots()


def test_joint_names_cover_arm_and_hand() -> None:
    names = joint_names(CFG)
    assert set(names) == {j.name for j in CFG.arm.joints} | {j.name for j in CFG.hand.joints}
    assert len(names) == len(set(names))


def test_starts_at_all_zeros_clamped_into_limits() -> None:
    robot = FakeRobot(CFG, clock=FakeClock().now)
    positions = robot.get_positions()
    assert set(positions) == set(joint_names(CFG))
    for value in positions.values():
        assert value == pytest.approx(0.0)


def test_send_targets_requires_connect_first() -> None:
    robot = FakeRobot(CFG, clock=FakeClock().now)
    with pytest.raises(RuntimeError):
        robot.send_targets({"joint1": 0.5})


def test_send_targets_rejects_unknown_joint() -> None:
    clock = FakeClock()
    robot = FakeRobot(CFG, clock=clock.now)
    robot.connect()
    with pytest.raises(KeyError):
        robot.send_targets({"not_a_joint": 0.1})


def test_first_send_is_bounded_by_the_max_step_clamp() -> None:
    clock = FakeClock()
    robot = FakeRobot(CFG, clock=clock.now)
    robot.connect()
    max_step = float(CFG.arm.safety["max_step_rad"])
    sent = robot.send_targets({"joint1": 999.0})
    assert sent["joint1"] == pytest.approx(max_step)


def test_repeated_overshoot_saturates_exactly_at_the_joint_limit() -> None:
    clock = FakeClock()
    robot = FakeRobot(CFG, clock=clock.now)
    robot.connect()
    limit = next(j for j in CFG.arm.joints if j.name == "joint1").limit
    max_step = float(CFG.arm.safety["max_step_rad"])
    steps = int(limit.max / max_step) + 2  # enough max_step hops to walk up to the limit
    sent = {}
    for _ in range(steps):
        sent = robot.send_targets({"joint1": limit.max + 100.0})
    assert sent["joint1"] == pytest.approx(limit.max)


def test_get_positions_settles_toward_target_over_time() -> None:
    clock = FakeClock()
    robot = FakeRobot(CFG, tau_s=0.1, clock=clock.now)
    robot.connect()
    max_step = float(CFG.arm.safety["max_step_rad"])
    target = max_step / 2  # stay within the step clamp so the lag target is exactly this
    robot.send_targets({"joint1": target})

    clock.advance(0.001)
    early = robot.get_positions()["joint1"]

    clock.advance(5.0)  # many time constants later
    late = robot.get_positions()["joint1"]

    assert 0.0 < early < target
    assert late == pytest.approx(target, abs=1e-3)


def test_disconnect_then_send_targets_raises() -> None:
    clock = FakeClock()
    robot = FakeRobot(CFG, clock=clock.now)
    robot.connect()
    robot.disconnect()
    with pytest.raises(RuntimeError):
        robot.send_targets({"joint1": 0.1})
