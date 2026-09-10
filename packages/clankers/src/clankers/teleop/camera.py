"""One webcam + hand tracking behind a lazy seam (CLAUDE.md heavy-deps rule).

Importing this module is cheap; `CameraTracker` imports cv2 and builds the LiteRT
`HandLandmarker` on construction, raising a clear error pointing at
`uv sync --extra teleop` when they're missing.

The tracker is `clankers.teleop.landmarker`, which runs mediapipe's own hand models
without the mediapipe runtime -- see that module for why the runtime is unusable here.
Landmark output is unchanged (21 points, normalized), so `retarget.py` is untouched.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from clankers.teleop.landmarker import HandLandmarker

# mediapipe's HAND_CONNECTIONS, inlined so drawing needs no mediapipe.
BONES: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4),  # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),  # index
    (9, 10), (10, 11), (11, 12),  # middle
    (13, 14), (14, 15), (15, 16),  # ring
    (0, 17), (17, 18), (18, 19), (19, 20),  # pinky
    (5, 9), (9, 13), (13, 17),  # palm
)


class CameraTracker:
    """One webcam + hand tracking. `read()` returns (landmarks | None, frame_bgr)."""

    def __init__(self, camera_index: int = 0, num_threads: int = 4) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise ImportError(
                "teleop needs opencv + ai-edge-litert. Run `uv sync --extra teleop`."
            ) from exc
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(camera_index)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"cannot open camera {camera_index}. On Linux check `ls /dev/video*` and "
                "that no other process holds the device; on macOS grant your terminal "
                "camera access in System Settings -> Privacy & Security -> Camera."
            )
        self._tracker = HandLandmarker(num_threads=num_threads)

    @property
    def tracking(self) -> bool:
        """True while the hand is held frame-to-frame (the palm detector is skipped)."""
        return self._tracker.tracking

    def read(self) -> tuple[np.ndarray | None, Any]:
        ok, frame = self._cap.read()
        if not ok:
            return None, None
        frame = self._cv2.flip(frame, 1)  # mirror: moving your hand right moves right on screen
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        result = self._tracker.detect(rgb)
        if result is None:
            return None, frame
        self._draw(frame, result.landmarks)
        return result.landmarks, frame

    def _draw(self, frame: Any, lm: np.ndarray) -> None:
        h, w = frame.shape[:2]
        px = (lm[:, :2] * (w, h)).astype(int)
        for i, j in BONES:
            self._cv2.line(frame, tuple(px[i]), tuple(px[j]), (0, 255, 0), 2)
        for x, y in px:
            self._cv2.circle(frame, (int(x), int(y)), 4, (0, 0, 255), -1)

    def show(self, frame: Any, text: str) -> int:
        """Draw the HUD line, show the preview window, return the pressed key (or -1)."""
        if frame is None:
            return -1
        # Dark band behind the text rather than a stroke+fill pair: cv2 5.0 changed putText
        # glyph advance with thickness, so the two copies of a stroked string drift apart
        # and every line renders doubled (see CLAUDE.md, and scripts/camera_debug.py).
        band = frame[0:40, :]
        self._cv2.addWeighted(band, 0.35, np.zeros_like(band), 0.0, 0.0, dst=band)
        self._cv2.putText(
            frame, text, (10, 28), self._cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
        )
        self._cv2.imshow("clankers teleop", frame)
        return self._cv2.waitKey(1) & 0xFF

    def close(self) -> None:
        self._cap.release()
        self._cv2.destroyAllWindows()
