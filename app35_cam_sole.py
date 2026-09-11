"""Forgeon rig streamer - production entry point.

One Flask process (port 5000) runs a multimodal sports-assessment recording
rig. Everything an operator does from the browser UI lands here.

Hardware / subsystems
  * 3 Raspberry Pi cameras (cam1 = side, cam2 = front, cam3 = back) serving
    MJPEG over RTSP via v4l2rtspserver. This app SSHes into each Pi to
    (re)start that server and keep it healthy (camera bootstrap), keeps a
    low-latency preview loop per camera for the UI, and records every stream
    with one FFmpeg process per camera at TARGET_FPS_WRITE.
  * 2 BLE pressure insoles (Left / Right) streaming 8-channel ADC samples at
    up to 200 Hz - InsoleDevice / BleCoordinator (bleak on a private asyncio
    loop thread).
  * A BLE heart-rate strap, handled by a sidecar process (heartbeat_manager).
  * An INMP441 I2S microphone on one of the camera Pis (mic_capture_manager,
    which runs remote_inmp441_capture.py on the Pi over SSH).
  * A lens focus stepper on each Pi (lens_move, driven over SSH + lgpio) and
    a focus scorer (codesharpnessmeasure) that grades the ChArUco cube.
  * A direct-upload worker (upload_worker.py) that pushes finished takes to
    the Forgeon cloud. It only exists once the rig is paired (/pair), and
    pairing is a hard gate on recording (rig_paired()).

Life of a take (start_combined / stop_combined)
  start: check sport + calibration -> arm insole stream and wait for packets
         -> start mic + HR snippet -> open insole WAL/log -> spawn FFmpeg
         recorders -> is_recording_evt set
  stop:  SIGINT the recorders (sensors keep running so they bracket the
         video end) -> stop insoles / HR / mic -> run_sync_on_dir (trim every
         camera to the common wall-clock window and window the sensor logs
         to it) -> postprocess_recording_for_upload (undistort cam1 when the
         sport needs it) -> validate_recording (validation_report.json)

On-disk layout (BASE_DIR/sessions/)
  session_<ts>/
    logs/<subsystem>.log         rig logs (SessionLogHandler, per subsystem)
    camera_bootstrap/<cam>.log   SSH bootstrap transcripts
    calibration/                 calibration_cam1.json/.npz, detections, previews
    snaps/snap_<ts>/<cam>.jpg    calibration / snapshot captures + manifest
    recording_N/
      <cam>.mp4 | <cam>.mkv      raw FFmpeg output: H.264 mp4 in encode mode,
      <cam>.log                  MJPEG stream-copy mkv in copy mode (deleted
                                 after a validated sync unless RIG_KEEP_RAW);
                                 the log carries the wall-clock "start:" used
                                 for synchronisation
      ble/                       insole WAL (.bin), assignment map, decoded JSON
      heartbeat/, audio/         HR JSONL and mic WAV
      sync/                      <cam>_sync_<view>.mp4, ble_sync.json,
                                 heart_rate_sync.jsonl, sync_manifest.json,
                                 cam1_xmap.pgm / cam1_ymap.pgm (ffmpeg remap)
      distorted/                 original cam1 kept aside after undistortion
                                 (opencv undistort backend only)
      processing_status.json, validation_report.json,
      pipeline_timing.json     per-stage stop timings + run config
                               (_run_stop_pipeline; docs/test-matrix.md)

Threading model
  Flask runs threaded. Long-lived daemon threads: one preview capture loop
  per camera (capture_frames) and one SSH bootstrap monitor per camera
  (_monitor_camera_backend). BLE work runs on the BleCoordinator's own asyncio
  loop thread; Flask handlers hop onto it with BleCoordinator._run(). Shared
  state is module globals guarded by session_state_lock (recording / session
  transitions), camera_backend_state_lock (bootstrap status) and the
  per-camera frame_locks. start_new_session() REASSIGNS the SESSION_* /
  CALIBRATION_* globals, so code that needs the current session must read
  them at call time - never capture them at import time.

Environment knobs
  RIG_API_TOKEN, RIG_SSH_STRICT / RIG_SSH_KNOWN_HOSTS, PI_SSH_USER,
  CAMERA_BOOTSTRAP_ENABLED, FORCE_CUDA_RECORD / REQUIRE_CUDA_RECORD /
  DISABLE_CUDA_RECORD,
  APP_USE_CASE (default sport), MIC_CAMERA_KEY, RIG_LOG_LEVEL /
  RIG_BLE_LOG_LEVEL, FORGEON_API_URL / FORGEON_DEVICE_TOKEN (debug override
  for the pairing file), RIG_CAPTURE_RES (camera WxH, default 1280x720),
  RIG_RECORD_MODE copy|encode, RIG_UNDISTORT_BACKEND ffmpeg|opencv,
  RIG_REMAP_OVERSAMPLE 1..3, RIG_KEEP_RAW (see the block after
  REQUIRE_CUDA_RECORD).

Route map (JSON unless noted)
  UI pages     /  /pair  /calibration  /recording                    (HTML)
  Preview      /video_feed/<cam> (MJPEG)  /focus/<cam>  /focus/all
               /focus/reset  /lens/<cam>/move  /lens/<cam>/reset  /lens/status
  Take/session /api/start_recording  /api/stop_recording  /api/new_session
               /api/list_recordings  /api/get_recording_files/<n>
               /api/validate_recording/<n>  /status  /api/camera/status
               (+ form-post twins /start_recording /stop_recording
                /new_session /capture_photos /select_sport for the legacy UI)
  Calibration  /api/calibration/status|capture|run|upload_json  /api/snapshots
  Sensors      /api/ble/*  (insoles)   /api/heartbeat/*   /api/mic/*
  Cloud        /api/pairing/status|claim  /api/upload_instance|queue|retry
  Files        /media/<path> (inline)   /download_file/<path> (attachment)
"""
import asyncio
import argparse
import atexit
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import struct
import subprocess
import threading
import traceback
import time
import wave
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from threading import Lock

import cv2
import numpy as np
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from heartbeat_manager import HeartbeatManager
from intrinsic_calibrate_charuco import CalibrationCandidate, calibrate as calibrate_charuco, make_maps
from mic_capture_manager import MicCaptureManager

# Focus check: codesharpnessmeasure (deployed beside this app; cube-aware
# version >= 2026-08-17 with the calibration/ package next to it) scores the
# ChArUco calibration CUBE region of the latest preview frame. A per-camera
# best-so-far (FocusTracker) grades PEAK / NEAR / LOW so a solo operator can
# turn (or motor) the lens, overshoot, and walk back to the peak.
try:
    from codesharpnessmeasure.focus.scorer import FocusTracker, measure_frame

    FOCUS_MEASURE_AVAILABLE = True
    FOCUS_MEASURE_ERROR = None
    FOCUS_TRACKER = FocusTracker()
except Exception as exc:
    measure_frame = None
    FOCUS_TRACKER = None
    FOCUS_MEASURE_AVAILABLE = False
    FOCUS_MEASURE_ERROR = str(exc)

# ==================== Camera Config ====================
# Single source of truth for camera network topology. Every IP previously
# appeared twice (CAMERA_SOURCES + CAMERA_BOOTSTRAP["host"]) with a third
# implicit coupling to the v4l2rtspserver -u argument; hand-syncing those is
# how rigs break when re-homed. Note the non-monotonic addressing is real:
# cam2 = .33, cam3 = .32.
CAMERA_HOSTS = {
    "cam1": "192.168.2.30",
    "cam2": "192.168.2.33",
    "cam3": "192.168.2.32",
}
CAMERA_STREAM_ROLES = {"cam1": "side", "cam2": "front", "cam3": "back"}
CAMERA_SOURCES = {
    cam: f"rtsp://{CAMERA_HOSTS[cam]}:8555/video0_{CAMERA_STREAM_ROLES[cam]}"
    for cam in CAMERA_HOSTS
}
CAMERA_SSH_USER = os.environ.get("PI_SSH_USER", "pi").strip() or "pi"

# Host-key policy for SSH/scp to the camera Pis. The historical behavior
# (StrictHostKeyChecking=no) trusts whoever answers at the IP — a swapped,
# reflashed, or spoofed device connects silently. Default keeps that behavior
# so a stock deploy is unaffected; once each Pi's host key is baked into
# RIG_SSH_KNOWN_HOSTS (copy /etc/ssh/ssh_host_ed25519_key.pub at imaging time
# over a trusted link), set RIG_SSH_STRICT=1 to pin identities. BatchMode makes
# a missing/changed key fail fast instead of prompting into piped stdin and
# hanging until the subprocess timeout.
RIG_SSH_STRICT = os.environ.get("RIG_SSH_STRICT", "").strip().lower() in ("1", "true", "yes", "on")
RIG_SSH_KNOWN_HOSTS = os.environ.get("RIG_SSH_KNOWN_HOSTS", "/etc/forgeon/known_hosts").strip()
if RIG_SSH_STRICT:
    CAMERA_SSH_OPTS = [
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={RIG_SSH_KNOWN_HOSTS}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
    ]
else:
    CAMERA_SSH_OPTS = ["-o", "StrictHostKeyChecking=no"]
CAMERA_BOOTSTRAP_ENABLED = os.environ.get("CAMERA_BOOTSTRAP_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CAMERA_BOOTSTRAP_BACKOFF = (2, 5, 10)
CAMERA_BOOTSTRAP_HEALTHCHECK_SEC = 5.0


def _parse_capture_res(raw: str, default: tuple[int, int] = (1280, 720)) -> tuple[int, int]:
    """Parse RIG_CAPTURE_RES ("WxH") into (width, height); fall back to default.

    The value is handed to v4l2rtspserver -W/-H on every camera Pi, so it must
    be an MJPEG mode the sensor actually advertises (check with
    `v4l2-ctl --list-formats-ext` on the Pi). Both sides are forced even so
    the H.264 encoders and the undistortion maps get 4:2:0-friendly frames.
    """
    m = re.fullmatch(r"\s*(\d{3,4})\s*[xX]\s*(\d{3,4})\s*", raw or "")
    if not m:
        return default
    w, h = int(m.group(1)), int(m.group(2))
    if w < 320 or h < 240:
        return default
    return (w - w % 2, h - h % 2)


# test/low-res: capture resolution requested from every camera Pi. Default is
# the as-built 1280x720. Lowering it shrinks MJPEG decode work during the take,
# every post-stop re-encode pass, and the uploaded files. Calibration JSONs
# captured at 1280x720 are rescaled on the fly by _candidate_from_calibration()
# as long as the aspect ratio matches. See tools/lowres_bench.py for numbers.
CAPTURE_RES = _parse_capture_res(os.environ.get("RIG_CAPTURE_RES", ""))
CAPTURE_WIDTH, CAPTURE_HEIGHT = CAPTURE_RES
CAMERA_BOOTSTRAP = {
    "cam1": {
        "name": "side camera",
        "host": CAMERA_HOSTS["cam1"],
        "stream_path": f"video0_{CAMERA_STREAM_ROLES['cam1']}",
        "command": [
            "v4l2rtspserver",
            "-v",
            "-Q",
            "400",
            "-t",
            "120",
            "-P",
            "8555",
            "-u",
            CAMERA_STREAM_ROLES["cam1"],
            "-f",
            "MJPG",
            "-W",
            str(CAPTURE_WIDTH),
            "-H",
            str(CAPTURE_HEIGHT),
            "-F",
            "90",
            "-s",
            "/dev/video0",
        ],
    },
    "cam2": {
        "name": "front camera",
        "host": CAMERA_HOSTS["cam2"],
        "stream_path": f"video0_{CAMERA_STREAM_ROLES['cam2']}",
        "command": [
            "v4l2rtspserver",
            "-v",
            "-Q",
            "400",
            "-t",
            "120",
            "-P",
            "8555",
            "-u",
            CAMERA_STREAM_ROLES["cam2"],
            "-f",
            "MJPG",
            "-W",
            str(CAPTURE_WIDTH),
            "-H",
            str(CAPTURE_HEIGHT),
            "-F",
            "90",
            # NOTE: cam1/cam3 pass "-s /dev/video0" but cam2 passes a bare
            # positional /dev/video0 — a live inconsistency on the deployed
            # rig. Confirm the intended variant against the actual Pi before
            # unifying; do not blindly copy one over the other.
            "/dev/video0",
        ],
    },
    "cam3": {
        "name": "back camera",
        "host": CAMERA_HOSTS["cam3"],
        "stream_path": f"video0_{CAMERA_STREAM_ROLES['cam3']}",
        "command": [
            "v4l2rtspserver",
            "-v",
            "-Q",
            "400",
            "-t",
            "120",
            "-P",
            "8555",
            "-u",
            CAMERA_STREAM_ROLES["cam3"],
            "-f",
            "MJPG",
            "-W",
            str(CAPTURE_WIDTH),
            "-H",
            str(CAPTURE_HEIGHT),
            "-F",
            "90",
            "-s",
            "/dev/video0",
        ],
    },
}

# ---- Lens focus motor (stepper on each camera Pi, driven via lgpio over SSH)
# BCM pins on the camera Pi. Chosen to avoid the INMP441 I2S lines.
LENS_MOTOR_DEFAULT_PINS = {"step": 5, "dir": 6, "en": 13}

# Per-camera overrides, e.g. {"cam2": {"step": 12, "dir": 16, "en": 26}}
LENS_MOTOR_PIN_OVERRIDES: dict[str, dict] = {}

LENS_STEP_COARSE = 50           # microsteps per coarse press
LENS_STEP_FINE = 10             # microsteps per fine press
LENS_STEP_DELAY = 0.0008        # seconds per half-pulse
LENS_TRAVEL_LIMIT = 4000        # max microsteps from zero, either direction
LENS_SSH_TIMEOUT = 25.0

lens_position = {k: 0 for k in CAMERA_SOURCES}
lens_locks = {k: threading.Lock() for k in CAMERA_SOURCES}
lens_last_error = {k: None for k in CAMERA_SOURCES}

# ---- Preview + recording tunables
# The preview loop resizes to FRAME_SIZE and is throttled (fps / JPEG quality)
# while a take is running so the MJPEG generators leave CPU for the encoders.
# TARGET_FPS_WRITE is the constant frame rate forced on every recording.
# Never upscale the preview past what the camera sends (test/low-res).
FRAME_SIZE = (min(1280, CAPTURE_WIDTH), min(720, CAPTURE_HEIGHT))  # preview resize only

STREAM_THROTTLE_ON_RECORD = True
NORMAL_PREVIEW_FPS = 25.0
NORMAL_JPEG_QUALITY = 70
RECORDING_PREVIEW_FPS = 10.0
RECORDING_JPEG_QUALITY = 55
TARGET_FPS_WRITE = 90
FORCE_CUDA_RECORD = os.environ.get("FORCE_CUDA_RECORD", "").strip().lower() in ("1", "true", "yes", "on")
REQUIRE_CUDA_RECORD = os.environ.get("REQUIRE_CUDA_RECORD", "").strip().lower() in ("1", "true", "yes", "on")
# DISABLE_CUDA_RECORD=1 makes the CUDA probe answer "unusable" so every stage
# takes the CPU path (software MJPEG decode, libx264) even on a rig whose GPU
# works. It wins over FORCE_CUDA_RECORD; with REQUIRE_CUDA_RECORD it refuses
# to record, which is the honest answer to a contradictory environment.
DISABLE_CUDA_RECORD = os.environ.get("DISABLE_CUDA_RECORD", "").strip().lower() in ("1", "true", "yes", "on")

# test/one-encode: how the take is recorded and how cam1 is undistorted.
#   RIG_RECORD_MODE=copy    (default) the recorder stream-copies the camera's
#                           MJPEG into <cam>.mkv: no decode and no encode during
#                           the take. The single H.264 encode happens in
#                           run_sync_on_dir(). Every MJPEG frame is a keyframe,
#                           so the sync trim stays frame-exact.
#   RIG_RECORD_MODE=encode  pre-branch behaviour: live MJPEG->H.264 into
#                           <cam>.mp4, then a second encode at sync.
#   RIG_UNDISTORT_BACKEND=ffmpeg (default) cam1 undistortion runs inside the
#                           sync ffmpeg pass (remap filter, maps written from
#                           the calibration JSON). opencv = the per-frame
#                           cv2.remap loop plus a third encode in
#                           postprocess_recording_for_upload().
#   RIG_REMAP_OVERSAMPLE    1..3 (default 2). ffmpeg's remap is nearest-
#                           neighbour; sampling from a 2x upscaled frame gets
#                           within about 1 dB of cv2's bilinear for ~5% more
#                           time. 1 = plain nearest-neighbour.
#   RIG_KEEP_RAW=1          keep <cam>.mkv after a validated sync. Default
#                           deletes them: MJPEG is ~0.7 GB/min/camera at 720p.
RECORD_MODE = os.environ.get("RIG_RECORD_MODE", "copy").strip().lower()
if RECORD_MODE not in ("copy", "encode"):
    RECORD_MODE = "copy"
UNDISTORT_BACKEND = os.environ.get("RIG_UNDISTORT_BACKEND", "ffmpeg").strip().lower()
if UNDISTORT_BACKEND not in ("ffmpeg", "opencv"):
    UNDISTORT_BACKEND = "ffmpeg"
try:
    REMAP_OVERSAMPLE = max(1, min(3, int(os.environ.get("RIG_REMAP_OVERSAMPLE", "2"))))
except ValueError:
    REMAP_OVERSAMPLE = 2
KEEP_RAW = os.environ.get("RIG_KEEP_RAW", "").strip().lower() in ("1", "true", "yes", "on")
RAW_COPY_SUFFIX = ".mkv"
# CPU levers for the sync encode (docs/test-matrix.md, runs R10+). All default
# to the pre-existing behaviour so the knobs only change a run that sets them.
#   RIG_SYNC_PRESET    libx264 preset for the sync encode (default veryfast).
#                      superfast / ultrafast trade file size and a little
#                      quality for roughly 1.3x / 2x encode speed.
#   RIG_SYNC_THREADS   -threads per sync ffmpeg (default 0 = x264 auto, which
#                      opens ~1.5x the logical CPUs per process; with three
#                      encodes in parallel that oversubscribes a small CPU).
#   RIG_OUTPUT_RES     WxH the synced files are scaled to inside the sync pass
#                      (after remap for cam1). Capture stays at RIG_CAPTURE_RES,
#                      so the calibration is untouched; this only shrinks the
#                      encode and the upload. Empty = no scaling.
#   RIG_PAUSE_PREVIEW_ON_STOP=1 (default) stops the three preview decoders
#                      while the stop pipeline runs so they do not compete with
#                      the encodes for CPU; the UI shows the last frame until
#                      the take is ready. 0 = keep the preview live.
_X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")
SYNC_PRESET = os.environ.get("RIG_SYNC_PRESET", "veryfast").strip().lower()
if SYNC_PRESET not in _X264_PRESETS:
    SYNC_PRESET = "veryfast"
try:
    SYNC_THREADS = max(0, int(os.environ.get("RIG_SYNC_THREADS", "0")))
except ValueError:
    SYNC_THREADS = 0
OUTPUT_RES = os.environ.get("RIG_OUTPUT_RES", "").strip().lower()
if OUTPUT_RES:
    try:
        _ow, _oh = (int(v) for v in OUTPUT_RES.split("x"))
        OUTPUT_RES = f"{_ow}x{_oh}" if _ow > 0 and _oh > 0 else ""
    except ValueError:
        OUTPUT_RES = ""
PAUSE_PREVIEW_ON_STOP = os.environ.get("RIG_PAUSE_PREVIEW_ON_STOP", "1").strip().lower() in ("1", "true", "yes", "on")
#   RIG_SYNC_DECODER   auto (default) decodes the raw MJPEG with mjpeg_cuvid in
#                      the sync pass when CUDA is usable (frames come back to
#                      system memory so fps/remap/scale run unchanged and NVENC
#                      takes them from there); software = ffmpeg's CPU decoder.
#                      Rig run R9 showed the NVENC sync capped at ~1.0x by the
#                      software MJPEG decode. Copy-mode raws only.
SYNC_DECODER = os.environ.get("RIG_SYNC_DECODER", "auto").strip().lower()
if SYNC_DECODER not in ("auto", "software"):
    SYNC_DECODER = "auto"


def _raw_video_path(recording_dir: Path, cam: str) -> Path | None:
    """The raw recording for `cam`: <cam>.mp4 (encode mode) or <cam>.mkv (copy
    mode), whichever exists; the current mode's extension wins if both do.
    """
    order = (RAW_COPY_SUFFIX, ".mp4") if RECORD_MODE == "copy" else (".mp4", RAW_COPY_SUFFIX)
    for suffix in order:
        candidate = recording_dir / f"{cam}{suffix}"
        if candidate.exists():
            return candidate
    return None

# Preview capture: give up on a stalled stream after RTSP_TIMEOUT_MS and
# reconnect with the (cyclic) RTSP_RETRY_BACKOFF delays.
RTSP_TIMEOUT_MS = 5000
RTSP_RETRY_BACKOFF = (1, 2, 5)

# Low-latency RTSP input flags shared by snapshot grabs. Recording uses its
# own, larger-buffered variant in build_ffmpeg_record_cmd().
FFMPEG_RTSP_INPUT = [
    "-rtsp_transport",
    "tcp",
    "-rtsp_flags",
    "prefer_tcp",
    "-fflags",
    "nobuffer",
    "-flags",
    "low_delay",
    "-fflags",
    "discardcorrupt",
    "-use_wallclock_as_timestamps",
    "1",
    "-avoid_negative_ts",
    "make_zero",
    "-rtbufsize",
    "64M",
    "-max_delay",
    "500000",
]


# ==================== BLE Config (from app13) ====================
# Insole GATT layout. ADC_CHAR notifies batches of SINGLE_SAMPLE_SIZE-byte
# packets (see InsoleDevice._handle_adc_data for the byte layout); CMD_CHAR
# takes the one-byte commands below; STATUS_CHAR notifies battery/charging/
# streaming state. The 0x2Axx UUIDs are the standard Device Information
# Service characteristics.
UUID_ADC_CHAR = "aa0a4d54-2b51-42f9-bbca-3b9304fbed92"
UUID_CMD_CHAR = "7d4a93e2-1b7e-41c5-a2ed-8f0cf19e68e3"
UUID_STATUS_CHAR = "7d4a93e2-1b22-4a61-95b4-564f0a2c7703"

UUID_MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"
UUID_MANUFACTURER_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
UUID_HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"

CMD_LED_TOGGLE = b"\x01"
CMD_FREQ_10HZ = b"\x0A"
CMD_FREQ_100HZ = b"\x0B"
CMD_FREQ_200HZ = b"\x0C"
FREQ_MAP = {"10Hz": CMD_FREQ_10HZ, "100Hz": CMD_FREQ_100HZ, "200Hz": CMD_FREQ_200HZ}
SINGLE_SAMPLE_SIZE = 20
SAMPLES_PER_NOTIFY = 12

# ==================== App Globals ====================
# The single production template lives in templates/active/ (see CLAUDE.md).
app = Flask(__name__, template_folder="templates/active")
CORS(
    app,
    origins=[
        "https://forgeon-dev-609217469146.us-central1.run.app",
        "https://test.forgelabs.in",
        "https://dev.forgelabs.in",
        # The admin Calibration tab (local Next.js dev) talks to this app
        # directly for camera snapshots - see /api/snapshots.
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
)

# Optional shared-token gate over the control/data surface. Every route is
# otherwise anonymous on 0.0.0.0:5000 — anyone on the venue LAN can stop a
# take mid-run, download every athlete's files, watch live previews, or drive
# the mic SSH path. Default (unset) keeps today's open behavior so the legacy
# operator UI is unaffected; set RIG_API_TOKEN on the rig and have clients send
# "Authorization: Bearer <token>", "X-Rig-Token: <token>", or "?token=<token>"
# (the query form exists so <img>/<video> tags can still reach /video_feed and
# /media). CORS is not access control — curl on the LAN ignores it.
RIG_API_TOKEN = os.environ.get("RIG_API_TOKEN", "").strip()
_TOKEN_PROTECTED_PREFIXES = (
    "/api/",
    "/download_file/",
    "/media/",
    "/video_feed/",
    "/status",
    "/focus/",
)


@app.before_request
def _require_rig_token():
    """Optional bearer-token gate (see RIG_API_TOKEN above). No-op when unset."""
    if not RIG_API_TOKEN:
        return None
    if not request.path.startswith(_TOKEN_PROTECTED_PREFIXES):
        return None
    supplied = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        supplied = auth_header[7:].strip()
    supplied = supplied or request.headers.get("X-Rig-Token", "").strip() or request.args.get("token", "")
    if supplied != RIG_API_TOKEN:
        return jsonify({"status": "error", "message": "Missing or invalid rig token"}), 401
    return None


# Session directory: one folder per app start (or per /api/new_session), every
# artifact of the rig lands beneath it. These four names are reassigned by
# start_new_session() - always read them at call time.
BASE_DIR = Path(__file__).resolve().parent
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SESSION_DIR = BASE_DIR / "sessions" / f"session_{SESSION_TIMESTAMP}"
SESSION_DIR.mkdir(parents=True, exist_ok=True)


# ==================== Rig Logging ====================
# Per-subsystem log files inside the CURRENT session directory. The handler
# resolves its target path on every emit because start_new_session() reassigns
# the SESSION_DIR module global — a FileHandler bound at import would write to
# the first session forever. File-append only: two BLE call sites emit from the
# asyncio loop thread, where blocking/network handlers are not safe. Never log
# RIG_API_TOKEN or other secrets.
class SessionLogHandler(logging.Handler):
    def __init__(self, path_provider):
        super().__init__()
        self._path_provider = path_provider
        self._open_path = None
        self._stream = None

    def emit(self, record):
        # Handler.handle() already serializes emit() under the handler lock.
        try:
            path = self._path_provider()
            if path != self._open_path:
                if self._stream is not None:
                    self._stream.close()
                path.parent.mkdir(parents=True, exist_ok=True)
                self._stream = open(path, "a", encoding="utf-8", buffering=1)
                self._open_path = path
            self._stream.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)

    def close(self):
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._stream = None
            self._open_path = None
            super().close()


RIG_LOG_LEVEL = os.environ.get("RIG_LOG_LEVEL", "INFO").strip().upper()
RIG_BLE_LOG_LEVEL = os.environ.get("RIG_BLE_LOG_LEVEL", "WARNING").strip().upper()
_RIG_LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_RIG_LOG_SUBSYSTEMS = ("app", "sync", "ble", "mic", "heartbeat", "bootstrap", "upload")


def _session_log_path(subsystem):
    """Path provider for SessionLogHandler - resolved on every emit so it follows
    SESSION_DIR when a new session starts.
    """
    return lambda: SESSION_DIR / "logs" / f"{subsystem}.log"


def _init_rig_logging():
    """Wire the 'rig' logger tree once: console handler on the root plus one
    session-scoped file per subsystem in _RIG_LOG_SUBSYSTEMS (rig.app,
    rig.sync, rig.ble, ...). Safe to call twice.
    """
    root = logging.getLogger("rig")
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setFormatter(_RIG_LOG_FORMAT)
    console.setLevel(getattr(logging, RIG_LOG_LEVEL, logging.INFO))
    root.addHandler(console)
    for subsystem in _RIG_LOG_SUBSYSTEMS:
        logger = logging.getLogger(f"rig.{subsystem}")
        handler = SessionLogHandler(_session_log_path(subsystem))
        handler.setFormatter(_RIG_LOG_FORMAT)
        logger.addHandler(handler)
        if subsystem == "ble":
            # 200 Hz notify callbacks live behind this logger — WARNING keeps
            # per-packet noise out unless an operator opts in.
            logger.setLevel(getattr(logging, RIG_BLE_LOG_LEVEL, logging.WARNING))


_init_rig_logging()
rig_log = logging.getLogger("rig.app")
sync_log = logging.getLogger("rig.sync")
# Named ble_logger (not ble_log): stop_combined uses ble_log as a local for
# the BLE stop-result dict.
ble_logger = logging.getLogger("rig.ble")
bootstrap_log = logging.getLogger("rig.bootstrap")


def log_exception(logger, message):
    """Log message at ERROR with the current exception's traceback attached."""
    logger.error(message, exc_info=True)


@app.errorhandler(Exception)
def _unhandled_error(exc):
    """Last-resort Flask error handler: HTTP errors pass through untouched,
    anything else is logged with a traceback and answered as JSON 500.
    """
    if isinstance(exc, HTTPException):
        return exc
    log_exception(rig_log, f"Unhandled error in {request.path}")
    return jsonify({"status": "error", "message": str(exc)}), 500
# ---- Calibration (side camera only). Intrinsics are solved from ChArUco
# snapshots of cam1; the JSON is what postprocess_recording_for_upload()
# uses to undistort cam1 for wide-angle sports. Reassigned per session.
CALIBRATION_DIR = SESSION_DIR / "calibration"
CALIBRATION_JSON = CALIBRATION_DIR / "calibration_cam1.json"
CALIBRATION_NPZ = CALIBRATION_DIR / "calibration_cam1.npz"
CALIBRATION_CAMERA = "cam1"
CALIBRATION_MIN_IMAGES = 40
CALIBRATION_MIN_CORNERS = 6
# The operator picks a sport before recording; it only decides whether
# calibration/undistortion is mandatory. APP_USE_CASE preselects the default.
SPORT_OPTIONS = {
    "wide_angle": {
        "label": "Wide-angle Sport",
        "description": "Use calibrated side camera undistortion before upload.",
        "calibration_required": True,
    },
    "archery": {
        "label": "Archery",
        "description": "Use normal lens flow without calibration or undistortion.",
        "calibration_required": False,
    },
}
DEFAULT_SPORT = os.environ.get("APP_USE_CASE", "wide_angle").strip().lower()
if DEFAULT_SPORT not in SPORT_OPTIONS:
    DEFAULT_SPORT = "wide_angle"
selected_sport = None
# Which camera Pi hosts the INMP441 microphone (None = no mic until the
# operator assigns one via /api/mic/assign).
MIC_CAMERA_KEY = os.environ.get("MIC_CAMERA_KEY", "").strip().lower() or None
if MIC_CAMERA_KEY not in CAMERA_SOURCES:
    MIC_CAMERA_KEY = None

# ---- Preview pipeline state (one entry per camera). capture_frames() writes
# the newest decoded frame + its wall-clock time under frame_locks; the MJPEG
# generators, snapshots and the focus scorer read from it. The two event maps
# tell the capture thread to exit / reopen its RTSP connection.
frames = {k: None for k in CAMERA_SOURCES}
frame_ts = {k: 0.0 for k in CAMERA_SOURCES}
frame_locks = {k: threading.Lock() for k in CAMERA_SOURCES}
stop_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}
reopen_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}
# Set by _run_stop_pipeline (RIG_PAUSE_PREVIEW_ON_STOP) while the stop
# pipeline runs: capture_frames releases its RTSP stream and waits, so the
# three preview decoders leave the CPU to the sync encodes.
preview_pause_evt = threading.Event()

