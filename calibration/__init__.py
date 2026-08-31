"""ChArUco calibration-cube system for multi-camera extrinsic calibration.

Single source of truth for geometry is config.yaml (loaded via CalibrationConfig).
Transform naming encodes direction everywhere: T_a_from_b maps points expressed
in frame b into frame a (P_a = T_a_from_b @ P_b).

The cube
--------
A rigid cube (config.yaml: 500 mm printed-surface to printed-surface) with one
ChArUco board on each of its six faces: FRONT, RIGHT, BACK, LEFT, TOP, BOTTOM.
Every face uses the same board layout (4x4 squares of 110 mm, 82.5 mm markers,
DICT_4X4_250) but a DISJOINT range of ArUco marker IDs (FRONT 0-7, RIGHT 10-17,
BACK 20-27, LEFT 30-37, TOP 40-47, BOTTOM 50-57), so any single detected marker
identifies its face. ChArUco chessboard-corner IDs are local (0..8 on every
face) and are only unique as the pair (face, local_id).

Pipeline and module ownership
-----------------------------
    config.yaml -> config.py   CalibrationConfig: dimensions, marker-ID layout
                               and the AS-BUILT mounting corrections
    boards.py                  one cv2.aruco.CharucoBoard per face (mm units)
    cube_geometry.py           cube frame, T_cube_from_face per face, CubeModel
                               (the global 3D database of every corner)
    detection.py               image -> markers -> faces -> ChArUco corners
                               -> 2D<->3D correspondences (CubeDetector)
    pose.py                    PnP -> T_camera_from_cube; pose averaging/spread
    intrinsics.py              per-camera K + distortion (flat board or cube)
                               and intrinsics file loaders
    multicam.py                fuse per-image poses per camera, chain cameras
                               through the cube, WorldFrame re-basing (floor
                               origin, venue axes), YAML/FileStorage writers
    rendering.py, simulate.py  synthetic validation with no hardware (Level 1
                               math round-trip, Level 2 rendered images, Level 3
                               build-tolerance Monte-Carlo, distance sweep)
    print_assets.py            face PNG/PDF artwork and the assembly net
    visualize.py               3D plot of the cube and solved cameras

Frames and units
----------------
Everything physical is in millimetres and angles are in degrees unless a
signature says otherwise. All frames are right-handed.

* Cube frame (the solver's "world"): origin at the cube centre, +X = outward
  normal of RIGHT, +Y = outward normal of BACK, +Z = outward normal of TOP.
  Standing at FRONT looking at the cube: X is your right, Z is up, Y points
  away from you.
* Board (face-local) frame: OpenCV's flat frame for one face, origin at the
  print's top-left, +x = print-right, +y = print-DOWN, z = 0 on the paper. Its
  +z points INTO the cube, so the outward face normal is -z_board.
* Camera frame: OpenCV convention, +X image-right, +Y image-down, +Z out
  through the lens. solvePnP returns T_camera_from_cube.
* World frame (optional, multicam.WorldFrame): the cube frame re-based so the
  origin is on the floor directly under the cube centre and +Y points down
  the venue. The solve never changes; only the reported poses are re-based.

On the rig
----------
app35_cam_sole.py does not import this package directly. The focus scorer
(codesharpnessmeasure/focus/charuco.py) builds one
CubeDetector(CubeModel(CalibrationConfig.load())) and calls detect() on
preview frames to find the cube and score lens focus on it. The multi-camera
solve, print-asset and simulation entry points are the upstream Forgeon
backend scripts (see README.md and SOURCE.md); they are not part of this repo.
config.yaml holds the AS-BUILT values of the physical cube and must be
preserved as-is.
"""

from .config import CalibrationConfig
from .cube_geometry import CubeModel, FACES

__all__ = ["CalibrationConfig", "CubeModel", "FACES"]
