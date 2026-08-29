"""Combined arm+adapter+hand URDF composition.

These run against the real vendored models (the arm URDF that ships with the studio and the
vendored LEAP hand URDF), because the thing worth protecting is that robots.yaml, the arm
model and the hand model still agree with each other -- a synthetic fixture would keep
passing while the real assembly rotted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from clankers.config import load_robots
from clankers.config.export import repo_root
from clankers.urdf import ComposeError, compose_urdf, hand_joint_rename_map

CFG = load_robots()
ARM_URDF = repo_root() / CFG.arm.urdf["path"]
HAND_URDF = repo_root() / "apps/studio/public/resources/clankers/source/leap_hand.urdf"
PACKAGE = "clankers_combined"

pytestmark = pytest.mark.skipif(
    not (ARM_URDF.is_file() and HAND_URDF.is_file()),
    reason="vendored URDFs missing; run `uv run clankers-build-urdf` first",
)


def _compose(**kw) -> ET.Element:
    return compose_urdf(CFG, ARM_URDF, HAND_URDF, package=PACKAGE, **kw).getroot()


def _joints(root: ET.Element) -> dict[str, ET.Element]:
    return {j.get("name"): j for j in root.findall("joint")}


def test_tree_has_exactly_one_root_and_no_orphans() -> None:
    root = _compose()
    links = {ln.get("name") for ln in root.findall("link")}
    children = {j.find("child").get("link") for j in root.findall("joint")}
    assert links - children == {"base_link"}, "combined tree must have base_link as sole root"
    for j in root.findall("joint"):
        assert j.find("parent").get("link") in links
        assert j.find("child").get("link") in links


def test_hand_joints_are_renamed_to_robots_yaml_names() -> None:
    """The LEAP URDF calls its joints "0".."15"; the studio and every other consumer in this
    repo speak the semantic names, so composition must translate."""
    joints = _joints(_compose())
    for canonical, name in hand_joint_rename_map(CFG).items():
        assert name in joints, f"canonical {canonical} should appear as {name!r}"
        assert canonical not in joints, f"raw LEAP joint name {canonical!r} must not survive"


def test_hand_joint_limits_survive_the_rename() -> None:
    joints = _joints(_compose())
    for j in CFG.hand.joints:
        lim = joints[j.name].find("limit")
        assert float(lim.get("lower")) == pytest.approx(j.limit.min)
        assert float(lim.get("upper")) == pytest.approx(j.limit.max)


def test_arm_joints_and_names_are_untouched() -> None:
    joints = _joints(_compose())
    for n in ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"):
        assert joints[n].get("type") == "revolute"


def test_adapter_bridges_flange_to_palm() -> None:
    root = _compose()
    joints = _joints(root)
    adapter_cfg = CFG.raw["adapter"]
    mount = joints["adapter_mount"]
    assert mount.get("type") == "fixed"
    assert mount.find("parent").get("link") == adapter_cfg["arm_mount"]["parent"]
    assert mount.find("child").get("link") == adapter_cfg["link_name"]

    hand_mount = joints["hand_mount"]
    assert hand_mount.get("type") == "fixed"
    assert hand_mount.find("parent").get("link") == adapter_cfg["link_name"]
    assert hand_mount.find("child").get("link") == (
        adapter_cfg["hand_link_prefix"] + adapter_cfg["hand_root_link"]
    )


def test_mount_transforms_come_from_robots_yaml() -> None:
    """Nothing may hard-code the geometry: the mount is provisional and gets tuned in YAML."""
    joints = _joints(_compose())
    for joint_name, key in (("adapter_mount", "arm_mount"), ("hand_mount", "hand_mount")):
        origin = joints[joint_name].find("origin")
        want = CFG.raw["adapter"][key]
        got_xyz = [float(v) for v in origin.get("xyz").split()]
        got_rpy = [float(v) for v in origin.get("rpy").split()]
        assert got_xyz == pytest.approx([float(v) for v in want["xyz"]])
        assert got_rpy == pytest.approx([float(v) for v in want["rpy"]])


def test_adapter_mesh_is_scaled_from_mm_to_m() -> None:
    root = _compose()
    link = next(ln for ln in root.findall("link")
                if ln.get("name") == CFG.raw["adapter"]["link_name"])
    mesh = link.find("visual/geometry/mesh")
    scale = [float(v) for v in mesh.get("scale").split()]
    assert scale == pytest.approx([CFG.raw["adapter"]["mesh_scale"]] * 3)
    assert mesh.get("filename").endswith(CFG.raw["adapter"]["mesh"])


def test_arm_meshes_keep_their_own_package_hand_meshes_move() -> None:
    """~24 MB of arm meshes are referenced in place rather than duplicated (build.py)."""
    root = _compose()
    packages = {m.get("filename").split("//")[1].split("/")[0] for m in root.iter("mesh")}
    assert packages == {PACKAGE, CFG.arm.urdf["package"]}


def test_gripper_can_be_dropped() -> None:
    kept = _joints(_compose(keep_gripper=True))
    dropped = _joints(_compose(keep_gripper=False))
    assert any("gripper" in n for n in kept)
    assert not any("gripper" in n for n in dropped)


def test_missing_parent_link_is_a_clear_error(tmp_path: Path) -> None:
    cfg = load_robots()
    cfg.raw["adapter"]["arm_mount"]["parent"] = "no_such_link"
    with pytest.raises(ComposeError, match="not a link"):
        compose_urdf(cfg, ARM_URDF, HAND_URDF, package=PACKAGE)


def test_hand_urdf_missing_expected_joints_is_a_clear_error(tmp_path: Path) -> None:
    tree = ET.parse(HAND_URDF)
    root = tree.getroot()
    root.remove(next(j for j in root.findall("joint") if j.get("name") == "15"))
    trimmed = tmp_path / "trimmed.urdf"
    tree.write(trimmed)
    with pytest.raises(ComposeError, match=r"missing joints \['15'\]"):
        compose_urdf(load_robots(), ARM_URDF, trimmed, package=PACKAGE)


GENERATED_URDF = repo_root() / "apps/studio/public/resources/clankers/urdf/clankers_combined.urdf"
STUDIO_PUBLIC = repo_root() / "apps/studio/public"


@pytest.mark.skipif(not GENERATED_URDF.is_file(), reason="run `uv run clankers-build-urdf` first")
def test_every_mesh_the_studio_will_fetch_exists_on_disk() -> None:
    """A missing mesh is silent in the browser -- the model just renders short a link -- so
    the check belongs here, where it fails loudly. Mirrors the viewer's package map."""
    package_dirs = {
        "clankers_combined": STUDIO_PUBLIC / "resources/clankers",
        CFG.arm.urdf["package"]: STUDIO_PUBLIC / "resources/arm02" / CFG.arm.urdf["package"],
    }
    missing = []
    for mesh in ET.parse(GENERATED_URDF).getroot().iter("mesh"):
        filename = mesh.get("filename", "")
        assert filename.startswith("package://"), filename
        package, rel = filename[len("package://") :].split("/", 1)
        base = package_dirs.get(package)
        if base is None or not (base / rel).is_file():
            missing.append(filename)
    assert not missing, f"combined URDF references meshes the studio cannot serve: {missing}"


@pytest.mark.skipif(not GENERATED_URDF.is_file(), reason="run `uv run clankers-build-urdf` first")
def test_generated_urdf_is_current_with_robots_yaml() -> None:
    """The checked-in URDF is generated; if robots.yaml moved the mount and nobody re-ran the
    builder, the studio would draw a stale assembly."""
    generated = ET.parse(GENERATED_URDF).getroot()
    fresh = _compose()
    for name in ("adapter_mount", "hand_mount"):
        got = _joints(generated)[name].find("origin")
        want = _joints(fresh)[name].find("origin")
        assert got.get("xyz") == want.get("xyz"), f"{name}: re-run `clankers-build-urdf`"
        assert got.get("rpy") == want.get("rpy"), f"{name}: re-run `clankers-build-urdf`"
