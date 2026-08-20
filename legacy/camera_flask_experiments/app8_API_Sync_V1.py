import os
import platform
import signal
import shutil
import time
import threading
import subprocess
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, render_template, Response, redirect, url_for, send_from_directory, jsonify, request
from flask_cors import CORS

# ==================== Config ====================
CAMERA_SOURCES = {
    "cam1": "rtsp://192.168.2.30:8555/video0_side",
    "cam2": "rtsp://192.168.2.33:8555/video0_front",
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

# FFmpeg RTSP input flags
FFMPEG_RTSP_INPUT = [
    "-rtsp_transport", "tcp",
    "-rtsp_flags", "prefer_tcp",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-fflags", "discardcorrupt",
    "-use_wallclock_as_timestamps", "1",
    "-avoid_negative_ts", "make_zero",
    "-rtbufsize", "64M",
    "-max_delay", "500000",
]

# ==================== Globals ====================
app = Flask(__name__)

# Allow CORS for the specified origin only'
CORS(app, origins=["https://forgeon-dev-609217469146.us-central1.run.app"])

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
recording_start_epoch = None  # UNIX seconds when start_recording_all() triggered
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

def _ffmpeg_has_decoder(name: str) -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-decoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ).stdout
        return name in out
    except Exception:
        return False

def best_encoder_args():
    """
    Prefer NVENC (Linux/Windows), VideoToolbox on macOS, else libx264.
    IMPORTANT: do NOT set '-r' on outputs. CFR is defined by 'fps=' filter or the container.
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
        # Use quality-based VT; '70' is visually good for sports with low latency
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-b:v", "0", "-q:v", "70"] + common_out

    if (sys == "linux" or sys == "windows") and _ffmpeg_has_encoder("h264_nvenc"):
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-tune", "ll",
            "-b:v", "0",
            "-cq", "23",
            "-g", str(int(TARGET_FPS_WRITE)),  # 1s GOP
            "-profile:v", "high",
        ] + common_out

    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"] + common_out

def best_record_decode_args() -> list:
    """
    Prefer GPU MJPEG decode (NVDEC) on NVIDIA to avoid CPU being the bottleneck.
    Safe fallback to CPU decode if not available.
    """
    sys = platform.system().lower()
    if sys in ("linux", "windows"):
        # mjpeg_cuvid exists only if FFmpeg was built with NVDEC/CUVID support
        if _ffmpeg_has_decoder("mjpeg_cuvid"):
            return [
                "-hwaccel", "cuda",
                "-hwaccel_output_format", "cuda",
                "-c:v", "mjpeg_cuvid",
            ]
    return []


def best_record_encoder_args():
    """
    Recording encoder: prioritize NVENC with higher quality and stable realtime behavior.
    IMPORTANT: do not force CFR here; use passthrough timing to avoid frame drop/dup.
    """
    sys = platform.system().lower()

    common_out = [
    "-movflags", "+faststart",
    "-bf", "0",
]

    if sys in ("linux", "windows") and _ffmpeg_has_encoder("h264_nvenc"):
      return [
        "-c:v", "h264_nvenc",
        "-preset", "p5",
        "-tune", "ll",
        "-b:v", "0",
        "-cq", "19",
        "-g", str(int(TARGET_FPS_WRITE)),
        "-profile:v", "high",
        "-pix_fmt", "nv12",   # <-- IMPORTANT: matches hwdownload output
    ] + common_out


    if sys in ("linux", "windows") and _ffmpeg_has_encoder("h264_nvenc"):
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-tune", "ll",
            "-b:v", "0",
            "-cq", "19",                 # higher quality than 23 (less compression)
            "-g", str(int(TARGET_FPS_WRITE)),  # keep 1s GOP (good for seeking)
            "-profile:v", "high",
        ] + common_out

    # CPU fallback
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"] + common_out


# ---------- Timecode & Sync Helpers (from sync_recording.py) ----------
START_REGEX = re.compile(r"start:\s*([0-9]+\.[0-9]+)")
CAMERA_NAME_MAPPING = {
    "cam1": "side",
    "cam2": "front",
    "cam3": "back",
}

def _read_start_time_from_log(log_path: Path) -> float | None:
    if not log_path.exists():
        return None
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = START_REGEX.search(line)
                if m:
                    return float(m.group(1))
    except Exception:
        return None
    return None

def _ffprobe_json(args: list) -> dict:
    proc = subprocess.run(["ffprobe", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {}

def _get_video_meta(video_path: Path):
    data = _ffprobe_json([
        "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=r_frame_rate,avg_frame_rate",
        "-of", "json", str(video_path)
    ])
    try:
        format_data = data.get("format", {})
        dur = float(format_data.get("duration", 0.0))
    except (ValueError, TypeError):
        dur = 0.0

    def parse_rate(rate: str):
        try:
            if not rate:
                return None
            if "/" in rate:
                n, d = map(float, rate.split("/"))
                return n / d if d > 0 else None
            return float(rate)
        except Exception:
            return None

    streams = data.get("streams", [{}])
    s0 = streams[0] if streams else {}
    fps = parse_rate(s0.get("r_frame_rate")) or parse_rate(s0.get("avg_frame_rate")) or TARGET_FPS_WRITE
    return dur, fps

def run_sync_on_dir(recording_dir: Path):
    """Main logic from sync_recording.py integrated here."""
    print(f"🎬 Starting synchronization for: {recording_dir.name}")
    starts = {}
    for cam in CAMERA_SOURCES.keys():
        st = _read_start_time_from_log(recording_dir / f"{cam}.log")
        if st is not None:
            starts[cam] = st

    if len(starts) < len(CAMERA_SOURCES):
        print("⚠️ Could not find start times for all cameras. Sync skipped.")
        return

    max_start = max(starts.values())
    offsets = {cam: max_start - st for cam, st in starts.items()}

    # Determine common duration and fps
    usable_durs = []
    fps_values = []
    for cam in CAMERA_SOURCES.keys():
        vid_file = recording_dir / f"{cam}.mp4"
        if not vid_file.exists():
            continue
        dur, fps = _get_video_meta(vid_file)
        usable_durs.append(max(0.0, dur - offsets[cam]))
        fps_values.append(fps)

    if not usable_durs:
        return

    common_dur = min(usable_durs)
    fps_values.sort()
    common_fps = fps_values[len(fps_values)//2] # median

    sync_dir = recording_dir / "sync"
    sync_dir.mkdir(exist_ok=True)

    for cam in CAMERA_SOURCES.keys():
        suffix = CAMERA_NAME_MAPPING.get(cam, cam)
        out_name = f"{cam}_sync_{suffix}.mp4"
        out_path = sync_dir / out_name

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{offsets[cam]:.6f}",
            "-i", str(recording_dir / f"{cam}.mp4"),
            "-t", f"{common_dur:.6f}",
            "-an", "-sn",
            "-vf", f"fps={common_fps:.6f},setpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-bf", "0", "-movflags", "+faststart",
            str(out_path)
        ]

        print(f"  [sync] Processing {cam}...")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"✅ Sync complete for {recording_dir.name}")

# ==================== Capture (Preview only) ====================
def _open_cv_rtsp(source_url: str):
    cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
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
            time.sleep(0.02)
            continue

        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, current_jpeg_quality()])
        if not ok:
            time.sleep(0.01)
            continue

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Cache-Control: no-cache\r\n\r\n" + buffer.tobytes() + b"\r\n")

        dt = time.perf_counter() - start
        sl = max(0.0, interval - dt)
        if sl > 0:
            time.sleep(sl)

# ==================== Recording (one FFmpeg per camera) ====================
def build_ffmpeg_record_cmd(src_url: str, out_path: Path):
    """
    Force correct 90-fps timing regardless of broken RTSP/RTP timestamps.
    This makes the MP4 playable and prevents the insane 90k-fps metadata.
    """
    fps = int(TARGET_FPS_WRITE)

    dec = best_record_decode_args()
    enc = best_record_encoder_args()
    using_cuvid = any("mjpeg_cuvid" in x for x in dec)

    if using_cuvid:
        # Decode on GPU -> download -> normalize cadence -> generate clean PTS -> log pts_time
        vf = f"settb=AVTB,hwdownload,format=nv12,fps={fps},setpts=N/({fps}*TB),showinfo"
    else:
        vf = f"settb=AVTB,fps={fps},setpts=N/({fps}*TB),showinfo"

    record_input = [
        "-rtsp_transport", "tcp",
        "-rtsp_flags", "prefer_tcp",
        "-fflags", "discardcorrupt",
        "-use_wallclock_as_timestamps", "1",
        "-avoid_negative_ts", "make_zero",
        "-rtbufsize", "256M",
        "-max_delay", "1000000",
        "-thread_queue_size", "8192",
    ]

    return (["ffmpeg", "-y"] + record_input + dec + [
        "-i", src_url,
        "-an", "-sn",
        "-filter:v", vf,
    ] + enc + [
        # optional but nice for MP4 players
        "-video_track_timescale", "90000",
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

# ==================== SNAPSHOT (photos) ====================
def _snap_output_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = current_recording_dir if (is_recording_evt.is_set() and current_recording_dir) else SESSION_DIR
    out = base / "snaps" / f"snap_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out

def _snapshot_via_ffmpeg(cam_key: str, src_url: str, out_path: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    cmd = ["ffmpeg", "-y"] + FFMPEG_RTSP_INPUT + [
        "-i", src_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-an", "-sn",
        str(out_path)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000
    except Exception:
        return False

def _snapshot_from_preview(cam_key: str, out_path: Path) -> bool:
    with frame_locks[cam_key]:
        frame = frames.get(cam_key)
    if frame is None:
        return False
    ok = cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return bool(ok and out_path.exists() and out_path.stat().st_size > 1000)

def capture_photos_all() -> dict:
    out_dir = _snap_output_dir()
    results = []
    for cam_key, src in CAMERA_SOURCES.items():
        fname = f"{cam_key}.jpg"
        out_path = out_dir / fname
        method = "ffmpeg_rtsp"
        ok = _snapshot_via_ffmpeg(cam_key, CAMERA_SOURCES[cam_key], out_path)
        if not ok:
            method = "preview_frame"
            ok = _snapshot_from_preview(cam_key, out_path)
        results.append({
            "camera": cam_key,
            "ok": bool(ok),
            "method": method,
            "path": str(out_path.relative_to(BASE_DIR)) if ok else None
        })
        print(f"[{cam_key}] 📸 snapshot ({method}) → {out_path if ok else 'FAILED'}")
    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_DIR.name,
        "recording": is_recording_evt.is_set(),
        "dir": str(out_dir.relative_to(BASE_DIR)),
        "results": results
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest

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
            try:
                p.kill()
            except Exception:
                pass

    # Close logs
    for cam_key, logf in list(record_logs.items()):
        try:
            logf.flush()
            logf.close()
        except Exception:
            pass
    record_logs.clear()
    record_procs.clear()

    # --- Trigger Sync (integrated from sync_recording.py) ---
    if current_recording_dir and current_recording_dir.exists():
        # Run sync in a background thread to avoid blocking the API response
        threading.Thread(target=run_sync_on_dir, args=(current_recording_dir,), daemon=True).start()

    # Refresh previews to clear latent jitter/buffers
    for k in CAMERA_SOURCES.keys():
        reopen_capture_evts[k].set()

    wall = time.time() - recording_start_epoch if recording_start_epoch else 0.0
    print(f"🧭 Recording finished. Wall time ≈ {wall:.2f}s")

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

# ---- SNAPSHOT routes ----
@app.route("/capture_photos", methods=["POST"])
def capture_photos():
    try:
        capture_photos_all()
        return redirect(url_for("index"))
    except Exception as e:
        return f"Failed to capture photos: {e}", 500

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

# ==================== Recording Control APIs ====================
@app.route("/api/start_recording", methods=["POST"])
def api_start_recording():
    """
    Start recording on the backend for all cameras.
    Returns: {"status": "recording_started", "recording_index": <int>, "timestamp": <ISO>}
    """
    try:
        if is_recording_evt.is_set():
            return jsonify({
                "status": "already_recording",
                "recording_index": recording_index,
                "message": "Recording is already in progress"
            }), 200

        start_recording_all()
        return jsonify({
            "status": "recording_started",
            "recording_index": recording_index,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Backend recording started (recording_{recording_index})"
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route("/api/stop_recording", methods=["POST"])
def api_stop_recording():
    """
    Stop recording on the backend for all cameras.
    Synchronizes video streams and generates sync files.
    Returns: {"status": "recording_stopped", "recording_dir": <str>, "files": {...}}
    """
    try:
        if not is_recording_evt.is_set():
            return jsonify({
                "status": "not_recording",
                "message": "No recording is currently in progress"
            }), 200

        stop_recording_all()

        # Return the files from the current recording dir
        if current_recording_dir and current_recording_dir.exists():
            files = _get_recording_files_info(current_recording_dir)
            return jsonify({
                "status": "recording_stopped",
                "recording_dir": str(current_recording_dir.relative_to(BASE_DIR)),
                "recording_index": recording_index,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "files": files,
                "message": "Backend recording stopped and synchronized"
            }), 200
        else:
            return jsonify({
                "status": "error",
                "message": "Recording directory not found"
            }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route("/api/get_recording_files/<int:rec_index>", methods=["GET"])
def api_get_recording_files(rec_index):
    """
    Get list of synced video files from a specific recording index.
    Returns: {"status": "success", "recording_dir": <str>, "files": {...}}
    """
    try:
        rec_dir = SESSION_DIR / f"recording_{rec_index}"
        if not rec_dir.exists():
            return jsonify({
                "status": "not_found",
                "message": f"Recording directory recording_{rec_index} not found"
            }), 404

        files = _get_recording_files_info(rec_dir)
        return jsonify({
            "status": "success",
            "recording_dir": str(rec_dir.relative_to(BASE_DIR)),
            "recording_index": rec_index,
            "files": files
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route("/api/list_recordings", methods=["GET"])
def api_list_recordings():
    """
    List all available recordings in the session.
    Returns: {"status": "success", "recordings": [{"index": <int>, "dir": <str>, "files": {...}}]}
    """
    try:
        recordings = []
        if SESSION_DIR.exists():
            for item in sorted(SESSION_DIR.iterdir()):
                if item.is_dir() and item.name.startswith("recording_"):
                    try:
                        rec_index = int(item.name.split("_")[1])
                        files = _get_recording_files_info(item)
                        recordings.append({
                            "index": rec_index,
                            "dir": str(item.relative_to(BASE_DIR)),
                            "files": files
                        })
                    except (ValueError, Exception):
                        continue

        return jsonify({
            "status": "success",
            "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
            "total_recordings": len(recordings),
            "recordings": recordings
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

def _get_recording_files_info(rec_dir: Path) -> dict:
    """
    Helper to extract file information from a recording directory.
    Returns dict mapping camera semantic names to file metadata.
    Mapping: cam1→side, cam2→front, cam3→back
    """
    files = {}
    for cam_key, semantic_name in CAMERA_NAME_MAPPING.items():
        # Original File
        orig_file = rec_dir / f"{cam_key}.mp4"
        if orig_file.exists():
            stat = orig_file.stat()
            files[semantic_name] = {
                "filename": orig_file.name,
                "path": str(orig_file.relative_to(BASE_DIR)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "original_video"
            }

        # Synced File (if exists)
        sync_file = rec_dir / "sync" / f"{cam_key}_sync_{semantic_name}.mp4"
        if sync_file.exists():
            stat = sync_file.stat()
            files[f"{semantic_name}_sync"] = {
                "filename": sync_file.name,
                "path": str(sync_file.relative_to(BASE_DIR)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "synced_video"
            }

    return files

@app.route("/download_file/<path:filepath>", methods=["GET"])
def download_file(filepath):
    """
    Download a recorded video file.
    filepath: relative path from BASE_DIR (e.g., "sessions/session_2025-01-09_14-30-00/recording_1/cam1_sync.mp4")
    """
    try:
        file_path = BASE_DIR / filepath

        # Security: ensure the file is within BASE_DIR
        if not str(file_path.resolve()).startswith(str(BASE_DIR.resolve())):
            return jsonify({"status": "error", "message": "Invalid file path"}), 403

        if not file_path.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404

        return send_from_directory(
            str(file_path.parent),
            file_path.name,
            as_attachment=True
        )
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# ==================== Main ====================
if __name__ == "__main__":
    print(f"📂 Session directory: {SESSION_DIR}")
    start_capture_threads()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
