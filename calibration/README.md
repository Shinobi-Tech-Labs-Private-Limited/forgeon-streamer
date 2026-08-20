# ChArUco Calibration Cube — Multi-Camera Extrinsic Calibration

A complete system for calibrating the relative poses (extrinsics) of multiple
cameras around a capture volume using a rigid cube with a unique ChArUco board
on each face. Cameras do **not** need to see the same face — any view of any
face localizes a camera in the same cube coordinate system.

```
PHYSICAL PRINTED CORNER
        ↓  (face-local x, y millimetres — config.yaml is the single source)
face-local coordinate
        ↓  T_cube_from_face   (cube_geometry.py, numerically verified)
known cube/world 3D coordinate
        ↓  detection (detection.py: markers → face → ChArUco corners)
detected image pixel
        ↓  PnP (pose.py: solvePnP → T_camera_from_cube)
camera extrinsics
        ↓  chaining (multicam.py: T_cam2_from_cam1 via the cube)
camera-to-camera transforms
```

Every arrow is covered by automated tests (`backend/tests/test_calibration_*.py`)
and by three levels of simulation that run **before anything is printed**.

---

## The mathematics, from zero

### Coordinate frames and transforms

A *frame* is a ruler set: an origin plus three perpendicular axes. The same
physical point has different numbers in different frames. A rigid transform
converts between them, and we name every transform by its direction:

```
P_a = T_a_from_b @ P_b        # reads right-to-left: "a from b"
```

`T` is a 4×4 matrix combining a rotation `R` (3×3) and translation `t` (3×1).
Inverting swaps the direction: `T_b_from_a = inv(T_a_from_b)`. A matrix named
just `T` is banned in this codebase — direction always matters.

### The cube frame (the world)

Origin at the cube centre. Right-handed, Z-up:

```
+X = outward normal of RIGHT      -X = LEFT
+Y = outward normal of BACK       -Y = FRONT
+Z = outward normal of TOP        -Z = BOTTOM
```

Stand in front of the cube: X is your right, Z is up, Y points away from you.
Each pattern lies on a plane at ±250 mm (for the 500 mm cube).

This is the *solver's* frame. For real use you almost always want the origin
on the **ground** and +Y **down the venue** — see "World frame, ground plane &
placement" below; the solver output is re-based, never re-solved.

### Face-local frames

OpenCV describes each ChArUco board in its own flat frame: origin at the
print's top-left, x = print-right, y = print-**down**, z = 0 on the paper.
`T_cube_from_face` (one per face, in `cube_geometry.py`) places that flat
frame onto the correct cube face:

```
P_cube = T_cube_from_face @ [x_face, y_face, 0, 1]^T
```

Because x is print-right and y is print-down, the board's z axis points INTO
the cube; the outward face normal is `-z_board`. Tests assert this, plus
right-handedness, plane placement, parallel/perpendicular faces.

`FACE_AXES` in `cube_geometry.py` is the **design** (cross-net, every tile
upright). The **as-built** print orientation is `FACE_AXES` rotated about the
outward normal by `config.yaml → mounting.face_rotation_deg[face]` (see
"As-built mounting" under Part C); `face_axes(cfg, face)` returns the built
axes and everything downstream uses them.

### Intrinsics vs extrinsics

- **Intrinsics** (`fx, fy, cx, cy` + distortion): how the camera maps a ray to
  a pixel. `fx, fy` scale (focal length in pixel units), `cx, cy` locate the
  image centre of projection, distortion bends straight lines near the edges.
  Property of the camera+lens unit; doesn't change when the camera moves.
  Calibrated separately with a flat board (`intrinsics.py`, `calib_intrinsics.py`).
- **Extrinsics** (`R, t`): where the camera is — the transform between the
  cube/world frame and the camera frame. This is what the cube estimates.
  The cube is used for extrinsics because it gives known 3D points spread
  around a volume that different cameras can see from different sides.

### What solvePnP returns (memorize this)

OpenCV's PnP solves for `(rvec, tvec)` such that

