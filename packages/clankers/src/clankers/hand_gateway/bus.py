"""Bus seam for the LEAP hand's Dynamixel bus (ADR-0002).

`HandBus` is the abstraction `GatewayService` drives. `MockBus` is a pure-python fake used by
unit tests and `--mock` runs. `LerobotDynamixelBus` wraps lerobot's `DynamixelMotorsBus`, lazily
importing lerobot/dynamixel-sdk inside `connect()` so importing this module never requires the
`hardware` extra (see CLAUDE.md: heavy deps isolated behind `clankers.hand_gateway.bus`).

All position values are radians. `servo_id` keys are raw Dynamixel IDs (0-15 on the hand bus,
provisional per robots.yaml until bring-up — see docs/plans/bringup.md).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

from clankers.config import HandConfig, load_robots

from .dynamixel_compat import patch_xc330_m288_table, rad_to_ticks, ticks_to_rad

DEFAULT_SCAN_BAUD = 4_000_000  # LEAP builds default; confirmed at scan per robots.yaml comment


class HandBus(ABC):
    """One physical LEAP hand bus connection (glossary: `bus`)."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def scan(self, baud: int | None = None) -> list[dict[str, Any]]:
        """Probe the bus. Returns `[{servo_id, model, baud}, ...]` for every servo found."""

    @abstractmethod
    def enable_torque(self, ids: list[int]) -> None: ...

    @abstractmethod
    def disable_torque(self, ids: list[int]) -> None: ...

    @abstractmethod
    def read_state(self) -> dict[int, dict[str, float]]:
        """Returns `{servo_id: {pos: rad, vel: rad_s, current_ma: float}}` for every servo."""

    @abstractmethod
    def read_health(self) -> dict[int, dict[str, float]]:
        """Returns `{servo_id: {temp_c, voltage_v, error_bits: int}}` for every servo."""

    @abstractmethod
    def write_positions(self, targets: dict[int, float]) -> None:
        """`targets` is `{servo_id: rad}`, already clamped upstream by SafetyClamp."""

    @abstractmethod
    def reboot(self, servo_id: int) -> None: ...

    @abstractmethod
    def set_current_limit(self, ma: float) -> None: ...


