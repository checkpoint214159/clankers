"""Regression tests for the 2026-08-24 adversarial safety review findings.

Each test pins one reviewed defect closed; see the finding number in each docstring.
"""

from __future__ import annotations

import dataclasses
import math

import pytest
from clankers.config import load_robots
from clankers.hand_gateway.bus import MockBus
from clankers.hand_gateway.dynamixel_compat import TICKS_PER_REV, rad_to_ticks, ticks_to_rad
from clankers.hand_gateway.service import GatewayError, GatewayService
from clankers.safety import SafetyClamp, clamp_step
from clankers.tasks.runner import TaskRunner

CFG = load_robots()


def _service(*, stepped_pos: bool = True, **kwargs) -> tuple[GatewayService, MockBus]:
    """A service whose `pos` step-clamps, regardless of what robots.yaml currently says.

    These findings are about the step clamp itself, so the config is constructed here rather
    than read from disk -- turning `step_clamp_pos` off in robots.yaml (which is valid once
    the servos enforce a firmware velocity profile) must not silently delete this coverage.
    """
    hand = dataclasses.replace(
        CFG.hand, safety={**CFG.hand.safety, "step_clamp_pos": stepped_pos}
    )
    bus = MockBus(hand)
    bus.connect()
    svc = GatewayService(bus, hand, **kwargs)
    return svc, bus


# -- finding 1: clamp must reseed from live positions on EVERY enable ----------------------


def test_clamp_reseeds_on_reenable_after_manual_repositioning() -> None:
    svc, bus = _service(allow_uncalibrated=True)
    svc.enable()
    svc.disable()
    # Hand back-driven while torque was off: servo 2 (index_pip) now sits at 1.0 rad.
    bus._pos[2] = 1.0
    bus._target[2] = 1.0
    svc.enable()
    out = svc.pos(targets={"index_pip": 0.0})
    # Step-limited from the LIVE 1.0 rad, not from the stale pre-disable target:
    max_step = float(CFG.hand.safety["max_step_rad"])
    assert out["targets"]["index_pip"] == pytest.approx(1.0 - max_step)


# -- finding 2: jog must keep the shared clamp's last-target in sync -----------------------


def test_pos_step_limits_from_jogged_position_not_stale_target() -> None:
    svc, _bus = _service(allow_uncalibrated=True)
    svc.enable()
    max_step = float(CFG.hand.safety["max_step_rad"])
    jogged = 0.0
    for _ in range(4):  # walk index_pip out to ~0.6 rad via jog
        jogged = svc.jog(servo_id=2, delta_rad=max_step)["target"]
    out = svc.pos(targets={"index_pip": 0.0})
    # One step back from where the jogs actually left the joint:
    assert out["targets"]["index_pip"] == pytest.approx(jogged - max_step)


# -- finding 3: non-finite targets are rejected, never coerced to a limit ------------------


def test_nan_rejected_everywhere() -> None:
    clamp = SafetyClamp.for_hand(CFG.hand)
    with pytest.raises(ValueError, match="non-finite"):
        clamp.apply({"index_pip": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        clamp_step(0.0, float("nan"), 0.15)

    svc, _bus = _service(allow_uncalibrated=True)
    svc.enable()
    with pytest.raises(GatewayError, match="non-finite"):
        svc.jog(servo_id=0, delta_rad=float("nan"))
    with pytest.raises(ValueError, match="non-finite"):
        svc.pos(targets={"index_pip": float("nan")})


def test_infinite_targets_rejected_like_nan() -> None:
    svc, _bus = _service(allow_uncalibrated=True)
    svc.enable()
    with pytest.raises(ValueError, match="non-finite"):
        svc.pos(targets={"index_pip": math.inf})


# -- finding 4: tick conversion stays inside the unsigned register range -------------------


def test_rad_to_ticks_in_unsigned_range_for_every_configured_joint() -> None:
    for j in CFG.hand.joints:
        for rad in (j.limit.min, j.limit.max, 0.0):
            ticks = rad_to_ticks(rad)
            assert 0 <= ticks < TICKS_PER_REV, (j.name, rad, ticks)
            assert ticks_to_rad(ticks) == pytest.approx(rad, abs=2 * math.tau / TICKS_PER_REV)


def test_zero_rad_is_center_tick() -> None:
    assert rad_to_ticks(0.0) == TICKS_PER_REV // 2


# -- finding 6: enable refuses servos with unacknowledged latched faults -------------------


def test_enable_refused_until_faulted_servo_rebooted() -> None:
    svc, bus = _service()
    bus.inject_fault(5, 1 << 5)  # overload latched on servo 5
    with pytest.raises(GatewayError, match="unacknowledged faults"):
        svc.enable()
    svc.reboot(servo_id=5)  # explicit acknowledgment (ADR-0004)
    assert svc.enable()["servo_ids"] == list(range(16))


# -- finding 5: runner hands off to stop_safe on a mid-task fault --------------------------


class _ExplodingAdapter:
    def __init__(self) -> None:
        self.stopped_safe = False
        self._n = 0

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def get_positions(self) -> dict[str, float]:
        return {j.name: 0.0 for j in CFG.hand.joints}

    def send_targets(self, targets: dict[str, float]) -> dict[str, float]:
        self._n += 1
        if self._n > 1:
            raise RuntimeError("bus fell over")
        return targets

    def stop_safe(self) -> None:
        self.stopped_safe = True


async def test_runner_calls_stop_safe_when_adapter_raises_mid_task() -> None:
    from clankers.tasks.library import load_library

    adapter = _ExplodingAdapter()
    events: list[str] = []
    runner = TaskRunner(
        adapter=adapter,
        library=load_library(CFG),
        on_event=lambda e: events.append(e.event),
    )
    with pytest.raises(RuntimeError, match="bus fell over"):
        await runner.run_pose("hand_open", duration_s=0.2)
    assert adapter.stopped_safe is True
    assert "error" in events


# -- firmware motion profile: the speed guard moves into the servo -------------------------


def test_pos_without_step_clamp_reaches_the_target_in_one_command() -> None:
    """With `step_clamp_pos: false` a pose command arrives in one call.

    The step clamp existed to stop a single command commanding a fast, far jump. Once
    Profile_Velocity bounds the servo's own speed, slicing the move host-side only means an
    operator has to press "go to zero" repeatedly to actually get there.
    """
    svc, bus = _service(stepped_pos=False, allow_uncalibrated=True)
    svc.enable()
    bus._pos[2] = 1.0
    bus._target[2] = 1.0
    svc.enable()  # reseed the clamp from the live position
    out = svc.pos(targets={"index_pip": 0.0})
    assert out["targets"]["index_pip"] == pytest.approx(0.0)


def test_pos_without_step_clamp_still_honours_joint_limits() -> None:
    """Dropping the step clamp must not drop the limit clamp with it."""
    svc, _bus = _service(stepped_pos=False, allow_uncalibrated=True)
    svc.enable()
    joint = CFG.hand.joint_by_servo_id(2)
    out = svc.pos(targets={joint.name: 99.0})
    assert out["targets"][joint.name] == pytest.approx(joint.limit.max)


def test_jog_keeps_its_step_clamp_even_when_pos_does_not() -> None:
    """Jog is a nudge primitive, so it stays bounded regardless of the pos setting."""
    svc, _bus = _service(stepped_pos=False, allow_uncalibrated=True)
    svc.enable()
    max_step = float(CFG.hand.safety["max_step_rad"])
    out = svc.jog(servo_id=2, delta_rad=99.0)
    assert out["target"] == pytest.approx(max_step)
