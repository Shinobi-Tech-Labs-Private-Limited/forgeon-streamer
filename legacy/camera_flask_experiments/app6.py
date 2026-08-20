# app.py
import os
import platform
import signal
import shutil
import time
import threading
import subprocess
import json
import re
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, render_template, Response, redirect, url_for, send_from_directory, jsonify

# ==================== Config ====================
CAMERA_SOURCES = {
    "cam1": "rtsp://192.168.2.30:8555/video0_side",
    "cam2": "rtsp://192.168.2.31:8555/video0_front",
    "cam3": "rtsp://192.168.2.32:8555/video0_back",
}
FRAME_SIZE = (1280, 720)  # preview resize only (recording keeps source size)

# Preview settings
STREAM_THROTTLE_ON_RECORD = True
NORMAL_PREVIEW_FPS = 25.0
NORMAL_JPEG_QUALITY = 70
RECORDING_PREVIEW_FPS = 10.0
RECORDING_JPEG_QUALITY = 55

# Target CFR for files (set once in filter; do NOT set '-r' on outputs)
TARGET_FPS_WRITE = 90

# RTSP robustness (OpenCV preview)
RTSP_TIMEOUT_MS = 5000
RTSP_RETRY_BACKOFF = (1, 2, 5)

# FFmpeg RTSP input flags (Ubuntu/FFmpeg 6.1-friendly)
FFMPEG_RTSP_INPUT = [
    "-rtsp_transport", "tcp",
    "-rtsp_flags", "prefer_tcp",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-fflags", "discardcorrupt",
    "-use_wallclock_as_timestamps", "1",
    "-avoid_negative_ts", "make_zero",
    "-rtbufsize", "64M",
    "-max_delay", "500000",  # 0.5 s demuxer delay
]

# ==================== Globals ====================
app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SESSION_DIR = BASE_DIR / "sessions" / f"session_{SESSION_TIMESTAMP}"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

frames = {k: None for k in CAMERA_SOURCES}
frame_ts = {k: 0.0 for k in CAMERA_SOURCES}
frame_locks = {k: threading.Lock() for k in CAMERA_SOURCES}
stop_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}
reopen_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}

is_recording_evt = threading.Event()
recording_index = 0
recording_start_epoch = None
current_recording_dir = None

# Per-camera FFmpeg processes + logs
record_procs = {}  # cam_key -> Popen
record_logs = {}   # cam_key -> open file

cv2.setNumThreads(max(1, os.cpu_count() // 2))

# ==================== Helpers ====================
def current_preview_fps():
    return float(RECORDING_PREVIEW_FPS if (STREAM_THROTTLE_ON_RECORD and is_recording_evt.is_set()) else NORMAL_PREVIEW_FPS)

def current_jpeg_quality():
    return int(RECORDING_JPEG_QUALITY if (STREAM_THROTTLE_ON_RECORD and is_recording_evt.is_set()) else NORMAL_JPEG_QUALITY)

def _assert_ffmpeg_available():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH. Install it and try again.")

def _ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ).stdout
        return name in out
    except Exception:
        return False

def best_encoder_args():
    """
    Prefer NVENC (Linux/Windows), VideoToolbox on macOS, else libx264.
    IMPORTANT: we DO NOT set '-r' here. CFR is defined once via the 'fps=' filter.
    Also disable B-frames to keep packet≈frame simple for probes.
    """
    sys = platform.system().lower()

    common_out = [
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-bf", "0",
        "-fps_mode", "cfr",
    ]

    if sys == "darwin" and _ffmpeg_has_encoder("h264_videotoolbox"):
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-b:v", "0", "-q:v", "70"] + common_out

    if (sys == "linux" or sys == "windows") and _ffmpeg_has_encoder("h264_nvenc"):
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-tune", "ll",
            "-b:v", "0",
            "-cq", "23",
            "-g", str(int(TARGET_FPS_WRITE)),  # GOP = 1s @ 120fps
            "-profile:v", "high",
        ] + common_out

    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"] + common_out

# ==================== Capture (Preview only) ====================
def _open_cv_rtsp(source_url: str):
    cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
    try: cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception: pass
    return cap

