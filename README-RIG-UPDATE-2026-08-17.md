# Rig update — 2026-08-17 (cube-aware focus + calibration snapshots)

Copy the contents of this folder into the rig laptop's recording-scripts
directory (the one holding app35_cam_sole_V11.py), overwriting:

    app35_cam_sole_V13.py                 <- rig app (V12 base plus the changes below, installed here as V13)
    templates/index35_cam_sole_V8.html    <- rig page (your Downloads copy of 21:30 + focus panel changes)
    codesharpnessmeasure/                 <- focus tool, now scores the calibration CUBE
    calibration/                          <- NEW: cube model package (needed by codesharpnessmeasure)

Layout after copying (side by side, templates/ is Flask's template folder):

    <scripts dir>/
      app35_cam_sole_V13.py
      templates/index35_cam_sole_V8.html
      heartbeat_manager.py, mic_capture_manager.py, intrinsic_calibrate_charuco.py, ...
      codesharpnessmeasure/
      calibration/            (config.yaml inside carries the as-built cube values)

Python deps (already present for V11): opencv-contrib-python (>= 4.8, cv2.aruco),
numpy, pyyaml, flask, flask-cors. `calibration/requirements.txt` lists them.

## What changed

app35_cam_sole_V13.py (4 small edits on top of V12)
- POST /api/snapshots  - one JPEG per camera via the existing ffmpeg snapshot path,
  no legacy calibration side effects (used by the admin Calibration tab)
- CORS: http://localhost:3000, http://127.0.0.1:3000 added
- /focus/<cam>, /focus/all now use codesharpnessmeasure.measure_frame() with a
  shared FocusTracker: label = PEAK / NEAR / LOW relative to that camera's
  best-so-far; payload adds best, ratio_to_best, markers, faces, target
- POST /focus/reset  (body {"camera": "cam1"} or empty = all) forgets the bests

templates/index35_cam_sole_V8.html (focus panel only, everything else untouched)
- per camera: big score, best, "Reset best" button, PEAK/NEAR/LOW badge, ratio bar,
  markers + faces seen. Still polls /focus/all every second as before.
- With the motorised lens buttons right below it: nudge coarse/fine, watch the
  score, overshoot, come back until the badge says PEAK.

codesharpnessmeasure/
- focus/charuco.py: detects the calibration cube (any faces) first, legacy flat
  board as fallback; crop = hull of all marker corners; square_px from marker sides
- focus/scorer.py: fixed INTER_AREA upscaling (blurrier distant board scored higher)
  and the +-5 % resize dead-band; FocusTracker (PEAK >= 97 % of best, NEAR >= 85 %,
  LOW; env FOCUS_PEAK_RATIO / FOCUS_NEAR_RATIO); measure_frame()
- app.py: uses measure_frame + tracker; POST /focus/reset
- templates/index.html: solo focusing page — auto-refresh, big score, best, PEAK
  badge + bar, per-camera / all reset, optional spoken "% of best" for one camera
- tests/test_focus.py (needs FORGEON_BACKEND=<repo>/backend to run)

calibration/ (new)
- The ChArUco cube model: config.yaml (500 mm cube, 4x4 @ 110 mm, DICT_4X4_250,
  ids stride 10, AS-BUILT mounting rotations FRONT 180 / RIGHT 90 / LEFT 270 and
  paste offsets), detector, geometry, pose, multicam, intrinsics, rendering, etc.
- Only config/boards/cube_geometry/detection are needed by the focus tool; the
  rest is harmless to ship (used by backend/scripts/calib_*.py in the repo).

## Focusing alone (per camera)
1. Start V13 (or `python codesharpnessmeasure/app.py`), open the calibration page.
2. Cube 2-3 m in front of the lens, 1-2 faces visible; check it says a face name,
   markers >= 4.
3. Press "Reset best" for that camera. Turn the focus ring: score climbs, then
   falls -> walk back until the badge says PEAK. Optionally choose the camera
   under "speak" to hear the % of best while at the lens.
4. Reset best whenever the cube or camera moves. Do not compare scores between
   cameras or distances - only against that camera's own best.

## Quick checks on the rig
    curl -X POST http://localhost:5000/api/snapshots
    curl http://localhost:5000/focus/all
    curl -X POST http://localhost:5000/focus/reset
