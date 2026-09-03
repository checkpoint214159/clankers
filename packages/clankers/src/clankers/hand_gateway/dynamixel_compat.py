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

TICKS_PER_REV = 4096
CENTER_TICK = TICKS_PER_REV // 2

XC330_M288_MODEL_NUMBER = 1240  # confirmed via emanual.robotis.com/docs/en/dxl/x/xc330-m288

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