def capture_frames(cam_key, source_url: str):
    backoffs = list(RTSP_RETRY_BACKOFF)
    last_frame_wall = 0.0

    while not stop_capture_evts[cam_key].is_set():
        cap = _open_cv_rtsp(source_url)
        if not cap.isOpened():
            print(f"[{cam_key}] ❌ Failed to open preview: {source_url}")
            time.sleep(backoffs[0])
            backoffs = backoffs[1:] + [backoffs[-1]]
            continue

        print(f"[{cam_key}] ✅ Preview capture started.")
        backoffs = list(RTSP_RETRY_BACKOFF)

        while not stop_capture_evts[cam_key].is_set():
            if reopen_capture_evts[cam_key].is_set():
                reopen_capture_evts[cam_key].clear()
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.01)
                if (time.time() - last_frame_wall) * 1000 > RTSP_TIMEOUT_MS:
                    print(f"[{cam_key}] ⚠️ Preview stalled, reopening...")
                    break
                continue

            last_frame_wall = time.time()
            if (frame.shape[1], frame.shape[0]) != FRAME_SIZE:
                frame = cv2.resize(frame, FRAME_SIZE, interpolation=cv2.INTER_AREA)

            with frame_locks[cam_key]:
                frames[cam_key] = frame
                frame_ts[cam_key] = time.time()

        cap.release()
        time.sleep(backoffs[0])
        backoffs = backoffs[1:] + [backoffs[-1]]

def start_capture_threads():
    for cam_key, src in CAMERA_SOURCES.items():
        t = threading.Thread(target=capture_frames, args=(cam_key, src), daemon=True)
        t.start()

# ==================== Streaming (MJPEG) ====================
def gen_frames(cam_key):
    while True:
        fps = current_preview_fps()
        if fps <= 0:
            time.sleep(0.1)
            continue
        interval = 1.0 / max(1.0, fps)

        start = time.perf_counter()
        with frame_locks[cam_key]:
            frame = frames.get(cam_key)

        if frame is None:
            time.sleep(0.02); continue

        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, current_jpeg_quality()])
        if not ok:
            time.sleep(0.01); continue

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Cache-Control: no-cache\r\n\r\n" + buffer.tobytes() + b"\r\n")

        dt = time.perf_counter() - start
        sl = max(0.0, interval - dt)
        if sl > 0: time.sleep(sl)

# ==================== Recording (one FFmpeg per camera) ====================
def build_ffmpeg_record_cmd(src_url: str, out_path: Path):
    """
    Record at CFR=TARGET_FPS_WRITE once via filter and log per-frame wallclock pts_time.
    This preserves correct playback speed and gives us absolute timestamps for overlap trim.
    """
    fps = int(TARGET_FPS_WRITE)
    enc = best_encoder_args()

    # settb=AVTB gives a stable timebase for showinfo reporting
    vf = f"settb=AVTB,showinfo,fps={fps},setpts=PTS-STARTPTS"

    return (["ffmpeg", "-y"] + FFMPEG_RTSP_INPUT + [
        "-i", src_url,
        "-an", "-sn",
        "-filter:v", vf,
    ] + enc + [
        str(out_path)
    ])

def start_recording_all():
    global recording_index, recording_start_epoch, current_recording_dir, record_procs, record_logs
    if is_recording_evt.is_set():
        return

    _assert_ffmpeg_available()
    recording_index += 1
    current_recording_dir = SESSION_DIR / f"recording_{recording_index}"
    current_recording_dir.mkdir(parents=True, exist_ok=True)

    is_recording_evt.set()
    recording_start_epoch = time.time()
    record_procs = {}
    record_logs = {}

    for cam_key, src in CAMERA_SOURCES.items():
        out_path = current_recording_dir / f"{cam_key}.mp4"
        log_path = current_recording_dir / f"{cam_key}.log"
        cmd = build_ffmpeg_record_cmd(src, out_path)
        try:
            logf = open(log_path, "w", buffering=1)
            record_logs[cam_key] = logf
            logf.write("CMD: " + " ".join(cmd) + "\n")
            print(f"[{cam_key}] ▶️ FFmpeg recording started → {out_path}")
            print(f"[{cam_key}] 📝 Log: {log_path}")

            p = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=logf,
                cwd=str(BASE_DIR)
            )
            record_procs[cam_key] = p

            time.sleep(0.3)
            if p.poll() is not None:
                print(f"[{cam_key}] ❌ FFmpeg exited immediately with code {p.returncode}. See log: {log_path}")
        except Exception as e:
            print(f"[{cam_key}] ❌ Failed to start FFmpeg: {e}")

