import os
import cv2
import time
import threading
import shutil
import signal
from datetime import datetime
from flask import Flask, render_template, Response, redirect, url_for, send_from_directory
import multiprocessing as mp
import subprocess

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
DRIVE_FOLDER = "CameraRecordings"
CAMERA_IDS = [0, 1, 2]
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FPS_READ = 60.0
FPS_WRITE = 60.0

recording_process = None
start_time = None

# Safe VideoWriter class
class SafeVideoWriter:
    def __init__(self, filename, fourcc, fps, frame_size):
        self.writer = cv2.VideoWriter(filename, fourcc, fps, frame_size)
        self.lock = threading.Lock()

    def write(self, frame):
        with self.lock:
            self.writer.write(frame)

    def release(self):
        with self.lock:
            self.writer.release()

# Camera streaming generator
def gen_frames(cam_id):
    cap = cv2.VideoCapture(cam_id)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    while True:
        success, frame = cap.read()
        if not success:
            break
        ret, buffer = cv2.imencode('.jpg', frame)
        frame = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
    cap.release()

@app.route('/')
def index():
    sessions = []
    for session in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
        session_path = os.path.join(RECORDINGS_DIR, session)
        if os.path.isdir(session_path):
            for recording in os.listdir(session_path):
                sessions.append((session, os.listdir(session_path)))
                break
    return render_template("index2.html", camera_ids=CAMERA_IDS, sessions=sessions, recording=(recording_process is not None), elapsed_time=(int(time.time() - start_time) if start_time else 0))

@app.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    return Response(gen_frames(cam_id), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/start', methods=['POST'])
def start():
    global recording_process, start_time
    if recording_process is None:
        recording_process = mp.Process(target=start_recording)
        recording_process.start()
        start_time = time.time()
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop():
    global recording_process, start_time
    if recording_process:
        recording_process.terminate()
        recording_process.join()
        recording_process = None
        start_time = None
        upload_to_drive()
    return redirect(url_for('index'))

def start_recording():
    session_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_folder = os.path.join(RECORDINGS_DIR, f"session_{session_time}")
    os.makedirs(session_folder, exist_ok=True)

    processes = []
    for idx in CAMERA_IDS:
        cam_folder = os.path.join(session_folder, f"recording_{idx}_{session_time}")
        os.makedirs(cam_folder, exist_ok=True)
        filename = os.path.join(cam_folder, f"camera_{idx}.mp4")
        p = threading.Thread(target=record_camera, args=(idx, filename))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

def record_camera(cam_id, filename):
    cap = cv2.VideoCapture(cam_id)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS_WRITE)

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = SafeVideoWriter(filename, fourcc, FPS_WRITE, (FRAME_WIDTH, FRAME_HEIGHT))

    while True:
        success, frame = cap.read()
        if not success:
            break
        out.write(frame)
        time.sleep(1.0 / FPS_WRITE)

    cap.release()
    out.release()
@app.route('/stop', methods=['POST'])

def stop_recording():
    global recording, writers, start_time
    recording = False

    # Release all writers
    for writer in writers.values():
        writer.release()
    writers = {}
    print("Recording stopped.")

    # Reset recording count for next session
    recording_count = 1
    recordings_path = session_folder  # the session folder from when recording started

    # Upload to Google Drive using rclone (non-blocking)
    try:
        subprocess.Popen([
            'rclone', 'copy', recordings_path, 'gdrive:camerarecordings',
            '--config', 'rclone.conf'
        ])
        print(f"Uploading {recordings_path} to Google Drive using Rclone.")
    except Exception as e:
        print(f"Rclone upload failed: {e}")

    return redirect(url_for('index'))


def upload_to_drive():
    subprocess.call(["rclone", "copy", RECORDINGS_DIR, f"remote:{DRIVE_FOLDER}", "--config", os.path.join(BASE_DIR, "rclone.conf")])

@app.route('/play/<session>/<filename>')
def play_video(session, filename):
    session_path = os.path.join(RECORDINGS_DIR, session)
    for folder in os.listdir(session_path):
        video_path = os.path.join(session_path, folder, filename)
        if os.path.exists(video_path):
            return send_from_directory(os.path.dirname(video_path), os.path.basename(video_path))
    return "Video not found", 404

@app.route('/download/<session>/<filename>')
def download_video(session, filename):
    session_path = os.path.join(RECORDINGS_DIR, session)
    for folder in os.listdir(session_path):
        video_path = os.path.join(session_path, folder, filename)
        if os.path.exists(video_path):
            return send_from_directory(os.path.dirname(video_path), os.path.basename(video_path), as_attachment=True)
    return "Video not found", 404

if __name__ == '__main__':
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    app.run(debug=True, threaded=True)
