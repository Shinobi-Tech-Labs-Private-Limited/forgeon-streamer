#!/usr/bin/env python3
"""Calibrate cam intrinsics from ChArUco snaps and undistort images/videos."""
#
# Overview
# --------
# Role on the rig: intrinsic (lens) calibration for one camera at a time, by
# default cam1 (CALIBRATION_CAMERA in the app). Two ways in:
#   * CLI:    python intrinsic_calibrate_charuco.py --snaps-dir sessions/<s>/snaps
#             --camera cam1 [--undistort-video some.mp4]
#   * In-app: app35_cam_sole.py builds an argparse.Namespace by hand and calls
#             calibrate(args) - a full run from its calibration routes, and a
#             --detect-only run for the snap preview. It also imports
#             CalibrationCandidate and make_maps to undistort recorded videos
#             with a previously saved calibration_cam1.json.
#
# Pipeline (see calibrate()):
#   1. collect_image_paths   snaps/<snap_N>/<camera>.jpg (or an --images glob)
#   2. detect_charuco        ArUco markers -> interpolated ChArUco corners per
#                            image; annotated copy saved to <out>/detections/
#   3. calibrate_pinhole /   fit one or both lens models (--model auto = both)
#      calibrate_fisheye
#   4. selection             keep numerically valid fits, choose the lower RMS
#   5. make_maps + undistort every used snap into <out>/undistorted_images/
#      (+ optional --undistort-image / --undistort-video)
#   6. write_outputs         calibration_<camera>.json (what the app reads) and
#                            calibration_<camera>.npz (numpy arrays, all models)
#
# Inputs on disk:  snap images of the ChArUco board / calibration cube, all at
#                  one resolution.
# Outputs on disk: <output_dir>/calibration_<camera>.{json,npz},
#                  <output_dir>/detections/*.jpg,
#                  <output_dir>/undistorted_images/*.jpg,
#                  <output_dir>/undistorted_videos/*.mp4,
#                  or just detections_<camera>.json in --detect-only mode.
#                  output_dir defaults to calibration_<camera>/<session name>.
#
# Units: --square-length / --marker-length are metres. They only scale the
# per-image poses (rvecs/tvecs); the intrinsics and the undistortion maps do
# not depend on them, but the squares:markers RATIO must match the print.
#
# Errors: every hard failure is raised as SystemExit with a message, which is
# CLI-friendly; in-app callers must catch it (the detect-only preview does).

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


DEFAULT_SNAPS_DIR = "sessions/session_2026-06-10_12-29-18/snaps"
DEFAULT_CAMERA = "cam1"


@dataclass
class CalibrationCandidate:
    """One fitted lens model for a camera plus everything needed to undistort with it.

    Produced by calibrate_pinhole() / calibrate_fisheye(); consumed by make_maps(),
    undistort_image() / undistort_video() and write_outputs(). app35_cam_sole.py
    also rebuilds one from the saved calibration_<camera>.json (with empty
    rvecs/tvecs) to undistort recorded videos.

    Fields:
        model: "pinhole" or "fisheye"; decides which cv2 undistortion API applies.
        rms: reprojection error in pixels reported by the solver. Lower is better;
            it is the auto-selection criterion.
        camera_matrix: 3x3 intrinsics K of the raw (distorted) image.
        dist_coeffs: pinhole -> OpenCV's (k1, k2, p1, p2, k3, ...) in solver order;
            fisheye -> (k1..k4) as a (4, 1) column. Use .reshape(-1) for a flat list.
        new_camera_matrix: 3x3 intrinsics of the UNDISTORTED output image (after
            alpha/balance cropping). Downstream consumers of the undistorted video
            should use this as K, not camera_matrix.
        roi: (x, y, w, h) valid-pixel rectangle inside the undistorted image. Only
            meaningful for pinhole; fisheye reports the full frame.
        rvecs / tvecs: per-used-image board pose (rotation / translation vectors),
            in the order the images were fed to the solver. Kept for the .npz only.
    """

    model: str
    rms: float
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    new_camera_matrix: np.ndarray
    roi: tuple[int, int, int, int]
    rvecs: tuple[np.ndarray, ...] | list[np.ndarray]
    tvecs: tuple[np.ndarray, ...] | list[np.ndarray]


