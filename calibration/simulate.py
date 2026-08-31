"""Synthetic validation of the cube system — everything testable before printing.

Level 1 (this module's core): pure math. Project known cube points through
known camera poses with cv2.projectPoints, optionally add pixel noise, run the
PnP pipeline, and compare recovered poses/extrinsics against ground truth.
Proves the coordinate conventions and multi-camera chaining are correct.

Level 2 (rendering.py): the same scene rendered as actual images from the
generated face prints, pushed through the real detection pipeline.

Also here: ``detection_distance_sweep`` (how far a camera spec can see the
cube; fixed the square size before printing) and Level 3,
``construction_tolerance_study`` (Monte-Carlo of an imperfectly built cube
against the ideal model, giving a build-error budget).

All camera poses in this module are GROUND TRUTH built with
``T_cube_from_camera_lookat`` and inverted to T_camera_from_cube before use;
errors are reported as rotation (deg) and camera-centre distance (mm).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import CameraSpec
from .cube_geometry import CubeModel
from .multicam import CameraExtrinsics, T_camB_from_camA, combine_camera_estimates
from .pose import (
    PoseEstimate,
    estimate_camera_from_cube,
    invert_T,
    rotation_angle_deg,
    rvec_tvec_from_T,
)

# A face is usable when viewed within this angle of its normal (beyond ~70 deg
# the pattern is too foreshortened to detect reliably anyway).
MAX_VIEW_ANGLE_DEG = 70.0


def camera_matrix_for(spec: CameraSpec) -> np.ndarray:
    """Ideal pinhole K for a CameraSpec: principal point at the exact image centre.

    (w - 1) / 2 uses the pixel-centre convention, matching cv2.projectPoints.
    """
    return np.array(
        [
            [spec.fx_px, 0.0, (spec.width - 1) / 2.0],
            [0.0, spec.fy_px, (spec.height - 1) / 2.0],
            [0.0, 0.0, 1.0],
        ]
    )


def T_cube_from_camera_lookat(
    camera_pos_cube_mm: np.ndarray,
    target_cube_mm: np.ndarray | None = None,
    up_cube: np.ndarray = np.array([0.0, 0.0, 1.0]),
) -> np.ndarray:
    """Pose a camera at a cube-frame position looking at a target point.

    Follows the OpenCV camera convention: +Z along the optical axis (towards
    the target), +X image-right, +Y image-down.
    """
    pos = np.asarray(camera_pos_cube_mm, dtype=np.float64)
    target = np.zeros(3) if target_cube_mm is None else np.asarray(target_cube_mm, dtype=np.float64)
    z = target - pos
    z = z / np.linalg.norm(z)
    x = np.cross(z, up_cube)
    # Camera +X (image-right) is perpendicular to both the view direction and
    # world up; +Y = z x x then points image-DOWN, as OpenCV expects.
    if np.linalg.norm(x) < 1e-9:  # looking straight up/down: pick any right
        x = np.array([1.0, 0.0, 0.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)

    T = np.eye(4)
    T[:3, :3] = np.column_stack([x, y, z])
    T[:3, 3] = pos
    # Columns are the camera axes in cube coordinates and the translation is
    # the camera position, i.e. this is T_cube_from_camera (not its inverse).
    return T


def visible_faces(model: CubeModel, camera_pos_cube_mm: np.ndarray) -> list[str]:
    """Faces geometrically visible from a camera position (self-occlusion only)."""
    faces = []
    half = model.cfg.cube_size_mm / 2.0 + model.cfg.pattern_offset_mm
    for face in model.cfg.face_order:
        normal = model.outward_normal(face)
        to_camera = np.asarray(camera_pos_cube_mm, dtype=np.float64) - normal * half
        # Measure the view angle from the face CENTRE, not the cube centre: a
        # camera can be well off-axis of the cube yet still see a face square on.
        distance = np.linalg.norm(to_camera)
        if distance < 1e-9:
            continue
        cos_angle = float(np.dot(normal, to_camera / distance))
        if cos_angle > np.cos(np.radians(MAX_VIEW_ANGLE_DEG)):
            faces.append(face)
    return faces


@dataclass
class SyntheticObservation:
    """Noise-free or noisy 2D<->3D pairs a perfect detector would return for one view.

    Same row-aligned layout as ``detection.CubeDetection``: cube-frame mm
    against pixel positions, plus the face of every row.
    """

    object_points_cube_mm: np.ndarray
    image_points_px: np.ndarray
    faces: tuple[str, ...]
    point_faces: tuple[str, ...]  # face of each row, for per-face stats


def synthesize_observation(
    model: CubeModel,
    T_camera_from_cube: np.ndarray,
    camera_matrix: np.ndarray,
    image_size: tuple[int, int],
    dist_coeffs: np.ndarray | None = None,
    noise_px: float = 0.0,
    rng: np.random.Generator | None = None,
    include_marker_corners: bool = False,
    faces: list[str] | None = None,
) -> SyntheticObservation:
    """Project the cube's known 3D points into a synthetic camera image.

    Only points on faces visible from the camera and landing inside the image
    are kept — the same information a real detector could at best recover.
    """
    camera_pos = invert_T(T_camera_from_cube)[:3, 3]
    use_faces = faces if faces is not None else visible_faces(model, camera_pos)

    object_points, point_faces = [], []
    for face in use_faces:
        pts = model.chessboard_corners_cube[face]
        object_points.append(pts)
        point_faces.extend([face] * len(pts))
        if include_marker_corners:
            for marker_id in model.cfg.marker_ids_for_face(face):
                corners = model.marker_corners_cube[marker_id]
                object_points.append(corners)
                point_faces.extend([face] * len(corners))
    obj = np.concatenate(object_points, axis=0)

    rvec = cv2.Rodrigues(np.ascontiguousarray(T_camera_from_cube[:3, :3]))[0]
    tvec = T_camera_from_cube[:3, 3].reshape(3, 1)
    projected, _ = cv2.projectPoints(
        obj.reshape(-1, 1, 3), rvec, tvec,
        camera_matrix, dist_coeffs if dist_coeffs is not None else np.zeros(5),
    )
    img = projected.reshape(-1, 2)

    if noise_px > 0:
        rng = rng or np.random.default_rng(0)
        img = img + rng.normal(0.0, noise_px, size=img.shape)
    # Note the noise is added BEFORE the in-image test below, so a point
    # jittered across the border is dropped exactly as a real edge point would be.

    width, height = image_size
    in_camera = obj @ T_camera_from_cube[:3, :3].T + T_camera_from_cube[:3, 3]
    keep = (
        (img[:, 0] >= 0) & (img[:, 0] < width)
        & (img[:, 1] >= 0) & (img[:, 1] < height)
        & (in_camera[:, 2] > 0)  # in front of the camera
    )
    point_faces = np.asarray(point_faces)
    return SyntheticObservation(
        object_points_cube_mm=obj[keep],
        image_points_px=img[keep],
        faces=tuple(dict.fromkeys(point_faces[keep].tolist())),
        point_faces=tuple(point_faces[keep].tolist()),
    )


@dataclass
class Level1CameraResult:
    """Recovered vs ground-truth pose for one simulated camera.

    ``rotation_error_deg`` is the geodesic angle between the recovered and true
    R_camera_from_cube; ``translation_error_mm`` is the distance between the
    recovered and true camera centres in the cube frame.
    """

    name: str
    ground_truth_T_camera_from_cube: np.ndarray
    extrinsics: CameraExtrinsics
    rotation_error_deg: float
    translation_error_mm: float


def run_level1(
    model: CubeModel,
    spec: CameraSpec,
    camera_positions_mm: dict[str, np.ndarray],
    n_images_per_camera: int = 5,
    noise_px: float = 0.3,
    seed: int = 7,
    include_marker_corners: bool = True,
) -> tuple[list[Level1CameraResult], dict[tuple[str, str], dict[str, float]]]:
    """Full Level-1 round trip for several cameras around the cube.

    Returns per-camera recovery errors and pairwise T_camB_from_camA errors
    against ground truth.
    """
    rng = np.random.default_rng(seed)
    K = camera_matrix_for(spec)
    image_size = (spec.width, spec.height)

    results: list[Level1CameraResult] = []
    for name, pos in camera_positions_mm.items():
        T_gt_cube_from_cam = T_cube_from_camera_lookat(np.asarray(pos, dtype=np.float64))
        T_gt_cam_from_cube = invert_T(T_gt_cube_from_cam)

        estimates: list[PoseEstimate] = []
        for _ in range(n_images_per_camera):
            obs = synthesize_observation(
                model, T_gt_cam_from_cube, K, image_size,
                noise_px=noise_px, rng=rng,
                include_marker_corners=include_marker_corners,
            )
            estimates.append(
                estimate_camera_from_cube(
                    obs.object_points_cube_mm, obs.image_points_px, K, None, faces=obs.faces
                )
            )
        extrinsics = combine_camera_estimates(name, estimates)
        results.append(
            Level1CameraResult(
                name=name,
                ground_truth_T_camera_from_cube=T_gt_cam_from_cube,
                extrinsics=extrinsics,
                rotation_error_deg=rotation_angle_deg(
                    extrinsics.T_camera_from_cube[:3, :3], T_gt_cam_from_cube[:3, :3]
                ),
                translation_error_mm=float(
                    np.linalg.norm(extrinsics.camera_centre_cube_mm - T_gt_cube_from_cam[:3, 3])
                ),
            )
        )

    pairwise: dict[tuple[str, str], dict[str, float]] = {}
    for a in results:
        for b in results:
            if a.name >= b.name:
                continue
            T_est = T_camB_from_camA(a.extrinsics, b.extrinsics)
            T_gt = b.ground_truth_T_camera_from_cube @ invert_T(a.ground_truth_T_camera_from_cube)
            pairwise[(a.name, b.name)] = {
                "rotation_error_deg": rotation_angle_deg(T_est[:3, :3], T_gt[:3, :3]),
                "translation_error_mm": float(np.linalg.norm(T_est[:3, 3] - T_gt[:3, 3])),
            }
    return results, pairwise


# ---------------------------------------------------------------------------
# Level 2: rendered images through the REAL detection pipeline
# ---------------------------------------------------------------------------

def run_level2(
    model: CubeModel,
    spec: CameraSpec,
    camera_positions_mm: dict[str, np.ndarray],
    n_images_per_camera: int = 3,
    seed: int = 7,
    blur_sigma_px: float = 0.6,
    noise_sigma: float = 2.0,
) -> tuple[list[Level1CameraResult], dict[tuple[str, str], dict[str, float]]]:
    """As run_level1, but views are rendered from the actual face artwork and
    pushed through CubeDetector — validating detection, face identification,
    corner interpolation and the ID database end-to-end."""
    from .detection import CubeDetector
    from .rendering import face_print_image, render_view

    rng = np.random.default_rng(seed)
    K = camera_matrix_for(spec)
    image_size = (spec.width, spec.height)
    detector = CubeDetector(model)
    face_images = {f: face_print_image(model, f) for f in model.cfg.face_order}

    results: list[Level1CameraResult] = []
    for name, pos in camera_positions_mm.items():
        T_gt_cube_from_cam = T_cube_from_camera_lookat(np.asarray(pos, dtype=np.float64))
        T_gt_cam_from_cube = invert_T(T_gt_cube_from_cam)

        estimates: list[PoseEstimate] = []
        for _ in range(n_images_per_camera):
            frame = render_view(
                model, T_gt_cam_from_cube, K, image_size,
                blur_sigma_px=blur_sigma_px, noise_sigma=noise_sigma, rng=rng,
                face_images=face_images,
            )
            detection = detector.detect(frame)
            if detection.n_correspondences < 4:
                continue
            estimates.append(
                estimate_camera_from_cube(
                    detection.object_points_cube_mm, detection.image_points_px,
                    K, None, faces=tuple(detection.visible_faces),
                )
            )
        extrinsics = combine_camera_estimates(name, estimates)
        results.append(
            Level1CameraResult(
                name=name,
                ground_truth_T_camera_from_cube=T_gt_cam_from_cube,
                extrinsics=extrinsics,
                rotation_error_deg=rotation_angle_deg(
                    extrinsics.T_camera_from_cube[:3, :3], T_gt_cam_from_cube[:3, :3]
                ),
                translation_error_mm=float(
                    np.linalg.norm(extrinsics.camera_centre_cube_mm - T_gt_cube_from_cam[:3, 3])
                ),
            )
        )

    pairwise: dict[tuple[str, str], dict[str, float]] = {}
    for a in results:
        for b in results:
            if a.name >= b.name:
                continue
            T_est = T_camB_from_camA(a.extrinsics, b.extrinsics)
            T_gt = b.ground_truth_T_camera_from_cube @ invert_T(a.ground_truth_T_camera_from_cube)
            pairwise[(a.name, b.name)] = {
                "rotation_error_deg": rotation_angle_deg(T_est[:3, :3], T_gt[:3, :3]),
                "translation_error_mm": float(np.linalg.norm(T_est[:3, 3] - T_gt[:3, 3])),
            }
    return results, pairwise


@dataclass
class SweepRow:
    """One distance step of ``detection_distance_sweep``.

    ``marker_px`` is the analytic fronto-parallel marker size
    fx * marker_length / distance (an upper bound; oblique faces project
    smaller). Errors are None when no unambiguous pose was recovered.
    """

    distance_mm: float
    marker_px: float          # projected marker side length
    n_faces_detected: int
    n_correspondences: int
    pose_ok: bool
    rotation_error_deg: float | None
    translation_error_mm: float | None


def detection_distance_sweep(
    model: CubeModel,
    spec: CameraSpec,
    distances_mm: list[float],
    oblique: bool = True,
    seed: int = 3,
) -> list[SweepRow]:
    """How far away does the cube stay detectable for a given camera spec?

    Renders a view per distance (oblique = from a front-right corner, the
    realistic two-face geometry) and reports detection + pose recovery. This is
    what freezes square size before printing, and what produces the outdoor
    (40 m) viability envelope.
    """
    from .detection import CubeDetector
    from .rendering import face_print_image, render_view

    rng = np.random.default_rng(seed)
    K = camera_matrix_for(spec)
    image_size = (spec.width, spec.height)
    detector = CubeDetector(model)
    face_images = {f: face_print_image(model, f) for f in model.cfg.face_order}

    rows: list[SweepRow] = []
    for distance in distances_mm:
        direction = np.array([0.55, -0.8, 0.16]) if oblique else np.array([0.0, -1.0, 0.1])
        # Oblique: front-right and slightly above, so FRONT and RIGHT are both
        # within the 70 deg view limit (two faces -> non-planar PnP). Non-oblique
        # looks almost straight at FRONT, the single-face worst case.
        pos = direction / np.linalg.norm(direction) * distance
        T_cube_from_cam = T_cube_from_camera_lookat(pos)
        T_cam_from_cube = invert_T(T_cube_from_cam)

        frame = render_view(model, T_cam_from_cube, K, image_size, rng=rng,
                            face_images=face_images)
        detection = detector.detect(frame)

        marker_px = spec.fx_px * model.cfg.marker_length_mm / distance
        rotation_error = translation_error = None
        pose_ok = False
        if detection.n_correspondences >= 4:
            try:
                est = estimate_camera_from_cube(
                    detection.object_points_cube_mm, detection.image_points_px, K, None,
                    faces=tuple(detection.visible_faces),
                )
                if not est.ambiguous:
                    pose_ok = True
                    rotation_error = rotation_angle_deg(
                        est.T_camera_from_cube[:3, :3], T_cam_from_cube[:3, :3]
                    )
                    translation_error = float(
                        np.linalg.norm(est.camera_centre_cube_mm - pos)
                    )
            except (RuntimeError, cv2.error):  # pragma: no cover - defensive
                pass
        rows.append(
            SweepRow(
                distance_mm=float(distance),
                marker_px=float(marker_px),
                n_faces_detected=len(detection.faces),
                n_correspondences=detection.n_correspondences,
                pose_ok=pose_ok,
                rotation_error_deg=rotation_error,
                translation_error_mm=translation_error,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Level 3: Monte-Carlo construction-tolerance study
# ---------------------------------------------------------------------------

def _perturbed_physical_corners(
    model: CubeModel,
    rng: np.random.Generator,
    face_offset_sigma_mm: float,
    face_tilt_sigma_deg: float,
    face_spin_sigma_deg: float,
    print_scale_sigma: float,
) -> dict[str, np.ndarray]:
    """Chessboard corners of an imperfectly-built cube, in cube coordinates.

    Per face: in-plane mounting offset, tilt out of plane, spin about the
    normal (all about the board centre), and isotropic print-scale error.
    """
    physical: dict[str, np.ndarray] = {}
    for face, fb in model.face_boards.items():
        pts = fb.chessboard_corners_board_mm.copy()
        centre = np.array(
            [model.cfg.board_width_mm / 2.0, model.cfg.board_height_mm / 2.0, 0.0]
        )
        scale = 1.0 + rng.normal(0.0, print_scale_sigma)
        pts = centre + (pts - centre) * scale

        tilt_x, tilt_y = np.radians(rng.normal(0.0, face_tilt_sigma_deg, 2))
        spin = np.radians(rng.normal(0.0, face_spin_sigma_deg))
        R_perturb = (
            cv2.Rodrigues(np.array([0.0, 0.0, spin]))[0]
            @ cv2.Rodrigues(np.array([tilt_x, 0.0, 0.0]))[0]
            @ cv2.Rodrigues(np.array([0.0, tilt_y, 0.0]))[0]
        )
        offset = np.array([*rng.normal(0.0, face_offset_sigma_mm, 2), 0.0])
        pts = (pts - centre) @ R_perturb.T + centre + offset

        physical[face] = transform_points_via(model, face, pts)
    return physical


def transform_points_via(model: CubeModel, face: str, pts_board: np.ndarray) -> np.ndarray:
    """Board-frame points of ``face`` into the cube frame via the IDEAL T_cube_from_face."""
    T = model.T_cube_from_face[face]
    return pts_board @ T[:3, :3].T + T[:3, 3]


@dataclass
class ToleranceSummary:
    """Aggregate of ``construction_tolerance_study`` over all trials and cameras.

    Single-camera rotation/translation errors are against ground truth in the
    cube frame; pairwise errors are the translation error of T_camB_from_camA.
    ``rms_px_mean`` is what a real capture of such a cube would show, which is
    deliberately small: build error mostly hides from reprojection RMS.
    """

    n_trials: int
    rotation_error_deg_mean: float
    rotation_error_deg_max: float
    translation_error_mm_mean: float
    translation_error_mm_max: float
    pairwise_translation_error_mm_mean: float
    pairwise_translation_error_mm_max: float
    rms_px_mean: float  # the observable symptom on a real, imperfect cube


def construction_tolerance_study(
    model: CubeModel,
    spec: CameraSpec,
    camera_positions_mm: dict[str, np.ndarray],
    n_trials: int = 25,
    face_offset_sigma_mm: float = 1.0,
    face_tilt_sigma_deg: float = 0.5,
    face_spin_sigma_deg: float = 0.2,
    print_scale_sigma: float = 0.002,
    noise_px: float = 0.3,
    seed: int = 13,
) -> ToleranceSummary:
    """How much extrinsic error does an imperfect BUILD cause?

    The physical cube is perturbed (offsets/tilts/spins/print scale) while the
    software keeps assuming the ideal model — exactly the real-world situation.
    Feed it your expected build tolerances to get an error budget, and compare
    the returned rms_px against real captures to detect a badly-built cube.
    """
    rng = np.random.default_rng(seed)
    K = camera_matrix_for(spec)
    width, height = spec.width, spec.height

    rot_errs, trans_errs, pair_errs, rms_list = [], [], [], []
    for _ in range(n_trials):
        physical = _perturbed_physical_corners(
            model, rng, face_offset_sigma_mm, face_tilt_sigma_deg,
            face_spin_sigma_deg, print_scale_sigma,
        )
        recovered: dict[str, CameraExtrinsics] = {}
        ground_truth: dict[str, np.ndarray] = {}
        for name, pos in camera_positions_mm.items():
            pos = np.asarray(pos, dtype=np.float64)
            T_gt_cube_from_cam = T_cube_from_camera_lookat(pos)
            T_gt_cam_from_cube = invert_T(T_gt_cube_from_cam)
            ground_truth[name] = T_gt_cam_from_cube

            ideal_pts, physical_pts = [], []
            for face in visible_faces(model, pos):
                ideal_pts.append(model.chessboard_corners_cube[face])
                physical_pts.append(physical[face])
            ideal = np.concatenate(ideal_pts)
            phys = np.concatenate(physical_pts)

            rvec, tvec = rvec_tvec_from_T(T_gt_cam_from_cube)
            projected, _ = cv2.projectPoints(
                phys.reshape(-1, 1, 3), rvec, tvec, K, np.zeros(5)
            )
            img = projected.reshape(-1, 2) + rng.normal(0.0, noise_px, (len(phys), 2))
            keep = (
                (img[:, 0] >= 0) & (img[:, 0] < width)
                & (img[:, 1] >= 0) & (img[:, 1] < height)
            )
            # Fewer than 6 in-image points is too thin for a meaningful pose.
            if keep.sum() < 6:
                continue
            est = estimate_camera_from_cube(ideal[keep], img[keep], K, None)
            # Key step: pixels come from the PERTURBED cube, but PnP is given
            # the IDEAL 3D points, exactly as the real solver would be.
            if est.ambiguous:
                # An IPPE flip is a capture problem, not a construction one —
                # it would swamp the build-error signal this study isolates.
                continue
            recovered[name] = combine_camera_estimates(name, [est])
            rms_list.append(est.rms_px)
            rot_errs.append(
                rotation_angle_deg(est.T_camera_from_cube[:3, :3], T_gt_cam_from_cube[:3, :3])
            )
            trans_errs.append(float(np.linalg.norm(est.camera_centre_cube_mm - pos)))

        names = list(recovered)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                T_est = T_camB_from_camA(recovered[a], recovered[b])
                T_gt = ground_truth[b] @ invert_T(ground_truth[a])
                pair_errs.append(float(np.linalg.norm(T_est[:3, 3] - T_gt[:3, 3])))

    return ToleranceSummary(
        n_trials=n_trials,
        rotation_error_deg_mean=float(np.mean(rot_errs)),
        rotation_error_deg_max=float(np.max(rot_errs)),
        translation_error_mm_mean=float(np.mean(trans_errs)),
        translation_error_mm_max=float(np.max(trans_errs)),
        pairwise_translation_error_mm_mean=float(np.mean(pair_errs)),
        pairwise_translation_error_mm_max=float(np.max(pair_errs)),
        rms_px_mean=float(np.mean(rms_list)),
    )
