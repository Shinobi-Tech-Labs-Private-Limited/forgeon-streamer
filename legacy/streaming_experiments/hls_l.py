"""
HLS Recorder with Per-Ball Threading Architecture

NEW ARCHITECTURE (Recommended):
- Each ball runs in its own thread: record → process → upload
- Parallel processing: multiple balls can be processed simultaneously
- Immediate S3 upload: each ball uploads as soon as it's ready
- Real-time status tracking: live progress updates for each ball

API ENDPOINTS:
- POST /ball/start-recording - Start recording a ball
- POST /ball/stop-recording - Stop recording a ball
- GET  /ball/status/{session}/{ball} - Get ball status
- GET  /ball/all-status/{session} - Get all balls status
- POST /ball/trigger-analysis/{session} - Trigger analysis

LEGACY ENDPOINTS (Deprecated):
- POST /record/start - Use /ball/start-recording instead
- POST /record/stop - Use /ball/stop-recording instead
- POST /record/start-session - For continuous session recording
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import requests
import re
import boto3
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List
from enum import Enum
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger


class StartRequest(BaseModel):
    # v1 compatibility
    url: Optional[str] = None  # HLS m3u8 URL (legacy key)
    # v2 fields
    hls_url: Optional[str] = None
    ui_session_id: Optional[str] = None
    ball_index: Optional[int] = None
    camera_id: Optional[str] = None
    out_dir: Optional[str] = None  # Optional output directory; defaults to ./recordings or structured by v2
    filename: Optional[str] = None  # Optional explicit filename; defaults to camera_<timestamp>.mp4
    extract_timestamps: Optional[bool] = True  # Extract EXT-X-PROGRAM-DATE-TIME timestamps
    fps: Optional[float] = None  # Override FPS detection (e.g., 30.0, 60.0)


class StopRequest(BaseModel):
    # v1: session_id; v2: recorder_session_id
    session_id: Optional[str] = None
    recorder_session_id: Optional[str] = None
    upload_to_s3: Optional[bool] = False
    s3_session_name: Optional[str] = None
    user_id: Optional[str] = None
# New requests for upload/reset after stop
class UploadRequest(BaseModel):
    session_id: str
    s3_session_name: Optional[str] = None
    user_id: Optional[str] = None

class ResetRequest(BaseModel):
    session_id: str

class UploadBallRequest(BaseModel):
    ui_session_id: str
    ball_index: int
    s3_base_prefix: str
    camera_files: List[Dict[str, str]]  # { camera_id, mp4_path, timestamp_json_path }
    metadata: Optional[Dict[str, object]] = None

class AnalysisTriggerRequest(BaseModel):
    ui_session_id: str
    s3_session_prefix: str
    ball_count: int

class CleanupBallRequest(BaseModel):
    ui_session_id: str
    ball_index: int
    base_dir: Optional[str] = None

class StartSessionRecordingRequest(BaseModel):
    hls_url: str
    ui_session_id: str
    camera_id: str
    out_dir: Optional[str] = None

class ExtractBallClipsRequest(BaseModel):
    ui_session_id: str
    ball_index: int
    camera_ids: List[str]
    start_offset_ms: int
    duration_ms: int

# New threading architecture models
class BallStatus(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    READY = "ready"
    FAILED = "failed"

@dataclass
class BallProgress:
    status: BallStatus = BallStatus.IDLE
    progress_percent: int = 0
    current_operation: str = ""
    error_message: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    upload_speed: Optional[str] = None
    file_size: Optional[int] = None
    s3_paths: Dict[str, str] = field(default_factory=dict)

@dataclass
class BallRecording:
    ball_index: int
    ui_session_id: str
    camera_ids: List[str]
    hls_urls: Dict[str, str]
    start_timestamp: float
    end_timestamp: Optional[float] = None
    local_files: Dict[str, str] = field(default_factory=dict)
    s3_files: Dict[str, str] = field(default_factory=dict)
    progress: BallProgress = field(default_factory=BallProgress)

class StartBallRecordingRequest(BaseModel):
    ui_session_id: str
    ball_index: int
    camera_ids: List[str]
    hls_urls: Dict[str, str]

class StopBallRecordingRequest(BaseModel):
    ui_session_id: str
    ball_index: int

class GetBallStatusRequest(BaseModel):
    ui_session_id: str
    ball_index: int



class TimestampInfo(BaseModel):
    segment_url: str
    program_date_time: str
    unix_timestamp_ms: int  # UNIX timestamp in milliseconds
    segment_duration: float
    frame_start_ms: int  # Start of this segment in UNIX ms
    frame_end_ms: int    # End of this segment in UNIX ms


app = FastAPI(title="Local HLS Recorder", version="1.0.0")

# LEGACY: Simple in-memory process registry (kept for backward compatibility)
# NEW: Use ball_thread_manager for per-ball threading architecture
processes: Dict[str, Dict[str, object]] = {}

# S3 Configuration - matches your existing backend setup
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "cricket-analysis-bucket")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Initialize S3 client
def get_s3_client():
    """Get S3 client with IAM role (same as your existing backend)"""
    try:
        return boto3.client('s3', region_name=AWS_REGION)
    except Exception as e:
        logger.error(f"Failed to initialize S3 client: {e}")
        return None


def _default_out_dir() -> Path:
    # Use relative recordings directory for better compatibility
    base = Path(__file__).parent / "recordings"
    base.mkdir(parents=True, exist_ok=True)
    print(f"📁 Using recordings directory: {base.absolute()}")
    return base


# ===================== Precise Sync Utilities (FFprobe/FFmpeg) =====================
_FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
_FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

def _secs_to_timecode(seconds: float, fps: int) -> str:
    """Convert seconds → HH:MM:SS:FF for a given integer fps."""
    if seconds < 0:
        seconds = 0.0
    hh = int(seconds // 3600)
    rem = seconds - hh * 3600
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

def _ffprobe_start_end(path: Path) -> Optional[tuple]:
    """Return (start_time_sec, end_time_sec) for a media file using ffprobe.
    Falls back to stream start_time/duration or format start_time/duration.
    """
    try:
        cmd = [
            _FFPROBE_BIN,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=start_time,duration",
            "-show_entries", "format=start_time,duration",
            "-of", "json",
            str(path)
        ]
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if out.returncode != 0:
            logger.warning(f"ffprobe failed for {path}: {out.stderr.strip()}")
            return None
        data = json.loads(out.stdout or "{}")
        # Prefer stream info
        start = None
        dur = None
        try:
            if data.get("streams"):
                s0 = data["streams"][0]
                if "start_time" in s0:
                    start = float(s0["start_time"]) if s0["start_time"] is not None else None
                if "duration" in s0:
                    dur = float(s0["duration"]) if s0["duration"] is not None else None
        except Exception:
            pass
        # Fallback to format
        try:
            fmt = data.get("format", {})
            if start is None and "start_time" in fmt and fmt["start_time"] is not None:
                start = float(fmt["start_time"])  # may be negative
            if dur is None and "duration" in fmt and fmt["duration"] is not None:
                dur = float(fmt["duration"])
        except Exception:
            pass
        if start is None:
            start = 0.0
        if dur is None:
            # last resort probe duration with simpler call
            return None
        end = start + dur
        return (start, end)
    except Exception as e:
        logger.warning(f"ffprobe exception for {path}: {e}")
        return None

def _ffmpeg_trim_reencode(src: Path, dst: Path, start_offset: float, duration: float, fps_tc: int = 30) -> None:
    """Trim a clip with re-encode for frame-accurate cuts and normalized timescale/timecode.
    - Sets PTS to start at 0
    - Sets video track timescale to 90000
    - Applies timecode starting at 00:00:00:00 (or derived from wall time if desired)
    """
    timecode = _secs_to_timecode(0.0, fps_tc)
    cmd = [
        _FFMPEG_BIN, "-y",
        "-ss", f"{start_offset:.6f}",
        "-i", str(src),
        "-t", f"{duration:.6f}",
        "-an", "-sn",
        "-filter:v", "setpts=PTS-STARTPTS",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-video_track_timescale", "90000",
        "-timecode", timecode,
        "-movflags", "+faststart",
        str(dst)
    ]
    logp = Path(str(dst).replace(".mp4", "_sync.log"))
    with open(logp, "w", buffering=1) as lf:
        lf.write("CMD: " + " ".join(cmd) + "\n")
        proc = subprocess.run(cmd, stdout=lf, stderr=lf)
        if proc.returncode != 0 or (not dst.exists()) or dst.stat().st_size < 2000:
            raise RuntimeError(f"Trim failed for {src.name}, see {logp}")

def sync_camera_files_to_overlap(files: Dict[str, str], work_dir: Path, fps_tc: int = 30) -> Dict[str, str]:
    """Given a mapping camera_id -> mp4 path, compute common overlap and write *_sync.mp4.
    Returns a new mapping camera_id -> synced mp4 path (only for successfully synced cameras).
    """
    # Probe start/end for each file
    infos = []
    for cam, fpath in files.items():
        p = Path(fpath)
        if not p.exists() or p.stat().st_size < 2000:
            logger.warning(f"[{cam}] missing/empty file; skipping from sync")
            continue
        se = _ffprobe_start_end(p)
        if not se:
            logger.warning(f"[{cam}] ffprobe failed; skipping from sync")
            continue
        s, e = se
        infos.append((cam, p, s, e))

    if len(infos) < 2:
        logger.warning("Not enough files for overlap sync; skipping")
        return {}

    # Compute maximal overlap
    global_start = max(s for _, _, s, _ in infos)
    global_end = min(e for _, _, _, e in infos)
    if global_end <= global_start:
        logger.warning(f"No positive overlap: start={global_start:.6f}, end={global_end:.6f}")
        return {}

    duration = max(0.0, global_end - global_start)
    if duration <= 0:
        logger.warning("Overlap duration <= 0; skipping")
        return {}

    # Produce *_sync.mp4 for each
    out_map: Dict[str, str] = {}
    for cam, srcp, s, _ in infos:
        rel_start = max(0.0, global_start - s)
        outp = srcp.parent / f"{srcp.stem}_sync.mp4"
        logger.info(f"[{cam}] trimming to overlap: start={rel_start:.6f}s, dur={duration:.6f}s → {outp.name}")
        try:
            _ffmpeg_trim_reencode(srcp, outp, rel_start, duration, fps_tc=fps_tc)
            out_map[cam] = str(outp)
        except Exception as e:
            logger.warning(f"[{cam}] trim failed: {e}")

    # Write manifest
    manifest = {
        "fps_timecode": fps_tc,
        "global_start_sec": round(global_start, 6),
        "global_end_sec": round(global_end, 6),
        "duration_sec": round(duration, 6),
        "synced_files": out_map,
    }
    try:
        (work_dir / "sync_info.json").write_text(json.dumps(manifest, indent=2))
    except Exception:
        pass

    return out_map


def _extract_hls_timestamps(m3u8_url: str) -> List[TimestampInfo]:
    """
    Extract EXT-X-PROGRAM-DATE-TIME timestamps from HLS playlist.
    Converts to UNIX timestamps for frame-accurate synchronization.
    """
    try:
        response = requests.get(m3u8_url, timeout=5)
        response.raise_for_status()
        playlist_content = response.text

        timestamps = []
        lines = playlist_content.strip().split('\n')

        current_timestamp = None
        current_duration = 0.0

        for i, line in enumerate(lines):
            line = line.strip()

            # Extract program date time
            if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
                current_timestamp = line.split(':', 1)[1]

            # Extract segment duration
            elif line.startswith('#EXTINF:'):
                duration_match = re.match(r'#EXTINF:([0-9.]+)', line)
                if duration_match:
                    current_duration = float(duration_match.group(1))

            # Extract segment URL
            elif line and not line.startswith('#') and current_timestamp:
                # Resolve relative URLs
                if not line.startswith('http'):
                    base_url = '/'.join(m3u8_url.split('/')[:-1])
                    segment_url = f"{base_url}/{line}"
                else:
                    segment_url = line

                # Convert ISO timestamp to UNIX milliseconds
                unix_ms = _iso_to_unix_ms(current_timestamp)
                frame_start_ms = unix_ms
                frame_end_ms = unix_ms + int(current_duration * 1000)

                timestamps.append(TimestampInfo(
                    segment_url=segment_url,
                    program_date_time=current_timestamp,
                    unix_timestamp_ms=unix_ms,
                    segment_duration=current_duration,
                    frame_start_ms=frame_start_ms,
                    frame_end_ms=frame_end_ms
                ))

                current_timestamp = None  # Reset for next segment

        return timestamps

    except Exception as e:
        print(f"Error extracting HLS timestamps: {e}")
        return []


def _iso_to_unix_ms(iso_timestamp: str) -> int:
    """Convert ISO timestamp to UNIX milliseconds."""
    try:
        # Handle various ISO formats
        iso_timestamp = iso_timestamp.replace('Z', '+00:00')

        # Parse the timestamp
        if '.' in iso_timestamp:
            # Has microseconds
            dt = datetime.fromisoformat(iso_timestamp)
        else:
            # No microseconds, add them
            if '+' in iso_timestamp:
                base_time, tz = iso_timestamp.rsplit('+', 1)
                iso_timestamp = f"{base_time}.000+{tz}"
            else:
                iso_timestamp = f"{iso_timestamp}.000"
            dt = datetime.fromisoformat(iso_timestamp)

        # Convert to UNIX milliseconds
        unix_seconds = dt.timestamp()
        unix_ms = int(unix_seconds * 1000)

        return unix_ms

    except Exception as e:
        print(f"Error converting timestamp {iso_timestamp}: {e}")
        # Fallback to current time
        return int(time.time() * 1000)


def _detect_camera_fps(hls_url: str) -> float:
    """
    Detect camera FPS by analyzing the HLS stream or inferring from common settings.
    Returns detected FPS or defaults to 30.0
    """
    try:
        # Try to get HLS playlist to infer FPS from segment duration
        response = requests.get(hls_url, timeout=5)
        if response.status_code == 200:
            content = response.text

            # Look for segment duration in HLS playlist
            for line in content.split('\n'):
                if line.startswith('#EXTINF:'):
                    duration_match = re.match(r'#EXTINF:([0-9.]+)', line)
                    if duration_match:
                        segment_duration = float(duration_match.group(1))
                        # Common segment durations and their typical FPS
                        if segment_duration <= 1.0:
                            return 30.0  # 1s segments usually 30fps
                        elif segment_duration <= 2.0:
                            return 30.0  # 2s segments usually 30fps
                        else:
                            return 30.0  # Default fallback

        # Fallback: assume 30fps (most common for biomechanics)
        return 30.0

    except Exception as e:
        print(f"Error detecting camera FPS: {e}, defaulting to 30fps")
        return 30.0


def _generate_frame_timestamps(session_id: str, fps: float, duration_seconds: int = 30) -> List[TimestampInfo]:
    """
    Generate frame-level UNIX timestamps based on detected camera FPS.
    Creates precise per-frame timestamps for biomechanics analysis.

    Args:
        session_id: Session identifier containing start timestamp
        fps: Frames per second (detected from camera/stream)
        duration_seconds: Total duration to generate timestamps for

    Returns:
        List of TimestampInfo objects with frame-level precision
    """
    try:
        # Always use current time for each new recording to avoid timestamp overlap
        unix_start_ms = int(time.time() * 1000)

        # Calculate frame duration in milliseconds
        frame_duration_ms = 1000.0 / fps
        total_frames = int(fps * duration_seconds)

        print(f"Generating frame-level timestamps: {fps}fps, {frame_duration_ms:.2f}ms per frame, {total_frames} total frames")
        print(f"Starting timestamp: {unix_start_ms} ms (fresh for each ball)")

        timestamps = []
        for frame_num in range(total_frames):
            frame_start_ms = unix_start_ms + int(frame_num * frame_duration_ms)
            frame_end_ms = unix_start_ms + int((frame_num + 1) * frame_duration_ms)

            # Create frame-level timestamp info
            timestamps.append(TimestampInfo(
                segment_url=f"frame_{frame_num:04d}.frame",
                program_date_time=unix_ms_to_datetime(frame_start_ms).isoformat() + "Z",
                unix_timestamp_ms=frame_start_ms,
                segment_duration=frame_duration_ms / 1000.0,  # Convert back to seconds
                frame_start_ms=frame_start_ms,
                frame_end_ms=frame_end_ms
            ))

        return timestamps

    except Exception as e:
        print(f"Error generating frame timestamps: {e}")
        return []


def _generate_synthetic_timestamps(session_id: str, hls_url: str = None, override_fps: float = None) -> List[TimestampInfo]:
    """
    Generate synthetic UNIX timestamps with automatic or manual FPS detection.
    Creates frame-level timestamps based on detected or specified camera FPS.
    """
    try:
        # Use override FPS if provided, otherwise detect from stream
        if override_fps:
            fps = override_fps
            print(f"Using override FPS: {fps}")
        elif hls_url:
            detected_fps = _detect_camera_fps(hls_url)
            fps = detected_fps
            print(f"Detected camera FPS: {fps}")
        else:
            fps = 30.0  # Default fallback
            print(f"Using default FPS: {fps}")

        # Generate frame-level timestamps
        return _generate_frame_timestamps(session_id, fps, duration_seconds=30)

    except Exception as e:
        print(f"Error generating synthetic timestamps: {e}")
        return []


def unix_ms_to_datetime(unix_ms: int):
    """Convert UNIX milliseconds to datetime object."""
    return datetime.fromtimestamp(unix_ms / 1000.0)


class BallThread(threading.Thread):
    """Individual thread for recording, processing, and uploading a single ball."""

    def __init__(self, ball_recording: BallRecording):
        super().__init__(daemon=True)
        self.ball_recording = ball_recording
        self.stop_event = threading.Event()
        self.recording_processes: Dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def run(self):
        """Main thread execution: record -> process -> upload."""
        try:
            logger.info(f"🎬 Starting Ball {self.ball_recording.ball_index} thread")

            # Phase 1: Recording
            self._update_status(BallStatus.RECORDING, "Recording from live stream", 0)
            self._record_ball()

            # Phase 2: Processing (always continue after recording stops)
            self._update_status(BallStatus.PROCESSING, "Processing video clips", 25)
            self._process_ball()

            # Phase 3: Uploading (always continue after processing)
            self._update_status(BallStatus.UPLOADING, "Uploading to S3", 50)
            self._upload_ball()

            # Phase 4: Complete
            self._update_status(BallStatus.READY, "Ready for analysis", 100)
            logger.info(f"✅ Ball {self.ball_recording.ball_index} completed successfully")

        except Exception as e:
            logger.error(f"❌ Ball {self.ball_recording.ball_index} failed: {e}")
            self._update_status(BallStatus.FAILED, f"Error: {str(e)}", 0)

    def _update_status(self, status: BallStatus, operation: str, progress: int):
        """Thread-safe status update."""
        with self.lock:
            self.ball_recording.progress.status = status
            self.ball_recording.progress.current_operation = operation
            self.ball_recording.progress.progress_percent = progress
            if status == BallStatus.RECORDING and not self.ball_recording.progress.start_time:
                self.ball_recording.progress.start_time = time.time()
            elif status in [BallStatus.READY, BallStatus.FAILED]:
                self.ball_recording.progress.end_time = time.time()

    def _record_ball(self):
        """Record ball from live stream."""
        try:
            # Create output directory for this ball
            base_dir = _default_out_dir()
            ball_dir = base_dir / self.ball_recording.ui_session_id / f"ball_{self.ball_recording.ball_index:02d}"
            ball_dir.mkdir(parents=True, exist_ok=True)

            successful_cameras = []

            # Record from each camera
            for camera_id in self.ball_recording.camera_ids:
                if camera_id not in self.ball_recording.hls_urls:
                    logger.warning(f"⚠️ No HLS URL found for camera {camera_id}")
                    continue

                hls_url = self.ball_recording.hls_urls[camera_id]
                camera_dir = ball_dir / camera_id
                camera_dir.mkdir(parents=True, exist_ok=True)

                # Create output file path
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = camera_dir / f"ball_{self.ball_recording.ball_index:02d}_{camera_id}_{timestamp}.mp4"

                try:
                    # Start FFmpeg recording
                    logger.info(f"🎬 Recording Ball {self.ball_recording.ball_index} from {camera_id} camera")
                    proc = _spawn_ffmpeg_record(hls_url, output_file, preserve_timestamps=True)

                    # Check if process started successfully
                    time.sleep(0.5)  # Give it a moment to start
                    if proc.poll() is not None:
                        logger.error(f"❌ FFmpeg process failed to start for {camera_id}")
                        continue

                    # Store process and file path
                    with self.lock:
                        self.recording_processes[camera_id] = proc
                        self.ball_recording.local_files[camera_id] = str(output_file)

                    successful_cameras.append(camera_id)
                    logger.info(f"📹 Ball {self.ball_recording.ball_index} recording started for {camera_id}")

                except Exception as e:
                    logger.error(f"❌ Failed to start recording for {camera_id}: {e}")
                    continue

            if not successful_cameras:
                raise Exception("No cameras could be recorded successfully")

            logger.info(f"📹 Recording from {len(successful_cameras)} cameras: {successful_cameras}")

            # Wait for recording to be stopped externally
            while not self.stop_event.is_set():
                time.sleep(0.1)

            # Stop all recording processes
            self._stop_recording_processes()

            logger.info(f"📹 Ball {self.ball_recording.ball_index} recording completed")

        except Exception as e:
            logger.error(f"❌ Recording failed for Ball {self.ball_recording.ball_index}: {e}")
            self._stop_recording_processes()
            raise

    def _stop_recording_processes(self):
        """Stop all recording processes for this ball."""
        with self.lock:
            for camera_id, proc in self.recording_processes.items():
                try:
                    if proc.poll() is None:
                        logger.info(f"🛑 Stopping recording for {camera_id}")
                        _graceful_stop(proc)
                except Exception as e:
                    logger.error(f"❌ Error stopping recording for {camera_id}: {e}")
            self.recording_processes.clear()

    def _process_ball(self):
        """Process recorded ball into clips."""
        try:
            logger.info(f"⚙️ Processing Ball {self.ball_recording.ball_index}")

            # Check if we have any recorded files
            if not self.ball_recording.local_files:
                logger.error(f"❌ No recorded files found for Ball {self.ball_recording.ball_index}")
                raise Exception("No recorded files found")

            logger.info(f"📁 Processing {len(self.ball_recording.local_files)} files: {list(self.ball_recording.local_files.keys())}")

            # Files are in place; now perform overlap-based synchronization across cameras

            # Generate timestamp files for each camera
            base_dir = _default_out_dir()
            ball_dir = base_dir / self.ball_recording.ui_session_id / f"ball_{self.ball_recording.ball_index:02d}"

            for camera_id, file_path in self.ball_recording.local_files.items():
                if not Path(file_path).exists():
                    logger.warning(f"⚠️ Recorded file not found: {file_path}")
                    continue

                # Create timestamp metadata
                timestamp_data = {
                    "ball_index": self.ball_recording.ball_index,
                    "camera_id": camera_id,
                    "session_id": self.ball_recording.ui_session_id,
                    "recorded_at": datetime.now().isoformat(),
                    "start_timestamp": self.ball_recording.start_timestamp,
                    "end_timestamp": self.ball_recording.end_timestamp or time.time(),
                    "file_path": file_path,
                    "file_size": Path(file_path).stat().st_size if Path(file_path).exists() else 0,
                    "fps": 30.0  # Default FPS
                }

                # Save timestamp file
                timestamp_file = ball_dir / camera_id / f"ball_{self.ball_recording.ball_index:02d}_{camera_id}_timestamps.json"
                with open(timestamp_file, 'w') as f:
                    json.dump(timestamp_data, f, indent=2)

                logger.info(f"📄 Timestamp file created: {timestamp_file}")

            # ----- Overlap-based sync step -----
            try:
                synced = sync_camera_files_to_overlap(self.ball_recording.local_files, ball_dir, fps_tc=30)
                if synced:
                    # Prefer synced outputs for subsequent upload
                    for cam, spath in synced.items():
                        self.ball_recording.local_files[cam] = spath
                    logger.info(f"🧭 Overlap sync completed for cameras: {list(synced.keys())}")
                else:
                    logger.warning("⚠️ Overlap sync skipped or failed; proceeding with original files")
            except Exception as e:
                logger.warning(f"⚠️ Sync step failed: {e}")

            logger.info(f"⚙️ Ball {self.ball_recording.ball_index} processing completed")

        except Exception as e:
            logger.error(f"❌ Processing failed for Ball {self.ball_recording.ball_index}: {e}")
            raise

    def _upload_ball(self):
        """Upload ball to S3 with retry logic."""
        max_retries = 3
        retry_delay = 2  # seconds

        logger.info(f"⬆️ Starting upload for Ball {self.ball_recording.ball_index}")
        logger.info(f"📁 Files to upload: {self.ball_recording.local_files}")

        for attempt in range(max_retries):
            try:
                logger.info(f"⬆️ Uploading Ball {self.ball_recording.ball_index} to S3 (attempt {attempt + 1}/{max_retries})")

                s3_client = get_s3_client()
                if not s3_client:
                    raise Exception("S3 client not available")

                logger.info(f"✅ S3 client obtained successfully")

                # Create S3 folder structure: stream-recordings/{session_name}/ball_{index}/{camera}/
                s3_base_prefix = f"stream-recordings/{self.ball_recording.ui_session_id}"

                for camera_id, file_path in self.ball_recording.local_files.items():
                    if not Path(file_path).exists():
                        logger.warning(f"⚠️ File not found for upload: {file_path}")
                        continue

                    # Upload MP4 file with retry
                    s3_key = f"{s3_base_prefix}/ball_{self.ball_recording.ball_index:02d}/{camera_id}/recording.mp4"

                    logger.info(f"⬆️ Uploading {camera_id} video: {file_path} -> s3://{S3_BUCKET}/{s3_key}")

                    self._upload_file_with_retry(
                        s3_client, file_path, S3_BUCKET, s3_key,
                        {'ContentType': 'video/mp4'}, max_retries=2
                    )

                    self.ball_recording.s3_files[camera_id] = s3_key
                    logger.info(f"✅ {camera_id} video uploaded successfully")

                    # Upload timestamp file
                    timestamp_file = Path(file_path).parent / f"ball_{self.ball_recording.ball_index:02d}_{camera_id}_timestamps.json"
                    if timestamp_file.exists():
                        timestamp_s3_key = f"{s3_base_prefix}/ball_{self.ball_recording.ball_index:02d}/{camera_id}/timestamps.json"

                        self._upload_file_with_retry(
                            s3_client, str(timestamp_file), S3_BUCKET, timestamp_s3_key,
                            {'ContentType': 'application/json'}, max_retries=2
                        )

                        logger.info(f"✅ {camera_id} timestamps uploaded successfully")

                # Update progress with S3 paths
                with self.lock:
                    self.ball_recording.progress.s3_paths = self.ball_recording.s3_files.copy()

                logger.info(f"⬆️ Ball {self.ball_recording.ball_index} upload completed")
                return  # Success, exit retry loop

            except Exception as e:
                logger.error(f"❌ Upload attempt {attempt + 1} failed for Ball {self.ball_recording.ball_index}: {e}")

                if attempt < max_retries - 1:
                    logger.info(f"🔄 Retrying upload in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(f"❌ All upload attempts failed for Ball {self.ball_recording.ball_index}")
                    raise

    def _upload_file_with_retry(self, s3_client, file_path: str, bucket: str, key: str, extra_args: dict, max_retries: int = 2):
        """Upload a single file to S3 with retry logic."""
        for attempt in range(max_retries):
            try:
                s3_client.upload_file(file_path, bucket, key, ExtraArgs=extra_args)
                return  # Success
            except Exception as e:
                logger.warning(f"⚠️ Upload attempt {attempt + 1} failed for {key}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)  # Short delay before retry
                else:
                    raise  # Re-raise on final attempt

    def stop(self):
        """Stop the thread gracefully."""
        self.stop_event.set()
        self._stop_recording_processes()
        logger.info(f"🛑 Stopping Ball {self.ball_recording.ball_index} thread")


class BallThreadManager:
    """Manages all ball recording threads."""

    def __init__(self):
        self.active_threads: Dict[str, BallThread] = {}
        self.completed_balls: Dict[str, BallRecording] = {}
        self.lock = threading.Lock()

    def start_ball_recording(self, ball_recording: BallRecording) -> str:
        """Start recording a new ball in its own thread."""
        session_key = f"{ball_recording.ui_session_id}_{ball_recording.ball_index}"

        with self.lock:
            # Stop existing thread for this ball if it exists
            if session_key in self.active_threads:
                self.active_threads[session_key].stop()
                del self.active_threads[session_key]

            # Create and start new thread
            thread = BallThread(ball_recording)
            self.active_threads[session_key] = thread
            thread.start()

            logger.info(f"🚀 Started Ball {ball_recording.ball_index} thread for session {ball_recording.ui_session_id}")
            return session_key

    def stop_ball_recording(self, ui_session_id: str, ball_index: int) -> bool:
        """Stop recording a specific ball."""
        session_key = f"{ui_session_id}_{ball_index}"

        with self.lock:
            if session_key in self.active_threads:
                thread = self.active_threads[session_key]
                thread.stop()

                # Wait for thread to finish
                thread.join(timeout=5)

                # If thread is still alive, wait a bit more for it to complete
                if thread.is_alive():
                    logger.info(f"⏳ Ball {ball_index} thread still alive, waiting for completion...")
                    thread.join(timeout=10)

                # Move to completed if successful (not failed)
                if thread.ball_recording.progress.status != BallStatus.FAILED:
                    self.completed_balls[session_key] = thread.ball_recording
                    logger.info(f"✅ Moved Ball {ball_index} to completed_balls with status: {thread.ball_recording.progress.status.value}")
                else:
                    logger.info(f"⚠️ Ball {ball_index} not moved to completed_balls, status: {thread.ball_recording.progress.status.value}")

                del self.active_threads[session_key]
                logger.info(f"🛑 Stopped Ball {ball_index} recording")
                return True

        return False

    def get_ball_status(self, ui_session_id: str, ball_index: int) -> Optional[BallProgress]:
        """Get current status of a specific ball."""
        session_key = f"{ui_session_id}_{ball_index}"

        with self.lock:
            # Check active threads first
            if session_key in self.active_threads:
                return self.active_threads[session_key].ball_recording.progress

            # Check completed balls
            if session_key in self.completed_balls:
                return self.completed_balls[session_key].progress

        return None

    def get_all_balls_status(self, ui_session_id: str) -> Dict[int, BallProgress]:
        """Get status of all balls for a session."""
        statuses = {}

        with self.lock:
            logger.info(f"🔍 Getting all balls status for session: {ui_session_id}")
            logger.info(f"🔍 Active threads: {list(self.active_threads.keys())}")
            logger.info(f"🔍 Completed balls: {list(self.completed_balls.keys())}")

            # Check all active threads for this session
            for session_key, thread in self.active_threads.items():
                if session_key.startswith(f"{ui_session_id}_"):
                    ball_index = int(session_key.split("_")[-1])
                    statuses[ball_index] = thread.ball_recording.progress
                    logger.info(f"🔍 Found active ball {ball_index} with status: {thread.ball_recording.progress.status.value}")

            # Check all completed balls for this session
            for session_key, ball_recording in self.completed_balls.items():
                if session_key.startswith(f"{ui_session_id}_"):
                    ball_index = int(session_key.split("_")[-1])
                    statuses[ball_index] = ball_recording.progress
                    logger.info(f"🔍 Found completed ball {ball_index} with status: {ball_recording.progress.status.value}")

        logger.info(f"🔍 Returning {len(statuses)} balls for session {ui_session_id}")
        return statuses

    def cleanup_completed_balls(self, ui_session_id: str):
        """Clean up completed balls for a session."""
        with self.lock:
            keys_to_remove = [key for key in self.completed_balls.keys() if key.startswith(f"{ui_session_id}_")]
            for key in keys_to_remove:
                del self.completed_balls[key]
            logger.info(f"🧹 Cleaned up {len(keys_to_remove)} completed balls for session {ui_session_id}")


# Global thread manager instance
ball_thread_manager = BallThreadManager()


def _save_timestamp_log(session_id: str, timestamps: List[TimestampInfo], output_dir: Path) -> None:
    """Save timestamp information with UNIX timestamps for later synchronization analysis."""
    timestamp_file = output_dir / f"{session_id}_timestamps.json"

    try:
        import json
        current_unix_ms = int(time.time() * 1000)

        timestamp_data = {
            "session_id": session_id,
            "extracted_at": datetime.now().isoformat(),
            "extracted_at_unix_ms": current_unix_ms,
            "frame_count": len(timestamps),
            "timestamps": [
                {
                    "segment_url": ts.segment_url,
                    "program_date_time": ts.program_date_time,
                    "unix_timestamp_ms": ts.unix_timestamp_ms,
                    "segment_duration": ts.segment_duration,
                    "frame_start_ms": ts.frame_start_ms,
                    "frame_end_ms": ts.frame_end_ms
                }
                for ts in timestamps
            ]
        }

        with open(timestamp_file, 'w') as f:
            json.dump(timestamp_data, f, indent=2)

        print(f"Timestamp log with UNIX timestamps saved to: {timestamp_file}")
        print(f"Frame count: {len(timestamps)}")
        if timestamps:
            print(f"First frame: {timestamps[0].unix_timestamp_ms} ms")
            print(f"Last frame: {timestamps[-1].unix_timestamp_ms} ms")

    except Exception as e:
        print(f"Error saving timestamp log: {e}")


def _spawn_ffmpeg_record(url: str, output_path: Path, preserve_timestamps: bool = True) -> subprocess.Popen:
    """
    Spawn ffmpeg to record an HLS stream into an MP4 file.
    We remux (copy) where possible, generating PTS if needed and enabling faststart.
    """
    # For HLS, copy video; if audio exists (AAC), convert ADTS to ASC.
    # -fflags +genpts helps when input lacks PTS/DTS continuity.
    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart+frag_keyframe+empty_moov"
    ]

    # Add timestamp preservation for frame-accurate sync
    if preserve_timestamps:
        cmd.extend([
            "-map_metadata", "0",  # Copy all metadata including timestamps
            "-avoid_negative_ts", "make_zero",  # Normalize timestamps to start from 0
            "-copyts"  # Copy timestamps exactly as they are
        ])

    cmd.append(str(output_path))
    print(f"🎬 FFmpeg command: {' '.join(cmd)}")
    print(f"📁 Output path: {output_path.absolute()}")
    print(f"🔧 Output path exists: {output_path.exists()}")

    # Use a pipe for stdin so we can send 'q' on stop
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Redirect stderr to stdout so we can see errors
        text=True,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
    )

    # Start a thread to read and print ffmpeg output
    def read_output():
        try:
            for line in proc.stdout:
                print(f"FFmpeg: {line.strip()}")
        except Exception as e:
            print(f"Error reading ffmpeg output: {e}")

    threading.Thread(target=read_output, daemon=True).start()

    # On platforms where SIGINT is quicker than 'q', send both when stopping
    # stop logic elsewhere already writes 'q' to stdin; additionally, we ensure
    # the process group receives SIGINT for immediate segment flush on POSIX.
    try:
        proc.send_signal  # attribute existence check
    except Exception:
        pass
    return proc


@app.on_event("startup")
def _on_startup() -> None:
    # CORS setup happens below
    pass


@app.post("/record/start-session")
def start_session_recording(req: StartSessionRecordingRequest):
    """Start session-level recording for continuous capture with timestamp tracking."""
    try:
        # Preflight: verify ffmpeg exists
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        except Exception as _e:
            raise HTTPException(status_code=500, detail=f"FFmpeg not available: {_e}")

        # Validate HLS URL returns a playlist
        try:
            import requests as _requests
            r = _requests.get(req.hls_url, timeout=5)
            r.raise_for_status()
            if "#EXTM3U" not in (r.text or ""):
                raise HTTPException(status_code=502, detail="Provided URL is not an HLS playlist")
        except HTTPException:
            raise
        except Exception as _e:
            raise HTTPException(status_code=502, detail=f"Unable to reach HLS URL: {_e}")

        # Build session id and output file
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"session_{req.ui_session_id}_{req.camera_id}_{ts}_{int(time.time())}"

        # Use structured directory for session recording
        if req.out_dir:
            out_dir = Path(req.out_dir)
        else:
            base = _default_out_dir()
            out_dir = base / req.ui_session_id / "session" / req.camera_id

        print(f"🔧 Session recording output directory: {out_dir.absolute()}")
        out_dir.mkdir(parents=True, exist_ok=True)

        # Create session recording filename
        base_name = f"session_{req.camera_id}_{ts}.mp4"
        output_path = out_dir / base_name

        # Check if file already exists
        if output_path.exists():
            print(f"⚠️ WARNING: Session file already exists: {output_path}")
            try:
                if output_path.stat().st_size < 1000:
                    print(f"🗑️ Removing small/incomplete file: {output_path}")
                    output_path.unlink()
            except Exception as e:
                print(f"⚠️ Could not remove existing file: {e}")

        print(f"🎬 Starting session recording: {req.hls_url}")
        print(f"📁 Output file: {output_path.absolute()}")

        # Generate frame-level timestamps for session
        timestamps = _generate_synthetic_timestamps(session_id, req.hls_url, 30.0)
        if timestamps:
            print(f"Generated {len(timestamps)} frame-level timestamp entries for session")
            _save_timestamp_log(session_id, timestamps, out_dir)

        proc = _spawn_ffmpeg_record(req.hls_url, output_path, preserve_timestamps=True)

        # Validate process started successfully
        if proc.poll() is not None:
            raise Exception(f"FFmpeg process failed to start, exit code: {proc.poll()}")

        # Wait for FFmpeg to initialize
        time.sleep(0.5)

        if proc.poll() is not None:
            raise Exception(f"FFmpeg process died immediately after start, exit code: {proc.poll()}")

        # Store in legacy processes dict for backward compatibility
        processes[session_id] = {
            "proc": proc,
            "url": req.hls_url,
            "output": str(output_path),
            "started_at": time.time(),
            "timestamps": timestamps,
            "timestamp_file": str(out_dir / f"{session_id}_timestamps.json") if timestamps else None,
            "session_type": "continuous",
            "ui_session_id": req.ui_session_id,
            "camera_id": req.camera_id
        }

        print(f"🎬 Session recording started with PID: {proc.pid}")
        print(f"📁 Recording to: {output_path.absolute()}")
        print(f"⏱️ Started at: {datetime.now().isoformat()}")

        return {
            "recorder_session_id": session_id,
            "start_time_utc": datetime.utcnow().isoformat() + "Z",
            "output": str(output_path),
            "timestamp_count": len(timestamps),
            "timestamp_file": str(out_dir / f"{session_id}_timestamps.json") if timestamps else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start session recording: {e}")


# Legacy endpoint - DEPRECATED: Use /ball/start-recording instead
@app.post("/record/start")
def start_recording_legacy(req: StartRequest):
    """Legacy endpoint - Use /ball/start-recording for new ball threading architecture."""
    logger.warning("⚠️ Using deprecated /record/start endpoint. Please use /ball/start-recording instead.")

    # Convert legacy request to new ball recording format
    if not req.ui_session_id or not req.ball_index or not req.camera_id:
        raise HTTPException(
            status_code=400,
            detail="Legacy endpoint requires ui_session_id, ball_index, and camera_id. Use /ball/start-recording instead."
        )

    # Create HLS URLs dict (assuming single camera for legacy)
    hls_urls = {req.camera_id: req.hls_url or req.url}

    # Create ball recording request
    ball_req = StartBallRecordingRequest(
        ui_session_id=req.ui_session_id,
        ball_index=req.ball_index,
        camera_ids=[req.camera_id],
        hls_urls=hls_urls
    )

    # Use new ball recording endpoint
    return start_ball_recording(ball_req)


def _graceful_stop(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    try:
        if proc.stdin:
            try:
                proc.stdin.write("q\n")  # Send as text since we opened with text=True
                proc.stdin.flush()
                print("Sent 'q' command to ffmpeg")
            except Exception as e:
                print(f"Error sending 'q' to ffmpeg: {e}")

        # Wait longer for ffmpeg to finish writing the file properly
        try:
            proc.wait(timeout=timeout)
            print("FFmpeg process finished gracefully")
        except subprocess.TimeoutExpired:
            print("FFmpeg didn't finish in time, force killing")
            proc.kill()
            proc.wait()
    except Exception as e:
        print(f"Error in graceful stop: {e}")
        # Force kill
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass


def upload_session_to_s3(session_id: str, session_name: str, user_id: str = None) -> Dict[str, str]:
    """
    Upload all session files (MP4s + timestamp JSONs) to S3 in the stream-recordings folder structure.

    Args:
        session_id: HLS recorder session ID
        session_name: User-friendly session name for S3 folder
        user_id: Optional user ID for data isolation

    Returns:
        Dict with S3 URLs of uploaded files
    """
    try:
        s3_client = get_s3_client()
        if not s3_client:
            raise Exception("S3 client not available")

        entry = processes.get(session_id)
        if not entry:
            raise Exception(f"Session {session_id} not found")

        output_path = Path(entry.get("output", ""))
        timestamp_file = entry.get("timestamp_file", "")

        # Create S3 folder structure: stream-recordings/{session_name}/
        s3_folder = f"stream-recordings/{session_name}/"

        uploaded_files = {}

        # Detect view name (side/front/back/runup) from filename or URL
        def _infer_view_name() -> str:
            try:
                name = output_path.name
                base = name.split('.')[0]
                prefix = base.split('_')[0]
                if prefix in {"side", "front", "back", "runup"}:
                    return prefix
            except Exception:
                pass
            try:
                url = entry.get("url", "") or ""
                # grab last path segment (e.g., /live/side/index.m3u8)
                seg = url.strip('/').split('/')[-2] if url.endswith('index.m3u8') else url.strip('/').split('/')[-1]
                if seg in {"side", "front", "back", "runup"}:
                    return seg
            except Exception:
                pass
            return "side"

        view_name = _infer_view_name()

        # Upload MP4 video file
        if output_path.exists():
            # Store under expected structure for analyze-s3: stream-recordings/{session}/{view}/recording.mp4
            video_s3_key = f"{s3_folder}{view_name}/recording.mp4"
            logger.info(f"Uploading video: {output_path} -> s3://{S3_BUCKET}/{video_s3_key}")

            s3_client.upload_file(
                str(output_path),
                S3_BUCKET,
                video_s3_key,
                ExtraArgs={'ContentType': 'video/mp4'}
            )
            uploaded_files['video'] = video_s3_key
            logger.info("✅ Video uploaded successfully")

            # If only one camera in use (commonly 'side'), duplicate as front/back to satisfy multi-view pipeline
            # Do NOT duplicate to other views; upload only the view that exists

        # Upload timestamp JSON file
        if timestamp_file and Path(timestamp_file).exists():
            # Store alongside video: stream-recordings/{session}/{view}/{session_id}_timestamps.json
            timestamp_s3_key = f"{s3_folder}{view_name}/{Path(timestamp_file).name}"
            logger.info(f"Uploading timestamps: {timestamp_file} -> s3://{S3_BUCKET}/{timestamp_s3_key}")

            s3_client.upload_file(
                timestamp_file,
                S3_BUCKET,
                timestamp_s3_key,
                ExtraArgs={'ContentType': 'application/json'}
            )
            uploaded_files['timestamps'] = timestamp_s3_key
            logger.info("✅ Timestamps uploaded successfully")

            # Duplicate timestamps for other views if missing
            # Do NOT duplicate timestamps to other views

        # Create session metadata file for the backend to pick up
        metadata = {
            "session_id": session_id,
            "session_name": session_name,
            "user_id": user_id,
            "recorded_at": datetime.now().isoformat(),
            "video_file": uploaded_files.get('video'),
            "timestamp_file": uploaded_files.get('timestamps'),
            "camera_info": {
                "url": entry.get("url"),
                "fps": entry.get("fps", 30.0),
                "duration_seconds": entry.get("stopped_at", time.time()) - entry.get("started_at", time.time())
            },
            "ready_for_analysis": True
        }

        metadata_s3_key = f"{s3_folder}session_metadata.json"
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=metadata_s3_key,
            Body=json.dumps(metadata, indent=2),
            ContentType='application/json'
        )
        uploaded_files['metadata'] = metadata_s3_key

        logger.info(f"🎉 Session {session_name} uploaded to S3 successfully!")
        logger.info(f"📁 S3 folder: s3://{S3_BUCKET}/{s3_folder}")

        return uploaded_files

    except Exception as e:
        logger.error(f"❌ Failed to upload session to S3: {e}")
        raise


def _safe_put_json(bucket: str, key: str, data: dict) -> None:
    s3 = get_s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data, indent=2), ContentType='application/json')


@app.post("/upload/ball")
def upload_ball(req: UploadBallRequest):
    """Server-side upload for a single ball's files to S3 under session prefix."""
    try:
        s3 = get_s3_client()
        if not s3:
            raise HTTPException(status_code=500, detail="S3 client not available")

        uploaded = []
        for item in req.camera_files:
            cam = item.get('camera_id')
            mp4 = item.get('mp4_path')
            tsj = item.get('timestamp_json_path')
            base = f"{req.s3_base_prefix}/ball_{int(req.ball_index):02d}/{cam}"
            if mp4 and Path(mp4).exists():
                mp4_key = f"{base}/{Path(mp4).name}"
                s3.upload_file(str(mp4), S3_BUCKET, mp4_key, ExtraArgs={'ContentType': 'video/mp4'})
            else:
                mp4_key = None
            if tsj and Path(tsj).exists():
                ts_key = f"{base}/{Path(tsj).name}"
                s3.upload_file(str(tsj), S3_BUCKET, ts_key, ExtraArgs={'ContentType': 'application/json'})
            else:
                ts_key = None
            uploaded.append({ 'camera_id': cam, 'mp4_s3_key': mp4_key, 'ts_s3_key': ts_key })

        # optional per-ball metadata
        meta_key = f"{req.s3_base_prefix}/ball_{int(req.ball_index):02d}/ball_meta.json"
        _safe_put_json(S3_BUCKET, meta_key, req.metadata or {})

        return { 'uploaded': uploaded }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analysis/trigger")
