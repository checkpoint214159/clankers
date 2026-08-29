"""`clankers-build-urdf`: vendor the meshes and write the combined arm+adapter+hand URDF.

The studio's URDF loader is configured with exactly one package
(`loader.packages = {name: path}`), so everything the combined model references has to live
under a single directory. This command copies the arm meshes, the adapter STL and the LEAP
meshes into that one place (LEAP names are prefixed `leap_` so `pip.stl` cannot collide with
anything the arm ships) and writes the URDF next to them.

Re-run it after editing the `adapter:` block in robots.yaml -- that block, not this file,
decides where the hand sits.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from clankers.config import load_robots
from clankers.config.export import repo_root

from .compose import ComposeError, compose_urdf

logger = logging.getLogger(__name__)

PACKAGE_NAME = "clankers_combined"
DEFAULT_OUT_DIR = Path("apps/studio/public/resources/clankers")
VENDORED_HAND_URDF = "leap_hand.urdf"


def _copy(src: Path, dest: Path) -> bool:
    """Copy unless src IS dest -- a no-argument rebuild reads from the vendored copies it
    would otherwise overwrite, and shutil raises SameFileError on that."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return False
    shutil.copy2(src, dest)
    return True


def _copy_meshes(src_dir: Path, dest: Path, *, prefix: str = "", exts=(".stl",)) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(src_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in exts:
            _copy(f, dest / f"{prefix}{f.name}")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clankers-build-urdf", description=__doc__)
    parser.add_argument("--hand-urdf", type=Path, default=None,
                        help="LEAP_Hand_Sim assets/leap_hand/robot.urdf (meshes taken from beside "
                             "it); defaults to the vendored copy from a previous run")
    parser.add_argument("--adapter-stl", type=Path, default=None,
                        help="adapter STL in millimetres; defaults to the vendored copy")
    parser.add_argument("--arm-urdf", type=Path, default=None,
                        help="override the arm URDF (default: arm.urdf.path from robots.yaml)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help=f"package directory to write (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--no-gripper", action="store_true",
                        help="drop the parallel gripper links/joints from the combined tree")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(message)s")

    cfg = load_robots()
    root = repo_root()
    arm_urdf = args.arm_urdf or (root / cfg.arm.urdf["path"])
    out_dir = args.out_dir or (root / DEFAULT_OUT_DIR)
    hand_urdf = args.hand_urdf or (out_dir / "source" / VENDORED_HAND_URDF)
    adapter_stl = args.adapter_stl or (out_dir / "meshes" / cfg.raw["adapter"]["mesh"])

    for label, p in (("arm URDF", arm_urdf), ("hand URDF", hand_urdf),
                     ("adapter STL", adapter_stl)):
        if not p.is_file():
            parser.error(f"{label} not found: {p}")

    meshes = out_dir / "meshes"
    # Arm meshes are deliberately NOT copied: the combined URDF points at the arm's own
    # package (see compose._rewrite_meshes) so ~24 MB is not duplicated in git.
    # On a no-argument rebuild hand_urdf is the vendored copy, whose directory holds no
    # meshes -- they were vendored into `meshes/` on the first run and are still there.
    _copy_meshes(hand_urdf.parent, meshes, prefix="leap_", exts=(".stl",))
    n_hand = len(list(meshes.glob("leap_*.stl")))
    _copy(adapter_stl, meshes / cfg.raw["adapter"]["mesh"])
    # Vendor the hand URDF too, so a rebuild does not need the LEAP_Hand_Sim clone.
    _copy(hand_urdf, out_dir / "source" / VENDORED_HAND_URDF)
    logger.info("package has %d leap meshes + adapter at %s (arm meshes referenced in place)",
                n_hand, meshes)

    try:
        tree = compose_urdf(
            cfg, arm_urdf, hand_urdf,
            package=PACKAGE_NAME, keep_gripper=not args.no_gripper,
        )
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    urdf_dir = out_dir / "urdf"
    urdf_dir.mkdir(parents=True, exist_ok=True)
    out = urdf_dir / f"{PACKAGE_NAME}.urdf"
    tree.write(out, encoding="unicode", xml_declaration=True)
    root_el = tree.getroot()
    logger.info(
        "wrote %s (%d links, %d joints)", out,
        len(root_el.findall("link")), len(root_el.findall("joint")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
