"""Camera intrinsic calibration and intrinsics loading.

Intrinsics vs extrinsics:
- INTRINSICS describe the camera internally — focal lengths (fx, fy), the
  principal point (cx, cy) and lens distortion. They do not change when the
  camera moves. Calibrated once per physical camera+lens unit, at close range,
  with a flat ChArUco board waved through the whole field of view.
- EXTRINSICS describe where the camera IS — the rigid transform between the
  camera frame and a world frame (here, the cube). That is what the cube
  system estimates, and it can only be as good as the intrinsics feeding it.

This module replaces the previously untracked rig-side script with a
versioned implementation. The flat intrinsics board uses its own marker-ID
range so it can never be confused with a cube face.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

from .config import CalibrationConfig

# Flat intrinsics board: dense corners, fits A3 self-print, IDs far above the
# cube's ranges (cube uses < 60 with the default config).
INTRINSICS_BOARD_SQUARES = (8, 6)
INTRINSICS_SQUARE_MM = 40.0
INTRINSICS_MARKER_MM = 30.0
INTRINSICS_FIRST_ID = 100


def intrinsics_board(cfg: CalibrationConfig) -> "cv2.aruco.CharucoBoard":
    """The flat 8x6 ChArUco board used for intrinsic calibration, in mm.

    Shares the cube's dictionary but starts its marker IDs at
    INTRINSICS_FIRST_ID so a stray cube face in a shot can never be mistaken
    for the board. Raises ValueError if the ranges would collide or overflow.
    """
    n_markers = (INTRINSICS_BOARD_SQUARES[0] * INTRINSICS_BOARD_SQUARES[1]) // 2
    ids = np.arange(INTRINSICS_FIRST_ID, INTRINSICS_FIRST_ID + n_markers, dtype=np.int32)
    cube_max = max(i for f in cfg.face_order for i in cfg.marker_ids_for_face(f))
    if INTRINSICS_FIRST_ID <= cube_max:
        raise ValueError("intrinsics board IDs overlap cube IDs")
    if ids[-1] >= cfg.dictionary_size:
        raise ValueError("intrinsics board IDs exceed dictionary size")
    return cv2.aruco.CharucoBoard(
        INTRINSICS_BOARD_SQUARES, INTRINSICS_SQUARE_MM, INTRINSICS_MARKER_MM,
        cfg.aruco_dictionary, ids=ids,
    )


@dataclass
class IntrinsicsResult:
    """Output of an intrinsic calibration run.

    ``camera_matrix`` is the 3x3 K (fx, fy, cx, cy in pixels);
    ``dist_coeffs`` is OpenCV's 5-term vector (k1, k2, p1, p2, k3);
    ``image_size`` is (width, height) in pixels and is the resolution the
    intrinsics are valid for. ``rms_px`` is OpenCV's overall reprojection RMS.
    """

    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    image_size: tuple[int, int]  # (width, height)
    rms_px: float
    per_image_rms_px: list[float]
    n_images_used: int
    n_images_rejected: int


def calibrate_intrinsics(
    images: list[np.ndarray],
    cfg: CalibrationConfig | None = None,
    min_corners_per_image: int = 10,
) -> IntrinsicsResult:
    """Calibrate pinhole intrinsics from images of the flat intrinsics board.

    cx/cy are solved (never pinned to the image centre) and the standard
    5-coefficient distortion model is used — over-parameterised rational
    models fit noise on small datasets.
    """
    cfg = cfg or CalibrationConfig.load()
    board = intrinsics_board(cfg)
    detector = cv2.aruco.CharucoDetector(board)

    image_size: tuple[int, int] | None = None
    all_object, all_image = [], []
    rejected = 0
    for image in images:
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if image_size is None:
            image_size = (gray.shape[1], gray.shape[0])
        elif image_size != (gray.shape[1], gray.shape[0]):
            raise ValueError("all calibration images must share one resolution")
        ch_corners, ch_ids, _, _ = detector.detectBoard(gray)
        if ch_ids is None or len(ch_ids) < min_corners_per_image:
            rejected += 1
            continue
        obj, img = board.matchImagePoints(ch_corners, ch_ids)
        # calibrateCamera wants float32 point arrays.
        all_object.append(obj.astype(np.float32))
        all_image.append(img.astype(np.float32))

    # 5 usable views is a floor for a stable solve, not a target; 20-40 is usual.
    if len(all_object) < 5:
        raise ValueError(
            f"need >= 5 usable images, got {len(all_object)} "
            f"({rejected} rejected) — capture more views of the flat board"
        )

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        all_object, all_image, image_size, None, None
    )
    per_image = []
    for obj, img, rvec, tvec in zip(all_object, all_image, rvecs, tvecs):
        projected, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
        err = np.linalg.norm(projected.reshape(-1, 2) - img.reshape(-1, 2), axis=1)
        per_image.append(float(np.sqrt(np.mean(err**2))))

    return IntrinsicsResult(
        camera_matrix=K,
        dist_coeffs=dist.ravel(),
        image_size=image_size,
        rms_px=float(rms),
        per_image_rms_px=per_image,
        n_images_used=len(all_object),
        n_images_rejected=rejected,
    )


def calibrate_intrinsics_from_cube(
    images: list[np.ndarray],
    cfg: CalibrationConfig | None = None,
    min_points_per_image: int = 15,
) -> IntrinsicsResult:
    """Calibrate intrinsics using the CUBE as the target — no flat board needed.

    Rule: only ONE face is used per image (the dominant one). A face print's
    internal geometry is printer-accurate, but the relative pose BETWEEN faces
    carries the cube's build tolerances — multi-face views would bake those
    build errors into the lens model, so any secondary face is ignored.

    Capture: only RELATIVE motion between lens and pattern matters, so either
    side may move. With rig-mounted cameras, a helper holds the cube ~1-1.5 m
    in front of the fixed lens and moves/tilts/rolls it through 20-40 sharp
    shots, visiting every image region (especially corners). It does not
    matter which face happens to be toward the camera in each shot — all six
    faces share identical board geometry, so the dominant face may change
    freely between views.

    Each view contributes the dominant face's ChArUco chessboard corners plus
    its ArUco marker corners (up to 9 + 32 = 41 planar points), in the face's
    own board frame (z = 0), which keeps Zhang initialisation valid.
    """
    from .cube_geometry import CubeModel
    from .detection import CubeDetector
    # Imported here, not at module top, because detection -> cube_geometry ->
    # boards -> config; importing them at the top would make a cycle.

    cfg = cfg or CalibrationConfig.load()
    model = CubeModel(cfg)
    detector = CubeDetector(model)

    image_size: tuple[int, int] | None = None
    all_object, all_image = [], []
    rejected = 0
    for image in images:
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if image_size is None:
            image_size = (gray.shape[1], gray.shape[0])
        elif image_size != (gray.shape[1], gray.shape[0]):
            raise ValueError("all calibration images must share one resolution")

        detection = detector.detect(gray)
        if not detection.faces:
            rejected += 1
            continue
        # Dominant face = most chessboard corners; everything else is ignored.
        face_det = max(detection.faces.values(), key=lambda d: len(d.charuco_local_ids))
        fb = model.face_boards[face_det.face]

        obj_pts = [fb.chessboard_corners_board_mm[face_det.charuco_local_ids]]
        # Board-frame (not cube-frame) object points: z = 0 for every point, so
        # OpenCV's planar (Zhang) initialisation applies, and no inter-face
        # build error can enter.
        img_pts = [face_det.charuco_corners_px]
        marker_corners_board = fb.marker_corners_board_mm
        for marker_id, corners_px in zip(face_det.marker_ids, face_det.marker_corners_px):
            obj_pts.append(marker_corners_board[marker_id])
            img_pts.append(corners_px)
        obj = np.concatenate(obj_pts).astype(np.float32)
        img = np.concatenate(img_pts).astype(np.float32)
        if obj.shape[0] < min_points_per_image:
            rejected += 1
            continue
        all_object.append(obj.reshape(-1, 1, 3))
        all_image.append(img.reshape(-1, 1, 2))

    if len(all_object) < 5:
        raise ValueError(
            f"need >= 5 usable single-face views, got {len(all_object)} "
            f"({rejected} rejected) — move the camera around one cube face and recapture"
        )

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        all_object, all_image, image_size, None, None
    )
    per_image = []
    for obj, img, rvec, tvec in zip(all_object, all_image, rvecs, tvecs):
        projected, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
        err = np.linalg.norm(projected.reshape(-1, 2) - img.reshape(-1, 2), axis=1)
        per_image.append(float(np.sqrt(np.mean(err**2))))

    return IntrinsicsResult(
        camera_matrix=K,
        dist_coeffs=dist.ravel(),
        image_size=image_size,
        rms_px=float(rms),
        per_image_rms_px=per_image,
        n_images_used=len(all_object),
        n_images_rejected=rejected,
    )


def save_intrinsics(result: IntrinsicsResult, path: str | Path) -> Path:
    """YAML (stress_3d-compatible field names) plus a sibling .npz."""
    # The .npz is the lossless copy; YAML floats are rounded for readability.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "image_width": result.image_size[0],
        "image_height": result.image_size[1],
        "camera_matrix": [[float(v) for v in row] for row in result.camera_matrix],
        "distortion_coefficients": [float(v) for v in result.dist_coeffs],
        "reprojection_error_rms": round(result.rms_px, 6),
        "images": {"used": result.n_images_used, "rejected": result.n_images_rejected},
        "model": "pinhole",
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    np.savez(
        path.with_suffix(".npz"),
        camera_matrix=result.camera_matrix,
        dist_coeffs=result.dist_coeffs,
        image_size=np.array(result.image_size),
    )
    return path


def load_intrinsics(path: str | Path) -> tuple[np.ndarray, np.ndarray, tuple[int, int] | None]:
    """Load (camera_matrix, dist_coeffs, image_size) from .yaml/.yml/.json/.npz.

    Accepts both this module's YAML and the existing stress_3d intrinsics
    YAML (same field names).
    """
    path = Path(path)
    if path.suffix == ".npz":
        data = np.load(path)
        size = tuple(int(v) for v in data["image_size"]) if "image_size" in data else None
        dist_key = "dist_coeffs" if "dist_coeffs" in data else "distortion_coefficients"
        return (
            np.asarray(data["camera_matrix"], dtype=np.float64),
            np.asarray(data[dist_key], dtype=np.float64).ravel(),
            size,  # type: ignore[return-value]
        )
    if path.suffix in (".yaml", ".yml", ".json"):
        import json

        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)

        def as_array(node) -> np.ndarray:
            """Accept a plain nested list or an OpenCV FileStorage-style matrix dict."""
            # stress_3d files use OpenCV-style {rows, cols, data} dicts.
            if isinstance(node, dict):
                arr = np.asarray(node["data"], dtype=np.float64)
                return arr.reshape(int(node["rows"]), int(node["cols"]))
            return np.asarray(node, dtype=np.float64)

        K = as_array(data["camera_matrix"]).reshape(3, 3)
        dist = as_array(
            data.get("distortion_coefficients", data.get("dist_coeffs", []))
        ).ravel()
        size = None
        if "image_width" in data and "image_height" in data:
            size = (int(data["image_width"]), int(data["image_height"]))
        return K, dist, size
    raise ValueError(f"unsupported intrinsics format: {path.suffix}")