```
X_camera = R_camera_from_cube @ X_cube + t_camera_from_cube
```

i.e. **T_camera_from_cube** — it maps world points INTO the camera frame
(camera frame: +X image-right, +Y image-down, +Z out through the lens).
The camera's *position* in the world is in the inverse:

```
T_cube_from_camera = inv(T_camera_from_cube)
camera_centre_cube = T_cube_from_camera[:3, 3]   ( = -R^T @ t )
```

### 2D↔3D correspondences and why PnP works

If you know that pixel (u, v) is the image of a known 3D point, each such pair
constrains where the camera can be. With ≥4 well-spread pairs (we typically
use 40–90), there is a unique camera pose that projects all the 3D points onto
their detected pixels. That is PnP.

**Planar trap:** if all points lie on ONE plane (a single cube face), two
mirror poses explain the image almost equally well (the "IPPE flip"). The code
uses `SOLVEPNP_IPPE` for single-face views, compares both solutions, and
rejects the frame as `ambiguous` when they can't be distinguished. Views
showing **two faces** are non-coplanar → unique solution. Prefer them.

### Why cameras seeing different faces still calibrate

Camera A sees FRONT corners; camera B sees only RIGHT corners; they share zero
markers. But both sets of observations map into the SAME rigid cube frame, so:

```
A: T_camA_from_cube        B: T_camB_from_cube
T_camB_from_camA = T_camB_from_cube @ inv(T_camA_from_cube)
```

The cube is the common anchor. (Verified in
`test_cameras_seeing_only_disjoint_faces_still_chain`.)

### ArUco marker ID vs ChArUco corner ID — never confuse them

| | ArUco marker ID | ChArUco corner ID |
|---|---|---|
| what | the code inside a black square | an interior checker intersection |
| uniqueness | **unique across the whole cube** (disjoint ranges per face) | local 0..8 — **repeats on every face** |
| identifies a face | ✔ (any single marker) | ✘ |
| precise 2D point | corners usable, centres **never** | ✔ best sub-pixel accuracy |
| role | identify + assist interpolation | the actual measurement points |

A corner is only unambiguous as `(face, local_id)` — the global key `FRONT:4`.
Marker *corners* (4 per marker) are used as supplementary correspondences;
marker *centres* never are.

---

## Layout & IDs (defaults, from `config.yaml`)

- Cube 500 mm (printed-surface to printed-surface). Margin 30 mm per side.
- Per face: **4×4 squares of 110 mm**, markers 82.5 mm, `DICT_4X4_250`,
  9 chessboard corners + 8 markers per face.
- Marker ID ranges: FRONT 0–7 · RIGHT 10–17 · BACK 20–27 · LEFT 30–37 ·
  TOP 40–47 · BOTTOM 50–57 (flat intrinsics board uses 100+).

Why so few, so large? Detection needs each marker ≥ ~20 px. On the indoor
OV9782 (1280×800, fx≈933 px) the simulated envelope is:

| distance | marker px | result |
|---|---|---|
| 2–3 m | 26–39 | 2 faces, 70–90 points, pose errors ≲0.15° / ≲7 mm |
| 4 m | 19 | marginal (single face, degraded) |
| ≥5 m | ≤15 | **no detection** |

The task doc's 70/50 mm defaults die beyond 2.5 m on this sensor — hence the
larger squares. For the outdoor stage (cameras up to 40 m out) NO printable
cube is directly detectable (a 500 mm face is ~11 px at 40 m); use
**multi-placement calibration**: move the cube between placements so each
placement is near (≤4 m) a subset of cameras, keep frames grouped per
placement, and chain cameras through shared placements. Never mix frames from
different cube placements in one solve — each placement is its own world frame.

---

## Calibration run sheet — start to finish

The one-page order of operations. Each step points at the Part with detail.
Result: every camera's intrinsics, every camera's pose in a **ground-origin,
venue-aligned** world frame, and a physical pass/fail.

