"""Joint-limit and step clamps (ADR-0004 layer 2).

Called from every software command path: hand_gateway ops, task runner, and
Robot.send_action in lerobot_robot_clankers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from clankers.config import HandConfig, JointLimit


def clamp_step(previous: float, target: float, max_step: float) -> float:
    """Limit how far a single command may move a joint from its last known target.

    Non-finite targets are rejected, never coerced: NaN compares False against every
    bound, so without this check it would fall straight through both step guards and
    later resolve to a joint's hard limit inside min/max-based limit clamps.
    """
    if max_step <= 0:
        raise ValueError("max_step must be positive")
    if not math.isfinite(target):
        raise ValueError(f"non-finite target {target!r}")
    delta = target - previous
    if delta > max_step:
        return previous + max_step
    if delta < -max_step:
        return previous - max_step
    return target


@dataclass
class SafetyClamp:
    """Stateful per-robot-side clamp: joint limits + per-command max step.

    Tracks the last commanded target per joint so step clamping works across calls.
    `reset(positions)` must be called with current measured positions when torque is
    enabled, so the first command can't jump from a stale target.
    """

    limits: dict[str, JointLimit]
    max_step: float
    _last: dict[str, float] = field(default_factory=dict)

    @classmethod
    def for_hand(cls, hand: HandConfig) -> SafetyClamp:
        return cls(
            limits={j.name: j.limit for j in hand.joints},
            max_step=float(hand.safety["max_step_rad"]),
        )

    def reset(self, positions: dict[str, float]) -> None:
        self._last = dict(positions)

    def note(self, name: str, value: float) -> None:
        """Record a position reached outside `apply` (e.g. a `jog`), so the next `apply`
        step-limits from where the joint actually is, not a stale target."""
        if name not in self.limits:
            raise KeyError(f"unknown joint {name!r}")
        self._last[name] = float(value)

    def apply_limits(self, targets: dict[str, float]) -> dict[str, float]:
        """Joint limits only, no per-command step clamp.

        For pose commands on a bus whose servos enforce their own velocity profile: the
        speed guard lives in firmware, so slicing the move host-side just makes a single
        commanded pose take several commands to arrive. Still updates `_last`, so a later
        stepped command steps from where this one actually put the joint.
        """
        out: dict[str, float] = {}
        for name, target in targets.items():
            limit = self.limits.get(name)
            if limit is None:
                raise KeyError(f"unknown joint {name!r}")
            value = float(target)
            if not math.isfinite(value):
                raise ValueError(f"non-finite target for {name!r}: {value!r}")
            value = limit.clamp(value)
            out[name] = value
            self._last[name] = value
        return out

    def apply(self, targets: dict[str, float]) -> dict[str, float]:
        """Clamp a {joint_name: target} command. Unknown joints and non-finite targets raise."""
        out: dict[str, float] = {}
        for name, target in targets.items():
            limit = self.limits.get(name)
            if limit is None:
                raise KeyError(f"unknown joint {name!r}")
            value = float(target)
            if not math.isfinite(value):
                raise ValueError(f"non-finite target for {name!r}: {value!r}")
            value = limit.clamp(value)
            prev = self._last.get(name)
            if prev is not None:
                value = clamp_step(prev, value, self.max_step)
            out[name] = value
            self._last[name] = value
        return out
