"""Webcam hand teleop (demo-grade).

Pipeline: camera frame -> hand landmarks (21 points, MediaPipe's layout, produced by
`landmarker.py` running MediaPipe's models on LiteRT) -> per-joint angle extraction ->
neutral-calibrated gain mapping onto the 16 LEAP joints -> `pos` op on the hand gateway
(:9003), which applies the full safety stack (limits, step clamp, watchdog).

This is joint-space RETARGETING, not IK: human and LEAP finger chains are similar enough
to map knuckle angles joint-to-joint. Fingertip-accurate IK retargeting (dex-retargeting)
is the roadmap upgrade, not this module.
"""

from .retarget import Retargeter

__all__ = ["Retargeter"]
