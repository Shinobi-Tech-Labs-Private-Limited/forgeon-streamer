"""Find the focus target in a frame and hand back the crop to score.

Target = the ChArUco calibration CUBE (backend/app/calibration/) — the thing
the rig is calibrated with, so focusing on it focuses where it matters. Any
face(s) in view will do: the crop is the convex hull of every detected marker
corner across faces, so the room, floor and operator never enter the score.

Fallback (calibration package not importable, or no cube marker seen) is the
legacy flat 4x3 ChArUco board (DICT_4X4_50) this tool was born with.

`detect_and_segment(frame)` returns:
    detected   bool
    target     'cube' | 'board' | None
    board_crop BGR crop of the target region (None if not detected)
    annotated  frame with detections + hull drawn
    corners    number of interpolated ChArUco corners (all faces)
    markers    number of ArUco markers
    faces      list of cube faces seen (cube only)
    square_px  checker square side in pixels (median nearest-neighbour
               distance between ChArUco corners) — feeds scale normalisation
"""

import cv2
import numpy as np

# ---- cube detector (preferred) ------------------------------------------
try:  # rig checkout: backend/app is the cwd, so `calibration` is importable
    from calibration.config import CalibrationConfig as _CubeConfig
    from calibration.cube_geometry import CubeModel as _CubeModel
    from calibration.detection import CubeDetector as _CubeDetector
    CUBE_AVAILABLE = True
    CUBE_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover
    try:  # backend package layout (tests, FastAPI app)
        from app.calibration.config import CalibrationConfig as _CubeConfig
        from app.calibration.cube_geometry import CubeModel as _CubeModel
        from app.calibration.detection import CubeDetector as _CubeDetector
        CUBE_AVAILABLE = True
        CUBE_IMPORT_ERROR = None
    except Exception as _exc2:  # pragma: no cover
        CUBE_AVAILABLE = False
        CUBE_IMPORT_ERROR = f"{_exc} / {_exc2}"

_CUBE_DETECTOR = None


def _cube_detector():
    global _CUBE_DETECTOR
    if _CUBE_DETECTOR is None:
        _CUBE_DETECTOR = _CubeDetector(_CubeModel(_CubeConfig.load()))
    return _CUBE_DETECTOR


# ---- legacy flat board (fallback) -----------------------------------------
DICT   = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
BOARD  = cv2.aruco.CharucoBoard((4, 3), 0.04, 0.03, DICT)
PARAMS = cv2.aruco.DetectorParameters()

HULL_PAD_FRAC = 0.10  # grow the hull a little so edge squares are fully inside


def _empty(frame):
    return {
        'detected':   False,
        'target':     None,
        'board_crop': None,
        'annotated':  frame.copy(),
        'corners':    0,
        'markers':    0,
        'faces':      [],
        'square_px':  None,
    }


def _square_px(points_xy):
    """Median nearest-neighbour distance among (N,2) points, or None."""
    pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return None
    nn = []
    for i in range(len(pts)):
        d = np.linalg.norm(pts - pts[i], axis=1)
        d[i] = np.inf
        nn.append(d.min())
    return float(np.median(nn))


def _hull_and_crop(frame, all_pts):
    h, w = frame.shape[:2]
    pts = np.asarray(all_pts, dtype=np.float32).reshape(-1, 1, 2)
    hull = cv2.convexHull(pts).reshape(-1, 2)
    centre = hull.mean(axis=0)
    hull = centre + (hull - centre) * (1.0 + HULL_PAD_FRAC)
    hull[:, 0] = np.clip(hull[:, 0], 0, w - 1)
    hull[:, 1] = np.clip(hull[:, 1], 0, h - 1)
    hull_i = np.round(hull).astype(np.int32)
    x, y, bw, bh = cv2.boundingRect(hull_i)
    return hull_i, frame[y:y + bh, x:x + bw]


def _detect_cube(frame):
    detector = _cube_detector()
    det = detector.detect(frame, min_markers_per_face=1)
    n_markers = sum(len(fd.marker_ids) for fd in det.faces.values())
    if n_markers == 0:
        return None
    all_pts = np.concatenate(
        [np.asarray(c, dtype=np.float32) for fd in det.faces.values() for c in fd.marker_corners_px]
    )
    hull, crop = _hull_and_crop(frame, all_pts)
    charuco_pts = [fd.charuco_corners_px for fd in det.faces.values() if len(fd.charuco_local_ids)]
    n_corners = int(sum(len(p) for p in charuco_pts))
    # Square size from the MARKER sides (always present when the cube is
    # found) times the square/marker ratio. Nearest-neighbour distances among
    # ChArUco corners are NOT used: with sparse corners (blur, partial face)
    # they span two squares and the scale normalisation then flatters a
    # blurrier image.
    sides = [np.mean(np.linalg.norm(np.roll(np.asarray(c), -1, axis=0) - np.asarray(c), axis=1))
             for fd in det.faces.values() for c in fd.marker_corners_px]
    cfg = detector.model.cfg
    square_px = float(np.median(sides)) * cfg.square_length_mm / cfg.marker_length_mm
    annotated = detector.draw_debug(frame, det)
    cv2.polylines(annotated, [hull], True, (0, 255, 0), 2)
    return {
        'detected':   True,
        'target':     'cube',
        'board_crop': crop,
        'annotated':  annotated,
        'corners':    n_corners,
        'markers':    int(n_markers),
        'faces':      sorted(det.faces.keys()),
        'square_px':  square_px,
    }


def _detect_flat_board(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    detector = cv2.aruco.ArucoDetector(DICT, PARAMS)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None or len(ids) < 4:
        return None
    ret, ch_corners, ch_ids = cv2.aruco.interpolateCornersCharuco(corners, ids, gray, BOARD)
    if ret < 4:
        return None
    all_pts = np.array([c[0] for c in corners], dtype=np.float32).reshape(-1, 2)
    hull, crop = _hull_and_crop(frame, all_pts)
    annotated = frame.copy()
    cv2.aruco.drawDetectedMarkers(annotated, corners, ids)
    cv2.aruco.drawDetectedCornersCharuco(annotated, ch_corners, ch_ids)
    cv2.polylines(annotated, [hull], True, (0, 255, 0), 2)
    return {
        'detected':   True,
        'target':     'board',
        'board_crop': crop,
        'annotated':  annotated,
        'corners':    int(ret),
        'markers':    int(len(ids)),
        'faces':      [],
        'square_px':  _square_px(ch_corners.reshape(-1, 2)) if ret >= 2 else None,
    }


def detect_and_segment(frame, target='auto'):
    """Detect ``cube``, ``board``, or prefer cube with board fallback (``auto``)."""
    target = str(target or 'auto').strip().lower()
    if target not in ('auto', 'cube', 'board'):
        raise ValueError(f"Unknown focus target: {target}")
    result = _empty(frame)
    if target in ('auto', 'cube') and CUBE_AVAILABLE:
        found = _detect_cube(frame)
        if found:
            return found
    if target in ('auto', 'board'):
        found = _detect_flat_board(frame)
        if found:
            return found
    return result
