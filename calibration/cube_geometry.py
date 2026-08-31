"""Cube coordinate system, face-to-cube transforms, and the global 3D corner DB.

Cube frame (right-handed, Z-up), origin at the cube centre:

    +X = outward normal of RIGHT      -X = LEFT
    +Y = outward normal of BACK       -Y = FRONT
    +Z = outward normal of TOP        -Z = BOTTOM

Stand in front of the cube (at -Y, looking along +Y): X is your right, Z is up.

Board (face-local) frame — measured empirically from OpenCV 4.9 CharucoBoard
object points: origin at the print's top-left, +x_board = print-right,
+y_board = print-DOWN, z = 0 on the pattern plane. Consequently
z_board = x_board × y_board points INTO the cube when the print is mounted,
i.e. the outward face normal is -z_board. Each face's rotation below is built
from where "print-right" and "print-down" go in cube coordinates; validators
assert z_board maps to -outward_normal and det(R) = +1.

The mounting orientations are chosen so a standard cross net
(LEFT-FRONT-RIGHT-BACK in a row, TOP above FRONT, BOTTOM below FRONT, all
tiles printed upright) folds into exactly these transforms with no per-face
rotation during assembly.

AS-BUILT deviations: if a print was pasted a quarter-turn (or half-turn) off
the design orientation, record it in config.yaml under
``mounting.face_rotation_deg`` (degrees, counter-clockwise as seen from
OUTSIDE the cube looking at that face). ``face_axes()`` rotates the design
print-right / print-down vectors about the outward normal by that angle, so
every downstream transform, corner database and solver sees the cube as it
physically is. Likewise ``mounting.face_shift_mm`` records where the pasted
pattern's centre actually sits relative to the face centre (mm right/up, seen
from outside in the cross-net orientation) and shifts the board placement.
FACE_AXES itself always stays the DESIGN (what the print assets and the
handbook assume).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .boards import FaceBoard, build_face_boards, marker_to_face_map
from .config import CalibrationConfig

FACES = ("FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM")
# Canonical face names (cfg.face_order may list them in a different order).

# face -> (outward normal, print-right direction, print-down direction), cube frame.
FACE_AXES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = {
    "FRONT":  ((0, -1, 0), (1, 0, 0),  (0, 0, -1)),
    "RIGHT":  ((1, 0, 0),  (0, 1, 0),  (0, 0, -1)),
    "BACK":   ((0, 1, 0),  (-1, 0, 0), (0, 0, -1)),
    "LEFT":   ((-1, 0, 0), (0, -1, 0), (0, 0, -1)),
    "TOP":    ((0, 0, 1),  (1, 0, 0),  (0, -1, 0)),
    "BOTTOM": ((0, 0, -1), (1, 0, 0),  (0, 1, 0)),
}


def _rotate_about_axis(v: np.ndarray, axis: np.ndarray, deg: float) -> np.ndarray:
    """Rodrigues rotation of v about unit axis by deg (right-hand rule)."""
    th = np.radians(deg)
    return (
        v * np.cos(th)
        + np.cross(axis, v) * np.sin(th)
        + axis * np.dot(axis, v) * (1.0 - np.cos(th))
    )


def face_axes(cfg: CalibrationConfig, face: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(outward normal, print-right, print-down) in the cube frame, AS BUILT.

    Starts from the design FACE_AXES and applies cfg.rotation_for_face(face):
    a positive (right-hand-rule) rotation about the outward normal, which is
    exactly a counter-clockwise turn of the paper as seen by someone outside
    the cube looking at that face.
    """
    normal, print_right, print_down = (np.array(v, dtype=np.float64) for v in FACE_AXES[face])
    deg = cfg.rotation_for_face(face)
    if deg:
        print_right = np.round(_rotate_about_axis(print_right, normal, deg))
        print_down = np.round(_rotate_about_axis(print_down, normal, deg))
        # Quarter turns of unit axis vectors are exact; round() strips the
        # cos/sin floating-point noise so the axes stay clean integers.
    return normal, print_right, print_down


def T_cube_from_face(cfg: CalibrationConfig, face: str) -> np.ndarray:
    """4x4 homogeneous transform: P_cube = T_cube_from_face @ P_face.

    P_face is a board-frame point [x, y, 0, 1]^T in mm; the board's centre is
    placed at the centre of the cube face, on the pattern plane
    (cube_size/2 + pattern_offset along the outward normal). Honours the
    as-built mounting rotation from config (see face_axes).
    """
    normal, print_right, print_down = face_axes(cfg, face)
    z_board = np.cross(print_right, print_down)  # = -normal by construction
    # Columns of R are the board's x, y, z axes expressed in the cube frame.

    R = np.column_stack([print_right, print_down, z_board])
    board_centre = np.array(
        [cfg.board_width_mm / 2.0, cfg.board_height_mm / 2.0, 0.0]
    )
    face_centre = normal * (cfg.cube_size_mm / 2.0 + cfg.pattern_offset_mm)
    # As-built off-centre paste: (right, up) in the DESIGN screen frame of the
    # face (design print-right, -design print-down), independent of rotation.
    shift_r, shift_u = cfg.shift_for_face(face)
    if shift_r or shift_u:
        design_right = np.array(FACE_AXES[face][1], dtype=np.float64)
        design_up = -np.array(FACE_AXES[face][2], dtype=np.float64)
        face_centre = face_centre + shift_r * design_right + shift_u * design_up
    # Choose t so the board centre lands on the (shifted) face centre:
    # face_centre = R @ board_centre + t.
    t = face_centre - R @ board_centre

    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def transform_points(T: np.ndarray, points_xyz: np.ndarray) -> np.ndarray:
    """Apply a 4x4 transform to (N, 3) points."""
    # Row-vector form of P' = R @ P + t for every row of points_xyz.
    pts = np.asarray(points_xyz, dtype=np.float64)
    return pts @ T[:3, :3].T + T[:3, 3]


