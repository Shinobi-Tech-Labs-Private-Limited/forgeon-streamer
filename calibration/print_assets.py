"""Print-ready face artwork, PDFs, and the cube assembly net.

Physical-scale guarantees:
- PNGs carry true DPI metadata so print software reproduces exact size.
- PDFs are authored at exact physical page size (face + a caption strip), with
  the face artwork occupying exactly cube_size x cube_size mm and a scale bar
  OUTSIDE the detection region for ruler verification.
- Nothing (labels, bars, text) is ever drawn inside the face square itself —
  the quiet margins around the board stay pure white.

Assembly convention (matches the DESIGN cube_geometry.FACE_AXES; as-built
deviations go in config.yaml mounting.face_rotation_deg): print every face
upright, assemble as a standard cross net —

              TOP
               |
  LEFT - FRONT - RIGHT - BACK
               |
             BOTTOM

folded with the print on the OUTSIDE. No face needs rotating.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .cube_geometry import CubeModel
from .rendering import face_print_image

MM_PER_INCH = 25.4


def write_face_pngs(model: CubeModel, out_dir: str | Path, dpi: int | None = None) -> list[Path]:
    """Exact-size face PNGs with embedded DPI so 100%-scale prints are true size."""
    cfg = model.cfg
    dpi = dpi or cfg.print_dpi
    px_per_mm = dpi / MM_PER_INCH
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for face in cfg.face_order:
        art = face_print_image(model, face, px_per_mm=px_per_mm)
        path = out_dir / f"{face.lower()}.png"
        # The DPI tag is what lets print software reproduce cube_size_mm exactly.
        Image.fromarray(art).save(path, dpi=(dpi, dpi))
        paths.append(path)
    return paths


def write_face_pdfs(model: CubeModel, out_dir: str | Path, dpi: int | None = None) -> list[Path]:
    """One PDF per face at exact physical size: the face square plus a caption
    strip BELOW it carrying the label, print specs, an UP arrow and a scale bar."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cfg = model.cfg
    dpi = dpi or cfg.print_dpi
    px_per_mm = dpi / MM_PER_INCH
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    strip_mm = 40.0  # caption strip height under the face
    # Page = one face square plus the strip, in inches, at the print DPI, so the
    # figure's pixel grid maps 1:1 onto the physical millimetre grid.
    page_w_in = cfg.cube_size_mm / MM_PER_INCH
    page_h_in = (cfg.cube_size_mm + strip_mm) / MM_PER_INCH

    paths = []
    for face in cfg.face_order:
        art = face_print_image(model, face, px_per_mm=px_per_mm)
        fig = plt.figure(figsize=(page_w_in, page_h_in), dpi=dpi)

        # Face artwork: exactly cube_size x cube_size at the top of the page.
        face_frac = cfg.cube_size_mm / (cfg.cube_size_mm + strip_mm)
        ax_face = fig.add_axes((0.0, 1.0 - face_frac, 1.0, face_frac))
        ax_face.imshow(art, cmap="gray", vmin=0, vmax=255, interpolation="none")
        # interpolation="none": any resampling would soften marker edges and
        # could shift the checker boundaries by a fraction of a pixel.
        ax_face.axis("off")

        # Caption strip (outside the detection region).
        ax_cap = fig.add_axes((0.0, 0.0, 1.0, 1.0 - face_frac))
        ax_cap.set_xlim(0, cfg.cube_size_mm)
        ax_cap.set_ylim(0, strip_mm)
        ax_cap.axis("off")
        ax_cap.annotate(
            "", xy=(20, strip_mm - 4), xytext=(20, 6),
            arrowprops={"arrowstyle": "-|>", "lw": 2},
        )
        ax_cap.text(28, strip_mm / 2, "UP", fontsize=14, va="center", weight="bold")
        ax_cap.text(
            70, strip_mm - 10,
            f"{face} — cube {cfg.cube_size_mm:.0f} mm — board "
            f"{cfg.board_width_mm:.0f}×{cfg.board_height_mm:.0f} mm — square "
            f"{cfg.square_length_mm:.1f} mm — marker {cfg.marker_length_mm:.1f} mm — "
            f"{cfg.dictionary_name}",
            fontsize=11, va="center",
        )
        ax_cap.text(
            70, strip_mm - 20,
            "PRINT AT 100% SCALE (no 'fit to page'). Verify the bar below "
            "measures exactly 400 mm.",
            fontsize=11, va="center", weight="bold",
        )
        # 400 mm scale bar with 100 mm ticks.
        bar_y, bar_x0 = 8.0, (cfg.cube_size_mm - 400.0) / 2.0
        ax_cap.plot([bar_x0, bar_x0 + 400.0], [bar_y, bar_y], "k-", lw=2)
        for k in range(5):
            x = bar_x0 + 100.0 * k
            ax_cap.plot([x, x], [bar_y - 2.5, bar_y + 2.5], "k-", lw=2)
            ax_cap.text(x, bar_y - 4, f"{100 * k}", fontsize=8, ha="center", va="top")
        ax_cap.text(bar_x0 + 420, bar_y, "mm", fontsize=9, va="center")

        path = out_dir / f"{face.lower()}.pdf"
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
        paths.append(path)
    return paths


