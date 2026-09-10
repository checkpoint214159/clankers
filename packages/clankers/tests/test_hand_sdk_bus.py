"""Tests for the raw-SDK controller bus (ADR-0005).

`DynamixelBus` and `LerobotDynamixelBus` are two implementations of one ABC driving the
same hardware, so the danger is not that one crashes but that they quietly *disagree* --
about where zero is, or what a current LSB means. These tests pin the wire format against
a fake register file and pin the shared constants against the lerobot implementation.

No hardware and no lerobot: the fake SDK below is a plain register file, so this runs on
the light test path.
"""

from __future__ import annotations

import math

import pytest
from clankers.config import load_robots
from clankers.hand_gateway.bus import LerobotDynamixelBus
from clankers.hand_gateway.dynamixel_compat import (
    CENTER_TICK,
    CONTROL_TABLE,
    decode_signed,
    encode_signed,
)
from clankers.hand_gateway.sdk_bus import DynamixelBus

CFG = load_robots()
SERVO_IDS = [j.servo_id for j in CFG.hand.joints]
COMM_SUCCESS = 0
REG_FILE_SIZE = 200


class FakeBusState:
    """A register file per servo, plus a set of servos that refuse to answer."""

    def __init__(self, ids: list[int]) -> None:
        self.regs = {sid: bytearray(REG_FILE_SIZE) for sid in ids}
        self.silent: set[int] = set()
        self.rebooted: list[int] = []
        self.model_number = 1240

    def put(self, sid: int, addr: int, size: int, value: int) -> None:
        raw = encode_signed(value, size)
        for i in range(size):
            self.regs[sid][addr + i] = (raw >> (8 * i)) & 0xFF

    def get(self, sid: int, addr: int, size: int) -> int:
        return sum(self.regs[sid][addr + i] << (8 * i) for i in range(size))


def _fake_sdk(state: FakeBusState) -> tuple:
    class FakePortHandler:
        def __init__(self, device: str) -> None:
            self.device, self.baud, self.open = device, 0, False

        def openPort(self) -> bool:
            self.open = True
            return True

        def closePort(self) -> None:
            self.open = False

        def setBaudRate(self, baud: int) -> bool:
            self.baud = baud
            return True

        def getBaudRate(self) -> int:
            return self.baud

    class FakePacketHandler:
        def __init__(self, protocol: float) -> None:
            self.protocol = protocol

        def broadcastPing(self, port):
            answers = {s: [state.model_number, 1] for s in state.regs if s not in state.silent}
            return answers, COMM_SUCCESS

        def reboot(self, port, sid):
            state.rebooted.append(sid)
            return COMM_SUCCESS, 0

        def _write(self, port, sid, addr, value, size):
            if sid in state.silent:
                return -1000, 0
            state.put(sid, addr, size, decode_signed(value, size))
            return COMM_SUCCESS, 0

        def write1ByteTxRx(self, port, sid, addr, value):
            return self._write(port, sid, addr, value, 1)

        def write2ByteTxRx(self, port, sid, addr, value):
            return self._write(port, sid, addr, value, 2)

        def write4ByteTxRx(self, port, sid, addr, value):
            return self._write(port, sid, addr, value, 4)

        def getTxRxResult(self, comm):
            return f"comm={comm}"

        def getRxPacketError(self, err):
            return f"err={err}"

    class FakeGroupSyncRead:
        def __init__(self, port, packet, addr, length) -> None:
            self.addr, self.length, self.ids, self.data = addr, length, [], {}

        def addParam(self, sid) -> bool:
            self.ids.append(sid)
            return True

        def txRxPacket(self):
            self.data = {
                sid: bytes(state.regs[sid][self.addr : self.addr + self.length])
                for sid in self.ids
                if sid not in state.silent
            }
            return COMM_SUCCESS

        def isAvailable(self, sid, addr, size) -> bool:
            return sid in self.data and self.addr <= addr and addr + size <= self.addr + self.length

        def getData(self, sid, addr, size) -> int:
            blob = self.data[sid][addr - self.addr : addr - self.addr + size]
            return sum(b << (8 * i) for i, b in enumerate(blob))

    class FakeGroupSyncWrite:
        def __init__(self, port, packet, addr, length) -> None:
            self.addr, self.length, self.pending = addr, length, {}

        def addParam(self, sid, data) -> bool:
            self.pending[sid] = list(data)
            return True

        def txPacket(self):
            for sid, data in self.pending.items():
                if sid in state.silent:
                    return -1000
                for i, byte in enumerate(data):
                    state.regs[sid][self.addr + i] = byte
            return COMM_SUCCESS

        def clearParam(self) -> None:
            self.pending = {}

    return COMM_SUCCESS, FakeGroupSyncRead, FakeGroupSyncWrite, FakePacketHandler, FakePortHandler


