#!/usr/bin/env python3
import argparse
import array
import json
import os
import re
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path


RATE = 48000
CHANNELS = 1
FORMAT = "S32_LE"
SAMPLE_WIDTH_BYTES = 4
CHUNK_SAMPLES = 512
THRESHOLD_FRACTION = 0.05
WINDOW_MS = 5
LIVE_WAVEFORM_POINTS = 240
LIVE_WAVEFORM_WRITE_INTERVAL = 0.15

CARD_MATCH_TOKENS = (
    "googlevoicehat",
    "google voicehat",
    "voicehat",
    "soundcard",
    "inmp441",
    "i2s",
    "mems",
)


def detect_alsa_card():
    proc = subprocess.run(["arecord", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()

    card_rows = []
    for line in proc.stdout.splitlines():
        match = re.search(r"card\s+(\d+):\s*([^\[]+)(?:\[(.*?)\])?", line, re.IGNORECASE)
        if not match:
            continue
        card = int(match.group(1))
        text = line.strip()
        lower = text.lower().replace("_", "")
        card_rows.append((card, text, lower))

    for card, text, lower in card_rows:
        if any(token in lower for token in CARD_MATCH_TOKENS):
            return f"{card},0", text

    if len(card_rows) == 1:
        card, text, _ = card_rows[0]
        return f"{card},0", text

    return None, proc.stdout.strip()


def read_pid(pid_path):
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def detect_onset(samples, sample_rate, threshold_fraction=THRESHOLD_FRACTION, window_ms=WINDOW_MS):
    if not samples:
        return None, 0.0, 0.0

    scale = float(2 ** 31)
    abs_samples = [abs(v) / scale for v in samples]
    window_samples = max(1, int(sample_rate * window_ms / 1000))

    running = 0.0
    envelope = []
    queue = []
    peak = 0.0
    for value in abs_samples:
        queue.append(value)
        running += value
        if len(queue) > window_samples:
            running -= queue.pop(0)
        env = running / len(queue)
        envelope.append(env)
        if env > peak:
            peak = env

    if peak <= 0:
        return None, peak, 0.0

    threshold = peak * threshold_fraction
    for idx, value in enumerate(envelope):
        if value >= threshold:
            return idx, peak, threshold
    return None, peak, threshold


def write_metadata(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_live_waveform(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def capture(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    pid_path = output_dir / "mic_capture.pid"
    wav_path = output_dir / "mic_capture.wav"
    json_path = output_dir / "onset_data.json"
    live_path = output_dir / "live_waveform.json"

    device, card_detail = detect_alsa_card()
    if not device:
        write_metadata(
            json_path,
            {
                "ok": False,
                "error": "INMP441 ALSA card not found",
                "alsa_output": card_detail,
                "sample_rate": RATE,
            },
        )
        return 2

    cmd = [
        "arecord",
        "-D",
        f"plughw:{device}",
        "-f",
        FORMAT,
        "-r",
        str(RATE),
        "-c",
        str(CHANNELS),
        "-t",
        "raw",
        "-q",
    ]

    stop_requested = False

    def _stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    stream_start = time.monotonic()
    wall_start = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    samples = array.array("i")
    live_points = []
    sample_cursor = 0
    last_live_write = 0.0
    bytes_per_chunk = CHUNK_SAMPLES * SAMPLE_WIDTH_BYTES * CHANNELS

    try:
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH_BYTES)
            wav.setframerate(RATE)

            while not stop_requested:
                data = proc.stdout.read(bytes_per_chunk) if proc.stdout else b""
                if not data:
                    break
                wav.writeframesraw(data)
                chunk = array.array("i")
                chunk.frombytes(data[: len(data) - (len(data) % SAMPLE_WIDTH_BYTES)])
                if sys.byteorder != "little":
                    chunk.byteswap()
                samples.extend(chunk)
                if chunk:
                    signed_peak = max(chunk, key=lambda v: abs(v))
                    peak = abs(signed_peak) / float(2 ** 31)
                    live_points.append(
                        {
                            "t": sample_cursor / RATE,
                            "value": round(signed_peak / float(2 ** 31), 6),
                            "peak": round(peak, 6),
                        }
                    )
                    sample_cursor += len(chunk)
                    live_points = live_points[-LIVE_WAVEFORM_POINTS:]
                    now = time.monotonic()
                    if now - last_live_write >= LIVE_WAVEFORM_WRITE_INTERVAL:
                        write_live_waveform(
                            live_path,
                            {
                                "ok": True,
                                "active": True,
                                "stream_start_monotonic": stream_start,
                                "stream_start_wall_time": wall_start,
                                "sample_rate": RATE,
                                "sample_count": sample_cursor,
                                "duration_seconds": sample_cursor / RATE,
                                "points": live_points,
                            },
                        )
                        last_live_write = now
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        stderr = ""
        try:
            stderr = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
        except Exception:
            stderr = ""
        try:
            pid_path.unlink()
        except OSError:
            pass

    onset_index, env_peak, threshold = detect_onset(samples, RATE)
    onset_timestamp = None if onset_index is None else stream_start + (onset_index / RATE)
    payload = {
        "ok": True,
        "stream_start_monotonic": stream_start,
        "stream_start_wall_time": wall_start,
        "onset_sample_index": onset_index,
        "onset_timestamp": onset_timestamp,
        "sample_rate": RATE,
        "sample_count": len(samples),
        "duration_seconds": len(samples) / RATE if RATE else 0,
        "alsa_device": f"plughw:{device}",
        "alsa_card": card_detail,
        "format": FORMAT,
        "channels": CHANNELS,
        "threshold_fraction": THRESHOLD_FRACTION,
        "window_ms": WINDOW_MS,
        "envelope_peak": env_peak,
        "threshold": threshold,
        "arecord_stderr": stderr,
    }
    write_metadata(json_path, payload)
    write_live_waveform(
        live_path,
        {
            "ok": True,
            "active": False,
            "stream_start_monotonic": stream_start,
            "stream_start_wall_time": wall_start,
            "sample_rate": RATE,
            "sample_count": len(samples),
            "duration_seconds": len(samples) / RATE if RATE else 0,
            "onset_sample_index": onset_index,
            "onset_seconds": (onset_index / RATE) if onset_index is not None else None,
            "points": live_points[-LIVE_WAVEFORM_POINTS:],
        },
    )
    return 0


def status(output_dir):
    pid_path = output_dir / "mic_capture.pid"
    pid = read_pid(pid_path)
    device, card_detail = detect_alsa_card()
    payload = {
        "ok": True,
        "mic_detected": bool(device),
        "alsa_device": f"plughw:{device}" if device else None,
        "alsa_card": card_detail,
        "active": pid_running(pid),
        "pid": pid,
        "output_dir": str(output_dir),
        "wav_path": str(output_dir / "mic_capture.wav"),
        "json_path": str(output_dir / "onset_data.json"),
    }
    print(json.dumps(payload))
    return 0 if device else 2


def stop(output_dir):
    pid_path = output_dir / "mic_capture.pid"
    pid = read_pid(pid_path)
    if pid and pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 8
        while time.time() < deadline and pid_running(pid):
            time.sleep(0.1)
        if pid_running(pid):
            os.kill(pid, signal.SIGKILL)
    return status(output_dir)


def main():
    parser = argparse.ArgumentParser(description="Remote INMP441 capture helper")
    parser.add_argument("--output-dir", default="/tmp/forge_mic_capture/current")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if args.status:
        raise SystemExit(status(output_dir))
    if args.stop:
        raise SystemExit(stop(output_dir))
    if args.capture:
        raise SystemExit(capture(output_dir))
    parser.error("one of --capture, --status, or --stop is required")


if __name__ == "__main__":
    main()
