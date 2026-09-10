"""Tests for the LiteRT hand landmarker's graph glue.

The geometry here is a reimplementation of mediapipe calculators, so these tests pin the
invariants that would silently mis-place the crop if a sign or an axis flipped -- the
failure mode is not a crash but subtly wrong finger angles.

Everything below the real-model test is pure numpy: no litert, no cv2, no model download,
so it runs on the light test path (CLAUDE.md).
"""

from __future__ import annotations

import math
import os

import numpy as np
import pytest
from clankers.teleop import landmarker as L

# The palm detector's box output is (2016, 18); anchors must line up row-for-row.
MODEL_ANCHOR_ROWS = 2016


def test_anchor_count_matches_the_models_box_rows() -> None:
    a = L.ssd_anchors()
    assert a.shape == (MODEL_ANCHOR_ROWS, 2)


def test_anchor_centres_are_inside_the_unit_square() -> None:
    a = L.ssd_anchors()
    assert (a > 0.0).all() and (a < 1.0).all()


def test_first_layer_is_a_24x24_grid_with_two_anchors_per_cell() -> None:
    a = L.ssd_anchors()
    stride8 = a[: 24 * 24 * 2]
    assert np.allclose(stride8[0], (0.5 / 24, 0.5 / 24))
    # consecutive pairs share a cell, and x advances one cell per pair
    assert np.allclose(stride8[0], stride8[1])
    assert np.allclose(stride8[2] - stride8[0], (1 / 24, 0.0))
    # the remainder is three stacked 12x12 layers, 2 anchors each
    assert len(a) - len(stride8) == 3 * 12 * 12 * 2


def test_upright_hand_has_zero_rotation() -> None:
    """Wrist below, middle-MCP above (y is down) is mediapipe's 90-degree target pose."""
    assert L._rotation(np.array([0.0, 100.0]), np.array([0.0, 0.0])) == pytest.approx(0.0)


@pytest.mark.parametrize("deg", [0.0, 30.0, 90.0, -45.0, 180.0])
def test_crop_up_axis_follows_the_wrist_to_knuckle_vector(deg: float) -> None:
    """The crop's "up" must point along wrist->MCP, whatever the hand's roll.

    This is the invariant that keeps the landmark model seeing an upright hand; get the
    sign wrong and it still returns 21 plausible points, just for a rotated crop.
    """
    theta = math.radians(deg)
    start = np.array([50.0, 50.0])
    # image y is down, so a "screen up" vector at angle theta is (sin, -cos)
    direction = np.array([math.sin(theta), -math.cos(theta)])
    end = start + 100.0 * direction

    rot = L._rotation(start, end)
    roi = L._transform_rect(50.0, 50.0, 40.0, 40.0, rot, scale=2.0, shift_y=0.0)
    m = L._crop_matrix(roi)

    def to_image(px: float, py: float) -> np.ndarray:
        return m[:, :2] @ (px, py) + m[:, 2]

    half = L.LANDMARK_SIZE / 2.0
    up = to_image(half, 0.0) - to_image(half, half)
    up /= np.linalg.norm(up)
    assert up == pytest.approx(direction, abs=1e-5)


def test_crop_matrix_maps_crop_centre_to_roi_centre() -> None:
    roi = (120.0, 80.0, 60.0, 60.0, 0.7)
    m = L._crop_matrix(roi)
    half = L.LANDMARK_SIZE / 2.0
    centre = m[:, :2] @ (half, half) + m[:, 2]
    assert centre == pytest.approx((120.0, 80.0))


def test_crop_matrix_spans_the_full_roi_side() -> None:
    roi = (0.0, 0.0, 64.0, 64.0, 0.0)
    m = L._crop_matrix(roi)
    left = m[:, :2] @ (0.0, 0.0) + m[:, 2]
    right = m[:, :2] @ (float(L.LANDMARK_SIZE), 0.0) + m[:, 2]
    assert float(np.linalg.norm(right - left)) == pytest.approx(64.0)


def test_transform_rect_squares_the_long_side_then_scales() -> None:
    _, _, w, h, _ = L._transform_rect(0.0, 0.0, 10.0, 40.0, 0.0, scale=2.0, shift_y=0.0)
    assert (w, h) == pytest.approx((80.0, 80.0))


def test_transform_rect_shift_is_along_the_rotated_axis() -> None:
    """shift_y -0.5 must walk toward the knuckles, i.e. along the crop's own up axis."""
    cx, cy, _, _, _ = L._transform_rect(
        0.0, 0.0, 40.0, 40.0, 0.0, scale=1.0, shift_y=L._PALM_SHIFT_Y
    )
    assert (cx, cy) == pytest.approx((0.0, -20.0))  # straight up in an upright crop

    rot = math.pi / 2  # hand rolled so "up" is +x
    cx, cy, _, _, _ = L._transform_rect(
        0.0, 0.0, 40.0, 40.0, rot, scale=1.0, shift_y=L._PALM_SHIFT_Y
    )
    assert (cx, cy) == pytest.approx((20.0, 0.0), abs=1e-9)


