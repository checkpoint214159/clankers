"""robots.yaml schema checks for `hand.operating_mode`."""

from __future__ import annotations

from pathlib import Path

import pytest
from clankers.config import load_robots
from clankers.config.loader import __file__ as loader_file

ROBOTS_YAML = Path(loader_file).with_name("robots.yaml")


def _with_hand_line(tmp_path: Path, old: str, new: str) -> Path:
    text = ROBOTS_YAML.read_text()
    assert text.count(old) == 1, f"robots.yaml no longer has exactly one {old!r}"
    out = tmp_path / "robots.yaml"
    out.write_text(text.replace(old, new))
    return out


def test_the_hand_runs_current_based_position() -> None:
    """Mode 5 is what makes current_limit_ma a real cap; mode 3 ignores it."""
    assert load_robots().hand.operating_mode == 5


def test_missing_operating_mode_means_the_factory_position_mode(tmp_path: Path) -> None:
    path = _with_hand_line(tmp_path, "  operating_mode: 5\n", "")
    assert load_robots(path).hand.operating_mode == 3


@pytest.mark.parametrize("mode", [0, 1, 4, 16])
def test_non_position_modes_are_rejected(tmp_path: Path, mode: int) -> None:
    """Every other mode would reinterpret the Goal_Position the gateway writes."""
    path = _with_hand_line(tmp_path, "  operating_mode: 5\n", f"  operating_mode: {mode}\n")
    with pytest.raises(ValueError, match="operating_mode"):
        load_robots(path)
