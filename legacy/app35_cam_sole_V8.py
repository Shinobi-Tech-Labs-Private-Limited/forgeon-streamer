import asyncio
import argparse
import atexit
import json
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

from intrinsic_calibrate_charuco import CalibrationCandidate, calibrate as calibrate_charuco, make_maps

try:
    from codesharpnessmeasure.focus.charuco import detect_and_segment
    from codesharpnessmeasure.focus.scorer import classify_score, compute_focus_score

    FOCUS_MEASURE_AVAILABLE = True
    FOCUS_MEASURE_ERROR = None
except Exception as exc:
    detect_and_segment = None
    classify_score = None
    compute_focus_score = None
    FOCUS_MEASURE_AVAILABLE = False
    FOCUS_MEASURE_ERROR = str(exc)

# ==================== Camera Config ====================
CAMERA_SOURCES = {
    "cam1": "rtsp://192.168.2.30:8555/video0_side",
    "cam2": "rtsp://192.168.2.33:8555/video0_front",
    "cam3": "rtsp://192.168.2.32:8555/video0_back",
}
CAMERA_SSH_USER = os.environ.get("PI_SSH_USER", "pi").strip() or "pi"
CAMERA_BOOTSTRAP_ENABLED = os.environ.get("CAMERA_BOOTSTRAP_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CAMERA_BOOTSTRAP_BACKOFF = (2, 5, 10)
CAMERA_BOOTSTRAP_HEALTHCHECK_SEC = 5.0
CAMERA_BOOTSTRAP = {
    "cam1": {
        "name": "side camera",
        "host": "192.168.2.30",
        "stream_path": "video0_side",
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
            "side",
            "-f",
            "MJPG",
            "-W",
            "1280",
            "-H",
            "720",
            "-F",
            "90",
            "-s",
            "/dev/video0",
        ],
    },
    "cam2": {
        "name": "front camera",
        "host": "192.168.2.33",
        "stream_path": "video0_front",
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
            "front",
            "-f",
            "MJPG",
            "-W",
            "1280",
            "-H",
            "720",
            "-F",
            "90",
            "/dev/video0",
        ],
    },
    "cam3": {
        "name": "back camera",
        "host": "192.168.2.32",
        "stream_path": "video0_back",
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
            "back",
            "-f",
            "MJPG",
            "-W",
            "1280",
            "-H",
            "720",
            "-F",
            "90",
            "-s",
            "/dev/video0",
        ],
    },
}
FRAME_SIZE = (1280, 720)  # preview resize only

STREAM_THROTTLE_ON_RECORD = True
NORMAL_PREVIEW_FPS = 25.0
NORMAL_JPEG_QUALITY = 70
RECORDING_PREVIEW_FPS = 10.0
RECORDING_JPEG_QUALITY = 55
TARGET_FPS_WRITE = 90
FORCE_CUDA_RECORD = os.environ.get("FORCE_CUDA_RECORD", "").strip().lower() in ("1", "true", "yes", "on")
REQUIRE_CUDA_RECORD = os.environ.get("REQUIRE_CUDA_RECORD", "").strip().lower() in ("1", "true", "yes", "on")

RTSP_TIMEOUT_MS = 5000
RTSP_RETRY_BACKOFF = (1, 2, 5)

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
app = Flask(__name__, template_folder="templates/legacy")
CORS(
    app,
    origins=[
        "https://forgeon-dev-609217469146.us-central1.run.app",
        "https://test.forgelabs.in",
        "https://dev.forgelabs.in",
    ],
)

BASE_DIR = Path(__file__).resolve().parent
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SESSION_DIR = BASE_DIR / "sessions" / f"session_{SESSION_TIMESTAMP}"
SESSION_DIR.mkdir(parents=True, exist_ok=True)
CALIBRATION_DIR = SESSION_DIR / "calibration"
CALIBRATION_JSON = CALIBRATION_DIR / "calibration_cam1.json"
CALIBRATION_NPZ = CALIBRATION_DIR / "calibration_cam1.npz"
CALIBRATION_CAMERA = "cam1"
CALIBRATION_MIN_IMAGES = 40
CALIBRATION_MIN_CORNERS = 6
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

frames = {k: None for k in CAMERA_SOURCES}
frame_ts = {k: 0.0 for k in CAMERA_SOURCES}
frame_locks = {k: threading.Lock() for k in CAMERA_SOURCES}
stop_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}
reopen_capture_evts = {k: threading.Event() for k in CAMERA_SOURCES}

