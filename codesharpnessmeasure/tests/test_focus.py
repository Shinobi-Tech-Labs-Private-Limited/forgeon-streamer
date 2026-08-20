"""codesharpnessmeasure on the calibration cube: the detector must find the
cube (any faces), the score must fall monotonically with defocus, and the
best-so-far tracker must grade PEAK / NEAR / LOW sensibly."""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

# Runs in either layout:
#  - rig scripts dir: calibration/ and codesharpnessmeasure/ side by side
#    (cd <scripts dir> && pytest codesharpnessmeasure/tests)
#  - forgeon repo: FORGEON_BACKEND=<repo>/backend pytest codesharpnessmeasure/tests
import os
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))  # dir containing calibration/ on the rig
if os.environ.get("FORGEON_BACKEND"):
    BACKEND = Path(os.environ["FORGEON_BACKEND"])
    sys.path.insert(0, str(BACKEND))
    sys.path.insert(0, str(BACKEND / "app"))

from codesharpnessmeasure.focus import charuco as charuco_mod  # noqa: E402
from codesharpnessmeasure.focus.charuco import detect_and_segment  # noqa: E402
from codesharpnessmeasure.focus.scorer import (  # noqa: E402
    FocusTracker,
    compute_focus_score,
    measure_frame,
)

try:  # forgeon backend layout (backend/ on sys.path)
    from app.calibration.config import CalibrationConfig  # noqa: E402
    from app.calibration.cube_geometry import CubeModel  # noqa: E402
    from app.calibration.pose import invert_T  # noqa: E402
    from app.calibration.rendering import render_view  # noqa: E402
    from app.calibration.simulate import T_cube_from_camera_lookat, camera_matrix_for  # noqa: E402
except ImportError:  # rig layout: calibration/ sits beside codesharpnessmeasure/
    from calibration.config import CalibrationConfig  # noqa: E402
    from calibration.cube_geometry import CubeModel  # noqa: E402
    from calibration.pose import invert_T  # noqa: E402
    from calibration.rendering import render_view  # noqa: E402
    from calibration.simulate import T_cube_from_camera_lookat, camera_matrix_for  # noqa: E402

CFG = CalibrationConfig.load()
MODEL = CubeModel(CFG)
SPEC = CFG.cameras["ov9782_2p8mm"]
K = camera_matrix_for(SPEC)
IMAGE_SIZE = (SPEC.width, SPEC.height)
CAM_POS = np.array([1800.0, -1400.0, 400.0])  # FRONT + RIGHT, ~2.3 m


def _frame(blur_sigma_px: float, background: int = 110) -> np.ndarray:
    T_cam_from_cube = invert_T(T_cube_from_camera_lookat(CAM_POS))
    gray = render_view(
        MODEL, T_cam_from_cube, K, IMAGE_SIZE,
        blur_sigma_px=blur_sigma_px, noise_sigma=1.0, background=background,
        rng=np.random.default_rng(1),
    )
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def test_cube_detector_is_available_and_used():
    assert charuco_mod.CUBE_AVAILABLE, charuco_mod.CUBE_IMPORT_ERROR
    det = detect_and_segment(_frame(0.5))
    assert det["detected"] and det["target"] == "cube"
    assert det["markers"] >= 8 and det["corners"] >= 9
    assert set(det["faces"]) & {"FRONT", "RIGHT"}
    assert 15 < det["square_px"] < 120
    h, w = det["board_crop"].shape[:2]
    assert w < 0.7 * SPEC.width and h < 0.9 * SPEC.height, (w, h)  # cube region, not the frame
    assert det["annotated"].shape == _frame(0.5).shape


def test_score_falls_monotonically_with_blur():
    scores = []
    for sigma in (0.3, 0.7, 1.1, 1.5, 1.9):
        det = detect_and_segment(_frame(sigma))
        assert det["detected"], sigma
        scores.append(compute_focus_score(det["board_crop"], square_px=det["square_px"]))
    assert all(a > b for a, b in zip(scores, scores[1:])), scores
    assert scores[-1] < 0.5 * scores[0], scores


def test_score_is_roughly_exposure_invariant():
    bright = detect_and_segment(_frame(0.8))
    dark_img = (_frame(0.8).astype(np.float32) * 0.6).astype(np.uint8)
    dark = detect_and_segment(dark_img)
    sb = compute_focus_score(bright["board_crop"], square_px=bright["square_px"])
    sd = compute_focus_score(dark["board_crop"], square_px=dark["square_px"])
    assert abs(sd - sb) / sb < 0.2, (sb, sd)


def test_no_cube_gives_no_cube_payload():
    frame = np.zeros((SPEC.height, SPEC.width, 3), dtype=np.uint8)
    cv2.randu(frame, 0, 255)
    payload = measure_frame(frame, FocusTracker(), "cam1")
    assert payload["detected"] is False and payload["label"] == "NO CUBE" and payload["score"] is None


def test_tracker_verdicts_follow_best_so_far():
    t = FocusTracker()
    best, ratio, label, _ = t.update("cam1", 0.10)
    assert (best, label) == (0.10, "PEAK")           # first sample is the best
    best, ratio, label, _ = t.update("cam1", 0.05)
    assert best == 0.10 and label == "LOW"            # far below best
    best, ratio, label, _ = t.update("cam1", 0.09)
    assert label == "NEAR"                            # 90 % of best
    best, ratio, label, _ = t.update("cam1", 0.12)
    assert best == 0.12 and label == "PEAK"           # new best
    assert t.best("cam2") is None                     # cameras independent
    t.reset("cam1")
    assert t.best("cam1") is None


def test_measure_frame_payload_shape_and_relative_labels():
    t = FocusTracker()
    sharp = measure_frame(_frame(0.4), t, "cam1")
    assert sharp["detected"] and sharp["target"] == "cube" and sharp["label"] == "PEAK"
    for key in ("score", "best", "ratio_to_best", "abs_label", "color", "corners", "markers", "faces", "square_px"):
        assert key in sharp, key
    blurred = measure_frame(_frame(1.6), t, "cam1")
    assert blurred["detected"] and blurred["label"] == "LOW" and blurred["best"] == sharp["best"]
    import json
    json.dumps(sharp); json.dumps(blurred)


@pytest.mark.parametrize("name", [
    "WhatsApp Image 2026-08-17 at 6.09.00 PM (1).jpeg",
    "WhatsApp Image 2026-08-17 at 6.28.53 PM.jpeg",
])
def test_real_build_photos_if_present(name):
    path = Path.home() / "Downloads" / name
    if not path.exists():
        pytest.skip("build photo not on this machine")
    img = cv2.imread(str(path))
    sharp = detect_and_segment(img)
    assert sharp["detected"] and sharp["target"] == "cube" and sharp["markers"] >= 8
    s0 = compute_focus_score(sharp["board_crop"], square_px=sharp["square_px"])
    blur = detect_and_segment(cv2.GaussianBlur(img, (0, 0), 3))
    assert blur["detected"]
    s1 = compute_focus_score(blur["board_crop"], square_px=blur["square_px"])
    assert s1 < 0.5 * s0, (s0, s1)
