"""Pose/skill library: named joint configurations and timed sequences.

Ships `poses.yaml` next to this module. Every pose is validated against the current
`robots.yaml` at load time — an unknown joint name or an out-of-limit value fails
loudly here rather than being silently clamped later by the robot adapter's
`SafetyClamp` on the way to hardware.

Poses may be partial: a pose only lists the joints it cares about. Filling in the
remaining joints from the robot's current position is the caller's job (`TaskRunner`),
not the library's — this module knows nothing about a live robot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clankers.config import JointLimit, RobotsConfig, load_robots


def poses_yaml_path() -> Path:
    return Path(__file__).parent / "poses.yaml"


@dataclass(frozen=True)
class SkillStep:
    pose: str
    duration_s: float


@dataclass(frozen=True)
class Skill:
    name: str
    steps: tuple[SkillStep, ...]

    @property
    def duration_s(self) -> float:
        return sum(step.duration_s for step in self.steps)


@dataclass(frozen=True)
class PoseLibrary:
    poses: dict[str, dict[str, float]]
    skills: dict[str, Skill]

    @property
    def pose_names(self) -> list[str]:
        return sorted(self.poses)

    @property
    def skill_names(self) -> list[str]:
        return sorted(self.skills)

    def pose(self, name: str) -> dict[str, float]:
        """A copy of the named pose's partial joint map. Raises KeyError if unknown."""
        try:
            return dict(self.poses[name])
        except KeyError:
            raise KeyError(f"unknown pose {name!r}") from None

    def skill(self, name: str) -> Skill:
        try:
            return self.skills[name]
        except KeyError:
            raise KeyError(f"unknown skill {name!r}") from None


def load_library(cfg: RobotsConfig | None = None, path: Path | None = None) -> PoseLibrary:
    """Load and validate `poses.yaml` (or `path`) against `cfg` (or `load_robots()`)."""
    cfg = cfg or load_robots()
    p = path or poses_yaml_path()
    raw = yaml.safe_load(p.read_text()) or {}

    limits: dict[str, JointLimit] = {j.name: j.limit for j in cfg.arm.joints}
    limits.update({j.name: j.limit for j in cfg.hand.joints})

    poses: dict[str, dict[str, float]] = {}
    for name, joints in (raw.get("poses") or {}).items():
        poses[name] = _validate_pose(name, joints, limits)
    if not poses:
        raise ValueError(f"{p} defines no poses")

    skills: dict[str, Skill] = {}
    for name, spec in (raw.get("skills") or {}).items():
        skills[name] = _validate_skill(name, spec, poses)

    return PoseLibrary(poses=poses, skills=skills)


def _validate_pose(
    name: str, joints: dict[str, Any] | None, limits: dict[str, JointLimit]
) -> dict[str, float]:
    if not joints:
        raise ValueError(f"pose {name!r} has no joints")
    out: dict[str, float] = {}
    for joint_name, value in joints.items():
        limit = limits.get(joint_name)
        if limit is None:
            raise ValueError(f"pose {name!r} references unknown joint {joint_name!r}")
        fvalue = float(value)
        if not (limit.min <= fvalue <= limit.max):
            raise ValueError(
                f"pose {name!r} joint {joint_name!r} value {fvalue} outside limit "
                f"[{limit.min}, {limit.max}]"
            )
        out[joint_name] = fvalue
    return out


def _validate_skill(
    name: str, spec: dict[str, Any] | None, poses: dict[str, dict[str, float]]
) -> Skill:
    steps_raw = (spec or {}).get("steps") or []
    if not steps_raw:
        raise ValueError(f"skill {name!r} has no steps")
    steps = []
    for step in steps_raw:
        pose_name = step["pose"]
        duration_s = float(step["duration_s"])
        if pose_name not in poses:
            raise ValueError(f"skill {name!r} references unknown pose {pose_name!r}")
        if duration_s <= 0:
            raise ValueError(f"skill {name!r} step {pose_name!r} has non-positive duration_s")
        steps.append(SkillStep(pose=pose_name, duration_s=duration_s))
    return Skill(name=name, steps=tuple(steps))