def analysis_trigger(req: AnalysisTriggerRequest):
    # This stub only writes session_meta.json; your main backend can pick it up
    meta = {
        'ui_session_id': req.ui_session_id,
        'ball_count': req.ball_count,
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'ready': True,
    }
    session_meta_key = f"{req.s3_session_prefix}/session_meta.json"
    _safe_put_json(S3_BUCKET, session_meta_key, meta)
    # return a pseudo job id
    return { 'analysis_job_id': f"job_{int(time.time())}" }


@app.post("/record/extract-clips")
def extract_ball_clips(req: ExtractBallClipsRequest):
    """Extract ball clips from session recordings using timestamps."""
    try:
        print(f"🎬 Extracting clips for Ball {req.ball_index} from session {req.ui_session_id}")
        print(f"📊 Start offset: {req.start_offset_ms}ms, Duration: {req.duration_ms}ms")

        # Find session recordings for this UI session
        session_recordings = {}
        for session_id, data in processes.items():
            if (data.get("session_type") == "continuous" and
                data.get("ui_session_id") == req.ui_session_id):
                camera_id = data.get("camera_id")
                if camera_id in req.camera_ids:
                    session_recordings[camera_id] = {
                        "session_id": session_id,
                        "output_path": data.get("output"),
                        "timestamp_file": data.get("timestamp_file")
                    }

        if not session_recordings:
            raise Exception(f"No session recordings found for UI session {req.ui_session_id}")

        print(f"📹 Found session recordings for cameras: {list(session_recordings.keys())}")

        # Extract clips for each camera
        extracted_clips = {}
        base_dir = _default_out_dir() / req.ui_session_id

        for camera_id, recording_info in session_recordings.items():
            try:
                session_output = Path(recording_info["output_path"])
                if not session_output.exists():
                    print(f"⚠️ Session recording not found for {camera_id}: {session_output}")
                    continue

                # Create ball directory structure
                ball_dir = base_dir / f"ball_{req.ball_index:02d}" / camera_id
                ball_dir.mkdir(parents=True, exist_ok=True)

                # Extract clip using FFmpeg
                clip_filename = f"ball_{req.ball_index:02d}_{camera_id}.mp4"
                clip_path = ball_dir / clip_filename

                # Convert milliseconds to seconds for FFmpeg
                start_seconds = req.start_offset_ms / 1000.0
                duration_seconds = req.duration_ms / 1000.0

                print(f"✂️ Extracting clip for {camera_id}: {start_seconds}s - {duration_seconds}s")

                # FFmpeg command to extract clip
                cmd = [
                    "ffmpeg",
                    "-y",  # Overwrite output file
                    "-ss", str(start_seconds),  # Start time
                    "-i", str(session_output),  # Input file
                    "-t", str(duration_seconds),  # Duration
                    "-c", "copy",  # Copy streams without re-encoding
                    "-avoid_negative_ts", "make_zero",
                    str(clip_path)
                ]

                print(f"🎬 FFmpeg command: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60  # 60 second timeout
                )

                if result.returncode == 0 and clip_path.exists():
                    print(f"✅ Clip extracted successfully: {clip_path}")

                    # Generate timestamp JSON for the clip
                    timestamp_data = {
                        "ball_index": req.ball_index,
                        "camera_id": camera_id,
                        "session_id": req.ui_session_id,
                        "extracted_at": datetime.now().isoformat(),
                        "source_file": str(session_output),
                        "start_offset_ms": req.start_offset_ms,
                        "duration_ms": req.duration_ms,
                        "start_seconds": start_seconds,
                        "duration_seconds": duration_seconds,
                        "clip_file": str(clip_path),
                        "frame_count": int(duration_seconds * 30),  # Assuming 30fps
                        "fps": 30.0
                    }

                    timestamp_file = ball_dir / f"ball_{req.ball_index:02d}_{camera_id}_timestamps.json"
                    with open(timestamp_file, 'w') as f:
                        json.dump(timestamp_data, f, indent=2)

                    extracted_clips[camera_id] = {
                        "mp4_path": str(clip_path),
                        "timestamp_json_path": str(timestamp_file),
                        "duration_ms": req.duration_ms,
                        "start_offset_ms": req.start_offset_ms
                    }

                    print(f"📄 Timestamp file created: {timestamp_file}")
                else:
                    print(f"❌ FFmpeg failed for {camera_id}: {result.stderr}")

            except Exception as e:
                print(f"❌ Error extracting clip for {camera_id}: {e}")
                continue

        if not extracted_clips:
            return {
                "success": False,
                "error": "Failed to extract clips for any camera",
                "clips": {}
            }

        print(f"🎉 Successfully extracted clips for {len(extracted_clips)} cameras")
        return {
            "success": True,
            "clips": extracted_clips,
            "ball_index": req.ball_index,
            "total_cameras": len(extracted_clips)
        }

    except Exception as e:
        print(f"❌ Ball clip extraction failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "clips": {}
        }