def make_board(args: argparse.Namespace) -> tuple[Any, Any]:
    """Build the ArUco dictionary and ChArUco board from the CLI board settings.

    Returns (dictionary, board). The geometry (squares_x by squares_y, square and
    marker size) must describe the printed board exactly, otherwise detection
    silently yields few or wrong corners. The defaults (DICT_4X4_50, 4x3,
    40 mm / 30 mm) are the same values app35_cam_sole.py hard-codes when it
    calls calibrate().
    """
    aruco = cv2.aruco
    dictionary = aruco.getPredefinedDictionary(getattr(aruco, args.dictionary))
    board = aruco.CharucoBoard(
        (args.squares_x, args.squares_y),
        args.square_length,
        args.marker_length,
        dictionary,
    )
    return dictionary, board


def collect_image_paths(args: argparse.Namespace) -> list[Path]:
    """List the calibration images in a deterministic (sorted) order.

    --images: a glob relative to the CWD. Otherwise <snaps_dir>/*/<camera>.jpg,
    i.e. one image per snap folder (snap_N/cam1.jpg), which is the layout the
    app's snap capture writes.
    """
    if args.images:
        return sorted(Path(".").glob(args.images))
    snaps_dir = Path(args.snaps_dir)
    return sorted(snaps_dir.glob(f"*/{args.camera}.jpg"))


def default_output_dir(args: argparse.Namespace) -> Path:
    """Output folder: --output-dir if given, else calibration_<camera>/<session name>.

    The session name is the parent of the snaps dir when that dir is literally
    called "snaps" (sessions/session_X/snaps -> calibration_cam1/session_X), so
    runs on different sessions do not overwrite each other.
    """
    if args.output_dir:
        return Path(args.output_dir)
    if args.snaps_dir:
        snaps_dir = Path(args.snaps_dir)
        session_dir = snaps_dir.parent if snaps_dir.name == "snaps" else snaps_dir
        return Path(f"calibration_{args.camera}") / session_dir.name
    return Path(f"calibration_{args.camera}")


