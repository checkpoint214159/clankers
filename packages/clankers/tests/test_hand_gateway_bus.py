"""MockBus unit tests: determinism, torque gating, limits, fault injection."""

from __future__ import annotations

import pytest
from clankers.config import load_robots
from clankers.hand_gateway.bus import MockBus

CFG = load_robots()


def _connected_bus() -> MockBus:
    bus = MockBus(CFG.hand)
    bus.connect()
    return bus


def test_scan_returns_all_16_configured_servos() -> None:
    bus = _connected_bus()
    servos = bus.scan()
    assert sorted(s["servo_id"] for s in servos) == list(range(16))
    assert all(s["model"] == CFG.hand.motor_model for s in servos)


def test_scan_reports_requested_baud() -> None:
    bus = _connected_bus()
    servos = bus.scan(baud=1_000_000)
    assert all(s["baud"] == 1_000_000 for s in servos)


def test_ops_before_connect_raise() -> None:
    bus = MockBus(CFG.hand)
    with pytest.raises(RuntimeError):
        bus.scan()
    with pytest.raises(RuntimeError):
        bus.read_state()


def test_unknown_servo_id_raises_keyerror() -> None:
    bus = _connected_bus()
    with pytest.raises(KeyError):
        bus.enable_torque([99])
    with pytest.raises(KeyError):
        bus.write_positions({99: 0.0})
    with pytest.raises(KeyError):
        bus.reboot(99)


def test_positions_do_not_move_while_torque_disabled() -> None:
    bus = _connected_bus()
    bus.write_positions({0: 1.0})
    before = bus.read_state()[0]["pos"]
    for _ in range(10):
        bus.read_state()
    after = bus.read_state()[0]["pos"]
    assert before == after == pytest.approx(0.0)


def test_positions_converge_toward_target_once_enabled() -> None:
    bus = _connected_bus()
    joint = CFG.hand.joints[0]
    target = joint.limit.max * 0.5
    bus.enable_torque([joint.servo_id])
    bus.write_positions({joint.servo_id: target})
    last = bus.read_state()[joint.servo_id]["pos"]
    for _ in range(200):
        pos = bus.read_state()[joint.servo_id]["pos"]
        assert abs(pos - target) <= abs(last - target) + 1e-12  # monotonically closes the gap
        last = pos
    assert last == pytest.approx(target, abs=1e-6)


def test_read_state_is_deterministic_given_the_same_call_sequence() -> None:
    joint = CFG.hand.joints[3]

    def run() -> list[float]:
        bus = _connected_bus()
        bus.enable_torque([joint.servo_id])
        bus.write_positions({joint.servo_id: joint.limit.max})
        return [bus.read_state()[joint.servo_id]["pos"] for _ in range(15)]

    assert run() == run()


def test_disabled_servo_reports_zero_current() -> None:
    bus = _connected_bus()
    bus.write_positions({0: 1.0})
    state = bus.read_state()[0]
    assert state["current_ma"] == 0.0


def test_current_never_exceeds_configured_limit() -> None:
    bus = _connected_bus()
    joint = CFG.hand.joints[0]
    bus.set_current_limit(10.0)
    bus.enable_torque([joint.servo_id])
    bus.write_positions({joint.servo_id: joint.limit.max})
    for _ in range(5):
        state = bus.read_state()[joint.servo_id]
        assert state["current_ma"] <= 10.0


def test_write_positions_clamps_to_joint_limit() -> None:
    bus = _connected_bus()
    joint = CFG.hand.joints[0]
    bus.enable_torque([joint.servo_id])
    bus.write_positions({joint.servo_id: joint.limit.max + 10.0})
    for _ in range(500):
        pos = bus.read_state()[joint.servo_id]["pos"]
    assert pos == pytest.approx(joint.limit.max, abs=1e-6)


def test_inject_fault_is_reflected_in_read_health() -> None:
    bus = _connected_bus()
    bus.inject_fault(2, 0b100001)
    health = bus.read_health()
    assert health[2]["error_bits"] == 0b100001
    assert health[0]["error_bits"] == 0


def test_reboot_clears_fault_and_drops_torque() -> None:
    bus = _connected_bus()
    bus.inject_fault(1, 1)
    bus.enable_torque([1])
    bus.reboot(1)
    assert bus.read_health()[1]["error_bits"] == 0
    bus.write_positions({1: CFG.hand.joints[1].limit.max})
    pos_before = bus.read_state()[1]["pos"]
    pos_after = bus.read_state()[1]["pos"]
    assert pos_before == pos_after  # torque dropped by reboot: no motion