@pytest.fixture
def state() -> FakeBusState:
    st = FakeBusState(SERVO_IDS)
    for sid in SERVO_IDS:
        st.put(sid, CONTROL_TABLE["Present_Position"].addr, 4, CENTER_TICK)
        st.put(sid, CONTROL_TABLE["Present_Input_Voltage"].addr, 2, 118)
        st.put(sid, CONTROL_TABLE["Present_Temperature"].addr, 1, 31)
    return st


@pytest.fixture
def bus(state: FakeBusState, monkeypatch: pytest.MonkeyPatch) -> DynamixelBus:
    monkeypatch.setattr(DynamixelBus, "_import_sdk", staticmethod(lambda: _fake_sdk(state)))
    b = DynamixelBus("/dev/fake", CFG.hand)
    b.connect()
    return b


def test_control_table_block_reads_stay_contiguous() -> None:
    """read_state and read_health each fold several registers into ONE sync_read.

    That is only correct while the registers really are adjacent; if a table edit ever
    breaks the adjacency the reads would silently slice the wrong bytes.
    """
    cur, vel, pos = (CONTROL_TABLE[n] for n in ("Present_Current", "Present_Velocity", "Present_Position"))
    assert cur.addr + cur.size == vel.addr
    assert vel.addr + vel.size == pos.addr
    volt, temp = CONTROL_TABLE["Present_Input_Voltage"], CONTROL_TABLE["Present_Temperature"]
    assert volt.addr + volt.size == temp.addr


def test_eeprom_registers_are_flagged() -> None:
    assert CONTROL_TABLE["Homing_Offset"].eeprom and CONTROL_TABLE["Current_Limit"].eeprom
    assert not CONTROL_TABLE["Goal_Position"].eeprom
    assert not CONTROL_TABLE["Torque_Enable"].eeprom


@pytest.mark.parametrize(
    ("value", "size"),
    [
        (0, 1), (1, 1), (-1, 1), (127, 1), (-128, 1),
        (0, 2), (2047, 2), (-2048, 2), (32767, 2), (-32768, 2),
        (0, 4), (1_048_575, 4), (-1_048_576, 4), (2**31 - 1, 4), (-(2**31), 4),
    ],
)
def test_signed_encoding_round_trips(value: int, size: int) -> None:
    assert decode_signed(encode_signed(value, size), size) == value


@pytest.mark.parametrize(("value", "size"), [(256, 1), (-129, 1), (65536, 2), (-32769, 2)])
def test_out_of_range_values_are_refused_not_silently_wrapped(value: int, size: int) -> None:
    """Byte-masking would turn an over-range write into a different, valid-looking command."""
    with pytest.raises(ValueError, match="does not fit"):
        encode_signed(value, size)


@pytest.mark.parametrize(("value", "size"), [(255, 1), (128, 1), (65535, 2), (4095, 2)])
def test_unsigned_registers_may_use_the_full_width(value: int, size: int) -> None:
    """The same encoder serves unsigned registers (Current_Limit, Torque_Enable), so the
    accepted range is the union of the signed and unsigned spans, not the signed one."""
    assert encode_signed(value, size) == value


