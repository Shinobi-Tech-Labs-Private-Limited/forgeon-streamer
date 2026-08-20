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
    return render_template('index.html')


@app.route('/focus/all')
def focus_all():
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
    reader = CAMERAS.get(cam_id)
    if not reader:
        return jsonify({'error': 'invalid cam'}), 404
    payload, code = _measure(reader, cam_id)
    return jsonify(payload), code


if __name__ == '__main__':
    for r in CAMERAS.values():
        r.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