is_recording_evt = threading.Event()
recording_index = 0
recording_start_epoch = None
current_recording_dir = None

record_procs = {}
record_logs = {}
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

cv2.setNumThreads(max(1, os.cpu_count() // 2))


def calculate_crc16(data):
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
def _camera_backend_log_path(cam_key: str) -> Path:
    return camera_backend_logs_dir / f"{cam_key}.log"


def _camera_backend_event(cam_key: str, message: str, *, state: str | None = None, error: str | None = None):
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
    print(f"[camera-bootstrap][{cam_key}] {message}")


def _camera_backend_snapshot():
    with camera_backend_state_lock:
        return {
            cam_key: {
                **status,
                "recent_events": list(status.get("recent_events", [])),
            }
            for cam_key, status in camera_backend_status.items()
        }


def _camera_status_update(cam_key: str, **updates):
    with camera_backend_state_lock:
        status = camera_backend_status[cam_key]
        status.update(updates)


def _preview_is_healthy(cam_key: str, max_age_s: float = 5.0) -> bool:
    ts = frame_ts.get(cam_key, 0.0)
    return bool(ts and (time.time() - ts) <= max_age_s)


def _camera_health_payload(cam_key: str):
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
    cfg = CAMERA_BOOTSTRAP[cam_key]
    target = f"{CAMERA_SSH_USER}@{cfg['host']}"
    return ["ssh", target, "bash", "-s"]


def _camera_log_line(cam_key: str, line: str):
    if not line:
        return
    if _camera_line_is_error(line):
        _camera_backend_event(cam_key, line, state="error", error=line)
    else:
        _camera_backend_event(cam_key, line)


def _run_remote_camera_command(cam_key: str, command: str, timeout: float = 20):
    cfg = CAMERA_BOOTSTRAP[cam_key]
    target = f"{CAMERA_SSH_USER}@{cfg['host']}"
    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
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


def _run_remote_camera_ssh_debug(cam_key: str):
    cfg = CAMERA_BOOTSTRAP[cam_key]
    target = f"{CAMERA_SSH_USER}@{cfg['host']}"
    ssh_cmd = [
        "ssh",
        "-vvv",
        "-o",
        "StrictHostKeyChecking=no",
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
    if not CAMERA_BOOTSTRAP_ENABLED:
        print("[camera-bootstrap] Disabled by CAMERA_BOOTSTRAP_ENABLED")
        return

    for cam_key in CAMERA_BOOTSTRAP:
        thread = camera_backend_threads.get(cam_key)
        if thread and thread.is_alive():
            continue
        thread = threading.Thread(target=_monitor_camera_backend, args=(cam_key,), daemon=True)
        camera_backend_threads[cam_key] = thread
        thread.start()


def stop_camera_bootstrap():
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
    stop_camera_bootstrap()
    raise SystemExit(128 + signum)


signal.signal(signal.SIGINT, _handle_shutdown_signal)
signal.signal(signal.SIGTERM, _handle_shutdown_signal)


# ==================== Camera Helpers ====================
def current_preview_fps():
    return float(
        RECORDING_PREVIEW_FPS
        if (STREAM_THROTTLE_ON_RECORD and is_recording_evt.is_set())
        else NORMAL_PREVIEW_FPS
    )


def current_jpeg_quality():
    return int(
        RECORDING_JPEG_QUALITY
        if (STREAM_THROTTLE_ON_RECORD and is_recording_evt.is_set())
        else NORMAL_JPEG_QUALITY
    )


def _assert_ffmpeg_available():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH. Install it and try again.")


def _ffmpeg_has_encoder(name: str) -> bool:
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
    global _cuda_available_cache
    if _cuda_available_cache is not None:
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
    if FORCE_CUDA_RECORD:
        return True
    return _ffmpeg_has_usable_cuda()


def best_record_decode_args() -> list:
    sys_name = platform.system().lower()
    if (
        sys_name in ("linux", "windows")
        and _ffmpeg_has_decoder("mjpeg_cuvid")
        and _record_cuda_enabled()
    ):
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", "mjpeg_cuvid"]
    return []


def best_record_encoder_args():
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

    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"] + common_out


START_REGEX = re.compile(r"start:\s*([0-9]+\.[0-9]+)")
CAMERA_NAME_MAPPING = {"cam1": "side", "cam2": "front", "cam3": "back"}


def _read_start_time_from_log(log_path: Path):
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


def run_sync_on_dir(recording_dir: Path):
    available = {}
    for cam in CAMERA_SOURCES.keys():
        vid_file = recording_dir / f"{cam}.mp4"
        if not vid_file.exists():
            continue
        st = _read_start_time_from_log(recording_dir / f"{cam}.log")
        if st is not None:
            available[cam] = {"start": st, "path": vid_file}

    if len(available) < 2:
        print(f"[sync] Need at least 2 cameras with video and start times; found {len(available)}. Skipping sync.")
        return

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
        return

    common_dur = min(usable_durs)
    fps_values.sort()
    common_fps = fps_values[len(fps_values) // 2]

    sync_dir = recording_dir / "sync"
    sync_dir.mkdir(exist_ok=True)

    # Launch all camera re-encodes in parallel (GPU NVENC when available, else libx264),
    # then wait on all. Identical inputs/filter/outputs to before — only the encoder backend
    # and the serial->parallel execution change.
    t0 = time.time()
    procs = []
    for cam, info in available.items():
        suffix = CAMERA_NAME_MAPPING.get(cam, cam)
        out_path = sync_dir / f"{cam}_sync_{suffix}.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{offsets[cam]:.6f}",
            "-i",
            str(info["path"]),
            "-t",
            f"{common_dur:.6f}",
            "-an",
            "-sn",
            "-vf",
            f"fps={common_fps:.6f},setpts=PTS-STARTPTS",
        ] + best_sync_encoder_args() + [str(out_path)]
        procs.append((cam, subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))

    for cam, p in procs:
        rc = p.wait()
        if rc != 0:
            print(f"[sync] {cam} re-encode exited with code {rc}")
    print(f"[sync] re-encoded {len(procs)} cameras in {time.time() - t0:.2f}s")


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
            time.sleep(backoffs[0])
            backoffs = backoffs[1:] + [backoffs[-1]]
            continue

        backoffs = list(RTSP_RETRY_BACKOFF)
        while not stop_capture_evts[cam_key].is_set():
            if reopen_capture_evts[cam_key].is_set():
                reopen_capture_evts[cam_key].clear()
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
    for cam_key, src in CAMERA_SOURCES.items():
        t = threading.Thread(target=capture_frames, args=(cam_key, src), daemon=True)
        t.start()


def gen_frames(cam_key):
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
    if cam_key not in CAMERA_SOURCES:
        return None
    with frame_locks[cam_key]:
        frame = frames.get(cam_key)
        return frame.copy() if frame is not None else None


def focus_payload_for_camera(cam_key):
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

    detection = detect_and_segment(frame)
    if not detection.get("detected"):
        return {
            "detected": False,
            "score": None,
            "label": "NO BOARD",
            "color": "#888888",
            "corners": int(detection.get("corners") or 0),
        }, 200

    score = compute_focus_score(detection["board_crop"])
    label, color = classify_score(score)
    return {
        "detected": True,
        "score": score,
        "label": label,
        "color": color,
        "corners": int(detection.get("corners") or 0),
    }, 200


def build_ffmpeg_record_cmd(src_url: str, out_path: Path):
    fps = int(TARGET_FPS_WRITE)

    dec = best_record_decode_args()
    enc = best_record_encoder_args()
    using_cuvid = any("mjpeg_cuvid" in x for x in dec)

    if using_cuvid:
        vf = f"settb=AVTB,hwdownload,format=nv12,fps={fps},setpts=N/({fps}*TB),showinfo"
    else:
        vf = f"settb=AVTB,fps={fps},setpts=N/({fps}*TB),showinfo"

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

    return (
        ["ffmpeg", "-y"]
        + record_input
        + dec
        + ["-i", src_url, "-an", "-sn", "-filter:v", vf]
        + enc
        + ["-video_track_timescale", "90000", str(out_path)]
    )


def start_recording_all():
    global recording_index, recording_start_epoch, current_recording_dir, record_procs, record_logs
    if is_recording_evt.is_set():
        return

    _assert_ffmpeg_available()

    cuda_ok = _ffmpeg_has_usable_cuda()
    if REQUIRE_CUDA_RECORD and not cuda_ok:
        raise RuntimeError("CUDA is required for recording, but FFmpeg CUDA init failed in this runtime.")

    print(
        f"[record] CUDA probe={'OK' if cuda_ok else 'FAIL'} "
        f"force_cuda={FORCE_CUDA_RECORD} require_cuda={REQUIRE_CUDA_RECORD}"
    )

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

        logf = open(log_path, "w", buffering=1)
        record_logs[cam_key] = logf
        logf.write("CMD: " + " ".join(cmd) + "\n")
        if "h264_nvenc" in cmd:
            logf.write("BACKEND: nvenc\n")
        elif "libx264" in cmd:
            logf.write("BACKEND: libx264\n")
        if "mjpeg_cuvid" in cmd:
            logf.write("DECODE: mjpeg_cuvid\n")
        else:
            logf.write("DECODE: software\n")
        print(f"[{cam_key}] ▶ FFmpeg recording started -> {out_path}")
        print(f"[{cam_key}] Log: {log_path}")

        p = subprocess.Popen(cmd, stdout=logf, stderr=logf, cwd=str(BASE_DIR))
        record_procs[cam_key] = p
        time.sleep(0.3)
        if p.poll() is not None:
            print(f"[{cam_key}] FFmpeg exited immediately with code {p.returncode}. See {log_path}")

    alive = [p for p in record_procs.values() if p and p.poll() is None]
    if not alive:
        raise RuntimeError(
            "All FFmpeg recording processes exited immediately. "
            "Check cam*.log in the recording folder for details."
        )


def _snap_output_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = current_recording_dir if (is_recording_evt.is_set() and current_recording_dir) else SESSION_DIR
    out = base / "snaps" / f"snap_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _snapshot_via_ffmpeg(src_url: str, out_path: Path) -> bool:
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
    if path is None:
        return None
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def _calibration_required_fields(data: dict) -> tuple[bool, str | None]:
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


def _load_calibration_json(path: Path = CALIBRATION_JSON) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    ok, _ = _calibration_required_fields(data)
    return data if ok else None


def calibration_available() -> bool:
    return _load_calibration_json() is not None


def sport_selected() -> bool:
    return selected_sport in SPORT_OPTIONS


def current_sport() -> str:
    return selected_sport if sport_selected() else DEFAULT_SPORT


def current_sport_config() -> dict:
    return SPORT_OPTIONS[current_sport()]


def sport_options_for_template() -> list[dict]:
    return [
        {"value": value, **config}
        for value, config in SPORT_OPTIONS.items()
    ]


def ui_template_context(page_mode: str) -> dict:
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
    return bool(current_sport_config()["calibration_required"])


def _calibration_summary(path: Path = CALIBRATION_JSON) -> dict:
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
    snaps = _snap_rows()
    usable = sum(1 for row in snaps if row.get("used") is True)
    calibration_data = _load_calibration_json()
    preview = None
    if calibration_data:
        originals = calibration_data.get("used_source_images") or calibration_data.get("source_images") or []
        undistorted = calibration_data.get("undistorted_images") or []
        if originals and undistorted:
            original_path = originals[-1]
            undistorted_path = undistorted[-1]
            preview = {
                "original_path": original_path,
                "original_url": url_for("media_file", filepath=original_path),
                "undistorted_path": undistorted_path,
                "undistorted_url": url_for("media_file", filepath=undistorted_path),
            }
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
    ok, error = _calibration_required_fields(data)
    if not ok:
        raise ValueError(error or "Invalid calibration JSON.")
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    saved = dict(data)
    saved["source"] = "uploaded_json"
    saved["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    CALIBRATION_JSON.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return saved


def _candidate_from_calibration(data: dict) -> tuple[CalibrationCandidate, tuple[int, int]]:
    image_size = (int(data["image_size"]["width"]), int(data["image_size"]["height"]))
    candidate = CalibrationCandidate(
        model=data.get("selected_model") or data.get("model"),
        rms=float(data.get("rms_reprojection_error_px") or 0.0),
        camera_matrix=np.array(data["camera_matrix"], dtype=np.float64),
        dist_coeffs=np.array(data["distortion_coefficients"], dtype=np.float64).reshape(-1, 1),
        new_camera_matrix=np.array(data["new_camera_matrix"], dtype=np.float64),
        roi=tuple(data.get("roi") or (0, 0, image_size[0], image_size[1])),
        rvecs=[],
        tvecs=[],
    )
    return candidate, image_size


def _undistort_video_file(src: Path, dst: Path, calibration_data: dict) -> dict:
    candidate, image_size = _candidate_from_calibration(calibration_data)
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video for undistortion: {src}")

    width = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
    height = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    fps = cap.get(cv2.CAP_PROP_FPS) or TARGET_FPS_WRITE
    if (width, height) != image_size:
        cap.release()
        raise RuntimeError(
            f"Video size mismatch for {src}: {width}x{height}, "
            f"expected {image_size[0]}x{image_size[1]} from calibration."
        )

    map1, map2 = make_maps(candidate, image_size)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_name(f"{dst.stem}_opencv_tmp{dst.suffix}")
    writer = cv2.VideoWriter(str(tmp_dst), cv2.VideoWriter_fourcc(*"mp4v"), fps, image_size)
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Could not open temporary video writer: {tmp_dst}")

    frames_written = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(cv2.remap(frame, map1, map2, cv2.INTER_LINEAR))
        frames_written += 1

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
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
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
    }


def postprocess_recording_for_upload(recording_dir: Path) -> dict:
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
        if len(synced_files) < 2:
            raise RuntimeError(f"Need at least 2 synced videos for upload; found {len(synced_files)}.")

        if calibration_required() and "cam1" in synced_files and not calibration_data:
            raise RuntimeError("Calibration JSON is missing or invalid.")

        if calibration_required() and "cam1" in synced_files:
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


def stop_recording_all():
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

    t_end = time.time() + 15.0
    for _, p in list(record_procs.items()):
        if p is None:
            continue
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

    if current_recording_dir and current_recording_dir.exists():
        run_sync_on_dir(current_recording_dir)
        postprocess_recording_for_upload(current_recording_dir)

    for k in CAMERA_SOURCES.keys():
        reopen_capture_evts[k].set()


# ==================== BLE Backend ====================
class InsoleDevice:
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

    async def connect(self):
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

        print(f"[BLE] Connect failed {self.address}: {last_err}")
        return False

    def _on_disconnect(self, client):
        self.connected = False
        self.is_streaming = False

    async def disconnect(self):
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
        if self.connected and self.client:
            await self.client.write_gatt_char(UUID_CMD_CHAR, CMD_LED_TOGGLE, response=False)

    async def set_frequency(self, freq_cmd: bytes):
        if self.connected and self.client:
            await self.client.write_gatt_char(UUID_CMD_CHAR, freq_cmd, response=False)
            if freq_cmd == CMD_FREQ_10HZ:
                self.frequency_code = 0x0A
            elif freq_cmd == CMD_FREQ_100HZ:
                self.frequency_code = 0x0B
            elif freq_cmd == CMD_FREQ_200HZ:
                self.frequency_code = 0x0C

    async def start_stream(self, freq_cmd: bytes | None = None):
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
        if not self.connected or not self.client:
            return
        try:
            await self.client.stop_notify(UUID_ADC_CHAR)
        except Exception:
            pass
        self.is_streaming = False

    def get_raw_data_and_clear(self):
        with self._buffer_lock:
            out = bytes(self._raw_buffer)
            self._raw_buffer = bytearray()
        return out

    def _handle_status(self, sender, data):
        if len(data) < 8:
            return
        self.is_charging = bool(data[0])
        self.is_streaming = bool(data[1])
        self.battery_voltage = struct.unpack("<H", data[2:4])[0]
        self.frequency_code = data[4]

    def _handle_adc_data(self, sender, data):
        if len(data) < SINGLE_SAMPLE_SIZE or len(data) % SINGLE_SAMPLE_SIZE != 0:
            return

        num_samples = len(data) // SINGLE_SAMPLE_SIZE
        host_ts = time.time()
        self.notify_count += 1
        with self._buffer_lock:
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
                self._raw_buffer.extend(struct.pack("<d", host_ts))
                self._raw_buffer.extend(packet)


class BleCoordinator:
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

        self._lock = Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout=30):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _device(self, address: str):
        return self.devices.get(address)

    def _active_stream_count(self) -> int:
        # Count only devices that have actually delivered stream data in this session.
        return sum(
            1
            for d in self.devices.values()
            if d.connected and (d.notify_count > 0 or d.packet_count > 0)
        )

    def _stream_targets(self):
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
        result = []
        devices = await BleakScanner.discover(timeout=5.0)
        for d in devices:
            name = d.name or f"BLE Device {d.address}"
            result.append({"name": name, "address": d.address, "rssi": getattr(d, "rssi", None)})
        return result

    def scan(self):
        rows = self._run(self._scan(), timeout=20)
        with self._lock:
            self.discovered = rows
        return rows

    def add_device(self, address: str, name: str):
        with self._lock:
            if address in self.devices:
                return self.devices[address]
            dev = InsoleDevice(address, name)
            self.devices[address] = dev
            return dev

    def remove_device(self, address: str):
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
        side = side.title()
        if side not in ("Left", "Right"):
            raise ValueError("side must be Left or Right")

        dev = self._device(address)
        if not dev:
            raise ValueError("device not found")

        with self._lock:
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

    async def _connect_all(self):
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
        return self._run(self._connect_all(), timeout=45)

    async def _disconnect_all(self):
        tasks = [d.disconnect() for d in self.devices.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def disconnect_all(self):
        try:
            self._run(self._disconnect_all(), timeout=30)
        finally:
            self.streaming = False

    async def _toggle_led(self, address: str):
        d = self._device(address)
        if not d:
            raise ValueError("device not found")
        if not d.connected:
            raise ValueError("device is not connected")
        await d.toggle_led()

    def toggle_led(self, address: str):
        self._run(self._toggle_led(address), timeout=10)

    async def _set_frequency(self, cmd: bytes):
        tasks = [d.set_frequency(cmd) for d in self.devices.values() if d.connected]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def set_frequency(self, label: str):
        cmd = FREQ_MAP.get(label, CMD_FREQ_200HZ)
        self.target_frequency_label = label
        self.target_frequency_cmd = cmd
        self._run(self._set_frequency(cmd), timeout=15)

    async def _start_stream(self):
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
        if self.target_frequency_cmd == CMD_FREQ_10HZ:
            return 10.0
        if self.target_frequency_cmd == CMD_FREQ_100HZ:
            return 100.0
        return 200.0

    async def _validate_stream_rates(self, duration_s: float = 2.0, retries: int = 1):
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
        end = time.time() + max(0.2, timeout_s)
        while time.time() < end:
            for d in self.devices.values():
                if d.connected and (d.packet_count > 0 or d.notify_count > 0):
                    return True
            await asyncio.sleep(poll_s)
        return False

    def start_streaming(self):
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
        return bool(self._run(self._wait_for_any_packets(timeout_s=timeout_s), timeout=max(5, int(timeout_s) + 3)))

    def validate_stream_rates(self, duration_s: float = 2.0, retries: int = 1):
        timeout = max(10, int(duration_s * (retries + 1)) + 8)
        return self._run(self._validate_stream_rates(duration_s=duration_s, retries=retries), timeout=timeout)

    async def _stop_stream(self):
        tasks = [d.stop_stream() for d in self.devices.values() if d.connected and d.is_streaming]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop_streaming(self):
        if not self.streaming:
            return
        try:
            self._run(self._stop_stream(), timeout=20)
        finally:
            self.streaming = False

    def start_logging(self, rec_dir: Path, rec_idx: int):
        ble_dir = rec_dir / "ble"
        ble_dir.mkdir(parents=True, exist_ok=True)
        self.current_log_file = ble_dir / f"insole_log_recording_{rec_idx}.json"
        self.logging_active = True

    def stop_logging(self):
        self.logging_active = False
        if not self.current_log_file:
            return {"ok": False, "message": "No target file"}

        output = {"Left": [], "Right": [], "Unassigned": []}
        packet_id = 0
        devices_to_dump = []
        seen = set()

        # Prefer explicit Left/Right ordering first.
        for address in [self.left_address, self.right_address]:
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

        for dev in devices_to_dump:
            raw_data = dev.get_raw_data_and_clear()
            if not raw_data:
                continue

            record_size = 28
            num_records = len(raw_data) // record_size
            bucket = dev.side if dev.side in ("Left", "Right") else "Unassigned"
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
                    "Device_Side": dev.side,
                    "Channels": {f"Ch{j}": int(val) for j, val in enumerate(channels)},
                }
                output[bucket].append(entry)

        total_entries = len(output["Left"]) + len(output["Right"]) + len(output["Unassigned"])
        if total_entries == 0:
            return {"ok": False, "message": "No BLE data to save"}

        self.current_log_file.write_text(json.dumps(output, indent=2), encoding="utf-8")
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


ble = BleCoordinator(SESSION_DIR)


# ==================== Combined Control ====================
def start_combined(capture_ble: bool = True):
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

    start_recording_all()
    if ble_active and current_recording_dir is not None:
        ble.start_logging(current_recording_dir, recording_index)
        ble_result["ble_logging"] = ble.logging_active

    return {
        "ok": True,
        "recording_index": recording_index,
        "recording_dir": str(current_recording_dir.relative_to(BASE_DIR)) if current_recording_dir else None,
        "calibration": _calibration_summary(),
        "ble": ble_result,
    }


def stop_combined():
    if not is_recording_evt.is_set():
        return {"ok": False, "message": "Recording not running"}

    ble_log = None
    ble_stop_error = None

    try:
        ble.stop_streaming()
        if ble.logging_active:
            ble_log = ble.stop_logging()
    except Exception as e:
        ble_stop_error = str(e)

    stop_recording_all()

    out = {
        "ok": True,
        "recording_index": recording_index,
        "recording_dir": str(current_recording_dir.relative_to(BASE_DIR)) if current_recording_dir else None,
        "files": _get_recording_files_info(current_recording_dir) if current_recording_dir else {},
        "ble_files": _get_ble_logs_info(current_recording_dir) if current_recording_dir else {},
        "processing": _get_processing_status(current_recording_dir) if current_recording_dir else {},
        "calibration": _calibration_summary(),
        "ble_log": ble_log,
    }
    if ble_stop_error:
        out["ble_error"] = ble_stop_error
    return out


# ==================== Routes (UI) ====================
@app.route("/")
def index():
    if not sport_selected():
        return render_template("index35_cam_sole_V8.html", **ui_template_context("sport_select"))
    return redirect(url_for("recording_page" if (not calibration_required() or calibration_available()) else "calibration_page"))


@app.route("/select_sport", methods=["POST"])
def select_sport_route():
    global selected_sport
    if is_recording_evt.is_set():
        return "Stop the current recording before switching sport.", 409
    body = request.get_json(silent=True) or {}
    sport = (request.form.get("sport") or body.get("sport") or "").strip().lower()
    if sport not in SPORT_OPTIONS:
        return render_template(
            "index35_cam_sole_V8.html",
            sport_error="Select a valid sport.",
            **ui_template_context("sport_select"),
        ), 400
    selected_sport = sport
    return redirect(url_for("recording_page" if not calibration_required() or calibration_available() else "calibration_page"))


@app.route("/calibration")
def calibration_page():
    if not sport_selected():
        return redirect(url_for("index"))
    if not calibration_required():
        return redirect(url_for("recording_page"))
    return render_template("index35_cam_sole_V8.html", **ui_template_context("calibration"))


@app.route("/recording")
def recording_page():
    if not sport_selected():
        return redirect(url_for("index"))
    if calibration_required() and not calibration_available():
        return redirect(url_for("calibration_page"))
    return render_template("index35_cam_sole_V8.html", **ui_template_context("recording"))


@app.route("/video_feed/<cam_key>")
def video_feed(cam_key):
    if cam_key not in CAMERA_SOURCES:
        return "Unknown camera", 404
    return Response(gen_frames(cam_key), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/focus/<cam_key>")
def focus_check(cam_key):
    payload, status_code = focus_payload_for_camera(cam_key)
    return jsonify(payload), status_code


@app.route("/focus/all")
def focus_all():
    out = {}
    status_code = 200
    for cam_key in CAMERA_SOURCES:
        payload, cam_status = focus_payload_for_camera(cam_key)
        out[cam_key] = payload
        if cam_status >= 500:
            status_code = max(status_code, cam_status)
    return jsonify(out), status_code


@app.route("/start_recording", methods=["POST"])
def start_recording_route():
    try:
        start_combined(capture_ble=True)
    except Exception as e:
        return f"Failed to start combined recording: {e}", 500
    return redirect(url_for("recording_page"))


@app.route("/stop_recording", methods=["POST"])
def stop_recording_route():
    try:
        stop_combined()
    except Exception as e:
        return f"Failed to stop combined recording: {e}", 500
    return redirect(url_for("recording_page"))


@app.route("/capture_photos", methods=["POST"])
def capture_photos_route():
    try:
        capture_photos_all()
        return redirect(url_for("calibration_page" if calibration_required() else "recording_page"))
    except Exception as e:
        return f"Failed to capture photos: {e}", 500


@app.route("/media/<path:filepath>", methods=["GET"])
def media_file(filepath):
    try:
        file_path = BASE_DIR / filepath
        if not str(file_path.resolve()).startswith(str(BASE_DIR.resolve())):
            return jsonify({"status": "error", "message": "Invalid file path"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404
        return send_from_directory(str(file_path.parent), file_path.name, as_attachment=False)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/status")
def status():
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
            "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
            "camera_bootstrap_enabled": CAMERA_BOOTSTRAP_ENABLED,
            "camera_bootstrap": _camera_backend_snapshot(),
            "camera_status": camera_status,
            "calibration": _calibration_summary(),
            "ble": ble.snapshot(),
        }
    )


@app.route("/api/calibration/status", methods=["GET"])
def api_calibration_status():
    return jsonify(_calibration_status_payload()), 200


@app.route("/api/calibration/capture", methods=["POST"])
def api_calibration_capture():
    try:
        manifest = capture_photos_all()
        detection = _run_detection_preview()
        payload = _calibration_status_payload()
        payload["capture"] = manifest
        payload["detection"] = detection
        return jsonify(payload), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/calibration/run", methods=["POST"])
def api_calibration_run():
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
    try:
        if request.files.get("file"):
            data = json.loads(request.files["file"].read().decode("utf-8"))
        else:
            body = request.get_json(silent=True) or {}
            if body.get("path"):
                src = BASE_DIR / body["path"]
                if not str(src.resolve()).startswith(str(BASE_DIR.resolve())):
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
    try:
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


@app.route("/api/stop_recording", methods=["POST"])
def api_stop_recording():
    try:
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
    try:
        rec_dir = SESSION_DIR / f"recording_{rec_index}"
        if not rec_dir.exists():
            return jsonify({"status": "not_found", "message": f"recording_{rec_index} not found"}), 404
        files = _get_recording_files_info(rec_dir)
        ble_files = _get_ble_logs_info(rec_dir)
        return jsonify(
            {
                "status": "success",
                "recording_dir": str(rec_dir.relative_to(BASE_DIR)),
                "recording_index": rec_index,
                "files": files,
                "ble_files": ble_files,
                "calibration": _calibration_summary(),
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/list_recordings", methods=["GET"])
def api_list_recordings():
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
                            "processing": _get_processing_status(item),
                            "calibration": _calibration_summary(),
                        }
                    )

        return jsonify(
            {
                "status": "success",
                "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
                "total_recordings": len(recordings),
                "recordings": recordings,
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/camera/status", methods=["GET"])
def api_camera_status():
    return jsonify(
        {
            "status": "success",
            "enabled": CAMERA_BOOTSTRAP_ENABLED,
            "ssh_user": CAMERA_SSH_USER,
            "session_dir": str(SESSION_DIR.relative_to(BASE_DIR)),
            "cameras": {cam_key: _camera_health_payload(cam_key) for cam_key in CAMERA_SOURCES},
        }
    )


# ==================== BLE API ====================
@app.route("/api/ble/status", methods=["GET"])
def api_ble_status():
    return jsonify({"status": "success", **ble.snapshot()})


@app.route("/api/ble/scan", methods=["POST"])
def api_ble_scan():
    try:
        rows = ble.scan()
        return jsonify({"status": "success", "count": len(rows), "devices": rows}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/add_device", methods=["POST"])
def api_ble_add_device():
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
    try:
        detail = ble.connect_all()
        status = "success" if detail.get("connected_all") else "partial"
        return jsonify({"status": status, **detail}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/disconnect_all", methods=["POST"])
def api_ble_disconnect_all():
    try:
        ble.disconnect_all()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ble/assign_side", methods=["POST"])
def api_ble_assign_side():
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
    try:
        ble.stop_streaming()
        saved = ble.stop_logging() if ble.logging_active else None
        return jsonify({"status": "success", "streaming": ble.streaming, "log": saved}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==================== Files ====================
def _get_processing_status(rec_dir: Path) -> dict:
    status_path = rec_dir / "processing_status.json"
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "errors": [f"Could not read processing status: {exc}"]}


def _get_recording_files_info(rec_dir: Path) -> dict:
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
    out = {}
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


@app.route("/download_file/<path:filepath>", methods=["GET"])
def download_file(filepath):
    try:
        file_path = BASE_DIR / filepath
        if not str(file_path.resolve()).startswith(str(BASE_DIR.resolve())):
            return jsonify({"status": "error", "message": "Invalid file path"}), 403
        if not file_path.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404
        return send_from_directory(str(file_path.parent), file_path.name, as_attachment=True)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    print(f"Session directory: {SESSION_DIR}")
    start_camera_bootstrap()
    start_capture_threads()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