# ==================== Overlap-trim sync (post step) ====================
PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")

def _extract_first_last_pts(log_path: Path):
    """
    Parse FFmpeg showinfo lines to get first and last pts_time (float seconds).
    Returns (first_ts, last_ts) or (None, None) if not found.
    """
    if not log_path.exists():
        return None, None
    first = None
    last = None
    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            m = PTS_RE.search(line)
            if not m:
                continue
            t = float(m.group(1))
            if first is None:
                first = t
            last = t
    return first, last

def _ffmpeg_trim_to(out_path: Path, src_path: Path, start_sec: float, dur_sec: float):
    """
    Trim by time relative to file start (file PTS starts at 0 due to setpts).
    Use -ss after -i for accurate decode trimming.
    """
    enc = best_encoder_args()
    cmd = ["ffmpeg","-y","-i",str(src_path),"-an","-sn","-ss",f"{start_sec:.6f}","-t",f"{dur_sec:.6f}"] + enc + [str(out_path)]
    logp = Path(str(out_path).replace(".mp4","_sync.log"))
    with open(logp, "w", buffering=1) as lf:
        lf.write("CMD: " + " ".join(cmd) + "\n")
        proc = subprocess.run(cmd, stdout=lf, stderr=lf)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 2000:
            raise RuntimeError(f"trim failed for {src_path.name}, see {logp}")

def normalize_recording_dir(rec_dir: Path, fps: int = int(TARGET_FPS_WRITE)):
    """
    Align by absolute time:
      - Read first/last absolute pts_time for each cam from its recording log.
      - Compute overlap: [max(firsts), min(lasts)].
      - For each cam, trim its MP4 to that window (relative to its own first).
    Since outputs are CFR=120, all trimmed segments will have identical duration & frame count.
    """
    cams = list(CAMERA_SOURCES.keys())
    info = []
    for cam in cams:
        mp4 = rec_dir / f"{cam}.mp4"
        log = rec_dir / f"{cam}.log"
        if not mp4.exists() or mp4.stat().st_size < 2000:
            print(f"[{cam}] ⚠️ missing/empty file; skipping")
            continue
        t0, t1 = _extract_first_last_pts(log)
        if t0 is None or t1 is None:
            print(f"[{cam}] ⚠️ no showinfo pts_time found in {log.name}; skipping")
            continue
        info.append((cam, mp4, t0, t1))

    if len(info) < 2:
        print("⚠️ Not enough streams to align; aborting sync.")
        return

    # Common overlap in absolute seconds
    global_start = max(t0 for _,_,t0,_ in info)
    global_end   = min(t1 for _,_,_,t1 in info)
    if global_end <= global_start:
        print(f"⚠️ No positive overlap: start={global_start:.6f}, end={global_end:.6f}")
        return

    # Snap duration to frame grid to avoid off-by-one due to float rounding
    frame = 1.0 / float(fps)

    for cam, mp4, t0, t1 in info:
        rel_start = max(0.0, global_start - t0)
        rel_end   = max(0.0, global_end   - t0)
        dur       = max(0.0, rel_end - rel_start)
        dur = round(dur / frame) * frame
        out_sync = rec_dir / f"{cam}_sync.mp4"
        print(f"[{cam}] ✂️ trimming to overlap: start={rel_start:.6f}s, dur={dur:.6f}s → {out_sync.name}")
        try:
            _ffmpeg_trim_to(out_sync, mp4, rel_start, dur)
        except Exception as e:
            print(f"[{cam}] ❌ trim failed: {e}")

    # Quick verify
    print("✅ Overlap trim done. Quick check:")
    for cam in cams:
        p = rec_dir / f"{cam}_sync.mp4"
        if p.exists():
            out = subprocess.run(
                ["ffprobe","-v","error","-select_streams","v:0","-count_packets",
                 "-show_entries","stream=nb_read_packets,avg_frame_rate,duration",
                 "-of","default=nokey=1:noprint_wrappers=1", str(p)],
                stdout=subprocess.PIPE, text=True
            ).stdout.strip().splitlines()
            if out:
                print(f"  {p.name}: " + " | ".join(out))

