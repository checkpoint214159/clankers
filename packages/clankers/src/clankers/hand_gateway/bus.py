"""Bus seam for the LEAP hand's Dynamixel bus (ADR-0002).

`HandBus` is the abstraction `GatewayService` drives. `MockBus` is a pure-python fake used by
unit tests and `--mock` runs. `LerobotDynamixelBus` wraps lerobot's `DynamixelMotorsBus`, lazily
importing lerobot/dynamixel-sdk inside `connect()` so importing this module never requires the
`hardware` extra (see CLAUDE.md: heavy deps isolated behind `clankers.hand_gateway.bus`).

All position values are radians. `servo_id` keys are raw Dynamixel IDs (0-15 on the hand bus,
provisional per robots.yaml until bring-up — see docs/plans/bringup.md).
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Any

from clankers.config import HandConfig, load_robots

from .dynamixel_compat import (
    patch_xc330_m288_table,
    rad_delta_to_ticks,
    rad_to_ticks,
    rev_per_min2_to_profile_accel,
    rev_per_min_to_profile_velocity,
    ticks_delta_to_rad,
    ticks_to_rad,
)

logger = logging.getLogger(__name__)

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

    def apply_motion_profile(self) -> dict[str, int]:
        """Write Profile_Velocity/Acceleration from robots.yaml. RAM, so no wear concern.

        Returns the register values written. Default implementation is a no-op for buses
        that do not model firmware profiles.
        """
        return {}

    @abstractmethod
    def read_torque_enabled(self, ids: list[int]) -> dict[int, bool]:
        """`{servo_id: torque_on}` — the EEPROM area is locked while torque is on."""

    @abstractmethod
    def read_homing_offsets(self, ids: list[int]) -> dict[int, int]:
        """`{servo_id: offset_ticks}` from Homing_Offset (EEPROM)."""

    @abstractmethod
    def write_homing_offsets(self, offsets: dict[int, int]) -> None:
        """Write Homing_Offset for each servo. EEPROM: wear-limited, torque must be off."""

    def set_mechanical_zero(self, ids: list[int]) -> dict[str, dict[int, int]]:
        """Make each servo's current physical position read as 0 rad.

        Dynamixels have no "set zero" command like the Damiao motors do; the equivalent is
        Homing_Offset, which is added to the raw encoder reading. To put zero here, shift the
        offset by exactly how far the servo currently reads from zero.

        Returns `{"previous": {...}, "new": {...}}` in ticks. `previous` is what makes this
        undoable -- unlike the arm's set_zero_position, which is a one-way write to flash.
        """
        previous = self.read_homing_offsets(ids)
        state = self.read_state()
        new: dict[int, int] = {}
        for sid in ids:
            reported_rad = float(state[sid]["pos"])
            new[sid] = int(previous[sid]) - rad_delta_to_ticks(reported_rad)
        self.write_homing_offsets(new)
        return {"previous": previous, "new": new}


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
        # `_pos` is the RAW encoder position; what a read reports is raw + homing offset,
        # exactly as the servo does it, so re-zeroing shifts readings the same way.
        self._homing_offset: dict[int, int] = dict.fromkeys(self._joints, 0)
        self._profile: dict[str, int] = {"velocity": 0, "acceleration": 0}

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
            reported = new_pos + ticks_delta_to_rad(self._homing_offset[sid])
            current_ma = (
                min(self._current_limit_ma, self.CURRENT_IDLE_MA + self.CURRENT_PER_RAD_S * abs(vel))
                if self._torque_enabled[sid]
                else 0.0
            )
            out[sid] = {"pos": reported, "vel": vel, "current_ma": current_ma}
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
            clamped = self._joints[sid].limit.clamp(float(rad))
            # Targets are in reported space; store raw so the lag model and the offset agree.
            self._target[sid] = clamped - ticks_delta_to_rad(self._homing_offset[sid])

    def reboot(self, servo_id: int) -> None:
        self._require_connected()
        self._check_ids([servo_id])
        # Real Dynamixel reboot clears Hardware_Error_Status and always drops torque.
        self._torque_enabled[servo_id] = False
        self._error_bits[servo_id] = 0

    def set_current_limit(self, ma: float) -> None:
        self._require_connected()
        self._current_limit_ma = float(ma)

    def apply_motion_profile(self) -> dict[str, int]:
        self._require_connected()
        profile = self._hand_cfg.profile or {}
        self._profile = {
            "velocity": rev_per_min_to_profile_velocity(profile.get("velocity_rev_per_min", 0)),
            "acceleration": rev_per_min2_to_profile_accel(
                profile.get("acceleration_rev_per_min2", 0)
            ),
        }
        return dict(self._profile)

    def read_torque_enabled(self, ids: list[int]) -> dict[int, bool]:
        self._require_connected()
        self._check_ids(ids)
        return {sid: self._torque_enabled[sid] for sid in ids}

    def read_homing_offsets(self, ids: list[int]) -> dict[int, int]:
        self._require_connected()
        self._check_ids(ids)
        return {sid: self._homing_offset[sid] for sid in ids}

    def write_homing_offsets(self, offsets: dict[int, int]) -> None:
        self._require_connected()
        self._check_ids(offsets.keys())
        # Mirrors the servo: the EEPROM area is read-only while torque is on.
        locked = sorted(sid for sid in offsets if self._torque_enabled[sid])
        if locked:
            raise RuntimeError(f"cannot write Homing_Offset while torque is on: {locked}")
        for sid, ticks in offsets.items():
            self._homing_offset[sid] = int(ticks)

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
        baudrate: int | None = None,  # None -> hand_cfg.baud (robots.yaml)
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
        baud = self._baudrate if self._baudrate is not None else self._hand_cfg.baud
        # Open the port WITHOUT lerobot's handshake. Its handshake pings every expected
        # servo at lerobot's own default baud (1M), but LEAP builds run the bus at 4M, so
        # the ping finds nothing and connect() fails before we ever get to set_baudrate.
        # Set the real baud first, then do the roll call ourselves with a better error.
        bus.connect(handshake=False)
        bus.set_baudrate(baud)
        self._bus = bus
        self._ticks_per_rev = bus.model_resolution_table[model]
        self._verify_roll_call(baud)
        self.apply_motion_profile()

    def _verify_roll_call(self, baud: int) -> None:
        """Ping every servo robots.yaml expects; fail loudly naming whoever is missing.

        Replaces lerobot's handshake (skipped in `connect()` for the baud reason above).
        Read-only: broadcast ping writes nothing and leaves torque untouched.
        """
        bus = self._require_connected()
        found = set(bus.broadcast_ping(raise_on_error=False) or {})
        missing = sorted(set(self._name_by_id) - found)
        if missing:
            # Torque was never enabled on this path, and lerobot's default disconnect writes
            # Torque_Enable to all 16 — on a bus that is already not answering that write
            # raises and would mask the far more useful roll-call error below.
            self.disconnect(disable_torque=False)
            raise ConnectionError(
                f"hand bus roll call failed at {baud} baud on {self._port}: "
                f"{len(missing)} of {len(self._name_by_id)} expected servos did not answer "
                f"(servo_ids {missing}). Checklist: hand 5V PSU on? TTL chain fully seated? "
                f"Run `uv run clankers-detect` to see what the bus actually reports."
            )

    def disconnect(self, *, disable_torque: bool = True) -> None:
        """Close the port. lerobot's disconnect WRITES Torque_Enable to every motor first,
        which is the safe default on a healthy bus but raises on a bus that is not answering
        — pass disable_torque=False when nothing was ever energized."""
        if self._bus is not None:
            try:
                self._bus.disconnect(disable_torque=disable_torque)
            finally:
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

    # A sync_read addresses all 16 servos in one transaction, so a single dropped status
    # packet fails the whole read. Retrying in the SDK is far cheaper than losing the poll.
    SYNC_READ_RETRIES = 3

    def read_state(self) -> dict[int, dict[str, float]]:
        bus = self._require_connected()
        n = self.SYNC_READ_RETRIES
        pos_ticks = bus.sync_read("Present_Position", normalize=False, num_retry=n)
        vel_raw = bus.sync_read("Present_Velocity", normalize=False, num_retry=n)
        cur_raw = bus.sync_read("Present_Current", normalize=False, num_retry=n)
        out: dict[int, dict[str, float]] = {}
        for sid, name in self._name_by_id.items():
            vel_rev_min = vel_raw[name] * self.VELOCITY_REV_PER_MIN_PER_LSB
            out[sid] = {
                "pos": self._ticks_to_rad(pos_ticks[name]),
                "vel": vel_rev_min * 2.0 * math.pi / 60.0,
                "current_ma": cur_raw[name] * self.CURRENT_MA_PER_LSB,
            }
        return out

    def apply_motion_profile(self) -> dict[str, int]:
        bus = self._require_connected()
        profile = self._hand_cfg.profile or {}
        values = {
            "Profile_Velocity": rev_per_min_to_profile_velocity(
                profile.get("velocity_rev_per_min", 0)
            ),
            "Profile_Acceleration": rev_per_min2_to_profile_accel(
                profile.get("acceleration_rev_per_min2", 0)
            ),
        }
        # The profile is a comfort setting, not a precondition for operating the hand, so a
        # servo that refuses it must not stop the gateway coming up. A latched hardware fault
        # makes a servo reject writes until it is rebooted (ADR-0004), and refusing to start
        # would leave the operator with no way to see the fault or clear it.
        failed: dict[int, str] = {}
        for reg, value in values.items():
            for sid, name in self._name_by_id.items():
                try:
                    bus.write(reg, name, int(value), normalize=False)
                except (RuntimeError, ConnectionError, OSError) as exc:
                    failed[sid] = f"{reg}: {exc}"
        if failed:
            logger.warning(
                "motion profile not applied to %d servo(s) %s — they will move at full speed "
                "toward each goal until this is fixed. A hardware error latches until the "
                "servo is rebooted; check `error_status` and use the `reboot` op. Details: %s",
                len(failed), sorted(failed), failed,
            )
        return {
            "velocity": values["Profile_Velocity"],
            "acceleration": values["Profile_Acceleration"],
            "failed_servo_ids": sorted(failed),
        }

    def read_torque_enabled(self, ids: list[int]) -> dict[int, bool]:
        bus = self._require_connected()
        raw = bus.sync_read("Torque_Enable", normalize=False)
        return {sid: bool(raw[self._name_by_id[sid]]) for sid in ids}

    def read_homing_offsets(self, ids: list[int]) -> dict[int, int]:
        bus = self._require_connected()
        raw = bus.sync_read("Homing_Offset", normalize=False)
        return {sid: int(raw[self._name_by_id[sid]]) for sid in ids}

    def write_homing_offsets(self, offsets: dict[int, int]) -> None:
        """EEPROM writes, one servo at a time (there is no sync write for EEPROM).

        Wear-limited, so callers batch a whole re-zero into one call and confirm it first
        (see CLAUDE.md); nothing here should be driven from a slider.
        """
        bus = self._require_connected()
        for sid, ticks in offsets.items():
            bus.write("Homing_Offset", self._name_by_id[sid], int(ticks), normalize=False)

    def read_health(self) -> dict[int, dict[str, float]]:
        bus = self._require_connected()
        n = self.SYNC_READ_RETRIES
        temp_raw = bus.sync_read("Present_Temperature", normalize=False, num_retry=n)
        volt_raw = bus.sync_read("Present_Input_Voltage", normalize=False, num_retry=n)
        err_raw = bus.sync_read("Hardware_Error_Status", normalize=False, num_retry=n)
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
