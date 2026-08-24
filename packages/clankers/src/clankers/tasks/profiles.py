"""Quintic minimum-jerk motion profiles for pose/skill playback.

The quintic profile has zero velocity and acceleration at both endpoints, so back-to-back
moves (e.g. skill steps) don't jerk the joints at the seams.
"""

from __future__ import annotations


def min_jerk(t01: float) -> float:
    """Quintic min-jerk fraction `s` for normalized time `t01` in [0, 1].

    `s(0) == 0`, `s(1) == 1`, monotonically non-decreasing in between. `t01` is clamped
    into [0, 1] so small floating-point overshoot at the endpoints is harmless.
    """
    t = min(1.0, max(0.0, t01))
    return t**3 * (10.0 - 15.0 * t + 6.0 * t**2)


def plan_move(
    start: dict[str, float], goal: dict[str, float], duration_s: float, hz: float = 20.0
) -> list[tuple[float, dict[str, float]]]:
    """Interpolate `start` -> `goal` over `duration_s` seconds at ~`hz` frames/second.

    Only `goal`'s keys are planned; a joint missing from `start` is treated as already
    at its goal value (zero movement for that joint). Returns `(t_offset_s, {joint: value})`
    frames, inclusive of both `t_offset_s == 0` (== `start`, for planned joints) and
    `t_offset_s == duration_s` (== `goal` exactly).
    """
    if duration_s < 0:
        raise ValueError("duration_s must be >= 0")
    if hz <= 0:
        raise ValueError("hz must be positive")

    joints = list(goal.keys())
    starts = {name: start.get(name, goal[name]) for name in joints}

    if duration_s == 0:
        return [(0.0, dict(goal))]

    n_steps = max(1, round(duration_s * hz))
    frames: list[tuple[float, dict[str, float]]] = []
    for i in range(n_steps + 1):
        t = duration_s * i / n_steps
        s = min_jerk(t / duration_s)
        frame = {name: starts[name] + (goal[name] - starts[name]) * s for name in joints}
        frames.append((t, frame))
    return frames
