#!/usr/bin/env python3
"""INMP441 microphone capture helper that runs ON the camera Raspberry Pi.

This file is not imported by the Flask app. ``MicCaptureManager`` (on the rig PC)
``scp``s it to ``/tmp/forge_mic_capture/remote_inmp441_capture.py`` on whichever Pi is
assigned as the mic host and invokes it over SSH in three modes:

* ``--status``: detect the I2S ALSA card and report whether a capture is running.
  Prints one JSON line to stdout; exit code 2 if no card is found.
* ``--capture``: record from ``arecord`` until SIGTERM/SIGINT, then run onset
  detection and write metadata. Launched with ``nohup ... &`` so it outlives the SSH
  session. Exit code 2 if no card is found (``onset_data.json`` then holds the error).
* ``--stop``: SIGTERM the running capture (SIGKILL after 8 s), then print ``--status``.

Hardware: an INMP441 I2S MEMS microphone attached to the Pi's I2S pins and exposed
through the ``googlevoicehat`` (or similar) ALSA driver. Audio is pulled with
``arecord -f S32_LE -r 48000 -c 1`` in raw mode; the INMP441 delivers 24-bit samples
left-justified in a 32-bit slot, so the low byte is padding.

Files written under ``--output-dir`` (default ``/tmp/forge_mic_capture/current``):

* ``mic_capture.wav`` -- mono, 48 kHz, 32-bit signed PCM, written incrementally.
* ``onset_data.json`` -- written once at the end of a capture: ``onset_sample_index``,
  ``stream_start_monotonic`` / ``stream_start_wall_time``, sample count, ALSA device
  and card text, detector parameters and ``arecord`` stderr. Atomic (tmp + rename).
* ``live_waveform.json`` -- rewritten every ~150 ms during capture with the last 240
  chunk peaks so the rig UI can show a live meter; final version has ``active: false``.
* ``mic_capture.pid`` -- PID of the running capture; removed on exit.

Must stay dependency-free (stdlib only) because it runs under the Pi's system
``python3`` with no virtualenv.
"""
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
# 512 samples * 4 bytes = 2 KiB per read (~10.7 ms of audio); small enough that the
# live waveform and a SIGTERM are noticed promptly.
CHUNK_SAMPLES = 512
# Onset = first point where the 5 ms moving-average envelope reaches 5% of the
# envelope peak seen anywhere in the take. Relative, so mic gain does not matter.
THRESHOLD_FRACTION = 0.05
WINDOW_MS = 5
LIVE_WAVEFORM_POINTS = 240
LIVE_WAVEFORM_WRITE_INTERVAL = 0.15

