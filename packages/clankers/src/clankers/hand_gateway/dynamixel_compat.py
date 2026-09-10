"""Shared Dynamixel conventions for the LEAP hand: tick<->radian conversion and the
local xc330-m288 model-table patch for lerobot.

Single source of truth used by BOTH bus consumers (ADR-0002): `hand_gateway.bus`
(debug gateway) and `lerobot_robot_clankers.leap_hand` (LeRobot runtime). Keeping the
conversion in one place prevents the two paths from disagreeing about where zero is —
a disagreement that commands a servo toward its mechanical hard stop.

XC330-M288 uses the standard Dynamixel X-series 4096-count encoder (one full turn).
Ticks are centered on the mechanical mid-point (half-turn homing convention, matching
`DynamixelMotorsBus.set_half_turn_homings`): raw tick 2048 == 0 rad. Every hand joint
limit in robots.yaml is well inside +/-pi rad, so conversions never wrap and always
produce ticks in [0, 4096).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TICKS_PER_REV = 4096
CENTER_TICK = TICKS_PER_REV // 2

XC330_M288_MODEL_NUMBER = 1240  # confirmed via emanual.robotis.com/docs/en/dxl/x/xc330-m288

# Model-number -> name without lerobot, so the controller install can name what it finds.
# Deliberately only what we have confirmed on this hardware: `detect` enriches this from
# lerobot's full table when lerobot happens to be installed, and anything still unknown is
# reported as its raw number rather than guessed at.
MODEL_NAMES: dict[int, str] = {XC330_M288_MODEL_NUMBER: "xc330-m288"}

# Below this address the control table is EEPROM: writes are wear-limited and are rejected
# while torque is on (CLAUDE.md house rule -- batch them and confirm explicitly).
EEPROM_LIMIT_ADDR = 64


@dataclass(frozen=True)
class Register:
    """One X-series control-table entry (protocol 2.0)."""

    addr: int
    size: int
    signed: bool = False

    @property
    def eeprom(self) -> bool:
        return self.addr < EEPROM_LIMIT_ADDR


# X-series control table. Names match lerobot's register names on purpose: the raw-SDK bus
# and the lerobot bus must not develop two vocabularies for the same register
# (docs/glossary.md is emphatic about this class of collision).
#
# Present_Current(126,2) Present_Velocity(128,4) Present_Position(132,4) are contiguous, so
# `read_state` pulls all three in ONE sync_read of 10 bytes from 126. That matters here:
# every extra transaction is another chance for the flaky servo 14/15 chain to drop a
# status packet and fail the whole poll (docs/plans/bringup.md).
CONTROL_TABLE: dict[str, Register] = {
    "Model_Number": Register(0, 2),
    "Operating_Mode": Register(11, 1),
    "Homing_Offset": Register(20, 4, signed=True),
    "Current_Limit": Register(38, 2),
    "Torque_Enable": Register(64, 1),
    "Hardware_Error_Status": Register(70, 1),
    "Profile_Acceleration": Register(108, 4),
    "Profile_Velocity": Register(112, 4),
    "Goal_Position": Register(116, 4, signed=True),
    "Present_Current": Register(126, 2, signed=True),
    "Present_Velocity": Register(128, 4, signed=True),
    "Present_Position": Register(132, 4, signed=True),
    "Present_Input_Voltage": Register(144, 2),
    "Present_Temperature": Register(146, 1),
}


def decode_signed(value: int, size: int) -> int:
    """Two's-complement fixup for a register read back as an unsigned little-endian int."""
    bits = 8 * size
    return value - (1 << bits) if value >= (1 << (bits - 1)) else value


def encode_signed(value: int, size: int) -> int:
    """Inverse of `decode_signed`: negative -> the unsigned int actually put on the wire.

    Refuses a value the register cannot hold. Callers serialize the result little-endian by
    masking each byte, which would otherwise wrap an out-of-range value into a plausible-
    looking but completely different command -- a homing offset silently landing half a
    turn away, say.
    """
    bits = 8 * size
    low, high = -(1 << (bits - 1)), (1 << bits) - 1
    if not low <= value <= high:
        raise ValueError(f"{value} does not fit in a {size}-byte register ({low}..{high})")
    return value + (1 << bits) if value < 0 else value

_dxl_patched = False


