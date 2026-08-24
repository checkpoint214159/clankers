"""VizSession: the one toggleable rerun entry point (ADR-0003).

Every process that logs telemetry — hand gateway, arm gateway, task runner, record
mode — owns exactly one `VizSession` and logs through it. `mode="off"` is a hard
no-op: it never imports `rerun` (see `rr_backend.get_rerun`), so processes and tests
that never enable viz don't need rerun-sdk installed.

Entity schema (LeRobot-compatible, ADR-0003):
    observation.joint_state.<side>/<name>   scalar, measured position
    action.<side>/<name>                    scalar, commanded target
    telemetry/<side>/<name>/temp            scalar, °C
    telemetry/<side>/<name>/current         scalar, mA (hand) or torque (arm)
    telemetry/<side>/<name>/voltage         scalar, V
    telemetry/<side>/<name>/fault           TextLog, only logged when faults are present
    events                                  TextLog, free-form session narration
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .rr_backend import Backend, NullBackend, RerunBackend, get_rerun

Mode = Literal["off", "live", "record", "both"]
Side = Literal["arm", "hand"]

_VALID_MODES: tuple[Mode, ...] = ("off", "live", "record", "both")


def default_rrd_dir() -> Path:
    """`data/rrd/` relative to the repo root."""
    # packages/clankers/src/clankers/viz/session.py -> repo root is 5 levels up.
    return Path(__file__).resolve().parents[5] / "data" / "rrd"


class VizSession:
    """One rerun recording for the lifetime of a debug/teleop/record/infer/task session."""

    def __init__(
        self,
        mode: Mode,
        session_name: str,
        out_dir: Path | None = None,
        *,
        memory_limit: str = "25%",
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid viz mode {mode!r}, must be one of {_VALID_MODES}")

        self.mode: Mode = mode
        self.session_name = session_name
        self.rrd_path: Path | None = None
        self._backend: Backend

        if mode == "off":
            self._backend = NullBackend()
            return

        rr = get_rerun()
        recording = rr.RecordingStream(application_id=session_name)

        sinks: list[object] = []
        if mode in ("live", "both"):
            # Launch (but don't yet connect) a viewer process, then wire it up via an
            # explicit GrpcSink below so it sits alongside a FileSink under set_sinks().
            recording.spawn(connect=False, memory_limit=memory_limit)
            sinks.append(rr.GrpcSink())
        if mode in ("record", "both"):
            resolved_dir = out_dir if out_dir is not None else default_rrd_dir()
            resolved_dir.mkdir(parents=True, exist_ok=True)
            # UTC to keep filenames unambiguous regardless of the host's local timezone.
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
            self.rrd_path = resolved_dir / f"{session_name}_{timestamp}.rrd"
            sinks.append(rr.FileSink(str(self.rrd_path)))

        recording.set_sinks(*sinks)
        self._backend = RerunBackend(rr, recording)

    def log_joint_state(
        self,
        side: Side,
        names: list[str],
        positions: list[float],
        targets: list[float] | None = None,
        *,
        ts: float,
    ) -> None:
        """Log measured positions (and optionally commanded targets) for one side."""
        if len(positions) != len(names):
            raise ValueError("names and positions must be the same length")
        if targets is not None and len(targets) != len(names):
            raise ValueError("names and targets must be the same length")

        self._backend.set_time(ts)
        for name, position in zip(names, positions):
            self._backend.log_scalar(f"observation.joint_state.{side}/{name}", position)
        if targets is not None:
            for name, target in zip(names, targets):
                self._backend.log_scalar(f"action.{side}/{name}", target)

    def log_health(
        self,
        side: Side,
        servo_or_motor_id: int,
        name: str,
        temp_c: float,
        current_ma_or_torque: float,
        voltage_v: float,
        faults: list[str],
        *,
        ts: float,
    ) -> None:
        """Log per-joint health telemetry; faults only produce a TextLog when non-empty."""
        self._backend.set_time(ts)
        base = f"telemetry/{side}/{name}"
        self._backend.log_scalar(f"{base}/temp", temp_c)
        self._backend.log_scalar(f"{base}/current", current_ma_or_torque)
        self._backend.log_scalar(f"{base}/voltage", voltage_v)
        if faults:
            text = f"id={servo_or_motor_id}: {', '.join(faults)}"
            self._backend.log_text(f"{base}/fault", text, is_error=True)

    def log_event(self, text: str, *, ts: float) -> None:
        """Free-form session narration (mode changes, operator acks, session start/end)."""
        self._backend.set_time(ts)
        self._backend.log_text("events", text)

    def close(self) -> None:
        self._backend.close()
