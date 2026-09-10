"""MediaPipe's hand models under LiteRT -- the tracker without the mediapipe runtime.

Why not mediapipe itself: 1.0.1 is the only release with a linux-aarch64 wheel, and that
binary is compiled with the ARMv8 crypto extensions enabled. This Pi's Cortex-A72 has no
`aes` in /proc/cpuinfo Features, so the very first call aborts the process:

    FATAL ERROR: This binary was compiled with aes enabled, but this feature is not
    available on this processor (go/sigill-fail-fast).

The *models* are plain .tflite and have no such problem. So this module pulls the two
models out of mediapipe's own `hand_landmarker.task` bundle (a zip), runs them on
ai-edge-litert, and reimplements the graph glue mediapipe would have done around them:
SSD anchor decode, palm -> hand ROI, rotated crop, landmark decode, and ROI carry-over
between frames.

Measured on this Pi 4 at 4 threads: palm detector ~61 ms, landmark model ~45 ms. The palm
detector only reruns when tracking is lost, so steady-state tracking is the landmark model
alone, ~22 fps.

Landmark layout is MediaPipe Hands' 21 points -- exactly what `retarget.py` consumes.
"""

from __future__ import annotations

import math
import os
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

BUNDLE_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/"
    "float16/1/hand_landmarker.task"
)
BUNDLE_NAME = "hand_landmarker.task"
DETECTOR_TFLITE = "hand_detector.tflite"
LANDMARK_TFLITE = "hand_landmarks_detector.tflite"

DETECTOR_SIZE = 192  # hand_detector.tflite input is 1x192x192x3
LANDMARK_SIZE = 224  # hand_landmarks_detector.tflite input is 1x224x224x3
NUM_LANDMARKS = 21

# SsdAnchorsCalculator options for the 192x192 palm detector. These four strides produce
# 24*24*2 + 3*(12*12*2) = 2016 anchors, which is exactly the model's box-output rows.
_STRIDES = (8, 16, 16, 16)
_MIN_SCALE = 0.1484375
_MAX_SCALE = 0.75
_ANCHOR_OFFSET = 0.5

# Palm detection -> hand ROI (mediapipe palm_detection_detection_to_roi.pbtxt).
_PALM_ROT_START_KP = 0  # center of wrist
_PALM_ROT_END_KP = 2  # MCP of middle finger
_PALM_SCALE = 2.6
_PALM_SHIFT_Y = -0.5

# Landmarks -> next-frame ROI (mediapipe hand_landmark_landmarks_to_roi.pbtxt).
_LM_ROT_START_KP = 0  # wrist
_LM_ROT_END_KP = 9  # middle finger MCP
_LM_SCALE = 2.0

_TARGET_ANGLE = math.pi / 2  # rotation_vector_target_angle_degrees: 90


def model_dir() -> Path:
    """Where the .task bundle and the extracted models live ($CLANKERS_MODEL_DIR wins)."""
    env = os.environ.get("CLANKERS_MODEL_DIR")
    return Path(env) if env else Path.home() / ".cache" / "clankers" / "models"


def ensure_models(*, download: bool = True) -> tuple[Path, Path]:
    """Return (detector, landmark) tflite paths, fetching the bundle once if needed."""
    d = model_dir()
    detector, landmark = d / DETECTOR_TFLITE, d / LANDMARK_TFLITE
    if detector.exists() and landmark.exists():
        return detector, landmark
    if not download:
        raise FileNotFoundError(
            f"hand models missing under {d}. Run with download enabled, or fetch "
            f"{BUNDLE_URL} to {d / BUNDLE_NAME} and unzip it there."
        )
    d.mkdir(parents=True, exist_ok=True)
    bundle = d / BUNDLE_NAME
    if not bundle.exists():
        tmp = bundle.with_suffix(".part")
        urllib.request.urlretrieve(BUNDLE_URL, tmp)
        tmp.replace(bundle)
    with zipfile.ZipFile(bundle) as z:
        names = set(z.namelist())
        missing = {DETECTOR_TFLITE, LANDMARK_TFLITE} - names
        if missing:
            raise RuntimeError(f"{bundle} is missing {sorted(missing)}; delete it and retry")
        z.extract(DETECTOR_TFLITE, d)
        z.extract(LANDMARK_TFLITE, d)
    return detector, landmark


def _interpreter(path: Path, num_threads: int) -> Any:
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError as exc:
        raise ImportError(
            "hand tracking needs ai-edge-litert. Run `uv sync --extra teleop`."
        ) from exc
    it = Interpreter(model_path=str(path), num_threads=num_threads)
    it.allocate_tensors()
    return it


