"""Rig-side direct-upload worker (phase 3 of docs/direct-upload-design.md).

A persistent queue + one background thread that uploads recorded instances
straight to the Forgeon cloud, using the phase-2 API:

    POST {api}/rig/instances/init      -> GCS resumable session URLs + context token
    PUT  <session url>                 -> file bytes straight to GCS
    POST {api}/rig/instances/complete  -> instance row created, status 'uploaded'

Key properties (design §3/§3b):
  - Queue lives at BASE_DIR/upload_queue.json (NOT the session dir — session
    dirs rotate per app launch, and pending balls must survive restarts).
    Entries reference recording dirs relative to BASE_DIR, so balls from any
    past session stay uploadable.
  - Enqueue is a local disk write: works offline, returns immediately.
    Idempotent per (assessment_id, instance_no) — re-posting an in-flight
    instance returns the existing entry, never a duplicate.
  - Init-at-upload: URLs/context are fetched fresh when the worker picks the
    ball up, so queue time never staleness-kills an upload.
  - Auto-retry with bounded backoff; after MAX_AUTO_ATTEMPTS the entry stays
    'failed' (visible, manual retry) — never silently dropped.
  - A '.uploaded' marker is written into the recording dir on success; the
    future retention job may only delete marked dirs.

Stdlib-only (urllib), same as the app.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("rig.upload")

QUEUE_FILENAME = "upload_queue.json"
UPLOADED_MARKER = ".uploaded"

MAX_AUTO_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30.0     # 30s, 60s, 120s, 240s, 480s
BACKOFF_CAP_SECONDS = 900.0
HTTP_TIMEOUT_SECONDS = 30.0
PUT_TIMEOUT_SECONDS = 600.0     # one 60 MB file on slow ground wifi

ACTIVE_STATES = ("queued", "uploading", "verifying")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UploadWorker:
    def __init__(self, base_dir: Path, api_url: str, device_token: str):
        self.base_dir = Path(base_dir)
        self.api_url = api_url.rstrip("/")
        self.device_token = device_token
        self.queue_path = self.base_dir / QUEUE_FILENAME
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._entries: Dict[str, dict] = {}
        self._load()
        self._thread = threading.Thread(target=self._run, name="upload-worker", daemon=True)
        self._thread.start()
        log.info("Upload worker started (api=%s, %d entries restored)", self.api_url, len(self._entries))

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.device_token)

    # ------------------------------------------------------------------ queue

    def _key(self, assessment_id: str, instance_no: int) -> str:
        return f"{assessment_id}:{int(instance_no)}"

    def _load(self) -> None:
        try:
            if self.queue_path.exists():
                data = json.loads(self.queue_path.read_text(encoding="utf-8"))
                self._entries = {e["key"]: e for e in data.get("entries", [])}
                # A crash mid-upload leaves 'uploading'/'verifying' — those bytes
                # may or may not have landed; init-at-upload makes redoing safe.
                for e in self._entries.values():
                    if e["state"] in ("uploading", "verifying"):
                        e["state"] = "queued"
                        e["last_error"] = "app restarted mid-upload; requeued"
        except Exception:
            log.exception("Could not read %s — starting with an empty queue", self.queue_path)
            self._entries = {}

    def _persist_locked(self) -> None:
        tmp = self.queue_path.with_suffix(".json.tmp")
        payload = {"version": 1, "entries": list(self._entries.values())}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.queue_path)

    def enqueue(
        self,
        assessment_id: str,
        instance_no: int,
        recording_dir: str,
        files: Dict[str, str],
        parameters: Optional[dict] = None,
        activity_type: Optional[str] = None,
        total_instances: Optional[int] = None,
    ) -> dict:
        """Add one instance to the queue. files = {api_field: path relative to BASE_DIR}."""
        key = self._key(assessment_id, instance_no)
        with self._lock:
            existing = self._entries.get(key)
            if existing and existing["state"] in ACTIVE_STATES:
                return existing  # idempotent: double-click can't duplicate a ball
            entry = {
                "key": key,
                "assessment_id": assessment_id,
                "instance_no": int(instance_no),
                "recording_dir": recording_dir,
                "files": files,
                "parameters": parameters,
                "activity_type": activity_type,
                "total_instances": total_instances,
                "state": "queued",
                "attempts": 0,
                "next_retry_at": 0.0,
                "progress": None,
                "last_error": None,
                "instance_id": None,
                "enqueued_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            self._entries[key] = entry
            self._persist_locked()
        self._wake.set()
        log.info("Enqueued %s (%s)", key, ", ".join(files))
        return entry

    def retry(self, assessment_id: str, instance_no: int) -> Optional[dict]:
        key = self._key(assessment_id, instance_no)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry["state"] == "failed":
                entry["state"] = "queued"
                entry["next_retry_at"] = 0.0
                entry["last_error"] = None
                entry["updated_at"] = _now_iso()
                self._persist_locked()
        self._wake.set()
        return entry

    def snapshot(self) -> List[dict]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e["enqueued_at"])

    def _set(self, entry: dict, **updates: Any) -> None:
        with self._lock:
            entry.update(updates)
            entry["updated_at"] = _now_iso()
            self._persist_locked()

    # ------------------------------------------------------------------ HTTP

    def _api(self, method: str, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.device_token}",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read() or b"{}")

    def _put_file(self, upload_url: str, file_path: Path, content_type: str) -> None:
        data = file_path.read_bytes()
        req = urllib.request.Request(
            upload_url, data=data, method="PUT", headers={"Content-Type": content_type}
        )
        with urllib.request.urlopen(req, timeout=PUT_TIMEOUT_SECONDS):
            pass

    # ------------------------------------------------------------------ worker

    def _next_entry(self) -> Optional[dict]:
        now = time.time()
        with self._lock:
            for entry in sorted(self._entries.values(), key=lambda e: e["enqueued_at"]):
                if entry["state"] == "queued" and entry.get("next_retry_at", 0.0) <= now:
                    return entry
        return None

    def _run(self) -> None:
        while True:
            entry = self._next_entry()
            if entry is None:
                self._wake.wait(timeout=10.0)
                self._wake.clear()
                continue
            try:
                self._upload(entry)
            except Exception as exc:
                attempts = entry["attempts"] + 1
                delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_CAP_SECONDS)
                auto = attempts < MAX_AUTO_ATTEMPTS
                self._set(
                    entry,
                    state="queued" if auto else "failed",
                    attempts=attempts,
                    next_retry_at=(time.time() + delay) if auto else 0.0,
                    last_error=str(exc)[:500],
                    progress=None,
                )
                if auto:
                    log.warning("Upload %s failed (attempt %d, retry in %ds): %s",
                                entry["key"], attempts, int(delay), exc)
                else:
                    log.error("Upload %s failed permanently after %d attempts: %s — "
                              "waiting for manual retry", entry["key"], attempts, exc)

    def _upload(self, entry: dict) -> None:
        # Resolve + sanity-check files from disk NOW (they may be gone).
        resolved: Dict[str, Path] = {}
        for field, rel in entry["files"].items():
            p = self.base_dir / rel
            if not p.is_file() or p.stat().st_size == 0:
                raise RuntimeError(f"file for {field} missing or empty on disk: {rel}")
            resolved[field] = p

        self._set(entry, state="uploading", progress="init")
        manifest = [
            {"field": field, "filename": p.name, "size_bytes": p.stat().st_size}
            for field, p in resolved.items()
        ]
        init = self._api("POST", "/rig/instances/init", {
            "assessment_id": entry["assessment_id"],
            "instance_no": entry["instance_no"],
            "files": manifest,
            "parameters": entry.get("parameters"),
            "activity_type": entry.get("activity_type"),
            "total_instances": entry.get("total_instances"),
        })

        total = len(init["files"])
        for i, (field, spec) in enumerate(init["files"].items(), start=1):
            self._set(entry, progress=f"uploading {field} ({i}/{total})")
            self._put_file(spec["upload_url"], resolved[field], spec["content_type"])

        self._set(entry, state="verifying", progress="complete")
        done = self._api("POST", "/rig/instances/complete", {
            "context_token": init["context_token"],
            "uploaded_fields": list(init["files"].keys()),
        })

        # Success: mark the recording dir so retention can never eat an
        # un-uploaded ball, then finish the entry.
        try:
            marker = self.base_dir / entry["recording_dir"] / UPLOADED_MARKER
            marker.write_text(json.dumps({
                "instance_id": done.get("instance_id"),
                "assessment_id": entry["assessment_id"],
                "instance_no": entry["instance_no"],
                "uploaded_at": _now_iso(),
            }, indent=2), encoding="utf-8")
        except Exception:
            log.exception("Uploaded OK but could not write %s marker", UPLOADED_MARKER)

        self._set(entry, state="done", progress=None, last_error=None,
                  instance_id=done.get("instance_id"))
        log.info("Uploaded %s -> instance %s", entry["key"], done.get("instance_id"))
