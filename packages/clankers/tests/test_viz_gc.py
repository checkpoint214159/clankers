"""rrd cache GC tests (ADR-0003): oldest-first, FAIL files retained longest."""

from __future__ import annotations

import os
import time
from pathlib import Path

from clankers.viz.gc import RrdFile, gb_to_bytes, plan_deletions, run_gc, scan_rrd_dir


def _make_rrd(dir_path: Path, name: str, size_bytes: int, age_s: float) -> Path:
    """Write a fake .rrd file `age_s` seconds older than now."""
    path = dir_path / name
    path.write_bytes(b"\0" * size_bytes)
    mtime = time.time() - age_s
    os.utime(path, (mtime, mtime))
    return path


def test_gb_to_bytes_is_decimal() -> None:
    assert gb_to_bytes(1) == 1_000_000_000
    assert gb_to_bytes(0.5) == 500_000_000


def test_scan_rrd_dir_reads_size_and_mtime(tmp_path: Path) -> None:
    _make_rrd(tmp_path, "a.rrd", 100, age_s=10)
    (tmp_path / "not_rrd.txt").write_text("ignore me")

    files = scan_rrd_dir(tmp_path)

    assert len(files) == 1
    assert files[0].path.name == "a.rrd"
    assert files[0].size_bytes == 100


def test_scan_rrd_dir_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert scan_rrd_dir(tmp_path / "does-not-exist") == []


def test_plan_deletions_noop_under_threshold() -> None:
    files = [RrdFile(Path("a.rrd"), size_bytes=100, mtime=1.0)]
    assert plan_deletions(files, max_bytes=1000) == []


def test_plan_deletions_oldest_non_fail_first() -> None:
    old = RrdFile(Path("old.rrd"), size_bytes=100, mtime=1.0)
    mid = RrdFile(Path("mid.rrd"), size_bytes=100, mtime=2.0)
    new = RrdFile(Path("new.rrd"), size_bytes=100, mtime=3.0)
    # total 300, cap 150 -> must delete oldest two to get to 100 (<=150).
    plan = plan_deletions([new, old, mid], max_bytes=150)

    assert [f.path.name for f in plan] == ["old.rrd", "mid.rrd"]


def test_plan_deletions_fail_files_deleted_only_after_all_non_fail(tmp_path: Path) -> None:
    # A FAIL file older than everything else must still survive until non-FAIL files
    # are exhausted.
    ancient_fail = RrdFile(Path("s_FAIL.rrd"), size_bytes=100, mtime=0.0)
    old = RrdFile(Path("old.rrd"), size_bytes=100, mtime=1.0)
    new = RrdFile(Path("new.rrd"), size_bytes=100, mtime=2.0)

    # total 300, cap 250 -> only need to free 50, so delete the single oldest non-FAIL.
    plan = plan_deletions([ancient_fail, new, old], max_bytes=250)
    assert [f.path.name for f in plan] == ["old.rrd"]

    # cap so small that even both non-FAIL files aren't enough -> FAIL file deleted last.
    plan_all = plan_deletions([ancient_fail, new, old], max_bytes=50)
    assert [f.path.name for f in plan_all] == ["old.rrd", "new.rrd", "s_FAIL.rrd"]


def test_plan_deletions_respects_byte_threshold_exactly() -> None:
    files = [
        RrdFile(Path("a.rrd"), size_bytes=100, mtime=1.0),
        RrdFile(Path("b.rrd"), size_bytes=100, mtime=2.0),
    ]
    # total 200, cap 100 -> deleting just "a" brings total to 100, which satisfies <=.
    plan = plan_deletions(files, max_bytes=100)
    assert [f.path.name for f in plan] == ["a.rrd"]


def test_run_gc_dry_run_does_not_delete(tmp_path: Path) -> None:
    _make_rrd(tmp_path, "old.rrd", 100, age_s=10)
    _make_rrd(tmp_path, "new.rrd", 100, age_s=1)

    plan = run_gc(tmp_path, max_bytes=100, dry_run=True)

    assert [f.path.name for f in plan] == ["old.rrd"]
    assert (tmp_path / "old.rrd").exists()  # dry-run: nothing actually removed


def test_run_gc_deletes_when_not_dry_run(tmp_path: Path) -> None:
    _make_rrd(tmp_path, "old.rrd", 100, age_s=10)
    _make_rrd(tmp_path, "new.rrd", 100, age_s=1)

    plan = run_gc(tmp_path, max_bytes=100, dry_run=False)

    assert [f.path.name for f in plan] == ["old.rrd"]
    assert not (tmp_path / "old.rrd").exists()
    assert (tmp_path / "new.rrd").exists()


def test_main_dry_run_cli(tmp_path: Path, capsys) -> None:
    from clankers.viz.gc import main

    _make_rrd(tmp_path, "old_FAIL.rrd", 100, age_s=10)
    _make_rrd(tmp_path, "new.rrd", 100, age_s=1)

    exit_code = main(["--dry-run", "--rrd-dir", str(tmp_path), "--max-gb", "0"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "would delete" in out
    assert "new.rrd" in out
    # both files present after a dry run
    assert (tmp_path / "old_FAIL.rrd").exists()
    assert (tmp_path / "new.rrd").exists()
