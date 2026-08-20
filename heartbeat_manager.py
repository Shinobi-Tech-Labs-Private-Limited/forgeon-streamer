import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


class HeartbeatServiceError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value: str | None) -> datetime | None:
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
    """

    def __init__(self, *, base_dir: Path, session_dir: Path):
        self.base_dir = base_dir
        self.session_dir = session_dir
        self.service_url = os.environ.get("HEARTBEAT_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
        default_source = base_dir / "heartbeat_sessions"
        self.source_dir = Path(os.environ.get("HEARTBEAT_SESSIONS_DIR", str(default_source))).expanduser()
        self.timeout_seconds = float(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "0.8"))
        self.autostart_enabled = os.environ.get("HEARTBEAT_AUTOSTART", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
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
        try:
            self.latest()
            return True
        except HeartbeatServiceError:
            return False

    def start_sidecar(self) -> dict[str, Any]:
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

    def _request(self, path: str) -> dict[str, Any]:
        url = f"{self.service_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise HeartbeatServiceError(str(exc)) from exc

    def _post_json(self, path: str, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
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
        return self._request("/hr")

    def device(self) -> dict[str, Any]:
        return self._request("/device")

    def sidecar_recording(self) -> dict[str, Any]:
        return self._request("/recording")

    def devices(self) -> dict[str, Any]:
        previous_timeout = self.timeout_seconds
        try:
            self.timeout_seconds = max(previous_timeout, 8.0)
            return self._request("/devices")
        finally:
            self.timeout_seconds = previous_timeout

    def connect_device(self, device: str | None) -> dict[str, Any]:
        device_filter = (device or "").strip() or None
        if device_filter is None:
            raise HeartbeatServiceError("Select a heart-rate device before connecting")
        return self._post_json("/connect", {"device": device_filter}, timeout=8.0)

    def disconnect_device(self) -> dict[str, Any]:
        return self._post_json("/disconnect", {}, timeout=8.0)

    def _source_logs(self) -> list[Path]:
        if not self.source_dir.exists():
            return []
        return sorted(
            (p for p in self.source_dir.glob("hr_*.jsonl") if p.is_file() and not p.name.endswith(".pushed")),
            key=lambda p: p.stat().st_mtime,
        )

    def _iter_events(self, start: datetime, stop: datetime):
        for path in self._source_logs():
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event_ts = _parse_iso(payload.get("timestamp"))
                        if event_ts is None:
                            continue
                        if start <= event_ts <= stop:
                            yield payload
            except OSError:
                continue

    def _write_slice(self, start: datetime, stop: datetime, out_path: Path, meta_path: Path) -> dict[str, Any]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sample_count = 0
        with out_path.open("w", encoding="utf-8") as handle:
            for payload in self._iter_events(start, stop):
                handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
                sample_count += 1

        meta = {
            "ok": True,
            "start": _iso(start),
            "stop": _iso(stop),
            "source_dir": str(self.source_dir),
            "output": str(out_path.relative_to(self.base_dir)),
            "sample_count": sample_count,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def count_samples(self, start: datetime | None, stop: datetime | None = None) -> int:
        if start is None:
            return 0
        end = stop or _utc_now()
        return sum(1 for _ in self._iter_events(start, end))

    def start_session(self) -> dict[str, Any]:
        with self._lock:
            if self.session_active:
                return self.session_status()
            self.session_active = True
            self.session_start = _utc_now()
            self.session_stop = None
            self.last_error = None
        return self.session_status()

    def stop_session(self) -> dict[str, Any]:
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
            stop,
            out_dir / "session_hr.jsonl",
            out_dir / "session_hr_status.json",
        )
        with self._lock:
            self.last_session_file = out_dir / "session_hr.jsonl"
        return self.session_status(extra=result)

    def start_snippet(self, recording_dir: Path, recording_index: int) -> dict[str, Any]:
        with self._lock:
            self.current_snippet = {
                "recording_dir": recording_dir,
                "recording_index": recording_index,
                "start": _utc_now(),
            }
        return {"ok": True, "active": True, "recording_index": recording_index}

    def stop_snippet(self, recording_dir: Path | None = None) -> dict[str, Any]:
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
            stop,
            out_dir / f"heart_rate_recording_{rec_index}.jsonl",
            out_dir / f"heart_rate_recording_{rec_index}_status.json",
        )
        with self._lock:
            self.last_snippet_file = out_dir / f"heart_rate_recording_{rec_index}.jsonl"
        return result

    def session_status(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
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
