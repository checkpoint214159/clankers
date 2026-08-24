"""The rerun seam: isolates the optional `rerun-sdk` dependency.

Mirrors the dexmanip `sim_backend.py` pattern referenced in CLAUDE.md: exactly one
module imports the heavy/optional dependency, and only from a getter that fails with
a clear, actionable error when it's missing. Nothing else in `clankers.viz` imports
`rerun` directly.

`NullBackend` gives `VizSession` a real object to hold when `mode="off"`, so callers
never need an `if mode == "off"` branch around every log call, and no `rerun` import
is attempted for the common "viz disabled" path.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Protocol


class RerunNotInstalledError(RuntimeError):
    """`rerun-sdk` is required for this viz mode but is not installed in this venv."""


def get_rerun() -> ModuleType:
    """Lazily import and return the `rerun` module.

    Only call this from a code path that actually needs `mode != "off"` — importing
    `rerun` is what this seam exists to avoid on the disabled path.
    """
    try:
        import rerun
    except ImportError as exc:
        raise RerunNotInstalledError(
            "rerun-sdk is required for viz mode != 'off' but is not installed in "
            "this environment. Install it with: uv sync --extra viz"
        ) from exc
    return rerun


class Backend(Protocol):
    """Logging surface `VizSession` drives. `NullBackend` and `RerunBackend` both implement it."""

    def set_time(self, seconds: float) -> None: ...

    def log_scalar(self, entity_path: str, value: float) -> None: ...

    def log_text(self, entity_path: str, text: str, *, is_error: bool = False) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class NullBackend:
    """No-op backend for `mode="off"`. Touches no rerun state and imports nothing."""

    def set_time(self, seconds: float) -> None:
        return

    def log_scalar(self, entity_path: str, value: float) -> None:
        return

    def log_text(self, entity_path: str, text: str, *, is_error: bool = False) -> None:
        return

    def flush(self) -> None:
        return

    def close(self) -> None:
        return


class RerunBackend:
    """Wraps a live `rerun.RecordingStream`. Only constructed once `get_rerun()` succeeds."""

    def __init__(self, rerun_module: ModuleType, recording: Any) -> None:
        self._rr = rerun_module
        self._rec = recording

    def set_time(self, seconds: float) -> None:
        # "session" timeline: elapsed seconds since the caller's own session clock, not
        # wall-clock — the caller decides what `seconds` means and stays consistent.
        self._rec.set_time("session", duration=float(seconds))

    def log_scalar(self, entity_path: str, value: float) -> None:
        self._rec.log(entity_path, self._rr.Scalars(float(value)))

    def log_text(self, entity_path: str, text: str, *, is_error: bool = False) -> None:
        level = self._rr.TextLogLevel.ERROR if is_error else self._rr.TextLogLevel.INFO
        self._rec.log(entity_path, self._rr.TextLog(text, level=level))

    def flush(self) -> None:
        self._rec.flush()

    def close(self) -> None:
        self._rec.flush()