# ---- Recording state. is_recording_evt is THE flag every route checks;
# recording_index counts takes within the session (recording_N folders);
# record_procs / record_logs hold the per-camera FFmpeg Popen + log handle.
# session_state_lock (re-entrant) serialises start/stop/new-session.
is_recording_evt = threading.Event()
recording_index = 0
recording_start_epoch = None
current_recording_dir = None
session_state_lock = threading.RLock()

record_procs = {}
record_logs = {}
# ---- Camera bootstrap (SSH supervisor) state. One monitor thread per camera
# keeps camera_backend_status[cam] current; every mutation goes through
# camera_backend_state_lock. *_clients / *_channels are legacy slots from the
# paramiko era and are only ever cleared today.
camera_backend_stop_evt = threading.Event()
camera_backend_threads = {}
camera_backend_clients = {}
camera_backend_channels = {}
camera_backend_state_lock = Lock()
camera_backend_shutdown_lock = Lock()
camera_backend_shutdown_done = False
camera_backend_logs_dir = SESSION_DIR / "camera_bootstrap"
camera_backend_logs_dir.mkdir(parents=True, exist_ok=True)
camera_backend_status = {
    cam_key: {
        "camera": cam_key,
        "name": cfg["name"],
        "host": cfg["host"],
        "ssh_target": f"{CAMERA_SSH_USER}@{cfg['host']}",
        "rtsp_url": CAMERA_SOURCES[cam_key],
        "state": "disabled" if not CAMERA_BOOTSTRAP_ENABLED else "idle",
        "ssh_ok": False,
        "camera_connected": False,
        "rtsp_server_running": False,
        "preview_healthy": False,
        "last_message": None,
        "last_error": None,
        "last_exit_code": None,
        "restart_count": 0,
        "last_started_at": None,
        "last_event_at": None,
        "log_path": str((camera_backend_logs_dir / f"{cam_key}.log").relative_to(BASE_DIR)),
        "recent_events": [],
    }
    for cam_key, cfg in CAMERA_BOOTSTRAP.items()
}

# Leave half the cores for FFmpeg; OpenCV is only used for preview/snapshots.
cv2.setNumThreads(max(1, os.cpu_count() // 2))


def calculate_crc16(data):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) as computed by the insole
    firmware over the first 18 bytes of each ADC packet.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return crc



# ==================== Camera Bootstrap Helpers ====================
# The rig does not trust the Pis to keep v4l2rtspserver running on their own.
# For each camera a daemon thread (_monitor_camera_backend) SSHes in, restarts
# the RTSP server with the exact argv from CAMERA_BOOTSTRAP, then polls a
# status script every CAMERA_BOOTSTRAP_HEALTHCHECK_SEC and restarts on any
# failure with CAMERA_BOOTSTRAP_BACKOFF. All remote work is plain bash fed to
# 'ssh <pi> bash -s' on stdin - nothing needs to be installed on the Pi
# besides v4l2rtspserver (and python3-lgpio for the lens motor).
def _camera_backend_log_path(cam_key: str) -> Path:
    """Per-camera SSH transcript file inside the current session."""
    return camera_backend_logs_dir / f"{cam_key}.log"


def _camera_backend_event(cam_key: str, message: str, *, state: str | None = None, error: str | None = None):
    """Record one bootstrap event: update the camera's status (state / last
    error / rolling 20-line history) under the lock and mirror it to the log.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    event = f"{ts} {message}"
    with camera_backend_state_lock:
        status = camera_backend_status[cam_key]
        status["last_message"] = message
        status["last_event_at"] = ts
        if state is not None:
            status["state"] = state
        if error is not None:
            status["last_error"] = error
        events = list(status.get("recent_events", []))
        events.append(event)
        status["recent_events"] = events[-20:]
    bootstrap_log.info("[%s] %s", cam_key, message)


def _camera_backend_snapshot():
    """Copy of every camera's bootstrap status dict for /status."""
    with camera_backend_state_lock:
        return {
            cam_key: {
                **status,
                "recent_events": list(status.get("recent_events", [])),
            }
            for cam_key, status in camera_backend_status.items()
        }


def _camera_status_update(cam_key: str, **updates):
    """Merge fields into one camera's bootstrap status under the state lock."""
    with camera_backend_state_lock:
        status = camera_backend_status[cam_key]
        status.update(updates)


def _preview_is_healthy(cam_key: str, max_age_s: float = 5.0) -> bool:
    """True when the preview loop published a frame within max_age_s.

    This is the single freshness predicate shared by /status and the focus
    scorer, so both agree on whether a stream is frozen.
    """
    ts = frame_ts.get(cam_key, 0.0)
    return bool(ts and (time.time() - ts) <= max_age_s)


def _camera_health_payload(cam_key: str):
    """Flatten bootstrap status + preview freshness into the shape the UI polls."""
    with camera_backend_state_lock:
        base = dict(camera_backend_status[cam_key])
    base["preview_healthy"] = _preview_is_healthy(cam_key)
    base["last_log_line"] = base.get("last_message") or ""
    return {
        "ssh_ok": bool(base.get("ssh_ok")),
        "camera_connected": bool(base.get("camera_connected")),
        "rtsp_server_running": bool(base.get("rtsp_server_running")),
        "preview_healthy": bool(base.get("preview_healthy")),
        "last_error": base.get("last_error") or "",
        "last_log_line": base.get("last_log_line") or "",
        "state": base.get("state"),
        "host": base.get("host"),
        "name": base.get("name"),
        "rtsp_url": base.get("rtsp_url"),
        "restart_count": base.get("restart_count"),
        "last_started_at": base.get("last_started_at"),
        "last_event_at": base.get("last_event_at"),
        "last_exit_code": base.get("last_exit_code"),
        "log_path": base.get("log_path"),
    }


def _build_remote_camera_command(cam_key: str) -> str:
    """Bash script that (re)starts v4l2rtspserver on the Pi.

    Steps: verify the binary and /dev/video0 exist, TERM then KILL any server
    already serving this stream name, refuse to start if the RTSP port is
    still bound, launch under nohup with stdout/err in /tmp/<cam>_...log and
    the pid in /tmp/<cam>_...pid, then confirm the pid is alive 2 s later.
    Exit codes: 0 ok, 127 no binary, 66 no /dev/video0, 98 old process
    would not die, 99 port busy, 1 process died at start (log tail printed).
    """
    cfg = CAMERA_BOOTSTRAP[cam_key]
    stream_name = cfg["command"][cfg["command"].index("-u") + 1]
    port = cfg["command"][cfg["command"].index("-P") + 1]
    cmd = " ".join(shlex.quote(part) for part in cfg["command"])
    remote_log = f"/tmp/{cam_key}_v4l2rtspserver.log"
    remote_pid = f"/tmp/{cam_key}_v4l2rtspserver.pid"
    match_expr = shlex.quote("v4l2rtspserver.*-u " + stream_name)
    return (
        "if ! command -v v4l2rtspserver >/dev/null 2>&1; then "
        "echo '[bootstrap] v4l2rtspserver not found in PATH'; exit 127; "
        "fi; "
        "if [ ! -e /dev/video0 ]; then "
        "echo '[bootstrap] /dev/video0 not found'; exit 66; "
        "fi; "
        f"pkill -TERM -f {match_expr} >/dev/null 2>&1 || true; "
        "sleep 1; "
        f"if pgrep -f {match_expr} >/dev/null 2>&1; then "
        f"pkill -KILL -f {match_expr} >/dev/null 2>&1 || true; "
        "sleep 1; "
        "fi; "
        f"if pgrep -f {match_expr} >/dev/null 2>&1; then "
        "echo '[bootstrap] previous v4l2rtspserver is still running'; "
        f"pgrep -af {match_expr} || true; "
        "exit 98; "
        "fi; "
        f"if command -v ss >/dev/null 2>&1 && ss -ltn '( sport = :{port} )' | grep -q LISTEN; then "
        f"echo '[bootstrap] port {port} is still listening before start'; "
        f"ss -ltnp '( sport = :{port} )' 2>/dev/null || true; "
        "exit 99; "
        "fi; "
        f"rm -f {shlex.quote(remote_pid)}; "
        f"echo '[bootstrap] starting v4l2rtspserver'; "
        f"nohup {cmd} > {shlex.quote(remote_log)} 2>&1 < /dev/null & "
        f"pid=$!; echo $pid > {shlex.quote(remote_pid)}; "
        "sleep 2; "
        "if kill -0 \"$pid\" >/dev/null 2>&1; then "
        f"echo '[bootstrap] started pid='\"$pid\"' log={remote_log}'; "
        "exit 0; "
        "fi; "
        f"echo '[bootstrap] startup failed'; "
        f"if [ -f {shlex.quote(remote_log)} ]; then tail -n 80 {shlex.quote(remote_log)}; fi; "
        "exit 1"
    )


def _build_remote_camera_status_command(cam_key: str) -> str:
    """Bash health probe run on every healthcheck tick: exit 0 while the server
    process is alive, 66 if /dev/video0 vanished, 1 if it stopped (prints the
    remote log tail so the failure reason lands in the transcript).
    """
    cfg = CAMERA_BOOTSTRAP[cam_key]
    stream_name = cfg["command"][cfg["command"].index("-u") + 1]
    remote_log = f"/tmp/{cam_key}_v4l2rtspserver.log"
    return (
        "if [ ! -e /dev/video0 ]; then "
        "echo '[bootstrap] /dev/video0 not found'; exit 66; "
        "fi; "
        f"if pgrep -f {shlex.quote('v4l2rtspserver.*-u ' + stream_name)} >/dev/null 2>&1; then "
        "echo '[bootstrap] running'; exit 0; "
        "fi; "
        "echo '[bootstrap] stopped'; "
        f"if [ -f {shlex.quote(remote_log)} ]; then tail -n 80 {shlex.quote(remote_log)}; fi; "
        "exit 1"
    )


def _build_remote_camera_stop_command(cam_key: str) -> str:
    """Bash script that stops the remote server (pid file, then pkill TERM ->
    KILL) and verifies nothing matching is running or listening on the port.
    """
    cfg = CAMERA_BOOTSTRAP[cam_key]
    stream_name = cfg["command"][cfg["command"].index("-u") + 1]
    remote_pid = f"/tmp/{cam_key}_v4l2rtspserver.pid"
    match_expr = shlex.quote("v4l2rtspserver.*-u " + stream_name)
    port = str(cfg["command"][cfg["command"].index("-P") + 1])
    return (
        "set +e; "
        f"if [ -f {shlex.quote(remote_pid)} ]; then "
        f"pid=$(cat {shlex.quote(remote_pid)} 2>/dev/null || true); "
        "if [ -n \"$pid\" ] && kill -0 \"$pid\" >/dev/null 2>&1; then "
        "kill -TERM \"$pid\" >/dev/null 2>&1 || true; "
        "fi; "
        "fi; "
        f"pkill -TERM -f {match_expr} >/dev/null 2>&1 || true; "
        "sleep 1; "
        f"if pgrep -f {match_expr} >/dev/null 2>&1; then "
        f"pkill -KILL -f {match_expr} >/dev/null 2>&1 || true; "
        "sleep 1; "
        "fi; "
        f"rm -f {shlex.quote(remote_pid)}; "
        f"if pgrep -af {match_expr} >/tmp/{cam_key}_v4l2rtspserver.stopcheck 2>/dev/null; then "
        "echo '[bootstrap] stop verification failed: matching process still running'; "
        f"cat /tmp/{cam_key}_v4l2rtspserver.stopcheck 2>/dev/null || true; "
        "exit 1; "
        "fi; "
        f"if command -v ss >/dev/null 2>&1 && ss -ltn '( sport = :{port} )' | grep -q LISTEN; then "
        f"echo '[bootstrap] stop verification failed: port {port} still listening'; "
        f"ss -ltnp '( sport = :{port} )' 2>/dev/null || true; "
        "exit 1; "
        "fi; "
        "echo '[bootstrap] stop verified'; "
        "exit 0"
    )


def _build_camera_ssh_command(cam_key: str) -> list[str]:
    """Human-readable ssh argv written to the transcript. The real invocation is
    _run_remote_camera_command(), which also applies CAMERA_SSH_OPTS.
    """
    cfg = CAMERA_BOOTSTRAP[cam_key]
    target = f"{CAMERA_SSH_USER}@{cfg['host']}"
    return ["ssh", target, "bash", "-s"]


def _camera_log_line(cam_key: str, line: str):
    """Feed one line of remote output into the event log; error-looking lines
    also flip the camera into the 'error' state.
    """
    if not line:
        return
    if _camera_line_is_error(line):
        _camera_backend_event(cam_key, line, state="error", error=line)
    else:
        _camera_backend_event(cam_key, line)


def _run_remote_camera_command(cam_key: str, command: str, timeout: float = 20):
    """Run a bash script on the camera Pi over SSH (script passed on stdin to
    'bash -s').

    Returns (returncode, stdout, stderr). A return code of 255 means ssh
    itself failed (unreachable host, auth, host-key mismatch) rather than the
    script. Raises subprocess.TimeoutExpired after `timeout` seconds.
    """
    cfg = CAMERA_BOOTSTRAP[cam_key]
    target = f"{CAMERA_SSH_USER}@{cfg['host']}"
    ssh_cmd = ["ssh"] + CAMERA_SSH_OPTS + [
        target,
        "bash",
        "-s",
    ]
    proc = subprocess.run(
        ssh_cmd,
        input=command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _lens_pins(cam_key) -> dict:
    """BCM pin map for a camera's focus stepper (per-camera override or default)."""
    return LENS_MOTOR_PIN_OVERRIDES.get(cam_key, LENS_MOTOR_DEFAULT_PINS)


def _build_lens_move_script(cam_key, steps, forward) -> str:
    """Python program executed ON THE PI to pulse the focus stepper driver.

    Uses lgpio: enable the driver (EN low), set DIR, then toggle STEP `steps`
    times with LENS_STEP_DELAY per half pulse, and always release EN. Prints
    'lens-ok' on success so the caller can tell a clean run from a partial
    one whose exit code happens to be 0.
    """
    pins = _lens_pins(cam_key)
    return f"""python3 - <<'LENSPY'
import sys, time
try:
    import lgpio
except ImportError:
    sys.exit('lens-error: lgpio missing, run: sudo apt install python3-lgpio')
STEP, DIR, EN = {pins['step']}, {pins['dir']}, {pins['en']}
STEPS, FWD, DELAY = {steps}, {1 if forward else 0}, {LENS_STEP_DELAY!r}
h = lgpio.gpiochip_open(0)
try:
    for p in (STEP, DIR, EN):
        lgpio.gpio_claim_output(h, p)
    lgpio.gpio_write(h, EN, 0)
    lgpio.gpio_write(h, DIR, FWD)
    time.sleep(0.01)
    for _ in range(STEPS):
        lgpio.gpio_write(h, STEP, 1)
        time.sleep(DELAY)
        lgpio.gpio_write(h, STEP, 0)
        time.sleep(DELAY)
finally:
    try:
        lgpio.gpio_write(h, EN, 1)
    except Exception:
        pass
    lgpio.gpiochip_close(h)
print('lens-ok')
LENSPY
"""


def lens_move(cam_key, steps, forward) -> tuple[dict, int]:
    """Move a camera's focus motor by `steps` microsteps (forward = 'in').

    Position is tracked in software only (lens_position, zeroed by
    /lens/<cam>/reset) and clamped to +/- LENS_TRAVEL_LIMIT. One move per
    camera at a time (lens_locks); a second request while busy gets 409.
    Returns (json_payload, http_status).
    """
    if cam_key not in CAMERA_SOURCES:
        return {"status": "error", "message": "invalid camera"}, 404

    def error_payload(message, status_code):
        lens_last_error[cam_key] = message
        return {
            "status": "error",
            "message": message,
            "position": lens_position[cam_key],
        }, status_code

    if cam_key not in CAMERA_BOOTSTRAP:
        return error_payload("no SSH config", 404)
    if steps <= 0:
        return error_payload("steps must be greater than zero", 400)

    lock = lens_locks[cam_key]
    if not lock.acquire(timeout=LENS_SSH_TIMEOUT + 5):
        return error_payload("motor busy", 409)

    try:
        current_position = lens_position[cam_key]
        delta = steps if forward else -steps
        target = current_position + delta
        if abs(target) > LENS_TRAVEL_LIMIT:
            return error_payload(f"travel limit is {LENS_TRAVEL_LIMIT} microsteps", 400)

        try:
            script = _build_lens_move_script(cam_key, steps, forward)
            returncode, stdout, stderr = _run_remote_camera_command(
                cam_key, script, timeout=LENS_SSH_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return error_payload("lens motor command timed out", 504)
        except Exception as exc:
            return error_payload(str(exc), 500)

        if returncode != 0 or "lens-ok" not in stdout:
            output_lines = (stdout + "\n" + stderr).strip().splitlines()
            message = output_lines[-1] if output_lines else "lens motor command failed"
            return error_payload(message, 502)

        lens_position[cam_key] = target
        lens_last_error[cam_key] = None
        return {
            "status": "ok",
            "position": target,
            "moved": delta,
        }, 200
    finally:
        lock.release()


def _run_remote_camera_ssh_debug(cam_key: str):
    """ssh -vvv probe used when a bootstrap SSH attempt fails with no output at
    all, so the handshake is captured in the transcript for diagnosis.
    """
    cfg = CAMERA_BOOTSTRAP[cam_key]
    target = f"{CAMERA_SSH_USER}@{cfg['host']}"
    ssh_cmd = ["ssh", "-vvv"] + CAMERA_SSH_OPTS + [
        target,
        "true",
    ]
    proc = subprocess.run(
        ssh_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _camera_line_is_error(line: str) -> bool:
    """Keyword heuristic that classifies a line of remote output as an error."""
    lower = line.lower()
    keywords = (
        "not found",
        "no such file",
        "cannot",
        "can't",
        "failed",
        "error",
        "denied",
        "unable",
        "timed out",
        "connection refused",
        "device busy",
        "device or resource busy",
        "usb",
    )
    return any(token in lower for token in keywords)


def _camera_connected_from_output(text: str) -> bool:
    """Heuristic: does the remote output suggest the USB camera is present?"""
    lower = (text or "").lower()
    if not lower:
        return False
    disconnected_tokens = (
        "/dev/video0 not found",
        "camera not detected",
        "no such file",
        "cannot open",
        "can't open",
        "device busy",
        "device or resource busy",
        "usb",
    )
    return not any(token in lower for token in disconnected_tokens)


def _monitor_camera_backend(cam_key: str):
    """Daemon thread body: supervise one camera's remote RTSP server forever.

    Loop: run the start script -> on success wait 2 s and run the status
    script -> while healthy, re-run the status script every
    CAMERA_BOOTSTRAP_HEALTHCHECK_SEC -> on any non-zero exit (or exception)
    record the failure, sleep CAMERA_BOOTSTRAP_BACKOFF[n] and start over.
    Every SSH exchange is appended verbatim to the per-camera transcript
    and summarised into camera_backend_status for /status. Exits when
    camera_backend_stop_evt is set.
    """
    backoff_index = 0
    log_path = _camera_backend_log_path(cam_key)

    while not camera_backend_stop_evt.is_set():
        remote_cmd = _build_remote_camera_command(cam_key)
        remote_status_cmd = _build_remote_camera_status_command(cam_key)
        ssh_cmd = _build_camera_ssh_command(cam_key)
        with camera_backend_state_lock:
            camera_backend_status[cam_key]["last_started_at"] = datetime.now().isoformat(timespec="seconds")
            camera_backend_status[cam_key]["ssh_target"] = f"{CAMERA_SSH_USER}@{CAMERA_BOOTSTRAP[cam_key]['host']}"

        _camera_backend_event(cam_key, f"Launching SSH session to {CAMERA_BOOTSTRAP[cam_key]['host']}", state="starting")

        return_code = None
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} starting bootstrap ===\n")
            logf.write("CMD: " + " ".join(ssh_cmd) + "\n")
            logf.write("REMOTE_CMD: " + remote_cmd + "\n")
            logf.flush()

            try:
                return_code, out, err = _run_remote_camera_command(cam_key, remote_cmd)
                ssh_ok = return_code != 255
                combined = (out + "\n" + err).strip()
                _camera_status_update(
                    cam_key,
                    ssh_ok=ssh_ok,
                    camera_connected=_camera_connected_from_output(combined) if combined else False,
                    rtsp_server_running=False,
                    preview_healthy=_preview_is_healthy(cam_key),
                )
                logf.write(f"SSH_EXIT_CODE: {return_code}\n")
                if out:
                    logf.write("SSH_STDOUT:\n" + out)
                    if not out.endswith("\n"):
                        logf.write("\n")
                if err:
                    logf.write("SSH_STDERR:\n" + err)
                    if not err.endswith("\n"):
                        logf.write("\n")
                if return_code == 255 and not (out or err):
                    dbg_code, dbg_out, dbg_err = _run_remote_camera_ssh_debug(cam_key)
                    logf.write(f"SSH_DEBUG_EXIT_CODE: {dbg_code}\n")
                    if dbg_out:
                        logf.write("SSH_DEBUG_STDOUT:\n" + dbg_out)
                        if not dbg_out.endswith("\n"):
                            logf.write("\n")
                    if dbg_err:
                        logf.write("SSH_DEBUG_STDERR:\n" + dbg_err)
                        if not dbg_err.endswith("\n"):
                            logf.write("\n")
                for line in (out + err).splitlines():
                    _camera_log_line(cam_key, line.rstrip())
                logf.flush()

                if return_code == 0:
                    _camera_status_update(
                        cam_key,
                        ssh_ok=True,
                        camera_connected=True,
                        rtsp_server_running=True,
                        preview_healthy=_preview_is_healthy(cam_key),
                    )
                    _camera_backend_event(cam_key, "SSH connected and remote v4l2rtspserver launched", state="running", error=None)
                    time.sleep(2.0)
                    status_code, status_out, status_err = _run_remote_camera_command(cam_key, remote_status_cmd)
                    status_combined = (status_out + "\n" + status_err).strip()
                    logf.write(f"SSH_STATUS_EXIT_CODE: {status_code}\n")
                    if status_out:
                        logf.write("SSH_STATUS_STDOUT:\n" + status_out)
                        if not status_out.endswith("\n"):
                            logf.write("\n")
                    if status_err:
                        logf.write("SSH_STATUS_STDERR:\n" + status_err)
                        if not status_err.endswith("\n"):
                            logf.write("\n")
                    for line in (status_out + status_err).splitlines():
                        _camera_log_line(cam_key, line.rstrip())
                    logf.flush()
                    return_code = status_code
                    _camera_status_update(
                        cam_key,
                        ssh_ok=status_code != 255,
                        camera_connected=_camera_connected_from_output(status_combined) if status_combined else True,
                        rtsp_server_running=(status_code == 0),
                        preview_healthy=_preview_is_healthy(cam_key),
                    )
                    if return_code == 0:
                        backoff_index = 0
                        while not camera_backend_stop_evt.is_set():
                            time.sleep(CAMERA_BOOTSTRAP_HEALTHCHECK_SEC)
                            status_code, status_out, status_err = _run_remote_camera_command(cam_key, remote_status_cmd)
                            status_combined = (status_out + "\n" + status_err).strip()
                            logf.write(f"SSH_STATUS_EXIT_CODE: {status_code}\n")
                            if status_out:
                                logf.write("SSH_STATUS_STDOUT:\n" + status_out)
                                if not status_out.endswith("\n"):
                                    logf.write("\n")
                            if status_err:
                                logf.write("SSH_STATUS_STDERR:\n" + status_err)
                                if not status_err.endswith("\n"):
                                    logf.write("\n")
                            for line in (status_out + status_err).splitlines():
                                _camera_log_line(cam_key, line.rstrip())
                            logf.flush()
                            _camera_status_update(
                                cam_key,
                                ssh_ok=status_code != 255,
                                camera_connected=_camera_connected_from_output(status_combined) if status_combined else True,
                                rtsp_server_running=(status_code == 0),
                                preview_healthy=_preview_is_healthy(cam_key),
                            )
                            if status_code != 0:
                                return_code = status_code
                                break
            except Exception as e:
                return_code = -1
                msg = str(e).strip() or repr(e)
                detail = traceback.format_exc()
                logf.write(msg + "\n")
                logf.write(detail + "\n")
                logf.flush()
                _camera_status_update(
                    cam_key,
                    ssh_ok=False,
                    camera_connected=False,
                    rtsp_server_running=False,
                    preview_healthy=_preview_is_healthy(cam_key),
                )
                _camera_backend_event(cam_key, msg, state="error", error=msg)
            finally:
                with camera_backend_state_lock:
                    camera_backend_status[cam_key]["last_exit_code"] = return_code
                    camera_backend_status[cam_key]["restart_count"] += 1
                    camera_backend_channels.pop(cam_key, None)
                    camera_backend_clients.pop(cam_key, None)

        if camera_backend_stop_evt.is_set():
            break

        with camera_backend_state_lock:
            last_error = camera_backend_status[cam_key]["last_error"] if return_code else None
        _camera_backend_event(
            cam_key,
            f"Remote process exited with code {return_code}; retrying",
            state="retrying",
            error=last_error,
        )
        sleep_s = CAMERA_BOOTSTRAP_BACKOFF[min(backoff_index, len(CAMERA_BOOTSTRAP_BACKOFF) - 1)]
        backoff_index += 1
        time.sleep(sleep_s)

    _camera_backend_event(cam_key, "Camera bootstrap worker stopped", state="stopped")


def start_camera_bootstrap():
    """Spawn one monitor thread per camera (idempotent; no-op when disabled)."""
    if not CAMERA_BOOTSTRAP_ENABLED:
        bootstrap_log.info("Disabled by CAMERA_BOOTSTRAP_ENABLED")
        return

    for cam_key in CAMERA_BOOTSTRAP:
        thread = camera_backend_threads.get(cam_key)
        if thread and thread.is_alive():
            continue
        thread = threading.Thread(target=_monitor_camera_backend, args=(cam_key,), daemon=True)
        camera_backend_threads[cam_key] = thread
        thread.start()


def stop_camera_bootstrap():
    """Shut the supervisor down: signal the monitors and stop every remote
    server over SSH. Idempotent - registered with atexit and also called
    from the SIGINT/SIGTERM handler.
    """
    global camera_backend_shutdown_done
    with camera_backend_shutdown_lock:
        if camera_backend_shutdown_done:
            return
        camera_backend_shutdown_done = True

    camera_backend_stop_evt.set()
    for channel in list(camera_backend_channels.values()):
        try:
            channel.close()
        except Exception:
            pass
    for client in list(camera_backend_clients.values()):
        try:
            client.close()
        except Exception:
            pass
    if CAMERA_BOOTSTRAP_ENABLED:
        for cam_key in CAMERA_BOOTSTRAP:
            try:
                stop_cmd = _build_remote_camera_stop_command(cam_key)
                return_code, out, err = _run_remote_camera_command(cam_key, stop_cmd, timeout=10)
                combined = "\n".join(part for part in (out.strip(), err.strip()) if part).strip()
                _camera_status_update(
                    cam_key,
                    ssh_ok=return_code != 255,
                    rtsp_server_running=False,
                    preview_healthy=_preview_is_healthy(cam_key),
                )
                if combined:
                    for line in combined.splitlines():
                        _camera_log_line(cam_key, line.rstrip())
                else:
                    _camera_backend_event(cam_key, "Remote stop requested", state="stopped", error=None)
            except Exception as e:
                msg = f"Remote shutdown failed: {e}"
                _camera_status_update(
                    cam_key,
                    ssh_ok=False,
                    rtsp_server_running=False,
                    preview_healthy=_preview_is_healthy(cam_key),
                )
                _camera_backend_event(cam_key, msg, state="error", error=msg)


atexit.register(stop_camera_bootstrap)


def _handle_shutdown_signal(signum, _frame):
    """Stop the remote RTSP servers before exiting on Ctrl-C / systemd stop."""
    stop_camera_bootstrap()
    raise SystemExit(128 + signum)


signal.signal(signal.SIGINT, _handle_shutdown_signal)
signal.signal(signal.SIGTERM, _handle_shutdown_signal)


# ==================== Camera Helpers ====================
def current_preview_fps():
    """Preview frame rate: throttled while a take is being recorded."""
    return float(
        RECORDING_PREVIEW_FPS
        if (STREAM_THROTTLE_ON_RECORD and is_recording_evt.is_set())
        else NORMAL_PREVIEW_FPS
    )


def current_jpeg_quality():
    """Preview JPEG quality: reduced while a take is being recorded."""
    return int(
        RECORDING_JPEG_QUALITY
        if (STREAM_THROTTLE_ON_RECORD and is_recording_evt.is_set())
        else NORMAL_JPEG_QUALITY
    )


def _assert_ffmpeg_available():
    """Raise a clear RuntimeError if ffmpeg is not on PATH."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH. Install it and try again.")


def _ffmpeg_has_encoder(name: str) -> bool:
    """Is `name` listed by `ffmpeg -encoders` (compile-time support only)?"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ).stdout
        return name in out
    except Exception:
        return False


def _ffmpeg_has_decoder(name: str) -> bool:
    """Is `name` listed by `ffmpeg -decoders` (compile-time support only)?"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-decoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ).stdout
        return name in out
    except Exception:
        return False


_cuda_available_cache = None


def _ffmpeg_has_usable_cuda() -> bool:
    """Probe once whether ffmpeg can actually initialise a CUDA device.

    Compile-time NVENC/CUVID support is not enough - a rig without a driver
    or GPU still lists them - so a tiny lavfi encode is run against
    cuda:0. The answer is cached for the life of the process.
    """
    global _cuda_available_cache
    if _cuda_available_cache is not None:
        return _cuda_available_cache

    if DISABLE_CUDA_RECORD:
        _cuda_available_cache = False
        return _cuda_available_cache

    sys_name = platform.system().lower()
    if sys_name not in ("linux", "windows"):
        _cuda_available_cache = False
        return _cuda_available_cache

    if not (_ffmpeg_has_encoder("h264_nvenc") or _ffmpeg_has_decoder("mjpeg_cuvid")):
        _cuda_available_cache = False
        return _cuda_available_cache

    # Detect runtime CUDA availability (not just FFmpeg compile-time support).
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-init_hw_device",
        "cuda=cuda:0",
        "-f",
        "lavfi",
        "-i",
        "color=size=16x16:rate=1",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _cuda_available_cache = proc.returncode == 0
    except Exception:
        _cuda_available_cache = False
    return _cuda_available_cache


def _record_cuda_enabled() -> bool:
    """CUDA path for recording: off with DISABLE_CUDA_RECORD, forced by
    FORCE_CUDA_RECORD, else probed."""
    if DISABLE_CUDA_RECORD:
        return False
    if FORCE_CUDA_RECORD:
        return True
    return _ffmpeg_has_usable_cuda()


def best_record_decode_args() -> list:
    """Hardware MJPEG decode (mjpeg_cuvid) when CUDA is usable, else let ffmpeg
    choose the software decoder.
    """
    sys_name = platform.system().lower()
    if (
        sys_name in ("linux", "windows")
        and _ffmpeg_has_decoder("mjpeg_cuvid")
        and _record_cuda_enabled()
    ):
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", "mjpeg_cuvid"]
    return []


def best_record_encoder_args():
    """Encoder args for the LIVE recording: NVENC when usable, else libx264.

    Both variants disable B-frames so every frame is independently seekable
    and the constant-frame-rate filter chain stays monotonic.
    """
    sys_name = platform.system().lower()
    # No +faststart on the LIVE recording: it adds a second pass at stop that
    # rewrites the entire file to front-load the moov atom, and a SIGKILL
    # landing mid-rewrite corrupts the whole MP4. The raw recording is an
    # ffmpeg-only intermediate — run_sync_on_dir() unconditionally re-encodes
    # every camera with best_sync_encoder_args(), which keeps +faststart on the
    # outputs that are actually served/uploaded.
    common_out = ["-bf", "0"]

    if (
        sys_name in ("linux", "windows")
        and _ffmpeg_has_encoder("h264_nvenc")
        and _record_cuda_enabled()
    ):
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "ll",
            "-b:v",
            "0",
            "-cq",
            "19",
            "-g",
            str(int(TARGET_FPS_WRITE)),
            "-profile:v",
            "high",
            "-pix_fmt",
            "nv12",
        ] + common_out

    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"] + common_out


def best_sync_encoder_args():
    """Encoder args for the post-recording alignment re-encode.

    Mirrors best_record_encoder_args() but tuned for an OFFLINE re-encode (no -tune ll).
    Falls back to the same libx264 settings the sync step has always used when CUDA is
    unusable, so non-GPU machines produce byte-equivalent output to before.
    """
    sys_name = platform.system().lower()
    common_out = ["-movflags", "+faststart", "-bf", "0"]

    if (
        sys_name in ("linux", "windows")
        and _ffmpeg_has_encoder("h264_nvenc")
        and _record_cuda_enabled()
    ):
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-b:v",
            "0",
            "-cq",
            "20",
            "-g",
            str(int(TARGET_FPS_WRITE)),
            "-profile:v",
            "high",
            "-pix_fmt",
            "nv12",
        ] + common_out

    args = ["-c:v", "libx264", "-preset", SYNC_PRESET, "-crf", "20", "-pix_fmt", "yuv420p"]
    if SYNC_THREADS > 0:
        args += ["-threads", str(SYNC_THREADS)]
    return args + common_out


# FFmpeg prints 'start: <seconds>' in its input stream dump. Because the
# recorders run with -use_wallclock_as_timestamps 1, that number is the Unix
# epoch at which the first frame arrived - it is the ONLY timing reference
# run_sync_on_dir() has to align the three cameras with each other and with
# the (host-clock stamped) insole / heart-rate samples.
START_REGEX = re.compile(r"start:\s*([0-9]+\.[0-9]+)")
CAMERA_NAME_MAPPING = {"cam1": "side", "cam2": "front", "cam3": "back"}


def _read_start_time_from_log(log_path: Path):
    """First 'start: <epoch>' value found in a per-camera ffmpeg log, or None."""
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
    """Run ffprobe with `args` and parse its JSON output ({} on any failure)."""
    proc = subprocess.run(["ffprobe", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {}


def _get_video_meta(video_path: Path):
    """Return (duration_s, fps) of a video via ffprobe.

    fps prefers r_frame_rate, then avg_frame_rate, then TARGET_FPS_WRITE.
    """
    data = _ffprobe_json(
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=r_frame_rate,avg_frame_rate",
            "-of",
            "json",
            str(video_path),
        ]
    )
    try:
        dur = float(data.get("format", {}).get("duration", 0.0))
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


def _get_video_size(video_path: Path) -> tuple[int, int] | None:
    """(width, height) of the first video stream via ffprobe, or None."""
    data = _ffprobe_json([
        "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(video_path),
    ])
    streams = data.get("streams") or [{}]
    try:
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
    except (TypeError, ValueError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def _parse_sensor_timestamp(value):
    """Return a Unix timestamp for UTC-aware or legacy local ISO timestamps."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Legacy BLE timestamps are deliberately naive because they were written
        # with datetime.fromtimestamp(). timestamp() interprets them in the same
        # host-local timezone, recovering the original host epoch.
        return parsed.timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _sync_ble_file(recording_dir: Path, sync_start: float, sync_end: float):
    """Window the decoded insole log to the synced video interval.

    Reads the newest ble/insole_log_recording_*.json, keeps only samples
    whose host timestamp falls in [sync_start, sync_end], adds Timestamp_UTC
    and video_time_s (seconds since the synced video's frame zero) and
    writes sync/ble_sync.json. Returns a stats dict (counts per side,
    dropped samples, >100 ms gaps, late start / early end) that ends up in
    sync_manifest.json and drives validate_recording().
    """
    ble_dir = recording_dir / "ble"
    candidates = sorted(ble_dir.glob("insole_log_recording_*.json")) if ble_dir.exists() else []
    candidates = [path for path in candidates if not path.name.endswith("_sync.json")]
    if not candidates:
        return {"ok": False, "missing": True, "message": "No BLE recording file found"}

    source = candidates[-1]
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "message": f"Could not read BLE data: {exc}"}

    output = {"Assignments": payload.get("Assignments", {}), "Left": [], "Right": [], "Unassigned": []}
    invalid_timestamps = 0
    outside_window = 0
    relative_times = []
    side_counts = {}
    for side in ("Left", "Right", "Unassigned"):
        for entry in payload.get(side, []):
            epoch = _parse_sensor_timestamp(entry.get("Timestamp"))
            if epoch is None:
                invalid_timestamps += 1
                continue
            if epoch < sync_start or epoch > sync_end:
                outside_window += 1
                continue
            synced = dict(entry)
            relative_time = max(0.0, epoch - sync_start)
            synced["Timestamp_UTC"] = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="microseconds")
            synced["video_time_s"] = round(relative_time, 6)
            output[side].append(synced)
            relative_times.append(relative_time)
        output[side].sort(key=lambda row: row["video_time_s"])
        side_counts[side.lower()] = len(output[side])

    destination = recording_dir / "sync" / "ble_sync.json"
    destination.write_text(json.dumps(output, separators=(",", ":")), encoding="utf-8")
    gaps = 0
    if len(relative_times) > 1:
        ordered = sorted(relative_times)
        gaps = sum(1 for left, right in zip(ordered, ordered[1:]) if right - left > 0.1)
    return {
        "ok": True,
        "source": str(source.relative_to(BASE_DIR)),
        "output": str(destination.relative_to(BASE_DIR)),
        "sample_count": len(relative_times),
        "side_counts": side_counts,
        "invalid_timestamps": invalid_timestamps,
        "samples_outside_window": outside_window,
        "gaps_over_100ms": gaps,
        "starts_late_s": round(min(relative_times), 6) if relative_times else None,
        "ends_early_s": round(sync_end - sync_start - max(relative_times), 6) if relative_times else None,
    }


def _sync_heartbeat_file(recording_dir: Path, sync_start: float, sync_end: float):
    """Same as _sync_ble_file() for the heart-rate JSONL: window the newest
    heartbeat/heart_rate_recording_*.jsonl to the video interval, add
    timestamp_utc / video_time_s and write sync/heart_rate_sync.jsonl.
    """
    heartbeat_dir = recording_dir / "heartbeat"
    candidates = sorted(heartbeat_dir.glob("heart_rate_recording_*.jsonl")) if heartbeat_dir.exists() else []
    if not candidates:
        return {"ok": False, "missing": True, "message": "No heart-rate recording file found"}

    source = candidates[-1]
    rows = []
    invalid_lines = 0
    outside_window = 0
    try:
        with source.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                epoch = _parse_sensor_timestamp(entry.get("timestamp"))
                if epoch is None:
                    invalid_lines += 1
                    continue
                if epoch < sync_start or epoch > sync_end:
                    outside_window += 1
                    continue
                synced = dict(entry)
                synced["timestamp_utc"] = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="microseconds")
                synced["video_time_s"] = round(max(0.0, epoch - sync_start), 6)
                rows.append(synced)
    except OSError as exc:
        return {"ok": False, "message": f"Could not read heart-rate data: {exc}"}

    rows.sort(key=lambda row: row["video_time_s"])
    destination = recording_dir / "sync" / "heart_rate_sync.jsonl"
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    gaps = sum(
        1 for left, right in zip(rows, rows[1:])
        if right["video_time_s"] - left["video_time_s"] > 2.0
    )
    duration = sync_end - sync_start
    return {
        "ok": True,
        "source": str(source.relative_to(BASE_DIR)),
        "output": str(destination.relative_to(BASE_DIR)),
        "sample_count": len(rows),
        "invalid_lines": invalid_lines,
        "samples_outside_window": outside_window,
        "gaps_over_2s": gaps,
        "starts_late_s": rows[0]["video_time_s"] if rows else None,
        "ends_early_s": round(duration - rows[-1]["video_time_s"], 6) if rows else None,
    }


