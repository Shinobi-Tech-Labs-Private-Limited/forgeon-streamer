# Standalone rig tools

These are operator/development utilities, not Flask rig entry points. None is
imported by `../app35_cam_sole.py`.

## `app13.py`

Standalone Tkinter GUI for scanning, connecting to, monitoring, and recording
BLE pressure-insole devices. Historical shell commands launched it manually as
`python app13.py` or `env/bin/python app13.py`.

New launch path from the application root:

```bash
.venv/bin/python tools/app13.py
```

## `app14.py`

Later standalone Tkinter BLE pressure-insole GUI with additional data handling
and export functionality. Historical shell commands launched it manually as
`python app14.py` or `python3 app14.py`.

New launch path from the application root:

```bash
.venv/bin/python tools/app14.py
```

Moving these files does not affect V13, but any desktop shortcut or operator
note that launches them directly must use the new paths above.

## Other standalone utilities

None of the following is imported by V13:

| File | Purpose | Launch from application root |
|---|---|---|
| `calib_focus.py` | Earlier interactive calibration-focus utility | `.venv/bin/python tools/calib_focus.py` |
| `focus.py` | Earlier standalone focus meter | `.venv/bin/python tools/focus.py` |
| `led.py` | Standalone BLE/HTTP LED synchronization utility | `.venv/bin/python tools/led.py` |
| `multicam_snap.py` | Interactive multi-camera snapshot utility | `.venv/bin/python tools/multicam_snap.py` |
| `lowres_bench.py` | Replays the record/sync/undistort pipeline at several capture resolutions and reports time, size, PSNR/SSIM (docs/lowres-benchmark.md) | `.venv/bin/python tools/lowres_bench.py --src take.mp4` |

These files were kept as tools because they are self-contained and may still
be useful during rig setup or diagnostics. They are not production service
entry points.
