"""Task runner: pose/skill library, min-jerk playback, and a WS control surface.

`clankers.tasks.robot_adapter` is the seam between this package and a real robot —
`FakeRobot` needs no hardware or heavy deps; `LerobotAdapter` lazily imports
`lerobot_robot_clankers` (see CLAUDE.md's "heavy/optional native deps" rule). Every
adapter clamps through `clankers.safety` before sending, per ADR-0004.
"""

from .library import PoseLibrary, Skill, SkillStep, load_library
from .robot_adapter import FakeRobot, LerobotAdapter, RobotAdapter
from .runner import TaskEvent, TaskRunner

__all__ = [
    "FakeRobot",
    "LerobotAdapter",
    "PoseLibrary",
    "RobotAdapter",
    "Skill",
    "SkillStep",
    "TaskEvent",
    "TaskRunner",
    "load_library",
]
