"""LeRobot plugin package: LEAP hand + ClankerStation (reBot B601-DM arm + hand) — ADR-0001.

Importing this package runs the `RobotConfig.register_subclass("leap_hand"/"clanker_station")`
decorators below, making both types resolvable from `--robot.type=...` on any lerobot CLI
(`lerobot-record`, `lerobot-calibrate`, `lerobot-teleoperate`, ...).

Plugin-discovery caveat: lerobot's own auto-discovery, `register_third_party_plugins()`
(`lerobot.utils.import_utils`), scans installed distributions and imports any whose
metadata `Name` starts with `"lerobot_robot_"` (underscore). This package's distribution
name is `lerobot-robot-clankers` (hyphenated — set in `pyproject.toml`, which is out of
this package's editable scope), so that literal prefix check does **not** match:
`dist_name.startswith(("lerobot_robot_", ...))` is `False` for `"lerobot-robot-clankers"`.
Concretely, this means lerobot CLIs will NOT auto-register `leap_hand`/`clanker_station`
as valid `--robot.type` choices on their own — something must `import lerobot_robot_clankers`
first (this is what the task runner / any entry point in this repo must do explicitly,
since the automatic path is broken by the project's naming). Fixing this for real requires
renaming the `[project] name` in `packages/lerobot_robot_clankers/pyproject.toml` to
`lerobot_robot_clankers` (underscore) — flagged here rather than done, since that file is
marked do-not-edit for this task.
"""

from .clanker_station import ClankerStation, ClankerStationConfig
from .leap_hand import LeapHand, LeapHandConfig

__all__ = [
    "ClankerStation",
    "ClankerStationConfig",
    "LeapHand",
    "LeapHandConfig",
]