| # | Step | Command / where | Done when |
|---|---|---|---|
| 0 | Assets exist & verified (once) | `calib_generate_assets.py` → `calib_verify_print_files.py` (Part A) | verify script passes on the PNG/PDFs |
| 1 | Cube printed, built, faces checked (once) | Parts B, C; `calib_detect_cube.py --image <photo>` per face | every face detects with its own IDs, correct name, all corners |
| 2 | **Intrinsics — one run per camera unit** | helper holds the cube 1–1.5 m in front of the *fixed* lens, 20–40 shots covering the whole image; `calib_intrinsics.py calibrate --target cube --images-dir data/<cam>_intrinsics --out intrinsics/<cam>.yaml` (Part E) — or the tab's Intrinsics stage | RMS ≲ 0.5 px, cx/cy near image centre, sane distortion |
| 3 | **Place the cube for extrinsics** | on its BOTTOM face on the playing surface at the athlete's spot, BACK toward the venue direction, spirit-level TOP, 2–3.5 m from each camera, each camera sees two faces (Part D + placement sketch below) | nobody touches it again until step 6 |
| 4 | Capture extrinsics images | 10–20 frames per camera, cube untouched, into `data/<cam>/` — or the tab's Extrinsics stage (rig snapshot) | every camera has ≥10 frames with 2-face detections |
| 5 | **Solve with the floor origin** | `calib_multicamera.py --data-dir data --intrinsics-dir intrinsics --out-dir output/calibration --world-origin floor --support-height-mm 0 --forward-face BACK --debug-overlays` — or the tab (World frame card, floor origin default) → *Solve calibration* | `calibration.yaml` has a `world` block, `world_frame: floor_under_cube_center` |
| 6 | Validate (Part F) | RMS ≈0.3–1 px; pose spread ≲0.3°/10 mm; `extrinsics_3d.png` looks like the room; **tape:** camera-to-camera distances vs `camera_center_*` AND each camera's `camera_center_world_mm[2]` vs its lens height above the floor | tape agrees within ~2–5 cm; heights within ~2 cm |
| 7 | Hand over | `calibration.yaml` (+ `T_world_from_camera`), pairwise `extrinsics_*.yml` for `pose_pipeline_1.py`; note the cube spot & forward direction in the session record | 3D output is metres above ground / down the venue |

Ground level is **not** a separate step: it is decided at step 3 (cube on the
floor) and applied at step 5 (`--world-origin floor`). If the cube had to sit
on a box, measure the box and pass `--support-height-mm`. Repeat steps 3–6
whenever a camera is moved or re-mounted; repeat step 2 only if a lens/focus
changes.

## Part A — Generate

```bash
cd backend
venv/Scripts/python scripts/calib_validate_cube.py       # model invariants
venv/Scripts/python scripts/calib_generate_assets.py     # faces, net, JSON DBs
venv/Scripts/python scripts/calib_verify_print_files.py  # detect on the shipped PNGs/PDFs
venv/Scripts/python scripts/calib_synthetic_test.py --mode all   # sims
python -m pytest tests/test_calibration_*.py             # full test suite
```

`calib_verify_print_files.py` runs the real detector against the exact files
going to the printer (full-res PNG, a simulated photograph of it, and the PDF
rasterized at print density) and checks IDs, corner counts and the measured
square size — never send files to print without it passing.

Outputs land in `app/calibration/output/`: `faces/*.png|pdf`, `faces/cube_net.png`,
`config/cube_points_3d.json`, `config/marker_face_map.json`,
`config/face_transforms.json`, `visualization/cube_model.png`.

## Part B — Print

- Faces are 500×500 mm → **large-format print shop** (A1 plotter or, better,
  direct UV print onto rigid board such as 3 mm dibond/ACM). Home printers
  cannot do this size; tiling A4 sheets ruins geometry.
- Give the shop the PDFs; demand **100% scale, no "fit to page"**.
- Verify each print with a steel rule: the scale bar under the face must read
  **400 mm within ±0.5 mm**, and any checker square must measure 110.0 mm.
  Print scale error translates DIRECTLY into translation error: a 1% scale
  error makes every distance estimate 1% wrong.
