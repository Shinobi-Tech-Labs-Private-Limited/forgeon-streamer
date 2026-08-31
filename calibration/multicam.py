"""Multi-camera extrinsics from per-camera cube poses.

Why cameras seeing different faces can still be calibrated: every observation,
whatever face it lands on, maps to a known 3D point in the SAME rigid cube
frame. So each camera independently yields T_cameraK_from_cube, and any pair
is related through the cube:

    T_cam2_from_cam1 = T_cam2_from_cube @ inv(T_cam1_from_cube)
                     = T_cam2_from_cube @ T_cube_from_cam1

This assumes all images used for one joint solve observe the cube in ONE fixed
pose (the cube frame is the shared world frame). If the cube moves between
shots, frames from different cube placements live in different world frames
and must not be mixed — group images per placement and chain through cameras
that share placements instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

from .config import CalibrationConfig
from .pose import PoseEstimate, average_poses, invert_T, pose_spread


@dataclass
class CameraExtrinsics:
    """Fused extrinsics of one camera plus the statistics that justify them.

    ``T_camera_from_cube`` is the SE(3) mean of the accepted per-image poses
    (cube-frame mm into camera frame). ``rms_px``/``median_px``/``max_px`` pool
    the per-point reprojection residuals of every accepted image. The two
    ``pose_*_spread`` fields are the largest rotation (deg) and translation
    (mm) distance of any accepted image pose from the mean: large values mean
    the cube or camera moved, or the intrinsics are wrong.
    """

    name: str
    T_camera_from_cube: np.ndarray
    n_images_used: int
    n_images_rejected: int
    rms_px: float
    median_px: float
    max_px: float
    pose_rotation_spread_deg: float
    pose_translation_spread_mm: float

    @property
    def T_cube_from_camera(self) -> np.ndarray:
        """Inverse pose: camera-frame points into the cube frame."""
        return invert_T(self.T_camera_from_cube)

    @property
    def camera_centre_cube_mm(self) -> np.ndarray:
        """Optical centre of the camera in cube coordinates, mm."""
        return self.T_cube_from_camera[:3, 3]


def combine_camera_estimates(
    name: str,
    estimates: list[PoseEstimate],
    max_rms_px: float = 3.0,
    reject_ambiguous: bool = True,
) -> CameraExtrinsics:
    """Fuse per-image pose estimates for one camera into one extrinsic.

    Rejects ambiguous single-face poses and high-reprojection outliers, then
    averages the survivors on SE(3) (quaternion mean, not naive matrix mean).
    """
    kept = [
        e for e in estimates
        if e.rms_px <= max_rms_px and not (reject_ambiguous and e.ambiguous)
    ]
    # max_rms_px = 3 px is far above the ~0.3-1 px a good frame gives, so it
    # removes gross failures (mis-oriented face, bad detection) rather than
    # trimming ordinary noise.
    if not kept:
        raise ValueError(
            f"{name}: no usable pose estimates "
            f"({len(estimates)} images, all rejected — check detections/intrinsics)"
        )
    T_mean = average_poses([e.T_camera_from_cube for e in kept])
    spread = pose_spread([e.T_camera_from_cube for e in kept], T_mean)
    all_errors = np.concatenate([e.per_point_px for e in kept])
    return CameraExtrinsics(
        name=name,
        T_camera_from_cube=T_mean,
        n_images_used=len(kept),
        n_images_rejected=len(estimates) - len(kept),
        rms_px=float(np.sqrt(np.mean(all_errors**2))),
        median_px=float(np.median(all_errors)),
        max_px=float(all_errors.max()),
        pose_rotation_spread_deg=spread["rotation_max_deg"],
        pose_translation_spread_mm=spread["translation_max_mm"],
    )


def T_camB_from_camA(cam_a: CameraExtrinsics, cam_b: CameraExtrinsics) -> np.ndarray:
    """Relative pose mapping camera-A coordinates into camera-B coordinates.

    T_camB_from_camA = T_camB_from_cube @ T_cube_from_camA. Both cameras must
    have been solved against the same cube placement.
    """
    return cam_b.T_camera_from_cube @ cam_a.T_cube_from_camera


# ---------------------------------------------------------------------------
# World frame: cube centre (solver native) or the floor under the cube
# ---------------------------------------------------------------------------

WORLD_ORIGINS = ("cube_center", "floor")

# Yaw (about +Z, counter-clockwise seen from above) that brings a side face's
# outward normal onto world +Y. BACK is already +Y in the cube frame.
FORWARD_FACE_YAW_DEG = {"BACK": 0.0, "RIGHT": 90.0, "FRONT": 180.0, "LEFT": -90.0}


def _rot_z(deg: float) -> np.ndarray:
    """3x3 rotation about +Z by ``deg`` degrees, counter-clockwise seen from above (+Z)."""
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


@dataclass
class WorldFrame:
    """A named rigid re-basing of the solved extrinsics.

    The solver always works in the cube frame (origin cube centre, Z-up). A
    WorldFrame says where the *reported* origin/axes should be instead:

    - origin ``cube_center``: unchanged (legacy output).
    - origin ``floor``: origin on the ground directly under the cube centre,
      Z=0 is the ground plane. Valid only if the cube stood on its BOTTOM face
      on the (level) playing surface, optionally on a support of known height.
    - ``forward_face``: which side face pointed "down the venue" (bowling /
      shooting direction). Its outward normal becomes world +Y.
    - ``yaw_deg``: extra counter-clockwise (from above) correction if the cube
      was not squarely aligned to the venue line.

    Ground-truth caveat: the surface the cube rests on defines "level" — a
    tilted floor tilts world Z by the same angle.
    """

    origin: str = "cube_center"
    support_height_mm: float = 0.0
    forward_face: str = "BACK"
    yaw_deg: float = 0.0

    def __post_init__(self) -> None:
        if self.origin not in WORLD_ORIGINS:
            raise ValueError(f"origin must be one of {WORLD_ORIGINS}, got {self.origin!r}")
        if self.forward_face not in FORWARD_FACE_YAW_DEG:
            raise ValueError(
                f"forward_face must be a side face {tuple(FORWARD_FACE_YAW_DEG)}, "
                f"got {self.forward_face!r}"
            )
        if self.support_height_mm < 0:
            raise ValueError("support_height_mm cannot be negative")

    @property
    def is_identity(self) -> bool:
        """True when the world frame coincides with the cube frame (legacy output)."""
        return (
            self.origin == "cube_center"
            and self.forward_face == "BACK"
            and abs(self.yaw_deg) < 1e-12
        )

    def contact_height_mm(self, cfg: CalibrationConfig) -> float:
        """Height of the cube centre above the surface it rests on (BOTTOM down).

        The cube touches the surface with its BOTTOM pattern plane if the
        pattern stands proud of the substrate, else with the substrate face.
        """
        return cfg.cube_size_mm / 2.0 + max(cfg.pattern_offset_mm, 0.0) + self.support_height_mm

    def floor_z_in_cube_mm(self, cfg: CalibrationConfig) -> float:
        """Z of the ground plane expressed in the cube frame (always negative)."""
        return -self.contact_height_mm(cfg)

    def T_world_from_cube(self, cfg: CalibrationConfig) -> np.ndarray:
        """4x4 transform taking cube-frame points into this world frame.

        T_world_from_cube = Rz(yaw_of(forward_face) + yaw_deg) then, for the
        floor origin, a +Z shift of ``contact_height_mm`` so the ground plane
        becomes world Z = 0 and the cube centre sits at (0, 0, contact).
        """
        R = _rot_z(FORWARD_FACE_YAW_DEG[self.forward_face] + self.yaw_deg)
        T = np.eye(4)
        T[:3, :3] = R
        if self.origin == "floor":
            # rotation is about Z, so the cube centre stays on the world Z axis
            T[2, 3] = self.contact_height_mm(cfg)
        return T

    def describe(self, cfg: CalibrationConfig) -> dict:
        """Plain-data description of the frame for the ``world`` block of calibration.yaml."""
        contact = self.contact_height_mm(cfg)
        d: dict = {
            "origin": self.origin,
            "axes": "right-handed, +Z up, +Y = outward normal of forward_face, +X = to its right",
            "forward_face": self.forward_face,
            "yaw_deg": float(self.yaw_deg),
            "support_height_mm": float(self.support_height_mm),
            "cube_center_world_mm": [0.0, 0.0, contact if self.origin == "floor" else 0.0],
        }
        if self.origin == "floor":
            d["ground_plane"] = {
                "frame": "world",
                "z_mm": 0.0,
                "normal": [0.0, 0.0, 1.0],
                "assumes": "cube stood on its BOTTOM face on a level surface",
            }
        else:
            d["ground_plane"] = {
                "frame": "cube",
                "z_mm": -contact,
                "normal": [0.0, 0.0, 1.0],
                "assumes": "ONLY valid if the cube stood on its BOTTOM face on the floor",
            }
        return d


def T_camera_from_world(cam: "CameraExtrinsics", T_world_from_cube: np.ndarray) -> np.ndarray:
    """T_camera_from_world = T_camera_from_cube @ T_cube_from_world (re-based extrinsic)."""
    return cam.T_camera_from_cube @ invert_T(T_world_from_cube)


def camera_centre_world_mm(cam: "CameraExtrinsics", T_world_from_cube: np.ndarray) -> np.ndarray:
    """Camera optical centre expressed in the world frame, mm (Z = height above floor)."""
    return (T_world_from_cube @ np.append(cam.camera_centre_cube_mm, 1.0))[:3]


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_calibration_yaml(
    cameras: list[CameraExtrinsics],
    path: str | Path,
    world: WorldFrame | None = None,
    cfg: CalibrationConfig | None = None,
) -> Path:
    """Human-readable calibration output (doc §26 format).

    Always emits the solver-native cube-frame poses. When ``world`` is given
    (needs ``cfg`` for the cube dimensions) it additionally emits every pose in
    that world frame plus the ground-plane description. Pairwise camera-to-
    camera transforms do not depend on the world choice.
    """
    if world is not None and cfg is None:
        raise ValueError("cfg is required to resolve a WorldFrame")
    payload: dict = {"world_frame": "charuco_cube_center", "units": "mm", "cameras": {}}
    T_w_c = None
    if world is not None:
        T_w_c = world.T_world_from_cube(cfg)
        payload["world_frame"] = (
            "floor_under_cube_center" if world.origin == "floor" else "charuco_cube_center"
        )
        payload["world"] = world.describe(cfg)
        payload["world"]["T_world_from_cube"] = [[float(v) for v in row] for row in T_w_c]
    for cam in cameras:
        entry: dict = {
            "T_camera_from_cube": [[float(v) for v in row] for row in cam.T_camera_from_cube],
            "T_cube_from_camera": [[float(v) for v in row] for row in cam.T_cube_from_camera],
            "camera_center_cube_mm": [float(v) for v in cam.camera_centre_cube_mm],
        }
        if T_w_c is not None:
            T_c_w = T_camera_from_world(cam, T_w_c)
            entry["T_camera_from_world"] = [[float(v) for v in row] for row in T_c_w]
            entry["T_world_from_camera"] = [[float(v) for v in row] for row in invert_T(T_c_w)]
            entry["camera_center_world_mm"] = [
                float(v) for v in camera_centre_world_mm(cam, T_w_c)
            ]
        entry.update({
            "images": {"used": cam.n_images_used, "rejected": cam.n_images_rejected},
            "pose_spread": {
                "rotation_max_deg": round(cam.pose_rotation_spread_deg, 4),
                "translation_max_mm": round(cam.pose_translation_spread_mm, 3),
            },
            "reprojection": {
                "rms_px": round(cam.rms_px, 4),
                "median_px": round(cam.median_px, 4),
                "max_px": round(cam.max_px, 4),
            },
        })
        payload["cameras"][cam.name] = entry
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def write_pairwise_filestorage(
    cameras: list[CameraExtrinsics], out_dir: str | Path, reference: str | None = None
) -> list[Path]:
    """OpenCV FileStorage R/T pairs against a reference camera.

    Matches the format consumed by stress_3d/3D/pose_pipeline_1.py
    (out_extrinsics_c1c2.yml style): R, T mapping reference-camera coordinates
    into the other camera's coordinates. T is emitted in mm (cube units).
    """
    by_name = {c.name: c for c in cameras}
    ref_name = reference or cameras[0].name
    ref = by_name[ref_name]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for cam in cameras:
        if cam.name == ref_name:
            continue
        T = T_camB_from_camA(ref, cam)
        path = out_dir / f"extrinsics_{ref_name}_to_{cam.name}.yml"
        fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
        fs.write("pair", f"{ref_name}-{cam.name}")
        fs.write("R", T[:3, :3])
        fs.write("T", T[:3, 3].reshape(3, 1))
        fs.write("units", "mm")
        fs.release()
        paths.append(path)
    return paths
