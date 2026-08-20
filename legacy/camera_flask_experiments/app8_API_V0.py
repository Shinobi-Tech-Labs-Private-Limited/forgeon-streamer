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

# ---------- Timecode helpers ----------
def _secs_to_timecode(secs: float, fps: int) -> str:
    """
    Convert seconds → HH:MM:SS:FF for a given integer fps.
    We clamp/normalize so FF never equals fps.
    """
    if secs < 0:
        secs = 0.0
    hh = int(secs // 3600)
    rem = secs - hh * 3600
    mm = int(rem // 60)
    rem -= mm * 60
    ss = int(rem)
    frac = rem - ss
    ff = int(round(frac * fps))
    if ff >= fps:
        ff = 0
        ss += 1
        if ss >= 60:
            ss = 0
            mm += 1
            if mm >= 60:
                mm = 0
                hh += 1
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

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
    Record at CFR=TARGET_FPS_WRITE once via filter and log per-frame wallclock-ish pts_time.
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

def _write_srt_for_range(log_path: Path, srt_out: Path, global_start: float, global_end: float, fps: int):
    """
    Build an SRT whose cue times are relative to the global_start (i.e., common zero).
    One cue per frame, with 1-frame-long duration, carrying "t=<seconds>".
    If logs are sparse, we approximate frame cadence at fps.
    """
    if not log_path.exists():
        return False

    pts_list = []
    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            m = PTS_RE.search(line)
            if m:
                t = float(m.group(1))
                if global_start <= t <= global_end:
                    pts_list.append(t)

    # Fall back: synthesize based on fps if nothing found in range
    if not pts_list:
        frame = 1.0 / float(fps)
        count = int(round((global_end - global_start) / frame))
        pts_list = [global_start + i * frame for i in range(count)]

    # Write SRT with times relative to global_start; each cue = one frame duration
    frame = 1.0 / float(fps)
    def fmt_srt_time(sec: float) -> str:
        # SRT requires ,mmm milliseconds delimiter
        ms = int(round((sec - int(sec)) * 1000))
        base = time.gmtime(int(sec))
        return f"{base.tm_hour:02}:{base.tm_min:02}:{base.tm_sec:02},{ms:03d}"

    with open(srt_out, "w", encoding="utf-8") as srt:
        for idx, t in enumerate(pts_list, start=1):
            rel = t - global_start
            start_sec = rel
            end_sec = rel + frame * 0.999  # almost one frame
            srt.write(f"{idx}\n")
            srt.write(f"{fmt_srt_time(start_sec)} --> {fmt_srt_time(end_sec)}\n")
            srt.write(f"t={t:.6f}s\n\n")
    return True

def _mux_subtitle_into_mp4(video_in: Path, srt_in: Path, video_out: Path):
    """
    Mux SRT as mov_text into MP4; keep video stream untouched (copy),
    but because prior step re-encoded, we can copy safely here.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-i", str(srt_in),
        "-map", "0:v:0", "-map", "1:0",
        "-c:v", "copy",
        "-c:s", "mov_text",
        str(video_out)
    ]
    logp = Path(str(video_out).replace(".mp4", "_muxsubs.log"))
    with open(logp, "w", buffering=1) as lf:
        lf.write("CMD: " + " ".join(cmd) + "\n")
        proc = subprocess.run(cmd, stdout=lf, stderr=lf)
        if proc.returncode != 0 or not video_out.exists() or video_out.stat().st_size < 2000:
            raise RuntimeError(f"subtitle mux failed for {video_in.name}, see {logp}")

def _ffmpeg_trim_to(out_path: Path, src_path: Path, start_sec: float, dur_sec: float, fps: int, timecode: str):
    """
    Trim + stamp shared timecode + normalize video track timescale.
    Reset PTS to zero so all _sync.mp4 start at the same local 0 while
    timecode provides a shared timestamp across files.
    """
    enc = best_encoder_args()

    timescale = "90000"  # common, high-resolution MP4 timescale
    vf = "setpts=PTS-STARTPTS"

    cmd = [
        "ffmpeg","-y",
        "-ss", f"{start_sec:.6f}",
        "-i", str(src_path),
        "-an", "-sn",
        "-t", f"{dur_sec:.6f}",
        "-filter:v", vf,
    ] + enc + [
        "-video_track_timescale", timescale,
        "-timecode", timecode,
        str(out_path)
    ]

    logp = Path(str(out_path).replace(".mp4","_sync.log"))
    with open(logp, "w", buffering=1) as lf:
        lf.write("CMD: " + " ".join(cmd) + "\n")
        proc = subprocess.run(cmd, stdout=lf, stderr=lf)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 2000:
            raise RuntimeError(f"trim failed for {src_path.name}, see {logp}")

def normalize_recording_dir(rec_dir: Path, fps: int = int(TARGET_FPS_WRITE)):
    """
    1) Read first/last pts_time from each .log
    2) Compute global overlap [global_start, global_end]
    3) Trim each MP4 into camX_sync.mp4 with identical start(=0), duration, timescale, and SHARED timecode (00:00:00:00)
    4) Generate SRT per camera over that span and mux it in (as mov_text)
    5) Write sync_info.json for auditability
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
        info.append((cam, mp4, log, t0, t1))

    if len(info) < 2:
        print("⚠️ Not enough streams to align; aborting sync.")
        return

    global_start = max(t0 for _,_,_,t0,_ in info)
    global_end   = min(t1 for _,_,_,_,t1 in info)
    if global_end <= global_start:
        print(f"⚠️ No positive overlap: start={global_start:.6f}, end={global_end:.6f}")
        return

    frame = 1.0 / float(fps)
    # Round duration to nearest frame for perfect CFR alignment
    dur = max(0.0, global_end - global_start)
    dur = round(dur / frame) * frame
    if dur <= 0:
        print("⚠️ Overlap duration rounded to 0; aborting.")
        return

    # Shared timecode (00:00:00:00). If you want real time-of-day, compute an offset here.
    shared_timecode = _secs_to_timecode(0.0, fps)

    sync_manifest = {
        "fps": fps,
        "global_start_pts_time": round(global_start, 6),
        "global_end_pts_time": round(global_end, 6),
        "duration_sec": round(dur, 6),
        "shared_timecode_start": shared_timecode,
        "note": "All *_sync.mp4 start at local PTS=0 and share this timecode. SRT carries per-frame t=pts_time aligned to the same zero."
    }

    print(f"🧭 Overlap window: start={global_start:.6f}s end={global_end:.6f}s dur≈{dur:.6f}s")

    # Trim, generate SRT, and mux subtitles
    for cam, mp4, log, t0, t1 in info:
        rel_start = max(0.0, global_start - t0)  # where to start inside this cam file
        out_sync = rec_dir / f"{cam}_sync.mp4"

        print(f"[{cam}] ✂️ trimming to overlap: start={rel_start:.6f}s, dur={dur:.6f}s → {out_sync.name}")
        try:
            _ffmpeg_trim_to(out_sync, mp4, rel_start, dur, fps=fps, timecode=shared_timecode)
        except Exception as e:
            print(f"[{cam}] ❌ trim failed: {e}")
            continue

        # Build SRT (relative to global_start) and mux into the trimmed file
        srt_tmp = rec_dir / f"{cam}_sync.srt"
        ok = _write_srt_for_range(log, srt_tmp, global_start, global_end, fps)
        if ok:
            muxed = rec_dir / f"{cam}_sync_tc.mp4"
            try:
                _mux_subtitle_into_mp4(out_sync, srt_tmp, muxed)
                # Replace _sync.mp4 with muxed version (so your UI keeps the same name if you prefer)
                # If you want both, comment out the replace lines below.
                out_sync.unlink(missing_ok=True)
                muxed.rename(out_sync)
                srt_tmp.unlink(missing_ok=True)
            except Exception as e:
                print(f"[{cam}] ⚠️ subtitle mux failed: {e}")
        else:
            print(f"[{cam}] ⚠️ could not generate SRT (no pts in range?)")

    # Quick probe
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

    # Write manifest
    (rec_dir / "sync_info.json").write_text(json.dumps(sync_manifest, indent=2))

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

    # Refresh previews to clear latent jitter/buffers
    for k in CAMERA_SOURCES.keys():
        reopen_capture_evts[k].set()

    # === Align to overlap (and embed shared timecode + SRT) ===
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
    # Map camera keys to semantic names
    camera_name_mapping = {
        "cam1": "side",
        "cam2": "front",
        "cam3": "back",
    }

    files = {}
    for cam_key, semantic_name in camera_name_mapping.items():
        # Primary synced file (with timecode + subtitles)
        sync_file = rec_dir / f"{cam_key}_sync.mp4"
        if sync_file.exists():
            stat = sync_file.stat()
            files[semantic_name] = {
                "filename": sync_file.name,
                "path": str(sync_file.relative_to(BASE_DIR)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "synced_video"
            }
        else:
            # Fallback to original file if sync not available
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