- The PNGs carry true DPI metadata if the shop prefers raster.

## Part C — Build

- Substrate: 3 mm aluminium composite (dibond) or 6 mm MDF. Avoid foam board
  and cardboard at this size — they bow, and a 1 mm bow moves corners ~1 mm.
- Build a square internal frame (aluminium extrusion or CNC-cut plywood
  skeleton); check squareness with a carpenter's square and by measuring both
  face diagonals (equal diagonals = square).
- **`cube.size_mm` means printed-surface to printed-surface.** After assembly,
  measure across opposing faces with calipers/tape at several spots: target
  500 ± 1 mm everywhere. If your panels add thickness, either build the core
  smaller so the outside stays 500 mm, or measure the real value and update
  `config.yaml`, then regenerate `output/config/`.
- Mount using `faces/cube_net.png`: every face printed upright, folded with
  print outside — **no face needs rotating**. UP arrows: side faces point to
  the cube top; TOP's UP points toward BACK; BOTTOM's toward FRONT.
- Centre each print on its face (equal margins by ruler), edges parallel to
  cube edges.

### As-built mounting (if a face went on rotated)

A tile pasted a quarter/half-turn off is **not** a rebuild — the marker IDs
still identify the face, only the in-plane orientation of its corner grid
differs. Record the deviation in `config.yaml`:

```yaml
mounting:
  face_rotation_deg:   # degrees CCW as seen from OUTSIDE the cube, quarter turns only
    FRONT: 180
    RIGHT: 90
    LEFT: 270
```

`face_axes()` rotates the design print axes about the outward normal by that
angle, so the corner database, solver, synthetic renderer and visualizer all
see the physical cube. Nothing else needs touching; `pytest
tests/test_calibration_cube_geometry.py` covers the rotation semantics.

**How to measure it** — do not use gravity (the cube gets rolled around for
photos). Use face adjacency: photograph the face together with a neighbour,
then note where the print's "down" points (`calib_detect_cube.py --image`
gives you the marker corner order: corner 0→1 = print-right, 0→3 =
print-down). Design: every side face's print-down points at BOTTOM; TOP's print-down
points at FRONT; BOTTOM's print-down points at BACK. Compare with what the
photo shows and write down the CCW quarter turns needed to get there. Cross-check by solving one PnP pose on a
two-face photo: with the correct rotation both faces agree (a few px RMS even
with rough phone intrinsics); a wrong quarter turn gives hundreds of px.

**Off-centre pastes** are recorded the same way. Measure the white margin on
each side of a face (design 30 mm); the pattern-centre shift is
`(opposite margin − this margin) / 2`, expressed as `right`/`up` seen from
outside in the cross-net orientation (side faces: viewer upright; TOP: up =
toward BACK; BOTTOM: up = toward FRONT):

```yaml
mounting:
  face_shift_mm:
    FRONT: {right: 0.0, up: 4.0}     # top margin 24, bottom 32
    LEFT:  {right: -6.5, up: -3.5}   # left 23 / right 36, top 33 / bottom 26
```

If the two margins on an axis do not sum to 2 × design margin, that is a
**scale** discrepancy (print not at 100 %, or face not `cube.size_mm`) — a
shift cannot fix it; ruler-check a 110 mm square and the 440 mm board and
measure face-to-face.

**The physical cube built 2026-08-17** measured: FRONT 180°, RIGHT 90°,
LEFT 270°, TOP 0°, BOTTOM 0°, BACK 0° — all six verified by two-face PnP on
build photos (1.4–5 px vs 140–500 px for every other quarter turn). Values
live in `config.yaml`; if a face is ever re-pasted, re-measure and update
only that file. Margins measured the same day gave shifts FRONT up 4,
RIGHT right −3.5, BACK up −5, LEFT right −6.5 / up −3.5 (TOP/BOTTOM not
measured); margins summed to 56–59 mm per axis, so print/face scale is still
to be confirmed.