def display_path(path: Path) -> str:
    """Path relative to the CWD when possible (for JSON and console output), else as given."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def artifact_name(image_path: Path) -> str:
    """'<snap folder>_<file name>', e.g. snap_3_cam1.jpg.

    Every snap folder holds a same-named cam1.jpg, so per-image outputs need
    the folder name folded in to stay distinct inside one flat output dir.
    """
    return f"{image_path.parent.name}_{image_path.name}"


def detect_charuco(
    image_path: Path,
    dictionary: Any,
    board: Any,
    min_corners: int,
) -> tuple[np.ndarray | None, np.ndarray | None, tuple[int, int] | None, np.ndarray | None, int]:
    """Stage 2: find ChArUco corners in one image.

    Detection is two-step: ArUco markers first, then interpolateCornersCharuco
    places the chessboard corners between them at sub-pixel precision. Those
    interpolated corners, not the marker corners, are what the solver fits,
    which is why a ChArUco board beats a plain ArUco grid for intrinsics.

    Returns (corners, ids, image_size, annotated, marker_count):
        corners / ids: ChArUco corner pixel positions and their board ids, or
            None when the image is unreadable or fewer than min_corners corners
            were found.
        image_size: (width, height), or None if the image could not be read.
        annotated: BGR copy with detected markers and corners drawn; calibrate()
            saves it to <out>/detections/ so a human can see why an image was or
            was not used. None only for unreadable images.
        marker_count: raw ArUco markers seen, before corner interpolation.
    """
    frame = cv2.imread(str(image_path))
    if frame is None:
        return None, None, None, None, 0

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    marker_corners, marker_ids, _ = detector.detectMarkers(gray)

    annotated = frame.copy()
    marker_count = 0 if marker_ids is None else len(marker_ids)
    if marker_count:
        cv2.aruco.drawDetectedMarkers(annotated, marker_corners, marker_ids)
    # gray.shape is (rows, cols); every OpenCV calibration API wants (width,
    # height), hence the [::-1]. Two markers is this script's floor before it
    # asks OpenCV to interpolate corners between them.
    if marker_ids is None or len(marker_ids) < 2:
        return None, None, gray.shape[::-1], annotated, marker_count

    count, charuco_corners, charuco_ids = cv2.aruco.interpolateCornersCharuco(
        marker_corners,
        marker_ids,
        gray,
        board,
    )
    if count < min_corners or charuco_corners is None or charuco_ids is None:
        return None, None, gray.shape[::-1], annotated, marker_count

    cv2.aruco.drawDetectedCornersCharuco(annotated, charuco_corners, charuco_ids)
    return charuco_corners, charuco_ids, gray.shape[::-1], annotated, marker_count


def charuco_object_points(
    board: Any,
    all_ids: list[np.ndarray],
    all_corners: list[np.ndarray],
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Convert ChArUco detections into the (object_points, image_points) lists
    that cv2.fisheye.calibrate needs.

    The pinhole path lets calibrateCameraCharuco do this internally; the fisheye
    API has no ChArUco-aware variant, so each detected corner id is looked up in
    the board's 3D chessboard corners here. Shapes are (N, 1, 3) / (N, 1, 2)
    float64 per image because cv2.fisheye.calibrate is strict about exactly
    that layout and dtype.
    """
    chessboard_corners = board.getChessboardCorners()
    object_points = []
    image_points = []

    for ids, corners in zip(all_ids, all_corners):
        ids_flat = ids.reshape(-1).astype(int)
        obj = chessboard_corners[ids_flat].reshape(-1, 1, 3).astype(np.float64)
        img = corners.reshape(-1, 1, 2).astype(np.float64)
        object_points.append(obj)
        image_points.append(img)

    return object_points, image_points


def is_valid_candidate(candidate: CalibrationCandidate) -> bool:
    """Reject solver output that is numerically garbage.

    OpenCV can hand back NaN/inf matrices or a zero RMS without raising. Such a
    candidate must never win the auto selection by comparing as a tiny RMS, so
    it is filtered out before min() runs.
    """
    arrays = [candidate.camera_matrix, candidate.dist_coeffs, candidate.new_camera_matrix]
    return (
        math.isfinite(candidate.rms)
        and candidate.rms > 0
        and all(np.isfinite(arr).all() for arr in arrays)
    )


def calibrate_pinhole(
    board: Any,
    all_corners: list[np.ndarray],
    all_ids: list[np.ndarray],
    image_size: tuple[int, int],
    alpha: float,
) -> CalibrationCandidate:
    """Stage 3a: fit the standard pinhole + polynomial distortion model.

    cameraMatrix=None / distCoeffs=None let the solver initialise K itself.
    new_camera_matrix and roi come from getOptimalNewCameraMatrix at the same
    output size: alpha=0 crops the undistorted image to valid pixels only,
    alpha=1 keeps the full source field of view with black corners.
    """
    rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.aruco.calibrateCameraCharuco(
        charucoCorners=all_corners,
        charucoIds=all_ids,
        board=board,
        imageSize=image_size,
        cameraMatrix=None,
        distCoeffs=None,
    )
    new_camera_matrix, roi = cv2.getOptimalNewCameraMatrix(
        camera_matrix, dist_coeffs, image_size, alpha, image_size
    )
    return CalibrationCandidate(
        model="pinhole",
        rms=float(rms),
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        new_camera_matrix=new_camera_matrix,
        roi=tuple(map(int, roi)),
        rvecs=rvecs,
        tvecs=tvecs,
    )


