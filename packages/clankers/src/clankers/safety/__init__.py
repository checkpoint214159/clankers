"""Safety layer: fault taxonomy, clamps, watchdog (ADR-0004).

Every command path calls into this module — gateway ops AND Robot.send_action.
Never rely on UI-side clamping alone.
"""

from .clamps import SafetyClamp, clamp_step
from .faults import Fault, decode_damiao_status, decode_dynamixel_error
from .watchdog import Watchdog

__all__ = [
    "Fault",
    "SafetyClamp",
    "Watchdog",
    "clamp_step",
    "decode_damiao_status",
    "decode_dynamixel_error",
]