def ticks_to_rad(ticks: int) -> float:
    return (ticks - CENTER_TICK) * (math.tau / TICKS_PER_REV)


def rad_to_ticks(rad: float) -> int:
    return CENTER_TICK + round(rad * TICKS_PER_REV / math.tau)


# Motion-profile register units (X-series datasheet). Writing these makes the servo ramp to
# a goal on a trapezoidal velocity profile in firmware, instead of slamming toward it at full
# speed -- which is what Profile_Velocity = 0 (the factory default) means.
PROFILE_VELOCITY_REV_PER_MIN_PER_LSB = 0.229
PROFILE_ACCEL_REV_PER_MIN2_PER_LSB = 214.577


def rev_per_min_to_profile_velocity(rev_per_min: float) -> int:
    """Profile_Velocity register value. 0 means "no limit", i.e. no profile at all."""
    if rev_per_min <= 0:
        return 0
    return max(1, round(rev_per_min / PROFILE_VELOCITY_REV_PER_MIN_PER_LSB))


def rev_per_min2_to_profile_accel(rev_per_min2: float) -> int:
    """Profile_Acceleration register value. 0 means "no limit" (instant ramp)."""
    if rev_per_min2 <= 0:
        return 0
    return max(1, round(rev_per_min2 / PROFILE_ACCEL_REV_PER_MIN2_PER_LSB))


def ticks_delta_to_rad(ticks: int) -> float:
    """Convert a tick *difference* to radians.

    Distinct from `ticks_to_rad`, which converts an absolute tick reading and therefore
    subtracts CENTER_TICK. Homing offsets are differences, so running them through the
    absolute conversion would shift every one by half a turn.
    """
    return ticks * (math.tau / TICKS_PER_REV)


def rad_delta_to_ticks(rad: float) -> int:
    return round(rad * TICKS_PER_REV / math.tau)


def patch_xc330_m288_table() -> None:
    """Add a local 'xc330-m288' entry to lerobot's Dynamixel model tables.

    Upstream `lerobot/motors/dynamixel/tables.py` (as of lerobot 0.6) only lists
    'xc330-t288' / 'xc330-t181' — not the M-series servo this hand actually uses. The
    control table, baud-rate table, and encoding table are identical across the whole
    X-series; only the model number differs, and XC330-M288 shares the same 4096-count
    resolution as its siblings. ADR-0001 flags this as "small PR or local table" — this
    is the local table until it lands upstream. Idempotent; patches both the `tables`
    module (read by fresh imports) and `DynamixelMotorsBus`'s class attributes directly
    (they're `deepcopy`d from the tables module at `dynamixel.py` import time, so a
    tables-only patch would be a no-op if that module was already imported first).
    """
    global _dxl_patched
    if _dxl_patched:
        return

    from lerobot.motors.dynamixel import tables as dxl_tables

    model = "xc330-m288"
    additions = {
        "MODEL_NUMBER_TABLE": XC330_M288_MODEL_NUMBER,
        "MODEL_CONTROL_TABLE": dxl_tables.X_SERIES_CONTROL_TABLE,
        "MODEL_BAUDRATE_TABLE": dxl_tables.X_SERIES_BAUDRATE_TABLE,
        "MODEL_ENCODING_TABLE": dxl_tables.X_SERIES_ENCODINGS_TABLE,
        "MODEL_RESOLUTION": TICKS_PER_REV,
    }
    for table_name, value in additions.items():
        getattr(dxl_tables, table_name).setdefault(model, value)

    try:
        from lerobot.motors.dynamixel.dynamixel import DynamixelMotorsBus

        DynamixelMotorsBus.model_number_table.setdefault(model, XC330_M288_MODEL_NUMBER)
        DynamixelMotorsBus.model_ctrl_table.setdefault(model, dxl_tables.X_SERIES_CONTROL_TABLE)
        DynamixelMotorsBus.model_baudrate_table.setdefault(
            model, dxl_tables.X_SERIES_BAUDRATE_TABLE
        )
        DynamixelMotorsBus.model_encoding_table.setdefault(
            model, dxl_tables.X_SERIES_ENCODINGS_TABLE
        )
        DynamixelMotorsBus.model_resolution_table.setdefault(model, TICKS_PER_REV)
    except ImportError:
        pass  # dynamixel-sdk not installed: DynamixelMotorsBus itself can't be built yet either.

    _dxl_patched = True
