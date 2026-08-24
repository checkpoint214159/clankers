"""Normalized fault taxonomy across Damiao and Dynamixel (ADR-0004).

Faults require explicit operator acknowledgment; a tripped Dynamixel shutdown needs an
explicit reboot op by design. Never auto-clear.
"""

from __future__ import annotations

from enum import Enum


class Fault(str, Enum):
    UNDERVOLTAGE = "undervoltage"
    OVERVOLTAGE = "overvoltage"
    OVERCURRENT = "overcurrent"
    OVERTEMP_MOSFET = "overtemp_mosfet"
    OVERTEMP_MOTOR = "overtemp_motor"
    OVERLOAD = "overload"
    COMMS_TIMEOUT = "comms_timeout"
    ENCODER_FAULT = "encoder_fault"
    ELECTRICAL_SHOCK = "electrical_shock"
    BUS_OFF = "bus_off"


# Dynamixel X-series Hardware Error Status (register addr 70), bitwise-OR of active faults.
# Verified against the XC330-M288 control table.
_DXL_ERROR_BITS: dict[int, Fault] = {
    0: Fault.UNDERVOLTAGE,  # input voltage out of range (<3.1V or >7.0V on XC330)
    2: Fault.OVERTEMP_MOTOR,  # exceeds Temperature Limit (addr 31, default 70°C)
    4: Fault.ELECTRICAL_SHOCK,
    5: Fault.OVERLOAD,
}

# Damiao status nibble (state frames / register reads). 0x0 disabled, 0x1 enabled are
# normal states; 0x8..0xE are faults.
_DAMIAO_STATUS: dict[int, Fault] = {
    0x8: Fault.OVERVOLTAGE,
    0x9: Fault.UNDERVOLTAGE,
    0xA: Fault.OVERCURRENT,
    0xB: Fault.OVERTEMP_MOSFET,
    0xC: Fault.OVERTEMP_MOTOR,
    0xD: Fault.COMMS_TIMEOUT,  # loss of communication / feedback
    0xE: Fault.OVERLOAD,
}

DAMIAO_NORMAL_STATUSES = {0x0, 0x1}


def decode_dynamixel_error(hardware_error_status: int) -> list[Fault]:
    """Decode Dynamixel addr-70 bits into normalized faults (empty list = healthy)."""
    return [f for bit, f in _DXL_ERROR_BITS.items() if hardware_error_status & (1 << bit)]


def decode_damiao_status(status: int) -> list[Fault]:
    """Decode a Damiao status nibble into normalized faults (empty list = normal)."""
    if status in DAMIAO_NORMAL_STATUSES:
        return []
    fault = _DAMIAO_STATUS.get(status)
    return [fault] if fault else [Fault.ENCODER_FAULT]
