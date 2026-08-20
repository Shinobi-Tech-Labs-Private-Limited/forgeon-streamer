import os
import time
import threading
from datetime import datetime
from pathlib import Path
from flask import send_from_directory

import cv2
from flask import Flask, render_template, Response, redirect, url_for, send_from_directory, jsonify

# -------------------- Config --------------------
# Add/modify cameras here. Ready for up to 5 (or more) cameras.
CAMERA_SOURCES = {
    "cam1": 0,  # USB
    # Historical authenticated-camera examples (credentials deliberately removed):
    #"cam2": "rtsp://camera-host-2:554/cam/realmonitor?channel=1&subtype=0",
    #"cam3": "rtsp://camera-host-3:554/cam/realmonitor?channel=1&subtype=0",
    "cam2": 1,   # add later
    "cam3": 2,   # add later
    "cam4": 3,
    "cam5": 4,
}

FRAME_SIZE = (1280, 720)   # uniform stream & recording size
FPS_WRITE = 60.0

# -------------------- Globals --------------------
app = Flask(__name__)

# Each run of app.py is one session
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SESSION_DIR = Path("sessions") / f"session_{SESSION_TIMESTAMP}"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# Sequential counter for recordings in this run
recording_index = 0

# Capture & frame storage
frames = {k: None for k in CAMERA_SOURCES}
frame_locks = {k: threading.Lock() for k in CAMERA_SOURCES}

# Recording state
is_recording = False
recording_threads = {}
recording_start_epoch = None
current_recording_dir = None

# -------------------- Camera threads --------------------
def capture_frames(cam_key, source):
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"[{cam_key}] ❌ Failed to open source: {source}")
        return
    print(f"[{cam_key}] ✅ Capture thread started.")
    while True:
        ret, frame = cap.read()
        if not ret:
            # RTSP/IP cams can drop; short sleep prevents busy loop
            time.sleep(0.1)
            continue
        # Resize to make all windows identical
        frame = cv2.resize(frame, FRAME_SIZE)
        with frame_locks[cam_key]:
            frames[cam_key] = frame

def start_capture_threads():
    for cam_key, src in CAMERA_SOURCES.items():
        t = threading.Thread(target=capture_frames, args=(cam_key, src), daemon=True)
        t.start()

# -------------------- Streaming --------------------
def gen_frames(cam_key):
    while True:
        with frame_locks[cam_key]:
            frame = frames.get(cam_key)
        if frame is None:
            time.sleep(0.05)
            continue
        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")

# -------------------- Recording --------------------
def record_worker(cam_key, out_path):
    global is_recording
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, FPS_WRITE, FRAME_SIZE)
    print(f"[{cam_key}] ▶️ Recording to {out_path}")

    frame_interval = 1.0 / FPS_WRITE
    next_frame_time = time.time()

    while is_recording:
        now = time.time()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)

        with frame_locks[cam_key]:
            frame = frames.get(cam_key)

        if frame is not None:
            writer.write(frame)
        next_frame_time += frame_interval

    writer.release()
    print(f"[{cam_key}] ⏹️ Recording stopped")


def start_recording_all():
    global is_recording, recording_index, recording_threads, recording_start_epoch, current_recording_dir
    if is_recording:
        return

    # Next recording folder name
    recording_index += 1
    current_recording_dir = SESSION_DIR / f"recording_{recording_index}"
    current_recording_dir.mkdir(parents=True, exist_ok=True)

    is_recording = True
    recording_start_epoch = time.time()
    recording_threads = {}

    for cam_key in CAMERA_SOURCES:
        out_path = current_recording_dir / f"{cam_key}.mp4"
        t = threading.Thread(target=record_worker, args=(cam_key, out_path), daemon=True)
        t.start()
        recording_threads[cam_key] = t

def stop_recording_all():
    global is_recording, recording_threads
    if not is_recording:
        return
    is_recording = False
    # join writers
    for t in recording_threads.values():
        t.join()
    recording_threads.clear()

# -------------------- Helpers --------------------
def list_recordings_in_this_session():
    if not SESSION_DIR.exists():
        return []
    return sorted([p.name for p in SESSION_DIR.iterdir() if p.is_dir()], key=lambda x: int(x.split('_')[-1]))

# -------------------- Routes --------------------
@app.route("/")
def index():
    recordings = list_recordings_in_this_session()
    return render_template(
        "index.html",
        session_name=SESSION_DIR.name,
        cameras=list(CAMERA_SOURCES.keys()),
        recordings=recordings,
        recording=is_recording,
        recording_start_epoch=recording_start_epoch,
    )

@app.route("/video_feed/<cam_key>")
def video_feed(cam_key):
    if cam_key not in CAMERA_SOURCES:
        return "Unknown camera", 404
    return Response(gen_frames(cam_key), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/start_recording", methods=["POST"])
def start_recording():
    start_recording_all()
    return redirect(url_for("index"))

@app.route("/stop_recording", methods=["POST"])
def stop_recording():
    stop_recording_all()
    return redirect(url_for("index"))

@app.route("/play/<recording_name>")
def play(recording_name):
    rec_dir = SESSION_DIR / recording_name
    if not rec_dir.exists():
        return "Not found", 404
    # Build per-camera video URLs
    videos = {
        cam: url_for("recording_file", recording_name=recording_name, filename=f"{cam}.mp4")
        for cam in CAMERA_SOURCES
        if (rec_dir / f"{cam}.mp4").exists()
    }
    return render_template("playback.html", recording_name=recording_name, videos=videos, frame_width=FRAME_SIZE[0], frame_height=FRAME_SIZE[1])

@app.route("/file/<recording_name>/<filename>")
def recording_file(recording_name, filename):
    rec_dir = Path("sessions") / SESSION_DIR.name / recording_name
    file_path = rec_dir / filename
    if not file_path.exists():
        return f"File {filename} not found in {rec_dir}", 404
    return send_from_directory(rec_dir, filename)


@app.route("/status")
def status():
    # (Optional) If you want AJAX polling for timer from client
    return jsonify({
        "recording": is_recording,
        "recording_start_epoch": recording_start_epoch
    })

# -------------------- Main --------------------
if __name__ == "__main__":
    print(f"📂 Session directory: {SESSION_DIR}")
    start_capture_threads()
    # Turn off debug to avoid double-threading due to reloader
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