def test_the_two_bus_implementations_agree_on_lsb_units() -> None:
    """A disagreement here would make the same hardware report two different currents."""
    for const in ("VELOCITY_REV_PER_MIN_PER_LSB", "CURRENT_MA_PER_LSB", "VOLTAGE_V_PER_LSB"):
        assert getattr(DynamixelBus, const) == getattr(LerobotDynamixelBus, const), const


def test_dynamixel_bus_implements_the_whole_abc() -> None:
    assert not getattr(DynamixelBus, "__abstractmethods__", set())


def test_connect_sets_the_configured_baud(bus: DynamixelBus) -> None:
    assert bus._port_handler.baud == CFG.hand.baud


def test_roll_call_names_the_servos_that_did_not_answer(
    state: FakeBusState, monkeypatch: pytest.MonkeyPatch
) -> None:
    state.silent = {14, 15}
    monkeypatch.setattr(DynamixelBus, "_import_sdk", staticmethod(lambda: _fake_sdk(state)))
    b = DynamixelBus("/dev/fake", CFG.hand)
    with pytest.raises(ConnectionError) as exc:
        b.connect()
    assert "14" in str(exc.value) and "15" in str(exc.value)
    assert "clankers-detect" in str(exc.value)


def test_centre_tick_reads_as_zero_radians(bus: DynamixelBus) -> None:
    """Half-turn homing: tick 2048 is 0 rad, the convention the LeRobot path also uses."""
    assert all(s["pos"] == pytest.approx(0.0) for s in bus.read_state().values())


def test_write_positions_round_trips_through_the_register_file(
    bus: DynamixelBus, state: FakeBusState
) -> None:
    targets = {sid: 0.25 * (1 if sid % 2 else -1) for sid in SERVO_IDS}
    bus.write_positions(targets)
    goal = CONTROL_TABLE["Goal_Position"]
    for sid, rad in targets.items():
        # copy the goal into the present-position register, as a real servo eventually would
        state.put(sid, CONTROL_TABLE["Present_Position"].addr, 4, state.get(sid, goal.addr, 4))
        assert bus.read_state()[sid]["pos"] == pytest.approx(rad, abs=2e-3)


def test_negative_current_survives_the_wire(bus: DynamixelBus, state: FakeBusState) -> None:
    """Present_Current is signed: a regen/backdrive reading must not become +65 A."""
    state.put(SERVO_IDS[0], CONTROL_TABLE["Present_Current"].addr, 2, -120)
    assert bus.read_state()[SERVO_IDS[0]]["current_ma"] == pytest.approx(-120.0)


def test_velocity_is_reported_in_radians_per_second(bus: DynamixelBus, state: FakeBusState) -> None:
    state.put(SERVO_IDS[0], CONTROL_TABLE["Present_Velocity"].addr, 4, 100)
    expected = 100 * DynamixelBus.VELOCITY_REV_PER_MIN_PER_LSB * 2.0 * math.pi / 60.0
    assert bus.read_state()[SERVO_IDS[0]]["vel"] == pytest.approx(expected)


def test_read_health_decodes_voltage_temperature_and_error_bits(
    bus: DynamixelBus, state: FakeBusState
) -> None:
    state.put(SERVO_IDS[3], CONTROL_TABLE["Hardware_Error_Status"].addr, 1, 0b100)
    health = bus.read_health()
    assert health[SERVO_IDS[0]]["voltage_v"] == pytest.approx(11.8)
    assert health[SERVO_IDS[0]]["temp_c"] == pytest.approx(31.0)
    assert health[SERVO_IDS[3]]["error_bits"] == 0b100


def test_a_single_dropout_fails_the_read_and_names_the_servo(
    bus: DynamixelBus, state: FakeBusState
) -> None:
    """The known bring-up blocker: one silent servo must not look like healthy telemetry."""
    state.silent = {15}
    with pytest.raises(ConnectionError) as exc:
        bus.read_state()
    assert "15" in str(exc.value)
    assert "bringup" in str(exc.value)