class MockBus(HandBus):
    """Pure-python fake of the 16-servo LEAP hand bus, seeded from robots.yaml.

    Movement is simulated as first-order lag toward the last written target, advanced by a
    fixed fraction *per `read_state()` call* (not wall-clock time) so tests are deterministic
    and fast regardless of how quickly they run. A disabled (torque-off) servo does not move.
    """

    LAG_ALPHA = 0.35  # fraction of remaining position error closed per read_state() call
    NOMINAL_DT_S = 0.02  # only used to report a plausible velocity; not real elapsed time
    CURRENT_IDLE_MA = 5.0
    CURRENT_PER_RAD_S = 40.0

    def __init__(self, hand_cfg: HandConfig | None = None) -> None:
        self._hand_cfg = hand_cfg or load_robots().hand
        self._joints = {j.servo_id: j for j in self._hand_cfg.joints}
        self._connected = False
        self._torque_enabled: dict[int, bool] = dict.fromkeys(self._joints, False)
        self._pos: dict[int, float] = {sid: j.limit.clamp(0.0) for sid, j in self._joints.items()}
        self._target: dict[int, float] = dict(self._pos)
        self._current_limit_ma = float(self._hand_cfg.current_limit_ma)
        self._error_bits: dict[int, int] = dict.fromkeys(self._joints, 0)
        self._temp_c: dict[int, float] = dict.fromkeys(self._joints, 25.0)
        self._voltage_v: dict[int, float] = dict.fromkeys(self._joints, 5.0)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MockBus is not connected; call connect() first")

    def _check_ids(self, ids: Any) -> None:
        unknown = sorted(set(ids) - set(self._joints))
        if unknown:
            raise KeyError(f"unknown servo_id(s): {unknown}")

    def scan(self, baud: int | None = None) -> list[dict[str, Any]]:
        self._require_connected()
        used_baud = baud or DEFAULT_SCAN_BAUD
        return [
            {"servo_id": sid, "model": self._hand_cfg.motor_model, "baud": used_baud}
            for sid in sorted(self._joints)
        ]

    def enable_torque(self, ids: list[int]) -> None:
        self._require_connected()
        self._check_ids(ids)
        for sid in ids:
            self._torque_enabled[sid] = True

    def disable_torque(self, ids: list[int]) -> None:
        self._require_connected()
        self._check_ids(ids)
        for sid in ids:
            self._torque_enabled[sid] = False

    def read_state(self) -> dict[int, dict[str, float]]:
        self._require_connected()
        out: dict[int, dict[str, float]] = {}
        for sid, joint in self._joints.items():
            prev = self._pos[sid]
            if self._torque_enabled[sid]:
                target = joint.limit.clamp(self._target[sid])
                new_pos = prev + self.LAG_ALPHA * (target - prev)
            else:
                new_pos = prev
            self._pos[sid] = new_pos
            vel = (new_pos - prev) / self.NOMINAL_DT_S
            current_ma = (
                min(self._current_limit_ma, self.CURRENT_IDLE_MA + self.CURRENT_PER_RAD_S * abs(vel))
                if self._torque_enabled[sid]
                else 0.0
            )
            out[sid] = {"pos": new_pos, "vel": vel, "current_ma": current_ma}
        return out

    def read_health(self) -> dict[int, dict[str, float]]:
        self._require_connected()
        return {
            sid: {
                "temp_c": self._temp_c[sid],
                "voltage_v": self._voltage_v[sid],
                "error_bits": self._error_bits[sid],
            }
            for sid in self._joints
        }

    def write_positions(self, targets: dict[int, float]) -> None:
        self._require_connected()
        self._check_ids(targets.keys())
        for sid, rad in targets.items():
            self._target[sid] = self._joints[sid].limit.clamp(float(rad))

    def reboot(self, servo_id: int) -> None:
        self._require_connected()
        self._check_ids([servo_id])
        # Real Dynamixel reboot clears Hardware_Error_Status and always drops torque.
        self._torque_enabled[servo_id] = False
        self._error_bits[servo_id] = 0

    def set_current_limit(self, ma: float) -> None:
        self._require_connected()
        self._current_limit_ma = float(ma)

    def inject_fault(self, servo_id: int, error_bits: int) -> None:
        """Test hook: set the raw addr-70-style Hardware_Error_Status bitmask for a servo."""
        self._check_ids([servo_id])
        self._error_bits[servo_id] = error_bits