def write_cube_net(model: CubeModel, path: str | Path, px_per_mm: float = 0.5) -> Path:
    """Assembly diagram: the cross net with orientation, axes, and fold notes."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cfg = model.cfg
    S = cfg.cube_size_mm
    # Net tile positions (col, row) in a 4x3 grid; row 0 = top.
    tiles = {"TOP": (1, 0), "LEFT": (0, 1), "FRONT": (1, 1), "RIGHT": (2, 1), "BACK": (3, 1), "BOTTOM": (1, 2)}

    fig, ax = plt.subplots(figsize=(16, 12))
    for face, (col, row) in tiles.items():
        art = face_print_image(model, face, px_per_mm=px_per_mm)
        x0, y0 = col * S, (2 - row) * S  # matplotlib y grows upward
        # Tiles are laid out in mm so the net is dimensionally true at any zoom.
        ax.imshow(art, cmap="gray", vmin=0, vmax=255,
                  extent=(x0, x0 + S, y0, y0 + S), interpolation="none")
        ax.add_patch(plt.Rectangle((x0, y0), S, S, fill=False, ec="tab:red", lw=1.5))
        ax.text(x0 + S / 2, y0 + S / 2, face, fontsize=26, ha="center", va="center",
                color="tab:red", weight="bold",
                bbox={"facecolor": "white", "alpha": 0.75, "boxstyle": "round"})
        ax.annotate("", xy=(x0 + 40, y0 + S - 15), xytext=(x0 + 40, y0 + S - 90),
                    arrowprops={"arrowstyle": "-|>", "color": "tab:blue", "lw": 2})
        ax.text(x0 + 52, y0 + S - 52, "UP", color="tab:blue", fontsize=12, weight="bold")

    ax.set_xlim(-60, 4 * S + 60)
    ax.set_ylim(-170, 3 * S + 60)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(
        "Cube assembly net — every face printed UPRIGHT, fold with print OUTSIDE, no rotations needed",
        fontsize=15,
    )
    ax.text(
        0, -30,
        "Fold: RIGHT wraps right of FRONT, BACK continues past RIGHT, LEFT wraps left, "
        "TOP folds back over the top, BOTTOM folds under.\n"
        "Cube frame (origin = cube centre): +X out of RIGHT · +Y out of BACK · +Z out of TOP "
        "(right-handed; stand at FRONT: X = your right, Z = up).\n"
        f"Each printed face is {S:.0f} × {S:.0f} mm; verify each PDF's scale bar reads 400 mm "
        "before mounting. Mount so the UP arrow points to the cube's top on the four side "
        "faces; TOP's UP arrow points toward BACK; BOTTOM's UP arrow points toward FRONT.",
        fontsize=11, va="top",
    )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path