Build-tolerance budget (Monte-Carlo, `--mode tolerance`): mounting offsets
≤1 mm and tilts ≤0.5° keep the build's contribution below the per-image noise
floor; 2 mm / 1° roughly doubles extrinsic error. **Warning:** reprojection RMS
barely rises with build error (0.37→0.57 px for 4× worse build) — a bad build
hides from RMS, which is why Part F includes physical cross-checks.

## Part D — Capture

- Put the cube **on its BOTTOM face directly on the playing surface** (pitch,
  lane, platform) at the spot the athlete will occupy, **2–3.5 m** from each
  OV9782 (hard limit ~4 m). BOTTOM is sacrificial — it faces the ground. If
  the cube must sit higher for visibility, use a rigid, levelled box of
  **known height** (measure it; it goes into `--support-height-mm`).
- Turn the cube so its **BACK face points down the venue** (bowling /
  shooting / lifting direction you want as +Y). It need not be perfect —
  `--forward-face` handles 90° cases and `--yaw-deg` trims the rest — but
  square is easiest to reason about later.
- The cube must remain **stationary** for one calibration set: its frame IS
  the world frame all cameras get chained through. (Images need NOT be
  simultaneous — a fixed cube makes time sync irrelevant.) If any camera can't
  see it well, use multiple placements as described above.
- Aim each camera so it sees **two faces** (stand off the face axes ~30–55°);
  a lone face viewed head-on is the ambiguous worst case and gets rejected.
  With three cameras around a volume this usually means the cube's corners —
  not its faces — point at the cameras (see the placement sketch below).
- Good even lighting, no glare (matte lamination helps); fixed focus set AT
  the cube distance; stop down if possible; avoid motion blur (nothing moves,
  so use a low gain / longer exposure).
- 10–20 images per camera. More images average down noise ~√N.

## Part E — Calibrate

```bash
# once per camera unit, with the flat board:
venv/Scripts/python scripts/calib_intrinsics.py board --out board.png   # print A3 @100%
venv/Scripts/python scripts/calib_intrinsics.py calibrate \
    --images-dir data/camera_1_intrinsics --out intrinsics/camera_1.yaml

# OR, without printing a flat board — use the cube itself (--target cube):
# with rig-mounted (immovable) cameras, a helper holds the cube ~1-1.5 m in
# front of the lens and moves/tilts/rolls it through 20-40 shots, visiting
# every image region. Only the dominant face per image is used, so cube build
# error cannot leak into the lens model (a face print is internally
# printer-accurate; only the BETWEEN-face geometry carries build tolerance),
# and it doesn't matter which face is toward the camera in each shot.
venv/Scripts/python scripts/calib_intrinsics.py calibrate --target cube \
    --images-dir data/camera_1_intrinsics --out intrinsics/camera_1.yaml

# sanity-check any single cube image:
venv/Scripts/python scripts/calib_detect_cube.py --image data/camera_1/img_001.jpg \
    --intrinsics intrinsics/camera_1.yaml

# the multi-camera solve:
venv/Scripts/python scripts/calib_multicamera.py --data-dir data \
    --intrinsics-dir intrinsics --out-dir output/calibration --debug-overlays
```

## Part F — Validate

Inspect, in order:

1. **Per-camera stats printed by `calib_multicamera.py`** — reprojection RMS
   (with these cameras/sizes expect ~0.3–1 px; RMS is resolution- and
   optics-dependent, there is no universal threshold), images used vs
   rejected, and **pose spread** across images (should be ≲0.3° / ≲10 mm —
   large spread = something moved or intrinsics are bad).
2. **Debug overlays** (`output/calibration/debug/`) — corners on corners,
   red reprojection lines invisible at 100% zoom, face labels correct.
3. **`output/calibration/extrinsics_3d.png`** — cameras must appear where they
   physically stand, optical axes toward the cube.