class LerobotDynamixelBus(HandBus):
    """Thin wrapper over lerobot's `DynamixelMotorsBus` (ADR-0001/0002 bus dedup).

    TODO(bringup): not yet hardware-validated (docs/plans/bringup.md). Position conversion
    uses the shared half-turn-homing convention from `dynamixel_compat` (tick 2048 == 0 rad,
    same as the LeRobot runtime path) — per-joint sign/offset is exactly what bring-up step 1
    determines; revisit once servo_id<->joint<->sign is confirmed and `hand.calibrated: true`.
    Velocity/current/voltage LSB units below are the standard X-series constants; double-check
    against the XC330-M288 datasheet once revision is confirmed (robots.yaml `hand.revision`).
    `connect()` applies the shared local `xc330-m288` table patch (ADR-0001).
    """

    VELOCITY_REV_PER_MIN_PER_LSB = 0.229
    CURRENT_MA_PER_LSB = 1.0
    VOLTAGE_V_PER_LSB = 0.1

    def __init__(
        self,
        port: str,
        hand_cfg: HandConfig | None = None,
        baudrate: int | None = None,
    ) -> None:
        self._port = port
        self._hand_cfg = hand_cfg or load_robots().hand
        self._baudrate = baudrate
        self._bus: Any = None
        self._name_by_id: dict[int, str] = {j.servo_id: j.name for j in self._hand_cfg.joints}
        self._ticks_per_rev: int | None = None

    @staticmethod
    def _import_lerobot() -> tuple[Any, Any, Any]:
        try:
            from lerobot.motors import Motor, MotorNormMode
            from lerobot.motors.dynamixel import DynamixelMotorsBus
        except ImportError as exc:
            raise ImportError(
                "LerobotDynamixelBus requires the 'hardware' extra (lerobot + dynamixel-sdk). "
                "Run `uv sync --extra hardware`."
            ) from exc
        return Motor, MotorNormMode, DynamixelMotorsBus

    def connect(self) -> None:
        Motor, MotorNormMode, DynamixelMotorsBus = self._import_lerobot()
        # lerobot 0.6 has no xc330-m288 table entry; shared local patch (ADR-0001).
        patch_xc330_m288_table()
        model = self._hand_cfg.motor_model
        motors = {
            joint.name: Motor(id=joint.servo_id, model=model, norm_mode=MotorNormMode.RANGE_M100_100)
            for joint in self._hand_cfg.joints
        }
        bus = DynamixelMotorsBus(port=self._port, motors=motors)
        bus.connect()
        if self._baudrate is not None:
            bus.set_baudrate(self._baudrate)
        self._bus = bus
        self._ticks_per_rev = bus.model_resolution_table[model]

    def disconnect(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    def _require_connected(self) -> Any:
        if self._bus is None:
            raise RuntimeError("LerobotDynamixelBus is not connected; call connect() first")
        return self._bus

    def _ticks_to_rad(self, ticks: int) -> float:
        # Half-turn-homing convention (tick 2048 == 0 rad), shared with the LeRobot
        # runtime path via dynamixel_compat — zero-at-tick-0 math would turn every
        # negative joint limit into a negative raw Goal_Position, which two's-complement
        # wraps into a huge out-of-range command on the wire.
        return ticks_to_rad(ticks)

    def _rad_to_ticks(self, rad: float) -> int:
        return rad_to_ticks(rad)

    def scan(self, baud: int | None = None) -> list[dict[str, Any]]:
        bus = self._require_connected()
        if baud is not None:
            bus.set_baudrate(baud)
        found = bus.broadcast_ping(raise_on_error=False) or {}
        used_baud = baud or bus.get_baudrate()
        model_by_number = {v: k for k, v in bus.model_number_table.items()}
        return [
            {"servo_id": sid, "model": model_by_number.get(model_nb, f"unknown(0x{model_nb:x})"), "baud": used_baud}
            for sid, model_nb in sorted(found.items())
        ]

    def enable_torque(self, ids: list[int]) -> None:
        bus = self._require_connected()
        bus.enable_torque([self._name_by_id[sid] for sid in ids])

    def disable_torque(self, ids: list[int]) -> None:
        bus = self._require_connected()
        bus.disable_torque([self._name_by_id[sid] for sid in ids])

    def read_state(self) -> dict[int, dict[str, float]]:
        bus = self._require_connected()
        pos_ticks = bus.sync_read("Present_Position", normalize=False)
        vel_raw = bus.sync_read("Present_Velocity", normalize=False)
        cur_raw = bus.sync_read("Present_Current", normalize=False)
        out: dict[int, dict[str, float]] = {}
        for sid, name in self._name_by_id.items():
            vel_rev_min = vel_raw[name] * self.VELOCITY_REV_PER_MIN_PER_LSB
            out[sid] = {
                "pos": self._ticks_to_rad(pos_ticks[name]),
                "vel": vel_rev_min * 2.0 * math.pi / 60.0,
                "current_ma": cur_raw[name] * self.CURRENT_MA_PER_LSB,
            }
        return out

    def read_health(self) -> dict[int, dict[str, float]]:
        bus = self._require_connected()
        temp_raw = bus.sync_read("Present_Temperature", normalize=False)
        volt_raw = bus.sync_read("Present_Input_Voltage", normalize=False)
        err_raw = bus.sync_read("Hardware_Error_Status", normalize=False)
        return {
            sid: {
                "temp_c": float(temp_raw[name]),
                "voltage_v": volt_raw[name] * self.VOLTAGE_V_PER_LSB,
                "error_bits": int(err_raw[name]),
            }
            for sid, name in self._name_by_id.items()
        }

    def write_positions(self, targets: dict[int, float]) -> None:
        bus = self._require_connected()
        bus.sync_write(
            "Goal_Position",
            {self._name_by_id[sid]: self._rad_to_ticks(rad) for sid, rad in targets.items()},
            normalize=False,
        )

    def reboot(self, servo_id: int) -> None:
        bus = self._require_connected()
        # SerialMotorsBus has no high-level reboot(); issue the protocol REBOOT instruction
        # directly via the packet handler (mirrors ping()/broadcast_ping() call shape).
        bus.packet_handler.reboot(bus.port_handler, servo_id)

    def set_current_limit(self, ma: float) -> None:
        bus = self._require_connected()
        ticks = round(ma / self.CURRENT_MA_PER_LSB)
        bus.sync_write("Current_Limit", dict.fromkeys(self._name_by_id.values(), ticks), normalize=False)