def calibrate_fisheye(
    board: Any,
    all_corners: list[np.ndarray],
    all_ids: list[np.ndarray],
    image_size: tuple[int, int],
    alpha: float,
) -> CalibrationCandidate:
    """Stage 3b: fit the equidistant fisheye model (cv2.fisheye, 4 coefficients).

    Tried alongside pinhole under --model auto in case the lens distortion is
    stronger than the polynomial pinhole model fits well; the lower RMS wins.
    `alpha` maps to the fisheye `balance` parameter (0 = crop, 1 = keep FOV),
    the same meaning as on the pinhole path.

    Raises cv2.error when the input is ill-conditioned (CALIB_CHECK_COND);
    calibrate() catches that per model and records it under errors["fisheye"].
    """
    object_points, image_points = charuco_object_points(board, all_ids, all_corners)
    camera_matrix = np.zeros((3, 3), dtype=np.float64)
    # The fisheye model has exactly four coefficients (k1..k4); both arrays are
    # zero-initialised because the API fills them in place.
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)
    # RECOMPUTE_EXTRINSIC: re-solve each view's pose on every iteration instead
    #   of keeping the initial guess (more accurate; image count is tiny).
    # CHECK_COND: raise cv2.error on an ill-conditioned system rather than
    #   return a meaningless fit; calibrate() relies on this to fall back.
    # FIX_SKEW: pin the skew term at 0; sensors have square, axis-aligned pixels.
    flags = (
        cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
        | cv2.fisheye.CALIB_CHECK_COND
        | cv2.fisheye.CALIB_FIX_SKEW
    )
    rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.fisheye.calibrate(
        # 200 iterations / 1e-7 eps: a tighter stop than OpenCV's default; the
        # extra work is negligible for a handful of images.
        object_points,
        image_points,
        image_size,
        camera_matrix,
        dist_coeffs,
        flags=flags,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-7),
    )
    new_camera_matrix = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
        camera_matrix,
        dist_coeffs,
        image_size,
        np.eye(3),
        balance=alpha,
        new_size=image_size,
    )
    # The fisheye API has no ROI concept: `balance` already decided the crop,
    # so the full frame is reported to keep the dataclass shape uniform.
    roi = (0, 0, image_size[0], image_size[1])
    return CalibrationCandidate(
        model="fisheye",
        rms=float(rms),
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        new_camera_matrix=new_camera_matrix,
        roi=roi,
        rvecs=rvecs,
        tvecs=tvecs,
    )