def test_torque_writes_land_on_the_right_register(bus: DynamixelBus, state: FakeBusState) -> None:
    addr = CONTROL_TABLE["Torque_Enable"].addr
    bus.enable_torque(SERVO_IDS)
    assert bus.read_torque_enabled(SERVO_IDS) == dict.fromkeys(SERVO_IDS, True)
    assert state.get(SERVO_IDS[0], addr, 1) == 1
    bus.disable_torque(SERVO_IDS)
    assert bus.read_torque_enabled(SERVO_IDS) == dict.fromkeys(SERVO_IDS, False)


def test_current_limit_round_trips(bus: DynamixelBus) -> None:
    bus.set_current_limit(300.0)
    assert bus.read_current_limits() == dict.fromkeys(SERVO_IDS, 300.0)


def test_negative_homing_offsets_round_trip(bus: DynamixelBus) -> None:
    offsets = {sid: -40 * (sid + 1) for sid in SERVO_IDS}
    bus.write_homing_offsets(offsets)
    assert bus.read_homing_offsets(SERVO_IDS) == offsets


def test_set_mechanical_zero_is_undoable(bus: DynamixelBus, state: FakeBusState) -> None:
    """The ABC's shared implementation must work over this bus too, offsets included."""
    for sid in SERVO_IDS:
        state.put(sid, CONTROL_TABLE["Present_Position"].addr, 4, CENTER_TICK + 100)
    result = bus.set_mechanical_zero(SERVO_IDS)
    assert result["previous"] == dict.fromkeys(SERVO_IDS, 0)
    assert all(v == -100 for v in result["new"].values())
    bus.write_homing_offsets(result["previous"])
    assert bus.read_homing_offsets(SERVO_IDS) == result["previous"]


def test_motion_profile_is_applied_from_robots_yaml(bus: DynamixelBus, state: FakeBusState) -> None:
    applied = bus.apply_motion_profile()
    assert applied["failed_servo_ids"] == []
    assert state.get(SERVO_IDS[0], CONTROL_TABLE["Profile_Velocity"].addr, 4) == applied["velocity"]
    assert (
        state.get(SERVO_IDS[0], CONTROL_TABLE["Profile_Acceleration"].addr, 4)
        == applied["acceleration"]
    )


def test_one_servo_refusing_the_profile_does_not_stop_the_gateway(
    bus: DynamixelBus, state: FakeBusState
) -> None:
    """A latched fault makes a servo reject writes; the operator still needs the gateway up."""
    state.silent = {7}
    applied = bus.apply_motion_profile()
    assert applied["failed_servo_ids"] == [7]


def test_scan_names_the_hands_own_servo_model_without_lerobot(bus: DynamixelBus) -> None:
    found = bus.scan()
    assert len(found) == len(SERVO_IDS)
    assert {row["model"] for row in found} == {"xc330-m288"}


def test_reboot_issues_the_instruction(bus: DynamixelBus, state: FakeBusState) -> None:
    bus.reboot(9)
    assert state.rebooted == [9]


def test_disconnect_cuts_torque_then_closes(bus: DynamixelBus, state: FakeBusState) -> None:
    bus.enable_torque(SERVO_IDS)
    handler = bus._port_handler
    bus.disconnect()
    assert state.get(SERVO_IDS[0], CONTROL_TABLE["Torque_Enable"].addr, 1) == 0
    assert not handler.open


def test_disconnect_still_releases_the_port_when_torque_off_fails(
    bus: DynamixelBus, state: FakeBusState
) -> None:
    """Leaving the port held would lock the bus for every later process (ADR-0002)."""
    handler = bus._port_handler
    state.silent = set(SERVO_IDS)
    bus.disconnect()
    assert not handler.open
    assert bus._port_handler is None
