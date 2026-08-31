"""Load and validate the calibration-cube configuration (config.yaml).

All physical dimensions flow from here. Modules must take a CalibrationConfig
rather than re-declaring lengths.

Two kinds of value live in config.yaml: the DESIGN (cube size, board layout,
marker-ID layout, print DPI) and the AS-BUILT mounting corrections measured on
the physical cube (per-face quarter-turn rotation and off-centre paste shift).
The as-built values are what make the solver see the real cube; never edit
config.yaml unless a face has physically been re-pasted and re-measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import yaml

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class CameraSpec:
    """Sensor/lens spec used only by the simulators."""

    name: str
    # Image size in pixels and focal lengths in pixel units (fx = f / pixel pitch).
    width: int
    height: int
    fx_px: float
    fy_px: float
    note: str = ""


@dataclass(frozen=True)
class CalibrationConfig:
    """Validated, immutable view of config.yaml plus derived geometry.

    Lengths are millimetres. ``face_order`` fixes the marker-ID allocation:
    face i uses IDs [i * id_stride, i * id_stride + markers_per_face). Build it
    with ``CalibrationConfig.load()``; the constructor does not validate.
    """

    cube_size_mm: float
    pattern_offset_mm: float
    dictionary_name: str
    squares_x: int
    squares_y: int
    square_length_mm: float
    marker_length_mm: float
    min_margin_mm: float
    id_stride: int
    face_order: tuple[str, ...]
    print_dpi: int
    cameras: dict[str, CameraSpec] = field(default_factory=dict)
    # AS-BUILT: in-plane rotation of each pasted print relative to the design
    # orientation (cube_geometry.FACE_AXES), degrees, counter-clockwise as seen
    # from OUTSIDE the cube looking at that face. Multiples of 90 only.
    face_rotation_deg: dict[str, int] = field(default_factory=dict)
    # AS-BUILT: where the pasted pattern's centre sits relative to the face
    # centre, mm, as (right, up) seen from OUTSIDE the cube looking at that
    # face in the cross-net orientation (side faces: viewer upright, TOP: up =
    # toward BACK, BOTTOM: up = toward FRONT). Independent of the rotation.
    face_shift_mm: dict[str, tuple[float, float]] = field(default_factory=dict)

    # ---- derived geometry -------------------------------------------------

    @property
    def board_width_mm(self) -> float:
        """Printed board width (squares_x * square length), mm, excluding margins."""
        return self.squares_x * self.square_length_mm

    @property
    def board_height_mm(self) -> float:
        """Printed board height (squares_y * square length), mm, excluding margins."""
        return self.squares_y * self.square_length_mm

    @property
    def margin_mm(self) -> float:
        """White margin between the board edge and the cube edge (per side)."""
        return (self.cube_size_mm - max(self.board_width_mm, self.board_height_mm)) / 2.0

    @property
    def markers_per_face(self) -> int:
        """Number of ArUco markers on one face's board."""
        # New (non-legacy) ChArUco pattern: first square black, markers on the
        # white squares -> floor(squares/2).
        return (self.squares_x * self.squares_y) // 2

    @property
    def chessboard_corners_per_face(self) -> int:
        """Number of interior ChArUco corners on one face (local IDs 0..N-1)."""
        return (self.squares_x - 1) * (self.squares_y - 1)

    @property
    def aruco_dictionary(self) -> "cv2.aruco.Dictionary":
        """The predefined cv2.aruco dictionary named by ``dictionary_name``."""
        return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary_name))

    @property
    def dictionary_size(self) -> int:
        """Number of marker codes in the dictionary (highest usable ID + 1)."""
        return int(self.aruco_dictionary.bytesList.shape[0])

    def rotation_for_face(self, face: str) -> int:
        """As-built in-plane rotation of the pasted print, degrees CCW seen from outside."""
        return int(self.face_rotation_deg.get(face, 0)) % 360

    def shift_for_face(self, face: str) -> tuple[float, float]:
        """As-built (right, up) offset of the pattern centre from the face centre, mm."""
        r, u = self.face_shift_mm.get(face, (0.0, 0.0))
        return float(r), float(u)

    def marker_ids_for_face(self, face: str) -> list[int]:
        """The contiguous, cube-unique ArUco marker IDs allocated to ``face``."""
        idx = self.face_order.index(face)
        start = idx * self.id_stride
        return list(range(start, start + self.markers_per_face))

    # ---- loading / validation --------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CalibrationConfig":
        """Read a YAML config (default: calibration/config.yaml), validate, return it.

        Raises ValueError (via ``validate``) if the geometry or ID layout is
        inconsistent, and KeyError if a required section is missing.
        """
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        cameras = {
            name: CameraSpec(name=name, **spec)
            for name, spec in (raw.get("cameras") or {}).items()
        }
        cfg = cls(
            cube_size_mm=float(raw["cube"]["size_mm"]),
            pattern_offset_mm=float(raw["cube"].get("pattern_offset_mm", 0.0)),
            dictionary_name=str(raw["charuco"]["dictionary"]),
            squares_x=int(raw["charuco"]["squares_x"]),
            squares_y=int(raw["charuco"]["squares_y"]),
            square_length_mm=float(raw["charuco"]["square_length_mm"]),
            marker_length_mm=float(raw["charuco"]["marker_length_mm"]),
            min_margin_mm=float(raw["charuco"].get("min_margin_mm", 15.0)),
            id_stride=int(raw["ids"]["stride"]),
            face_order=tuple(raw["ids"]["face_order"]),
            print_dpi=int(raw["printing"]["dpi"]),
            cameras=cameras,
            face_rotation_deg={
                str(k).upper(): int(v)
                for k, v in ((raw.get("mounting") or {}).get("face_rotation_deg") or {}).items()
            },
            face_shift_mm={
                str(k).upper(): (float(v.get("right", 0.0)), float(v.get("up", 0.0)))
                for k, v in ((raw.get("mounting") or {}).get("face_shift_mm") or {}).items()
            },
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Check geometric and ID-layout invariants; raise ValueError listing all problems.

        Checks: all six faces named once; marker fits inside its square; the
        board leaves at least ``min_margin_mm`` of white on each side; per-face
        ID ranges do not overlap and fit in the dictionary; as-built rotations
        are quarter turns of known faces; as-built shifts keep the pattern on
        the face.
        """
        problems: list[str] = []
        if sorted(self.face_order) != sorted(["FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM"]):
            problems.append(f"face_order must name the six cube faces exactly once, got {self.face_order}")
        if self.marker_length_mm >= self.square_length_mm:
            problems.append("marker_length_mm must be smaller than square_length_mm")
        if self.margin_mm < self.min_margin_mm:
            problems.append(
                f"board {self.board_width_mm:.0f}x{self.board_height_mm:.0f} mm leaves only "
                f"{self.margin_mm:.1f} mm margin on a {self.cube_size_mm:.0f} mm face "
                f"(min {self.min_margin_mm:.0f} mm)"
            )
        if self.id_stride < self.markers_per_face:
            problems.append(
                f"id stride {self.id_stride} < markers per face {self.markers_per_face}: ID ranges would overlap"
            )
        max_id = (len(self.face_order) - 1) * self.id_stride + self.markers_per_face - 1
        if max_id >= self.dictionary_size:
            problems.append(
                f"highest marker ID {max_id} does not fit in {self.dictionary_name} (size {self.dictionary_size})"
            )
        for face, deg in self.face_rotation_deg.items():
            if face not in self.face_order:
                problems.append(f"mounting.face_rotation_deg names unknown face {face!r}")
            if deg % 90 != 0:
                problems.append(
                    f"mounting.face_rotation_deg[{face}] = {deg}: prints can only be pasted "
                    f"in quarter turns (0/90/180/270)"
                )
        for face, (r, u) in self.face_shift_mm.items():
            if face not in self.face_order:
                problems.append(f"mounting.face_shift_mm names unknown face {face!r}")
            if max(abs(r), abs(u)) > self.margin_mm:
                problems.append(
                    f"mounting.face_shift_mm[{face}] = ({r}, {u}) exceeds the {self.margin_mm:.0f} mm "
                    f"margin: the pattern would hang off the face"
                )
        if problems:
            raise ValueError("Invalid calibration config:\n- " + "\n- ".join(problems))