# ==================== Stop / finalize ====================
def stop_recording_all():
    global record_procs, recording_start_epoch, record_logs, current_recording_dir
    if not is_recording_evt.is_set():
        return

    is_recording_evt.clear()

    # Graceful stop → finalize MP4 moov
    for cam_key, p in list(record_procs.items()):
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    p.terminate()
                else:
                    p.send_signal(signal.SIGINT)
            except Exception as e:
                print(f"[{cam_key}] ⚠️ SIGINT failed: {e}")

    t_end = time.time() + 15.0
    for cam_key, p in list(record_procs.items()):
        if p is None:
            continue
        while time.time() < t_end:
            if p.poll() is not None:
                break
            time.sleep(0.05)
        if p.poll() is None:
            print(f"[{cam_key}] ⛔ Forcing kill (finalize timeout)")
            try: p.kill()
            except Exception: pass

    # Close logs
    for cam_key, logf in list(record_logs.items()):
        try: logf.flush(); logf.close()
        except Exception: pass
    record_logs.clear()
    record_procs.clear()

    # Refresh previews to clear latent jitter/buffers
    for k in CAMERA_SOURCES.keys():
        reopen_capture_evts[k].set()

    # === Align to overlap ===
    try:
        if current_recording_dir and current_recording_dir.exists():
            normalize_recording_dir(current_recording_dir, fps=int(TARGET_FPS_WRITE))
    except Exception as e:
        print(f"⚠️ normalize_recording_dir failed: {e}")

    wall = time.time() - recording_start_epoch if recording_start_epoch else 0.0
    print(f"🧭 Recording wall time ≈ {wall:.2f}s")

# ==================== Routes ====================
@app.route("/")
def index():
    return render_template(
        "index.html",
        session_name=SESSION_DIR.name,
        cameras=list(CAMERA_SOURCES.keys()),
        recording=is_recording_evt.is_set(),
        recording_start_epoch=recording_start_epoch,
    )

@app.route("/video_feed/<cam_key>")
def video_feed(cam_key):
    if cam_key not in CAMERA_SOURCES:
        return "Unknown camera", 404
    return Response(gen_frames(cam_key),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/start_recording", methods=["POST"])
def start_recording():
    try:
        start_recording_all()
        return redirect(url_for("index"))
    except Exception as e:
        return f"Failed to start recording: {e}", 500

@app.route("/stop_recording", methods=["POST"])
def stop_recording():
    try:
        stop_recording_all()
        return redirect(url_for("index"))
    except Exception as e:
        return f"Failed to stop recording: {e}", 500

# Optional: serve files/logs directly
@app.route("/file/<recording_name>/<filename>")
def recording_file(recording_name, filename):
    rec_dir = SESSION_DIR / recording_name
    file_path = rec_dir / filename
    if not file_path.exists():
        return f"File {filename} not found in {rec_dir}", 404
    return send_from_directory(rec_dir, filename)

@app.route("/status")
def status():
    enc_label = "unknown"
    try:
        enc_args = best_encoder_args()
        enc_label = next((enc_args[i+1] for i, a in enumerate(enc_args) if a == "-c:v"), "unknown")
    except Exception:
        pass
    return jsonify({
        "recording": is_recording_evt.is_set(),
        "recording_start_epoch": recording_start_epoch,
        "target_fps": TARGET_FPS_WRITE,
        "preview_fps": current_preview_fps(),
        "jpeg_quality": current_jpeg_quality(),
        "ffmpeg_in_path": bool(shutil.which("ffmpeg")),
        "encoder_label": enc_label
    })

# ==================== Main ====================
if __name__ == "__main__":
    print(f"📂 Session directory: {SESSION_DIR}")
    start_capture_threads()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
