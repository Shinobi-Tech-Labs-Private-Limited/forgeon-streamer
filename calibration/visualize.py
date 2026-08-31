"""3D visualization: cube wireframe, pattern corners, normals, and cameras.

Lets you eyeball whether calibrated extrinsics make physical sense: camera
positions, optical axes and frusta are drawn in the cube/world frame.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from .cube_geometry import CubeModel

FACE_PLOT_COLOURS = {
    "FRONT": "tab:orange",
    "RIGHT": "tab:green",
    "BACK": "tab:blue",
    "LEFT": "tab:purple",
    "TOP": "tab:red",
    "BOTTOM": "tab:olive",
}


def load_calibration_yaml(path: str | Path) -> dict[str, np.ndarray]:
    """Read the doc-§26 calibration.yaml -> {name: T_cube_from_camera}."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {
        name: np.array(entry["T_cube_from_camera"], dtype=np.float64)
        for name, entry in data["cameras"].items()
    }


def _draw_cube(ax, model: CubeModel) -> None:
    """Draw the cube wireframe, every face's corners, outward normals and the cube axes."""
    half = model.cfg.cube_size_mm / 2.0
    corners = np.array(
        [[sx, sy, sz] for sx in (-half, half) for sy in (-half, half) for sz in (-half, half)]
    )
    edges = [
        (0, 1), (0, 2), (0, 4), (3, 1), (3, 2), (3, 7),
        (5, 1), (5, 4), (5, 7), (6, 2), (6, 4), (6, 7),
    ]
    # corners[] enumerates sign combinations as 3-bit indices (x, y, z); an
    # edge joins two indices that differ in exactly one bit.
    for i, j in edges:
        ax.plot(*zip(corners[i], corners[j]), color="0.55", lw=1.0)

    for face in model.cfg.face_order:
        colour = FACE_PLOT_COLOURS[face]
        pts = model.chessboard_corners_cube[face]
        ax.scatter(pts[:, 0], pts[:, 1], pts[:, 2], s=10, color=colour, depthshade=False)
        normal = model.outward_normal(face)
        centre = normal * half
        tip = normal * (half + 180.0)
        ax.quiver(*centre, *(tip - centre), color=colour, arrow_length_ratio=0.25)
        ax.text(*(normal * (half + 230.0)), face, color=colour, fontsize=10, ha="center")

    axis_len = half * 1.6
    for direction, colour, label in (
        ((1, 0, 0), "r", "+X"), ((0, 1, 0), "g", "+Y"), ((0, 0, 1), "b", "+Z"),
    ):
        d = np.array(direction, dtype=float) * axis_len
        ax.quiver(0, 0, 0, *d, color=colour, lw=2, arrow_length_ratio=0.06)
        ax.text(*(d * 1.08), label, color=colour, fontsize=11, weight="bold")


def _draw_camera(ax, name: str, T_cube_from_camera: np.ndarray, scale_mm: float) -> None:
    """Draw one camera (centre, RGB = camera x/y/z axes, frustum) from T_cube_from_camera."""
    centre = T_cube_from_camera[:3, 3]
    R = T_cube_from_camera[:3, :3]  # columns = camera x, y, z axes in cube frame
    ax.scatter(*centre, s=45, color="black", marker="o", depthshade=False)
    for axis_idx, colour in ((0, "r"), (1, "g"), (2, "b")):
        d = R[:, axis_idx] * scale_mm * (2.0 if axis_idx == 2 else 1.0)
        # Optical axis (+z, blue) drawn twice as long so viewing direction reads
        # at a glance.
        ax.quiver(*centre, *d, color=colour, arrow_length_ratio=0.12)

    # Small frustum along +z (optical axis), ~70 deg HFOV proportions.
    depth = scale_mm * 1.6
    half_w, half_h = 0.7 * depth, 0.45 * depth
    corners_cam = np.array(
        [[-half_w, -half_h, depth], [half_w, -half_h, depth],
         [half_w, half_h, depth], [-half_w, half_h, depth]]
    )
    corners_cube = corners_cam @ R.T + centre
    # Camera-frame frustum corners into the cube frame (same as transform_points).
    for corner in corners_cube:
        ax.plot(*zip(centre, corner), color="0.3", lw=0.8)
    loop = np.vstack([corners_cube, corners_cube[:1]])
    ax.plot(loop[:, 0], loop[:, 1], loop[:, 2], color="0.3", lw=0.8)
    ax.text(*(centre + R[:, 2] * scale_mm * 0.4), name, fontsize=10, weight="bold")


def visualize(
    model: CubeModel,
    cameras: dict[str, np.ndarray] | None = None,
    save_path: str | Path | None = None,
    show: bool = False,
    title: str = "ChArUco calibration cube",
):
    """Render the cube (and optionally calibrated cameras, as
    {name: T_cube_from_camera}) in 3D. Returns the matplotlib figure."""
    import matplotlib

    if not show:
        matplotlib.use("Agg")
        # Headless backend must be selected before pyplot is first imported.
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(11, 9))
    ax = fig.add_subplot(projection="3d")
    _draw_cube(ax, model)

    reach = model.cfg.cube_size_mm
    if cameras:
        for name, T in cameras.items():
            _draw_camera(ax, name, np.asarray(T, dtype=np.float64), scale_mm=reach * 0.25)
            reach = max(reach, float(np.linalg.norm(T[:3, 3])))

    lim = reach * 1.1
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_zlim(-lim, lim)
    ax.set_box_aspect((1, 1, 1))
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_zlabel("Z (mm)")
    ax.set_title(title)

    if save_path:
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(save_path, dpi=140, bbox_inches="tight")
    if show:
        plt.show()
    else:
        plt.close(fig)
    return fig
