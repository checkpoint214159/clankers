"""LerobotDynamixelBus.connect() sequencing — the real-hardware path, faked.

The bus runs at 4M (robots.yaml `hand.baud`) while lerobot's DynamixelMotorsBus defaults
to 1M and handshake-pings every expected motor on connect. If we let that handshake run at
the default baud it finds nothing and connect() fails, so `connect()` must open the port
with handshake=False, set the real baud, and only then do its own roll call.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from clankers.config import load_robots
from clankers.hand_gateway import bus as bus_mod
from clankers.hand_gateway.bus import LerobotDynamixelBus

CFG = load_robots()


class FakeLerobotBus:
    """Records the call order connect() drives, so the test can assert on sequencing."""

    model_resolution_table: ClassVar[dict] = {CFG.hand.motor_model: 4096}
    model_number_table: ClassVar[dict] = {CFG.hand.motor_model: 1200}

    def __init__(self, port: str, motors: dict, responders: set[int] | None = None) -> None:
        self.port = port
        self.motors = motors
        self.calls: list[tuple] = []
        self.baudrate = 1_000_000  # lerobot's default, as on a fresh port
        self._responders = set(range(16)) if responders is None else responders

    def connect(self, handshake: bool = True) -> None:
        self.calls.append(("connect", handshake))

    def disconnect(self, disable_torque: bool = True) -> None:
        # lerobot writes Torque_Enable to every motor when disable_torque is True; on a bus
        # that is not answering that write raises, which is exactly what we must not do on
        # the roll-call failure path.
        self.calls.append(("disconnect", disable_torque))
        if disable_torque and self.baudrate != CFG.hand.baud:
            raise ConnectionError("Failed to write 'Torque_Enable': no status packet")

    def set_baudrate(self, baud: int) -> None:
        self.calls.append(("set_baudrate", baud))
        self.baudrate = baud

    def get_baudrate(self) -> int:
        return self.baudrate

    def broadcast_ping(self, raise_on_error: bool = False) -> dict[int, int]:
        self.calls.append(("broadcast_ping", self.baudrate))
        # Real servos only answer at their configured baud.
        if self.baudrate != CFG.hand.baud:
            return {}
        return {sid: 1200 for sid in sorted(self._responders)}


@pytest.fixture
def fake_lerobot(monkeypatch: pytest.MonkeyPatch):
    """Swap lerobot out entirely: these tests run without the `hardware` extra."""
    built: list[FakeLerobotBus] = []
    responders: set[int] = set(range(16))

    class Motor:
        def __init__(self, id: int, model: str, norm_mode: object) -> None:
            self.id, self.model, self.norm_mode = id, model, norm_mode

    class MotorNormMode:
        RANGE_M100_100 = object()

    def factory(port: str, motors: dict) -> FakeLerobotBus:
        b = FakeLerobotBus(port, motors, responders=responders)
        built.append(b)
        return b

    monkeypatch.setattr(
        LerobotDynamixelBus, "_import_lerobot", staticmethod(lambda: (Motor, MotorNormMode, factory))
    )
    monkeypatch.setattr(bus_mod, "patch_xc330_m288_table", lambda: None)
    return built, responders


def test_connect_sets_baud_before_any_ping(fake_lerobot) -> None:
    built, _ = fake_lerobot
    LerobotDynamixelBus("/dev/fake", CFG.hand).connect()

    calls = built[0].calls
    assert ("connect", False) in calls, "must skip lerobot's default-baud handshake"
    baud_at = calls.index(("set_baudrate", CFG.hand.baud))
    ping_at = next(i for i, c in enumerate(calls) if c[0] == "broadcast_ping")
    assert baud_at < ping_at, "baud must be set before the roll call, not after"


def test_connect_rolls_call_at_configured_baud(fake_lerobot) -> None:
    built, _ = fake_lerobot
    LerobotDynamixelBus("/dev/fake", CFG.hand).connect()
    assert ("broadcast_ping", CFG.hand.baud) in built[0].calls


def test_explicit_baudrate_overrides_config(fake_lerobot) -> None:
    built, _ = fake_lerobot
    bus = LerobotDynamixelBus("/dev/fake", CFG.hand, baudrate=57_600)
    with pytest.raises(ConnectionError):  # nothing answers at 57600 in the fake
        bus.connect()
    assert ("set_baudrate", 57_600) in built[0].calls


def test_missing_servos_raise_naming_them_and_disconnect(fake_lerobot) -> None:
    built, responders = fake_lerobot
    responders.difference_update({3, 11})

    with pytest.raises(ConnectionError) as exc:
        LerobotDynamixelBus("/dev/fake", CFG.hand).connect()

    msg = str(exc.value)
    assert "[3, 11]" in msg and "2 of 16" in msg
    assert "clankers-detect" in msg, "error should point at the diagnostic tool"
    assert ("disconnect", False) in built[0].calls, (
        "a failed roll call must close the port WITHOUT the torque write"
    )


def test_failed_roll_call_error_survives_a_dead_bus(fake_lerobot) -> None:
    """The roll-call error must reach the operator even when the bus is too broken to
    accept lerobot's torque-off write — otherwise a ConnectionError about Torque_Enable
    masks the real diagnosis."""
    _built, responders = fake_lerobot
    responders.clear()  # nothing answers at all

    with pytest.raises(ConnectionError) as exc:
        LerobotDynamixelBus("/dev/fake", CFG.hand).connect()

    assert "roll call failed" in str(exc.value)
    assert "Torque_Enable" not in str(exc.value)
