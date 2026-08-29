"""Tests for the lerobot_robot_clankers plugin.

Never touches real hardware: LeapHand is always built with `use_mock=True` (an in-memory
bus, see leap_hand._MockDynamixelBus) so nothing here opens a serial port.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("lerobot")

from clankers.config import load_robots
from lerobot_robot_clankers import (
    ClankerStation,
    ClankerStationConfig,
    LeapHand,
    LeapHandConfig,
)
from lerobot_robot_clankers import leap_hand as leap_hand_module
from lerobot_robot_clankers.leap_hand import (
    _CENTER_TICK,
    HandNotCalibratedError,
    _patch_xc330_m288_table,
    _rad_to_ticks,
    _ticks_to_rad,
)

HAND_CFG = load_robots().hand
HAND_JOINT_NAMES = {joint.name for joint in HAND_CFG.joints}


def make_hand(*, allow_uncalibrated: bool = True, port: str = "/dev/mock-u2d2") -> LeapHand:
    return LeapHand(LeapHandConfig(port=port, use_mock=True, allow_uncalibrated=allow_uncalibrated))


def test_tick_rad_roundtrip_is_center_safe():
    assert _ticks_to_rad(_CENTER_TICK) == 0.0
    assert _rad_to_ticks(0.0) == _CENTER_TICK
    for rad in (-1.047, 0.5, 2.443):
        assert abs(_ticks_to_rad(_rad_to_ticks(rad)) - rad) < 1e-3


def test_leap_hand_feature_dicts_have_exactly_16_hand_keys():
    hand = make_hand()

    assert len(hand.observation_features) == 16
    assert len(hand.action_features) == 16
    assert set(hand.observation_features) == {f"hand.{name}.pos" for name in HAND_JOINT_NAMES}
    assert set(hand.action_features) == set(hand.observation_features)
    assert all(t is float for t in hand.observation_features.values())


def test_leap_hand_connect_get_observation_disconnect():
    hand = make_hand()
    assert not hand.is_connected

    hand.connect()
    assert hand.is_connected
    assert hand.is_calibrated  # LeRobot's own calibration mechanism doesn't apply here

    obs = hand.get_observation()
    assert set(obs) == set(hand.observation_features)
    assert all(value == 0.0 for value in obs.values())  # mock bus starts centered

    hand.disconnect()
    assert not hand.is_connected


def test_send_action_uncalibrated_guard_raises(monkeypatch):
    # The guard is about an unconfirmed joint map, not about whatever robots.yaml says
    # today: patch the config the plugin reads so that confirming the hand on the bench
    # cannot silently delete this coverage.
    import dataclasses

    from clankers.config import load_robots as _load

    full = _load()
    patched = dataclasses.replace(full, hand=dataclasses.replace(full.hand, calibrated=False))
    monkeypatch.setattr(leap_hand_module, "load_robots", lambda: patched)

    hand = make_hand(allow_uncalibrated=False)
    assert hand._hand_cfg.calibrated is False
    hand.connect()
    try:
        with pytest.raises(HandNotCalibratedError):
            hand.send_action({"hand.index_mcp_side.pos": 0.1})
    finally:
        hand.disconnect()


def test_send_action_clamps_past_limit():
    hand = make_hand(allow_uncalibrated=True)
    hand.connect()

    write_spy = MagicMock(wraps=hand.bus.sync_write)
    hand.bus.sync_write = write_spy

    joint = next(j for j in HAND_CFG.joints if j.name == "index_mcp_side")
    overshoot = joint.limit.max + 10.0  # far past the joint's own limit

    result = {}
    for _ in range(20):  # max_step_rad caps each single command; ramp until it converges
        result = hand.send_action({f"hand.{joint.name}.pos": overshoot})

    assert result[f"hand.{joint.name}.pos"] == pytest.approx(joint.limit.max, abs=1e-6)

    written_ticks = write_spy.call_args.args[1]
    assert _ticks_to_rad(written_ticks[joint.name]) == pytest.approx(joint.limit.max, abs=1e-3)

    hand.disconnect()


def test_xc330_m288_model_table_patch():
    _patch_xc330_m288_table()
    from lerobot.motors.dynamixel import tables as dxl_tables
    from lerobot.motors.dynamixel.dynamixel import DynamixelMotorsBus

    assert "xc330-m288" in dxl_tables.MODEL_CONTROL_TABLE
    assert dxl_tables.MODEL_RESOLUTION["xc330-m288"] == 4096
    assert "xc330-m288" in DynamixelMotorsBus.model_ctrl_table
    assert DynamixelMotorsBus.model_number_table["xc330-m288"] == 1240


def make_station(**overrides) -> ClankerStation:
    config = ClankerStationConfig(
        hand_port="/dev/mock-u2d2",
        hand_use_mock=True,
        hand_allow_uncalibrated=True,
        **overrides,
    )
    return ClankerStation(config)


def test_clanker_station_hand_only_composes_hand_prefixed_features_only():
    station = make_station()  # hand_only=True is the default

    assert station.arm is None
    assert len(station.observation_features) == 16
    assert len(station.action_features) == 16
    assert all(key.startswith("hand.") for key in station.observation_features)
    assert all(key.startswith("hand.") for key in station.action_features)
    assert not any(key.startswith("arm.") for key in station.observation_features)


def test_clanker_station_hand_only_connect_and_roundtrip():
    station = make_station()

    station.connect()
    assert station.is_connected
    assert station.is_calibrated

    obs = station.get_observation()
    assert set(obs) == set(station.observation_features)

    sent = station.send_action({"hand.index_mcp_flex.pos": 0.05})
    assert sent == {"hand.index_mcp_flex.pos": pytest.approx(0.05, abs=1e-3)}

    station.disconnect()
    assert not station.is_connected


def test_clanker_station_requires_arm_port_unless_hand_only():
    with pytest.raises(ValueError):
        ClankerStationConfig(hand_port="/dev/mock-u2d2", hand_only=False)
