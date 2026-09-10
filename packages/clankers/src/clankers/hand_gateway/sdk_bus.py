"""`DynamixelBus`: the 16-servo LEAP hand over raw dynamixel-sdk -- the CONTROLLER's bus.

ADR-0005 splits the stack in two. The Pi is the *controller*: it owns the serial buses and
serves the WS envelope, and installs `--extra controller` (dynamixel-sdk, ~1 MB). The
*brain* -- teleop, policies, training -- runs elsewhere and reaches the hardware only over
WS. `LerobotDynamixelBus` in `bus.py` does the same job through lerobot, which drags in
torch and the CUDA runtime; that is fine on a workstation and absurd on a Pi, so this
module speaks protocol 2.0 directly instead.

Both implementations are the same `HandBus` ABC and MUST agree on behaviour: identical
half-turn homing (tick 2048 == 0 rad), identical LSB units, identical failure semantics.
Everything they share lives in `dynamixel_compat` -- including the control table, so
neither can drift into its own register vocabulary (docs/glossary.md).

Telemetry is read in as few transactions as possible. `sync_read` addresses all 16 servos
in one packet, so a single dropout fails the entire read, and this hand has a known flaky
14/15 chain (docs/plans/bringup.md). Reads retry, and when they still fail they name the
servos that stayed silent rather than reporting a generic timeout.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from clankers.config import HandConfig, load_robots

from .bus import HandBus
from .dynamixel_compat import (
    CONTROL_TABLE,
    MODEL_NAMES,
    Register,
    decode_signed,
    encode_signed,
    rad_to_ticks,
    rev_per_min2_to_profile_accel,
    rev_per_min_to_profile_velocity,
    ticks_to_rad,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 2.0


class DynamixelBus(HandBus):
    """The LEAP hand bus with no lerobot and no torch.

    TODO(bringup): not yet hardware-validated (docs/plans/bringup.md), same caveat as
    `LerobotDynamixelBus` -- per-joint sign/offset is what bring-up step 1 determines.
    LSB units below are the standard X-series constants; confirm against the XC330-M288
    datasheet once `hand.revision` is known.
    """

    VELOCITY_REV_PER_MIN_PER_LSB = 0.229
    CURRENT_MA_PER_LSB = 1.0
    VOLTAGE_V_PER_LSB = 0.1
    SYNC_READ_RETRIES = 3

    def __init__(
        self,
        port: str,
        hand_cfg: HandConfig | None = None,
        baudrate: int | None = None,  # None -> hand_cfg.baud (robots.yaml)
    ) -> None:
        self._port = port
        self._hand_cfg = hand_cfg or load_robots().hand
        self._baudrate = baudrate
        self._name_by_id: dict[int, str] = {j.servo_id: j.name for j in self._hand_cfg.joints}
        self._port_handler: Any = None
        self._packet: Any = None
        self._readers: dict[tuple[int, int], Any] = {}

    # ---------------------------------------------------------------- lifecycle

    @staticmethod
    def _import_sdk() -> tuple[Any, ...]:
        try:
            from dynamixel_sdk import (
                COMM_SUCCESS,
                GroupSyncRead,
                GroupSyncWrite,
                PacketHandler,
                PortHandler,
            )
        except ImportError as exc:
            raise ImportError(
                "DynamixelBus requires the 'controller' extra (dynamixel-sdk). "
                "Run `uv sync --extra controller`."
            ) from exc
        return COMM_SUCCESS, GroupSyncRead, GroupSyncWrite, PacketHandler, PortHandler

    def connect(self) -> None:
        _, _, _, PacketHandler, PortHandler = self._import_sdk()
        handler = PortHandler(self._port)
        if not handler.openPort():
            raise ConnectionError(
                f"cannot open {self._port} — is another process (a gateway?) holding it? "
                "One process per bus (ADR-0002)."
            )
        baud = self._baudrate if self._baudrate is not None else self._hand_cfg.baud
        if not handler.setBaudRate(baud):
            handler.closePort()
            raise ConnectionError(f"{self._port} rejected baud {baud}")
        self._port_handler = handler
        self._packet = PacketHandler(PROTOCOL_VERSION)
        self._readers.clear()
        self._verify_roll_call(baud)
        self.apply_motion_profile()

    def _verify_roll_call(self, baud: int) -> None:
        """Ping every servo robots.yaml expects; fail loudly naming whoever is missing.

        Read-only: a broadcast ping writes nothing and leaves torque untouched.
        """
        found = set(self.ping_all())
        missing = sorted(set(self._name_by_id) - found)
        if missing:
            # Nothing was ever energized on this path, so do not try to write Torque_Enable
            # on the way out — on a bus that is already not answering, that write's error
            # would mask the far more useful roll-call message below.
            self.disconnect(disable_torque=False)
            raise ConnectionError(
                f"hand bus roll call failed at {baud} baud on {self._port}: "
                f"{len(missing)} of {len(self._name_by_id)} expected servos did not answer "
                f"(servo_ids {missing}). Checklist: hand 5V PSU on? TTL chain fully seated? "
                f"Run `uv run clankers-detect` to see what the bus actually reports."
            )

    def disconnect(self, *, disable_torque: bool = True) -> None:
        """Close the port, optionally cutting torque first.

        A failure to cut torque is logged loudly rather than raised: the port still has to
        be released, and swallowing the close would leave the bus locked for the next
        process. Pass disable_torque=False when nothing was ever energized.
        """
        if self._port_handler is None:
            return
        try:
            if disable_torque:
                try:
                    self.disable_torque(list(self._name_by_id))
                except (ConnectionError, RuntimeError, OSError):
                    logger.exception(
                        "could not disable torque on %s while disconnecting — the hand may "
                        "still be energized. Power-cycle the 5V rail if it stays stiff.",
                        self._port,
                    )
        finally:
            self._port_handler.closePort()
            self._port_handler = None
            self._packet = None
            self._readers.clear()

    def _require_connected(self) -> tuple[Any, Any]:
        if self._port_handler is None or self._packet is None:
            raise RuntimeError("DynamixelBus is not connected; call connect() first")
        return self._port_handler, self._packet

    # ------------------------------------------------------------------- io core

    def _reader(self, start: int, length: int) -> Any:
        """A GroupSyncRead over every expected servo, cached per (address, length)."""
        key = (start, length)
        reader = self._readers.get(key)
        if reader is None:
            handler, packet = self._require_connected()
            _, GroupSyncRead, _, _, _ = self._import_sdk()
            reader = GroupSyncRead(handler, packet, start, length)
            for sid in self._name_by_id:
                reader.addParam(sid)
            self._readers[key] = reader
        return reader

    def _read_block(self, start: int, length: int) -> Any:
        """sync_read `length` bytes from `start` on all servos, retrying before giving up."""
        self._require_connected()
        reader = self._reader(start, length)
        missing: list[int] = []
        for _ in range(self.SYNC_READ_RETRIES):
            reader.txRxPacket()
            missing = [
                sid for sid in self._name_by_id if not reader.isAvailable(sid, start, length)
            ]
            if not missing:
                return reader
        raise ConnectionError(
            f"sync_read of {length}B at address {start} failed after {self.SYNC_READ_RETRIES} "
            f"attempts: servo_ids {missing} did not answer. One dropout fails the whole "
            f"16-servo read — see the flaky 14/15 chain in docs/plans/bringup.md."
        )

    def _field(self, reader: Any, sid: int, reg: Register) -> int:
        raw = int(reader.getData(sid, reg.addr, reg.size))
        return decode_signed(raw, reg.size) if reg.signed else raw

    @staticmethod
    def _le_bytes(value: int, size: int) -> list[int]:
        return [(value >> (8 * i)) & 0xFF for i in range(size)]

    def _sync_write(self, reg: Register, values: dict[int, int]) -> None:
        handler, packet = self._require_connected()
        COMM_SUCCESS, _, GroupSyncWrite, _, _ = self._import_sdk()
        writer = GroupSyncWrite(handler, packet, reg.addr, reg.size)
        try:
            for sid, value in values.items():
                writer.addParam(sid, self._le_bytes(encode_signed(int(value), reg.size), reg.size))
            comm = writer.txPacket()
        finally:
            writer.clearParam()
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"sync_write to address {reg.addr} failed: {packet.getTxRxResult(comm)}"
            )

    def _write_one(self, reg: Register, sid: int, value: int) -> None:
        """Single-servo register write. EEPROM registers have no sync-write path."""
        handler, packet = self._require_connected()
        COMM_SUCCESS, *_ = self._import_sdk()
        writer = {1: packet.write1ByteTxRx, 2: packet.write2ByteTxRx, 4: packet.write4ByteTxRx}[
            reg.size
        ]
        comm, err = writer(handler, sid, reg.addr, encode_signed(int(value), reg.size))
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"write to address {reg.addr} on servo {sid} failed: "
                f"{packet.getTxRxResult(comm)}"
            )
        if err != 0:
            raise RuntimeError(
                f"servo {sid} rejected the write to address {reg.addr}: "
                f"{packet.getRxPacketError(err)}"
            )

    def ping_all(self) -> dict[int, int]:
        """`{servo_id: model_number}` from one broadcast ping. Read-only."""
        handler, packet = self._require_connected()
        found, _ = packet.broadcastPing(handler)
        return {int(sid): int(data[0]) for sid, data in (found or {}).items()}

    # ------------------------------------------------------------------ HandBus

    def scan(self, baud: int | None = None) -> list[dict[str, Any]]:
        handler, _ = self._require_connected()
        if baud is not None and not handler.setBaudRate(baud):
            raise ConnectionError(f"{self._port} rejected baud {baud}")
        used_baud = baud if baud is not None else handler.getBaudRate()
        return [
            {
                "servo_id": sid,
                "model": MODEL_NAMES.get(model_nb, f"unknown(0x{model_nb:x})"),
                "baud": used_baud,
            }
            for sid, model_nb in sorted(self.ping_all().items())
        ]

    def enable_torque(self, ids: list[int]) -> None:
        self._sync_write(CONTROL_TABLE["Torque_Enable"], dict.fromkeys(ids, 1))

    def disable_torque(self, ids: list[int]) -> None:
        self._sync_write(CONTROL_TABLE["Torque_Enable"], dict.fromkeys(ids, 0))

    def read_state(self) -> dict[int, dict[str, float]]:
        current = CONTROL_TABLE["Present_Current"]
        velocity = CONTROL_TABLE["Present_Velocity"]
        position = CONTROL_TABLE["Present_Position"]
        # 126..135 is contiguous, so all three come back in one transaction.
        start = current.addr
        reader = self._read_block(start, position.addr + position.size - start)
        out: dict[int, dict[str, float]] = {}
        for sid in self._name_by_id:
            vel_rev_min = self._field(reader, sid, velocity) * self.VELOCITY_REV_PER_MIN_PER_LSB
            out[sid] = {
                "pos": ticks_to_rad(self._field(reader, sid, position)),
                "vel": vel_rev_min * 2.0 * math.pi / 60.0,
                "current_ma": self._field(reader, sid, current) * self.CURRENT_MA_PER_LSB,
            }
        return out

    def read_health(self) -> dict[int, dict[str, float]]:
        voltage = CONTROL_TABLE["Present_Input_Voltage"]
        temperature = CONTROL_TABLE["Present_Temperature"]
        errors = CONTROL_TABLE["Hardware_Error_Status"]
        start = voltage.addr  # 144..146 is contiguous
        vt = self._read_block(start, temperature.addr + temperature.size - start)
        er = self._read_block(errors.addr, errors.size)
        return {
            sid: {
                "temp_c": float(self._field(vt, sid, temperature)),
                "voltage_v": self._field(vt, sid, voltage) * self.VOLTAGE_V_PER_LSB,
                "error_bits": self._field(er, sid, errors),
            }
            for sid in self._name_by_id
        }

    def write_positions(self, targets: dict[int, float]) -> None:
        self._sync_write(
            CONTROL_TABLE["Goal_Position"],
            {sid: rad_to_ticks(rad) for sid, rad in targets.items()},
        )

    def reboot(self, servo_id: int) -> None:
        handler, packet = self._require_connected()
        COMM_SUCCESS, *_ = self._import_sdk()
        comm, err = packet.reboot(handler, servo_id)
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"reboot of servo {servo_id} failed: {packet.getTxRxResult(comm)}"
            )
        if err != 0:
            raise RuntimeError(
                f"servo {servo_id} rejected reboot: {packet.getRxPacketError(err)}"
            )

    def set_current_limit(self, ma: float) -> None:
        ticks = round(ma / self.CURRENT_MA_PER_LSB)
        self._sync_write(
            CONTROL_TABLE["Current_Limit"], dict.fromkeys(self._name_by_id, ticks)
        )

    def apply_motion_profile(self) -> dict[str, int]:
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
        for reg_name, value in values.items():
            for sid in self._name_by_id:
                try:
                    self._write_one(CONTROL_TABLE[reg_name], sid, int(value))
                except (RuntimeError, ConnectionError, OSError) as exc:
                    failed[sid] = f"{reg_name}: {exc}"
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

    def read_current_limits(self) -> dict[int, float]:
        reg = CONTROL_TABLE["Current_Limit"]
        reader = self._read_block(reg.addr, reg.size)
        return {
            sid: float(self._field(reader, sid, reg)) * self.CURRENT_MA_PER_LSB
            for sid in self._name_by_id
        }

    def read_torque_enabled(self, ids: list[int]) -> dict[int, bool]:
        reg = CONTROL_TABLE["Torque_Enable"]
        reader = self._read_block(reg.addr, reg.size)
        return {sid: bool(self._field(reader, sid, reg)) for sid in ids}

    def read_homing_offsets(self, ids: list[int]) -> dict[int, int]:
        reg = CONTROL_TABLE["Homing_Offset"]
        reader = self._read_block(reg.addr, reg.size)
        return {sid: self._field(reader, sid, reg) for sid in ids}

    def write_homing_offsets(self, offsets: dict[int, int]) -> None:
        """EEPROM writes, one servo at a time (there is no sync write for EEPROM).

        Wear-limited, so callers batch a whole re-zero into one call and confirm it first
        (see CLAUDE.md); nothing here should be driven from a slider.
        """
        reg = CONTROL_TABLE["Homing_Offset"]
        for sid, ticks in offsets.items():
            self._write_one(reg, sid, int(ticks))
