"""Stitch arm URDF + adapter STL + LEAP hand URDF into one tree.

Naming is the whole point of this module. The LEAP URDF names its joints "0".."15", which
collide with nothing in the arm URDF but mean nothing to an operator and are ambiguous in the
studio (whose joint-map normalizer reads a bare integer key as `joint<N>`). So every hand
joint is renamed to its `robots.yaml` semantic name (canonical index -> name), and every hand
link gets a `hand_` prefix. After composition the combined tree speaks exactly the vocabulary
the rest of the repo already uses.

Mesh paths: the arm keeps its own `package://` root (its meshes already ship with the studio),
while the adapter and the LEAP meshes move into one new package. The studio's URDF loader
takes a package map, so it resolves both.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from clankers.config import RobotsConfig


class ComposeError(RuntimeError):
    """Raised when the source URDFs do not contain what robots.yaml says they should."""


@dataclass(frozen=True)
class MountTransform:
    """A fixed-joint placement: translation in metres, rotation as URDF rpy in radians."""

    xyz: tuple[float, float, float]
    rpy: tuple[float, float, float]

    @staticmethod
    def from_config(raw: dict) -> MountTransform:
        xyz = tuple(float(v) for v in raw["xyz"])
        rpy = tuple(float(v) for v in raw["rpy"])
        if len(xyz) != 3 or len(rpy) != 3:
            raise ComposeError(f"mount transform needs 3 values each, got {raw!r}")
        return MountTransform(xyz=xyz, rpy=rpy)  # type: ignore[arg-type]

    @property
    def xyz_attr(self) -> str:
        return " ".join(f"{v:.9g}" for v in self.xyz)

    @property
    def rpy_attr(self) -> str:
        return " ".join(f"{v:.9g}" for v in self.rpy)


def hand_joint_rename_map(cfg: RobotsConfig) -> dict[str, str]:
    """LEAP URDF joint name (the canonical index as a string) -> robots.yaml joint name."""
    return {str(j.canonical): j.name for j in cfg.hand.joints}


def _rewrite_meshes(root: ET.Element, package: str, prefix: str) -> None:
    """Point every <mesh> at the single combined package, prefixing the basename."""
    for mesh in root.iter("mesh"):
        fn = mesh.get("filename")
        if not fn:
            continue
        base = fn.rsplit("/", 1)[-1]
        mesh.set("filename", f"package://{package}/meshes/{prefix}{base}")


def _prefix_links(root: ET.Element, prefix: str, rename: dict[str, str]) -> None:
    """Prefix link names (and every reference to them) so two robots can share a tree."""
    for link in root.findall("link"):
        link.set("name", prefix + link.get("name", ""))
    for joint in root.findall("joint"):
        joint.set("name", rename.get(joint.get("name", ""), prefix + joint.get("name", "")))
        for side in ("parent", "child"):
            el = joint.find(side)
            if el is not None:
                el.set("link", prefix + el.get("link", ""))


def _fixed_joint(name: str, parent: str, child: str, mount: MountTransform) -> ET.Element:
    j = ET.Element("joint", {"name": name, "type": "fixed"})
    ET.SubElement(j, "origin", {"xyz": mount.xyz_attr, "rpy": mount.rpy_attr})
    ET.SubElement(j, "parent", {"link": parent})
    ET.SubElement(j, "child", {"link": child})
    return j


def _adapter_link(name: str, mesh: str, scale: float, package: str) -> ET.Element:
    """A single rigid plate: one mesh reused for visual and collision.

    The STL is authored in millimetres while URDF is metres, so the scale factor is applied
    on the <mesh> element rather than by rewriting the file.
    """
    link = ET.Element("link", {"name": name})
    sc = " ".join([f"{scale:.9g}"] * 3)
    for tag in ("visual", "collision"):
        el = ET.SubElement(link, tag)
        ET.SubElement(el, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
        geom = ET.SubElement(el, "geometry")
        ET.SubElement(geom, "mesh", {"filename": f"package://{package}/meshes/{mesh}", "scale": sc})
        if tag == "visual":
            mat = ET.SubElement(el, "material", {"name": "adapter_material"})
            ET.SubElement(mat, "color", {"rgba": "0.25 0.55 0.85 1"})
    inertial = ET.SubElement(link, "inertial")
    ET.SubElement(inertial, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
    ET.SubElement(inertial, "mass", {"value": "0.12"})
    ET.SubElement(
        inertial,
        "inertia",
        {"ixx": "1e-4", "ixy": "0", "ixz": "0", "iyy": "1e-4", "iyz": "0", "izz": "1e-4"},
    )
    return link


def compose_urdf(
    cfg: RobotsConfig,
    arm_urdf: Path,
    hand_urdf: Path,
    *,
    package: str,
    robot_name: str = "clankers_combined",
    keep_gripper: bool = True,
) -> ET.ElementTree:
    """Build the combined tree: arm -> adapter -> hand, driven entirely by robots.yaml."""
    adapter = cfg.raw.get("adapter")
    if not adapter:
        raise ComposeError("robots.yaml has no `adapter:` section; cannot place the hand")

    arm_root = ET.parse(arm_urdf).getroot()
    hand_root = ET.parse(hand_urdf).getroot()

    arm_mount = MountTransform.from_config(adapter["arm_mount"])
    hand_mount = MountTransform.from_config(adapter["hand_mount"])
    parent_link = str(adapter["arm_mount"]["parent"])
    hand_prefix = str(adapter.get("hand_link_prefix", "hand_"))
    adapter_link_name = str(adapter.get("link_name", "adapter_link"))

    arm_links = {ln.get("name") for ln in arm_root.findall("link")}
    if parent_link not in arm_links:
        raise ComposeError(
            f"robots.yaml adapter.arm_mount.parent is {parent_link!r}, which is not a link in "
            f"{arm_urdf.name} (has {sorted(arm_links)})"
        )

    rename = hand_joint_rename_map(cfg)
    hand_joint_names = {j.get("name") for j in hand_root.findall("joint")}
    missing = sorted(set(rename) - hand_joint_names, key=int)
    if missing:
        raise ComposeError(
            f"{hand_urdf.name} is missing joints {missing} that robots.yaml expects "
            "(canonical index -> URDF joint name)"
        )

    # The arm meshes already ship with the studio under their own package, and they are
    # ~24 MB -- copying them into a second package just to rename them would double that in
    # git for nothing. The loader takes a package map, so the combined URDF simply keeps
    # referring to the arm package and only the new parts move into `package`.
    _rewrite_meshes(hand_root, package, prefix="leap_")
    _prefix_links(hand_root, hand_prefix, rename)

    out = ET.Element("robot", {"name": robot_name})
    for el in arm_root:
        if el.tag == "joint" and not keep_gripper and "gripper" in (el.get("name") or ""):
            continue
        if el.tag == "link" and not keep_gripper and "gripper" in (el.get("name") or ""):
            continue
        out.append(el)

    out.append(_adapter_link(adapter_link_name, str(adapter["mesh"]),
                             float(adapter["mesh_scale"]), package))
    out.append(_fixed_joint("adapter_mount", parent_link, adapter_link_name, arm_mount))

    hand_root_link = str(adapter.get("hand_root_link", "palm_lower"))
    out.append(
        _fixed_joint("hand_mount", adapter_link_name, hand_prefix + hand_root_link, hand_mount)
    )
    for el in hand_root:
        out.append(el)

    tree = ET.ElementTree(out)
    ET.indent(tree, space="  ")
    return tree