def _write_remap_maps(calibration_data: dict, image_size: tuple[int, int], out_dir: Path,
                      oversample: int) -> tuple[Path, Path]:
    """Write cam1's undistortion lookup tables as 16-bit PGMs for ffmpeg's remap filter.

    Same model choice as intrinsic_calibrate_charuco.make_maps() but as float
    source coordinates (CV_32FC1). ffmpeg's remap is nearest-neighbour, so
    the maps point into a frame upscaled `oversample` times; _build_sync_cmd()
    scales by the same factor before remap. Source coordinates that fall
    outside the frame are set to 65535 so remap paints them with its fill
    colour (black), matching cv2.remap's constant border.
    """
    candidate, size = _candidate_from_calibration(calibration_data, image_size)
    w, h = size
    if candidate.model == "fisheye":
        map_x, map_y = cv2.fisheye.initUndistortRectifyMap(
            candidate.camera_matrix, candidate.dist_coeffs, np.eye(3),
            candidate.new_camera_matrix, (w, h), cv2.CV_32FC1,
        )
    else:
        map_x, map_y = cv2.initUndistortRectifyMap(
            candidate.camera_matrix, candidate.dist_coeffs, None,
            candidate.new_camera_matrix, (w, h), cv2.CV_32FC1,
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = (out_dir / "cam1_xmap.pgm", out_dir / "cam1_ymap.pgm")
    for path, m, limit in zip(paths, (map_x, map_y), (w, h)):
        outside = (m < 0) | (m > limit - 1)
        values = np.rint(m * oversample + (oversample - 1) / 2.0)
        values[outside] = 65535
        arr = np.clip(values, 0, 65535).astype(">u2")
        with open(path, "wb") as fh:
            fh.write(f"P5\n{w} {h}\n65535\n".encode("ascii"))
            fh.write(arr.tobytes())
    return paths


def _sync_remap_maps(cam: str, raw_path: Path, sync_dir: Path) -> tuple[tuple[Path, Path] | None, dict | None]:
    """(maps, info) for the sync pass: remap PGMs when `cam` is cam1, the sport
    needs undistortion and RIG_UNDISTORT_BACKEND=ffmpeg; otherwise (None, info)
    where info says why (or None for cam2/cam3, which are never undistorted).
    When maps cannot be built, postprocess_recording_for_upload() falls back
    to the OpenCV loop because the manifest records applied=False.
    """
    if cam != "cam1":
        return None, None
    if not calibration_required():
        return None, {"applied": False, "reason": f"{current_sport()}_mode"}
    if UNDISTORT_BACKEND != "ffmpeg":
        return None, {"applied": False, "reason": "opencv_backend"}
    data = _load_calibration_json()
    if not data:
        return None, {"applied": False, "reason": "calibration_missing"}
    size = _get_video_size(raw_path)
    if not size:
        return None, {"applied": False, "reason": "raw_size_unknown"}
    try:
        maps = _write_remap_maps(data, size, sync_dir, REMAP_OVERSAMPLE)
    except Exception as exc:
        log_exception(sync_log, "Could not build ffmpeg remap maps; falling back to OpenCV undistortion")
        return None, {"applied": False, "reason": f"map_build_failed: {exc}"}
    return maps, {
        "applied": True,
        "backend": "ffmpeg_remap",
        "oversample": REMAP_OVERSAMPLE,
        "camera": "cam1",
        "source_size": list(size),
        "maps": [_rel(maps[0]), _rel(maps[1])],
    }


def _sync_decode_args(raw_path: Path) -> list:
    """Input options for the sync pass: mjpeg_cuvid for a copy-mode MJPEG raw
    when CUDA is usable and RIG_SYNC_DECODER=auto, else nothing (software).
    Deliberately without -hwaccel_output_format cuda: the filters that follow
    (fps, scale, remap) are CPU filters, so decoded frames must land in system
    memory. NVENC uploads them again; at 720p90 that copy is negligible next
    to the decode it replaces.
    """
    if SYNC_DECODER != "auto" or raw_path.suffix.lower() != RAW_COPY_SUFFIX:
        return []
    if platform.system().lower() not in ("linux", "windows"):
        return []
    if _ffmpeg_has_decoder("mjpeg_cuvid") and _record_cuda_enabled():
        return ["-c:v", "mjpeg_cuvid"]
    return []


def _sync_decoder_name(raw_path: Path | None = None) -> str:
    """'mjpeg_cuvid' or 'software' for the config record."""
    probe = raw_path or Path(f"cam1{RAW_COPY_SUFFIX}")
    return "mjpeg_cuvid" if "mjpeg_cuvid" in _sync_decode_args(probe) else "software"


def _build_sync_cmd(raw_path: Path, offset_s: float, duration_s: float, fps: float, out_path: Path,
                    remap_maps: tuple[Path, Path] | None) -> list:
    """One ffmpeg pass: trim to the common window, force CFR, optionally
    undistort (remap), and encode with best_sync_encoder_args().

    Works for both raw kinds: an H.264 mp4 (encode mode; this is the second
    encode) or an MJPEG mkv (copy mode; this is the only encode, and because
    every MJPEG frame is a keyframe -ss lands exactly on the requested frame).
    """
    # Every camera must end up with the same frame count (validate_recording
    # requires it). -t alone gives floor(duration*fps) on most cameras and one
    # more on some (fps-filter rounding at the window edge; seen on every
    # pipeline, one camera off by one). So the cut is -frames:v at the floor,
    # with -t one frame period longer as the safety net.
    n_frames = max(1, int(duration_s * fps + 1e-3))
    limit_s = duration_s + (1.0 / fps if fps > 0 else 0.0)
    decode = _sync_decode_args(raw_path)
    cmd = ["ffmpeg", "-y", "-ss", f"{offset_s:.6f}"] + decode + ["-i", str(raw_path)]
    chain = f"fps={fps:.6f},setpts=PTS-STARTPTS"
    if decode:
        # mjpeg_cuvid hands back NV12 tagged with "reserved" primaries/transfer;
        # newer swscale refuses to convert such frames for remap/scale. Restate
        # what the camera's JPEG actually is (full-range bt470bg) up front.
        chain = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt470bg:range=pc," + chain
    out_scale = f"scale={OUTPUT_RES.replace('x', ':')}:flags=bicubic" if OUTPUT_RES else ""
    trim = ["-t", f"{limit_s:.6f}", "-frames:v", str(n_frames), "-an", "-sn"]
    if remap_maps is None:
        if out_scale:
            chain += f",{out_scale}"
        cmd += trim + ["-vf", chain]
    else:
        xmap, ymap = remap_maps
        if REMAP_OVERSAMPLE > 1:
            chain += f",scale=iw*{REMAP_OVERSAMPLE}:ih*{REMAP_OVERSAMPLE}:flags=bilinear"
        tail = f",{out_scale}" if out_scale else ""
        cmd += [
            "-i", str(xmap), "-i", str(ymap),
            *trim,
            "-filter_complex", f"[0:v]{chain}[v];[v][1:v][2:v]remap{tail}[out]",
            "-map", "[out]",
        ]
    return cmd + best_sync_encoder_args() + [str(out_path)]


def _discard_raw_copies(recording_dir: Path, validation: dict | None) -> None:
    """Copy mode only: delete <cam>.mkv once the take is synced AND validated
    usable (RIG_KEEP_RAW=1 keeps them). Encode mode never deletes anything.
    """
    if RECORD_MODE != "copy" or KEEP_RAW:
        return
    sync_ok = bool(_get_sync_status(recording_dir).get("ok"))
    usable = bool((validation or {}).get("usable"))
    if not (sync_ok and usable):
        rig_log.info("[record] keeping raw MJPEG in %s (sync ok=%s, validation usable=%s)",
                     recording_dir.name, sync_ok, usable)
        return
    for cam in CAMERA_SOURCES.keys():
        raw = recording_dir / f"{cam}{RAW_COPY_SUFFIX}"
        if not raw.exists():
            continue
        try:
            size_mb = raw.stat().st_size / (1024 * 1024)
            raw.unlink()
            rig_log.info("[record] deleted raw MJPEG %s (%.1f MB)", raw.name, size_mb)
        except OSError as exc:
            rig_log.warning("[record] could not delete raw MJPEG %s: %s", raw, exc)


def run_sync_on_dir(recording_dir: Path):
    """Align the raw per-camera recordings into a common time window.

    Algorithm:
      1. For each cam with both a raw recording (<cam>.mp4 or <cam>.mkv, see
         _raw_video_path) and a 'start:' epoch in <cam>.log, note its
         wall-clock start.
      2. sync_start = the LATEST start; each camera is trimmed by
         (sync_start - its own start) so frame zero is simultaneous.
      3. common duration = shortest remaining video; common fps = median of
         the cameras' fps (encode mode) or TARGET_FPS_WRITE (copy mode, where
         the raw MJPEG carries wall-clock VFR timestamps). Every camera is
         encoded in parallel to sync/<cam>_sync_<view>.mp4 with
         fps=<common>,setpts=PTS-STARTPTS (_build_sync_cmd), so all outputs
         have identical frame counts - validate_recording() checks exactly
         that. With RIG_UNDISTORT_BACKEND=ffmpeg, cam1 is undistorted inside
         the same pass (remap filter; manifest key "undistort").
      4. Insole and heart-rate logs are windowed to [sync_start, sync_end]
         (see _sync_ble_file / _sync_heartbeat_file).
    With a single camera an H.264 raw file is copied; an MJPEG raw is encoded
    once. Everything is summarised in sync/sync_manifest.json (also returned);
    ok requires >= 2 successfully encoded cameras.
    """
    sync_dir = recording_dir / "sync"
    sync_dir.mkdir(exist_ok=True)
    available = {}
    for cam in CAMERA_SOURCES.keys():
        vid_file = _raw_video_path(recording_dir, cam)
        if vid_file is None:
            continue
        st = _read_start_time_from_log(recording_dir / f"{cam}.log")
        if st is not None:
            available[cam] = {"start": st, "path": vid_file}

    if not available:
        message = "Need at least 1 camera with video and start time; found 0"
        sync_log.warning("%s. Skipping sync.", message)
        result = {"ok": False, "message": message, "successful_cameras": [], "warnings": [message]}
        (sync_dir / "sync_manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    if len(available) == 1:
        cam, info = next(iter(available.items()))
        suffix = CAMERA_NAME_MAPPING.get(cam, cam)
        out_path = sync_dir / f"{cam}_sync_{suffix}.mp4"
        undistort_info = None
        if info["path"].suffix.lower() == ".mp4":
            shutil.copy2(info["path"], out_path)
        else:
            # Copy-mode raw is MJPEG: this is the take's one and only encode.
            raw_dur, _ = _get_video_meta(info["path"])
            maps, undistort_info = _sync_remap_maps(cam, info["path"], sync_dir)
            cmd = _build_sync_cmd(info["path"], 0.0, raw_dur, float(TARGET_FPS_WRITE), out_path, maps)
            with open(sync_dir / f"{cam}_sync.log", "w", buffering=1) as sync_logf:
                sync_logf.write("CMD: " + " ".join(cmd) + "\n")
                rc = subprocess.run(cmd, stdout=sync_logf, stderr=sync_logf).returncode
            if rc != 0:
                sync_log.error("%s encode exited with code %s; see sync/%s_sync.log", cam, rc, cam)
        duration, fps = _get_video_meta(out_path) if out_path.exists() else (0.0, float(TARGET_FPS_WRITE))
        sync_start = info["start"]
        sync_end = sync_start + max(0.0, duration)
        message = f"Only {cam} was available; copied raw video as the synchronized {suffix} view"
        result = {
            "ok": duration > 0,
            "message": message,
            "sync_start_epoch": sync_start,
            "sync_start_utc": datetime.fromtimestamp(sync_start, tz=timezone.utc).isoformat(timespec="microseconds"),
            "sync_end_epoch": sync_end,
            "sync_end_utc": datetime.fromtimestamp(sync_end, tz=timezone.utc).isoformat(timespec="microseconds"),
            "duration_s": round(duration, 6),
            "fps": fps,
            "ble_expected_frequency_hz": int(str(ble.target_frequency_label).replace("Hz", "")),
            "microphone_expected": bool(mic.assigned_camera_key),
            "microphone_camera": mic.assigned_camera_key,
            "cameras": {
                cam: {
                    "start_epoch": sync_start,
                    "start_utc": datetime.fromtimestamp(sync_start, tz=timezone.utc).isoformat(timespec="microseconds"),
                    "trim_offset_s": 0.0,
                }
            },
            "successful_cameras": [cam] if duration > 0 else [],
            "failures": [] if duration > 0 else [{"camera": cam, "message": "Copied video has no readable duration"}],
            "warnings": [message],
            "record_mode": RECORD_MODE,
            "raw_files": {cam: info["path"].name},
            "undistort": undistort_info,
        }
        if result["ok"]:
            try:
                result["ble"] = _sync_ble_file(recording_dir, sync_start, sync_end)
            except Exception as exc:
                result["ble"] = {"ok": False, "message": f"BLE synchronization failed: {exc}"}
            try:
                result["heart_rate"] = _sync_heartbeat_file(recording_dir, sync_start, sync_end)
            except Exception as exc:
                result["heart_rate"] = {"ok": False, "message": f"Heart-rate synchronization failed: {exc}"}
        (sync_dir / "sync_manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        sync_log.info("[sync] %s: %s", message, out_path)
        return result

    starts = {cam: info["start"] for cam, info in available.items()}
    max_start = max(starts.values())
    offsets = {cam: max_start - st for cam, st in starts.items()}

    usable_durs = []
    fps_values = []
    for cam, info in available.items():
        vid_file = info["path"]
        dur, fps = _get_video_meta(vid_file)
        usable_durs.append(max(0.0, dur - offsets[cam]))
        fps_values.append(fps)

    if not usable_durs:
        result = {"ok": False, "message": "No usable video durations found", "warnings": ["No usable video durations found"]}
        (sync_dir / "sync_manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    common_dur = min(usable_durs)
    if any(info["path"].suffix.lower() != ".mp4" for info in available.values()):
        # Copy-mode raw MJPEG carries wall-clock VFR timestamps; ffprobe's
        # r_frame_rate is meaningless there. The fps filter below makes the
        # outputs CFR at the rate the recorders were always meant to produce.
        common_fps = float(TARGET_FPS_WRITE)
    else:
        fps_values.sort()
        common_fps = fps_values[len(fps_values) // 2]

    # Launch all camera encodes in parallel (GPU NVENC when available, else libx264),
    # then wait on all. cam1 picks up the remap maps when the ffmpeg undistort
    # backend is active, so its undistortion costs no extra pass.
    t0 = time.time()
    procs = []
    undistort_info = None
    for cam, info in available.items():
        suffix = CAMERA_NAME_MAPPING.get(cam, cam)
        out_path = sync_dir / f"{cam}_sync_{suffix}.mp4"
        maps, cam_undistort = _sync_remap_maps(cam, info["path"], sync_dir)
        if cam_undistort is not None:
            undistort_info = cam_undistort
        cmd = _build_sync_cmd(info["path"], offsets[cam], common_dur, common_fps, out_path, maps)
        # Keep ffmpeg's full output beside the synced file: with stderr sent to
        # DEVNULL a failed re-encode was undiagnosable in principle — only the
        # return code survived.
        sync_logf = open(sync_dir / f"{cam}_sync.log", "w", buffering=1)
        sync_logf.write("CMD: " + " ".join(cmd) + "\n")
        procs.append((cam, subprocess.Popen(cmd, stdout=sync_logf, stderr=sync_logf), sync_logf))

    successful = []
    failures = []
    for cam, p, sync_logf in procs:
        rc = p.wait()
        try:
            sync_logf.close()
        except Exception:
            pass
        if rc != 0:
            sync_log.error("%s re-encode exited with code %s; see sync/%s_sync.log", cam, rc, cam)
            failures.append({"camera": cam, "returncode": rc})
        else:
            successful.append(cam)
    encode_wall_s = round(time.time() - t0, 3)
    sync_log.info("re-encoded %d cameras in %.2fs", len(procs), encode_wall_s)

    output_durations = []
    for cam in successful:
        suffix = CAMERA_NAME_MAPPING.get(cam, cam)
        duration, _ = _get_video_meta(sync_dir / f"{cam}_sync_{suffix}.mp4")
        if duration > 0:
            output_durations.append(duration)
    synced_duration = min(output_durations) if output_durations else 0.0
    sync_end = max_start + synced_duration
    camera_details = {
        cam: {
            "start_epoch": starts[cam],
            "start_utc": datetime.fromtimestamp(starts[cam], tz=timezone.utc).isoformat(timespec="microseconds"),
            "trim_offset_s": round(offsets[cam], 6),
        }
        for cam in available
    }
    result = {
        "ok": len(successful) >= 2 and len(output_durations) >= 2 and synced_duration > 0,
        "sync_start_epoch": max_start,
        "sync_start_utc": datetime.fromtimestamp(max_start, tz=timezone.utc).isoformat(timespec="microseconds"),
        "sync_end_epoch": sync_end,
        "sync_end_utc": datetime.fromtimestamp(sync_end, tz=timezone.utc).isoformat(timespec="microseconds"),
        "duration_s": round(synced_duration, 6),
        "fps": common_fps,
        "ble_expected_frequency_hz": int(str(ble.target_frequency_label).replace("Hz", "")),
        "microphone_expected": bool(mic.assigned_camera_key),
        "microphone_camera": mic.assigned_camera_key,
        "cameras": camera_details,
        "successful_cameras": successful,
        "failures": failures,
        "warnings": [] if len(successful) == len(CAMERA_SOURCES) else ["Synchronization does not contain all three cameras"],
        "record_mode": RECORD_MODE,
        "raw_files": {cam: info["path"].name for cam, info in available.items()},
        "undistort": undistort_info,
        "encode_wall_s": encode_wall_s,
    }
    if result["ok"]:
        try:
            result["ble"] = _sync_ble_file(recording_dir, max_start, sync_end)
        except Exception as exc:
            result["ble"] = {"ok": False, "message": f"BLE synchronization failed: {exc}"}
        try:
            result["heart_rate"] = _sync_heartbeat_file(recording_dir, max_start, sync_end)
        except Exception as exc:
            result["heart_rate"] = {"ok": False, "message": f"Heart-rate synchronization failed: {exc}"}
        if not result["ble"].get("ok"):
            result["warnings"].append(result["ble"].get("message", "BLE synchronization failed"))
        elif not result["ble"].get("side_counts", {}).get("left") or not result["ble"].get("side_counts", {}).get("right"):
            result["warnings"].append("Synchronized BLE data does not contain both Left and Right samples")
        elif not result["ble"].get("sample_count"):
            result["warnings"].append("Synchronized BLE file contains no samples in the video window")
        if not result["heart_rate"].get("ok"):
            result["warnings"].append(result["heart_rate"].get("message", "Heart-rate synchronization failed"))
        elif not result["heart_rate"].get("sample_count"):
            result["warnings"].append("Synchronized heart-rate file contains no samples in the video window")
    else:
        result["warnings"].append("Fewer than two valid synchronized video outputs were produced")
    (sync_dir / "sync_manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _open_cv_rtsp(source_url: str):
    """cv2 capture over the FFmpeg backend with a 1-frame buffer, so the
    preview shows the newest frame instead of draining a queue.
    """
    cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def capture_frames(cam_key, source_url: str):
    """Preview thread body (one per camera).

    Keeps an RTSP capture open, resizes each frame to FRAME_SIZE and
    publishes it into frames[cam_key] / frame_ts[cam_key]. Reconnects with
    RTSP_RETRY_BACKOFF when the stream cannot be opened or goes silent for
    RTSP_TIMEOUT_MS; reopen_capture_evts forces a reconnect (set after every
    recording stop, because the remote server is restarted then).
    """
    backoffs = list(RTSP_RETRY_BACKOFF)
    last_frame_wall = 0.0

    while not stop_capture_evts[cam_key].is_set():
        if preview_pause_evt.is_set():
            time.sleep(0.2)
            continue
        cap = _open_cv_rtsp(source_url)
        if not cap.isOpened():
            time.sleep(backoffs[0])
            backoffs = backoffs[1:] + [backoffs[-1]]
            continue

        backoffs = list(RTSP_RETRY_BACKOFF)
        while not stop_capture_evts[cam_key].is_set():
            if reopen_capture_evts[cam_key].is_set():
                reopen_capture_evts[cam_key].clear()
                break
            if preview_pause_evt.is_set():
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.01)
                if (time.time() - last_frame_wall) * 1000 > RTSP_TIMEOUT_MS:
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
    """Start the per-camera preview threads (called once from __main__)."""
    for cam_key, src in CAMERA_SOURCES.items():
        t = threading.Thread(target=capture_frames, args=(cam_key, src), daemon=True)
        t.start()


def gen_frames(cam_key):
    """Generator behind /video_feed/<cam>: JPEG-encode the latest preview
    frame at the current fps / quality as a multipart/x-mixed-replace
    stream. Runs for as long as the browser keeps the connection open.
    """
    while True:
        fps = current_preview_fps()
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

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-cache\r\n\r\n" + buffer.tobytes() + b"\r\n"
        )

        dt = time.perf_counter() - start
        time.sleep(max(0.0, interval - dt))


def latest_frame_copy(cam_key):
    """Thread-safe copy of the newest preview frame (None if nothing yet)."""
    if cam_key not in CAMERA_SOURCES:
        return None
    with frame_locks[cam_key]:
        frame = frames.get(cam_key)
        return frame.copy() if frame is not None else None


def focus_payload_for_camera(cam_key, target="cube"):
    """Score lens focus on a camera's newest preview frame.

    Delegates to codesharpnessmeasure.measure_frame() with the per-camera
    best-so-far tracker. Returns (payload, http_status): 503 when the
    scorer is unavailable, there is no frame, or the frame is stale.
    """
    if cam_key not in CAMERA_SOURCES:
        return {"error": "invalid camera"}, 404
    if not FOCUS_MEASURE_AVAILABLE:
        return {
            "detected": False,
            "score": None,
            "label": "UNAVAILABLE",
            "color": "#888888",
            "corners": 0,
            "error": FOCUS_MEASURE_ERROR or "focus measure unavailable",
        }, 503

    frame = latest_frame_copy(cam_key)
    if frame is None:
        return {"error": "no frame"}, 503

    # frames[] keeps the last decoded frame forever, so a wedged stream would
    # still be scored - and a frozen board reads SHARP while /status on the
    # same page says preview_healthy: false. Gate on the same age predicate.
    # Distinct label + color: UNAVAILABLE/NO BOARD grey must not absorb this.
    if not _preview_is_healthy(cam_key):
        return {
            "detected": False,
            "score": None,
            "label": "STALE",
            "color": "#b45309",
            "corners": 0,
            "error": "preview frame is stale; the camera stream appears frozen",
        }, 503

    if target not in ("cube", "board"):
        return {"error": "focus target must be cube or board"}, 400
    return measure_frame(frame, FOCUS_TRACKER, cam_key, target=target), 200


def build_ffmpeg_record_cmd(src_url: str, out_path: Path):
    """argv for one camera's recorder.

    Input: RTSP over TCP with wall-clock timestamps and generous buffers
    (a 90 fps MJPEG stream bursts).

    Copy mode (RIG_RECORD_MODE=copy): the MJPEG packets are written to an
    mkv untouched (-c:v copy). No decoder, no filter, no encoder; the CFR
    timeline and the H.264 encode happen once, in run_sync_on_dir().

    Encode mode: settb/fps/setpts force a constant TARGET_FPS_WRITE timeline
    (frames are duplicated or dropped as needed) and showinfo logs per-frame
    metadata; the hwdownload step is only present when decoding on the GPU.
    Output: no audio/subtitles, a 90 kHz track timescale so sub-frame
    timestamps survive the mp4 muxer.
    """
    fps = int(TARGET_FPS_WRITE)

    record_input = [
        "-rtsp_transport",
        "tcp",
        "-rtsp_flags",
        "prefer_tcp",
        "-fflags",
        "discardcorrupt",
        "-use_wallclock_as_timestamps",
        "1",
        "-avoid_negative_ts",
        "make_zero",
        "-rtbufsize",
        "256M",
        "-max_delay",
        "1000000",
        "-thread_queue_size",
        "8192",
    ]

    if RECORD_MODE == "copy":
        return ["ffmpeg", "-y"] + record_input + ["-i", src_url, "-an", "-sn", "-c:v", "copy", str(out_path)]

    dec = best_record_decode_args()
    enc = best_record_encoder_args()
    using_cuvid = any("mjpeg_cuvid" in x for x in dec)

    if using_cuvid:
        vf = f"settb=AVTB,hwdownload,format=nv12,fps={fps},setpts=N/({fps}*TB),showinfo"
    else:
        vf = f"settb=AVTB,fps={fps},setpts=N/({fps}*TB),showinfo"

    return (
        ["ffmpeg", "-y"]
        + record_input
        + dec
        + ["-i", src_url, "-an", "-sn", "-filter:v", vf]
        + enc
        + ["-video_track_timescale", "90000", str(out_path)]
    )


def start_recording_all():
    """Spawn one FFmpeg recorder per camera into a fresh recording_N folder.

    Bumps recording_index, sets recording_start_epoch / current_recording_dir
    and, only once at least one encoder is confirmed alive, is_recording_evt.
    Raises RuntimeError when ffmpeg is missing, when REQUIRE_CUDA_RECORD is
    set but CUDA is unusable, or when every encoder dies immediately (any
    encoders that did start are killed first). Callers hold
    session_state_lock; start_combined() is the normal entry point.
    """
    global recording_index, recording_start_epoch, current_recording_dir, record_procs, record_logs
    if is_recording_evt.is_set():
        return

    _assert_ffmpeg_available()

    cuda_ok = _ffmpeg_has_usable_cuda()
    if REQUIRE_CUDA_RECORD and not cuda_ok:
        raise RuntimeError("CUDA is required for recording, but FFmpeg CUDA init failed in this runtime.")

    rig_log.info(
        "[record] CUDA probe=%s force_cuda=%s require_cuda=%s disable_cuda=%s",
        "OK" if cuda_ok else "FAIL",
        FORCE_CUDA_RECORD,
        REQUIRE_CUDA_RECORD,
        DISABLE_CUDA_RECORD,
    )

    recording_index += 1
    current_recording_dir = SESSION_DIR / f"recording_{recording_index}"
    current_recording_dir.mkdir(parents=True, exist_ok=True)

    recording_start_epoch = time.time()
    record_procs = {}
    record_logs = {}

    try:
        for cam_key, src in CAMERA_SOURCES.items():
            raw_suffix = RAW_COPY_SUFFIX if RECORD_MODE == "copy" else ".mp4"
            out_path = current_recording_dir / f"{cam_key}{raw_suffix}"
            log_path = current_recording_dir / f"{cam_key}.log"
            cmd = build_ffmpeg_record_cmd(src, out_path)

            logf = open(log_path, "w", buffering=1)
            record_logs[cam_key] = logf
            logf.write("CMD: " + " ".join(cmd) + "\n")
            logf.write(f"MODE: {RECORD_MODE}\n")
            if RECORD_MODE == "copy":
                logf.write("BACKEND: copy (MJPEG passthrough, encoded once at sync)\n")
                logf.write("DECODE: none\n")
            else:
                if "h264_nvenc" in cmd:
                    logf.write("BACKEND: nvenc\n")
                elif "libx264" in cmd:
                    logf.write("BACKEND: libx264\n")
                if "mjpeg_cuvid" in cmd:
                    logf.write("DECODE: mjpeg_cuvid\n")
                else:
                    logf.write("DECODE: software\n")
            rig_log.info("[%s] FFmpeg recording started -> %s (log: %s)", cam_key, out_path, log_path)

            p = subprocess.Popen(cmd, stdout=logf, stderr=logf, cwd=str(BASE_DIR))
            record_procs[cam_key] = p
            time.sleep(0.3)
            if p.poll() is not None:
                rig_log.error("[%s] FFmpeg exited immediately with code %s. See %s", cam_key, p.returncode, log_path)

        alive = [p for p in record_procs.values() if p and p.poll() is None]
        if not alive:
            raise RuntimeError(
                "All FFmpeg recording processes exited immediately. "
                "Check cam*.log in the recording folder for details."
            )
    except Exception:
        # Roll back everything this start spawned. Without this, a failure on
        # camera N leaves cameras 1..N-1's encoders running - and the next
        # start reassigns record_procs, dropping the last handle to those PIDs
        # so they can only be killed from a shell.
        log_exception(rig_log, "Recording start failed; rolling back spawned encoders")
        for _, p in list(record_procs.items()):
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        for _, logf in list(record_logs.items()):
            try:
                logf.flush()
                logf.close()
            except Exception:
                pass
        record_procs = {}
        record_logs = {}
        raise

    # Only mark the rig as recording once at least one encoder is confirmed
    # alive. Setting the flag before the spawn loop meant any start failure
    # left it stuck: /status reported recording, retrying Start returned a
    # success-shaped "already_recording", and only pressing Stop (which then
    # sync/validated an empty folder) recovered the rig.
    is_recording_evt.set()


def _snap_output_dir() -> Path:
    """New snaps/snap_<ts>/ folder under the active recording (if any) or the
    session.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = current_recording_dir if (is_recording_evt.is_set() and current_recording_dir) else SESSION_DIR
    out = base / "snaps" / f"snap_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _snapshot_via_ffmpeg(src_url: str, out_path: Path) -> bool:
    """Grab one full-resolution JPEG straight from the RTSP stream (preferred:
    not subject to the preview resize). False on failure or a tiny file.
    """
    if not shutil.which("ffmpeg"):
        return False
    cmd = ["ffmpeg", "-y"] + FFMPEG_RTSP_INPUT + [
        "-i",
        src_url,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-an",
        "-sn",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000
    except Exception:
        return False


def _snapshot_from_preview(cam_key: str, out_path: Path) -> bool:
    """Fallback snapshot from the last preview frame (already FRAME_SIZE)."""
    with frame_locks[cam_key]:
        frame = frames.get(cam_key)
    if frame is None:
        return False
    ok = cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return bool(ok and out_path.exists() and out_path.stat().st_size > 1000)


def capture_photos_all() -> dict:
    """Snapshot every camera into a fresh snap folder and write manifest.json.

    Backs calibration image capture (/api/calibration/capture), the admin
    snapshot endpoint (/api/snapshots) and the legacy /capture_photos form.
    """
    out_dir = _snap_output_dir()
    results = []
    for cam_key, src in CAMERA_SOURCES.items():
        out_path = out_dir / f"{cam_key}.jpg"
        method = "ffmpeg_rtsp"
        ok = _snapshot_via_ffmpeg(src, out_path)
        if not ok:
            method = "preview_frame"
            ok = _snapshot_from_preview(cam_key, out_path)
        results.append(
            {
                "camera": cam_key,
                "ok": bool(ok),
                "method": method,
                "path": str(out_path.relative_to(BASE_DIR)) if ok else None,
            }
        )

    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_DIR.name,
        "recording": is_recording_evt.is_set(),
        "dir": str(out_dir.relative_to(BASE_DIR)),
        "results": results,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _rel(path: Path | None) -> str | None:
    """Path relative to BASE_DIR as a string (the form the UI/API exchange)."""
    if path is None:
        return None
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def _calibration_required_fields(data: dict) -> tuple[bool, str | None]:
    """Minimal schema check shared by in-app and uploaded calibration JSON."""
    model = data.get("selected_model") or data.get("model")
    if model not in ("pinhole", "fisheye"):
        return False, "Calibration JSON must include selected_model/model as pinhole or fisheye."
    image_size = data.get("image_size")
    if not isinstance(image_size, dict) or "width" not in image_size or "height" not in image_size:
        return False, "Calibration JSON must include image_size.width and image_size.height."
    for key in ("camera_matrix", "new_camera_matrix", "distortion_coefficients"):
        if key not in data:
            return False, f"Calibration JSON missing {key}."
    return True, None


def _load_calibration_json(path: Path | None = None) -> dict | None:
    """Parsed calibration JSON for the current session if present AND valid."""
    path = path or CALIBRATION_JSON
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    ok, _ = _calibration_required_fields(data)
    return data if ok else None


def calibration_available() -> bool:
    """Does the current session hold a valid cam1 calibration?"""
    return _load_calibration_json() is not None


def sport_selected() -> bool:
    """Has the operator chosen a sport for this process yet?"""
    return selected_sport in SPORT_OPTIONS


def current_sport() -> str:
    """Selected sport, or DEFAULT_SPORT before one is chosen."""
    return selected_sport if sport_selected() else DEFAULT_SPORT


def current_sport_config() -> dict:
    """SPORT_OPTIONS entry for the current sport."""
    return SPORT_OPTIONS[current_sport()]


def sport_options_for_template() -> list[dict]:
    """SPORT_OPTIONS as a list for the sport-select page / status payloads."""
    return [
        {"value": value, **config}
        for value, config in SPORT_OPTIONS.items()
    ]


def ui_template_context(page_mode: str) -> dict:
    """Context for index35_cam_sole.html. The single template renders one of
    three views selected by page_mode: sport_select, calibration, recording.
    """
    return {
        "page_mode": page_mode,
        "sport_options": sport_options_for_template(),
        "selected_sport": selected_sport,
        "default_sport": DEFAULT_SPORT,
        "sport_selected": sport_selected(),
        "app_use_case": current_sport(),
        "sport_label": current_sport_config()["label"],
        "calibration_required": calibration_required(),
        "session_name": SESSION_DIR.name,
        "cameras": list(CAMERA_SOURCES.keys()),
        "recording": is_recording_evt.is_set(),
        "recording_start_epoch": recording_start_epoch,
        "target_fps": TARGET_FPS_WRITE,
        "calibration": _calibration_summary(),
    }


def calibration_required() -> bool:
    """Whether the selected sport needs an undistorted cam1 (see SPORT_OPTIONS)."""
    return bool(current_sport_config()["calibration_required"])


def _calibration_summary(path: Path | None = None) -> dict:
    """Compact calibration descriptor embedded in most status payloads."""
    path = path or CALIBRATION_JSON
    data = _load_calibration_json(path)
    if not data:
        return {
            "available": False,
            "required": calibration_required(),
            "use_case": current_sport(),
            "json_path": _rel(path) if path.exists() else None,
        }
    return {
        "available": True,
        "required": calibration_required(),
        "use_case": current_sport(),
        "json_path": _rel(path),
        "npz_path": _rel(CALIBRATION_NPZ) if CALIBRATION_NPZ.exists() else None,
        "model": data.get("selected_model") or data.get("model"),
        "rms_reprojection_error_px": data.get("rms_reprojection_error_px"),
        "used_images": data.get("used_images"),
        "total_images": data.get("total_images"),
        "image_size": data.get("image_size"),
        "source": data.get("source", "in_app"),
    }


def _snap_rows() -> list[dict]:
    """One row per calibration snapshot of CALIBRATION_CAMERA, joined with the
    detector's per-image results (marker / corner counts, 'used' flag) from
    either the calibration JSON or the detection-only pass.
    """
    detections_by_image = {}
    detections_path = CALIBRATION_DIR / "detections_cam1.json"
    calibration_data = _load_calibration_json()
    if calibration_data:
        for row in calibration_data.get("detections", []) or []:
            detections_by_image[row.get("image")] = row
    elif detections_path.exists():
        try:
            data = json.loads(detections_path.read_text(encoding="utf-8"))
            for row in data.get("detections", []) or []:
                detections_by_image[row.get("image")] = row
        except Exception:
            pass

    rows = []
    for image_path in sorted((SESSION_DIR / "snaps").glob(f"*/{CALIBRATION_CAMERA}.jpg")):
        rel = _rel(image_path)
        det = detections_by_image.get(rel) or detections_by_image.get(str(image_path)) or {}
        rows.append(
            {
                "snap": image_path.parent.name,
                "image_path": rel,
                "image_url": url_for("media_file", filepath=rel) if rel else None,
                "used": det.get("used"),
                "markers": det.get("markers"),
                "charuco_corners": det.get("charuco_corners"),
            }
        )
    return rows


def _calibration_status_payload() -> dict:
    """Full /api/calibration/status payload: snap rows, usable-image count vs
    the CALIBRATION_MIN_* thresholds, the calibration summary and an
    original/undistorted preview pair (generated on the fly for uploaded
    calibrations whose preview images are not on this machine).
    """
    snaps = _snap_rows()
    usable = sum(1 for row in snaps if row.get("used") is True)
    calibration_data = _load_calibration_json()
    preview = None
    if calibration_data:
        originals = calibration_data.get("used_source_images") or calibration_data.get("source_images") or []
        undistorted = calibration_data.get("undistorted_images") or []
        if originals and undistorted:
            original_path = Path(originals[-1])
            undistorted_path = Path(undistorted[-1])
            original_file = original_path if original_path.is_absolute() else BASE_DIR / original_path
            undistorted_file = undistorted_path if undistorted_path.is_absolute() else BASE_DIR / undistorted_path
            if original_file.is_file() and undistorted_file.is_file():
                preview = {
                    "original_path": _rel(original_file),
                    "original_url": url_for("media_file", filepath=_rel(original_file)),
                    "undistorted_path": _rel(undistorted_file),
                    "undistorted_url": url_for("media_file", filepath=_rel(undistorted_file)),
                }

        # Uploaded calibrations often refer to source/preview images that only
        # existed on the machine where calibration was performed.  Build a
        # preview from the newest image in this session instead of returning
        # broken media URLs for those stale paths.
        if preview is None and snaps:
            original_file = BASE_DIR / snaps[-1]["image_path"]
            preview_dir = CALIBRATION_DIR / "undistorted_images"
            undistorted_file = preview_dir / f"{original_file.parent.name}_{original_file.stem}_undistorted.jpg"
            try:
                frame = cv2.imread(str(original_file))
                candidate, image_size = _candidate_from_calibration(calibration_data)
                if frame is None:
                    raise ValueError(f"Could not read preview image: {_rel(original_file)}")
                actual_size = (frame.shape[1], frame.shape[0])
                if actual_size != image_size:
                    raise ValueError(
                        f"Preview image is {actual_size[0]}x{actual_size[1]}, but calibration expects "
                        f"{image_size[0]}x{image_size[1]}"
                    )
                if not undistorted_file.is_file():
                    map1, map2 = make_maps(candidate, image_size)
                    corrected = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
                    preview_dir.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(undistorted_file), corrected):
                        raise ValueError(f"Could not write preview image: {_rel(undistorted_file)}")
                preview = {
                    "original_path": _rel(original_file),
                    "original_url": url_for("media_file", filepath=_rel(original_file)),
                    "undistorted_path": _rel(undistorted_file),
                    "undistorted_url": url_for("media_file", filepath=_rel(undistorted_file)),
                }
            except (KeyError, TypeError, ValueError, cv2.error):
                preview = None
    return {
        "status": "success",
        "session_dir": _rel(SESSION_DIR),
        "snaps_dir": _rel(SESSION_DIR / "snaps"),
        "snap_count": len(snaps),
        "usable_images": usable,
        "min_images": CALIBRATION_MIN_IMAGES,
        "min_corners": CALIBRATION_MIN_CORNERS,
        "calibration": _calibration_summary(),
        "preview": preview,
        "snaps": snaps,
    }


def _run_session_calibration() -> dict:
    """Solve cam1 intrinsics from this session's snaps and persist
    calibration_cam1.json.

    Board geometry is fixed here: ChArUco 4x3 squares, 40 mm squares with
    30 mm markers, DICT_4X4_50; model 'auto' lets the calibrator pick
    pinhole vs fisheye. The calibrator raises SystemExit when fewer than
    CALIBRATION_MIN_IMAGES usable images exist (surfaced as HTTP 400).
    """
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(
        images=None,
        snaps_dir=str(SESSION_DIR / "snaps"),
        camera=CALIBRATION_CAMERA,
        model="auto",
        undistort_image=None,
        undistort_video=None,
        output_dir=str(CALIBRATION_DIR),
        dictionary="DICT_4X4_50",
        squares_x=4,
        squares_y=3,
        square_length=0.04,
        marker_length=0.03,
        min_corners=CALIBRATION_MIN_CORNERS,
        min_images=CALIBRATION_MIN_IMAGES,
        alpha=0.0,
        detect_only=False,
    )
    result = calibrate_charuco(args)
    result["source"] = "in_app"
    result["calibrated_at"] = datetime.now(timezone.utc).isoformat()
    CALIBRATION_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _run_detection_preview() -> dict | None:
    """Detection-only pass (no solve) so the UI can show per-snap marker and
    corner counts right after each capture. None if there are no snaps yet.
    """
    if len(list((SESSION_DIR / "snaps").glob(f"*/{CALIBRATION_CAMERA}.jpg"))) < 1:
        return None
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(
        images=None,
        snaps_dir=str(SESSION_DIR / "snaps"),
        camera=CALIBRATION_CAMERA,
        model="auto",
        undistort_image=None,
        undistort_video=None,
        output_dir=str(CALIBRATION_DIR),
        dictionary="DICT_4X4_50",
        squares_x=4,
        squares_y=3,
        square_length=0.04,
        marker_length=0.03,
        min_corners=CALIBRATION_MIN_CORNERS,
        min_images=1,
        alpha=0.0,
        detect_only=True,
    )
    try:
        return calibrate_charuco(args)
    except SystemExit:
        return None


def _save_uploaded_calibration_json(data: dict) -> dict:
    """Persist an operator-supplied calibration JSON as this session's
    calibration (tagged source=uploaded_json). Raises ValueError if invalid.
    """
    ok, error = _calibration_required_fields(data)
    if not ok:
        raise ValueError(error or "Invalid calibration JSON.")
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    saved = dict(data)
    saved["source"] = "uploaded_json"
    saved["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    CALIBRATION_JSON.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return saved


def _candidate_from_calibration(
    data: dict, target_size: tuple[int, int] | None = None
) -> tuple[CalibrationCandidate, tuple[int, int]]:
    """Rebuild the calibrator's CalibrationCandidate from stored JSON so that
    make_maps() can produce undistortion maps. Returns (candidate, (w, h)).

    test/low-res: when ``target_size`` differs from the calibration's own
    image_size but has the same aspect ratio, the intrinsics are rescaled to
    the target. Focal lengths and principal point scale linearly with the
    image; distortion coefficients are expressed in normalised coordinates
    and stay as they are. A mismatched aspect ratio raises, because a crop
    (not a resize) happened somewhere and the calibration no longer applies.
    """
    image_size = (int(data["image_size"]["width"]), int(data["image_size"]["height"]))
    camera_matrix = np.array(data["camera_matrix"], dtype=np.float64)
    new_camera_matrix = np.array(data["new_camera_matrix"], dtype=np.float64)
    roi = tuple(data.get("roi") or (0, 0, image_size[0], image_size[1]))

    if target_size is not None and tuple(target_size) != image_size:
        sx = target_size[0] / image_size[0]
        sy = target_size[1] / image_size[1]
        if abs(sx - sy) > 0.01:
            raise RuntimeError(
                f"Calibration is {image_size[0]}x{image_size[1]} but the video is "
                f"{target_size[0]}x{target_size[1]}; aspect ratios differ, so the "
                "calibration cannot be rescaled. Recalibrate at the capture resolution."
            )
        scale = np.array([[sx, 0.0, 0.0], [0.0, sy, 0.0], [0.0, 0.0, 1.0]])
        camera_matrix = scale @ camera_matrix
        new_camera_matrix = scale @ new_camera_matrix
        roi = (
            int(round(roi[0] * sx)),
            int(round(roi[1] * sy)),
            int(round(roi[2] * sx)),
            int(round(roi[3] * sy)),
        )
        image_size = (int(target_size[0]), int(target_size[1]))

    candidate = CalibrationCandidate(
        model=data.get("selected_model") or data.get("model"),
        rms=float(data.get("rms_reprojection_error_px") or 0.0),
        camera_matrix=camera_matrix,
        dist_coeffs=np.array(data["distortion_coefficients"], dtype=np.float64).reshape(-1, 1),
        new_camera_matrix=new_camera_matrix,
        roi=roi,
        rvecs=[],
        tvecs=[],
    )
    return candidate, image_size


def _undistort_video_file(src: Path, dst: Path, calibration_data: dict) -> dict:
    """Undistort a whole video: OpenCV remap frame by frame into an mp4v
    intermediate, then a final ffmpeg re-encode with best_sync_encoder_args().

    OpenCV's own H.264 writer is unreliable across builds, hence the two
    stages. Raises RuntimeError if the video's aspect ratio does not match
    the calibration's image_size (a same-aspect size difference is handled by
    rescaling the intrinsics) or no frame was written.
    """
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video for undistortion: {src}")

    width = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
    height = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    fps = cap.get(cv2.CAP_PROP_FPS) or TARGET_FPS_WRITE
    # test/low-res: rescale the calibration to the recorded size instead of
    # refusing; only an aspect-ratio mismatch is fatal (raised inside).
    try:
        candidate, image_size = _candidate_from_calibration(calibration_data, (width, height))
    except RuntimeError:
        cap.release()
        raise

    map1, map2 = make_maps(candidate, image_size)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_name(f"{dst.stem}_opencv_tmp{dst.suffix}")
    writer = cv2.VideoWriter(str(tmp_dst), cv2.VideoWriter_fourcc(*"mp4v"), fps, image_size)
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Could not open temporary video writer: {tmp_dst}")

    frames_written = 0
    loop_t0 = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(cv2.remap(frame, map1, map2, cv2.INTER_LINEAR))
        frames_written += 1
    loop_seconds = time.time() - loop_t0

    cap.release()
    writer.release()
    if frames_written == 0:
        raise RuntimeError(f"No frames written while undistorting {src}")

    if dst.exists():
        dst.unlink()
    cmd = [
        "ffmpeg", "-y",
        "-i", str(tmp_dst),
        "-map", "0:v:0",
        "-an", "-sn",
        "-r", f"{fps:.6f}",
    ] + best_sync_encoder_args() + [
        "-video_track_timescale", "90000",
        str(dst),
    ]
    encode_t0 = time.time()
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    encode_seconds = time.time() - encode_t0
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg re-encode failed for undistorted cam1: {proc.stderr[-1000:]}")

    try:
        tmp_dst.unlink()
    except OSError:
        pass

    return {
        "frames": frames_written,
        "fps": fps,
        "model": candidate.model,
        "path": _rel(dst),
        "encoder": "ffmpeg_sync_encoder",
        # Timings for the test matrix (docs/test-matrix.md): the Python remap
        # loop and the extra H.264 encode it forces, separately.
        "loop_seconds": round(loop_seconds, 3),
        "encode_seconds": round(encode_seconds, 3),
        "seconds": round(loop_seconds + encode_seconds, 3),
    }


FFMPEG_PROGRESS_RE = re.compile(
    r"(?:frame=\s*(?P<frame>\d+).*?fps=\s*(?P<fps>[\d.]+).*?)?"
    r"(?:size=\s*(?P<size_kb>\d+)kB.*?)?time=\s*(?P<time>[\d:.]+).*?"
    r"(?:dup=\s*(?P<dup>\d+).*?drop=\s*(?P<drop>\d+).*?)?speed=\s*(?P<speed>[\d.]+)x"
)


def _ffmpeg_time_to_s(text: str) -> float | None:
    """'HH:MM:SS.ss' from an ffmpeg progress line as seconds."""
    try:
        parts = [float(v) for v in text.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return round(parts[0] * 3600 + parts[1] * 60 + parts[2], 3)


def _ffmpeg_log_stats(log_path: Path) -> dict | None:
    """Last progress line of an ffmpeg log as {frame, fps, time_s, dup, drop, speed}.

    ffmpeg writes progress with carriage returns, so the file is split on
    both \r and \n. `speed` is the real-time factor (>= 1.0 kept up),
    `drop`/`dup` come from the fps filter (encode-mode recorders only).
    Stream-copy recorders (copy mode) print no frame=/fps=, only size, time
    and speed, so `frame`/`fps` are absent for them and `time_s` (seconds of
    media written) is the progress figure instead.
    Returns None when the log is missing or carries no progress line.
    """
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    last = None
    for line in re.split(r"[\r\n]+", text):
        m = FFMPEG_PROGRESS_RE.search(line)
        if m:
            last = m
    if last is None:
        return None
    out = {"speed": float(last.group("speed"))}
    if last.group("frame") is not None:
        out["frame"] = int(last.group("frame"))
        out["fps"] = float(last.group("fps"))
    time_s = _ffmpeg_time_to_s(last.group("time"))
    if time_s is not None:
        out["time_s"] = time_s
    if last.group("size_kb") is not None:
        out["size_mb"] = round(int(last.group("size_kb")) / 1024.0, 2)
    if last.group("dup") is not None:
        out["dup"] = int(last.group("dup"))
        out["drop"] = int(last.group("drop"))
    return out


def _read_version_file() -> str | None:
    """Contents of the repository VERSION file (the rig release number), or None."""
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _pipeline_config() -> dict:
    """The knobs that define a test-matrix run, recorded next to its timings
    so every recording folder says how it was produced (docs/test-matrix.md).
    """
    record_enc = best_record_encoder_args()
    sync_enc = best_sync_encoder_args()
    cfg = {
        "version": _read_version_file(),
        "capture_res": f"{CAPTURE_WIDTH}x{CAPTURE_HEIGHT}",
        "target_fps": TARGET_FPS_WRITE,
        "cuda_usable": bool(_ffmpeg_has_usable_cuda()),
        "force_cuda": FORCE_CUDA_RECORD,
        "require_cuda": REQUIRE_CUDA_RECORD,
        "disable_cuda": DISABLE_CUDA_RECORD,
        "record_encoder": record_enc[record_enc.index("-c:v") + 1] if "-c:v" in record_enc else None,
        "record_decoder": "mjpeg_cuvid" if any("mjpeg_cuvid" in a for a in best_record_decode_args()) else "software",
        "sync_encoder": sync_enc[sync_enc.index("-c:v") + 1] if "-c:v" in sync_enc else None,
        "sync_decoder": _sync_decoder_name() if RECORD_MODE == "copy" else "software",
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
    }
    # Knobs that only exist on some branches (test/one-encode).
    for name in ("RECORD_MODE", "UNDISTORT_BACKEND", "REMAP_OVERSAMPLE", "KEEP_RAW",
                 "SYNC_PRESET", "SYNC_THREADS", "OUTPUT_RES", "PAUSE_PREVIEW_ON_STOP", "SYNC_DECODER"):
        if name in globals():
            # sync_decoder is the decoder that actually runs (set above);
            # the knob's own value goes under its own key.
            key = "sync_decoder_knob" if name == "SYNC_DECODER" else name.lower()
            cfg[key] = globals()[name]
    return cfg


def _run_stop_pipeline(recording_dir: Path, stop_started_at: float | None = None) -> dict:
    """Run sync -> post-process -> validate for a finished take and write
    recording_N/pipeline_timing.json.

    The timing file is the primary measurement for docs/test-matrix.md: wall
    seconds per stage, the total, the configuration that produced the take
    (_pipeline_config), the recorders' last ffmpeg progress line (drop /
    speed), the sync encodes' speed per camera, and the synced file sizes.
    Also logged as one summary line on rig.log. Returns the timing dict.
    """
    stages = {}
    t_total = time.time()
    if PAUSE_PREVIEW_ON_STOP:
        preview_pause_evt.set()
    try:
        t0 = time.time()
        sync_result = run_sync_on_dir(recording_dir)
        stages["sync_s"] = round(time.time() - t0, 3)

        t0 = time.time()
        processing = postprocess_recording_for_upload(recording_dir)
        stages["postprocess_s"] = round(time.time() - t0, 3)

        t0 = time.time()
        validation = validate_recording(recording_dir)
        stages["validate_s"] = round(time.time() - t0, 3)

        extra = _stop_pipeline_extra(recording_dir, sync_result, validation)
        if extra:
            stages.update(extra)
    finally:
        preview_pause_evt.clear()

    pipeline_s = round(time.time() - t_total, 3)
    now = time.time()
    timing = {
        "recording_dir": str(recording_dir.relative_to(BASE_DIR)) if recording_dir.is_relative_to(BASE_DIR) else str(recording_dir),
        "config": _pipeline_config(),
        "stages": stages,
        "pipeline_s": pipeline_s,
        # From the operator pressing Stop (stop_combined entry) to the end of
        # validation: what the UI actually waits for.
        "stop_to_ready_s": round(now - stop_started_at, 3) if stop_started_at else None,
        "sync_ok": bool((sync_result or {}).get("ok")),
        "sync_duration_s": (sync_result or {}).get("duration_s"),
        "sync_fps": (sync_result or {}).get("fps"),
        "sync_encode_wall_s": (sync_result or {}).get("encode_wall_s"),
        "undistort_step": next(
            (step for step in (processing or {}).get("steps", []) if step.get("name") in ("undistort_side", "skip_undistort")),
            None,
        ),
        "validation_status": (validation or {}).get("status"),
        "validation_usable": bool((validation or {}).get("usable")),
        "recorders": {},
        "sync_encodes": {},
        "sync_files_mb": {},
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    for cam in CAMERA_SOURCES.keys():
        rec_stats = _ffmpeg_log_stats(recording_dir / f"{cam}.log")
        if rec_stats:
            timing["recorders"][cam] = rec_stats
        sync_stats = _ffmpeg_log_stats(recording_dir / "sync" / f"{cam}_sync.log")
        if sync_stats:
            timing["sync_encodes"][cam] = sync_stats
        suffix = CAMERA_NAME_MAPPING.get(cam, cam)
        sync_file = recording_dir / "sync" / f"{cam}_sync_{suffix}.mp4"
        if sync_file.exists():
            timing["sync_files_mb"][cam] = round(sync_file.stat().st_size / (1024 * 1024), 2)

    try:
        (recording_dir / "pipeline_timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    except OSError as exc:
        rig_log.warning("[stop] could not write pipeline_timing.json: %s", exc)

    rig_log.info(
        "[stop] %s pipeline %.2fs (sync %.2fs, postprocess %.2fs, validate %.2fs) stop->ready %s s | "
        "recorders %s | sync speed %s | validation %s | cfg %s",
        recording_dir.name, pipeline_s, stages["sync_s"], stages["postprocess_s"], stages["validate_s"],
        timing["stop_to_ready_s"],
        {c: f"drop={v.get('drop', '-')} speed={v['speed']}x" for c, v in timing["recorders"].items()},
        {c: f"{v['speed']}x" for c, v in timing["sync_encodes"].items()},
        timing["validation_status"],
        {k: timing["config"][k] for k in ("capture_res", "record_encoder", "record_decoder", "sync_encoder", "cuda_usable")},
    )
    return timing


def _stop_pipeline_extra(recording_dir: Path, sync_result: dict, validation: dict) -> dict:
    """Post-validation work on this branch: drop the raw MJPEG copies once the
    take is synced and validated (copy mode only). Returns its stage timing.
    """
    t0 = time.time()
    _discard_raw_copies(recording_dir, validation)
    return {"discard_raw_s": round(time.time() - t0, 3)}


def postprocess_recording_for_upload(recording_dir: Path) -> dict:
    """Prepare the synced outputs for upload; writes processing_status.json.

    For sports that require calibration, the synced cam1 file is moved to
    distorted/ and replaced in sync/ by its undistorted version, so the
    file names the upload / download paths use stay the same. Other sports
    (or a missing cam1) just record a skip_undistort step. Never raises;
    failures land in the returned status['errors'].
    """
    status = {
        "ok": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "calibration": _calibration_summary(),
        "steps": [],
        "errors": [],
    }
    status_path = recording_dir / "processing_status.json"
    sync_dir = recording_dir / "sync"
    distorted_dir = recording_dir / "distorted"
    calibration_data = _load_calibration_json()

    try:
        synced_files = {
            cam: sync_dir / f"{cam}_sync_{CAMERA_NAME_MAPPING.get(cam, cam)}.mp4"
            for cam in CAMERA_SOURCES.keys()
            if (sync_dir / f"{cam}_sync_{CAMERA_NAME_MAPPING.get(cam, cam)}.mp4").exists()
        }
        if not synced_files:
            raise RuntimeError("Need at least 1 synchronized video for upload; found 0.")

        sync_status = _get_sync_status(recording_dir)
        fused = sync_status.get("undistort") or {}
        fused_ok = (
            bool(fused.get("applied"))
            and "cam1" in (sync_status.get("successful_cameras") or [])
        )

        if calibration_required() and "cam1" in synced_files and fused_ok:
            # The sync pass already undistorted cam1 (ffmpeg remap backend):
            # nothing to re-encode, and no distorted/ copy exists by design.
            status["steps"].append({
                "name": "undistort_side",
                "backend": fused.get("backend"),
                "oversample": fused.get("oversample"),
                "path": _rel(synced_files["cam1"]),
                "encoder": "sync_pass",
            })
        elif calibration_required() and "cam1" in synced_files and not calibration_data:
            raise RuntimeError("Calibration JSON is missing or invalid.")
        elif calibration_required() and "cam1" in synced_files:
            sync_side = synced_files["cam1"]
            distorted_side = distorted_dir / "cam1_sync_side.mp4"
            distorted_dir.mkdir(parents=True, exist_ok=True)
            if sync_side.exists():
                if distorted_side.exists():
                    distorted_side.unlink()
                shutil.move(str(sync_side), str(distorted_side))
                status["steps"].append({"name": "move_distorted_side", "path": _rel(distorted_side)})

            result = _undistort_video_file(distorted_side, sync_side, calibration_data)
            status["steps"].append({"name": "undistort_side", **result})
        else:
            reason = f"{current_sport()}_mode" if not calibration_required() else "cam1_sync_missing"
            status["steps"].append({"name": "skip_undistort", "reason": reason})

        for cam, path in sorted(synced_files.items()):
            status["steps"].append({"name": "keep_synced_video", "path": _rel(path)})

        status["ok"] = True
    except Exception as exc:
        status["errors"].append(str(exc))
    finally:
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")

    return status


def validate_recording(recording_dir: Path) -> dict:
    """Validate that a completed recording contains usable multimodal data."""
    report_path = recording_dir / "validation_report.json"
    checks = []
    modalities = {name: {"checks": []} for name in ("video", "ble", "heart_rate", "audio", "synchronization")}

    def add(modality, check_id, passed, required, message, details=None, warning=False):
        status = "passed" if passed else ("warning" if warning or not required else "failed")
        row = {
            "id": check_id,
            "status": status,
            "required": bool(required),
            "message": message,
        }
        if details is not None:
            row["details"] = details
        checks.append(row)
        modalities[modality]["checks"].append(row)
        return passed

    try:
        sync = _get_sync_status(recording_dir)
        sync_ok = bool(sync.get("ok"))
        duration = float(sync.get("duration_s") or 0.0)
        fps = float(sync.get("fps") or 0.0)
        add("synchronization", "manifest", sync_ok, True,
            "Synchronization manifest is valid" if sync_ok else "Synchronization manifest is missing or failed",
            {"duration_s": duration, "fps": fps, "warnings": sync.get("warnings", [])})
        add("synchronization", "minimum_duration", duration >= 1.0, True,
            f"Synchronized duration is {duration:.3f}s", {"minimum_s": 1.0})

        processing = _get_processing_status(recording_dir)
        add("synchronization", "postprocessing", bool(processing.get("ok")), True,
            "Video post-processing completed" if processing.get("ok") else "Video post-processing failed",
            {"errors": processing.get("errors", [])})

        video_meta = {}
        decoded_frames = {}
        for cam, semantic_name in CAMERA_NAME_MAPPING.items():
            raw_path = _raw_video_path(recording_dir, cam) or (recording_dir / f"{cam}.mp4")
            sync_path = recording_dir / "sync" / f"{cam}_sync_{semantic_name}.mp4"
            add("video", f"{cam}_raw_exists", raw_path.exists() and raw_path.stat().st_size > 1024, True,
                f"{cam} raw video is present" if raw_path.exists() and raw_path.stat().st_size > 1024 else f"{cam} raw video is missing or empty")
            exists = sync_path.exists() and sync_path.stat().st_size > 1024
            if not add("video", f"{cam}_sync_exists", exists, True,
                       f"{cam} synchronized video is present" if exists else f"{cam} synchronized video is missing or empty"):
                continue

            probe = _ffprobe_json([
                "-v", "error", "-select_streams", "v:0",
                "-show_entries", "format=duration:stream=width,height,avg_frame_rate,nb_frames",
                "-of", "json", str(sync_path),
            ])
            stream = (probe.get("streams") or [{}])[0]
            try:
                video_duration = float((probe.get("format") or {}).get("duration") or 0.0)
                frame_count = int(stream.get("nb_frames") or 0)
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                rate = stream.get("avg_frame_rate") or "0/1"
                num, den = (float(value) for value in rate.split("/"))
                video_fps = num / den if den else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                video_duration = frame_count = width = height = video_fps = 0
            valid_meta = video_duration > 0 and frame_count > 0 and width > 0 and height > 0 and video_fps > 0
            add("video", f"{cam}_metadata", valid_meta, True,
                f"{cam} video metadata is valid" if valid_meta else f"{cam} video metadata is invalid",
                {"duration_s": video_duration, "fps": video_fps, "frames": frame_count, "width": width, "height": height})
            if not valid_meta:
                continue
            video_meta[cam] = {"duration": video_duration, "fps": video_fps, "frames": frame_count, "size": [width, height]}

            cap = cv2.VideoCapture(str(sync_path))
            sample_stats = []
            for frame_index in sorted({0, max(0, frame_count // 2), max(0, frame_count - 2)}):
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = cap.read()
                if ok and frame is not None:
                    sample_stats.append({"frame": frame_index, "mean": float(frame.mean()), "std": float(frame.std())})
            cap.release()
            decoded_frames[cam] = sample_stats
            add("video", f"{cam}_decodable", len(sample_stats) == 3, True,
                f"{cam} beginning, middle and end frames decode" if len(sample_stats) == 3 else f"{cam} could decode only {len(sample_stats)}/3 sampled frames",
                sample_stats)
            abnormal = any(row["std"] < 1.0 or row["mean"] < 2.0 or row["mean"] > 253.0 for row in sample_stats)
            add("video", f"{cam}_visual_signal", not abnormal, False,
                f"{cam} sampled frames have usable visual signal" if not abnormal else f"{cam} may contain blank, dark or saturated frames",
                sample_stats, warning=abnormal)

        if len(video_meta) == 3:
            frame_counts = [row["frames"] for row in video_meta.values()]
            durations = [row["duration"] for row in video_meta.values()]
            frame_rates = [row["fps"] for row in video_meta.values()]
            sizes = [row["size"] for row in video_meta.values()]
            add("video", "matching_frame_counts", len(set(frame_counts)) == 1, True,
                "All synchronized videos have matching frame counts", {"values": frame_counts})
            tolerance = (1.0 / min(frame_rates)) + 0.01 if min(frame_rates) > 0 else 0.0
            add("video", "matching_durations", max(durations) - min(durations) <= tolerance, True,
                "All synchronized video durations match within one frame",
                {"values": durations, "tolerance_s": tolerance})
            add("video", "matching_fps", max(frame_rates) - min(frame_rates) < 0.01, True,
                "All synchronized videos have matching FPS", {"values": frame_rates})
            add("video", "matching_resolution", len({tuple(value) for value in sizes}) == 1, True,
                "All synchronized videos have matching resolution", {"values": sizes})

        ble_path = recording_dir / "sync" / "ble_sync.json"
        if add("ble", "file", ble_path.exists() and ble_path.stat().st_size > 2, True,
               "Synchronized BLE file is present" if ble_path.exists() else "Synchronized BLE file is missing"):
            try:
                ble_data = json.loads(ble_path.read_text(encoding="utf-8"))
                expected_hz = float(sync.get("ble_expected_frequency_hz") or 200.0)
                for side in ("Left", "Right"):
                    rows = ble_data.get(side) or []
                    add("ble", f"{side.lower()}_samples", bool(rows), True,
                        f"{side} contains {len(rows)} samples")
                    if not rows:
                        continue
                    times = [float(row.get("video_time_s")) for row in rows if row.get("video_time_s") is not None]
                    monotonic = len(times) == len(rows) and all(right >= left for left, right in zip(times, times[1:]))
                    add("ble", f"{side.lower()}_timestamps", monotonic, True,
                        f"{side} timestamps are valid and monotonic")
                    schema_ok = all(isinstance(row.get("Channels"), dict) and len(row["Channels"]) == 8 for row in rows)
                    add("ble", f"{side.lower()}_schema", schema_ok, True,
                        f"{side} samples contain eight channels" if schema_ok else f"{side} has malformed channel data")
                    if times and duration > 0:
                        coverage = max(0.0, min(1.0, (times[-1] - times[0]) / duration))
                        add("ble", f"{side.lower()}_coverage", coverage >= 0.95, True,
                            f"{side} covers {coverage * 100:.1f}% of synchronized video", {"minimum_percent": 95.0})
                        measured_hz = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
                        rate_ok = expected_hz * 0.75 <= measured_hz <= expected_hz * 1.25
                        add("ble", f"{side.lower()}_sample_rate", rate_ok, True,
                            f"{side} measured sample rate is {measured_hz:.1f}Hz",
                            {"expected_hz": expected_hz, "allowed_deviation_percent": 25})
                    if schema_ok:
                        channel_ranges = {
                            channel: [min(int(row["Channels"][channel]) for row in rows), max(int(row["Channels"][channel]) for row in rows)]
                            for channel in rows[0]["Channels"]
                        }
                        static_channels = [channel for channel, values in channel_ranges.items() if values[0] == values[1]]
                        add("ble", f"{side.lower()}_channel_activity", not static_channels, False,
                            f"{side} channels show changing data" if not static_channels else f"{side} has static channels: {', '.join(static_channels)}",
                            {"ranges": channel_ranges}, warning=bool(static_channels))
            except Exception as exc:
                add("ble", "parse", False, True, f"Synchronized BLE data is invalid: {exc}")

        heart_path = recording_dir / "sync" / "heart_rate_sync.jsonl"
        if not heart_path.exists():
            add("heart_rate", "file", False, False, "Synchronized heart-rate file is missing", warning=True)
        else:
            heart_rows = []
            invalid = 0
            for line in heart_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    heart_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    invalid += 1
            add("heart_rate", "parse", invalid == 0, False,
                f"Heart-rate file contains {len(heart_rows)} samples and {invalid} malformed lines", warning=invalid > 0)
            if not heart_rows:
                add("heart_rate", "samples", False, False, "No heart-rate samples fall inside the video window", warning=True)
            else:
                times = [float(row.get("video_time_s")) for row in heart_rows if row.get("video_time_s") is not None]
                bpm_values = [float(row["bpm"]) for row in heart_rows if row.get("bpm") is not None]
                valid = len(times) == len(heart_rows) and all(0 <= value <= duration + 1e-3 for value in times)
                add("heart_rate", "timestamps", valid, False, "Heart-rate timestamps align with the video", warning=not valid)
                plausible = bool(bpm_values) and all(30 <= value <= 240 for value in bpm_values)
                add("heart_rate", "bpm_range", plausible, False,
                    "Heart-rate values are physiologically plausible" if plausible else "Heart-rate values are missing or outside 30–240 BPM",
                    {"minimum": min(bpm_values) if bpm_values else None, "maximum": max(bpm_values) if bpm_values else None}, warning=not plausible)

        mic_expected = bool(sync.get("microphone_expected", bool(mic.assigned_camera_key)))
        wav_path = recording_dir / "audio" / "mic_capture.wav"
        if not wav_path.exists():
            add("audio", "file", False, mic_expected,
                "Microphone was assigned but its WAV file is missing" if mic_expected else "No microphone was assigned; audio is optional",
                warning=not mic_expected)
        else:
            try:
                with wave.open(str(wav_path), "rb") as wav_file:
                    channels = wav_file.getnchannels()
                    sample_width = wav_file.getsampwidth()
                    sample_rate = wav_file.getframerate()
                    frame_count = wav_file.getnframes()
                    audio_duration = frame_count / sample_rate if sample_rate else 0.0
                    raw = wav_file.readframes(min(frame_count, sample_rate * 30))
                add("audio", "decodable", frame_count > 0 and sample_rate > 0, mic_expected,
                    f"Audio is decodable ({audio_duration:.3f}s, {sample_rate}Hz, {channels} channel(s))")
                # A force-killed capture (deadlock, OOM, power cut) leaves the
                # header declaring the size of the FIRST write (~10 ms) while
                # the whole take sits in the file — and 512 frames passes a
                # bare frame_count > 0 test. Compare the declared data size
                # against the bytes actually on disk.
                declared_bytes = frame_count * sample_width * channels
                actual_bytes = max(0, wav_path.stat().st_size - 44)
                header_ok = actual_bytes <= declared_bytes + 4096
                est_duration = (
                    actual_bytes / (sample_width * channels * sample_rate)
                    if (sample_width and channels and sample_rate)
                    else 0.0
                )
                add("audio", "header_length", header_ok, mic_expected,
                    "WAV header length matches the file" if header_ok else
                    f"WAV header declares {audio_duration:.3f}s but the file holds ~{est_duration:.1f}s — "
                    "the capture was force-killed mid-write. The audio is intact and recoverable by "
                    "repairing the header's two length fields; do NOT delete this file",
                    {"declared_data_bytes": declared_bytes, "actual_data_bytes": actual_bytes})
                coverage_ok = duration <= 0 or audio_duration >= duration * 0.95
                add("audio", "coverage", coverage_ok, mic_expected,
                    f"Audio duration covers {(audio_duration / duration * 100) if duration else 0:.1f}% of synchronized video",
                    {"audio_duration_s": audio_duration, "video_duration_s": duration}, warning=not mic_expected and not coverage_ok)
                dtype = np.int16 if sample_width == 2 else (np.int32 if sample_width == 4 else None)
                signal_ok = False
                peak = 0
                if dtype is not None and raw:
                    samples = np.frombuffer(raw, dtype=dtype)
                    peak = int(np.max(np.abs(samples.astype(np.int64)))) if samples.size else 0
                    signal_ok = peak > 8
                add("audio", "signal", signal_ok, mic_expected,
                    "Audio contains a measurable signal" if signal_ok else "Audio appears silent or uses an unsupported sample width",
                    {"sample_width_bytes": sample_width, "peak": peak}, warning=not mic_expected and not signal_ok)
            except Exception as exc:
                add("audio", "decodable", False, mic_expected, f"Audio file is not decodable: {exc}", warning=not mic_expected)

        for modality in modalities.values():
            statuses = [row["status"] for row in modality["checks"]]
            modality["status"] = "failed" if "failed" in statuses else ("warning" if "warning" in statuses else "passed")
        counts = {status: sum(1 for row in checks if row["status"] == status) for status in ("passed", "warning", "failed")}
        usable = counts["failed"] == 0
        status = "usable" if usable and counts["warning"] == 0 else ("usable_with_warnings" if usable else "unusable")
        report = {
            "status": status,
            "usable": usable,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "recording_dir": str(recording_dir.relative_to(BASE_DIR)),
            "summary": counts,
            "modalities": modalities,
        }
    except Exception as exc:
        report = {
            "status": "unusable",
            "usable": False,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "recording_dir": str(recording_dir.relative_to(BASE_DIR)),
            "summary": {"passed": 0, "warning": 0, "failed": 1},
            "modalities": modalities,
            "validator_error": str(exc),
        }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def stop_recording_all(process_outputs: bool = True):
    """Stop every camera recorder and (optionally) run the post-stop pipeline.

    Clears is_recording_evt first so no route can start a second take,
    sends SIGINT (graceful mp4 finalisation) to each ffmpeg with a 15 s
    per-process grace before SIGKILL, closes the logs, then - when
    process_outputs - runs sync, post-processing and validation. Finally
    asks the preview threads to reconnect. stop_combined() passes
    process_outputs=False and runs the pipeline itself after the sensors
    have stopped.
    """
    global record_procs, recording_start_epoch, record_logs, current_recording_dir
    if not is_recording_evt.is_set():
        return

    is_recording_evt.clear()

    for cam_key, p in list(record_procs.items()):
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    p.terminate()
                else:
                    p.send_signal(signal.SIGINT)
            except Exception:
                pass

    for _, p in list(record_procs.items()):
        if p is None:
            continue
        # Per-camera grace: each encoder gets its own 15 s. The +faststart moov
        # rewrites run concurrently and compete for the disk, so a shared
        # wall-clock stamp lets one slow camera exhaust the budget and get the
        # others SIGKILLed mid-rewrite (corrupting their MP4s).
        t_end = time.time() + 15.0
        while time.time() < t_end:
            if p.poll() is not None:
                break
            time.sleep(0.05)
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    for _, logf in list(record_logs.items()):
        try:
            logf.flush()
            logf.close()
        except Exception:
            pass
    record_logs.clear()
    record_procs.clear()

    if process_outputs and current_recording_dir and current_recording_dir.exists():
        _run_stop_pipeline(current_recording_dir)

    for k in CAMERA_SOURCES.keys():
        reopen_capture_evts[k].set()


# ==================== BLE Backend ====================
# Two classes: InsoleDevice wraps one physical insole (a bleak client plus
# its sample buffers), BleCoordinator owns the pair, the asyncio loop they
# run on, side assignment and the per-take logging. Flask handlers only ever
# talk to the module-level `ble` coordinator; every coroutine is executed on
# the coordinator's loop thread via BleCoordinator._run().
class InsoleDevice:
    """One BLE pressure insole.

    Owns the bleak client, the notify handlers and three copies of the data:
      * data_buffer  - last 2000 decoded samples for the live UI
      * _raw_buffer  - every accepted sample for the take, as 28-byte records
                       (8-byte little-endian double host timestamp + the raw
                       20-byte packet); decoded by BleCoordinator.stop_logging
      * WAL file     - the same 28-byte records appended to disk as they
                       arrive (armed per take), so a crash loses <= ~1 s
    All buffer access is under _buffer_lock because the notify callbacks
    run on the BLE asyncio loop thread while Flask threads read/clear.
    Counters (packet_count, notify_count, crc_errors, ...) are plain ints
    read without the lock for status only.
    """
    def __init__(self, address: str, name: str):
        self.address = address
        self.name = name or "Unknown"
        self.client = None
        self.connected = False
        self.side = None

        self.battery_voltage = 0
        self.is_charging = False
        self.is_streaming = False
        self.frequency_code = 0x0C

        self.model_number = "--"
        self.manufacturer_name = "--"
        self.firmware_revision = "--"
        self.hardware_revision = "--"

        self.packet_count = 0
        self.notify_count = 0
        self.crc_errors = 0
        self.sample_count = 0
        self.start_time = 0
        self.last_packet_time = 0
        self.channel_data = [0] * 8
        self.data_buffer = deque(maxlen=2000)

        self._raw_buffer = bytearray()
        self._buffer_lock = Lock()

        # Write-ahead log: every accepted sample is appended to disk as it
        # arrives, so a crash/OOM/power cut costs at most ~1 s of tail instead
        # of the whole take. (Heart rate already works this way via its sidecar
        # JSONL; insoles were RAM-only until stop.)
        self._wal_file = None
        self._wal_path = None
        self._wal_last_fsync = 0.0
        self.wal_errors = 0

    def set_wal(self, path: Path):
        """Open (or replace) the write-ahead log file for the current take."""
        with self._buffer_lock:
            self._close_wal_locked()
            self._wal_path = path
            self._wal_file = open(path, "ab")
            self._wal_last_fsync = time.time()

    def _close_wal_locked(self):
        """Flush + fsync + close the WAL; caller holds _buffer_lock."""
        if self._wal_file:
            try:
                self._wal_file.flush()
                os.fsync(self._wal_file.fileno())
            except Exception:
                pass
            try:
                self._wal_file.close()
            except Exception:
                pass
            self._wal_file = None

    def close_wal(self):
        """Seal the WAL (flush/fsync/close) without deleting it."""
        with self._buffer_lock:
            self._close_wal_locked()

    def remove_wal(self):
        """Close and delete the WAL file (see note below on ordering)."""
        # Call only after the decoded JSON is durably on disk — the WAL is the
        # sole copy of this take's samples until then.
        with self._buffer_lock:
            self._close_wal_locked()
            if self._wal_path:
                try:
                    os.unlink(self._wal_path)
                except OSError:
                    pass
                self._wal_path = None

    async def connect(self):
        """Connect, subscribe to status notifications and read device info.

        Retries up to four times with increasing delays but only for BlueZ's
        transient 'InProgress' errors; any other failure aborts immediately.
        Returns True on success, False (after logging) on failure.
        """
        if self.connected and self.client:
            return True
        retries = [0.0, 0.8, 1.5, 2.5]
        last_err = None
        for delay in retries:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                if self.client:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                self.client = BleakClient(self.address, timeout=20.0, disconnected_callback=self._on_disconnect)
                await self.client.connect()
                self.connected = True
                await self.client.start_notify(UUID_STATUS_CHAR, self._handle_status)
                await self.read_device_info()
                return True
            except (BleakError, Exception) as e:
                last_err = e
                self.connected = False
                emsg = str(e)
                if "InProgress" in emsg or "Operation already in progress" in emsg:
                    continue
                break

        ble_logger.error("Connect failed %s: %s", self.address, last_err)
        return False

    def _on_disconnect(self, client):
        """bleak callback: mark the device offline so status/logging notice."""
        self.connected = False
        self.is_streaming = False

    async def disconnect(self):
        """Stop streaming (if active) and drop the BLE connection; never raises."""
        if self.client:
            try:
                if self.is_streaming:
                    await self.stop_stream()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.connected = False

    async def read_device_info(self):
        """Populate model / manufacturer / firmware / hardware from the standard
        Device Information Service; missing characteristics are ignored.
        """
        if not self.connected or not self.client:
            return
        for uuid_attr, field in [
            (UUID_MODEL_NUMBER, "model_number"),
            (UUID_MANUFACTURER_NAME, "manufacturer_name"),
            (UUID_FIRMWARE_REVISION, "firmware_revision"),
            (UUID_HARDWARE_REVISION, "hardware_revision"),
        ]:
            try:
                data = await self.client.read_gatt_char(uuid_attr)
                setattr(self, field, data.decode("utf-8", errors="ignore").strip("\x00"))
            except Exception:
                pass

    async def toggle_led(self):
        """Blink the insole's LED so the operator can tell Left from Right."""
        if self.connected and self.client:
            await self.client.write_gatt_char(UUID_CMD_CHAR, CMD_LED_TOGGLE, response=False)

    async def set_frequency(self, freq_cmd: bytes):
        """Send a sample-rate command (CMD_FREQ_*) and remember the new code."""
        if self.connected and self.client:
            await self.client.write_gatt_char(UUID_CMD_CHAR, freq_cmd, response=False)
            if freq_cmd == CMD_FREQ_10HZ:
                self.frequency_code = 0x0A
            elif freq_cmd == CMD_FREQ_100HZ:
                self.frequency_code = 0x0B
            elif freq_cmd == CMD_FREQ_200HZ:
                self.frequency_code = 0x0C

    async def start_stream(self, freq_cmd: bytes | None = None):
        """Reset all counters/buffers, optionally push a frequency, and subscribe
        to ADC notifications. The device starts streaming as soon as the
        notify is armed.
        """
        if not self.connected or not self.client:
            return
        self.packet_count = 0
        self.notify_count = 0
        self.crc_errors = 0
        self.sample_count = 0
        self.start_time = time.time()
        with self._buffer_lock:
            self._raw_buffer = bytearray()
        self.data_buffer.clear()
        if freq_cmd is not None:
            try:
                await self.client.write_gatt_char(UUID_CMD_CHAR, freq_cmd, response=False)
            except Exception:
                pass
            if freq_cmd == CMD_FREQ_10HZ:
                self.frequency_code = 0x0A
            elif freq_cmd == CMD_FREQ_100HZ:
                self.frequency_code = 0x0B
            elif freq_cmd == CMD_FREQ_200HZ:
                self.frequency_code = 0x0C
        # Arm ADC notify.
        await self.client.start_notify(UUID_ADC_CHAR, self._handle_adc_data)
        self.is_streaming = True

    async def wait_for_data(self, timeout_s: float = 1.2, poll_s: float = 0.05) -> bool:
        """Poll until at least one ADC notification has arrived (True) or timeout."""
        end = time.time() + max(0.1, timeout_s)
        while time.time() < end:
            if self.notify_count > 0 or self.packet_count > 0:
                return True
            await asyncio.sleep(poll_s)
        return False

    async def force_stream_rearm(self, freq_cmd: bytes | None = None):
        """Fallback sequence for firmware/stack variants that need a command kick + notify reset."""
        if not self.connected or not self.client:
            return
        cmd = freq_cmd
        if cmd is None:
            cmd = CMD_FREQ_200HZ
            if self.frequency_code == 0x0A:
                cmd = CMD_FREQ_10HZ
            elif self.frequency_code == 0x0B:
                cmd = CMD_FREQ_100HZ
        try:
            await self.client.write_gatt_char(UUID_CMD_CHAR, cmd, response=False)
        except Exception:
            pass
        try:
            await self.client.stop_notify(UUID_ADC_CHAR)
        except Exception:
            pass
        await self.client.start_notify(UUID_ADC_CHAR, self._handle_adc_data)
        self.is_streaming = True

    async def stop_stream(self):
        """Unsubscribe from ADC notifications; the device stops sending."""
        if not self.connected or not self.client:
            return
        try:
            await self.client.stop_notify(UUID_ADC_CHAR)
        except Exception:
            pass
        self.is_streaming = False

    def get_raw_data_and_clear(self):
        """Destructive read of the take buffer (used to discard pre-roll samples)."""
        with self._buffer_lock:
            out = bytes(self._raw_buffer)
            self._raw_buffer = bytearray()
        return out

    def get_raw_data(self):
        """Non-destructive copy of the take buffer (used to decode at stop)."""
        # Non-destructive read: stop_logging() decodes from this and clears
        # only AFTER the JSON is durably written. The old clear-then-serialize
        # order destroyed the RAM copy before anything reached disk, so any
        # exception in between (MemoryError, corrupt timestamp, disk full)
        # silently lost the take's pressure data.
        with self._buffer_lock:
            return bytes(self._raw_buffer)

    def clear_raw_data(self):
        """Drop the take buffer - only after its contents are durably on disk."""
        with self._buffer_lock:
            self._raw_buffer = bytearray()

    def _handle_status(self, sender, data):
        """STATUS_CHAR notify: [charging u8][streaming u8][battery mV u16 LE]
        [frequency code u8] ...
        """
        if len(data) < 8:
            return
        self.is_charging = bool(data[0])
        self.is_streaming = bool(data[1])
        self.battery_voltage = struct.unpack("<H", data[2:4])[0]
        self.frequency_code = data[4]

    def _handle_adc_data(self, sender, data):
        """ADC_CHAR notify: N back-to-back 20-byte packets (typically
        SAMPLES_PER_NOTIFY).

        Packet layout (little-endian): [device ms u16][8 x channel u16]
        [CRC-16 u16 over the first 18 bytes]. Packets failing the CRC are
        counted and dropped. Each accepted packet is stamped with the host
        clock (the value later aligned to video via video_time_s), appended
        to the live ring, the take buffer and the WAL. Runs on the BLE loop
        thread at up to 200 Hz / SAMPLES_PER_NOTIFY notifies per second, so
        it must stay cheap.
        """
        if len(data) < SINGLE_SAMPLE_SIZE or len(data) % SINGLE_SAMPLE_SIZE != 0:
            return

        num_samples = len(data) // SINGLE_SAMPLE_SIZE
        host_ts = time.time()
        self.notify_count += 1
        with self._buffer_lock:
            wal_chunk = bytearray()
            for i in range(num_samples):
                packet = data[i * SINGLE_SAMPLE_SIZE : (i + 1) * SINGLE_SAMPLE_SIZE]
                payload = packet[:18]
                received_crc = struct.unpack("<H", packet[18:20])[0]
                calculated_crc = calculate_crc16(payload)

                if received_crc != calculated_crc:
                    self.crc_errors += 1
                    continue

                timestamp_ms = struct.unpack("<H", packet[0:2])[0]
                channels = struct.unpack("<8H", packet[2:18])
                self.packet_count += 1
                self.sample_count += 1
                self.last_packet_time = host_ts
                self.channel_data = list(channels)
                self.data_buffer.append(
                    {
                        "timestamp": host_ts,
                        "dev_ts": timestamp_ms,
                        "channels": channels,
                        "packet_id": self.packet_count,
                    }
                )
                record = struct.pack("<d", host_ts) + packet
                self._raw_buffer.extend(record)
                wal_chunk.extend(record)

            if self._wal_file and wal_chunk:
                try:
                    self._wal_file.write(wal_chunk)
                    self._wal_file.flush()
                    # fsync on a ~1 s cadence only: this callback runs on the
                    # BLE asyncio loop thread, and per-notify fsync latency
                    # would drop notifications. flush() to the page cache is
                    # cheap; fsync bounds the loss window to about a second.
                    if host_ts - self._wal_last_fsync >= 1.0:
                        os.fsync(self._wal_file.fileno())
                        self._wal_last_fsync = host_ts
                except Exception:
                    self.wal_errors += 1


class BleCoordinator:
    """Owns the insole pair and everything Flask needs to drive it.

    Responsibilities: device pool + BLE scan, Left/Right assignment,
    connect/stream/frequency control, per-take logging (WAL + JSON dump)
    and the status snapshot. Runs a private asyncio loop on a daemon thread;
    public methods are synchronous wrappers that submit the matching
    coroutine with _run() and block with a timeout. _lock guards the pool
    and assignment fields against concurrent Flask threads; the loop thread
    itself is single-threaded so coroutines do not take it.

    Lifecycle per take: start_logging(rec_dir, idx) freezes the assignment
    map, discards pre-roll samples and arms the WALs; stop_logging() seals
    the WALs, decodes the buffers into ble/insole_log_recording_<idx>.json
    (atomic write), and only then clears buffers and deletes the WALs.
    recover_ble_wals() replays any WAL left behind by a crash at startup.
    """
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.devices = {}  # address -> InsoleDevice
        self.left_address = None
        self.right_address = None
        self.discovered = []
        self.streaming = False
        self.target_frequency_label = "200Hz"
        self.target_frequency_cmd = CMD_FREQ_200HZ

        self.logging_active = False
        self.current_log_file = None
        self.recording_assignments = {}

        self._lock = Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        """Thread body: run the private asyncio loop forever."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout=30):
        """Execute `coro` on the BLE loop from a Flask thread and wait for it.
        Raises concurrent.futures.TimeoutError after `timeout` seconds.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _device(self, address: str):
        """InsoleDevice for a MAC address, or None."""
        return self.devices.get(address)

    def _active_stream_count(self) -> int:
        """Number of connected devices that have delivered at least one packet."""
        # Count only devices that have actually delivered stream data in this session.
        return sum(
            1
            for d in self.devices.values()
            if d.connected and (d.notify_count > 0 or d.packet_count > 0)
        )

    def _stream_targets(self):
        """Connected devices to (re)arm, Left then Right first, then any others."""
        targets = []
        seen = set()
        for addr in [self.left_address, self.right_address]:
            if not addr or addr in seen:
                continue
            d = self._device(addr)
            if d and d.connected:
                targets.append(d)
                seen.add(addr)
        for d in self.devices.values():
            if d.address in seen:
                continue
            if d.connected:
                targets.append(d)
                seen.add(d.address)
        return targets

    async def _scan(self):
        """5 s BLE discovery; returns [{name, address, rssi}, ...]."""
        result = []
        devices = await BleakScanner.discover(timeout=5.0)
        for d in devices:
            name = d.name or f"BLE Device {d.address}"
            result.append({"name": name, "address": d.address, "rssi": getattr(d, "rssi", None)})
        return result

    def scan(self):
        """Blocking BLE scan; caches the result in `discovered` for snapshot()."""
        rows = self._run(self._scan(), timeout=20)
        with self._lock:
            self.discovered = rows
        return rows

    def add_device(self, address: str, name: str):
        """Add an insole to the pool (no connection yet); idempotent per address."""
        with self._lock:
            if address in self.devices:
                return self.devices[address]
            dev = InsoleDevice(address, name)
            self.devices[address] = dev
            return dev

    def remove_device(self, address: str):
        """Disconnect and drop a device, clearing any side it was assigned to."""
        dev = self._device(address)
        if not dev:
            return
        try:
            self._run(dev.disconnect(), timeout=15)
        except Exception:
            pass
        with self._lock:
            self.devices.pop(address, None)
            if self.left_address == address:
                self.left_address = None
            if self.right_address == address:
                self.right_address = None

    def assign_side(self, address: str, side: str):
        """Assign a pooled device to 'Left' or 'Right', evicting whatever was
        there before. Raises ValueError for unknown device / bad side.
        """
        side = side.title()
        if side not in ("Left", "Right"):
            raise ValueError("side must be Left or Right")

        dev = self._device(address)
        if not dev:
            raise ValueError("device not found")

        with self._lock:
            # A device can occupy only one side. Clear its previous slot when it
            # is reassigned, as well as any previous device on the target side.
            if self.left_address == address and side != "Left":
                self.left_address = None
            if self.right_address == address and side != "Right":
                self.right_address = None

            if side == "Left":
                if self.left_address and self.left_address != address:
                    old = self._device(self.left_address)
                    if old:
                        old.side = None
                self.left_address = address
            else:
                if self.right_address and self.right_address != address:
                    old = self._device(self.right_address)
                    if old:
                        old.side = None
                self.right_address = address
            dev.side = side

    def validate_assignments(self):
        """Enforce the recording precondition: exactly two connected insoles,
        one assigned Left and one Right, no extras. Returns
        {'Left': addr, 'Right': addr} or raises ValueError listing every
        problem (the message is shown verbatim to the operator).
        """
        with self._lock:
            connected = [d for d in self.devices.values() if d.connected]
            errors = []

            if len(connected) != 2:
                errors.append(f"exactly two connected soles are required (found {len(connected)})")

            left = self._device(self.left_address) if self.left_address else None
            right = self._device(self.right_address) if self.right_address else None
            if not left or not left.connected or left.side != "Left":
                errors.append("assign one connected sole to Left")
            if not right or not right.connected or right.side != "Right":
                errors.append("assign one connected sole to Right")
            if self.left_address and self.left_address == self.right_address:
                errors.append("Left and Right must be different devices")

            assigned = {self.left_address, self.right_address}
            unassigned = [d.address for d in connected if d.address not in assigned]
            if unassigned:
                errors.append("connected sole(s) without a unique side: " + ", ".join(unassigned))

            if errors:
                raise ValueError("Invalid sole assignment: " + "; ".join(errors))

            return {"Left": self.left_address, "Right": self.right_address}

    async def _connect_all(self):
        """Connect pooled devices one at a time with a 1 s settle between them
        (parallel connects make the adapter flaky). Never raises; per-device
        outcomes are in the returned results list.
        """
        # Match app14: connect one-by-one and let the adapter settle between devices.
        results = []
        devices = list(self.devices.values())
        for index, d in enumerate(devices):
            entry = {"address": d.address, "name": d.name, "ok": False, "error": None}
            try:
                ok = await d.connect()
                entry["ok"] = bool(ok)
                if not ok:
                    entry["error"] = "connect returned False"
            except Exception as e:
                entry["ok"] = False
                entry["error"] = str(e)
            results.append(entry)
            if index < len(devices) - 1:
                await asyncio.sleep(1.0)

        connected = sum(1 for r in results if r["ok"])
        return {
            "requested": len(results),
            "connected": connected,
            "connected_all": connected == len(results) if results else True,
            "results": results,
        }

    def connect_all(self):
        """Blocking wrapper for _connect_all()."""
        return self._run(self._connect_all(), timeout=45)

    async def _disconnect_all(self):
        """Disconnect every pooled device concurrently, ignoring errors."""
        tasks = [d.disconnect() for d in self.devices.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def disconnect_all(self):
        """Blocking wrapper for _disconnect_all(); always clears `streaming`."""
        try:
            self._run(self._disconnect_all(), timeout=30)
        finally:
            self.streaming = False

    async def _toggle_led(self, address: str):
        """Blink one device's LED; ValueError if unknown / not connected."""
        d = self._device(address)
        if not d:
            raise ValueError("device not found")
        if not d.connected:
            raise ValueError("device is not connected")
        await d.toggle_led()

    def toggle_led(self, address: str):
        """Blocking wrapper for _toggle_led()."""
        self._run(self._toggle_led(address), timeout=10)

    async def _set_frequency(self, cmd: bytes):
        """Push a frequency command to every connected device concurrently."""
        tasks = [d.set_frequency(cmd) for d in self.devices.values() if d.connected]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def set_frequency(self, label: str):
        """Set the target sample rate ('10Hz' | '100Hz' | '200Hz') for all
        devices and remember it for later stream (re)arms.
        """
        cmd = FREQ_MAP.get(label, CMD_FREQ_200HZ)
        self.target_frequency_label = label
        self.target_frequency_cmd = cmd
        self._run(self._set_frequency(cmd), timeout=15)

    async def _start_stream(self):
        """Arm streaming on every connected device and confirm data flows.

        Push the target frequency, arm ADC notifications on all targets as
        close together as possible, wait 1 s, re-arm any device that stayed
        silent (force_stream_rearm), wait again, then report which devices
        delivered packets ('started') and which did not ('skipped').
        """
        started = []
        already = []
        skipped = []
        targets = self._stream_targets()

        if not targets:
            for d in self.devices.values():
                if not d.connected:
                    skipped.append({"address": d.address, "name": d.name, "reason": "not_connected"})
            return {"started": started, "already_streaming": already, "skipped": skipped}

        # Match app14 behavior more closely: push the selected frequency to all connected devices,
        # then arm ADC notifications for both sides as close together as possible.
        await self._set_frequency(self.target_frequency_cmd)
        await asyncio.gather(*(d.start_stream() for d in targets), return_exceptions=True)

        await asyncio.sleep(1.0)

        for d in targets:
            got_data = (d.notify_count > 0 or d.packet_count > 0)
            if not got_data:
                await d.force_stream_rearm(freq_cmd=self.target_frequency_cmd)

        await asyncio.sleep(1.2)

        for d in targets:
            if d.notify_count > 0 or d.packet_count > 0:
                started.append({"address": d.address, "name": d.name})
            else:
                try:
                    await d.stop_stream()
                except Exception:
                    pass
                skipped.append({"address": d.address, "name": d.name, "reason": "no_notify_data"})

        return {"started": started, "already_streaming": already, "skipped": skipped}

    async def _measure_device_rates(self, duration_s: float = 2.0, poll_s: float = 0.05):
        """Sample packet_count over `duration_s` and return per-device Hz."""
        targets = self._stream_targets()
        start_counts = {d.address: int(d.packet_count or 0) for d in targets}
        start_t = time.time()
        end_t = start_t + max(0.5, duration_s)
        while time.time() < end_t:
            await asyncio.sleep(poll_s)
        elapsed = max(0.001, time.time() - start_t)
        rates = []
        for d in targets:
            before = start_counts.get(d.address, 0)
            after = int(d.packet_count or 0)
            hz = (after - before) / elapsed
            rates.append({"address": d.address, "name": d.name, "hz": hz, "packets": after - before})
        return rates

    def _expected_hz(self) -> float:
        """Nominal sample rate implied by the current target frequency command."""
        if self.target_frequency_cmd == CMD_FREQ_10HZ:
            return 10.0
        if self.target_frequency_cmd == CMD_FREQ_100HZ:
            return 100.0
        return 200.0

    async def _validate_stream_rates(self, duration_s: float = 2.0, retries: int = 1):
        """Check every streaming device delivers >= 85% of the expected rate;
        re-arm slow devices and retry up to `retries` times.
        """
        expected = self._expected_hz()
        min_hz = expected * 0.85

        for attempt in range(retries + 1):
            rates = await self._measure_device_rates(duration_s=duration_s)
            low = [r for r in rates if r["hz"] < min_hz]
            if not low:
                return {"ok": True, "expected_hz": expected, "min_hz": min_hz, "rates": rates, "attempts": attempt + 1}

            if attempt >= retries:
                return {"ok": False, "expected_hz": expected, "min_hz": min_hz, "rates": rates, "low": low, "attempts": attempt + 1}

            low_addrs = {r["address"] for r in low}
            for d in self._stream_targets():
                if d.address in low_addrs:
                    await d.force_stream_rearm(freq_cmd=self.target_frequency_cmd)
                    await asyncio.sleep(0.2)

        return {"ok": False, "expected_hz": expected, "min_hz": min_hz, "rates": [], "low": [], "attempts": retries + 1}

    async def _wait_for_any_packets(self, timeout_s: float = 2.5, poll_s: float = 0.05) -> bool:
        """True as soon as any connected device has received a packet."""
        end = time.time() + max(0.2, timeout_s)
        while time.time() < end:
            for d in self.devices.values():
                if d.connected and (d.packet_count > 0 or d.notify_count > 0):
                    return True
            await asyncio.sleep(poll_s)
        return False

    def start_streaming(self):
        """Validate assignments, arm streaming and require at least one device
        to actually deliver data; raises ValueError otherwise. Called both
        from the UI (/api/ble/start_stream) and by start_combined().
        """
        self.validate_assignments()
        if self.streaming:
            # Recover from stale state after disconnect/reconnect cycles.
            if self._active_stream_count() == 0:
                self.streaming = False
            else:
                # Re-arm anyway to avoid false-positive streaming state.
                self.streaming = False

        detail = self._run(self._start_stream(), timeout=35)
        self.streaming = self._active_stream_count() > 0
        if not self.streaming:
            raise ValueError("Connected BLE devices found, but no ADC notifications were received")
        return detail

    def wait_for_any_packets(self, timeout_s: float = 2.5) -> bool:
        """Blocking wrapper for _wait_for_any_packets()."""
        return bool(self._run(self._wait_for_any_packets(timeout_s=timeout_s), timeout=max(5, int(timeout_s) + 3)))

    def validate_stream_rates(self, duration_s: float = 2.0, retries: int = 1):
        """Blocking wrapper for _validate_stream_rates()."""
        timeout = max(10, int(duration_s * (retries + 1)) + 8)
        return self._run(self._validate_stream_rates(duration_s=duration_s, retries=retries), timeout=timeout)

    async def _stop_stream(self):
        """Unsubscribe ADC notifications on every streaming device."""
        tasks = [d.stop_stream() for d in self.devices.values() if d.connected and d.is_streaming]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop_streaming(self):
        """Stop all streams (no-op when not streaming); always clears `streaming`."""
        if not self.streaming:
            return
        try:
            self._run(self._stop_stream(), timeout=20)
        finally:
            self.streaming = False

    def start_logging(self, rec_dir: Path, rec_idx: int):
        """Open the recording window for a take: freeze the L/R map, discard
        pre-roll samples, persist the assignment map and arm one WAL per
        device under rec_dir/ble/. Raises ValueError (via
        validate_assignments) if the pair is not ready.
        """
        # Freeze the mapping for this recording. Later UI changes must not
        # relabel data that was captured under the original assignment.
        self.recording_assignments = self.validate_assignments()

        # BLE streaming may be started from its standalone UI control before
        # the video recording begins. Notifications are buffered continuously,
        # so discard those pre-recording samples here. stop_logging() also
        # considers connected/unassigned devices, therefore clear every device
        # rather than only the currently assigned Left/Right pair.
        discarded_samples = 0
        for dev in self.devices.values():
            discarded_samples += len(dev.get_raw_data_and_clear()) // 28

        ble_dir = rec_dir / "ble"
        ble_dir.mkdir(parents=True, exist_ok=True)

        # Persist the frozen L/R assignment map BEFORE any samples land. It
        # otherwise lives only in RAM and dies with a crash, so WAL recovery
        # could not tell which foot a WAL belongs to.
        assignments_path = ble_dir / f"assignments_recording_{rec_idx}.json"
        try:
            assignments_path.write_text(json.dumps(self.recording_assignments), encoding="utf-8")
        except Exception as exc:
            ble_logger.error("Failed to persist assignment map: %s", exc)

        # Arm a per-device WAL. Per-device files are required: the 28-byte
        # frame carries no device identity, so a shared file could not be
        # attributed on recovery. Armed only from start_logging, so the WAL is
        # never polluted with pre-roll from the free-running stream.
        for dev in self.devices.values():
            try:
                dev.set_wal(ble_dir / f"wal_recording_{rec_idx}_{dev.address.replace(':', '-')}.bin")
            except Exception as exc:
                ble_logger.error("Failed to arm WAL for %s: %s", dev.address, exc)

        self.current_log_file = ble_dir / f"insole_log_recording_{rec_idx}.json"
        self.logging_active = True
        if discarded_samples:
            ble_logger.warning("Discarded %d pre-recording samples", discarded_samples)

    def stop_logging(self):
        """Close the recording window and write ble/insole_log_recording_N.json.

        Decodes each device's 28-byte records into rows bucketed by the
        FROZEN assignment (Left / Right / Unassigned), writes the JSON
        atomically (tmp + fsync + replace) and only then clears buffers and
        removes the WALs. Returns a summary dict with ok/path/entry counts;
        ok=False with a message when nothing was captured.
        """
        self.logging_active = False
        if not self.current_log_file:
            return {"ok": False, "message": "No target file"}

        # Seal the WALs first so their tails are flushed before decode.
        for dev in self.devices.values():
            dev.close_wal()

        assignments = dict(self.recording_assignments)
        output = {"Assignments": assignments, "Left": [], "Right": [], "Unassigned": []}
        packet_id = 0
        devices_to_dump = []
        seen = set()

        # Prefer explicit Left/Right ordering first.
        for address in [assignments.get("Left"), assignments.get("Right")]:
            if not address or address in seen:
                continue
            dev = self._device(address)
            if not dev:
                continue
            devices_to_dump.append(dev)
            seen.add(address)

        # Also include any other connected devices so data is not lost when side assignment is missing.
        for dev in self.devices.values():
            if dev.address in seen:
                continue
            if dev.connected or dev.is_streaming or dev.packet_count > 0:
                devices_to_dump.append(dev)
                seen.add(dev.address)

        side_by_address = {address: side for side, address in assignments.items()}
        for dev in devices_to_dump:
            # Decode from a non-destructive copy — buffers and WALs are cleared
            # only after the JSON is durably on disk (below), so a failure
            # anywhere in between leaves the data recoverable.
            raw_data = dev.get_raw_data()
            if not raw_data:
                continue

            record_size = 28
            num_records = len(raw_data) // record_size
            recorded_side = side_by_address.get(dev.address)
            bucket = recorded_side if recorded_side in ("Left", "Right") else "Unassigned"
            for i in range(num_records):
                offset = i * record_size
                record = raw_data[offset : offset + record_size]
                timestamp = struct.unpack("<d", record[0:8])[0]
                dev_ts = struct.unpack("<H", record[8:10])[0]
                channels = struct.unpack("<8H", record[10:26])

                packet_id += 1
                entry = {
                    "Timestamp": datetime.fromtimestamp(timestamp).isoformat(),
                    "Device_TS_ms": dev_ts,
                    "Packet_ID": packet_id,
                    "Device_Address": dev.address,
                    "Device_Name": dev.name,
                    "Device_Side": recorded_side,
                    "Channels": {f"Ch{j}": int(val) for j, val in enumerate(channels)},
                }
                output[bucket].append(entry)

        total_entries = len(output["Left"]) + len(output["Right"]) + len(output["Unassigned"])
        if total_entries == 0:
            # Nothing captured — the empty WALs carry nothing worth recovering.
            for dev in self.devices.values():
                dev.remove_wal()
            return {"ok": False, "message": "No BLE data to save"}

        # Durable write: temp file + fsync + atomic replace, so a crash mid-dump
        # can never leave a half-written JSON masquerading as the take's log.
        # (indent dropped — a ten-minute two-sole take is ~240k entries and the
        # pretty-printed string roughly doubles the stop-time memory spike.)
        tmp_path = self.current_log_file.with_name(self.current_log_file.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.current_log_file)

        # JSON is durable — now the RAM copies and WALs can go.
        for dev in self.devices.values():
            dev.clear_raw_data()
            dev.remove_wal()

        stat = self.current_log_file.stat()
        return {
            "ok": True,
            "path": str(self.current_log_file.relative_to(BASE_DIR)),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "entries": total_entries,
            "left_entries": len(output["Left"]),
            "right_entries": len(output["Right"]),
            "unassigned_entries": len(output["Unassigned"]),
        }

    def snapshot(self):
        """Status payload for /api/ble/status and /status: pool, per-device
        counters and live channel values, assignment, streaming/logging flags.
        """
        with self._lock:
            discovered = list(self.discovered)
            device_rows = []
            for d in self.devices.values():
                device_rows.append(
                    {
                        "name": d.name,
                        "address": d.address,
                        "connected": d.connected,
                        "side": d.side,
                        "battery_voltage": d.battery_voltage,
                        "is_charging": d.is_charging,
                        "is_streaming": d.is_streaming,
                        "frequency_code": d.frequency_code,
                        "packet_count": d.packet_count,
                        "notify_count": d.notify_count,
                        "crc_errors": d.crc_errors,
                        "wal_errors": d.wal_errors,
                        "sample_count": d.sample_count,
                        "samples_per_notify": SAMPLES_PER_NOTIFY,
                        "last_packet_time": d.last_packet_time,
                        "channels": d.channel_data,
                        "device_info": {
                            "model_number": d.model_number,
                            "manufacturer_name": d.manufacturer_name,
                            "firmware_revision": d.firmware_revision,
                            "hardware_revision": d.hardware_revision,
                        },
                    }
                )

            connected_count = sum(1 for d in self.devices.values() if d.connected)
            actively_streaming = sum(1 for d in self.devices.values() if d.is_streaming)
            total_packets = sum(int(d.packet_count or 0) for d in self.devices.values())
            return {
                "streaming": self.streaming,
                "logging_active": self.logging_active,
                "left_address": self.left_address,
                "right_address": self.right_address,
                "discovered": discovered,
                "devices": device_rows,
                "pool_count": len(self.devices),
                "connected_count": connected_count,
                "streaming_devices": actively_streaming,
                "total_packets": total_packets,
                "session_dir": str(self.session_dir.relative_to(BASE_DIR)),
            }


def recover_ble_wals(sessions_root: Path) -> int:
    """Decode orphaned insole WALs left behind by a crash into their JSON logs.

    A WAL survives only when the app died between samples arriving and
    stop_logging()'s durable write. Each WAL lives inside its own recording's
    ble/ folder next to the persisted assignment map, so the decoded JSON lands
    in the correct (old) take even though SESSION_DIR has since moved on.
    Returns the number of recovered log files.
    """
    recovered = 0
    if not sessions_root.exists():
        return 0
    try:
        wal_paths = sorted(sessions_root.glob("session_*/recording_*/ble/wal_recording_*.bin"))
    except OSError:
        return 0

    groups = {}  # (ble_dir, rec_idx) -> [(address, wal_path), ...]
    for wal_path in wal_paths:
        parts = wal_path.stem.split("_")  # wal_recording_{idx}_{AA-BB-...}
        if len(parts) < 4:
            continue
        rec_idx, address = parts[2], "_".join(parts[3:]).replace("-", ":")
        groups.setdefault((wal_path.parent, rec_idx), []).append((address, wal_path))

    for (ble_dir, rec_idx), members in groups.items():
        json_path = ble_dir / f"insole_log_recording_{rec_idx}.json"
        if json_path.exists():
            # The dump succeeded and only the cleanup was lost — WALs are stale.
            for _, wal_path in members:
                try:
                    os.unlink(wal_path)
                except OSError:
                    pass
            continue

        assignments = {}
        assignments_path = ble_dir / f"assignments_recording_{rec_idx}.json"
        if assignments_path.exists():
            try:
                assignments = json.loads(assignments_path.read_text(encoding="utf-8")) or {}
            except Exception:
                assignments = {}
        side_by_address = {address: side for side, address in assignments.items()}

        output = {
            "Assignments": assignments,
            "Left": [],
            "Right": [],
            "Unassigned": [],
            "Recovered_From_WAL": True,
        }
        packet_id = 0
        for address, wal_path in members:
            try:
                raw_data = wal_path.read_bytes()
            except OSError:
                continue
            recorded_side = side_by_address.get(address)
            bucket = recorded_side if recorded_side in ("Left", "Right") else "Unassigned"
            record_size = 28
            for i in range(len(raw_data) // record_size):
                record = raw_data[i * record_size : (i + 1) * record_size]
                timestamp = struct.unpack("<d", record[0:8])[0]
                dev_ts = struct.unpack("<H", record[8:10])[0]
                channels = struct.unpack("<8H", record[10:26])
                packet_id += 1
                output[bucket].append(
                    {
                        "Timestamp": datetime.fromtimestamp(timestamp).isoformat(),
                        "Device_TS_ms": dev_ts,
                        "Packet_ID": packet_id,
                        "Device_Address": address,
                        "Device_Name": address,
                        "Device_Side": recorded_side,
                        "Channels": {f"Ch{j}": int(val) for j, val in enumerate(channels)},
                    }
                )

        total_entries = len(output["Left"]) + len(output["Right"]) + len(output["Unassigned"])
        if total_entries:
            try:
                tmp_path = json_path.with_name(json_path.name + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(output, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, json_path)
                recovered += 1
                ble_logger.warning("Recovered %d insole samples from WAL -> %s", total_entries, json_path)
            except Exception as exc:
                ble_logger.error("WAL recovery failed for %s: %s", json_path, exc)
                continue  # keep the WALs for a manual pass
        for _, wal_path in members:
            try:
                os.unlink(wal_path)
            except OSError:
                pass
    return recovered


# ---- Subsystem singletons. Created at import so the routes can reference
# them directly; start_new_session() retargets their session_dir in place.
ble = BleCoordinator(SESSION_DIR)
mic = MicCaptureManager(
    base_dir=BASE_DIR,
    camera_bootstrap=CAMERA_BOOTSTRAP,
    ssh_user=CAMERA_SSH_USER,
    script_path=BASE_DIR / "remote_inmp441_capture.py",
    initial_camera_key=MIC_CAMERA_KEY,
)

# ---------------------------------------------------------------------------
# Direct-upload worker (docs/direct-upload-design.md §3). Configured via env:
#   FORGEON_API_URL      e.g. https://api-dev-new.forgelabs.in/dev
#   FORGEON_DEVICE_TOKEN the device token minted by POST /rig/devices
# Unset -> the /api/upload_* routes answer 503 and nothing else changes.
from upload_worker import UploadWorker

# Credentials resolution order (docs §1b + phase-5 pairing):
#   1. env FORGEON_API_URL + FORGEON_DEVICE_TOKEN (debug override)
#   2. BASE_DIR/rig_device.json — written by the /pair flow, owned by the app.
# Neither present -> not paired: upload routes 503, /pair page offers pairing.
RIG_DEVICE_FILE = BASE_DIR / "rig_device.json"
DEFAULT_FORGEON_API_URL = "https://api-dev-new.forgelabs.in/dev"


def _load_rig_credentials():
    """Cloud credentials per the resolution order above, or None when the rig
    is not paired. The returned dict feeds UploadWorker and /api/pairing/status.
    """
    api = os.environ.get("FORGEON_API_URL", "").strip()
    tok = os.environ.get("FORGEON_DEVICE_TOKEN", "").strip()
    if api and tok:
        return {"api_url": api, "device_token": tok, "source": "env"}
    try:
        if RIG_DEVICE_FILE.is_file():
            data = json.loads(RIG_DEVICE_FILE.read_text(encoding="utf-8"))
            if data.get("api_url") and data.get("device_token"):
                data["source"] = "file"
                return data
    except Exception:
        logging.getLogger("rig.upload").exception("Could not read %s", RIG_DEVICE_FILE)
    return None


upload_worker = None
_rig_device_info = _load_rig_credentials()
if _rig_device_info:
    upload_worker = UploadWorker(BASE_DIR, _rig_device_info["api_url"], _rig_device_info["device_token"])
else:
    logging.getLogger("rig.upload").info(
        "Not paired: no env credentials and no %s — pair via /pair", RIG_DEVICE_FILE.name
    )
# Heart-rate strap: HeartbeatManager launches and talks to a sidecar process;
# the sidecar is started in __main__ and stopped at exit.
heartbeat = HeartbeatManager(base_dir=BASE_DIR, session_dir=SESSION_DIR)
atexit.register(heartbeat.stop_sidecar)


# ==================== Combined Control ====================
def start_new_session():
    """Roll over to a fresh sessions/session_<ts>/ folder.

    Refuses (RuntimeError) while a take, insole logging or an HR snippet is
    active. Reassigns the SESSION_* / CALIBRATION_* globals, resets the
    recording counter and retargets the BLE / heartbeat managers without
    disconnecting their devices. A manually started continuous HR session
    is closed in the old folder and reopened in the new one. Calibration
    does NOT carry over - a new session needs a new (or re-uploaded)
    calibration for wide-angle sports.
    """
    global SESSION_TIMESTAMP, SESSION_DIR, CALIBRATION_DIR, CALIBRATION_JSON, CALIBRATION_NPZ
    global camera_backend_logs_dir, recording_index, recording_start_epoch, current_recording_dir

    with session_state_lock:
        if is_recording_evt.is_set():
            raise RuntimeError("Stop the active recording before starting a new session.")
        if ble.logging_active:
            raise RuntimeError("Stop active BLE recording before starting a new session.")
        if heartbeat.current_snippet is not None:
            raise RuntimeError("Stop the active heart-rate recording snippet before starting a new session.")

        # Preserve a manually started continuous heart-rate session across the
        # rollover, while closing its old-session slice in the correct folder.
        heartbeat_session_was_active = bool(heartbeat.session_active)
        if heartbeat_session_was_active:
            heartbeat.stop_session()

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        new_dir = BASE_DIR / "sessions" / f"session_{timestamp}"
        suffix = 1
        while new_dir.exists():
            new_dir = BASE_DIR / "sessions" / f"session_{timestamp}_{suffix:02d}"
            suffix += 1
        new_dir.mkdir(parents=True, exist_ok=False)

        SESSION_TIMESTAMP = timestamp
        SESSION_DIR = new_dir
        CALIBRATION_DIR = SESSION_DIR / "calibration"
        CALIBRATION_JSON = CALIBRATION_DIR / "calibration_cam1.json"
        CALIBRATION_NPZ = CALIBRATION_DIR / "calibration_cam1.npz"
        camera_backend_logs_dir = SESSION_DIR / "camera_bootstrap"

        recording_index = 0
        recording_start_epoch = None
        current_recording_dir = None
        record_procs.clear()
        record_logs.clear()

        # Retarget session-scoped managers without disconnecting their devices.
        ble.session_dir = SESSION_DIR
        heartbeat.session_dir = SESSION_DIR
        heartbeat.sidecar_log_path = SESSION_DIR / "heartbeat_sidecar.log"
        heartbeat.last_session_file = None
        heartbeat.last_snippet_file = None
        if heartbeat_session_was_active:
            heartbeat.start_session()

        return {
            "ok": True,
            "session": SESSION_DIR.name,
            "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
            "recording_index": recording_index,
            "heartbeat_session_continued": heartbeat_session_was_active,
        }


def start_combined(capture_ble: bool = True):
    """Start a take across every modality; returns the per-subsystem outcome.

    Preconditions (RuntimeError): a sport is selected and, if it needs it,
    calibration exists. Insoles are best-effort: if they cannot stream the
    take proceeds without them and ble['ok'] is False. Mic and HR snippet
    are started for the upcoming recording_N folder, insole logging is
    opened, and finally the camera recorders are spawned; if that last step
    fails everything started here is rolled back and the error re-raised.
    Caller must hold session_state_lock.
    """
    if is_recording_evt.is_set():
        return {"ok": False, "message": "Recording already running"}
    if not sport_selected():
        raise RuntimeError("Select a sport before recording.")
    if calibration_required() and not calibration_available():
        raise RuntimeError("Calibration required before recording. Capture calibration photos or upload a valid calibration JSON.")

    ble_result = {"ok": True, "message": "BLE skipped"}
    ble_active = False
    if capture_ble:
        try:
            ble.validate_assignments()
            if not ble.streaming:
                detail = ble.start_streaming()
            else:
                detail = {
                    "started": [],
                    "already_streaming": [{"side": "manager", "reason": "ble_stream_already_on"}],
                    "skipped": [],
                }
            if not ble.wait_for_any_packets(timeout_s=2.5):
                raise RuntimeError(
                    "BLE stream was armed but no packet notifications arrived within 2.5s. "
                    "Check BLE status counters and device stream mode."
                )
            ble_result = {
                "ok": True,
                "message": "BLE streaming active and packets detected",
                "ble_streaming": ble.streaming,
                "detail": detail,
            }
            ble_active = True
        except Exception as e:
            ble_result = {
                "ok": False,
                "message": f"BLE unavailable, recording without insoles: {e}",
                "ble_streaming": False,
            }

    mic_result = {"ok": True, "skipped": True, "message": "Mic capture skipped; no camera assigned"}
    next_recording_index = recording_index + 1
    next_recording_dir = SESSION_DIR / f"recording_{next_recording_index}"
    mic_result = mic.start_for_recording(next_recording_dir, next_recording_index)
    heartbeat_result = heartbeat.start_snippet(next_recording_dir, next_recording_index)

    # Begin the BLE recording window before launching FFmpeg. The synchronized
    # derivative is trimmed later to the cameras' actual common wall-clock
    # interval, ensuring sensor data brackets frame zero instead of starting late.
    try:
        if ble_active:
            ble.start_logging(next_recording_dir, next_recording_index)
            ble_result["ble_logging"] = ble.logging_active
        start_recording_all()
    except Exception:
        if ble.logging_active:
            ble.stop_logging()
        mic.stop_for_recording(next_recording_dir)
        heartbeat.stop_snippet(next_recording_dir)
        raise

    return {
        "ok": True,
        "recording_index": recording_index,
        "recording_dir": str(current_recording_dir.relative_to(BASE_DIR)) if current_recording_dir else None,
        "calibration": _calibration_summary(),
        "ble": ble_result,
        "mic": mic_result,
        "heartbeat": heartbeat_result,
    }


def stop_combined():
    """Stop a take across every modality and run the post-stop pipeline.

    Order matters: cameras stop first (fast), then insole stream/log, HR
    snippet and mic, so sensor data extends past the last video frame.
    Only then are run_sync_on_dir / postprocess / validate run (slow).
    Sensor failures are captured into *_error / *_warning fields rather
    than raised, so a flaky strap never leaves the rig stuck in the
    recording state. The returned dict is what /api/stop_recording answers
    with. Caller must hold session_state_lock.
    """
    if not is_recording_evt.is_set():
        return {"ok": False, "message": "Recording not running"}

    stop_started_at = time.time()
    ble_log = None
    ble_stop_error = None
    mic_log = None
    mic_stop_error = None
    heartbeat_log = None
    heartbeat_stop_error = None
    heartbeat_warning = None

    # Finalize the raw camera files first. BLE and heart-rate capture remain
    # active during this short shutdown so their data brackets the video end.
    # Expensive synchronization/post-processing runs only after sensors stop.
    stop_recording_all(process_outputs=False)

    try:
        ble.stop_streaming()
    except Exception as e:
        log_exception(ble_logger, "BLE stream stop failed during stop_combined")
        ble_stop_error = str(e)
    try:
        if ble.logging_active:
            ble_log = ble.stop_logging()
    except Exception as e:
        log_exception(ble_logger, "BLE logging stop failed during stop_combined")
        message = str(e)
        ble_stop_error = f"{ble_stop_error}; {message}" if ble_stop_error else message

    try:
        heartbeat_log = heartbeat.stop_snippet(current_recording_dir)
        if heartbeat_log and not heartbeat_log.get("ok") and not heartbeat_log.get("skipped"):
            heartbeat_stop_error = heartbeat_log.get("message") or "Heartbeat snippet stop failed"
        elif heartbeat_log and not heartbeat_log.get("skipped") and heartbeat_log.get("sample_count") == 0:
            # The manager's ok flag does not gate on sample count, so a take
            # with zero HR samples otherwise reports full success. Surface it
            # as a distinct warning — NOT as an error, which would flag every
            # deliberate no-strap take as a rig failure.
            heartbeat_warning = (
                "Heartbeat capture returned 0 samples for this take "
                "(strap off, out of range, or sidecar restarted)"
            )
    except Exception as e:
        log_exception(logging.getLogger("rig.heartbeat"), "Heartbeat snippet stop failed during stop_combined")
        heartbeat_stop_error = str(e)

    try:
        mic_log = mic.stop_for_recording(current_recording_dir)
        if mic_log and not mic_log.get("ok") and not mic_log.get("skipped"):
            mic_stop_error = "; ".join(mic_log.get("errors") or [mic_log.get("message") or "Mic stop failed"])
    except Exception as e:
        log_exception(logging.getLogger("rig.mic"), "Mic stop failed during stop_combined")
        mic_stop_error = str(e)

    timing = None
    if current_recording_dir and current_recording_dir.exists():
        timing = _run_stop_pipeline(current_recording_dir, stop_started_at)

    out = {
        "ok": True,
        "timing": timing,
        "recording_index": recording_index,
        "recording_dir": str(current_recording_dir.relative_to(BASE_DIR)) if current_recording_dir else None,
        "files": _get_recording_files_info(current_recording_dir) if current_recording_dir else {},
        "ble_files": _get_ble_logs_info(current_recording_dir) if current_recording_dir else {},
        "audio_files": _get_audio_files_info(current_recording_dir) if current_recording_dir else {},
        "heartbeat_files": _get_heartbeat_files_info(current_recording_dir) if current_recording_dir else {},
        "processing": _get_processing_status(current_recording_dir) if current_recording_dir else {},
        "sync": _get_sync_status(current_recording_dir) if current_recording_dir else {},
        "validation": _get_validation_report(current_recording_dir) if current_recording_dir else {},
        "calibration": _calibration_summary(),
        "ble_log": ble_log,
        "mic_log": mic_log,
        "heartbeat_log": heartbeat_log,
    }
    if ble_stop_error:
        out["ble_error"] = ble_stop_error
    if mic_stop_error:
        out["mic_error"] = mic_stop_error
    if heartbeat_stop_error:
        out["heartbeat_error"] = heartbeat_stop_error
    if heartbeat_warning:
        out["heartbeat_warning"] = heartbeat_warning
    return out



# ==================== Routes (UI) ====================
# Legacy operator UI: these render/redirect the single production template
# and use form POSTs. The JSON twins under /api/ are what the newer
# browser-based flow and the admin app call.
@app.route("/")
def index():
    """Landing page: /pair until paired, sport picker until a sport is chosen,
    then calibration or recording depending on the sport.
    """
    if not rig_paired():
        return redirect("/pair")
    if not sport_selected():
        return render_template("index35_cam_sole.html", **ui_template_context("sport_select"))
    return redirect(url_for("recording_page" if (not calibration_required() or calibration_available()) else "calibration_page"))


@app.route("/select_sport", methods=["POST"])
def select_sport_route():
    """Form POST from the sport picker; refused (409) mid-take."""
    global selected_sport
    if is_recording_evt.is_set():
        return "Stop the current recording before switching sport.", 409
    body = request.get_json(silent=True) or {}
    sport = (request.form.get("sport") or body.get("sport") or "").strip().lower()
    if sport not in SPORT_OPTIONS:
        return render_template(
            "index35_cam_sole.html",
            sport_error="Select a valid sport.",
            **ui_template_context("sport_select"),
        ), 400
    selected_sport = sport
    return redirect(url_for("recording_page" if not calibration_required() or calibration_available() else "calibration_page"))


@app.route("/calibration")
def calibration_page():
    """Calibration view (only reachable for sports that require it)."""
    if not sport_selected():
        return redirect(url_for("index"))
    if not calibration_required():
        return redirect(url_for("recording_page"))
    return render_template("index35_cam_sole.html", **ui_template_context("calibration"))


@app.route("/recording")
def recording_page():
    """Main recording view; redirects back through the gates it depends on."""
    if not rig_paired():
        return redirect("/pair")
    if not sport_selected():
        return redirect(url_for("index"))
    if calibration_required() and not calibration_available():
        return redirect(url_for("calibration_page"))
    return render_template("index35_cam_sole.html", **ui_template_context("recording"))


@app.route("/video_feed/<cam_key>")
def video_feed(cam_key):
    """Live MJPEG preview stream for one camera (see gen_frames)."""
    if cam_key not in CAMERA_SOURCES:
        return "Unknown camera", 404
    return Response(gen_frames(cam_key), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/focus/<cam_key>")
def focus_check(cam_key):
    """Focus score for one camera; ?target=cube|board."""
    target = request.args.get("target", "cube").strip().lower()
    payload, status_code = focus_payload_for_camera(cam_key, target)
    return jsonify(payload), status_code


@app.route("/focus/all")
def focus_all():
    """Focus scores for every camera; overall status is the worst camera's."""
    target = request.args.get("target", "cube").strip().lower()
    if target not in ("cube", "board"):
        return jsonify({"error": "focus target must be cube or board"}), 400
    out = {}
    status_code = 200
    for cam_key in CAMERA_SOURCES:
        payload, cam_status = focus_payload_for_camera(cam_key, target)
        out[cam_key] = payload
        if cam_status >= 500:
            status_code = max(status_code, cam_status)
    return jsonify(out), status_code


@app.route("/focus/reset", methods=["POST"])
def focus_reset():
    """Forget the per-camera best focus score (after moving the cube / re-aiming).

    Body {"camera": "cam1"} for one camera, empty for all.
    """
    body = request.get_json(silent=True) or {}
    cam_key = body.get("camera") or request.args.get("camera")
    target = (body.get("target") or request.args.get("target") or "cube").strip().lower()
    if FOCUS_TRACKER is None:
        return jsonify({"status": "error", "message": FOCUS_MEASURE_ERROR}), 503
    tracker_key = f"{cam_key}:{target}" if cam_key else None
    return jsonify({"status": "ok", "best": FOCUS_TRACKER.reset(tracker_key)}), 200


@app.route("/lens/<cam_key>/move", methods=["POST"])
def lens_move_route(cam_key):
    """Body {size: 'coarse'|'fine'|'<int>', direction: 'in'|'out'} -> lens_move()."""
    if cam_key not in CAMERA_SOURCES:
        payload, status_code = lens_move(cam_key, 0, False)
        return jsonify(payload), status_code

    body = request.get_json(silent=True) or {}
    size = body.get("size")
    if size == "coarse":
        steps = LENS_STEP_COARSE
    elif size == "fine":
        steps = LENS_STEP_FINE
    elif isinstance(size, str):
        try:
            steps = int(size)
        except ValueError:
            steps = 0
    else:
        steps = 0

    direction = body.get("direction")
    if size not in ("coarse", "fine") and (not isinstance(size, str) or steps <= 0):
        message = "size must be coarse, fine, or a positive integer string"
        lens_last_error[cam_key] = message
        return jsonify({
            "status": "error",
            "message": message,
            "position": lens_position[cam_key],
        }), 400
    if direction not in ("in", "out"):
        message = "direction must be in or out"
        lens_last_error[cam_key] = message
        return jsonify({
            "status": "error",
            "message": message,
            "position": lens_position[cam_key],
        }), 400

    payload, status_code = lens_move(cam_key, steps, direction == "in")
    return jsonify(payload), status_code


@app.route("/lens/<cam_key>/reset", methods=["POST"])
def lens_reset_route(cam_key):
    """Declare the current motor position as zero (no motor movement)."""
    if cam_key not in CAMERA_SOURCES:
        return jsonify({"status": "error", "message": "invalid camera"}), 404
    with lens_locks[cam_key]:
        lens_position[cam_key] = 0
        lens_last_error[cam_key] = None
    return jsonify({"status": "ok", "position": 0}), 200


@app.route("/lens/status")
def lens_status_route():
    """Software position / busy flag / last error for every camera's lens motor."""
    return jsonify({
        cam_key: {
            "position": lens_position[cam_key],
            "busy": lens_locks[cam_key].locked(),
            "last_error": lens_last_error[cam_key],
        }
        for cam_key in CAMERA_SOURCES
    }), 200


@app.route("/start_recording", methods=["POST"])
def start_recording_route():
    """Legacy form POST: start a take (BLE included) and bounce to the page."""
    if not rig_paired():
        return "This rig is not paired with Forgeon yet - open /pair first.", 403
    try:
        with session_state_lock:
            start_combined(capture_ble=True)
    except Exception as e:
        return f"Failed to start combined recording: {e}", 500
    return redirect(url_for("recording_page"))


@app.route("/stop_recording", methods=["POST"])
def stop_recording_route():
    """Legacy form POST: stop the take and bounce to the recording page."""
    try:
        with session_state_lock:
            stop_combined()
    except Exception as e:
        return f"Failed to stop combined recording: {e}", 500
    return redirect(url_for("recording_page"))


@app.route("/new_session", methods=["POST"])
def new_session_route():
    """Legacy form POST twin of /api/new_session."""
    try:
        start_new_session()
    except RuntimeError as exc:
        return str(exc), 409
    except Exception as exc:
        return f"Failed to start a new session: {exc}", 500
    destination = "calibration_page" if calibration_required() and not calibration_available() else "recording_page"
    return redirect(url_for(destination))


@app.route("/capture_photos", methods=["POST"])
def capture_photos_route():
    """Legacy form POST: snapshot all cameras (calibration image capture)."""
    try:
        capture_photos_all()
        return redirect(url_for("calibration_page" if calibration_required() else "recording_page"))
    except Exception as e:
        return f"Failed to capture photos: {e}", 500


@app.route("/media/<path:filepath>", methods=["GET"])
def media_file(filepath):
    """Serve any file under BASE_DIR inline (previews, snaps, synced videos)."""
    try:
        # is_relative_to, not startswith: a bare string-prefix check admits
        # sibling paths that share the directory-name prefix (e.g.
        # <BASE_DIR>_backup), which a %2e%2e-encoded request can reach.
        file_path = (BASE_DIR / filepath).resolve()
        if not file_path.is_relative_to(BASE_DIR.resolve()):
            return jsonify({"status": "error", "message": "Invalid file path"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404
        # file_path is fully resolved, so send_from_directory receives a parent
        # with no ".." components left for its own safe_join to mis-handle.
        return send_from_directory(str(file_path.parent), file_path.name, as_attachment=False)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/status")
def status():
    """The big status payload the UI polls: recording flags, sport, calibration,
    camera bootstrap health, insole / mic / heartbeat snapshots.
    """
    camera_status = {cam_key: _camera_health_payload(cam_key) for cam_key in CAMERA_SOURCES}
    return jsonify(
        {
            "recording": is_recording_evt.is_set(),
            "recording_start_epoch": recording_start_epoch,
            "target_fps": TARGET_FPS_WRITE,
            "preview_fps": current_preview_fps(),
            "jpeg_quality": current_jpeg_quality(),
            "app_use_case": current_sport(),
            "sport_selected": sport_selected(),
            "sport_options": sport_options_for_template(),
            "calibration_required": calibration_required(),
            "ffmpeg_in_path": bool(shutil.which("ffmpeg")),
            "focus_measure_available": FOCUS_MEASURE_AVAILABLE,
            "focus_measure_error": FOCUS_MEASURE_ERROR,
            "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
            "log_dir": str((SESSION_DIR / "logs").relative_to(BASE_DIR)),
            "camera_bootstrap_enabled": CAMERA_BOOTSTRAP_ENABLED,
            "camera_bootstrap": _camera_backend_snapshot(),
            "camera_status": camera_status,
            "calibration": _calibration_summary(),
            "ble": ble.snapshot(),
            "mic": mic.snapshot(include_remote=False),
            "heartbeat": heartbeat.session_status(),
        }
    )


@app.route("/api/calibration/status", methods=["GET"])
def api_calibration_status():
    """Calibration progress (snap rows, usable count, summary, preview pair)."""
    return jsonify(_calibration_status_payload()), 200


@app.route("/api/calibration/capture", methods=["POST"])
def api_calibration_capture():
    """Snapshot all cameras, run the detection-only pass, return status."""
    try:
        manifest = capture_photos_all()
        detection = _run_detection_preview()
        payload = _calibration_status_payload()
        payload["capture"] = manifest
        payload["detection"] = detection
        return jsonify(payload), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/snapshots", methods=["POST"])
def api_snapshots():
    """One JPEG snapshot from every camera; no calibration side effects.

    For the admin Calibration tab: unlike /api/calibration/capture this does
    NOT run the legacy detection/calibration preview - it only captures
    (ffmpeg first, preview-frame fallback) and returns the manifest. Fetch
    the files via /media/<path>.
    """
    try:
        return jsonify(capture_photos_all()), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/calibration/run", methods=["POST"])
def api_calibration_run():
    """Solve intrinsics from this session's snaps (400 if too few usable)."""
    try:
        result = _run_session_calibration()
        payload = _calibration_status_payload()
        payload["result"] = result
        return jsonify(payload), 200
    except SystemExit as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/calibration/upload_json", methods=["POST"])
def api_calibration_upload_json():
    """Install a calibration JSON: multipart 'file', {path} under BASE_DIR, or
    the JSON body itself.
    """
    try:
        if request.files.get("file"):
            data = json.loads(request.files["file"].read().decode("utf-8"))
        else:
            body = request.get_json(silent=True) or {}
            if body.get("path"):
                src = (BASE_DIR / body["path"]).resolve()
                if not src.is_relative_to(BASE_DIR.resolve()):
                    return jsonify({"status": "error", "message": "Invalid calibration JSON path"}), 403
                data = json.loads(src.read_text(encoding="utf-8"))
            else:
                data = body
        saved = _save_uploaded_calibration_json(data)
        payload = _calibration_status_payload()
        payload["result"] = saved
        return jsonify(payload), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400



# ==================== Routes (API Camera+Combined) ====================
@app.route("/api/start_recording", methods=["POST"])
def api_start_recording():
    """Start a take. Distinct statuses for the gates: not_paired (403),
    sport_required / calibration_required (400), already_recording (200).
    """
    if not rig_paired():
        return jsonify({
            "status": "not_paired",
            "message": "This rig is not paired with Forgeon. Open localhost:5000/pair on the rig and enter a code from the admin Rig devices page.",
        }), 403
    try:
        with session_state_lock:
            if is_recording_evt.is_set():
                return jsonify({"status": "already_recording", "recording_index": recording_index}), 200
            if not sport_selected():
                return jsonify(
                    {
                        "status": "sport_required",
                        "message": "Select a sport before recording.",
                        "sport_options": sport_options_for_template(),
                    }
                ), 400
            if calibration_required() and not calibration_available():
                return jsonify(
                    {
                        "status": "calibration_required",
                        "message": "Calibration required before recording. Capture photos or upload a valid calibration JSON.",
                        "calibration": _calibration_summary(),
                    }
                ), 400
            result = start_combined(capture_ble=True)
        return jsonify(
            {
                "status": "recording_started",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **result,
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/new_session", methods=["POST"])
def api_new_session():
    """Roll over to a new session folder (409 while anything is recording)."""
    try:
        result = start_new_session()
        return jsonify({"status": "success", **result}), 200
    except RuntimeError as exc:
        return jsonify({"status": "recording_active", "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/stop_recording", methods=["POST"])
def api_stop_recording():
    """Stop the take and return the full stop_combined() report."""
    try:
        with session_state_lock:
            if not is_recording_evt.is_set():
                return jsonify({"status": "not_recording", "message": "No recording in progress"}), 200
            result = stop_combined()
        return jsonify(
            {
                "status": "recording_stopped",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **result,
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/get_recording_files/<int:rec_index>", methods=["GET"])
def api_get_recording_files(rec_index):
    """Everything known about one take: synced files, sensor files, sync and
    validation reports.
    """
    try:
        rec_dir = SESSION_DIR / f"recording_{rec_index}"
        if not rec_dir.exists():
            return jsonify({"status": "not_found", "message": f"recording_{rec_index} not found"}), 404
        files = _get_recording_files_info(rec_dir)
        ble_files = _get_ble_logs_info(rec_dir)
        audio_files = _get_audio_files_info(rec_dir)
        heartbeat_files = _get_heartbeat_files_info(rec_dir)
        return jsonify(
            {
                "status": "success",
                "recording_dir": str(rec_dir.relative_to(BASE_DIR)),
                "recording_index": rec_index,
                "files": files,
                "ble_files": ble_files,
                "audio_files": audio_files,
                "heartbeat_files": heartbeat_files,
                "sync": _get_sync_status(rec_dir),
                "validation": _get_validation_report(rec_dir),
                "calibration": _calibration_summary(),
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/validate_recording/<int:rec_index>", methods=["POST"])
def api_validate_recording(rec_index):
    """Re-run validate_recording() on a finished take."""
    try:
        with session_state_lock:
            if is_recording_evt.is_set() and rec_index == recording_index:
                return jsonify({"status": "recording_active", "message": "Stop the recording before validating it."}), 409
            rec_dir = SESSION_DIR / f"recording_{rec_index}"
            if not rec_dir.exists():
                return jsonify({"status": "not_found", "message": f"recording_{rec_index} not found"}), 404
            report = validate_recording(rec_dir)
        return jsonify({"status": "success", "validation": report}), 200
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/list_recordings", methods=["GET"])
def api_list_recordings():
    """All takes in the current session with their file / report summaries."""
    try:
        recordings = []
        if SESSION_DIR.exists():
            for item in sorted(SESSION_DIR.iterdir()):
                if item.is_dir() and item.name.startswith("recording_"):
                    try:
                        rec_index = int(item.name.split("_")[1])
                    except ValueError:
                        continue
                    recordings.append(
                        {
                            "index": rec_index,
                            "dir": str(item.relative_to(BASE_DIR)),
                            "files": _get_recording_files_info(item),
                            "ble_logs": _get_ble_logs_info(item),
                            "audio_files": _get_audio_files_info(item),
                            "heartbeat_files": _get_heartbeat_files_info(item),
                            "processing": _get_processing_status(item),
                            "sync": _get_sync_status(item),
                            "validation": _get_validation_report(item),
                            "calibration": _calibration_summary(),
                        }
                    )

        return jsonify(
            {
                "status": "success",
                "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
                "heartbeat_session_files": heartbeat.session_files_info(),
                "total_recordings": len(recordings),
                "recordings": recordings,
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/camera/status", methods=["GET"])
def api_camera_status():
    """Bootstrap + preview health for every camera."""
    return jsonify(
        {
            "status": "success",
            "enabled": CAMERA_BOOTSTRAP_ENABLED,
            "ssh_user": CAMERA_SSH_USER,
            "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
            "cameras": {cam_key: _camera_health_payload(cam_key) for cam_key in CAMERA_SOURCES},
        }
    )



# ==================== Mic API ====================
# Thin wrappers over the MicCaptureManager singleton (`mic`).
@app.route("/api/mic/status", methods=["GET"])
def api_mic_status():
    """Mic assignment + remote capture state."""
    return jsonify({"status": "success", **mic.snapshot(include_remote=True)}), 200


@app.route("/api/mic/assign", methods=["POST"])
def api_mic_assign():
    """Choose which camera Pi hosts the mic ({camera_key} or null to unassign)."""
    data = request.get_json(silent=True) or {}
    camera_key = data.get("camera_key")
    if camera_key is not None:
        camera_key = str(camera_key).strip().lower() or None
    try:
        payload = mic.assign(camera_key)
        return jsonify({"status": "success", **payload}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/mic/onset", methods=["GET"])
def api_mic_onset():
    """Detected audio onset of the last take (404 if none)."""
    payload = mic.onset()
    status_code = 200 if payload.get("ok") else 404
    return jsonify({"status": "success" if payload.get("ok") else "not_found", **payload}), status_code


@app.route("/api/mic/waveform", methods=["GET"])
def api_mic_waveform():
    """Downsampled waveform of the last take's WAV (404 if none)."""
    try:
        payload = mic.waveform()
        status_code = 200 if payload.get("ok") else 404
        return jsonify({"status": "success" if payload.get("ok") else "not_found", **payload}), status_code
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/mic/live_waveform", methods=["GET"])
def api_mic_live_waveform():
    """Short live waveform from the remote mic for level checks."""
    try:
        payload = mic.live_waveform()
        status_code = 200 if payload.get("ok") else 404
        return jsonify({"status": "success" if payload.get("ok") else "not_found", **payload}), status_code
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



# ==================== Heartbeat API ====================
# Thin wrappers over the HeartbeatManager singleton (`heartbeat`). 502 means
# the sidecar process did not answer.
@app.route("/api/heartbeat/status", methods=["GET"])
def api_heartbeat_status():
    """Sidecar / device / session state."""
    return jsonify({"status": "success", **heartbeat.session_status()}), 200


@app.route("/api/heartbeat/service/start", methods=["POST"])
def api_heartbeat_service_start():
    """(Re)start the heart-rate sidecar process."""
    try:
        return jsonify({"status": "success", **heartbeat.start_sidecar()}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/heartbeat/hr", methods=["GET"])
def api_heartbeat_hr():
    """Latest BPM sample from the sidecar."""
    try:
        return jsonify({"status": "success", **heartbeat.latest()}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": "Heartbeat service unavailable", "message": str(e)}), 502


@app.route("/api/heartbeat/device", methods=["GET"])
def api_heartbeat_device():
    """Currently connected strap, if any."""
    try:
        return jsonify({"status": "success", **heartbeat.device()}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": "Heartbeat service unavailable", "message": str(e)}), 502


@app.route("/api/heartbeat/devices", methods=["POST"])
def api_heartbeat_devices():
    """Scan for straps (refused mid-take: scanning interrupts notifications)."""
    if is_recording_evt.is_set():
        return jsonify({
            "status": "recording_active",
            "message": "Recording in progress; a BLE device scan suspends heart-rate "
                       "notifications and would punch a hole in this take's HR data. "
                       "Stop the recording first.",
        }), 409
    try:
        return jsonify({"status": "success", **heartbeat.devices()}), 200
    except Exception as e:
        log_exception(logging.getLogger("rig.heartbeat"), "Heartbeat device scan failed")
        return jsonify({"status": "error", "detail": "Heartbeat device scan failed", "message": str(e)}), 502


@app.route("/api/heartbeat/connect", methods=["POST"])
def api_heartbeat_connect():
    """Connect to a strap ({device} address/name, or the sidecar's default)."""
    data = request.get_json(silent=True) or {}
    device = data.get("device")
    if device is not None:
        device = str(device).strip() or None
    try:
        return jsonify({"status": "success", **heartbeat.connect_device(device)}), 200
    except Exception as e:
        log_exception(logging.getLogger("rig.heartbeat"), "Heartbeat device connect failed")
        return jsonify({"status": "error", "detail": "Heartbeat device connect failed", "message": str(e)}), 502


@app.route("/api/heartbeat/disconnect", methods=["POST"])
def api_heartbeat_disconnect():
    """Disconnect the strap."""
    try:
        return jsonify({"status": "success", **heartbeat.disconnect_device()}), 200
    except Exception as e:
        log_exception(logging.getLogger("rig.heartbeat"), "Heartbeat device disconnect failed")
        return jsonify({"status": "error", "detail": "Heartbeat device disconnect failed", "message": str(e)}), 502


@app.route("/api/heartbeat/session/start", methods=["POST"])
def api_heartbeat_session_start():
    """Start a continuous session-long HR log (independent of takes)."""
    try:
        return jsonify({"status": "success", **heartbeat.start_session()}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/heartbeat/session/stop", methods=["POST"])
def api_heartbeat_session_stop():
    """Stop the continuous HR log."""
    try:
        return jsonify({"status": "success", **heartbeat.stop_session()}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400



# ==================== BLE API ====================
# Insole control. Several handlers refuse (409 recording_active) while a take
# is running because they would silently corrupt that take's pressure log -
# the reasons are spelled out inline where it matters.
@app.route("/api/ble/status", methods=["GET"])
def api_ble_status():
    """BleCoordinator.snapshot()."""
    return jsonify({"status": "success", **ble.snapshot()})


@app.route("/api/ble/scan", methods=["POST"])
def api_ble_scan():
    """5 s discovery of nearby BLE devices."""
    try:
        rows = ble.scan()
        return jsonify({"status": "success", "count": len(rows), "devices": rows}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/add_device", methods=["POST"])
def api_ble_add_device():
    """Add {address, name} to the insole pool."""
    data = request.get_json(silent=True) or {}
    address = data.get("address")
    name = data.get("name") or "Unknown"
    if not address:
        return jsonify({"status": "error", "message": "address is required"}), 400

    try:
        ble.add_device(address, name)
        return jsonify({"status": "success", "message": "Device added"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/remove_device", methods=["POST"])
def api_ble_remove_device():
    """Remove a device from the pool (refused mid-take)."""
    # Removing a device mid-take pops it from the coordinator, so stop_logging
    # skips it and that foot's pressure data vanishes with no error.
    if is_recording_evt.is_set():
        return jsonify(
            {
                "status": "recording_active",
                "message": "Recording in progress; removing an insole now would silently drop that foot's pressure data. Stop the recording first.",
            }
        ), 409
    data = request.get_json(silent=True) or {}
    address = data.get("address")
    if not address:
        return jsonify({"status": "error", "message": "address is required"}), 400

    try:
        ble.remove_device(address)
        return jsonify({"status": "success", "message": "Device removed"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/connect_all", methods=["POST"])
def api_ble_connect_all():
    """Connect every pooled device; 'partial' when some failed."""
    try:
        detail = ble.connect_all()
        status = "success" if detail.get("connected_all") else "partial"
        return jsonify({"status": status, **detail}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/disconnect_all", methods=["POST"])
def api_ble_disconnect_all():
    """Disconnect every pooled device."""
    try:
        ble.disconnect_all()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/assign_side", methods=["POST"])
def api_ble_assign_side():
    """Assign {address} to {side: Left|Right}."""
    data = request.get_json(silent=True) or {}
    address = data.get("address")
    side = data.get("side")
    if not address or not side:
        return jsonify({"status": "error", "message": "address and side are required"}), 400

    try:
        ble.assign_side(address, side)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/ble/toggle_led", methods=["POST"])
def api_ble_toggle_led():
    """Blink one insole's LED to identify it."""
    data = request.get_json(silent=True) or {}
    address = data.get("address")
    if not address:
        return jsonify({"status": "error", "message": "address is required"}), 400

    try:
        ble.toggle_led(address)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/ble/set_frequency", methods=["POST"])
def api_ble_set_frequency():
    """Set the sample rate for all insoles ({frequency: 10Hz|100Hz|200Hz})."""
    data = request.get_json(silent=True) or {}
    freq = data.get("frequency", "200Hz")
    if freq not in FREQ_MAP:
        return jsonify({"status": "error", "message": "frequency must be 10Hz, 100Hz, or 200Hz"}), 400

    try:
        ble.set_frequency(freq)
        return jsonify({"status": "success", "frequency": freq}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/start_stream", methods=["POST"])
def api_ble_start_stream():
    """Arm streaming outside of a take (refused mid-take)."""
    # Guard BEFORE any BLE work: re-arming mid-take clears the buffer and
    # recomputes the same deterministic log filename, so the dump at stop
    # would overwrite the take's earlier segment — losing the beginning AND
    # the middle of the pressure data.
    if is_recording_evt.is_set():
        return jsonify(
            {
                "status": "recording_active",
                "message": "Recording in progress; re-arming the insole stream would overwrite this take's pressure log. Stop the recording first.",
            }
        ), 409
    try:
        detail = ble.start_streaming()
        if is_recording_evt.is_set() and current_recording_dir is not None:
            ble.start_logging(current_recording_dir, recording_index)
        return jsonify(
            {
                "status": "success",
                "streaming": ble.streaming,
                "logging_active": ble.logging_active,
                "detail": detail,
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/ble/stop_stream", methods=["POST"])
def api_ble_stop_stream():
    """Stop streaming and flush any open log (refused mid-take)."""
    # Guard at the top of the handler: stop_streaming() is a no-op when
    # already stopped, so a check placed after it would still let stop_logging
    # truncate a live recording's insole log.
    if is_recording_evt.is_set():
        return jsonify(
            {
                "status": "recording_active",
                "message": "Recording in progress; stopping the insole stream now would truncate this take's pressure data. Stop the recording first.",
            }
        ), 409
    try:
        ble.stop_streaming()
        saved = ble.stop_logging() if ble.logging_active else None
        return jsonify({"status": "success", "streaming": ble.streaming, "log": saved}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



# ==================== Files ====================
# Readers for the per-take report files written by the stop pipeline. They
# never raise: a missing file is {} and a corrupt one is a failed-shaped dict.
def _get_processing_status(rec_dir: Path) -> dict:
    """recording_N/processing_status.json (postprocess_recording_for_upload)."""
    status_path = rec_dir / "processing_status.json"
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "errors": [f"Could not read processing status: {exc}"]}


def _get_sync_status(rec_dir: Path) -> dict:
    """recording_N/sync/sync_manifest.json (run_sync_on_dir)."""
    manifest_path = rec_dir / "sync" / "sync_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "warnings": [f"Could not read synchronization manifest: {exc}"]}


def _get_validation_report(rec_dir: Path) -> dict:
    """recording_N/validation_report.json (validate_recording)."""
    report_path = rec_dir / "validation_report.json"
    if not report_path.exists():
        return {}
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unusable", "usable": False, "validator_error": str(exc)}


def _get_recording_files_info(rec_dir: Path) -> dict:
    """Synced camera files keyed by view (side/front/back) with size/mtime -
    the same set the browser downloads and the uploader sends.
    """
    files = {}
    for cam_key, semantic_name in CAMERA_NAME_MAPPING.items():
        sync_file = rec_dir / "sync" / f"{cam_key}_sync_{semantic_name}.mp4"
        if sync_file.exists():
            stat = sync_file.stat()
            files[semantic_name] = {
                "filename": sync_file.name,
                "path": str(sync_file.relative_to(BASE_DIR)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "undistorted_synced_video" if cam_key == "cam1" and calibration_required() else "synced_video",
            }
    return files


def _get_ble_logs_info(rec_dir: Path) -> dict:
    """Insole file(s) for a take: the synced ble_sync.json when sync succeeded,
    otherwise the raw ble/*.json logs.
    """
    out = {}
    sync_file = rec_dir / "sync" / "ble_sync.json"
    sync_status = _get_sync_status(rec_dir)
    sync_failed = bool(sync_status) and (
        sync_status.get("ok") is False
        or (isinstance(sync_status.get("ble"), dict) and sync_status["ble"].get("ok") is False)
    )
    if sync_file.is_file() and sync_file.stat().st_size > 0 and not sync_failed:
        stat = sync_file.stat()
        out[sync_file.name] = {
            "filename": sync_file.name,
            "path": str(sync_file.relative_to(BASE_DIR)),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "type": "ble_json",
        }
        return out

    # Preserve the previous raw-file behavior when synchronization did not
    # produce a usable BLE derivative.
    ble_dir = rec_dir / "ble"
    if not ble_dir.exists():
        return out
    for f in sorted(ble_dir.glob("*.json")):
        stat = f.stat()
        out[f.name] = {
            "filename": f.name,
            "path": str(f.relative_to(BASE_DIR)),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "type": "ble_json",
        }
    return out


def _get_audio_files_info(rec_dir: Path) -> dict:
    """Mic WAV info for a take (delegated to MicCaptureManager)."""
    return mic.files_info(rec_dir)


def _get_heartbeat_files_info(rec_dir: Path) -> dict:
    """Heart-rate file for a take: synced JSONL when sync succeeded, else the
    manager's raw listing.
    """
    sync_file = rec_dir / "sync" / "heart_rate_sync.jsonl"
    sync_status = _get_sync_status(rec_dir)
    sync_failed = bool(sync_status) and (
        sync_status.get("ok") is False
        or (isinstance(sync_status.get("heart_rate"), dict) and sync_status["heart_rate"].get("ok") is False)
    )
    if sync_file.is_file() and sync_file.stat().st_size > 0 and not sync_failed:
        stat = sync_file.stat()
        return {
            sync_file.name: {
                "filename": sync_file.name,
                "path": str(sync_file.relative_to(BASE_DIR)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "heartbeat_jsonl",
            }
        }

    # The heartbeat manager retains the original raw JSONL/status listing as
    # the fallback when no synchronized derivative is available.
    return heartbeat.files_info(rec_dir)


def rig_paired() -> bool:
    """Pairing gate: the rig may only record once it holds cloud credentials
    (env override or rig_device.json). Recording without pairing produces
    takes that can neither auto-upload nor be traced to a device - /pair is
    therefore the landing page until pairing is done."""
    return upload_worker is not None


# ---- Pairing (phase-5 enrollment). An admin mints a one-time code in the
# Forgeon admin UI; the operator types it into /pair on the rig; the rig
# exchanges it for a device token and stores rig_device.json (mode 0600).
@app.route("/api/pairing/status", methods=["GET"])
def api_pairing_status():
    """Whether the rig is paired, and with which name / API / credential source."""
    return jsonify({
        "paired": upload_worker is not None,
        "name": (_rig_device_info or {}).get("name"),
        "api_url": (_rig_device_info or {}).get("api_url"),
        "source": (_rig_device_info or {}).get("source"),
    }), 200


@app.route("/api/pairing/claim", methods=["POST"])
def api_pairing_claim():
    """Exchange a one-time admin-minted code for this rig's tokens and store
    them in rig_device.json (owned by the app; no .env, no terminal)."""
    global upload_worker, _rig_device_info
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    api_url = str(body.get("api_url") or DEFAULT_FORGEON_API_URL).strip().rstrip("/")
    if len(code) < 6:
        return jsonify({"status": "error", "message": "Enter the pairing code from the Forgeon admin page."}), 400
    try:
        import urllib.request as _rq
        req = _rq.Request(
            f"{api_url}/rig/devices/claim",
            data=json.dumps({"code": code}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _rq.urlopen(req, timeout=20) as resp:
            claimed = json.loads(resp.read())
    except Exception as exc:
        detail = getattr(exc, "reason", None) or exc
        try:
            detail = json.loads(exc.read()).get("detail", str(exc))  # type: ignore[attr-defined]
        except Exception:
            pass
        rig_log.warning("Pairing claim failed: %s", detail)
        return jsonify({"status": "error", "message": f"Pairing failed: {detail}"}), 502
    info = {
        "api_url": api_url,
        "device_token": claimed["device_token"],
        "lan_token": claimed.get("lan_token"),
        "device_id": claimed.get("id"),
        "name": claimed.get("name"),
        "paired_at": datetime.now(timezone.utc).isoformat(),
    }
    RIG_DEVICE_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")
    try:
        os.chmod(RIG_DEVICE_FILE, 0o600)
    except OSError:
        pass
    _rig_device_info = dict(info, source="file")
    if upload_worker is None:
        upload_worker = UploadWorker(BASE_DIR, api_url, claimed["device_token"])
    rig_log.info("Paired with Forgeon as '%s' (%s)", info.get("name"), info.get("device_id"))
    return jsonify({"status": "paired", "name": info.get("name"), "device_id": info.get("device_id")}), 200


@app.route("/pair", methods=["GET"])
def pairing_page():
    """Self-contained pairing page — mint a code on the Forgeon admin
    'Rig devices' page, type it here. No template file needed."""
    return (
        """<!doctype html><meta charset=utf-8><title>Pair this rig</title>
<body style="font-family:system-ui;max-width:460px;margin:60px auto;padding:0 16px;color:#222">
<h2>Pair this rig with Forgeon</h2>
<p id=state style="color:#666">Checking…</p>
<div id=form style="display:none">
<p>Mint a code on the Forgeon admin → <b>Rig devices</b> page, then enter it:</p>
<input id=code placeholder="XXXX-XXXX" style="font-family:monospace;font-size:24px;letter-spacing:3px;width:200px;text-transform:uppercase;padding:8px">
<button onclick=claim() style="font-size:16px;padding:9px 18px;margin-left:8px">Pair</button>
<p id=msg style="color:#b00"></p></div>
<script>
async function refresh(){const r=await fetch('/api/pairing/status');const d=await r.json();
 if(d.paired){document.getElementById('state').innerHTML='Paired as “'+(d.name||'this rig')+'” → '+d.api_url+' ('+d.source+') &nbsp; <a href="/">Go to recording</a>';document.getElementById('form').style.display='none';}
 else{document.getElementById('state').textContent='Not paired yet.';document.getElementById('form').style.display='block';}}
async function claim(){const c=document.getElementById('code').value;const r=await fetch('/api/pairing/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c})});
 const d=await r.json();if(r.ok){document.getElementById('msg').textContent='';refresh();}else{document.getElementById('msg').textContent=d.message||'Pairing failed';}}
refresh();
</script>"""
    ), 200


# ---- Direct upload. Maps this rig's view names onto the API's manifest
# fields; 'top' has no camera on this rig but the API accepts it.
VIEW_FIELD_MAPPING = {"side": "side_view", "front": "front_view", "back": "back_view", "top": "top_view"}


def _upload_files_for_recording(rec_dir: Path) -> dict:
    """Resolve the uploadable artifacts of a recording into API manifest fields.

    Views come from the synced outputs (same files the browser flow downloads);
    HR/insole are attached only when a non-empty synced derivative exists —
    a 0-byte HR file (strap never connected) is skipped, mirroring the browser
    flow's non-fatal handling.
    """
    files = {}
    for semantic, info in _get_recording_files_info(rec_dir).items():
        field = VIEW_FIELD_MAPPING.get(semantic)
        if field:
            files[field] = info["path"]
    hr = rec_dir / "sync" / "heart_rate_sync.jsonl"
    if hr.is_file() and hr.stat().st_size > 0:
        files["hr_file"] = str(hr.relative_to(BASE_DIR))
    insole = rec_dir / "sync" / "ble_sync.json"
    if insole.is_file() and insole.stat().st_size > 0:
        files["insole_file"] = str(insole.relative_to(BASE_DIR))
    return files


@app.route("/api/upload_instance", methods=["POST"])
def api_upload_instance():
    """Delegate an upload to the rig: enqueue recording N as assessment/instance.

    Body: {assessment_id, instance_no, recording_index, parameters?,
           activity_type?, total_instances?}
    Enqueue is a local disk write — returns immediately, works offline.
    """
    if upload_worker is None:
        return jsonify({"status": "error", "message": "Direct upload is not configured on this rig (set FORGEON_API_URL and FORGEON_DEVICE_TOKEN)."}), 503
    body = request.get_json(silent=True) or {}
    assessment_id = (body.get("assessment_id") or "").strip()
    instance_no = body.get("instance_no")
    rec_index = body.get("recording_index")
    if not assessment_id or instance_no is None or rec_index is None:
        return jsonify({"status": "error", "message": "assessment_id, instance_no and recording_index are required"}), 400
    rec_dir = SESSION_DIR / f"recording_{int(rec_index)}"
    if not rec_dir.exists():
        return jsonify({"status": "not_found", "message": f"recording_{rec_index} not found"}), 404
    files = _upload_files_for_recording(rec_dir)
    if not any(f in ("front_view", "back_view", "side_view", "top_view") for f in files):
        return jsonify({"status": "error", "message": "No synced camera files found for this recording — run sync first."}), 409
    entry = upload_worker.enqueue(
        assessment_id=assessment_id,
        instance_no=int(instance_no),
        recording_dir=str(rec_dir.relative_to(BASE_DIR)),
        files=files,
        parameters=body.get("parameters"),
        activity_type=body.get("activity_type"),
        total_instances=body.get("total_instances"),
    )
    return jsonify({"status": "queued", "entry": entry}), 202


@app.route("/api/upload_queue", methods=["GET"])
def api_upload_queue():
    """Current upload queue entries (empty + 'unconfigured' when not paired)."""
    if upload_worker is None:
        return jsonify({"status": "unconfigured", "entries": []}), 200
    return jsonify({"status": "success", "entries": upload_worker.snapshot()}), 200


@app.route("/api/upload_retry", methods=["POST"])
def api_upload_retry():
    """Re-queue a failed entry identified by {assessment_id, instance_no}."""
    if upload_worker is None:
        return jsonify({"status": "error", "message": "Direct upload is not configured on this rig."}), 503
    body = request.get_json(silent=True) or {}
    entry = upload_worker.retry((body.get("assessment_id") or "").strip(), int(body.get("instance_no") or 0))
    if entry is None:
        return jsonify({"status": "not_found", "message": "No such queue entry"}), 404
    return jsonify({"status": "success", "entry": entry}), 200


@app.route("/download_file/<path:filepath>", methods=["GET"])
def download_file(filepath):
    """Serve any file under BASE_DIR as an attachment (browser download flow)."""
    try:
        # Same fix as /media: prefix-sharing siblings defeat startswith.
        file_path = (BASE_DIR / filepath).resolve()
        if not file_path.is_relative_to(BASE_DIR.resolve()):
            return jsonify({"status": "error", "message": "Invalid file path"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404
        return send_from_directory(str(file_path.parent), file_path.name, as_attachment=True)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# Startup order: replay crash WALs into their takes' JSON logs, launch the
# heart-rate sidecar, start the SSH camera supervisors, start the preview
# capture threads, then serve. Bootstrap and previews keep retrying in the
# background, so the UI comes up even when the Pis are still booting.
if __name__ == "__main__":
    rig_log.info("Session directory: %s", SESSION_DIR)
    try:
        recovered_logs = recover_ble_wals(BASE_DIR / "sessions")
        if recovered_logs:
            ble_logger.warning("Recovered %d insole log(s) from crash WALs", recovered_logs)
    except Exception as exc:
        log_exception(ble_logger, f"WAL recovery scan failed: {exc}")
    heartbeat_start = heartbeat.start_sidecar()
    rig_log.info("Heartbeat sidecar: %s", heartbeat_start)
    start_camera_bootstrap()
    start_capture_threads()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