def make_maps(candidate: CalibrationCandidate, image_size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Stage 5: build the remap lookup tables (map1, map2) for cv2.remap.

    Picks the fisheye or pinhole initUndistortRectifyMap by candidate.model. The
    maps are the expensive part of undistortion; build them once and reuse them
    for every frame (undistort_video here, and app35_cam_sole.py's own video
    undistortion). No rectification rotation (identity R / None): this is a
    single camera, not a stereo pair.

    CV_16SC2 asks for the fixed-point map pair, the fastest layout for
    cv2.remap and smaller than two float32 maps.
    """
    if candidate.model == "fisheye":
        return cv2.fisheye.initUndistortRectifyMap(
            candidate.camera_matrix,
            candidate.dist_coeffs,
            np.eye(3),
            candidate.new_camera_matrix,
            image_size,
            cv2.CV_16SC2,
        )
    return cv2.initUndistortRectifyMap(
        candidate.camera_matrix,
        candidate.dist_coeffs,
        None,
        candidate.new_camera_matrix,
        image_size,
        cv2.CV_16SC2,
    )


def ensure_matching_size(actual_size: tuple[int, int], expected_size: tuple[int, int], source: Path) -> None:
    """SystemExit unless the media matches the calibrated resolution.

    Remap tables are resolution-specific; applying them to a differently sized
    frame would not fail, it would silently produce a wrong image.
    """
    if actual_size != expected_size:
        raise SystemExit(
            f"Size mismatch for {source}: {actual_size[0]}x{actual_size[1]}, "
            f"expected {expected_size[0]}x{expected_size[1]} from calibration."
        )


def undistort_image(
    image_path: Path,
    output_dir: Path,
    candidate: CalibrationCandidate,
    image_size: tuple[int, int],
    output_stem: str | None = None,
) -> Path:
    """Undistort one still with the selected model; save <stem>_undistorted.jpg.

    Returns the written path. output_stem overrides the file stem (calibrate()
    passes the snap-qualified artifact_name so per-snap outputs do not
    collide). Rebuilds the remap tables on every call, which is fine for a
    handful of snaps.
    """
    frame = cv2.imread(str(image_path))
    if frame is None:
        raise SystemExit(f"Could not read image to undistort: {image_path}")
    ensure_matching_size((frame.shape[1], frame.shape[0]), image_size, image_path)

    map1, map2 = make_maps(candidate, image_size)
    undistorted = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem or image_path.stem
    out_path = output_dir / f"{stem}_undistorted.jpg"
    if not cv2.imwrite(str(out_path), undistorted):
        raise SystemExit(f"Failed to write undistorted image: {out_path}")
    return out_path


def undistort_video(
    video_path: Path,
    output_dir: Path,
    candidate: CalibrationCandidate,
    image_size: tuple[int, int],
) -> Path:
    """Undistort every frame of a video into <stem>_<model>_undistorted.mp4.

    Returns the written path. Output uses the mp4v codec at the source fps
    (30 assumed when the container reports 0). Maps are built once and reused
    per frame. Raises SystemExit if the video cannot be opened, its size does
    not match the calibration, or no frame was written.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise SystemExit(f"Could not open video to undistort: {video_path}")

    width = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
    height = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    ensure_matching_size((width, height), image_size, video_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{video_path.stem}_{candidate.model}_undistorted.mp4"
    writer = cv2.VideoWriter(
        str(out_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        image_size,
    )
    if not writer.isOpened():
        cap.release()
        raise SystemExit(f"Could not open video writer: {out_path}")

    map1, map2 = make_maps(candidate, image_size)
    frames_written = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        undistorted = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
        writer.write(undistorted)
        frames_written += 1

    cap.release()
    writer.release()
    if frames_written == 0:
        raise SystemExit(f"No frames were written from video: {video_path}")
    return out_path


def candidate_to_json(candidate: CalibrationCandidate | None, error: str | None = None) -> dict[str, Any] | None:
    """JSON-friendly summary of one model's outcome for the "model_results" block.

    None when the model was not attempted, {"error": ...} when its solver
    raised, otherwise rms + matrices (+ "error" if both a result and an error
    text exist).
    """
    if candidate is None:
        if error is None:
            return None
        return {"error": error}
    return {
        "rms_reprojection_error_px": candidate.rms,
        "camera_matrix": candidate.camera_matrix.tolist(),
        "new_camera_matrix": candidate.new_camera_matrix.tolist(),
        "distortion_coefficients": candidate.dist_coeffs.reshape(-1).tolist(),
        "roi": list(map(int, candidate.roi)),
        **({"error": error} if error else {}),
    }


def write_outputs(
    output_dir: Path,
    camera: str,
    result: dict[str, Any],
    selected: CalibrationCandidate,
    candidates: dict[str, CalibrationCandidate | None],
    errors: dict[str, str],
    image_size: tuple[int, int],
) -> None:
    """Stage 6: persist results as calibration_<camera>.json and .npz.

    JSON: the full `result` dict from calibrate(); this is what
    app35_cam_sole.py reads back and what a human inspects.
    NPZ: numpy arrays of the SELECTED model under plain names (model,
    camera_matrix, dist_coeffs, new_camera_matrix, roi, image_size, rvecs,
    tvecs) plus every attempted model's arrays under a <model>_ prefix and any
    solver error text as <model>_error.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / f"calibration_{camera}.json").open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    npz_payload: dict[str, Any] = {
        "model": np.array(selected.model),
        "camera_matrix": selected.camera_matrix,
        "dist_coeffs": selected.dist_coeffs,
        "new_camera_matrix": selected.new_camera_matrix,
        "roi": np.array(selected.roi),
        "image_size": np.array([image_size[0], image_size[1]]),
        # object arrays: np.load(...) must be called with allow_pickle=True to
        # read these two keys back.
        "rvecs": np.array(selected.rvecs, dtype=object),
        "tvecs": np.array(selected.tvecs, dtype=object),
    }
    for model, candidate in candidates.items():
        if candidate is None:
            continue
        npz_payload[f"{model}_camera_matrix"] = candidate.camera_matrix
        npz_payload[f"{model}_dist_coeffs"] = candidate.dist_coeffs
        npz_payload[f"{model}_new_camera_matrix"] = candidate.new_camera_matrix
        npz_payload[f"{model}_rms"] = np.array(candidate.rms)
    for model, error in errors.items():
        npz_payload[f"{model}_error"] = np.array(error)
    np.savez(output_dir / f"calibration_{camera}.npz", **npz_payload)


def calibrate(args: argparse.Namespace) -> dict[str, Any]:
    """Run the whole pipeline for one camera and return the result dict.

    `args` is an argparse.Namespace: from parse_args() on the CLI, or built by
    hand in app35_cam_sole.py (_run_session_calibration and
    _run_detection_preview), so every attribute read here is part of the
    contract with the app.

    Stages:
      1. collect images; SystemExit if none matched.
      2. detect_charuco on each. Every readable image must share one
         resolution. An annotated copy is saved to <out>/detections/ for each
         readable image. SystemExit if fewer than --min-images were usable.
      3. --detect-only: write detections_<camera>.json and return early (the
         app uses this to preview snap quality before committing to a run).
      4. fit pinhole and/or fisheye per --model. A cv2.error from a solver is
         recorded in `errors`, not fatal, so the other model can still win.
      5. select: the explicitly requested model (which must be valid), or for
         auto the valid candidate with the lowest RMS.
      6. undistort every used snap, plus the optional --undistort-image /
         --undistort-video.
      7. write_outputs and return.

    Returns the result dict (also written to calibration_<camera>.json). Key
    fields: selected_model, rms_reprojection_error_px, camera_matrix,
    new_camera_matrix, distortion_coefficients (flat list), roi, image_size
    {width, height}, model_results (per model, see candidate_to_json), board
    settings, per-image detections (image / used / markers / charuco_corners),
    and the undistorted output paths. In --detect-only mode only camera,
    image_size, used_images, total_images and detections are present.

    Raises SystemExit with a message on every hard failure.
    """
    image_paths = collect_image_paths(args)
    if not image_paths:
        source = args.images if args.images else f"{args.snaps_dir}/*/{args.camera}.jpg"
        raise SystemExit(f"No images matched: {source}")

    dictionary, board = make_board(args)
    output_dir = default_output_dir(args)
    annotated_dir = output_dir / "detections"
    undistorted_dir = output_dir / "undistorted_images"
    undistorted_video_dir = output_dir / "undistorted_videos"
    annotated_dir.mkdir(parents=True, exist_ok=True)
    undistorted_dir.mkdir(parents=True, exist_ok=True)

    all_corners: list[np.ndarray] = []
    all_ids: list[np.ndarray] = []
    used_image_paths: list[Path] = []
    image_size: tuple[int, int] | None = None
    detections: list[dict[str, Any]] = []

    for image_path in image_paths:
        corners, ids, size, annotated, marker_count = detect_charuco(
            image_path, dictionary, board, args.min_corners
        )
        if size is not None:
            if image_size is None:
                image_size = size
            elif image_size != size:
                raise SystemExit(
                    f"Image size mismatch: {image_path} is {size}, expected {image_size}"
                )

        detected = corners is not None and ids is not None
        corner_count = int(len(ids)) if detected else 0
        detections.append(
            {
                "image": display_path(image_path),
                "used": detected,
                "markers": int(marker_count),
                "charuco_corners": corner_count,
            }
        )
        if annotated is not None:
            cv2.imwrite(str(annotated_dir / artifact_name(image_path)), annotated)
        if detected:
            all_corners.append(corners)
            all_ids.append(ids)
            used_image_paths.append(image_path)

    if image_size is None:
        raise SystemExit("Could not read any calibration images.")
    if len(all_corners) < args.min_images:
        raise SystemExit(
            f"Only {len(all_corners)} usable images found; need at least {args.min_images}. "
            "Check board settings or capture more board poses."
        )

    if args.detect_only:
        result = {
            "camera": args.camera,
            "image_size": {"width": image_size[0], "height": image_size[1]},
            "used_images": len(all_corners),
            "total_images": len(image_paths),
            "detections": detections,
        }
        with (output_dir / f"detections_{args.camera}.json").open("w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    candidates: dict[str, CalibrationCandidate | None] = {"pinhole": None, "fisheye": None}
    errors: dict[str, str] = {}

    if args.model in ("auto", "pinhole"):
        try:
            candidates["pinhole"] = calibrate_pinhole(
                board, all_corners, all_ids, image_size, args.alpha
            )
        except cv2.error as exc:
            errors["pinhole"] = str(exc)

    if args.model in ("auto", "fisheye"):
        try:
            candidates["fisheye"] = calibrate_fisheye(
                board, all_corners, all_ids, image_size, args.alpha
            )
        except cv2.error as exc:
            errors["fisheye"] = str(exc)

    valid_candidates = [
        candidate for candidate in candidates.values() if candidate is not None and is_valid_candidate(candidate)
    ]
    # Explicit --model: that model must have succeeded. auto: lowest RMS wins;
    # NaN results were already filtered so min() is well defined.
    if args.model != "auto":
        selected = candidates.get(args.model)
        if selected is None or not is_valid_candidate(selected):
            raise SystemExit(f"{args.model} calibration failed: {errors.get(args.model, 'invalid result')}")
    elif valid_candidates:
        selected = min(valid_candidates, key=lambda item: item.rms)
    else:
        raise SystemExit(f"No valid calibration model. Errors: {errors}")

    undistorted_images: list[str] = []
    for image_path in used_image_paths:
        out_path = undistort_image(
            image_path,
            undistorted_dir,
            selected,
            image_size,
            output_stem=Path(artifact_name(image_path)).stem,
        )
        undistorted_images.append(display_path(out_path))

    requested_undistorted_image = None
    if args.undistort_image:
        requested_undistorted_image = display_path(
            undistort_image(Path(args.undistort_image), undistorted_dir, selected, image_size)
        )

    requested_undistorted_video = None
    if args.undistort_video:
        requested_undistorted_video = display_path(
            undistort_video(Path(args.undistort_video), undistorted_video_dir, selected, image_size)
        )

    result = {
        "camera": args.camera,
        "selected_model": selected.model,
        "rms_reprojection_error_px": selected.rms,
        "model_results": {
            "pinhole": candidate_to_json(candidates["pinhole"], errors.get("pinhole")),
            "fisheye": candidate_to_json(candidates["fisheye"], errors.get("fisheye")),
        },
        "image_size": {"width": image_size[0], "height": image_size[1]},
        "board": {
            "dictionary": args.dictionary,
            "squares_x": args.squares_x,
            "squares_y": args.squares_y,
            "square_length": args.square_length,
            "marker_length": args.marker_length,
        },
        "used_images": len(all_corners),
        "total_images": len(image_paths),
        "source_images": [display_path(path) for path in image_paths],
        "used_source_images": [display_path(path) for path in used_image_paths],
        "camera_matrix": selected.camera_matrix.tolist(),
        "new_camera_matrix": selected.new_camera_matrix.tolist(),
        "distortion_coefficients": selected.dist_coeffs.reshape(-1).tolist(),
        "roi": list(map(int, selected.roi)),
        "detections": detections,
        "undistorted_images": undistorted_images,
        "requested_undistorted_image": requested_undistorted_image,
        "requested_undistorted_video": requested_undistorted_video,
    }
    write_outputs(output_dir, args.camera, result, selected, candidates, errors, image_size)
    return result


def parse_args() -> argparse.Namespace:
    """CLI options. See calibrate() for what each one drives.

    Board defaults must match the physical board; the app passes the same
    values programmatically instead of going through this parser.
    """
    parser = argparse.ArgumentParser(
        description="Intrinsic calibration from nested ChArUco snap images."
    )
    parser.add_argument("--images", default=None, help="Optional glob for calibration images.")
    parser.add_argument("--snaps-dir", default=DEFAULT_SNAPS_DIR, help="Snap folder containing snap_*/cam*.jpg.")
    parser.add_argument("--camera", default=DEFAULT_CAMERA, help="Camera image name to collect, e.g. cam1.")
    parser.add_argument(
        "--model",
        choices=("auto", "pinhole", "fisheye"),
        default="auto",
        help="Calibration model to use. Auto selects the valid model with lower RMS.",
    )
    parser.add_argument("--undistort-image", default=None, help="Optional still image to undistort.")
    parser.add_argument("--undistort-video", default=None, help="Optional video to undistort.")
    parser.add_argument("--output-dir", default=None, help="Output folder.")
    parser.add_argument("--dictionary", default="DICT_4X4_50", help="OpenCV ArUco dictionary.")
    parser.add_argument("--squares-x", type=int, default=4, help="ChArUco squares across.")
    parser.add_argument("--squares-y", type=int, default=3, help="ChArUco squares down.")
    parser.add_argument("--square-length", type=float, default=0.04, help="Square size in meters.")
    parser.add_argument("--marker-length", type=float, default=0.03, help="Marker size in meters.")
    parser.add_argument("--min-corners", type=int, default=4, help="Minimum ChArUco corners per image.")
    parser.add_argument("--min-images", type=int, default=3, help="Minimum usable images.")
    parser.add_argument("--alpha", type=float, default=0.0, help="Pinhole alpha/fisheye balance; 0 crops, 1 keeps FOV.")
    parser.add_argument("--detect-only", action="store_true", help="Write detections without calibrating.")
    return parser.parse_args()


def main() -> None:
    """CLI entry point: run calibrate() and print a human-readable summary.

    The RMS > 2 px warning is a rule of thumb; a solid ChArUco intrinsic
    calibration typically lands well under 1 px.
    """
    args = parse_args()
    result = calibrate(args)
    output_dir = default_output_dir(args)
    print(f"Used images: {result['used_images']} / {result['total_images']}")
    if args.detect_only:
        print(f"Saved detections: {output_dir / f'detections_{args.camera}.json'}")
        return

    print(f"Selected model: {result['selected_model']}")
    print(f"RMS reprojection error: {result['rms_reprojection_error_px']:.4f} px")
    for model, model_result in result["model_results"].items():
        if not model_result:
            continue
        if "rms_reprojection_error_px" in model_result:
            print(f"{model} RMS: {model_result['rms_reprojection_error_px']:.4f} px")
        elif "error" in model_result:
            print(f"{model} error: {model_result['error'].splitlines()[0]}")
    if result["rms_reprojection_error_px"] > 2.0:
        print("WARNING: RMS is high; capture more board poses for reliable calibration.")
    print("Camera matrix:")
    print(np.array(result["camera_matrix"]))
    print("Distortion coefficients:")
    print(np.array(result["distortion_coefficients"]))
    if result["requested_undistorted_image"]:
        print(f"Undistorted image: {result['requested_undistorted_image']}")
    if result["requested_undistorted_video"]:
        print(f"Undistorted video: {result['requested_undistorted_video']}")
    print(f"Saved: {output_dir / f'calibration_{args.camera}.json'}")


if __name__ == "__main__":
    main()
