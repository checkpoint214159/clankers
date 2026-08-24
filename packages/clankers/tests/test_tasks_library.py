"""Pose/skill library validation tests (clankers.tasks.library)."""

from __future__ import annotations

import pytest
import yaml
from clankers.config import load_robots
from clankers.tasks.library import load_library, poses_yaml_path

CFG = load_robots()


def test_default_library_loads_and_has_starter_entries() -> None:
    lib = load_library(CFG)
    assert set(lib.pose_names) >= {"home", "hand_open", "hand_curl"}
    assert set(lib.skill_names) >= {"wave_fingers", "demo_open_close"}


def test_home_pose_covers_every_known_joint_at_zero() -> None:
    lib = load_library(CFG)
    home = lib.pose("home")
    known = {j.name for j in CFG.arm.joints} | {j.name for j in CFG.hand.joints}
    assert set(home) == known
    assert all(v == 0.0 for v in home.values())


def test_hand_open_and_curl_only_touch_hand_joints() -> None:
    lib = load_library(CFG)
    hand_names = {j.name for j in CFG.hand.joints}
    assert set(lib.pose("hand_open")) <= hand_names
    assert set(lib.pose("hand_curl")) <= hand_names


def test_skills_reference_only_known_poses_with_positive_duration() -> None:
    lib = load_library(CFG)
    for skill_name in lib.skill_names:
        skill = lib.skill(skill_name)
        assert skill.steps
        for step in skill.steps:
            assert step.pose in lib.poses
            assert step.duration_s > 0


def test_pose_lookup_of_unknown_name_raises_keyerror() -> None:
    lib = load_library(CFG)
    with pytest.raises(KeyError):
        lib.pose("does_not_exist")


def test_skill_lookup_of_unknown_name_raises_keyerror() -> None:
    lib = load_library(CFG)
    with pytest.raises(KeyError):
        lib.skill("does_not_exist")


def test_unknown_joint_name_in_pose_fails_loudly_at_load(tmp_path) -> None:
    bad = tmp_path / "poses.yaml"
    bad.write_text(
        yaml.safe_dump({"poses": {"broken": {"not_a_real_joint": 0.0}}, "skills": {}})
    )
    with pytest.raises(ValueError, match="unknown joint"):
        load_library(CFG, path=bad)


def test_out_of_limit_value_in_pose_fails_loudly_at_load(tmp_path) -> None:
    joint = CFG.arm.joints[0]
    over_limit = joint.limit.max + 10.0
    bad = tmp_path / "poses.yaml"
    bad.write_text(
        yaml.safe_dump({"poses": {"broken": {joint.name: over_limit}}, "skills": {}})
    )
    with pytest.raises(ValueError, match="outside limit"):
        load_library(CFG, path=bad)


def test_skill_referencing_unknown_pose_fails_loudly_at_load(tmp_path) -> None:
    bad = tmp_path / "poses.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "poses": {"home": {"joint1": 0.0}},
                "skills": {"broken": {"steps": [{"pose": "ghost", "duration_s": 1.0}]}},
            }
        )
    )
    with pytest.raises(ValueError, match="unknown pose"):
        load_library(CFG, path=bad)


def test_poses_yaml_ships_next_to_the_module() -> None:
    assert poses_yaml_path().is_file()
