"""Microphone capture orchestration for the recording rig (Flask side).

The rig's microphone is an INMP441 I2S MEMS mic wired to one of the three camera
Raspberry Pis, not to the machine running Flask. Everything audio-related therefore
happens over SSH: this module copies ``remote_inmp441_capture.py`` to the chosen Pi,
starts it there with ``nohup`` when a take begins, asks it to stop when the take ends,
and ``scp``s the results back into the recording folder.

Who calls it (single module-level instance ``mic`` in ``app35_cam_sole.py``):

* ``POST /api/mic/assign`` -> ``assign()``: pick which camera Pi hosts the mic
  (``cam1``/``cam2``/``cam3`` or null). Initial value comes from ``MIC_CAMERA_KEY``.
* ``GET /api/mic/status`` -> ``snapshot(include_remote=True)``.
* ``start_combined`` -> ``start_for_recording()`` just before FFmpeg is launched;
  ``stop_combined`` -> ``stop_for_recording()`` after the cameras are finalised.
  Both are also used for rollback if the camera start fails.
* ``GET /api/mic/onset``, ``/api/mic/waveform``, ``/api/mic/live_waveform`` ->
  ``onset()``, ``waveform()``, ``live_waveform()`` for the UI's audio panel.
* Recording listings and the upload manifest use ``files_info()``.

Remote layout on the Pi (fixed): ``/tmp/forge_mic_capture/remote_inmp441_capture.py``
and ``/tmp/forge_mic_capture/current/`` holding ``mic_capture.wav``, ``onset_data.json``,
``live_waveform.json``, ``mic_capture.pid`` and ``mic_capture.log``. Only one capture at
a time is supported because that directory is wiped before every start.

Local artifacts produced per take, under ``<recording_dir>/audio/``:

* ``mic_capture.wav`` -- mono, 48 kHz, 32-bit signed PCM (see the remote script).
* ``onset_data.json`` -- the remote script's metadata: onset sample index, stream
  start times, ALSA device, arecord stderr, etc. The app's validation step checks
  the WAV; the onset is exposed to the UI for sync inspection.
"""

import array
import json
import os
import shlex
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


