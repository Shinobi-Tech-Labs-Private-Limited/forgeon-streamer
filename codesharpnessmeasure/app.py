"""Standalone focus-check web app for the rig cameras.

A small Flask service that pulls the latest frame from each camera's RTSP
stream, scores how sharply the ChArUco calibration cube is focused, and
reports a relative PEAK / NEAR / LOW verdict so an operator turning a lens
by hand can overshoot and walk back to the peak.

Role on the rig: a bench/setup tool, not part of the recording path. The
production app (app35_cam_sole.py) does NOT run this module; it imports the
scoring pieces (codesharpnessmeasure.focus.scorer.FocusTracker and
measure_frame) directly and feeds them its own preview frames. This file is
the thin HTTP wrapper for focusing cameras without the full app. It binds
port 5000, the same port the production app uses, so run one or the other.

Routes (JSON unless noted):
  GET  /               -> templates/index.html, which polls /focus/all
  GET  /focus/all      -> {cam_id: payload} for every camera, always 200
  GET  /focus/<cam_id> -> payload for one camera; 404 unknown cam, 503 when
                          no frame has arrived or the newest frame is stale
  POST /focus/reset    -> forget best-so-far for one camera ({"camera": id}
                          in the JSON body, or ?camera=) or for all cameras

The payload shape is defined by focus/scorer.py: measure_frame(); roughly
{detected, target, score, label, color, corners, markers, faces, best, ...}.

Config (env):
  FOCUS_CAM1_URL / FOCUS_CAM2_URL / FOCUS_CAM3_URL  RTSP sources. The defaults
      below (192.168.1.10x:8555/camera) do not match the current rig addressing
      in app35_cam_sole.py (CAMERA_SOURCES); set them explicitly.
  FOCUS_MAX_FRAME_AGE   seconds before a frame counts as stale (default 5).
  FOCUS_POOR_THRESH / FOCUS_OK_THRESH  absolute label thresholds, read by
      focus/scorer.py.

Nothing is written to disk; all state is in-process (TRACKER, CAMERAS).
"""

import os
import time

from flask import Flask, jsonify, render_template, request

from focus.scorer import FocusTracker, measure_frame
from focus.stream_reader import RTSPReader

app = Flask(__name__)

# Best-so-far per camera (relative PEAK/NEAR/LOW verdicts; see focus/scorer.py)
TRACKER = FocusTracker()

MAX_FRAME_AGE_SECONDS = float(os.environ.get("FOCUS_MAX_FRAME_AGE", "5"))

# Construction does no I/O; readers start lazily on first route access (or
# eagerly under __main__), so importing this module no longer costs ~9 s of
# warm-up sleeps.
CAMERAS = {
    'cam1': RTSPReader(os.environ.get('FOCUS_CAM1_URL', 'rtsp://192.168.1.101:8555/camera')),
    'cam2': RTSPReader(os.environ.get('FOCUS_CAM2_URL', 'rtsp://192.168.1.102:8555/camera')),
    'cam3': RTSPReader(os.environ.get('FOCUS_CAM3_URL', 'rtsp://192.168.1.103:8555/camera')),
}

# CAMERAS = {
#     'cam1': RTSPReader(0),  # 0 = laptop webcam
# }


def _measure(reader, cam_id=None):
    """Grab the newest frame from `reader` and score it.

    Returns (payload, http_status). reader.start() is idempotent: the first
    call spawns the capture thread and blocks about 3 s for warm-up, later
    calls return at once. Answers 503 with an 'error' key when no frame has
    arrived yet or the newest frame is older than MAX_FRAME_AGE_SECONDS.
    cam_id keys the best-so-far tracker so verdicts are per camera.
    """
    reader.start()
    # retry up to 5 times: right after a lazy start the first frame may not
    # have arrived yet
    frame = None
    for _ in range(5):
        frame = reader.get_frame()
        if frame is not None:
            break
        time.sleep(0.2)

    if frame is None:
        return {'error': 'no frame'}, 503
    age = reader.get_frame_age()
    if age is not None and age > MAX_FRAME_AGE_SECONDS:
        # A frozen RTSP feed keeps serving its last frame forever; a focus
        # verdict on a stale frame is worthless.
        return {'error': 'stale frame', 'frame_age_seconds': round(age, 1)}, 503
    return measure_frame(frame, TRACKER, cam_id), 200


@app.route('/')
def index():
    """Serve the focus page (templates/index.html), which polls /focus/all."""
    return render_template('index.html')


@app.route('/focus/all')
def focus_all():
    """Score every camera in one response for the page's poll loop.

    The status codes from _measure are dropped on purpose: a dead or stale
    camera shows up as {'error': ...} in its own slot so one bad feed does not
    fail the whole poll. Cameras are measured sequentially, so the very first
    call after startup can take about 3 s per camera while readers warm up.
    """
    out = {}
    for cam_id, reader in CAMERAS.items():
        payload, _ = _measure(reader, cam_id)
        out[cam_id] = payload
    return jsonify(out)


@app.route('/focus/reset', methods=['POST'])
def focus_reset():
    """Forget best-so-far (all cameras, or {"camera": id}) after moving the cube."""
    cam_id = (request.get_json(silent=True) or {}).get('camera') or request.args.get('camera')
    return jsonify({'status': 'ok', 'best': TRACKER.reset(cam_id)})


@app.route('/focus/<cam_id>')
def focus_check(cam_id):
    """Score one camera; propagates _measure's 503 so a caller can tell
    "no usable frame" from "in focus but low score"."""
    reader = CAMERAS.get(cam_id)
    if not reader:
        return jsonify({'error': 'invalid cam'}), 404
    payload, code = _measure(reader, cam_id)
    return jsonify(payload), code


if __name__ == '__main__':
    # Eager start when run directly so the first request does not pay the lazy
    # warm-ups; on plain import the readers stay idle until a route needs them.
    for r in CAMERAS.values():
        r.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
