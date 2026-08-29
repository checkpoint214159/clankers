"""Tests for the teleop retargeting math (pure numpy — no mediapipe/cv2 needed)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from clankers.config import load_robots
from clankers.teleop.retarget import Retargeter, raw_angles
from clankers.teleop.synthetic import hand_landmarks, wave

CFG = load_robots()
JOINT_NAMES = {j.name for j in CFG.hand.joints}
FLEX_JOINTS = [n for n in JOINT_NAMES if n.endswith(("_mcp_flex", "_pip", "_dip"))]


def test_raw_angles_covers_all_16_joints_and_is_finite() -> None:
    out = raw_angles(hand_landmarks(curl=0.5))
    assert set(out) == JOINT_NAMES
    assert all(math.isfinite(v) for v in out.values())


def test_open_hand_after_neutral_maps_to_near_zero() -> None:
    r = Retargeter(hand_cfg=CFG.hand)
    open_lm = hand_landmarks(curl=0.0)
    r.set_neutral(open_lm)
    out = r(open_lm)
    for name, value in out.items():
        assert abs(value) < 1e-6, (name, value)


def test_curl_increases_flexion_monotonically() -> None:
    r = Retargeter(hand_cfg=CFG.hand)
    r.set_neutral(hand_landmarks(curl=0.0))
    prev_total = -1.0
    for curl in (0.2, 0.5, 0.8, 1.1):
        out = r(hand_landmarks(curl=curl))
        total = sum(out[n] for n in FLEX_JOINTS)
        assert total > prev_total, f"flexion did not increase at curl={curl}"
        prev_total = total
    # A strong curl should meaningfully flex every straight-finger PIP joint.
    strong = r(hand_landmarks(curl=1.1))
    for name in ("index_pip", "middle_pip", "ring_pip"):
        assert strong[name] > 0.5, (name, strong[name])


def test_outputs_always_within_joint_limits() -> None:
    r = Retargeter(hand_cfg=CFG.hand)
    r.set_neutral(hand_landmarks(curl=0.0))
    limits = {j.name: j.limit for j in CFG.hand.joints}
    for curl in np.linspace(0.0, 1.6, 9):  # includes curls beyond any human range
        out = r(hand_landmarks(curl=float(curl)))
        for name, value in out.items():
            lim = limits[name]
            assert lim.min <= value <= lim.max, (name, value)


def test_bad_landmark_shape_raises() -> None:
    with pytest.raises(ValueError, match="21, 3"):
        raw_angles(np.zeros((20, 3)))


def test_wave_generator_cycles_open_and_curled() -> None:
    frames = [lm for lm, _ in zip(wave(hz=20, period_s=2.0), range(40), strict=False)]
    totals = [sum(raw_angles(f)[n] for n in FLEX_JOINTS) for f in frames]
    assert max(totals) > min(totals) + 1.0  # one full open<->curl cycle happened