def ssd_anchors() -> np.ndarray:
    """(N, 2) anchor centers in [0,1]. Sizes are fixed at 1.0, so only centers matter."""
    centers: list[tuple[float, float]] = []
    layer = 0
    n_layers = len(_STRIDES)
    while layer < n_layers:
        last = layer
        n_anchors_per_loc = 0
        while last < n_layers and _STRIDES[last] == _STRIDES[layer]:
            # one anchor for aspect_ratios: [1.0], plus one for the interpolated scale
            n_anchors_per_loc += 2
            last += 1
        fm = math.ceil(DETECTOR_SIZE / _STRIDES[layer])
        for y in range(fm):
            for x in range(fm):
                cx, cy = (x + _ANCHOR_OFFSET) / fm, (y + _ANCHOR_OFFSET) / fm
                centers.extend([(cx, cy)] * n_anchors_per_loc)
        layer = last
    return np.array(centers, dtype=np.float32)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -100.0, 100.0)))


def _rotation(start: np.ndarray, end: np.ndarray) -> float:
    """MediaPipe's DetectionsToRects angle: y is down, hence the negated dy."""
    return _TARGET_ANGLE - math.atan2(-(end[1] - start[1]), end[0] - start[0])


def _transform_rect(
    cx: float, cy: float, w: float, h: float, rot: float, *, scale: float, shift_y: float
) -> tuple[float, float, float, float, float]:
    """RectTransformationCalculator: shift along the rotated axes, square_long, then scale."""
    if shift_y:
        cx += -shift_y * h * math.sin(rot)
        cy += shift_y * h * math.cos(rot)
    side = max(w, h) * scale
    return cx, cy, side, side, rot


def _crop_matrix(roi: tuple[float, float, float, float, float]) -> np.ndarray:
    """2x3 mapping crop pixel -> image pixel (mediapipe's rotated-subrect transform)."""
    cx, cy, w, h, rot = roi
    c, s = math.cos(rot), math.sin(rot)
    return np.array(
        [
            [w * c / LANDMARK_SIZE, -h * s / LANDMARK_SIZE, cx - 0.5 * w * c + 0.5 * h * s],
            [w * s / LANDMARK_SIZE, h * c / LANDMARK_SIZE, cy - 0.5 * w * s - 0.5 * h * c],
        ],
        dtype=np.float32,
    )


@dataclass(frozen=True)
class HandResult:
    """One tracked hand.

    `landmarks` keeps the old mediapipe contract `retarget.py` was tuned against: x and y
    normalized by image width and height separately, z on the same scale as x. `world`
    is the model's metric, wrist-origin output -- aspect-correct and the better input for
    angle extraction, but not what the current retargeter expects.
    """

    landmarks: np.ndarray  # (21, 3) normalized image coords
    world: np.ndarray  # (21, 3) metres, wrist-origin
    score: float  # hand-presence
    handedness: float  # >0.5 == right hand in the model's (unmirrored) view


