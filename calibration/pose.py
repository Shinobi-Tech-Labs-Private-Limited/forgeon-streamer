"""Camera pose estimation from cube observations, with explicit direction naming.

OpenCV's solvePnP returns (rvec, tvec) such that

    X_camera = R_camera_from_cube @ X_cube + t_camera_from_cube

i.e. it maps points FROM the cube/world frame INTO the camera frame. We store
that as the 4x4 T_camera_from_cube. The camera's own position in cube
coordinates is the translation of the inverse:

    T_cube_from_camera = inv(T_camera_from_cube)
    camera_centre_cube = T_cube_from_camera[:3, 3]  ( = -R^T @ t )

Planar ambiguity: a single cube face is a planar target, for which PnP has a
two-fold "flip" ambiguity. We use SOLVEPNP_IPPE for the coplanar case, compare
both returned solutions' reprojection errors, and mark the estimate ambiguous
when they are too close to distinguish. Observations spanning >= 2 faces are
non-coplanar and have a unique solution.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# Best/second reprojection-error ratio above which an IPPE result is ambiguous.
IPPE_AMBIGUITY_RATIO = 0.6


def T_from_rvec_tvec(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64))[0]
    T[:3, 3] = np.asarray(tvec, dtype=np.float64).ravel()
    return T


def rvec_tvec_from_T(T: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rvec = cv2.Rodrigues(np.ascontiguousarray(T[:3, :3]))[0]
    return rvec, T[:3, 3].reshape(3, 1).copy()


def invert_T(T: np.ndarray) -> np.ndarray:
    R, t = T[:3, :3], T[:3, 3]
    Ti = np.eye(4)
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ t
    return Ti


def rotation_angle_deg(R_a: np.ndarray, R_b: np.ndarray) -> float:
    """Geodesic angle between two rotations, degrees."""
    cos = (np.trace(R_a.T @ R_b) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


@dataclass
class PoseEstimate:
    T_camera_from_cube: np.ndarray
    n_points: int
    faces: tuple[str, ...]
    rms_px: float
    max_px: float
    per_point_px: np.ndarray = field(repr=False)
    coplanar: bool = False
    ambiguous: bool = False

    @property
    def T_cube_from_camera(self) -> np.ndarray:
        return invert_T(self.T_camera_from_cube)

    @property
    def camera_centre_cube_mm(self) -> np.ndarray:
        return self.T_cube_from_camera[:3, 3]


def reprojection_errors(
    object_points_cube_mm: np.ndarray,
    image_points_px: np.ndarray,
    T_camera_from_cube: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray | None,
) -> np.ndarray:
    """Per-point pixel distance between detections and reprojections."""
    rvec, tvec = rvec_tvec_from_T(T_camera_from_cube)
    projected, _ = cv2.projectPoints(
        np.asarray(object_points_cube_mm, dtype=np.float64).reshape(-1, 1, 3),
        rvec,
        tvec,
        camera_matrix,
        dist_coeffs if dist_coeffs is not None else np.zeros(5),
    )
    diff = projected.reshape(-1, 2) - np.asarray(image_points_px, dtype=np.float64).reshape(-1, 2)
    return np.linalg.norm(diff, axis=1)


def _points_are_coplanar(object_points: np.ndarray, tol_mm: float = 1.0) -> bool:
    pts = object_points - object_points.mean(axis=0)
    singular_values = np.linalg.svd(pts, compute_uv=False)
    return bool(singular_values[-1] < tol_mm)


def estimate_camera_from_cube(
    object_points_cube_mm: np.ndarray,
    image_points_px: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray | None,
    faces: tuple[str, ...] = (),
) -> PoseEstimate:
    """Estimate T_camera_from_cube from 2D<->3D correspondences.

    Non-coplanar point sets (>= 2 faces) use SQPNP + LM refinement. Coplanar
    sets use IPPE, disambiguated by reprojection error.
    """
    obj = np.asarray(object_points_cube_mm, dtype=np.float64).reshape(-1, 3)
    img = np.asarray(image_points_px, dtype=np.float64).reshape(-1, 2)
    if obj.shape[0] < 4:
        raise ValueError(f"PnP needs >= 4 correspondences, got {obj.shape[0]}")
    dist = dist_coeffs if dist_coeffs is not None else np.zeros(5)

    coplanar = _points_are_coplanar(obj)
    ambiguous = False
    if coplanar:
        n_solutions, rvecs, tvecs, errors = cv2.solvePnPGeneric(
            obj.reshape(-1, 1, 3), img.reshape(-1, 1, 2), camera_matrix, dist,
            flags=cv2.SOLVEPNP_IPPE,
        )
        if n_solutions == 0:
            raise RuntimeError("IPPE PnP failed")
        errs = np.asarray(errors, dtype=np.float64).ravel()[:n_solutions]
        best = int(np.argmin(errs))
        rvec, tvec = rvecs[best], tvecs[best]
        if n_solutions > 1:
            others = np.delete(errs, best)
            ambiguous = bool(errs[best] / max(float(others.min()), 1e-12) > IPPE_AMBIGUITY_RATIO)
    else:
        ok, rvec, tvec = cv2.solvePnP(
            obj.reshape(-1, 1, 3), img.reshape(-1, 1, 2), camera_matrix, dist,
            flags=cv2.SOLVEPNP_SQPNP,
        )
        if not ok:
            raise RuntimeError("PnP failed")

    rvec, tvec = cv2.solvePnPRefineLM(
        obj.reshape(-1, 1, 3), img.reshape(-1, 1, 2), camera_matrix, dist, rvec, tvec
    )
    T = T_from_rvec_tvec(rvec, tvec)
    per_point = reprojection_errors(obj, img, T, camera_matrix, dist)
    return PoseEstimate(
        T_camera_from_cube=T,
        n_points=obj.shape[0],
        faces=tuple(faces),
        rms_px=float(np.sqrt(np.mean(per_point**2))),
        max_px=float(per_point.max()),
        per_point_px=per_point,
        coplanar=coplanar,
        ambiguous=ambiguous,
    )


def average_poses(T_list: list[np.ndarray]) -> np.ndarray:
    """Average rigid transforms: chordal quaternion mean (Markley) + mean translation.

    Naive element-wise averaging of rotation matrices is NOT a rotation; this is
    the standard eigenvector method on sign-aligned quaternions.
    """
    if not T_list:
        raise ValueError("no poses to average")
    if len(T_list) == 1:
        return T_list[0].copy()

    quats = []
    for T in T_list:
        R = T[:3, :3]
        # Rotation matrix -> quaternion (w, x, y, z).
        w = np.sqrt(max(0.0, 1.0 + np.trace(R))) / 2.0
        if w > 1e-8:
            q = np.array([
                w,
                (R[2, 1] - R[1, 2]) / (4 * w),
                (R[0, 2] - R[2, 0]) / (4 * w),
                (R[1, 0] - R[0, 1]) / (4 * w),
            ])
        else:  # w ~ 0: use the largest diagonal element branch
            i = int(np.argmax(np.diag(R)))
            j, k = (i + 1) % 3, (i + 2) % 3
            s = np.sqrt(max(1e-12, 1.0 + R[i, i] - R[j, j] - R[k, k])) * 2.0
            q = np.zeros(4)
            q[0] = (R[k, j] - R[j, k]) / s
            q[1 + i] = s / 4.0
            q[1 + j] = (R[j, i] + R[i, j]) / s
            q[1 + k] = (R[k, i] + R[i, k]) / s
        quats.append(q / np.linalg.norm(q))

    reference = quats[0]
    A = np.zeros((4, 4))
    for q in quats:
        if np.dot(q, reference) < 0:
            q = -q
        A += np.outer(q, q)
    eigenvalues, eigenvectors = np.linalg.eigh(A)
    q_mean = eigenvectors[:, np.argmax(eigenvalues)]
    w, x, y, z = q_mean
    R_mean = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])

    T_mean = np.eye(4)
    T_mean[:3, :3] = R_mean
    T_mean[:3, 3] = np.mean([T[:3, 3] for T in T_list], axis=0)
    return T_mean


def pose_spread(T_list: list[np.ndarray], T_ref: np.ndarray) -> dict[str, float]:
    """Rotation (deg) and translation (mm) spread of poses around a reference."""
    rot = [rotation_angle_deg(T[:3, :3], T_ref[:3, :3]) for T in T_list]
    trans = [float(np.linalg.norm(T[:3, 3] - T_ref[:3, 3])) for T in T_list]
    return {
        "rotation_max_deg": max(rot) if rot else 0.0,
        "translation_max_mm": max(trans) if trans else 0.0,
    }
