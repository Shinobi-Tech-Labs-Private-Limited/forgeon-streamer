"""Heart-rate strap integration for the recording rig (Flask side).

The rig does not talk to the BLE heart-rate strap directly. A separate "Heartbeat"
sidecar process (an external project, launched here via ``python -m heartbeat serve``)
owns the BLE connection, exposes a small HTTP API on localhost (default
``http://127.0.0.1:8000``) and appends every notification it receives to continuous
``hr_*.jsonl`` logs under ``HEARTBEAT_SESSIONS_DIR`` (default ``<base_dir>/heartbeat_sessions``).

This module provides :class:`HeartbeatManager`, which the app uses to:

* launch / stop / inspect that sidecar (``start_sidecar`` at app boot and via
  ``POST /api/heartbeat/service/start``; ``stop_sidecar`` is registered with ``atexit``);
* proxy live status to the UI (``/api/heartbeat/hr``, ``/device``, ``/devices``,
  ``/connect``, ``/disconnect``, ``/status``);
* cut the sidecar's continuous logs into per-session and per-recording slices so the
  heart-rate data lives next to the videos it belongs to.

Time-window bookkeeping is done on the Flask side: ``start_session``/``stop_session``
(``POST /api/heartbeat/session/start|stop`` and ``start_new_session``) and
``start_snippet``/``stop_snippet`` (called from ``start_combined``/``stop_combined``
around each take) only remember wall-clock timestamps, then re-read the sidecar logs
and copy matching rows out.

On-disk artifacts (all JSON Lines, one sidecar event per line, plus a JSON status file):

* ``<session_dir>/heartbeat/session_hr.jsonl`` and ``session_hr_status.json`` -- the
  manual, whole-session slice.
* ``<recording_dir>/heartbeat/heart_rate_recording_<N>.jsonl`` and
  ``heart_rate_recording_<N>_status.json`` -- the per-take slice. The app's
  ``_sync_heartbeat_file`` later trims this to the video window and writes
  ``<recording_dir>/sync/heart_rate_sync.jsonl``.
* ``<session_dir>/heartbeat_sidecar.log`` -- stdout/stderr of the managed sidecar.

Configuration is entirely via environment variables (``HEARTBEAT_SERVICE_URL``,
``HEARTBEAT_SESSIONS_DIR``, ``HEARTBEAT_TIMEOUT_SECONDS``, ``HEARTBEAT_TAIL_GRACE_SECONDS``,
``HEARTBEAT_AUTOSTART``, ``HEARTBEAT_PROJECT_DIR``, ``HEARTBEAT_PYTHON``, ``HEARTBEAT_PORT``,
``HEARTBEAT_DEVICE``); see ``HeartbeatManager.__init__``.
"""

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any


log = logging.getLogger("rig.heartbeat")

# Files whose mtime predates a slice start by more than this cannot contain
# events inside the slice window; they are skipped unopened.
MTIME_SKEW_GRACE_SECONDS = 60.0


class HeartbeatServiceError(RuntimeError):
    """The Heartbeat sidecar HTTP API could not be reached or returned garbage.

    Raised by every proxy method (``latest``, ``device``, ``devices``, ``connect_device``,
    ``disconnect_device``) and by ``connect_device`` when no device was selected. Routes
    in the app translate it into an error JSON response; ``session_status`` and
    ``is_service_available`` swallow it and report ``service_available: false`` instead.
    """
    pass