class HandLandmarker:
    """Two-stage hand tracker: palm detector on acquisition, landmarks every frame."""

    def __init__(
        self,
        *,
        num_threads: int = 4,
        min_detection_score: float = 0.5,
        min_presence_score: float = 0.5,
        download: bool = True,
    ) -> None:
        detector_path, landmark_path = ensure_models(download=download)
        self._detector = _interpreter(detector_path, num_threads)
        self._landmark = _interpreter(landmark_path, num_threads)
        self._anchors = ssd_anchors()
        self._min_detection = min_detection_score
        self._min_presence = min_presence_score
        self._roi: tuple[float, float, float, float, float] | None = None
        try:
            import cv2
        except ImportError as exc:
            raise ImportError(
                "hand tracking needs opencv. Run `uv sync --extra teleop`."
            ) from exc
        self._cv2 = cv2

    @property
    def tracking(self) -> bool:
        """True when the last frame held the hand, so the palm detector can be skipped."""
        return self._roi is not None

    def reset(self) -> None:
        self._roi = None

    def detect(self, rgb: np.ndarray) -> HandResult | None:
        """Track one hand in an RGB uint8 frame. Returns None when no hand is found."""
        if self._roi is None:
            self._roi = self._detect_palm(rgb)
            if self._roi is None:
                return None
        result = self._run_landmarks(rgb, self._roi)
        if result is None:
            # Tracking lost. Retry once from a fresh palm detection on this same frame.
            self._roi = self._detect_palm(rgb)
            if self._roi is None:
                return None
            result = self._run_landmarks(rgb, self._roi)
            if result is None:
                self._roi = None
                return None
        self._roi = self._roi_from_landmarks(result.landmarks, rgb.shape[1], rgb.shape[0])
        return result

    def _detect_palm(
        self, rgb: np.ndarray
    ) -> tuple[float, float, float, float, float] | None:
        h, w = rgb.shape[:2]
        scale = min(DETECTOR_SIZE / w, DETECTOR_SIZE / h)
        new_w, new_h = round(w * scale), round(h * scale)
        pad_x, pad_y = (DETECTOR_SIZE - new_w) // 2, (DETECTOR_SIZE - new_h) // 2
        canvas = np.zeros((DETECTOR_SIZE, DETECTOR_SIZE, 3), dtype=np.uint8)
        canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = self._cv2.resize(
            rgb, (new_w, new_h), interpolation=self._cv2.INTER_LINEAR
        )

        inp = self._detector.get_input_details()[0]
        self._detector.set_tensor(
            inp["index"], (canvas.astype(np.float32) / 255.0)[None, ...]
        )
        self._detector.invoke()
        out = self._detector.get_output_details()
        raw = self._detector.get_tensor(out[0]["index"])[0]  # (2016, 18)
        scores = _sigmoid(self._detector.get_tensor(out[1]["index"])[0, :, 0])

        best = int(np.argmax(scores))
        if scores[best] < self._min_detection:
            return None

        # Decode against the anchor. reverse_output_order: raw is (x, y, w, h, kp...),
        # and every value is in DETECTOR_SIZE units relative to the anchor centre.
        ax, ay = self._anchors[best]
        box = raw[best] / DETECTOR_SIZE
        cx_n, cy_n = box[0] + ax, box[1] + ay
        w_n, h_n = box[2], box[3]
        kp = box[4 : 4 + 2 * 7].reshape(7, 2) + (ax, ay)

        # Undo the letterbox, back into original-image pixels.
        def to_px(xy: np.ndarray) -> np.ndarray:
            return np.stack(
                [
                    (xy[..., 0] * DETECTOR_SIZE - pad_x) / scale,
                    (xy[..., 1] * DETECTOR_SIZE - pad_y) / scale,
                ],
                axis=-1,
            )

        centre = to_px(np.array([cx_n, cy_n], dtype=np.float32))
        kp_px = to_px(kp)
        box_w = w_n * DETECTOR_SIZE / scale
        box_h = h_n * DETECTOR_SIZE / scale
        rot = _rotation(kp_px[_PALM_ROT_START_KP], kp_px[_PALM_ROT_END_KP])
        return _transform_rect(
            float(centre[0]),
            float(centre[1]),
            float(box_w),
            float(box_h),
            rot,
            scale=_PALM_SCALE,
            shift_y=_PALM_SHIFT_Y,
        )

    def _run_landmarks(
        self, rgb: np.ndarray, roi: tuple[float, float, float, float, float]
    ) -> HandResult | None:
        h, w = rgb.shape[:2]
        m = _crop_matrix(roi)
        crop = self._cv2.warpAffine(
            rgb,
            m,
            (LANDMARK_SIZE, LANDMARK_SIZE),
            flags=self._cv2.INTER_LINEAR | self._cv2.WARP_INVERSE_MAP,
        )

        inp = self._landmark.get_input_details()[0]
        self._landmark.set_tensor(inp["index"], (crop.astype(np.float32) / 255.0)[None, ...])
        self._landmark.invoke()
        out = self._landmark.get_output_details()
        lm_crop = self._landmark.get_tensor(out[0]["index"]).reshape(NUM_LANDMARKS, 3)
        presence = float(_sigmoid(self._landmark.get_tensor(out[1]["index"])).ravel()[0])
        handedness = float(_sigmoid(self._landmark.get_tensor(out[2]["index"])).ravel()[0])
        world = self._landmark.get_tensor(out[3]["index"]).reshape(NUM_LANDMARKS, 3)

        if presence < self._min_presence:
            return None

        # Crop pixels -> image pixels -> the normalized coords retarget.py expects.
        xy = lm_crop[:, :2] @ m[:, :2].T + m[:, 2]
        # z shares x's scale: crop-relative in, image-relative out.
        z = lm_crop[:, 2] * (roi[2] / LANDMARK_SIZE) / w
        landmarks = np.stack([xy[:, 0] / w, xy[:, 1] / h, z], axis=-1).astype(float)
        return HandResult(
            landmarks=landmarks, world=world.astype(float), score=presence, handedness=handedness
        )

    @staticmethod
    def _roi_from_landmarks(
        landmarks: np.ndarray, width: int, height: int
    ) -> tuple[float, float, float, float, float]:
        px = landmarks[:, :2] * (width, height)
        lo, hi = px.min(axis=0), px.max(axis=0)
        cx, cy = (lo + hi) / 2.0
        rot = _rotation(px[_LM_ROT_START_KP], px[_LM_ROT_END_KP])
        return _transform_rect(
            float(cx),
            float(cy),
            float(hi[0] - lo[0]),
            float(hi[1] - lo[1]),
            rot,
            scale=_LM_SCALE,
            shift_y=0.0,
        )
