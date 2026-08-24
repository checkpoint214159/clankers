"""Typed access to robots.yaml — the single source of truth for topology/IDs/limits.

Usage:
    from clankers.config import load_robots
    cfg = load_robots()
    cfg.hand.joints[0].name  # "index_mcp_side"
"""

from .loader import (
    ArmConfig,
    ArmJoint,
    HandConfig,
    HandJoint,
    JointLimit,
    RobotsConfig,
    load_robots,
    robots_yaml_path,
)

__all__ = [
    "ArmConfig",
    "ArmJoint",
    "HandConfig",
    "HandJoint",
    "JointLimit",
    "RobotsConfig",
    "load_robots",
    "robots_yaml_path",
]