def test_roi_from_landmarks_covers_the_hand() -> None:
    rng = np.random.default_rng(0)
    lm = np.zeros((L.NUM_LANDMARKS, 3))
    lm[:, :2] = rng.uniform(0.3, 0.6, size=(L.NUM_LANDMARKS, 2))
    roi = L.HandLandmarker._roi_from_landmarks(lm, 640, 480)
    px = lm[:, :2] * (640, 480)
    span = float(np.abs(px - px.mean(axis=0)).max()) * 2
    assert roi[2] > span  # scale 2.0 on the long side always encloses the points


def test_model_dir_honours_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLANKERS_MODEL_DIR", "/tmp/does-not-exist-clankers")
    assert str(L.model_dir()) == "/tmp/does-not-exist-clankers"


def test_missing_models_without_download_say_where_to_get_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CLANKERS_MODEL_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError) as exc:
        L.ensure_models(download=False)
    assert L.BUNDLE_URL in str(exc.value)


def _models_available() -> bool:
    try:
        import ai_edge_litert  # noqa: F401
        import cv2  # noqa: F401
    except ImportError:
        return False
    d = L.model_dir()
    return (d / L.DETECTOR_TFLITE).exists() and (d / L.LANDMARK_TFLITE).exists()


@pytest.mark.skipif(
    not _models_available(),
    reason="needs `uv sync --extra teleop` and a cached hand_landmarker.task",
)
def test_landmark_model_wiring_against_the_real_model() -> None:
    """Pin the decode contract against the real tflite: shapes, sigmoids, crop mapping.

    Deliberately not a detection-quality test -- it forces a known ROI and accepts any
    presence score, so it fails only if OUR tensor unpacking or coordinate mapping is
    wrong. Detection quality is exercised by test_real_photo_tracks below.
    """
    import cv2  # noqa: F401

    rng = np.random.default_rng(0)
    rgb = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    tracker = L.HandLandmarker(download=False, min_presence_score=0.0)

    roi = (320.0, 240.0, 200.0, 200.0, 0.0)
    result = tracker._run_landmarks(rgb, roi)
    assert result is not None

    assert result.landmarks.shape == (L.NUM_LANDMARKS, 3)
    assert result.world.shape == (L.NUM_LANDMARKS, 3)
    assert 0.0 <= result.score <= 1.0, "presence must be a sigmoid, not a logit"
    assert 0.0 <= result.handedness <= 1.0, "handedness must be a sigmoid, not a logit"
    assert np.isfinite(result.landmarks).all()

    # A 200 px ROI centred in a 640x480 frame can only produce landmarks near the centre;
    # if the crop-to-image affine were wrong they would scatter across the whole frame.
    px = result.landmarks[:, :2] * (640, 480)
    assert (np.abs(px - (320, 240)) < 200).all()


@pytest.mark.skipif(
    not _models_available(),
    reason="needs `uv sync --extra teleop` and a cached hand_landmarker.task",
)
def test_palm_detector_finds_nothing_in_noise() -> None:
    """The acquisition path must reject a frame with no hand rather than invent one."""
    rng = np.random.default_rng(1)
    rgb = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert L.HandLandmarker(download=False).detect(rgb) is None


@pytest.mark.skipif(
    not _models_available() or not os.environ.get("CLANKERS_TEST_HAND_IMAGE"),
    reason="set CLANKERS_TEST_HAND_IMAGE to a photo of a hand to run the detection check",
)
def test_real_photo_tracks() -> None:
    """End-to-end on a real photograph: acquire, then hold across the ROI hand-off."""
    import cv2

    img = cv2.imread(os.environ["CLANKERS_TEST_HAND_IMAGE"])
    assert img is not None, "CLANKERS_TEST_HAND_IMAGE could not be read"
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    tracker = L.HandLandmarker(download=False)
    first = tracker.detect(rgb)
    assert first is not None, "no hand found in the reference photo"
    assert first.score > 0.5
    assert ((first.landmarks[:, :2] > 0.0) & (first.landmarks[:, :2] < 1.0)).all()
    assert tracker.tracking

    second = tracker.detect(rgb)
    assert second is not None, "tracking dropped on the second frame"

    # Same frame twice: the landmark-derived ROI must not walk the hand off the truth.
    h, w = rgb.shape[:2]
    drift = np.abs((second.landmarks[:, :2] - first.landmarks[:, :2]) * (w, h)).max()
    assert drift < 0.1 * max(w, h)

    # Metric world landmarks should be an anatomically sane hand, not arbitrary units.
    for chain in ((5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16)):
        length = sum(
            float(np.linalg.norm(first.world[chain[i + 1]] - first.world[chain[i]]))
            for i in range(3)
        )
        assert 0.04 < length < 0.15, f"finger chain {length * 100:.1f} cm is not human-sized"
