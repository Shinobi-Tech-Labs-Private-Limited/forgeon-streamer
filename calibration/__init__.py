"""ChArUco calibration-cube system for multi-camera extrinsic calibration.

Single source of truth for geometry is config.yaml (loaded via CalibrationConfig).
Transform naming encodes direction everywhere: T_a_from_b maps points expressed
in frame b into frame a (P_a = T_a_from_b @ P_b).
"""

from .config import CalibrationConfig
from .cube_geometry import CubeModel, FACES

__all__ = ["CalibrationConfig", "CubeModel", "FACES"]
