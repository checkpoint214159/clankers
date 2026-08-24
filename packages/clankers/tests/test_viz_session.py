"""VizSession tests.

`mode="off"` must never call the rerun seam's import getter — proven here by
monkeypatching `clankers.viz.session.get_rerun` to blow up if it's ever invoked
(the "fake seam" technique), which works regardless of whether rerun-sdk happens to
be installed in the test environment.
"""

from __future__ import annotations

import sys

import pytest
from clankers.viz import VizSession
from clankers.viz.rr_backend import RerunNotInstalledError, get_rerun


def _forbid_get_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise AssertionError("get_rerun() must not be called when mode='off'")

    monkeypatch.setattr("clankers.viz.session.get_rerun", _boom)


def test_off_mode_never_imports_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_get_rerun(monkeypatch)

    session = VizSession(mode="off", session_name="unit-test")

    assert session.rrd_path is None


def test_off_mode_logging_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_get_rerun(monkeypatch)
    session = VizSession(mode="off", session_name="unit-test")

    session.log_joint_state("arm", ["joint1", "joint2"], [0.1, 0.2], targets=[0.15, 0.25], ts=0.0)
    session.log_health(
        "hand",
        servo_or_motor_id=3,
        name="thumb_mcp",
        temp_c=42.0,
        current_ma_or_torque=120.0,
        voltage_v=5.0,
        faults=["overtemp_motor"],
        ts=1.0,
    )
    session.log_event("session started", ts=0.0)
    session.close()
    # No exception, and nothing above touched the rerun seam.


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError):
        VizSession(mode="bogus", session_name="x")  # type: ignore[arg-type]


def test_log_joint_state_length_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_get_rerun(monkeypatch)
    session = VizSession(mode="off", session_name="unit-test")

    with pytest.raises(ValueError):
        session.log_joint_state("arm", ["joint1", "joint2"], [0.1], ts=0.0)

    with pytest.raises(ValueError):
        session.log_joint_state("arm", ["joint1", "joint2"], [0.1, 0.2], targets=[0.1], ts=0.0)


def test_get_rerun_raises_clear_error_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inserting None for a module name makes `import rerun` raise ImportError, without
    # needing rerun-sdk to actually be absent from this environment.
    monkeypatch.setitem(sys.modules, "rerun", None)

    with pytest.raises(RerunNotInstalledError, match="uv sync --extra viz"):
        get_rerun()


# --- integration: only runs if rerun-sdk is actually importable in this venv ---

rerun = pytest.importorskip("rerun")


def test_record_mode_writes_a_nonempty_rrd(tmp_path) -> None:
    session = VizSession(mode="record", session_name="itest", out_dir=tmp_path)
    try:
        session.log_joint_state(
            "arm",
            ["joint1", "joint2"],
            [0.1, 0.2],
            targets=[0.11, 0.21],
            ts=0.0,
        )
        session.log_health(
            "hand",
            servo_or_motor_id=7,
            name="index_mcp",
            temp_c=41.5,
            current_ma_or_torque=110.0,
            voltage_v=5.1,
            faults=["overload"],
            ts=0.1,
        )
        session.log_event("itest session", ts=0.0)
    finally:
        session.close()

    assert session.rrd_path is not None
    assert session.rrd_path.parent == tmp_path
    assert session.rrd_path.suffix == ".rrd"
    assert session.rrd_path.name.startswith("itest_")
    assert session.rrd_path.exists()
    assert session.rrd_path.stat().st_size > 0
