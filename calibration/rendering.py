"""Render synthetic camera views of the cube from the actual face artwork.

This is the Level-2 simulator: the very PNGs that will be printed are warped
onto a virtual camera's image plane (exact pinhole homography per planar face),
so the REAL detection pipeline (ArUco + ChArUco interpolation + face ID) can be
exercised end-to-end before anything is printed. A convex cube's front-facing
faces never overlap in projection, so faces can be composited independently.

Distortion is deliberately zero here — the sim validates geometry, IDs and
detection limits; real lens distortion is handled by per-unit intrinsics.
"""

from __future__ import annotations

import cv2
import numpy as np

from .cube_geometry import CubeModel, transform_points
from .pose import invert_T, rvec_tvec_from_T
from .simulate import visible_faces


def face_print_image(model: CubeModel, face: str, px_per_mm: float = 2.0) -> np.ndarray:
    """Full-face artwork (board + white margins), grayscale, print-oriented.

    Pixel (u, v) corresponds to board-frame millimetres
    (u / px_per_mm - margin, v / px_per_mm - margin).
    """
    cfg = model.cfg
    board_px = int(round(cfg.board_width_mm * px_per_mm))
    board_py = int(round(cfg.board_height_mm * px_per_mm))
    margin_px = int(round(cfg.margin_mm * px_per_mm))
    board_img = model.face_boards[face].board.generateImage((board_px, board_py), marginSize=0)
    return cv2.copyMakeBorder(
        board_img, margin_px, margin_px, margin_px, margin_px,
        cv2.BORDER_CONSTANT, value=255,
    )


def render_view(
    model: CubeModel,
    T_camera_from_cube: np.ndarray,
    camera_matrix: np.ndarray,
    image_size: tuple[int, int],
    px_per_mm: float = 2.0,
    background: int = 110,
    blur_sigma_px: float = 0.6,
    noise_sigma: float = 2.0,
    rng: np.random.Generator | None = None,
    face_images: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Render one grayscale camera view of the cube (uint8, width x height)."""
    cfg = model.cfg
    width, height = image_size
    canvas = np.full((height, width), background, dtype=np.uint8)

    camera_pos = invert_T(T_camera_from_cube)[:3, 3]
    rvec, tvec = rvec_tvec_from_T(T_camera_from_cube)

    for face in visible_faces(model, camera_pos):
        art = (face_images or {}).get(face)
        if art is None:
            art = face_print_image(model, face, px_per_mm)
        h_px, w_px = art.shape[:2]

        # Face artwork corners in board-frame mm (pixel-centre convention).
        m = cfg.margin_mm
        src_px = np.array(
            [[-0.5, -0.5], [w_px - 0.5, -0.5], [w_px - 0.5, h_px - 0.5], [-0.5, h_px - 0.5]],
            dtype=np.float64,
        )
        corners_board_mm = np.array(
            [
                [-m, -m, 0.0],
                [cfg.board_width_mm + m, -m, 0.0],
                [cfg.board_width_mm + m, cfg.board_height_mm + m, 0.0],
                [-m, cfg.board_height_mm + m, 0.0],
            ]
        )
        corners_cube = transform_points(model.T_cube_from_face[face], corners_board_mm)
        dst_px, _ = cv2.projectPoints(
            corners_cube.reshape(-1, 1, 3), rvec, tvec, camera_matrix, np.zeros(5)
        )
        H = cv2.getPerspectiveTransform(
            src_px.astype(np.float32), dst_px.reshape(-1, 2).astype(np.float32)
        )
        warped = cv2.warpPerspective(
            art, H, (width, height), flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
        mask = cv2.warpPerspective(
            np.full_like(art, 255), H, (width, height), flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
        canvas[mask > 127] = warped[mask > 127]

    if blur_sigma_px > 0:
        canvas = cv2.GaussianBlur(canvas, (0, 0), blur_sigma_px)
    if noise_sigma > 0:
        rng = rng or np.random.default_rng(0)
        noisy = canvas.astype(np.float64) + rng.normal(0.0, noise_sigma, canvas.shape)
        canvas = np.clip(noisy, 0, 255).astype(np.uint8)
    return canvas
