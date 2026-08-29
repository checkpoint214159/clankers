"""Landmarks -> 16 LEAP joint angles (pure numpy; no mediapipe/cv2 imports here).

Landmark layout is MediaPipe Hands' 21 points, shape (21, 3):
0 wrist; thumb 1-4 (CMC, MCP, IP, TIP); index 5-8 (MCP, PIP, DIP, TIP);
middle 9-12; ring 13-16; pinky 17-20. Angles are scale-invariant, so normalized
image coordinates work as well as metric ones.

Finger mapping (see docs/glossary.md — robots.yaml names inherit the dexmanip empirical
labelling, where the three straight fingers are called index/thumb/middle and the
OPPOSABLE finger is called "ring"):

    human index  -> robot "index"   (straight)
    human middle -> robot "thumb"   (straight, middle position — naming quirk)
    human ring   -> robot "middle"  (straight)
    human thumb  -> robot "ring"    (opposable)
    human pinky  -> unused (LEAP has no pinky)

PROVISIONAL like everything hand-side: bring-up (docs/plans/bringup.md) may flip signs or
reassign fingers; adjust FINGER_MAP / gains there, not by rewriting the math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from clankers.config import HandConfig, load_robots

# human finger name -> (robot finger prefix, landmark chain (mcp, pip, dip, tip))
FINGER_MAP: dict[str, tuple[str, tuple[int, int, int, int]]] = {
    "index": ("index", (5, 6, 7, 8)),
    "middle": ("thumb", (9, 10, 11, 12)),
    "ring": ("middle", (13, 14, 15, 16)),
}
THUMB_CHAIN = (1, 2, 3, 4)  # human thumb CMC, MCP, IP, TIP -> robot "ring" (opposable)

WRIST = 0
MIDDLE_MCP = 9
INDEX_MCP = 5
PINKY_MCP = 17


def _angle(v1: np.ndarray, v2: np.ndarray) -> float:
    n1 = float(np.linalg.norm(v1))
    n2 = float(np.linalg.norm(v2))
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cos = float(np.dot(v1, v2) / (n1 * n2))
    return math.acos(max(-1.0, min(1.0, cos)))


def _palm_normal(lm: np.ndarray) -> np.ndarray:
    n = np.cross(lm[INDEX_MCP] - lm[WRIST], lm[PINKY_MCP] - lm[WRIST])
    norm = float(np.linalg.norm(n))
    return n / norm if norm > 1e-9 else np.array([0.0, 0.0, 1.0])


def _project_onto_palm(v: np.ndarray, normal: np.ndarray) -> np.ndarray:
    return v - np.dot(v, normal) * normal


def _signed_angle_in_plane(v1: np.ndarray, v2: np.ndarray, normal: np.ndarray) -> float:
    p1 = _project_onto_palm(v1, normal)
    p2 = _project_onto_palm(v2, normal)
    ang = _angle(p1, p2)
    sign = 1.0 if float(np.dot(np.cross(p1, p2), normal)) >= 0 else -1.0
    return sign * ang


def raw_angles(lm: np.ndarray) -> dict[str, float]:
    """Extract raw (uncalibrated) angles for all 16 robot joints from one landmark set.

    Flexion at a knuckle = angle between the two adjacent bone vectors (0 when straight).
    Side-side / splay = signed in-palm-plane angle of the finger's base bone relative to
    the wrist->middle-MCP axis. Raw values carry each person's resting offsets — the
    Retargeter subtracts a captured neutral before mapping onto robot ranges.
    """
    lm = np.asarray(lm, dtype=float)
    if lm.shape != (21, 3):
        raise ValueError(f"expected (21, 3) landmarks, got {lm.shape}")
    normal = _palm_normal(lm)
    palm_axis = lm[MIDDLE_MCP] - lm[WRIST]

    out: dict[str, float] = {}
    for robot, (mcp, pip, dip, tip) in FINGER_MAP.values():
        out[f"{robot}_mcp_side"] = _signed_angle_in_plane(lm[pip] - lm[mcp], palm_axis, normal)
        out[f"{robot}_mcp_flex"] = _angle(lm[mcp] - lm[WRIST], lm[pip] - lm[mcp])
        out[f"{robot}_pip"] = _angle(lm[pip] - lm[mcp], lm[dip] - lm[pip])
        out[f"{robot}_dip"] = _angle(lm[dip] - lm[pip], lm[tip] - lm[dip])

    cmc, mcp, ip, tip = THUMB_CHAIN
    # Opposition/splay of the whole thumb relative to the palm axis drives the base
    # rotation; the three flexion knuckles map onto mcp/pip, with dip coupled to pip
    # (the human thumb has one fewer joint than the robot finger).
    out["ring_base_rot"] = abs(_signed_angle_in_plane(lm[mcp] - lm[WRIST], palm_axis, normal))
    out["ring_mcp_flex"] = _angle(lm[mcp] - lm[cmc], lm[ip] - lm[mcp])
    out["ring_pip"] = _angle(lm[ip] - lm[mcp], lm[tip] - lm[ip])
    out["ring_dip"] = 0.7 * out["ring_pip"]
    return out


@dataclass
class Retargeter:
    """Neutral-calibrated linear map from raw landmark angles to robot joint targets.

    target = clamp(gain * (raw - neutral), joint limits). Capture the neutral with an
    open, relaxed hand (`set_neutral`) — it zeroes out both the person's resting flexion
    and the splay offsets between fingers, so an open hand commands ~0 rad everywhere.
    """

    hand_cfg: HandConfig = field(default_factory=lambda: load_robots().hand)
    flex_gain: float = 1.1
    side_gain: float = 1.0
    thumb_gain: float = 1.2
    _neutral: dict[str, float] | None = None

    def __post_init__(self) -> None:
        self._limits = {j.name: j.limit for j in self.hand_cfg.joints}

    @property
    def has_neutral(self) -> bool:
        return self._neutral is not None

    def set_neutral(self, lm: np.ndarray) -> None:
        self._neutral = raw_angles(lm)

    def _gain_for(self, name: str) -> float:
        if name.startswith("ring_"):
            return self.thumb_gain
        if name.endswith("_mcp_side"):
            return self.side_gain
        return self.flex_gain

    def __call__(self, lm: np.ndarray) -> dict[str, float]:
        raw = raw_angles(lm)
        neutral = self._neutral or dict.fromkeys(raw, 0.0)
        out: dict[str, float] = {}
        for name, value in raw.items():
            mapped = self._gain_for(name) * (value - neutral[name])
            out[name] = self._limits[name].clamp(mapped)
        return out
