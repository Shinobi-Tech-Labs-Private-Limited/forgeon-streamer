#!/usr/bin/env python3
"""Calibrate cam intrinsics from ChArUco snaps and undistort images/videos."""

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
    model: str
    rms: float
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    new_camera_matrix: np.ndarray
    roi: tuple[int, int, int, int]
    rvecs: tuple[np.ndarray, ...] | list[np.ndarray]
    tvecs: tuple[np.ndarray, ...] | list[np.ndarray]


def make_board(args: argparse.Namespace) -> tuple[Any, Any]:
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
    if args.images:
        return sorted(Path(".").glob(args.images))
    snaps_dir = Path(args.snaps_dir)
    return sorted(snaps_dir.glob(f"*/{args.camera}.jpg"))


def default_output_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    if args.snaps_dir:
        snaps_dir = Path(args.snaps_dir)
        session_dir = snaps_dir.parent if snaps_dir.name == "snaps" else snaps_dir
        return Path(f"calibration_{args.camera}") / session_dir.name
    return Path(f"calibration_{args.camera}")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def artifact_name(image_path: Path) -> str:
    return f"{image_path.parent.name}_{image_path.name}"


def detect_charuco(
    image_path: Path,
    dictionary: Any,
    board: Any,
    min_corners: int,
) -> tuple[np.ndarray | None, np.ndarray | None, tuple[int, int] | None, np.ndarray | None, int]:
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
    object_points, image_points = charuco_object_points(board, all_ids, all_corners)
    camera_matrix = np.zeros((3, 3), dtype=np.float64)
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)
    flags = (
        cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
        | cv2.fisheye.CALIB_CHECK_COND
        | cv2.fisheye.CALIB_FIX_SKEW
    )
    rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.fisheye.calibrate(
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
