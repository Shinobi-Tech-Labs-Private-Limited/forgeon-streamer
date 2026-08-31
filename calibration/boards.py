"""Per-face ChArUco board construction with cube-unique marker IDs.

ArUco marker ID vs ChArUco corner ID — these are different things:

- ArUco marker IDs are the codes inside the black squares. We assign each face
  a disjoint range, so any single detected marker identifies its face.
- ChArUco (chessboard) corner IDs are the interior checker intersections of one
  board, always numbered locally 0..N-1 by OpenCV. Every face has the same
  local corner IDs, so a corner is only unambiguous as (face, local_corner_id).

Markers identify; interpolated chessboard corners localize (sub-pixel).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import CalibrationConfig


@dataclass(frozen=True)
class FaceBoard:
    """One cube face's ChArUco board and the marker IDs it carries.

    ``board`` is an OpenCV CharucoBoard built in millimetres, so every object
    point it reports is in the board (face-local) frame: origin at the print's
    top-left, +x print-right, +y print-down, z = 0. ``marker_ids`` is the
    disjoint ID range allocated to this face, in board order.
    """

    face: str
    board: "cv2.aruco.CharucoBoard"
    marker_ids: tuple[int, ...]

    @property
    def chessboard_corners_board_mm(self) -> np.ndarray:
        """(N, 3) local board-frame coordinates of the ChArUco corners, mm."""
        return np.asarray(self.board.getChessboardCorners(), dtype=np.float64)

    @property
    def marker_corners_board_mm(self) -> dict[int, np.ndarray]:
        """marker_id -> (4, 3) local board-frame corner coordinates, mm."""
        obj_pts = self.board.getObjPoints()
        ids = self.board.getIds().ravel().tolist()
        return {int(i): np.asarray(p, dtype=np.float64) for i, p in zip(ids, obj_pts)}


def build_face_boards(cfg: CalibrationConfig) -> dict[str, FaceBoard]:
    """One CharucoBoard per face, each with its own disjoint marker-ID range.

    Uses the modern (non-legacy) ChArUco pattern: first square black. All
    lengths are passed in millimetres, so board object points come out in mm.
    """
    dictionary = cfg.aruco_dictionary
    boards: dict[str, FaceBoard] = {}
    for face in cfg.face_order:
        ids = np.asarray(cfg.marker_ids_for_face(face), dtype=np.int32)
        # ids= replaces OpenCV's default 0..N-1 numbering with this face's own
        # range; that is the whole mechanism by which one marker identifies a
        # face. Size is (columns, rows).
        board = cv2.aruco.CharucoBoard(
            (cfg.squares_x, cfg.squares_y),
            cfg.square_length_mm,
            cfg.marker_length_mm,
            dictionary,
            ids=ids,
        )
        boards[face] = FaceBoard(face=face, board=board, marker_ids=tuple(int(i) for i in ids))
    return boards


def marker_to_face_map(cfg: CalibrationConfig) -> dict[int, str]:
    """marker_id -> face name, for face identification from detections."""
    mapping: dict[int, str] = {}
    for face in cfg.face_order:
        for marker_id in cfg.marker_ids_for_face(face):
            mapping[marker_id] = face
    return mapping


def validate_ids(cfg: CalibrationConfig) -> None:
    """Assert the cube-wide marker-ID invariants. Raises ValueError on failure."""
    problems: list[str] = []
    seen: dict[int, str] = {}
    for face in cfg.face_order:
        ids = cfg.marker_ids_for_face(face)
        if len(ids) != cfg.markers_per_face:
            problems.append(f"{face}: expected {cfg.markers_per_face} markers, got {len(ids)}")
        for marker_id in ids:
            if marker_id in seen:
                problems.append(f"marker ID {marker_id} on both {seen[marker_id]} and {face}")
            seen[marker_id] = face
            if not (0 <= marker_id < cfg.dictionary_size):
                problems.append(f"marker ID {marker_id} outside {cfg.dictionary_name}")

    # Every board must actually carry the IDs we allocated.
    for face, fb in build_face_boards(cfg).items():
        if list(fb.marker_ids) != cfg.marker_ids_for_face(face):
            problems.append(f"{face}: board IDs {fb.marker_ids} != allocated {cfg.marker_ids_for_face(face)}")

    if problems:
        raise ValueError("Marker ID validation failed:\n- " + "\n- ".join(problems))
