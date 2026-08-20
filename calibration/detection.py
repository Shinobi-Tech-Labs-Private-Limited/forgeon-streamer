"""Cube detection in images: markers -> faces -> ChArUco corners -> 2D<->3D pairs.

Pipeline per image:

1. Detect ALL ArUco markers once (one tuned ArucoDetector).
2. Group detections by cube face via the unique marker-ID ranges. A camera near
   a cube corner legitimately sees 2-3 faces in one image.
3. For each visible face, interpolate that face's ChArUco chessboard corners
   with the face's own CharucoDetector, passing ONLY that face's markers —
   markers from other faces must never contribute to another board's
   interpolation (they lie on a different plane).
4. Map every ChArUco corner (face, local_id) -> global cube 3D point, giving
   the 2D<->3D correspondences for PnP. Marker corners can be appended as
   supplementary correspondences (marker CENTRES are never used).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .cube_geometry import CubeModel

# Face colours (BGR) for debug overlays.
FACE_COLOURS = {
    "FRONT": (0, 200, 255),
    "RIGHT": (0, 220, 0),
    "BACK": (255, 120, 0),
    "LEFT": (200, 0, 200),
    "TOP": (0, 0, 255),
    "BOTTOM": (180, 180, 0),
}


def _tuned_detector_parameters() -> "cv2.aruco.DetectorParameters":
    # Based on the field-tuned parameters in
    # player_anthropometrics/anthropometric_analysis_rtmlib.py, except
    # minMarkerDistanceRate: 0.01 there (tuned for one lone marker) lets the
    # nested inner/outer quads of the SAME marker both survive under blur,
    # producing duplicate IDs — 0.05 merges them.
    params = cv2.aruco.DetectorParameters()
    params.adaptiveThreshWinSizeMin = 3
    params.adaptiveThreshWinSizeMax = 23
    params.adaptiveThreshWinSizeStep = 10
    params.minMarkerPerimeterRate = 0.01
    params.maxMarkerPerimeterRate = 4.0
    params.polygonalApproxAccuracyRate = 0.01
    params.minCornerDistanceRate = 0.01
    params.minDistanceToBorder = 1
    params.minMarkerDistanceRate = 0.05
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return params


@dataclass
class FaceDetection:
    face: str
    marker_ids: list[int]
    marker_corners_px: list[np.ndarray]              # each (4, 2)
    charuco_local_ids: np.ndarray                    # (N,)
    charuco_corners_px: np.ndarray                   # (N, 2)


@dataclass
class CubeDetection:
    faces: dict[str, FaceDetection] = field(default_factory=dict)
    unknown_marker_ids: list[int] = field(default_factory=list)
    # Stacked 2D<->3D correspondences across all faces:
    object_points_cube_mm: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    image_points_px: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    point_faces: list[str] = field(default_factory=list)

    @property
    def n_correspondences(self) -> int:
        return int(self.object_points_cube_mm.shape[0])

    @property
    def visible_faces(self) -> list[str]:
        return list(self.faces.keys())


class CubeDetector:
    def __init__(self, model: CubeModel):
        self.model = model
        self._aruco = cv2.aruco.ArucoDetector(
            model.cfg.aruco_dictionary, _tuned_detector_parameters()
        )
        self._charuco = {
            face: cv2.aruco.CharucoDetector(fb.board)
            for face, fb in model.face_boards.items()
        }

    # Marker detection runs on a copy downscaled to at most this many pixels
    # on the long side (the adaptive-threshold windows don't scale to
    # thousand-pixel squares); ChArUco corner refinement still uses the
    # full-resolution image, so no sub-pixel accuracy is lost.
    MAX_DETECT_DIM = 2000

    def detect(
        self,
        image: np.ndarray,
        min_markers_per_face: int = 2,
        include_marker_corners: bool = True,
    ) -> CubeDetection:
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        scale = min(1.0, self.MAX_DETECT_DIM / max(gray.shape))
        if scale < 1.0:
            detect_gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        else:
            detect_gray = gray
        corners, ids, _ = self._aruco.detectMarkers(detect_gray)

        result = CubeDetection()
        if ids is None or len(ids) == 0:
            return result
        if scale < 1.0:
            corners = [c / scale for c in corners]

        # Group marker detections by face, keeping ONE detection per marker ID
        # (largest perimeter wins — duplicates would double-weight the PnP and
        # can corrupt ChArUco interpolation).
        best_by_id: dict[int, np.ndarray] = {}
        for marker_corners, marker_id in zip(corners, ids.ravel().tolist()):
            marker_id = int(marker_id)
            if self.model.face_of_marker(marker_id) is None:
                if marker_id not in result.unknown_marker_ids:
                    result.unknown_marker_ids.append(marker_id)
                continue
            previous = best_by_id.get(marker_id)
            if previous is not None and cv2.arcLength(
                previous.reshape(-1, 1, 2).astype(np.float32), True
            ) >= cv2.arcLength(marker_corners.reshape(-1, 1, 2).astype(np.float32), True):
                continue
            best_by_id[marker_id] = marker_corners

        by_face: dict[str, list[tuple[int, np.ndarray]]] = {}
        for marker_id, marker_corners in best_by_id.items():
            face = self.model.face_of_marker(marker_id)
            by_face.setdefault(face, []).append((marker_id, marker_corners))

        object_points: list[np.ndarray] = []
        image_points: list[np.ndarray] = []
        point_faces: list[str] = []

        for face, detections in by_face.items():
            if len(detections) < min_markers_per_face:
                continue
            face_marker_ids = np.array([d[0] for d in detections], dtype=np.int32).reshape(-1, 1)
            face_marker_corners = [d[1] for d in detections]

            # Interpolate this face's chessboard corners from ONLY its markers.
            ch_corners, ch_ids, _, _ = self._charuco[face].detectBoard(
                gray, markerCorners=face_marker_corners, markerIds=face_marker_ids
            )
            if ch_ids is None or len(ch_ids) == 0:
                continue

            local_ids = ch_ids.ravel().astype(int)
            corners_px = ch_corners.reshape(-1, 2)
            result.faces[face] = FaceDetection(
                face=face,
                marker_ids=[d[0] for d in detections],
                marker_corners_px=[c.reshape(4, 2) for c in face_marker_corners],
                charuco_local_ids=local_ids,
                charuco_corners_px=corners_px,
            )

            cube_pts = self.model.chessboard_corners_cube[face][local_ids]
            object_points.append(cube_pts)
            image_points.append(corners_px)
            point_faces.extend([face] * len(local_ids))

            if include_marker_corners:
                for marker_id, marker_corners in detections:
                    object_points.append(self.model.marker_corners_cube[marker_id])
                    image_points.append(marker_corners.reshape(4, 2))
                    point_faces.extend([face] * 4)

        if object_points:
            result.object_points_cube_mm = np.concatenate(object_points, axis=0)
            result.image_points_px = np.concatenate(image_points, axis=0)
            result.point_faces = point_faces
        return result

    def draw_debug(self, image: np.ndarray, detection: CubeDetection) -> np.ndarray:
        """Colour-coded overlay: markers, ChArUco corners, face names, counts."""
        canvas = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image.copy()
        for face, det in detection.faces.items():
            colour = FACE_COLOURS.get(face, (255, 255, 255))
            for marker_id, corners in zip(det.marker_ids, det.marker_corners_px):
                pts = corners.astype(np.int32).reshape(-1, 1, 2)
                cv2.polylines(canvas, [pts], True, colour, 2)
                centre = corners.mean(axis=0).astype(int)
                cv2.putText(canvas, str(marker_id), tuple(centre),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
            for local_id, corner in zip(det.charuco_local_ids, det.charuco_corners_px):
                point = tuple(np.round(corner).astype(int))
                cv2.circle(canvas, point, 4, colour, -1)
                cv2.putText(canvas, str(local_id), (point[0] + 5, point[1] - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, colour, 1, cv2.LINE_AA)
            anchor = det.charuco_corners_px.mean(axis=0).astype(int)
            cv2.putText(canvas, face, (anchor[0] - 30, anchor[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, colour, 2, cv2.LINE_AA)
        summary = (
            f"faces: {','.join(detection.visible_faces) or 'none'}  "
            f"correspondences: {detection.n_correspondences}"
        )
        cv2.putText(canvas, summary, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        return canvas
