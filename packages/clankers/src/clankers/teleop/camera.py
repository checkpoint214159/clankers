"""MacBook-camera hand tracking behind a lazy seam (CLAUDE.md heavy-deps rule).

Importing this module is cheap; `CameraTracker` imports mediapipe + cv2 on construction
and raises a clear error pointing at `uv sync --extra teleop` when they're missing.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class CameraTracker:
    """One webcam + MediaPipe Hands. `read()` returns (landmarks | None, frame_bgr)."""

    def __init__(self, camera_index: int = 0, model_complexity: int = 1) -> None:
        try:
            import cv2
            import mediapipe as mp
        except ImportError as exc:
            raise ImportError(
                "teleop needs mediapipe + opencv. Run `uv sync --extra teleop`."
            ) from exc
        self._cv2 = cv2
        self._mp = mp
        self._cap = cv2.VideoCapture(camera_index)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"cannot open camera {camera_index}. On macOS, grant your terminal camera "
                "access in System Settings -> Privacy & Security -> Camera, then retry."
            )
        self._hands = mp.solutions.hands.Hands(
            max_num_hands=1,
            model_complexity=model_complexity,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )

    def read(self) -> tuple[np.ndarray | None, Any]:
        ok, frame = self._cap.read()
        if not ok:
            return None, None
        frame = self._cv2.flip(frame, 1)  # mirror: moving your hand right moves right on screen
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        result = self._hands.process(rgb)
        if not result.multi_hand_landmarks:
            return None, frame
        hand = result.multi_hand_landmarks[0]
        self._mp.solutions.drawing_utils.draw_landmarks(
            frame, hand, self._mp.solutions.hands.HAND_CONNECTIONS
        )
        lm = np.array([[p.x, p.y, p.z] for p in hand.landmark], dtype=float)
        return lm, frame

    def show(self, frame: Any, text: str) -> int:
        """Draw the HUD line, show the preview window, return the pressed key (or -1)."""
        if frame is None:
            return -1
        self._cv2.putText(
            frame, text, (10, 28), self._cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
        )
        self._cv2.imshow("clankers teleop", frame)
        return self._cv2.waitKey(1) & 0xFF

    def close(self) -> None:
        self._cap.release()
        self._hands.close()
        self._cv2.destroyAllWindows()