# Substrings (lower-cased, underscores removed) that identify the I2S mic card in
# "arecord -l" output. Covers the common Pi I2S overlays.
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
    """Find the ALSA capture card for the I2S mic.

    Returns:
        ``(device, detail)`` where ``device`` is ``"<card>,0"`` (device 0 of the card,
        suitable for ``plughw:``) and ``detail`` is the matching ``arecord -l`` line.
        If nothing matches but exactly one capture card exists, that card is used.
        On failure returns ``(None, text)`` where ``text`` is arecord's output/stderr
        for the status payload.
    """
    proc = subprocess.run(["arecord", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()

    card_rows = []
    for line in proc.stdout.splitlines():
        # arecord -l lines look like:
        #   card 1: sndrpigooglevoi [snd_rpi_googlevoicehat_soundcar], device 0: ...
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
    """Read the PID from ``mic_capture.pid``; ``None`` if missing or malformed."""
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_running(pid):
    """True if a process with ``pid`` exists (signal 0 probe); False for a falsy pid."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def detect_onset(samples, sample_rate, threshold_fraction=THRESHOLD_FRACTION, window_ms=WINDOW_MS):
    """Locate the first loud event in the take (e.g. a starting clap or foot strike).

    Builds a moving-average envelope of |sample| over a ``window_ms`` window, finds the
    envelope's global peak, then returns the index of the first envelope value that
    reaches ``threshold_fraction * peak``. Because the threshold is relative to the
    take's own peak, it needs the whole recording and is run once at the end.

    Args:
        samples: Signed 32-bit samples (``array('i')`` or list).
        sample_rate: Hz, used to convert ``window_ms`` to a sample count.

    Returns:
        ``(onset_index, envelope_peak, threshold)``. ``onset_index`` is ``None`` when
        there are no samples or the take is completely silent.
    """
    if not samples:
        return None, 0.0, 0.0

    scale = float(2 ** 31)
    abs_samples = [abs(v) / scale for v in samples]
    window_samples = max(1, int(sample_rate * window_ms / 1000))

    running = 0.0
    envelope = []
    queue = []
    peak = 0.0
    # Pure-Python running sum (no numpy on the Pi). queue.pop(0) is O(n) but the
    # window is only ~240 samples at 48 kHz, so this stays fast enough for a take.
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
    """Write ``payload`` as pretty JSON via tmp-file + rename; readers never see a partial file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_live_waveform(path, payload):
    """Like ``write_metadata`` but compact JSON; the rig ``cat``s this file over SSH mid-capture."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def capture(output_dir):
    """Record from the I2S mic until SIGTERM/SIGINT, then write onset metadata.

    Main loop: read fixed-size raw chunks from ``arecord``'s stdout, append them to the
    WAV, keep all samples in memory for the final onset pass, and periodically dump a
    live peak envelope. On stop, arecord is terminated, the WAV header is finalised by
    ``wave``'s context manager, and ``onset_data.json`` / ``live_waveform.json`` are
    written.

    Returns:
        Process exit code: 0 on success, 2 if no ALSA card was found (in which case
        ``onset_data.json`` holds ``ok: False`` and the arecord output).
    """
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

    # plughw: (not hw:) lets ALSA convert rate/format if the driver's native mode
    # differs; "-t raw -q" gives a headerless byte stream with no progress output.
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

    # The signal handler only sets a flag; the read loop checks it once per chunk so
    # the WAV is closed cleanly (valid header) instead of dying mid-write.
    def _stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    # Both clocks are captured just before arecord launches: monotonic for
    # onset_timestamp arithmetic, wall time so the rig can align sample 0 with the
    # cameras' wall-clock timeline. The real first-sample time is slightly later
    # (arecord startup), which is not corrected here.
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
                # Blocking read; returns short/empty only when arecord exits (device
                # unplugged, ALSA error) which ends the loop like a stop request.
                data = proc.stdout.read(bytes_per_chunk) if proc.stdout else b""
                if not data:
                    break
                # writeframesraw skips the per-call header rewrite; wave patches the
                # header sizes once when the context manager closes the file.
                wav.writeframesraw(data)
                chunk = array.array("i")
                # Truncate to whole 4-byte samples (a partial trailing sample would make
                # frombytes raise). S32_LE bytes are decoded as native int32; on a
                # big-endian host they would need swapping, hence the byteorder check.
                chunk.frombytes(data[: len(data) - (len(data) % SAMPLE_WIDTH_BYTES)])
                if sys.byteorder != "little":
                    chunk.byteswap()
                samples.extend(chunk)
                if chunk:
                    # One live point per chunk: the sample with the largest magnitude
                    # (signed, so the UI can show polarity) normalised to -1..1.
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
                    # Ring buffer of the most recent points; rewriting the file on
                    # every chunk (~90/s) would hammer the SD card, so rate-limit it.
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
        # Always reached, including on a stop request: stop arecord, collect its
        # stderr for the metadata, and drop the PID file so --status reports idle.
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
    # onset_timestamp is on the Pi's *monotonic* clock (same base as
    # stream_start_monotonic), not wall time; add (onset_index / RATE) to
    # stream_start_wall_time if an epoch value is needed.
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
    """Print one JSON line describing mic detection and capture state; exit 0/2.

    Exit code 2 means "no ALSA card". ``MicCaptureManager._remote_status`` parses the
    last stdout line and refuses to start a take when ``mic_detected`` is false.
    """
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
    """Stop a running capture (SIGTERM, wait up to 8 s, then SIGKILL) and print status.

    SIGTERM gives ``capture()`` the chance to close the WAV and write
    ``onset_data.json``; SIGKILL is the fallback for a wedged process and leaves the
    WAV header unfinalised (the rig's validation step tolerates that).
    """
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
    """CLI entry: exactly one of ``--status``, ``--stop``, ``--capture`` (checked in that order)."""
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