@app.post("/record/cleanup")
def cleanup_ball(req: CleanupBallRequest):
    try:
        # Use default recordings directory
        base = Path(req.base_dir or str(_default_out_dir()))
        folder = base / req.ui_session_id / f"ball_{int(req.ball_index):02d}"
        print(f"🧹 Cleaning up folder: {folder.absolute()}")

        if folder.exists():
            for p in folder.rglob('*'):
                try:
                    if p.is_file():
                        print(f"🗑️ Deleting file: {p}")
                        p.unlink(missing_ok=True)
                except Exception as e:
                    print(f"⚠️ Error deleting file {p}: {e}")
            # remove empty dirs
            for d in sorted([d for d in folder.rglob('*') if d.is_dir()], reverse=True):
                try:
                    d.rmdir()
                except Exception:
                    pass
            try:
                folder.rmdir()
                print(f"✅ Cleaned up folder: {folder}")
            except Exception:
                pass
        else:
            print(f"📁 Folder doesn't exist: {folder}")
        return { 'ok': True }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Legacy endpoint - DEPRECATED: Use /ball/stop-recording instead
@app.post("/record/stop")
def stop_recording_legacy(req: StopRequest):
    """Legacy endpoint - Use /ball/stop-recording for new ball threading architecture."""
    logger.warning("⚠️ Using deprecated /record/stop endpoint. Please use /ball/stop-recording instead.")

    # For legacy compatibility, we need to extract ball info from the old session_id format
    # This is a simplified approach - in practice, you'd need to track this mapping
    if not req.recorder_session_id and not req.session_id:
        raise HTTPException(status_code=400, detail="recorder_session_id is required")

    # This is a simplified approach - in practice, you'd need better mapping
    # For now, we'll return an error asking to use the new endpoint
    raise HTTPException(
        status_code=400,
        detail="Legacy stop endpoint not supported with new threading architecture. Use /ball/stop-recording with ui_session_id and ball_index."
    )
