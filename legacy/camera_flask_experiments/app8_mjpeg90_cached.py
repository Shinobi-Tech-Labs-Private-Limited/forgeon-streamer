#!/usr/bin/env python3
"""
app8_mjpeg90_cached.py

Purpose:
- Ingest RTSP cameras (e.g., v4l2rtspserver MJPG @ 90fps).
- Serve browser-friendly MJPEG-over-HTTP endpoints at 90fps:
    /video_feed/cam1
    /video_feed/cam2
    /video_feed/cam3

Key design choice (important for smoothness):
- JPEG is encoded ONCE per frame per camera in the capture thread.
- Each connected client only reuses the latest encoded JPEG bytes (no per-client re-encode),
  so adding a "recording" client does not double CPU load.

Notes:
- "Highest possible" JPEG quality is 100, but it can explode bandwidth and create stutter.
  Default here is 95 (visually near-max, usually safer than 100).
- Your frontend can "record" by consuming the MJPEG stream; the Flask app does NOT record.
"""

import os
import time
import threading
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify
from flask_cors import CORS

# ==================== Config ====================

CAMERA_SOURCES = {
    "cam1": "rtsp://192.168.2.30:8555/video0_side",
    "cam2": "rtsp://192.168.2.33:8555/video0_front",
    "cam3": "rtsp://192.168.2.32:8555/video0_back",
}

# Preview resize (keeps your UI consistent; set None to keep source size)
FRAME_SIZE = (1280, 720)

# MJPEG HTTP output settings
MJPEG_FPS = 90.0

# JPEG quality: 0..100 (OpenCV). 95 is near-max but less risky than 100.
JPEG_QUALITY = 95

# RTSP robustness (OpenCV preview)
RTSP_TIMEOUT_MS = 5000
RTSP_RETRY_BACKOFF = (1, 2, 5)

# CORS: if your frontend uses fetch() (for recording), you need CORS.
# If your frontend only uses <img src="...">, CORS is not required.
ALLOWED_ORIGINS = [
    "https://forgeon-dev-609217469146.us-central1.run.app",
    # add more origins, or set to ["*"] to allow any origin
]

# ==================== App ====================
app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS)

# ==================== Globals ====================
frames = {k: None for k in CAMERA_SOURCES}
frame_locks = {k: threading.Lock() for k in CAMERA_SOURCES}

jpeg_bytes = {k: None for k in CAMERA_SOURCES}
jpeg_locks = {k: threading.Lock() for k in CAMERA_SOURCES}
jpeg_ts = {k: 0.0 for k in CAMERA_SOURCES}

stop_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}
reopen_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}

cv2.setNumThreads(max(1, os.cpu_count() // 2))


# ==================== Capture ====================
def _open_cv_rtsp(source_url: str):
    cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def capture_frames(cam_key: str, source_url: str):
    """
    Reads RTSP frames and stores:
      1) latest raw frame (optional, for debugging)
      2) latest JPEG-encoded bytes (used by all MJPEG clients)
    """
    backoffs = list(RTSP_RETRY_BACKOFF)
    last_frame_wall = 0.0

    while not stop_capture_evts[cam_key].is_set():
        cap = _open_cv_rtsp(source_url)
        if not cap.isOpened():
            print(f"[{cam_key}] ❌ Failed to open RTSP: {source_url}")
            time.sleep(backoffs[0])
            backoffs = backoffs[1:] + [backoffs[-1]]
            continue

        print(f"[{cam_key}] ✅ Capture started.")
        backoffs = list(RTSP_RETRY_BACKOFF)

        while not stop_capture_evts[cam_key].is_set():
            if reopen_capture_evts[cam_key].is_set():
                reopen_capture_evts[cam_key].clear()
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.005)
                if (time.time() - last_frame_wall) * 1000 > RTSP_TIMEOUT_MS:
                    print(f"[{cam_key}] ⚠️ RTSP stalled, reopening…")
                    break
                continue

            last_frame_wall = time.time()

            if FRAME_SIZE and (frame.shape[1], frame.shape[0]) != FRAME_SIZE:
                frame = cv2.resize(frame, FRAME_SIZE, interpolation=cv2.INTER_AREA)

            # Store raw frame (optional)
            with frame_locks[cam_key]:
                frames[cam_key] = frame

            # Encode JPEG ONCE per frame and cache bytes
            ok2, buf = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, int(JPEG_QUALITY)]
            )
            if ok2:
                payload = buf.tobytes()
                with jpeg_locks[cam_key]:
                    jpeg_bytes[cam_key] = payload
                    jpeg_ts[cam_key] = last_frame_wall

        cap.release()
        time.sleep(backoffs[0])
        backoffs = backoffs[1:] + [backoffs[-1]]


def start_capture_threads():
    for cam_key, src in CAMERA_SOURCES.items():
        t = threading.Thread(target=capture_frames, args=(cam_key, src), daemon=True)
        t.start()


# ==================== MJPEG streaming ====================
def gen_mjpeg(cam_key: str):
    """
    Yields multipart/x-mixed-replace MJPEG stream.
    Uses cached JPEG bytes to avoid per-client encoding.
    """
    interval = 1.0 / max(1.0, float(MJPEG_FPS))

    while True:
        start = time.perf_counter()

        with jpeg_locks[cam_key]:
            payload = jpeg_bytes.get(cam_key)

        if payload is None:
            time.sleep(0.01)
            continue

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Cache-Control: no-cache\r\n"
               b"Pragma: no-cache\r\n\r\n" + payload + b"\r\n")

        dt = time.perf_counter() - start
        time.sleep(max(0.0, interval - dt))


# ==================== Routes ====================
@app.route("/health")
def health():
    # Basic health + last JPEG timestamp per cam
    now = time.time()
    info = {}
    for k in CAMERA_SOURCES.keys():
        with jpeg_locks[k]:
            ts = jpeg_ts.get(k, 0.0)
        info[k] = {"last_jpeg_age_ms": round((now - ts) * 1000, 1) if ts else None}
    return jsonify({"ok": True, "mjpeg_fps": MJPEG_FPS, "jpeg_quality": JPEG_QUALITY, "cams": info})


@app.route("/video_feed/<cam_key>")
def video_feed(cam_key: str):
    if cam_key not in CAMERA_SOURCES:
        return "Unknown camera key", 404
    return Response(gen_mjpeg(cam_key), mimetype="multipart/x-mixed-replace; boundary=frame")


# ==================== Main ====================
def main():
    start_capture_threads()
    # For production: run behind gunicorn with threads:
    #   gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 app8_mjpeg90_cached:app
    app.run(host="0.0.0.0", port=5000, threaded=True)


if __name__ == "__main__":
    main()
