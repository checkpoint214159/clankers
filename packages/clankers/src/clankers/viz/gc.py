"""rrd cache GC (ADR-0003): `data/rrd/` is ephemeral and size-capped.

    uv run python -m clankers.viz.gc [--dry-run] [--rrd-dir PATH] [--max-gb N]

The planning/deletion core (`scan_rrd_dir`, `plan_deletions`, `run_gc`) is pure
stdlib and takes the byte threshold as a plain argument, so it's testable without
`clankers.config` or any optional dependency. Only `main()` reaches into
`clankers.config` for the configured default, and only when actually run as a script.

Deletion order: oldest-first by mtime among files NOT matching `_FAIL` in their name,
then oldest-first among `_FAIL` files — failures are retained longest, per ADR-0003.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

_FAIL_MARKER = "_FAIL"
_BYTES_PER_GB = 1_000_000_000  # decimal GB, matching config key name `rrd_cache_max_gb`


@dataclass(frozen=True)
class RrdFile:
    path: Path
    size_bytes: int
    mtime: float

    @property
    def is_fail(self) -> bool:
        return _FAIL_MARKER in self.path.name


def gb_to_bytes(gb: float) -> int:
    return int(gb * _BYTES_PER_GB)


def scan_rrd_dir(rrd_dir: Path) -> list[RrdFile]:
    """List `*.rrd` files under `rrd_dir` with their size and mtime. Empty if the dir is absent."""
    if not rrd_dir.is_dir():
        return []
    files = []
    for path in rrd_dir.glob("*.rrd"):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(RrdFile(path=path, size_bytes=stat.st_size, mtime=stat.st_mtime))
    return files


def plan_deletions(files: list[RrdFile], max_bytes: int) -> list[RrdFile]:
    """Return the files to delete, oldest-first, to bring total size at/under `max_bytes`.

    Non-FAIL files are exhausted (oldest first) before any FAIL file is ever selected,
    regardless of relative age — failures are retained longest by design.
    """
    total = sum(f.size_bytes for f in files)
    if total <= max_bytes:
        return []

    non_fail = sorted((f for f in files if not f.is_fail), key=lambda f: f.mtime)
    fail = sorted((f for f in files if f.is_fail), key=lambda f: f.mtime)

    to_delete: list[RrdFile] = []
    for f in (*non_fail, *fail):
        if total <= max_bytes:
            break
        to_delete.append(f)
        total -= f.size_bytes
    return to_delete


def run_gc(rrd_dir: Path, max_bytes: int, *, dry_run: bool = False) -> list[RrdFile]:
    """Scan `rrd_dir`, compute the deletion plan, and (unless `dry_run`) apply it.

    Returns the planned/deleted files in deletion order either way.
    """
    files = scan_rrd_dir(rrd_dir)
    to_delete = plan_deletions(files, max_bytes)
    if not dry_run:
        for f in to_delete:
            f.path.unlink(missing_ok=True)
    return to_delete


def _default_max_gb() -> float:
    from clankers.config import load_robots

    return float(load_robots().viz["rrd_cache_max_gb"])


def _default_rrd_dir() -> Path:
    # packages/clankers/src/clankers/viz/gc.py -> repo root is 5 levels up.
    return Path(__file__).resolve().parents[5] / "data" / "rrd"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GC the data/rrd/ cache (ADR-0003).")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, delete nothing")
    parser.add_argument("--rrd-dir", type=Path, default=None, help="override data/rrd/ location")
    parser.add_argument(
        "--max-gb", type=float, default=None, help="override viz.rrd_cache_max_gb from config"
    )
    args = parser.parse_args(argv)

    rrd_dir = args.rrd_dir if args.rrd_dir is not None else _default_rrd_dir()
    max_gb = args.max_gb if args.max_gb is not None else _default_max_gb()
    max_bytes = gb_to_bytes(max_gb)

    planned = run_gc(rrd_dir, max_bytes, dry_run=args.dry_run)

    verb = "would delete" if args.dry_run else "deleted"
    if not planned:
        print(f"{rrd_dir}: under {max_gb:g} GB cap, nothing to do")
        return 0
    for f in planned:
        tag = " [FAIL]" if f.is_fail else ""
        print(f"{verb} {f.path} ({f.size_bytes} bytes){tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