class MicCaptureManager:
    """Drive the remote INMP441 capture script on a camera Pi over SSH.

    Lifecycle: construct once at import time -> ``assign()`` a camera (or via env) ->
    per take ``start_for_recording()`` / ``stop_for_recording()``. There is no session
    retargeting here: the manager never stores the session directory, it receives the
    concrete ``recording_dir`` on every start/stop call.

    Threading: called from Flask request threads (status polls, waveform fetches) and
    from the recording control path concurrently. ``_lock`` guards the small state
    block (``assigned_camera_key``, ``capture_active``, ``active_recording_dir``,
    ``last_result``, ``last_error``, ``last_onset``, ``last_audio_file``). The lock is
    never held while SSH/scp runs; every remote call is a blocking ``subprocess.run``
    with an explicit timeout on the calling thread. Note ``last_audio_file`` and
    ``last_onset`` are written in ``stop_for_recording`` without the lock.

    Error strategy: ``assign`` raises ``ValueError`` (mapped to HTTP 400 by the app).
    ``start_for_recording`` and ``stop_for_recording`` never raise; they return a dict
    with ``ok``/``skipped``/``message`` (or ``errors``) and record ``last_error``, so a
    missing or broken mic never blocks a take. Private ``_ensure_remote_script`` raises
    ``RuntimeError``; ``subprocess.TimeoutExpired`` from SSH propagates to the callers
    above, which catch ``Exception``.
    """

    def __init__(
        self,
        *,
        base_dir: Path,
        camera_bootstrap: dict,
        ssh_user: str,
        script_path: Path,
        initial_camera_key: str | None = None,
    ):
        """Store configuration; performs no I/O.

        Args:
            base_dir: Repository root; paths in returned payloads are relative to it.
            camera_bootstrap: The app's ``CAMERA_BOOTSTRAP`` mapping
                (``cam1``.. -> ``{"host": ..., ...}``); only ``host`` is used here.
            ssh_user: Login user on the Pis (``PI_SSH_USER``, default ``pi``). Key-based
                auth must already be set up; nothing here can answer a password prompt.
            script_path: Local path of ``remote_inmp441_capture.py`` to deploy.
            initial_camera_key: Pre-assigned mic host; ignored if not in
                ``camera_bootstrap``.
        """
        self.base_dir = base_dir
        self.camera_bootstrap = camera_bootstrap
        self.ssh_user = ssh_user
        self.script_path = script_path
        self.remote_dir = "/tmp/forge_mic_capture"
        self.remote_script = f"{self.remote_dir}/remote_inmp441_capture.py"
        self.remote_output_dir = f"{self.remote_dir}/current"
        self.assigned_camera_key = initial_camera_key if initial_camera_key in camera_bootstrap else None
        self.capture_active = False
        self.active_recording_dir: Path | None = None
        self.last_result = None
        self.last_error = None
        self.last_onset = None
        self.last_audio_file: Path | None = None
        self._lock = Lock()

    def assign(self, camera_key: str | None):
        """Choose which camera Pi hosts the microphone (``POST /api/mic/assign``).

        Args:
            camera_key: ``cam1``/``cam2``/``cam3``, or any of ``None``/``""``/``"none"``/
                ``"null"`` to unassign.

        Returns:
            ``snapshot(include_remote=True)``, so the UI immediately sees whether the
            Pi is reachable and whether the ALSA card is detected.

        Raises:
            ValueError: unknown key, or a capture is currently active.
        """
        if camera_key in ("", "none", "None", "null"):
            camera_key = None
        if camera_key is not None and camera_key not in self.camera_bootstrap:
            raise ValueError("camera_key must be cam1, cam2, cam3, or null")
        with self._lock:
            if self.capture_active:
                raise ValueError("Stop the current recording before changing mic assignment")
            self.assigned_camera_key = camera_key
            self.last_error = None
        return self.snapshot(include_remote=True)

    def _target(self, camera_key: str | None = None):
        """Return ``(camera_key, "user@host")`` for ``camera_key`` or the assigned camera.

        Returns ``(None, None)`` when nothing is assigned.
        """
        key = camera_key or self.assigned_camera_key
        if not key:
            return None, None
        cfg = self.camera_bootstrap[key]
        return key, f"{self.ssh_user}@{cfg['host']}"

    def _run_ssh(self, camera_key: str, command: str, timeout: float = 20):
        """Run a shell snippet on the Pi and return ``(returncode, stdout, stderr)``.

        The script is fed to ``bash -s`` on stdin rather than passed as an argument,
        which sidesteps a second layer of remote quoting. ssh itself exits 255 on
        connection failures, which ``_remote_status`` uses to distinguish "Pi
        unreachable" from "script failed".

        Raises:
            subprocess.TimeoutExpired: if the command does not finish in ``timeout`` s.
        """
        _, target = self._target(camera_key)
        proc = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", target, "bash", "-s"],
            input=command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _scp_from(self, camera_key: str, remote_path: str, local_path: Path, timeout: float = 30):
        """Copy one file from the Pi to ``local_path``; returns ``(returncode, stdout, stderr)``."""
        _, target = self._target(camera_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", f"{target}:{remote_path}", str(local_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _ensure_remote_script(self, camera_key: str):
        """Deploy ``remote_inmp441_capture.py`` to the Pi (mkdir, scp, chmod +x).

        Re-copied unconditionally before every status/start/stop call. That costs three
        SSH round trips but guarantees the Pi always runs the version checked into this
        repo, with no separate provisioning step for a fresh Pi.

        Raises:
            RuntimeError: local script missing, or any remote step fails.
        """
        if not self.script_path.exists():
            raise RuntimeError(f"Local mic capture script missing: {self.script_path}")
        mkdir_cmd = f"mkdir -p {shlex.quote(self.remote_dir)} {shlex.quote(self.remote_output_dir)}"
        code, out, err = self._run_ssh(camera_key, mkdir_cmd, timeout=10)
        if code != 0:
            raise RuntimeError((out + err).strip() or f"Could not create remote mic dir on {camera_key}")
        _, target = self._target(camera_key)
        proc = subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", str(self.script_path), f"{target}:{self.remote_script}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout + proc.stderr).strip() or f"Could not deploy mic script to {camera_key}")
        chmod_cmd = f"chmod +x {shlex.quote(self.remote_script)}"
        code, out, err = self._run_ssh(camera_key, chmod_cmd, timeout=10)
        if code != 0:
            raise RuntimeError((out + err).strip() or f"Could not chmod remote mic script on {camera_key}")

    def _remote_status(self, camera_key: str):
        """Run the remote script with ``--status`` and parse its JSON line.

        Returns the script's payload (``mic_detected``, ``alsa_device``, ``active``,
        ``pid``...) plus ``ssh_ok`` (False only on ssh exit code 255) and
        ``return_code``. On unparseable output returns an ``ok: False`` stub with the
        raw text in ``error``. Propagates exceptions from ``_ensure_remote_script`` and
        SSH timeouts.
        """
        self._ensure_remote_script(camera_key)
        cmd = f"python3 {shlex.quote(self.remote_script)} --status --output-dir {shlex.quote(self.remote_output_dir)}"
        code, out, err = self._run_ssh(camera_key, cmd, timeout=15)
        payload = {}
        try:
            # Only the last stdout line is JSON; anything before it would be login
            # banners or stray prints on the Pi.
            payload = json.loads((out or "").strip().splitlines()[-1])
        except Exception:
            payload = {
                "ok": False,
                "mic_detected": False,
                "active": False,
                "error": (out + err).strip() or "Could not parse remote mic status",
            }
        payload["ssh_ok"] = code != 255
        payload["return_code"] = code
        if err.strip():
            payload["stderr"] = err.strip()
        return payload

    def snapshot(self, include_remote: bool = False):
        """Status payload for ``GET /api/mic/status``.

        Args:
            include_remote: If True and a camera is assigned, also SSH to the Pi for
                ``_remote_status`` (several seconds worst case). Any failure there is
                folded into ``remote`` as an ``ok: False`` dict rather than raised.
        """
        with self._lock:
            assigned = self.assigned_camera_key
            active = self.capture_active
            last_error = self.last_error
            last_result = self.last_result
            last_onset = self.last_onset
        out = {
            "assigned": assigned is not None,
            "camera_key": assigned,
            "capture_active": active,
            "last_error": last_error,
            "last_result": last_result,
            "last_onset": last_onset,
            "remote_script": self.remote_script,
            "remote_output_dir": self.remote_output_dir,
        }
        if assigned and include_remote:
            try:
                out["remote"] = self._remote_status(assigned)
            except Exception as exc:
                out["remote"] = {
                    "ok": False,
                    "ssh_ok": False,
                    "mic_detected": False,
                    "active": False,
                    "error": str(exc),
                }
        return out

    def start_for_recording(self, recording_dir: Path, recording_index: int):
        """Start a capture on the assigned Pi for the take that is about to begin.

        Sequence: deploy script + query ``--status`` (bails out if no ALSA card), wipe
        the previous take's files from the remote output dir, then launch
        ``--capture`` in the background with ``nohup`` and read its PID from stdout.
        The audio stream starts before FFmpeg does (by however long the SSH steps
        take), which is why the remote script records its own start timestamps for sync.

        Args:
            recording_dir: Remembered as ``active_recording_dir`` so ``stop_for_recording``
                knows where to put the files.
            recording_index: Stored in ``last_result`` for the UI.

        Returns:
            ``{"ok": True, "skipped": True}`` when no camera is assigned;
            ``{"ok": False, "skipped": True, "message": ...}`` when the mic is missing
            or any SSH step fails (also sets ``last_error``); otherwise ``ok: True``
            with ``camera_key``, ``recording_index``, ``started_at`` and ``remote_pid``.
            Never raises.
        """
        with self._lock:
            camera_key = self.assigned_camera_key
        if not camera_key:
            return {"ok": True, "skipped": True, "message": "Mic capture skipped; no camera assigned"}

        try:
            status = self._remote_status(camera_key)
            if not status.get("mic_detected"):
                message = status.get("error") or status.get("alsa_card") or "INMP441 ALSA card not found"
                with self._lock:
                    self.last_error = message
                    self.capture_active = False
                return {"ok": False, "skipped": True, "camera_key": camera_key, "message": message, "status": status}

            cleanup = (
                f"mkdir -p {shlex.quote(self.remote_output_dir)}; "
                f"rm -f {shlex.quote(self.remote_output_dir)}/mic_capture.wav "
                f"{shlex.quote(self.remote_output_dir)}/onset_data.json "
                f"{shlex.quote(self.remote_output_dir)}/mic_capture.pid"
            )
            code, out, err = self._run_ssh(camera_key, cleanup, timeout=10)
            if code != 0:
                raise RuntimeError((out + err).strip() or "Remote mic cleanup failed")

            log_path = f"{self.remote_output_dir}/mic_capture.log"
            # nohup + all three stdio streams redirected: otherwise the capture dies
            # (or ssh hangs waiting on the open pipe) when this ssh session ends.
            # "echo $!" hands back the background PID as the last stdout line.
            start_cmd = (
                f"nohup python3 {shlex.quote(self.remote_script)} --capture "
                f"--output-dir {shlex.quote(self.remote_output_dir)} "
                f"> {shlex.quote(log_path)} 2>&1 < /dev/null & "
                "echo $!"
            )
            code, out, err = self._run_ssh(camera_key, start_cmd, timeout=10)
            if code != 0:
                raise RuntimeError((out + err).strip() or "Remote mic start failed")

            with self._lock:
                self.capture_active = True
                self.active_recording_dir = recording_dir
                self.last_error = None
                self.last_result = {
                    "ok": True,
                    "camera_key": camera_key,
                    "recording_index": recording_index,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "remote_pid": (out or "").strip().splitlines()[-1] if out.strip() else None,
                }
            return {"ok": True, "skipped": False, **self.last_result}
        except Exception as exc:
            with self._lock:
                self.capture_active = False
                self.last_error = str(exc)
            return {"ok": False, "skipped": True, "camera_key": camera_key, "message": str(exc)}

    def stop_for_recording(self, recording_dir: Path | None = None):
        """Stop the remote capture and pull its outputs into ``<recording_dir>/audio``.

        Runs the remote script with ``--stop`` (SIGTERM, then SIGKILL after 8 s; the
        script finalises the WAV header and writes ``onset_data.json`` on SIGTERM), then
        ``scp``s ``mic_capture.wav`` and ``onset_data.json`` back and parses the onset.
        Called by ``stop_combined`` after the cameras are finalised and by
        ``start_combined`` during rollback.

        Args:
            recording_dir: Destination take folder; falls back to the directory given to
                ``start_for_recording``.

        Returns:
            A result dict with ``ok``, ``camera_key``, ``audio_dir``, ``files``
            (name -> path/size), ``errors`` (list of strings), ``remote_stop`` and, when
            parsed, ``onset``. ``ok`` is True only when there were no errors and at
            least one file was pulled. ``skipped: True`` variants are returned when no
            camera is assigned or no active directory is known. Never raises; always
            clears ``capture_active``.
        """
        with self._lock:
            camera_key = self.assigned_camera_key
            active_dir = recording_dir or self.active_recording_dir
        if not camera_key:
            return {"ok": True, "skipped": True, "message": "Mic capture skipped; no camera assigned"}
        if active_dir is None:
            return {"ok": False, "skipped": True, "message": "No active recording directory for mic output"}

        audio_dir = active_dir / "audio"
        result = {
            "ok": False,
            "camera_key": camera_key,
            "audio_dir": str(audio_dir.relative_to(self.base_dir)),
            "files": {},
            "errors": [],
        }
        try:
            self._ensure_remote_script(camera_key)
            stop_cmd = f"python3 {shlex.quote(self.remote_script)} --stop --output-dir {shlex.quote(self.remote_output_dir)}"
            code, out, err = self._run_ssh(camera_key, stop_cmd, timeout=20)
            result["remote_stop"] = {"return_code": code, "stdout": out.strip(), "stderr": err.strip()}

            for remote_name, local_name in (
                ("mic_capture.wav", "mic_capture.wav"),
                ("onset_data.json", "onset_data.json"),
            ):
                local_path = audio_dir / local_name
                scp_code, scp_out, scp_err = self._scp_from(
                    camera_key,
                    f"{self.remote_output_dir}/{remote_name}",
                    local_path,
                    timeout=45,
                )
                if scp_code == 0 and local_path.exists():
                    stat = local_path.stat()
                    result["files"][local_name] = {
                        "path": str(local_path.relative_to(self.base_dir)),
                        "size_bytes": stat.st_size,
                    }
                    if local_name == "mic_capture.wav":
                        self.last_audio_file = local_path
                else:
                    result["errors"].append((scp_out + scp_err).strip() or f"Could not pull {remote_name}")

            onset_path = audio_dir / "onset_data.json"
            if onset_path.exists():
                try:
                    self.last_onset = json.loads(onset_path.read_text(encoding="utf-8"))
                    result["onset"] = self.last_onset
                except Exception as exc:
                    result["errors"].append(f"Could not parse onset_data.json: {exc}")

            result["ok"] = not result["errors"] and bool(result["files"])
            with self._lock:
                self.capture_active = False
                self.active_recording_dir = None
                self.last_result = result
                self.last_error = "; ".join(result["errors"]) if result["errors"] else None
            return result
        except Exception as exc:
            result["errors"].append(str(exc))
            with self._lock:
                self.capture_active = False
                self.active_recording_dir = None
                self.last_result = result
                self.last_error = str(exc)
            return result

    def onset(self):
        """Last parsed ``onset_data.json`` (from the latest stop), for ``GET /api/mic/onset``."""
        if self.last_onset is not None:
            return {"ok": True, "onset": self.last_onset}
        return {"ok": False, "onset": None, "message": "No mic onset data available yet"}

    def waveform(self, max_points: int = 600):
        """Downsampled peak envelope of the last pulled WAV, for ``GET /api/mic/waveform``.

        Reads the whole file into memory (a 60 s take at 48 kHz/32-bit is ~11 MB) and
        reduces it to at most ``max_points`` buckets. Each point carries ``t`` (seconds),
        ``peak`` (max |sample| in the bucket, 0..1) and ``value`` (the signed sample with
        the largest magnitude, so the UI can draw polarity). Falls back to the path in
        ``last_result`` if ``last_audio_file`` is unset (e.g. after a restart).

        Returns:
            ``ok: False`` with an empty ``points`` list when no WAV is available, it is
            empty, or the sample width is not 16/32-bit; otherwise ``ok: True`` with
            ``sample_rate``, ``sample_count``, ``duration_seconds``, ``onset_sample_index``,
            ``onset_seconds`` and ``points``.
        """
        with self._lock:
            audio_path = self.last_audio_file
            onset = self.last_onset
        if audio_path is None and self.last_result:
            file_info = (self.last_result.get("files") or {}).get("mic_capture.wav") if isinstance(self.last_result, dict) else None
            if file_info and file_info.get("path"):
                audio_path = self.base_dir / file_info["path"]
        if audio_path is None or not audio_path.exists():
            return {"ok": False, "message": "No mic waveform available yet", "points": []}

        with wave.open(str(audio_path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            raw = wav.readframes(frame_count)

        # The remote script writes S32_LE (width 4); 16-bit is accepted so a WAV
        # produced by other tooling can still be previewed. array typecodes "i"/"h"
        # are native-endian, which matches on the little-endian hosts the rig uses.
        if sample_width == 4:
            typecode = "i"
            scale = float(2 ** 31)
        elif sample_width == 2:
            typecode = "h"
            scale = float(2 ** 15)
        else:
            return {"ok": False, "message": f"Unsupported mic sample width: {sample_width}", "points": []}

        samples = array.array(typecode)
        # Drop a trailing partial sample: a capture killed mid-write can leave the
        # data chunk a few bytes short of a whole frame, and frombytes() would raise.
        samples.frombytes(raw[: len(raw) - (len(raw) % sample_width)])
        if channels > 1:
            # Keep channel 0 only (interleaved frames); the rig mic is mono anyway.
            samples = array.array(typecode, samples[::channels])
        total = len(samples)
        if total == 0:
            return {"ok": False, "message": "Mic audio is empty", "points": []}

        bucket_count = max(1, min(max_points, total))
        step = max(1, total // bucket_count)
        points = []
        for start in range(0, total, step):
            chunk = samples[start : min(total, start + step)]
            if not chunk:
                continue
            peak = max(abs(v) for v in chunk) / scale
            signed_peak = max(chunk, key=lambda v: abs(v)) / scale
            points.append(
                {
                    "t": start / sample_rate,
                    "peak": round(float(peak), 6),
                    "value": round(float(signed_peak), 6),
                }
            )
            if len(points) >= max_points:
                break

        onset_sample = onset.get("onset_sample_index") if isinstance(onset, dict) else None
        return {
            "ok": True,
            "path": str(audio_path.relative_to(self.base_dir)),
            "sample_rate": sample_rate,
            "sample_count": total,
            "duration_seconds": total / sample_rate,
            "onset_sample_index": onset_sample,
            "onset_seconds": (onset_sample / sample_rate) if onset_sample is not None else None,
            "points": points,
        }

    def files_info(self, rec_dir: Path):
        """List files in ``<rec_dir>/audio`` keyed by name (for recording listings/uploads)."""
        out = {}
        audio_dir = rec_dir / "audio"
        if not audio_dir.exists():
            return out
        for f in sorted(audio_dir.glob("*")):
            if not f.is_file():
                continue
            stat = f.stat()
            out[f.name] = {
                "filename": f.name,
                "path": str(f.relative_to(self.base_dir)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "mic_onset_json" if f.suffix.lower() == ".json" else "mic_audio",
            }
        return out

    def live_waveform(self):
        """Waveform for the UI's live meter (``GET /api/mic/live_waveform``).

        While a capture is active this ``cat``s ``live_waveform.json`` from the Pi (the
        remote script rewrites it every ~150 ms with the last 240 chunk peaks). When
        idle it returns ``waveform()`` of the last take tagged ``source: last_capture``.
        Never raises; SSH problems come back as ``ok: False`` with empty ``points``.
        """
        with self._lock:
            camera_key = self.assigned_camera_key
            active = self.capture_active
        if not camera_key:
            return {"ok": False, "message": "No mic camera assigned", "active": False, "points": []}
        if not active:
            payload = self.waveform()
            payload["active"] = False
            payload["source"] = "last_capture"
            return payload

        cmd = f"cat {shlex.quote(self.remote_output_dir)}/live_waveform.json"
        try:
            # Short timeout: this is polled frequently by the UI and must not pile up
            # SSH sessions if the Pi is slow. A miss just means "not ready yet".
            code, out, err = self._run_ssh(camera_key, cmd, timeout=2.0)
            if code != 0:
                return {
                    "ok": False,
                    "active": True,
                    "source": "remote_live",
                    "message": (out + err).strip() or "Live mic waveform is not ready yet",
                    "points": [],
                }
            payload = json.loads(out or "{}")
            payload["source"] = "remote_live"
            payload["camera_key"] = camera_key
            return payload
        except Exception as exc:
            return {
                "ok": False,
                "active": True,
                "source": "remote_live",
                "message": str(exc),
                "points": [],
            }