def _utc_now() -> datetime:
    """Timezone-aware UTC "now"; all window bookkeeping in this module is UTC."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 UTC string with millisecond precision."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse the sidecar's ``timestamp`` field into an aware UTC datetime.

    Accepts a trailing ``Z`` (which ``datetime.fromisoformat`` rejected before 3.11).
    Returns ``None`` for empty or unparseable input so callers can simply skip the row.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


class HeartbeatManager:
    """Flask-side coordinator for the Heartbeat sidecar.

    The sidecar owns BLE and writes continuous hr_*.jsonl logs. This class only
    proxies live status and slices those logs into files that belong to the Flask
    session/recording folders.

    Lifecycle (one instance, module-level ``heartbeat`` in the app):

    1. Construct with ``base_dir`` (repo root, used to relativise paths in responses)
       and the current ``session_dir``.
    2. ``start_sidecar()`` at app boot (``__main__``) or on demand from the UI. It is a
       best-effort launcher: failures are returned in the dict, not raised.
    3. ``start_session()`` / ``stop_session()`` bracket a manual whole-session HR log.
       ``start_snippet()`` / ``stop_snippet()`` bracket each recording take.
    4. When the app rolls to a new session (``start_new_session``) it retargets this
       object in place by assigning ``session_dir``, ``sidecar_log_path``,
       ``last_session_file`` and ``last_snippet_file`` directly, so the sidecar and its
       BLE connection survive the rollover. Nothing here caches ``session_dir``-derived
       paths, which is what makes that safe.
    5. ``stop_sidecar()`` via ``atexit``.

    Threading: Flask serves requests on multiple threads, so any route may call any
    method concurrently with the recording control path. ``_lock`` guards only the
    small window-bookkeeping state (``session_active``, ``session_start``,
    ``session_stop``, ``current_snippet``, ``last_*_file``, ``last_error``). The lock
    is never held across HTTP calls or file scans; slices are written outside it.
    ``_sidecar_proc`` is unguarded and is only expected to be touched from the boot
    thread, the service-start route and ``atexit``. The lock is a plain
    ``threading.Lock`` (not reentrant), so no method may call another lock-taking
    method while holding it.

    Errors: HTTP proxy methods raise ``HeartbeatServiceError``; ``stop_session``
    raises ``RuntimeError`` if nothing is active; everything that touches the
    sidecar process or the filesystem returns ``{"ok": False, ...}`` and records
    ``last_error`` instead of raising, so a missing strap never blocks a take.
    """

    def __init__(self, *, base_dir: Path, session_dir: Path):
        """Read configuration from the environment; does not start anything.

        Args:
            base_dir: Repository root. Output paths in status payloads are made
                relative to it for the UI.
            session_dir: Current session folder. Reassigned by the app on session
                rollover (see class docstring).
        """
        self.base_dir = base_dir
        self.session_dir = session_dir
        self.service_url = os.environ.get("HEARTBEAT_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
        default_source = base_dir / "heartbeat_sessions"
        self.source_dir = Path(os.environ.get("HEARTBEAT_SESSIONS_DIR", str(default_source))).expanduser()
        # Default HTTP timeout is deliberately short (0.8 s): session_status() is
        # polled at ~1 Hz by the UI and makes two of these calls per poll, so a
        # dead sidecar must fail fast rather than stall the status endpoint.
        self.timeout_seconds = float(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "0.8"))
        # Slice windows are extended by this much past the requested stop and
        # trimmed downstream (_sync_heartbeat_file cuts to the video window),
        # so the last in-flight notification is not lost at the boundary.
        self.tail_grace_seconds = float(os.environ.get("HEARTBEAT_TAIL_GRACE_SECONDS", "1.5"))
        self.autostart_enabled = os.environ.get("HEARTBEAT_AUTOSTART", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        # The sidecar is a separate checkout with its own virtualenv; the default
        # path is the location on the original rig image and should normally be
        # overridden with HEARTBEAT_PROJECT_DIR / HEARTBEAT_PYTHON on a new machine.
        default_project = Path("/home/shikhar/Downloads/heartbeat/heartbeat")
        self.project_dir = Path(os.environ.get("HEARTBEAT_PROJECT_DIR", str(default_project))).expanduser()
        default_python = self.project_dir / ".venv-linux" / "bin" / "python"
        if not default_python.exists():
            default_python = self.project_dir / ".venv" / "bin" / "python"
        self.python_bin = Path(os.environ.get("HEARTBEAT_PYTHON", str(default_python))).expanduser()
        self.port = int(os.environ.get("HEARTBEAT_PORT", "8000"))
        self.device_filter = os.environ.get("HEARTBEAT_DEVICE", "").strip() or None
        self.sidecar_log_path = session_dir / "heartbeat_sidecar.log"
        self._sidecar_proc: subprocess.Popen | None = None

        self.session_active = False
        self.session_start: datetime | None = None
        self.session_stop: datetime | None = None
        self.current_snippet: dict[str, Any] | None = None
        self.last_session_file: Path | None = None
        self.last_snippet_file: Path | None = None
        self.last_error: str | None = None
        self._lock = Lock()

    def is_service_available(self) -> bool:
        """Return True if the sidecar answers ``GET /hr`` within the default timeout."""
        try:
            self.latest()
            return True
        except HeartbeatServiceError:
            return False

    def start_sidecar(self) -> dict[str, Any]:
        """Launch the Heartbeat sidecar process if it is not already answering.

        Called once at app boot and from ``POST /api/heartbeat/service/start``. The
        sidecar is started detached (``start_new_session=True``) with stdout/stderr
        appended to ``sidecar_log_path`` so it survives Flask's reloader and its
        BLE chatter does not pollute the app log.

        Returns:
            A dict that always has ``ok``. Variants: ``skipped`` (autostart disabled),
            ``already_running``, ``started`` (with ``pid``), or ``ok: False`` with a
            ``message`` when the project/python is missing, the process exits at
            once, or Popen fails. Never raises; failures are stored in ``last_error``.
        """
        if not self.autostart_enabled:
            return {"ok": True, "skipped": True, "message": "Heartbeat autostart disabled"}
        if self.is_service_available():
            return {"ok": True, "already_running": True, "service_url": self.service_url}
        if not self.project_dir.exists():
            msg = f"Heartbeat project directory not found: {self.project_dir}"
            self.last_error = msg
            return {"ok": False, "message": msg}
        if not self.python_bin.exists():
            msg = f"Heartbeat Python not found: {self.python_bin}"
            self.last_error = msg
            return {"ok": False, "message": msg}

        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.sidecar_log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        # The sidecar uses a src/ layout and is run un-installed, hence PYTHONPATH.
        src_path = self.project_dir / "src"
        env["PYTHONPATH"] = str(src_path)
        cmd = [
            str(self.python_bin),
            "-m",
            "heartbeat",
            "serve",
            "--port",
            str(self.port),
            "--sessions-dir",
            str(self.source_dir),
        ]
        if self.device_filter:
            cmd.extend(["--device", self.device_filter])
        try:
            logf = self.sidecar_log_path.open("a", encoding="utf-8", buffering=1)
            logf.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} starting heartbeat sidecar ===\n")
            logf.write("CMD: " + " ".join(cmd) + "\n")
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_dir),
                env=env,
                stdout=logf,
                stderr=logf,
                start_new_session=True,
            )
            self._sidecar_proc = proc
            # Poll for up to ~5 s (20 x 0.25 s). BLE discovery can take longer than
            # that, so timing out here is still reported as ok/started; the UI keeps
            # polling /api/heartbeat/status until the service answers.
            for _ in range(20):
                if proc.poll() is not None:
                    msg = f"Heartbeat sidecar exited immediately with code {proc.returncode}"
                    self.last_error = msg
                    return {"ok": False, "message": msg, "log_path": str(self.sidecar_log_path)}
                if self.is_service_available():
                    self.last_error = None
                    return {
                        "ok": True,
                        "started": True,
                        "pid": proc.pid,
                        "service_url": self.service_url,
                        "source_dir": str(self.source_dir),
                        "log_path": str(self.sidecar_log_path.relative_to(self.base_dir)),
                    }
                time.sleep(0.25)
            return {
                "ok": True,
                "started": True,
                "pid": proc.pid,
                "service_url": self.service_url,
                "source_dir": str(self.source_dir),
                "log_path": str(self.sidecar_log_path.relative_to(self.base_dir)),
                "message": "Heartbeat sidecar started; waiting for BLE/service readiness",
            }
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "message": str(exc)}

    def stop_sidecar(self) -> None:
        """Terminate the sidecar we launched (SIGTERM, then SIGKILL after 8 s).

        Registered with ``atexit``. A sidecar that was already running when the app
        started (``already_running``) is not ours and is left alone.
        """
        proc = self._sidecar_proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._sidecar_proc = None

    def managed_status(self) -> dict[str, Any]:
        """Describe the sidecar process we manage (embedded in ``session_status``)."""
        proc = self._sidecar_proc
        return {
            "autostart_enabled": self.autostart_enabled,
            "managed": proc is not None,
            "pid": proc.pid if proc is not None else None,
            "process_running": bool(proc is not None and proc.poll() is None),
            "project_dir": str(self.project_dir),
            "python": str(self.python_bin),
            "device_filter": self.device_filter,
            "log_path": str(self.sidecar_log_path.relative_to(self.base_dir)),
        }

    def _request(self, path: str, timeout: float | None = None) -> dict[str, Any]:
        """GET ``path`` from the sidecar and decode JSON.

        Raises:
            HeartbeatServiceError: on any network, timeout or decode failure.
        """
        url = f"{self.service_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout or self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise HeartbeatServiceError(str(exc)) from exc

    def _post_json(self, path: str, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """POST ``body`` as JSON to the sidecar and decode the JSON reply.

        Raises:
            HeartbeatServiceError: on any network, timeout or decode failure.
        """
        url = f"{self.service_url}{path}"
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise HeartbeatServiceError(str(exc)) from exc

    def latest(self) -> dict[str, Any]:
        """Most recent heart-rate sample (sidecar ``GET /hr``); backs ``/api/heartbeat/hr``."""
        return self._request("/hr")

    def device(self) -> dict[str, Any]:
        """Currently connected strap, as reported by the sidecar (``GET /device``)."""
        return self._request("/device")

    def sidecar_recording(self) -> dict[str, Any]:
        """The sidecar's own recording state (``GET /recording``). Not used by the app routes."""
        return self._request("/recording")

    def devices(self) -> dict[str, Any]:
        """Trigger a BLE scan and list candidate straps (``GET /devices``).

        Uses a longer timeout (at least 8 s) because the sidecar scans synchronously.
        """
        # Per-request timeout: the old try/finally mutated the instance-wide
        # timeout_seconds, and two overlapping calls could leave it stuck at
        # 8.0 for every 1 Hz status poll until restart.
        return self._request("/devices", timeout=max(self.timeout_seconds, 8.0))

    def connect_device(self, device: str | None) -> dict[str, Any]:
        """Ask the sidecar to connect to ``device`` (name or address as returned by ``devices``).

        Raises:
            HeartbeatServiceError: if ``device`` is empty, or the sidecar call fails.
        """
        device_filter = (device or "").strip() or None
        if device_filter is None:
            raise HeartbeatServiceError("Select a heart-rate device before connecting")
        return self._post_json("/connect", {"device": device_filter}, timeout=8.0)

    def disconnect_device(self) -> dict[str, Any]:
        """Ask the sidecar to drop the current strap connection (``POST /disconnect``)."""
        return self._post_json("/disconnect", {}, timeout=8.0)

    def _source_logs(self) -> list[Path]:
        """All sidecar ``hr_*.jsonl`` logs, oldest-modified first.

        Sorting by mtime lets ``_iter_events`` keep rows in roughly chronological order
        across sidecar restarts (each restart opens a new file). The ``.pushed`` check
        appears to be a guard against files renamed after being uploaded elsewhere; the
        glob already excludes them, so it is defensive only.
        """
        if not self.source_dir.exists():
            return []
        return sorted(
            (p for p in self.source_dir.glob("hr_*.jsonl") if p.is_file() and not p.name.endswith(".pushed")),
            key=lambda p: p.stat().st_mtime,
        )

    def _iter_events(self, start: datetime, stop: datetime, stats: dict[str, int] | None = None):
        """Yield every sidecar event whose ``timestamp`` falls in ``[start, stop]``.

        Scans the raw log files each time it is called (there is no index), so cost is
        proportional to the total size of the sidecar's logs. Files whose mtime is older
        than ``start`` (minus ``MTIME_SKEW_GRACE_SECONDS``) are skipped without opening.
        Unparseable lines and unreadable files are counted in ``stats`` (if given) and
        otherwise ignored; nothing is raised.

        Args:
            start, stop: Aware UTC bounds, inclusive.
            stats: Optional dict that receives ``files_scanned``, ``files_skipped_mtime``,
                ``parse_errors`` and ``read_errors`` counters.
        """
        for path in self._source_logs():
            try:
                if path.stat().st_mtime < start.timestamp() - MTIME_SKEW_GRACE_SECONDS:
                    if stats is not None:
                        stats["files_skipped_mtime"] = stats.get("files_skipped_mtime", 0) + 1
                    continue
            except OSError:
                pass
            if stats is not None:
                stats["files_scanned"] = stats.get("files_scanned", 0) + 1
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            if stats is not None:
                                stats["parse_errors"] = stats.get("parse_errors", 0) + 1
                            continue
                        event_ts = _parse_iso(payload.get("timestamp"))
                        if event_ts is None:
                            continue
                        if start <= event_ts <= stop:
                            yield payload
            except OSError:
                if stats is not None:
                    stats["read_errors"] = stats.get("read_errors", 0) + 1
                continue

    def _write_slice(self, start: datetime, stop: datetime, out_path: Path, meta_path: Path) -> dict[str, Any]:
        """Copy events in ``[start, stop]`` to ``out_path`` (JSONL) and write ``meta_path``.

        The output rows are the sidecar's payloads verbatim (compact JSON, one per
        line). The metadata file records the window, the counters from ``_iter_events``
        and ``sample_count`` (rows minus disconnect sentinels), which the app uses to
        warn about zero-sample takes. ``out_path`` is truncated and rewritten even when
        no events match, so a stale slice from a previous run is never left behind.

        Returns:
            The metadata dict that was written, with ``ok: True``.
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stats: dict[str, int] = {}
        total_rows = 0
        sentinel_rows = 0
        with out_path.open("w", encoding="utf-8") as handle:
            for payload in self._iter_events(start, stop, stats=stats):
                handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
                total_rows += 1
                # connected:false rows are disconnect sentinels, not heart-rate
                # samples — counting them let a fully-disconnected take report
                # sample_count 1 and dodge the zero-samples warning.
                if payload.get("connected") is False:
                    sentinel_rows += 1

        read_errors = stats.get("read_errors", 0)
        parse_errors = stats.get("parse_errors", 0)
        if read_errors or parse_errors:
            log.warning(
                "Heartbeat slice %s hit %d unreadable file(s) and %d unparseable line(s) — slice may be partial",
                out_path.name,
                read_errors,
                parse_errors,
            )
        meta = {
            "ok": True,
            "start": _iso(start),
            "stop": _iso(stop),
            "source_dir": str(self.source_dir),
            "output": str(out_path.relative_to(self.base_dir)),
            "sample_count": total_rows - sentinel_rows,
            "total_rows": total_rows,
            "sentinel_rows": sentinel_rows,
            "read_errors": read_errors,
            "parse_errors": parse_errors,
            "files_scanned": stats.get("files_scanned", 0),
            "files_skipped_mtime": stats.get("files_skipped_mtime", 0),
            "tail_grace_seconds": self.tail_grace_seconds,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def count_samples(self, start: datetime | None, stop: datetime | None = None) -> int:
        """Number of sidecar events between ``start`` and ``stop`` (default: now).

        Unlike ``_write_slice`` this counts disconnect sentinels too. It rescans the
        logs on every call and is invoked twice per ``session_status`` poll.
        """
        if start is None:
            return 0
        end = stop or _utc_now()
        return sum(1 for _ in self._iter_events(start, end))

    def start_session(self) -> dict[str, Any]:
        """Mark the start of a manual whole-session heart-rate log.

        Only records ``session_start``; no data is copied until ``stop_session``.
        Called from ``POST /api/heartbeat/session/start`` and by the app's
        ``start_new_session`` to carry an active session across a rollover.

        Returns:
            ``session_status()``.
        """
        with self._lock:
            if self.session_active:
                # NOTE: session_status() re-acquires the non-reentrant _lock, so this
                # early return (second start while already active) blocks forever.
                # Left as-is in this documentation pass; see the class docstring.
                return self.session_status()
            self.session_active = True
            self.session_start = _utc_now()
            self.session_stop = None
            self.last_error = None
        return self.session_status()

    def stop_session(self) -> dict[str, Any]:
        """Close the manual session log and write ``<session_dir>/heartbeat/session_hr.jsonl``.

        The slice window runs from ``session_start`` to now plus ``tail_grace_seconds``.

        Returns:
            ``session_status()`` with the slice metadata under ``result``.

        Raises:
            RuntimeError: if no session log is active.
        """
        with self._lock:
            if not self.session_active or self.session_start is None:
                raise RuntimeError("No Heartbeat session recording is active")
            start = self.session_start
            stop = _utc_now()
            self.session_active = False
            self.session_stop = stop

        out_dir = self.session_dir / "heartbeat"
        result = self._write_slice(
            start,
            stop + timedelta(seconds=self.tail_grace_seconds),
            out_dir / "session_hr.jsonl",
            out_dir / "session_hr_status.json",
        )
        with self._lock:
            self.last_session_file = out_dir / "session_hr.jsonl"
        return self.session_status(extra=result)

    def start_snippet(self, recording_dir: Path, recording_index: int) -> dict[str, Any]:
        """Mark the start of a per-take slice (called by ``start_combined`` before FFmpeg).

        Always succeeds and never touches the sidecar: the strap may be disconnected and
        the take still proceeds. Silently replaces any snippet that was still open.

        Args:
            recording_dir: ``<session_dir>/recording_<N>``; where the slice will be written.
            recording_index: N, used in the output file name.
        """
        with self._lock:
            self.current_snippet = {
                "recording_dir": recording_dir,
                "recording_index": recording_index,
                "start": _utc_now(),
            }
        return {"ok": True, "active": True, "recording_index": recording_index}

    def stop_snippet(self, recording_dir: Path | None = None) -> dict[str, Any]:
        """Close the per-take slice and write ``heart_rate_recording_<N>.jsonl``.

        Called by ``stop_combined`` after the camera processes have been finalised (so
        the HR data brackets the end of the video) and by ``start_combined`` on rollback.

        Args:
            recording_dir: Overrides the directory remembered by ``start_snippet``;
                the app passes the real ``current_recording_dir``.

        Returns:
            The slice metadata from ``_write_slice`` (``ok``, ``sample_count``, ...), or
            ``{"ok": True, "skipped": True}`` if no snippet was open. Note ``ok`` does
            not depend on ``sample_count``; the app checks for zero separately.
        """
        with self._lock:
            snippet = self.current_snippet
            self.current_snippet = None
        if not snippet:
            return {"ok": True, "skipped": True, "message": "No Heartbeat snippet was active"}

        rec_dir = recording_dir or snippet["recording_dir"]
        rec_index = int(snippet["recording_index"])
        start = snippet["start"]
        stop = _utc_now()
        out_dir = rec_dir / "heartbeat"
        result = self._write_slice(
            start,
            stop + timedelta(seconds=self.tail_grace_seconds),
            out_dir / f"heart_rate_recording_{rec_index}.jsonl",
            out_dir / f"heart_rate_recording_{rec_index}_status.json",
        )
        with self._lock:
            self.last_snippet_file = out_dir / f"heart_rate_recording_{rec_index}.jsonl"
        return result

    def session_status(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Full status payload for ``GET /api/heartbeat/status`` (polled ~1 Hz by the UI).

        Combines a snapshot of the local window state (taken under the lock) with two
        live sidecar calls (``/hr`` and ``/device``) and two log rescans
        (``count_samples`` for the session and the snippet). Sidecar failures are
        reported via ``service_available`` / ``service_error``, never raised.

        Args:
            extra: Attached under ``result`` (used by ``stop_session`` to return the
                slice metadata alongside the status).
        """
        with self._lock:
            start = self.session_start
            stop = self.session_stop
            active = self.session_active
            current = dict(self.current_snippet) if self.current_snippet else None
            last_session_file = self.last_session_file
            last_snippet_file = self.last_snippet_file
            last_error = self.last_error

        latest = None
        device = None
        service_available = True
        service_error = None
        try:
            latest = self.latest()
        except HeartbeatServiceError as exc:
            service_available = False
            service_error = str(exc)
        try:
            device = self.device()
        except HeartbeatServiceError as exc:
            service_available = False
            service_error = service_error or str(exc)

        payload = {
            "service_available": service_available,
            "service_error": service_error,
            "service_url": self.service_url,
            "source_dir": str(self.source_dir),
            "managed_sidecar": self.managed_status(),
            "latest": latest,
            "device": device,
            "session_recording": {
                "active": active,
                "start": _iso(start) if start else None,
                "stop": _iso(stop) if stop else None,
                "sample_count": self.count_samples(start, None if active else stop),
                "path": str(last_session_file.relative_to(self.base_dir)) if last_session_file else None,
            },
            "snippet": {
                "active": current is not None,
                "recording_index": current.get("recording_index") if current else None,
                "start": _iso(current.get("start")) if current else None,
                "sample_count": self.count_samples(current.get("start")) if current else 0,
                "last_path": str(last_snippet_file.relative_to(self.base_dir)) if last_snippet_file else None,
            },
            "last_error": last_error,
        }
        if extra:
            payload["result"] = extra
        return payload

    def files_info(self, rec_dir: Path) -> dict[str, Any]:
        """List files in ``<rec_dir>/heartbeat`` keyed by name (for recording listings/uploads)."""
        out = {}
        hb_dir = rec_dir / "heartbeat"
        if not hb_dir.exists():
            return out
        for f in sorted(hb_dir.glob("*")):
            if not f.is_file():
                continue
            stat = f.stat()
            out[f.name] = {
                "filename": f.name,
                "path": str(f.relative_to(self.base_dir)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "heartbeat_jsonl" if f.suffix.lower() == ".jsonl" else "heartbeat_metadata",
            }
        return out

    def session_files_info(self) -> dict[str, Any]:
        """Same as ``files_info`` but for the session-level ``session_hr*`` files."""
        out = {}
        hb_dir = self.session_dir / "heartbeat"
        if not hb_dir.exists():
            return out
        for f in sorted(hb_dir.glob("*")):
            if not f.is_file():
                continue
            stat = f.stat()
            out[f.name] = {
                "filename": f.name,
                "path": str(f.relative_to(self.base_dir)),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "type": "heartbeat_session_jsonl" if f.suffix.lower() == ".jsonl" else "heartbeat_metadata",
            }
        return out