class CubeModel:
    """The single mathematical model of the physical cube.

    Answers: "I detected ChArUco corner N on face F (or ArUco marker M) —
    what is its exact 3D position on the cube?"
    """

    def __init__(self, cfg: CalibrationConfig | None = None):
        """Build boards, the marker->face map, T_cube_from_face and all 3D corners.

        Args:
            cfg: configuration to model; defaults to ``CalibrationConfig.load()``.
        """
        self.cfg = cfg or CalibrationConfig.load()
        self.face_boards: dict[str, FaceBoard] = build_face_boards(self.cfg)
        self.marker_to_face: dict[int, str] = marker_to_face_map(self.cfg)
        self.T_cube_from_face: dict[str, np.ndarray] = {
            face: T_cube_from_face(self.cfg, face) for face in self.cfg.face_order
        }

        # ChArUco chessboard corners: face -> (N, 3) cube coords, row = local id.
        self.chessboard_corners_cube: dict[str, np.ndarray] = {}
        # ArUco marker corners: marker_id -> (4, 3) cube coords (corner order as
        # detected by OpenCV: top-left, top-right, bottom-right, bottom-left in
        # the print).
        self.marker_corners_cube: dict[int, np.ndarray] = {}

        for face, fb in self.face_boards.items():
            T = self.T_cube_from_face[face]
            self.chessboard_corners_cube[face] = transform_points(
                T, fb.chessboard_corners_board_mm
            )
            for marker_id, corners in fb.marker_corners_board_mm.items():
                self.marker_corners_cube[marker_id] = transform_points(T, corners)

    # ---- lookups -----------------------------------------------------------

    def corner_cube_mm(self, face: str, local_corner_id: int) -> np.ndarray:
        """Global 3D position (mm, cube frame) of ChArUco corner (face, id)."""
        return self.chessboard_corners_cube[face][local_corner_id]

    def face_of_marker(self, marker_id: int) -> str | None:
        """Face carrying ArUco ``marker_id``, or None if the ID is not on the cube."""
        return self.marker_to_face.get(int(marker_id))

    def outward_normal(self, face: str) -> np.ndarray:
        """Unit outward normal of ``face`` in the cube frame (design; rotation-independent)."""
        return np.array(FACE_AXES[face][0], dtype=np.float64)

    # ---- export ------------------------------------------------------------

    def export(self, out_dir: str | Path) -> dict[str, Path]:
        """Write cube_points_3d.json, marker_face_map.json, transforms.json."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        corners = []
        for face in self.cfg.face_order:
            local = self.face_boards[face].chessboard_corners_board_mm
            cube = self.chessboard_corners_cube[face]
            for local_id, (p_local, p_cube) in enumerate(zip(local, cube)):
                corners.append(
                    {
                        "global_key": f"{face}:{local_id}",
                        "face": face,
                        "local_corner_id": local_id,
                        "local_xyz_mm": [round(float(v), 6) for v in p_local],
                        "cube_xyz_mm": [round(float(v), 6) for v in p_cube],
                    }
                )
        markers = {
            str(marker_id): {
                "face": self.marker_to_face[marker_id],
                "corners_cube_mm": [[round(float(v), 6) for v in c] for c in corners_cube],
            }
            for marker_id, corners_cube in sorted(self.marker_corners_cube.items())
        }
        transforms = {
            f"T_cube_from_{face.lower()}": [[round(float(v), 12) for v in row] for row in T]
            for face, T in self.T_cube_from_face.items()
        }

        paths = {
            "cube_points_3d": out_dir / "cube_points_3d.json",
            "marker_face_map": out_dir / "marker_face_map.json",
            "transforms": out_dir / "face_transforms.json",
        }
        meta = {
            "units": "mm",
            "frame": "cube centre origin; +X=RIGHT, +Y=BACK, +Z=TOP outward normals",
            "cube_size_mm": self.cfg.cube_size_mm,
            "dictionary": self.cfg.dictionary_name,
        }
        paths["cube_points_3d"].write_text(
            json.dumps({**meta, "chessboard_corners": corners}, indent=2), encoding="utf-8"
        )
        paths["marker_face_map"].write_text(
            json.dumps({**meta, "markers": markers}, indent=2), encoding="utf-8"
        )
        paths["transforms"].write_text(
            json.dumps({**meta, "transforms": transforms}, indent=2), encoding="utf-8"
        )
        return paths