4. **Physical cross-check (catches what RMS can't):** tape-measure the
   camera-to-camera distances and compare with
   `norm(camera_center_cube_mm_i - camera_center_cube_mm_j)` from
   `calibration.yaml`. Agreement within your tolerance budget (~2–5 cm here)
   is the real pass criterion.
5. **Upside-down face check:** a mis-mounted face makes its markers detect
   fine but its corners reproject tens–hundreds of px off, so that face's
   frames get rejected en masse — per-face stats in `calib_detect_cube.py`
   expose exactly which face disagrees with the others.

## World frame, ground plane & placement

### What the cube gives you

Every camera solves `T_camera_from_cube`, so all cameras share one metric
frame. Any point seen by ≥2 cameras triangulates to millimetres in that frame
— that is your "3D space". Three things the cube does **not** do on its own:

1. It does not know where the ground is — unless you tell it how the cube
   was resting.
2. It does not know which way the venue runs — unless you orient the cube
   (or name the face) accordingly.
3. It does not define the usable volume — that is wherever ≥2 camera views
   overlap. The cube only has to be visible to each camera during capture.

Points 1 and 2 are solved by a **WorldFrame** re-basing of the solver output
(`multicam.WorldFrame`), never by re-solving.

### Ground plane from the cube on the floor

If the cube stands on its BOTTOM face on the playing surface, the ground is
the plane `z = -(size/2 + max(pattern_offset, 0) + support_height)` in the
cube frame — with the 500 mm cube and no support that is **z = −250 mm**.
Choosing `--world-origin floor` moves the origin to the ground point directly
under the cube centre: `T_world_from_cube = Rz(yaw) · translate(0, 0, +250)`,
so ground = **Z = 0**, cube centre at Z = +250 mm, camera `Z` = height above
the ground you can check with a tape.

The surface the cube rests on defines "level" — a floor tilted 0.5° tilts
world Z by 0.5° (≈9 mm of "height" per metre of horizontal distance). So:
flattest spot in the capture volume, ideally the actual playing surface, and
a spirit level on TOP before capture. Do **not** put the cube on a
tripod/stand unless it is levelled and its height is measured; "sacrificial
BOTTOM on a stand" (the old advice) throws the ground plane away.

### Venue axes

`--forward-face` names the side face whose outward normal becomes world +Y
(BACK is already +Y in the cube frame; RIGHT → yaw +90°, FRONT → 180°,
LEFT → −90°). `--yaw-deg` adds a counter-clockwise-from-above trim for a
cube that was not squared to the venue line (positive undoes a clockwise
mis-placement). Z stays up; X is to the right when looking along +Y.

### Where to place the cube — 3-camera indoor rig

Sketch, viewed from above (`C` = camera, `■` = cube, +Y = venue direction):

```
              +Y  (down the venue)
               ▲
     C2        │        C3
       ╲       │       ╱
        ╲      │      ╱
         ╲     │     ╱
          ╲   ┌─┴─┐ ╱
           ╲  │ ■ │╱     cube corner — not a face — points at each camera
            ╲ └───┘      (each camera sees two faces, 30–55° off-axis)
              ╲   ╱
               ╲ ╱
                C1        2–3.5 m from every camera
```

- Cube where the athlete will stand (its ground contact IS the origin).
- Cameras 2–3.5 m away, each looking at a vertical **edge** of the cube so
  two faces are visible; a face seen square-on gets rejected as ambiguous.
- If one camera cannot see the cube from that spot, don't move the cameras
  — either accept a second **placement** (see multi-placement caveats above;
  chaining is not yet implemented, so keep to one placement for now) or move
  the cube slightly and re-capture *all* cameras.
- Cube heights above the floor also let you sanity-check camera mounts:
  the solved camera Z should equal the tape-measured lens height.

### How to run it

```bash
venv/Scripts/python scripts/calib_multicamera.py --data-dir data \
    --intrinsics-dir intrinsics --out-dir output/calibration \
    --world-origin floor --support-height-mm 0 --forward-face BACK --yaw-deg 0
```

The Calibration tab exposes the same four fields ("World frame for the
solve"; floor origin is its default) and the API accepts them as the JSON
body of `POST /calibration/extrinsics/solve`.

`calibration.yaml` then carries, per camera, both the raw
`T_camera_from_cube` / `T_cube_from_camera` and `T_camera_from_world` /
`T_world_from_camera` / `camera_center_world_mm`, plus a top-level `world`
block (`origin`, `forward_face`, `yaw_deg`, `support_height_mm`,
`cube_center_world_mm`, `ground_plane`, `T_world_from_cube`). The pairwise
FileStorage files are **unchanged** by the world choice — camera-to-camera
transforms are frame-free — so `pose_pipeline_1.py` keeps working; apply
`T_world_from_camera` (of the reference camera) to its triangulated points to
land them on the ground frame.

## Part G — Use the extrinsics

`calibration.yaml` holds `T_camera_from_cube` and `T_cube_from_camera` per
camera; pairwise `extrinsics_camera_1_to_camera_N.yml` (OpenCV FileStorage
R/T, in mm) match the format `stress_3d/3D/pose_pipeline_1.py` already loads.
To triangulate a human joint seen by several cameras: undistort each detected
pixel, back-project a ray per camera, transform rays into the cube frame with
`T_cube_from_camera`, and intersect (DLT / least squares) — the intersection
is the joint's 3D position in cube coordinates, in millimetres. Better
extrinsics → tighter intersections → smaller 3D error. Multiply by
`T_world_from_cube` (or triangulate with `T_world_from_camera` directly) to
get ground-referenced coordinates: Z = height above the floor, Y = distance
down the venue.
NOTE: `pose_pipeline_1.py` currently triangulates with K only and drops the
distortion terms — undistort points first (or feed undistorted frames) or
accuracy silently degrades at the image edges.

---

## What the physical errors do (doc §24)

| defect | effect |
|---|---|
| cube 501 mm but model says 500 | uniform ~0.2% scale: camera distances all 0.2% off; rotations unaffected; reprojection barely changes (it hides) |
| one face tilted 1° | that face's points leave their plane by up to ~4 mm; single-face poses off that face are rotated ~1°; multi-face PnP splits the difference and RMS rises for that face |
| paper stretch (print scale) | same as wrong square size: per-face scale error → translation bias along the view axis |
| board mounted 2 mm off-centre | rigid 2 mm bias of that face's points → per-face translation bias; shows as per-face reprojection disagreement in multi-face views |
| face rotated 90/180° | markers still identify the face, but corner positions are wildly wrong → huge reprojection → frames rejected; see Part F.5 |
| cube not square (skewed) | systematic inter-face inconsistency; multi-face RMS floor rises; physical cross-check (F.4) disagrees |
| wrong marker size in config | ChArUco interpolation still works (corners are checker intersections) but supplementary marker-corner correspondences bias the solve — keep `marker_length_mm` exact |

Translation is hurt by scale-type errors, rotation by tilt-type errors;
triangulation inherits both (baseline error ≈ proportional 3D error).
Validation: measure with calipers (squares, marker sides, cube span, margins)
and let the per-face reprojection stats arbitrate disagreements.

## The classic mistakes this system is built to avoid (doc §25)

Identical IDs on every face · assuming shared markers between cameras ·
per-face coordinate systems that never unify · guessed face rotations ·
mm/m mixing (everything is mm) · `T` without direction in the name ·
OpenCV camera convention vs cube convention confusion (documented above) ·
marker IDs vs corner IDs interchanged · cross-face corner interpolation ·
one-photo calibration · ignoring distortion · ignoring print scale ·
ignoring build error · unvalidated geometry — every item has a test or a
validated code path; see `tests/test_calibration_*.py`.

## The ten proof questions (doc §30)

1. **Global 3D of ChArUco corner 5 on FRONT?** `CubeModel().corner_cube_mm("FRONT", 5)`
   → `[110.0, -250.0, 0.0]` mm (also in `cube_points_3d.json` as `FRONT:5`).
2. **Corner 5 on RIGHT?** `corner_cube_mm("RIGHT", 5)` → `[250.0, 110.0, 0.0]` mm.
3. **Same local ID, different points — why?** Local corner IDs number the
   interior intersections of each board separately (0..8 on every face); the
   physical point is the pair `(face, local_id)` — that's why the global key
   is `FACE:id`, and why `T_cube_from_face` differs per face.
4. **Marker ID → face?** Integer-range lookup (`marker_face_map.json` /
   `CubeModel.face_of_marker`): 0–7 FRONT, 10–17 RIGHT, … 50–57 BOTTOM.
5. **Cam1 sees only FRONT, cam2 only RIGHT — how do they unify?** Each solves
   PnP against cube-frame 3D points of its own face, yielding
   `T_cam1_from_cube` and `T_cam2_from_cube` in the SAME frame; then
   `T_cam2_from_cam1 = T_cam2_from_cube @ inv(T_cam1_from_cube)`.
6. **Cube→camera-1 transform?** `T_camera1_from_cube` (what solvePnP returns).
7. **Camera-1's position in cube coordinates?**
   `T_cube_from_camera1 = inv(T_camera1_from_cube)`; its translation column is
   the camera centre (`camera_center_cube_mm` in calibration.yaml).
8. **Camera 2 relative to camera 1?** `T_cam2_from_cam1 = T_cam2_from_cube @
   T_cube_from_cam1` (implemented as `multicam.T_camB_from_camA`).
9. **Verify the printed cube matches the model?** Rulers/calipers against
   config (square 110.0 mm, board 440 mm, span 500 mm, scale bar 400 mm), then
   photograph and check per-face reprojection stats + physical camera-distance
   cross-check (Part F).
10. **Detect a face mounted upside-down?** Its markers still identify it, but
    every corner reprojects far off (the 180° flip moves corner `(face,0)` to
    where `(face,8)` should be) — per-face RMS explodes for exactly that face
    while others stay sub-pixel (Part F.5).

## Module map

```
app/calibration/
  config.yaml       single source of all physical dimensions + camera specs
  config.py         load/validate; derived geometry (margins, ID ranges)
  boards.py         per-face CharucoBoard with unique IDs; ID validators
  cube_geometry.py  cube frame, T_cube_from_face, global 3D corner DB, export
  detection.py      markers → faces → per-face ChArUco corners → 2D↔3D pairs
  pose.py           PnP (IPPE ambiguity handling), SE(3)/quaternion averaging
  multicam.py       per-camera fusion, camera-to-camera chaining, WorldFrame (floor origin / venue axes), writers
  intrinsics.py     flat-board intrinsic calibration + intrinsics loaders
  rendering.py      Level-2 simulator: render real artwork into virtual cameras
  simulate.py       Level-1 round-trip, Level-2 runner, distance sweep, Level-3 Monte-Carlo
  print_assets.py   face PNGs/PDFs (true scale + scale bars), assembly net
  visualize.py      3D cube + calibrated camera visualization

scripts/            calib_validate_cube · calib_generate_assets · calib_intrinsics
                    · calib_detect_cube · calib_multicamera · calib_synthetic_test
tests/              test_calibration_cube_geometry · test_calibration_synthetic_pose
                    · test_calibration_detection · test_calibration_intrinsics
```

Requirements: the backend venv already satisfies everything
(`opencv-contrib-python==4.9.0.80`, `numpy==1.26.4`, `PyYAML`, `matplotlib`,
`Pillow`); `requirements.txt` here lists the same pins for standalone use.
OpenCV note: written for the modern ChArUco API (`cv2.aruco.CharucoBoard`
tuple constructor + `CharucoDetector`, available 4.7+); the deprecated
`interpolateCornersCharuco` path is deliberately not used, and boards use the
modern (non-legacy) pattern — if another tool regenerates the artwork, its
start-square colour must match (`setLegacyPattern(False)`).
