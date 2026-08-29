"""Synthetic MediaPipe-layout hand landmarks (test fixture + camera-free demo mode).

A toy right hand: palm in the x-y plane, fingers along +y, curl bends each finger
knuckle by `curl` radians into -z (toward the palm). Not anatomically exact — it exists
so the angle-extraction math and the full teleop loop can run deterministically with no
camera and no mediapipe install.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np

# (base x-offset, segment lengths) per finger, palm-width units.
_FINGERS: dict[str, tuple[float, tuple[float, float, float]]] = {
    "index": (-0.3, (0.35, 0.22, 0.15)),
    "middle": (0.0, (0.40, 0.25, 0.17)),
    "ring": (0.3, (0.35, 0.22, 0.15)),
    "pinky": (0.6, (0.28, 0.18, 0.12)),
}
_PALM_LEN = 1.0


def hand_landmarks(curl: float = 0.0, thumb_curl: float | None = None) -> np.ndarray:
    """Build (21, 3) landmarks. curl=0 is a flat open hand; curl~1.3 is a fist."""
    if thumb_curl is None:
        thumb_curl = curl
    lm = np.zeros((21, 3))
    lm[0] = (0.0, 0.0, 0.0)  # wrist

    chains = {"index": (5, 6, 7, 8), "middle": (9, 10, 11, 12), "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}
    for name, (x_off, segs) in _FINGERS.items():
        base = np.array([x_off, _PALM_LEN, 0.0])
        idx = chains[name]
        lm[idx[0]] = base
        pos = base.copy()
        pitch = 0.0  # accumulated bend away from +y into -z
        for i, seg in enumerate(segs):
            pitch += curl
            direction = np.array([0.0, math.cos(pitch), -math.sin(pitch)])
            pos = pos + seg * direction
            lm[idx[i + 1]] = pos

    # Thumb: splayed off to -x from a CMC near the wrist, curling toward the palm.
    cmc = np.array([-0.45, 0.25, 0.0])
    lm[1] = cmc
    pos = cmc.copy()
    splay = math.radians(55.0)
    pitch = 0.0
    for i, seg in enumerate((0.35, 0.28, 0.20)):
        pitch += thumb_curl
        direction = np.array(
            [-math.sin(splay) * math.cos(pitch), math.cos(splay) * math.cos(pitch), -math.sin(pitch)]
        )
        pos = pos + seg * direction
        lm[2 + i] = pos
    return lm


def wave(hz: float = 20.0, period_s: float = 4.0, max_curl: float = 1.1) -> Iterator[np.ndarray]:
    """Endless open<->curl wave at `hz` frames per second (one cycle per `period_s`)."""
    n = 0
    while True:
        phase = (n / hz) * (2.0 * math.pi / period_s)
        curl = max_curl * 0.5 * (1.0 - math.cos(phase))
        yield hand_landmarks(curl=curl)
        n += 1