# Upload previously recorded files to S3 (on-demand at Complete Analysis)
@app.post("/record/upload")
def upload_recording(req: UploadRequest):
    entry = processes.get(req.session_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    session_name = req.s3_session_name or f"live_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    logger.info(f"📤 On-demand upload to S3 for session {req.session_id} as {session_name}")
    uploaded_files = upload_session_to_s3(req.session_id, session_name, req.user_id)

    return {
        "ok": True,
        "session_id": req.session_id,
        "s3_session_name": session_name,
        "s3_files": uploaded_files,
        "s3_bucket": S3_BUCKET,
    }


# Retake: delete local files and clear session state (no S3)
@app.post("/record/reset")
def reset_recording(req: ResetRequest):
    entry = processes.pop(req.session_id, None)
    if not entry:
        return {"ok": True, "message": "Nothing to reset"}

    # Kill process if still around
    proc: subprocess.Popen = entry.get("proc")  # type: ignore[assignment]
    if proc and proc.poll() is None:
        try:
            if proc.stdin:
                proc.stdin.write("q\n")
                proc.stdin.flush()
            proc.kill()
        except Exception:
            pass

    # Delete local files
    try:
        output = entry.get("output")
        if output and Path(output).exists():
            Path(output).unlink(missing_ok=True)
        ts_file = entry.get("timestamp_file")
        if ts_file and Path(ts_file).exists():
            Path(ts_file).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Failed cleaning files for {req.session_id}: {e}")

    return {"ok": True, "message": "Session reset, local files removed"}


@app.post("/record/stop-multi")
def stop_multi_camera_recording(session_ids: List[str], session_name: str, user_id: str = None, upload_to_s3: bool = True):
    """
    Stop multiple camera recordings and upload all files to a single S3 session folder.
    Perfect for your 3-camera cricket setup (side, front, back).

    Args:
        session_ids: List of HLS recorder session IDs (e.g., from 3 cameras)
        session_name: User-friendly session name for S3 folder
        user_id: Optional user ID for data isolation
        upload_to_s3: Whether to upload to S3 (default: True)

    Returns:
        Combined results from all camera recordings
    """
    try:
        results = []
        all_files = {}

        # Stop all recordings first
        for session_id in session_ids:
            entry = processes.get(session_id)
            if not entry:
                logger.warning(f"Session {session_id} not found, skipping")
                continue

            proc: subprocess.Popen = entry["proc"]  # type: ignore[assignment]
            logger.info(f"Stopping recording for session: {session_id}")
            _graceful_stop(proc)
            entry["stopped_at"] = time.time()

            results.append({
                "session_id": session_id,
                "output": entry.get("output"),
                "stopped": True
            })

        # Upload all files to S3 if requested
        if upload_to_s3:
            try:
                s3_client = get_s3_client()
                if not s3_client:
                    raise Exception("S3 client not available")

                # Create S3 folder structure: stream-recordings/{session_name}/
                s3_folder = f"stream-recordings/{session_name}/"
                camera_files = {}

                # Map camera indices to view names for cricket analysis
                view_names = ["side", "front", "back", "runup"]

                # Upload files from each camera
                for i, session_id in enumerate(session_ids):
                    entry = processes.get(session_id)
                    if not entry:
                        continue

                    # Use proper view names for cricket analysis
                    view_name = view_names[i] if i < len(view_names) else f"camera_{i+1}"
                    camera_files[view_name] = {}

                    # Upload MP4 video file to proper structure: stream-recordings/session_name/view/recording.mp4
                    output_path = Path(entry.get("output", ""))
                    if output_path.exists():
                        video_s3_key = f"{s3_folder}{view_name}/recording.mp4"

                        s3_client.upload_file(
                            str(output_path),
                            S3_BUCKET,
                            video_s3_key,
                            ExtraArgs={'ContentType': 'video/mp4'}
                        )
                        camera_files[view_name]['video'] = video_s3_key
                        logger.info(f"✅ {view_name} video uploaded: {video_s3_key}")

                    # Upload timestamp JSON file
                    timestamp_file = entry.get("timestamp_file", "")
                    if timestamp_file and Path(timestamp_file).exists():
                        timestamp_s3_key = f"{s3_folder}{view_name}/{Path(timestamp_file).name}"

                        s3_client.upload_file(
                            timestamp_file,
                            S3_BUCKET,
                            timestamp_s3_key,
                            ExtraArgs={'ContentType': 'application/json'}
                        )
                        camera_files[view_name]['timestamps'] = timestamp_s3_key
                        logger.info(f"✅ {view_name} timestamps uploaded: {timestamp_s3_key}")

                # Create comprehensive session metadata
                metadata = {
                    "session_name": session_name,
                    "user_id": user_id,
                    "recorded_at": datetime.now().isoformat(),
                    "camera_count": len(session_ids),
                    "cameras": camera_files,
                    "session_ids": session_ids,
                    "ready_for_cricket_analysis": True,
                    "analysis_type": "multi_camera_cricket"
                }

                metadata_s3_key = f"{s3_folder}session_metadata.json"
                s3_client.put_object(
                    Bucket=S3_BUCKET,
                    Key=metadata_s3_key,
                    Body=json.dumps(metadata, indent=2),
                    ContentType='application/json'
                )

                all_files = {
                    "cameras": camera_files,
                    "metadata": metadata_s3_key,
                    "s3_folder": s3_folder
                }

                logger.info(f"🎉 Multi-camera session {session_name} uploaded to S3!")
                logger.info(f"📁 S3 folder: s3://{S3_BUCKET}/{s3_folder}")

                # Trigger automatic cricket analysis if user_id provided
                if user_id:
                    try:
                        logger.info(f"🤖 Triggering automatic cricket analysis for session: {session_name}")

                        # Call the cricket analysis API
                        import requests
                        analysis_url = "https://3.226.166.57.nip.io:8000/cricket/analyze-stream-recording"
                        analysis_payload = {
                            "s3_session_folder": s3_folder,
                            "session_name": session_name,
                            "user_id": user_id,
                            "session_metadata": {
                                "bowler": session_name,
                                "elbowWristDistance": "30",  # Default values
                                "hipWidth": "32",
                                "bowlerType": "fast"
                            }
                        }

                        # Make async call to avoid blocking
                        import threading

                        def trigger_analysis():
                            try:
                                response = requests.post(analysis_url, json=analysis_payload, timeout=30)
                                if response.status_code == 200:
                                    logger.info("✅ Automatic cricket analysis started successfully")
                                else:
                                    logger.warning(f"⚠️ Cricket analysis request failed: {response.status_code}")
                            except Exception as e:
                                logger.warning(f"⚠️ Failed to trigger cricket analysis: {e}")

                        # Run analysis in background thread
                        analysis_thread = threading.Thread(target=trigger_analysis)
                        analysis_thread.daemon = True
                        analysis_thread.start()

                    except Exception as e:
                        logger.warning(f"⚠️ Failed to trigger automatic analysis: {e}")

            except Exception as e:
                logger.error(f"❌ S3 upload failed: {e}")
                return {
                    "ok": False,
                    "error": f"S3 upload failed: {e}",
                    "results": results
                }

        return {
            "ok": True,
            "session_name": session_name,
            "cameras_stopped": len(results),
            "s3_upload": upload_to_s3,
            "s3_files": all_files,
            "s3_bucket": S3_BUCKET,
            "ready_for_analysis": True,
            "results": results
        }

    except Exception as e:
        logger.error(f"❌ Multi-camera stop failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to stop multi-camera recording: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "active_sessions": list(processes.keys())}

# New Ball Threading API Endpoints

@app.post("/ball/start-recording")
def start_ball_recording(req: StartBallRecordingRequest):
    """Start recording a new ball in its own thread."""
    try:
        # Create ball recording object
        ball_recording = BallRecording(
            ball_index=req.ball_index,
            ui_session_id=req.ui_session_id,
            camera_ids=req.camera_ids,
            hls_urls=req.hls_urls,
            start_timestamp=time.time()
        )

        # Start the ball thread
        session_key = ball_thread_manager.start_ball_recording(ball_recording)

        logger.info(f"🎬 Started Ball {req.ball_index} recording for session {req.ui_session_id}")

        return {
            "success": True,
            "ball_index": req.ball_index,
            "session_key": session_key,
            "status": "recording",
            "message": f"Ball {req.ball_index} recording started"
        }

    except Exception as e:
        logger.error(f"❌ Failed to start Ball {req.ball_index} recording: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start ball recording: {e}")

@app.post("/ball/stop-recording")
def stop_ball_recording(req: StopBallRecordingRequest):
    """Signal stop immediately and return without waiting for processing/upload."""
    try:
        # Non-blocking: just signal and return
        session_key = f"{req.ui_session_id}_{req.ball_index}"
        with ball_thread_manager.lock:
            thread = ball_thread_manager.active_threads.get(session_key)
            if thread:
                thread.stop_event.set()
                logger.info(f"🛑 Stop signaled for Ball {req.ball_index} (session {req.ui_session_id})")
                return {
                    "success": True,
                    "ball_index": req.ball_index,
                    "status": "stopping",
                    "message": f"Stop signaled for Ball {req.ball_index}; processing/upload will continue in background"
                }
        return {
            "success": False,
            "ball_index": req.ball_index,
            "status": "not_found",
            "message": f"Ball {req.ball_index} recording not found"
        }
    except Exception as e:
        logger.error(f"❌ Failed to signal stop for Ball {req.ball_index}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to signal stop: {e}")

@app.get("/ball/status/{ui_session_id}/{ball_index}")
def get_ball_status(ui_session_id: str, ball_index: int):
    """Get current status of a specific ball."""
    try:
        status = ball_thread_manager.get_ball_status(ui_session_id, ball_index)

        if status:
            return {
                "success": True,
                "ball_index": ball_index,
                "status": status.status.value,
                "progress_percent": status.progress_percent,
                "current_operation": status.current_operation,
                "error_message": status.error_message,
                "start_time": status.start_time,
                "end_time": status.end_time,
                "upload_speed": status.upload_speed,
                "file_size": status.file_size,
                "s3_paths": status.s3_paths
            }
        else:
            return {
                "success": False,
                "ball_index": ball_index,
                "status": "not_found",
                "message": f"Ball {ball_index} not found for session {ui_session_id}"
            }

    except Exception as e:
        logger.error(f"❌ Failed to get Ball {ball_index} status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get ball status: {e}")

@app.get("/ball/all-status/{ui_session_id}")
def get_all_balls_status(ui_session_id: str):
    """Get status of all balls for a session."""
    try:
        all_statuses = ball_thread_manager.get_all_balls_status(ui_session_id)

        result = {}
        for ball_index, status in all_statuses.items():
            result[ball_index] = {
                "status": status.status.value,
                "progress_percent": status.progress_percent,
                "current_operation": status.current_operation,
                "error_message": status.error_message,
                "start_time": status.start_time,
                "end_time": status.end_time,
                "upload_speed": status.upload_speed,
                "file_size": status.file_size,
                "s3_paths": status.s3_paths
            }

        return {
            "success": True,
            "ui_session_id": ui_session_id,
            "balls": result,
            "total_balls": len(result)
        }

    except Exception as e:
        logger.error(f"❌ Failed to get all balls status for session {ui_session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get all balls status: {e}")

@app.post("/ball/cleanup-session/{ui_session_id}")
def cleanup_session_balls(ui_session_id: str):
    """Clean up all completed balls for a session."""
    try:
        ball_thread_manager.cleanup_completed_balls(ui_session_id)

        logger.info(f"🧹 Cleaned up completed balls for session {ui_session_id}")

        return {
            "success": True,
            "ui_session_id": ui_session_id,
            "message": f"Cleaned up completed balls for session {ui_session_id}"
        }

    except Exception as e:
        logger.error(f"❌ Failed to cleanup session {ui_session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cleanup session: {e}")

@app.get("/ball/debug/active-threads")
def debug_active_threads():
    """Debug endpoint to see all active ball threads."""
    try:
        with ball_thread_manager.lock:
            active_info = {}
            for session_key, thread in ball_thread_manager.active_threads.items():
                active_info[session_key] = {
                    "ball_index": thread.ball_recording.ball_index,
                    "ui_session_id": thread.ball_recording.ui_session_id,
                    "status": thread.ball_recording.progress.status.value,
                    "progress_percent": thread.ball_recording.progress.progress_percent,
                    "current_operation": thread.ball_recording.progress.current_operation,
                    "is_alive": thread.is_alive()
                }

            completed_info = {}
            for session_key, ball_recording in ball_thread_manager.completed_balls.items():
                completed_info[session_key] = {
                    "ball_index": ball_recording.ball_index,
                    "ui_session_id": ball_recording.ui_session_id,
                    "status": ball_recording.progress.status.value,
                    "progress_percent": ball_recording.progress.progress_percent
                }

        return {
            "active_threads": active_info,
            "completed_balls": completed_info,
            "total_active": len(active_info),
            "total_completed": len(completed_info)
        }

    except Exception as e:
        logger.error(f"❌ Failed to get debug info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get debug info: {e}")

@app.get("/ball/debug/session/{ui_session_id}")
def debug_session_status(ui_session_id: str):
    """Debug endpoint to see all balls for a specific session."""
    try:
        with ball_thread_manager.lock:
            # Get all statuses for this session
            all_statuses = ball_thread_manager.get_all_balls_status(ui_session_id)

            # Also get raw data
            active_threads_for_session = {}
            completed_balls_for_session = {}

            for session_key, thread in ball_thread_manager.active_threads.items():
                if session_key.startswith(f"{ui_session_id}_"):
                    active_threads_for_session[session_key] = {
                        "ball_index": thread.ball_recording.ball_index,
                        "status": thread.ball_recording.progress.status.value,
                        "progress_percent": thread.ball_recording.progress.progress_percent,
                        "current_operation": thread.ball_recording.progress.current_operation
                    }

            for session_key, ball_recording in ball_thread_manager.completed_balls.items():
                if session_key.startswith(f"{ui_session_id}_"):
                    completed_balls_for_session[session_key] = {
                        "ball_index": ball_recording.ball_index,
                        "status": ball_recording.progress.status.value,
                        "progress_percent": ball_recording.progress.progress_percent,
                        "current_operation": ball_recording.progress.current_operation
                    }

        return {
            "success": True,
            "ui_session_id": ui_session_id,
            "all_statuses": {str(k): v.status.value for k, v in all_statuses.items()},
            "active_threads": active_threads_for_session,
            "completed_balls": completed_balls_for_session,
            "total_balls": len(all_statuses)
        }

    except Exception as e:
        logger.error(f"❌ Failed to get session debug info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get session debug info: {e}")

@app.post("/ball/trigger-analysis/{ui_session_id}")
def trigger_ball_analysis(ui_session_id: str, user_id: Optional[str] = None):
    """Trigger analysis for all completed balls in a session."""
    try:
        # Get all balls for this session
        all_statuses = ball_thread_manager.get_all_balls_status(ui_session_id)

        # Check if all balls are ready
        ready_balls = []
        not_ready_balls = []

        for ball_index, status in all_statuses.items():
            if status.status == BallStatus.READY:
                ready_balls.append(ball_index)
            else:
                not_ready_balls.append({
                    "ball_index": ball_index,
                    "status": status.status.value,
                    "progress_percent": status.progress_percent
                })

        if not_ready_balls:
            return {
                "success": False,
                "message": "Not all balls are ready for analysis",
                "ready_balls": ready_balls,
                "not_ready_balls": not_ready_balls,
                "total_ready": len(ready_balls),
                "total_not_ready": len(not_ready_balls)
            }

        # Create session metadata for analysis
        s3_base_prefix = f"stream-recordings/{ui_session_id}"

        # Get S3 paths for all ready balls
        s3_ball_paths = {}
        for ball_index in ready_balls:
            status = ball_thread_manager.get_ball_status(ui_session_id, ball_index)
            if status and status.s3_paths:
                s3_ball_paths[ball_index] = status.s3_paths

        # Create session metadata
        session_metadata = {
            "ui_session_id": ui_session_id,
            "user_id": user_id,
            "ball_count": len(ready_balls),
            "ready_balls": ready_balls,
            "s3_ball_paths": s3_ball_paths,
            "created_at": datetime.now().isoformat(),
            "ready_for_analysis": True,
            "analysis_type": "multi_ball_cricket"
        }

        # Upload session metadata to S3
        s3_client = get_s3_client()
        if s3_client:
            metadata_s3_key = f"{s3_base_prefix}/session_metadata.json"
            s3_client.put_object(
                Bucket=S3_BUCKET,
                Key=metadata_s3_key,
                Body=json.dumps(session_metadata, indent=2),
                ContentType='application/json'
            )
            logger.info(f"📄 Session metadata uploaded: s3://{S3_BUCKET}/{metadata_s3_key}")

        # Trigger analysis (this would call your main backend)
        try:
            import requests
            # Use batch endpoint to process all balls in parallel
            analysis_url = "https://3.226.166.57.nip.io:8000/cricket/analyze-stream-recording-batch"
            analysis_payload = {
                "s3_session_folder": f"{s3_base_prefix}/",
                "session_name": ui_session_id,
                "user_id": user_id,
                "session_metadata": {
                    "bowler": ui_session_id,
                    "elbowWristDistance": "30",
                    "hipWidth": "32",
                    "bowlerType": "fast"
                }
            }

            # Make async call to avoid blocking
            import threading

            def trigger_analysis():
                try:
                    response = requests.post(analysis_url, json=analysis_payload, timeout=30)
                    if response.status_code == 200:
                        logger.info("✅ Ball analysis triggered successfully")
                    else:
                        logger.warning(f"⚠️ Analysis request failed: {response.status_code}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to trigger analysis: {e}")

            # Run analysis in background thread
            analysis_thread = threading.Thread(target=trigger_analysis)
            analysis_thread.daemon = True
            analysis_thread.start()

        except Exception as e:
            logger.warning(f"⚠️ Failed to trigger automatic analysis: {e}")

        logger.info(f"🎉 Analysis triggered for session {ui_session_id} with {len(ready_balls)} balls")

        return {
            "success": True,
            "ui_session_id": ui_session_id,
            "ready_balls": ready_balls,
            "total_balls": len(ready_balls),
            "s3_ball_paths": s3_ball_paths,
            "analysis_triggered": True,
            "message": f"Analysis triggered for {len(ready_balls)} balls"
        }

    except Exception as e:
        logger.error(f"❌ Failed to trigger analysis for session {ui_session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to trigger analysis: {e}")

@app.post("/record/force-stop-all")
def force_stop_all():
    """Emergency function to stop all active recordings and clean up processes."""
    try:
        stopped_count = 0
        for session_id, data in list(processes.items()):
            try:
                if data.get("proc"):
                    print(f"🛑 Force stopping process {session_id} (PID: {data['proc'].pid})")
                    data["proc"].kill()
                    data["proc"].wait(timeout=3)
                stopped_count += 1
            except Exception as e:
                print(f"⚠️ Error stopping process {session_id}: {e}")

        processes.clear()
        print(f"✅ Force stopped {stopped_count} processes")
        return {"stopped_count": stopped_count, "message": f"Force stopped {stopped_count} processes"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to force stop all: {e}")

@app.get("/debug/status")
def debug_status():
    """Debug endpoint to check current status of all processes and files."""
    try:
        debug_info = {
            "active_processes": len(processes),
            "processes": {},
            "recordings_directory": str(_default_out_dir().absolute()),
            "recordings_directory_exists": _default_out_dir().exists(),
        }

        for session_id, data in processes.items():
            output_path = Path(data.get("output", ""))
            debug_info["processes"][session_id] = {
                "pid": data.get("proc").pid if data.get("proc") else None,
                "output_path": str(output_path.absolute()),
                "file_exists": output_path.exists(),
                "file_size": output_path.stat().st_size if output_path.exists() else 0,
                "started_at": data.get("started_at"),
                "cleanup_key": data.get("cleanup_key"),
            }

        return debug_info
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/session/{ui_session_id}")
def debug_session(ui_session_id: str):
    """Debug a specific session by UI session ID."""
    try:
        session_processes = []

        for session_id, data in processes.items():
            cleanup_key = data.get("cleanup_key", "")
            if cleanup_key and cleanup_key.startswith(f"{ui_session_id}_"):
                output_path = Path(data.get("output", ""))
                session_processes.append({
                    "session_id": session_id,
                    "pid": data.get("proc").pid if data.get("proc") else None,
                    "output_path": str(output_path.absolute()),
                    "file_exists": output_path.exists(),
                    "file_size": output_path.stat().st_size if output_path.exists() else 0,
                    "started_at": data.get("started_at"),
                    "cleanup_key": data.get("cleanup_key"),
                    "process_alive": data.get("proc").poll() is None if data.get("proc") else False,
                })

        return {
            "ui_session_id": ui_session_id,
            "processes": session_processes,
            "total_processes": len(session_processes)
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/record/emergency-cleanup")
def emergency_cleanup():
    """Emergency cleanup - kill all processes and remove incomplete files."""
    try:
        cleaned_count = 0
        for session_id, data in list(processes.items()):
            try:
                if data.get("proc"):
                    print(f"🛑 Emergency killing process {session_id} (PID: {data['proc'].pid})")
                    data["proc"].kill()
                    data["proc"].wait(timeout=3)

                # Remove incomplete files
                output_path = Path(data.get("output", ""))
                if output_path.exists() and output_path.stat().st_size < 1000:
                    print(f"🗑️ Removing incomplete file: {output_path}")
                    output_path.unlink(missing_ok=True)

                cleaned_count += 1
            except Exception as e:
                print(f"⚠️ Error in emergency cleanup for {session_id}: {e}")

        processes.clear()
        print(f"✅ Emergency cleanup completed: {cleaned_count} processes killed")
        return {"cleaned_count": cleaned_count, "message": f"Emergency cleanup completed: {cleaned_count} processes killed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Emergency cleanup failed: {e}")

@app.post("/record/force-reset-session")
def force_reset_session(ui_session_id: str):
    """Force reset a specific session - kill all processes and clean up files."""
    try:
        cleaned_count = 0
        processes_to_remove = []

        # Find all processes for this session
        for session_id, data in list(processes.items()):
            cleanup_key = data.get("cleanup_key", "")
            if cleanup_key and cleanup_key.startswith(f"{ui_session_id}_"):
                processes_to_remove.append(session_id)

        print(f"🔄 Force resetting session {ui_session_id}, found {len(processes_to_remove)} processes")

        for session_id in processes_to_remove:
            try:
                data = processes[session_id]
                if data.get("proc"):
                    print(f"🛑 Killing process {session_id} (PID: {data['proc'].pid})")
                    data["proc"].kill()
                    data["proc"].wait(timeout=3)

                # Remove incomplete files
                output_path = Path(data.get("output", ""))
                if output_path.exists():
                    print(f"🗑️ Removing file: {output_path}")
                    output_path.unlink(missing_ok=True)

                # Remove timestamp files
                timestamp_file = data.get("timestamp_file")
                if timestamp_file and Path(timestamp_file).exists():
                    print(f"🗑️ Removing timestamp file: {timestamp_file}")
                    Path(timestamp_file).unlink(missing_ok=True)

                cleaned_count += 1
            except Exception as e:
                print(f"⚠️ Error cleaning up {session_id}: {e}")

        # Remove from processes dict
        for session_id in processes_to_remove:
            processes.pop(session_id, None)

        print(f"✅ Force reset completed: {cleaned_count} processes killed")
        return {
            "cleaned_count": cleaned_count,
            "message": f"Session {ui_session_id} force reset: {cleaned_count} processes killed"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Force reset failed: {e}")


# CORS for Netlify and localhost dev - Enhanced for private network access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "https://forgeoncricket.netlify.app",
        "https://*.netlify.app",  # Any Netlify subdomain
        "*",  # local utility
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "Access-Control-Allow-Private-Network"],
    expose_headers=["*", "Access-Control-Allow-Private-Network"],
)

# Add custom middleware for private network access (Chrome requirement)
@app.middleware("http")
async def add_private_network_headers(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("RECORDER_PORT", "7000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
