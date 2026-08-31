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

Who drives it (app35_cam_sole.py):
  - Constructed once at import time when credentials are found (env
    FORGEON_API_URL + FORGEON_DEVICE_TOKEN, else BASE_DIR/rig_device.json),
    or later by POST /api/pairing/claim once the operator pairs the rig.
    The app treats "upload_worker is None" as "not paired" and refuses to
    record until it exists.
  - POST /api/upload_instance -> enqueue()
  - GET  /api/upload_queue    -> snapshot()
  - POST /api/upload_retry    -> retry()

Queue entry states: queued -> uploading -> verifying -> done, or failed.
Entry fields (plain JSON; the dict is returned verbatim to the browser):
  key, assessment_id, instance_no, recording_dir, files, parameters,
  activity_type, total_instances, state, attempts, next_retry_at (epoch
  seconds; 0.0 = eligible now), progress (free-text stage shown in the UI),
  last_error, instance_id (cloud id once done), enqueued_at, updated_at.

Error strategy: every exception inside one upload attempt (file missing on
disk, HTTP 4xx/5xx, timeout, bad JSON) is handled the same way by _run():
the entry goes back to 'queued' with a backoff deadline, or to 'failed'
once the attempt budget is spent. There is no per-status-code handling, so
a permanent 4xx (e.g. unknown assessment) is retried until the budget runs
out and then parks as 'failed' with the last error text attached.
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

# Retry budget. A failed attempt n waits 30 * 2**(n-1) seconds before the
# next one; once attempts reaches MAX_AUTO_ATTEMPTS the entry parks as
# 'failed' and waits for a human (POST /api/upload_retry). With these values
# the fifth failure is terminal, so the 480 s step is computed but never
# waited, and BACKOFF_CAP_SECONDS never actually binds; both are kept so the
# numbers can be raised without touching _run().

MAX_AUTO_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30.0     # 30s, 60s, 120s, 240s, 480s
BACKOFF_CAP_SECONDS = 900.0
# init/complete are small JSON round-trips; a PUT carries a whole video file,
# hence the two very different timeouts.
HTTP_TIMEOUT_SECONDS = 30.0
PUT_TIMEOUT_SECONDS = 600.0     # one 60 MB file on slow ground wifi

# States in which a repeat enqueue for the same (assessment_id, instance_no)
# is answered with the existing entry. A 'done' or 'failed' entry is NOT
# active, so re-posting it (re-record, or a retry from the record page)
# replaces it with a fresh entry.

ACTIVE_STATES = ("queued", "uploading", "verifying")


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 form, used for every *_at field in the queue."""
    return datetime.now(timezone.utc).isoformat()


class UploadWorker:
    """Persistent upload queue plus the single daemon thread that drains it.

    Responsibility: own BASE_DIR/upload_queue.json and turn each entry into
    the init -> PUT -> complete sequence against the Forgeon API.

    Threading model:
      - Flask request threads call the public API: enqueue(), retry(),
        snapshot(), configured.
      - One daemon thread ("upload-worker", started in __init__) runs _run()
        forever and is the only caller of _upload().
      - _lock guards _entries and every write of the queue file. All state
        mutation goes through enqueue()/retry()/_set(), each of which
        persists under the lock, so the file on disk is always a complete,
        consistent snapshot.
      - _wake is an Event that lets enqueue()/retry() cut short the worker's
        10 s idle wait so a new ball starts uploading immediately.
      - Entry dicts are shared by reference: the worker mutates them under
        _lock and snapshot()/enqueue()/retry() hand the same objects to the
        caller. Treat returned entries as read-only.

    Lifecycle: constructed once per process (import time, or on first
    pairing). There is no stop(); the thread is a daemon and dies with the
    app. Restart recovery lives in _load(): entries caught mid-upload are
    requeued.

    Error strategy: nothing raised by an upload escapes the thread. See
    _run() for the retry/backoff policy.
    """

    def __init__(self, base_dir: Path, api_url: str, device_token: str):
        """Load the queue from disk and start the worker thread.

        Args:
            base_dir: the app's BASE_DIR. The queue file lives here, and
                every recording_dir / file path stored in an entry is
                relative to it.
            api_url: Forgeon API root, e.g. https://api-dev-new.forgelabs.in/dev
                (a trailing slash is stripped).
            device_token: Bearer token for this rig, minted by pairing
                (POST /rig/devices/claim) or by POST /rig/devices.
        """
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
        """True when both an API URL and a device token were supplied.

        Not consulted by app35_cam_sole.py today: the app gates on whether the
        worker object exists at all (None = not paired).
        """
        return bool(self.api_url and self.device_token)

    # ------------------------------------------------------------------ queue

    def _key(self, assessment_id: str, instance_no: int) -> str:
        """Queue identity: "<assessment_id>:<instance_no>", the same pair the
        record page keys its per-instance state chips on."""
        return f"{assessment_id}:{int(instance_no)}"

    def _load(self) -> None:
        """Restore the queue from upload_queue.json at startup (no lock needed:
        called from __init__ before the worker thread exists).

        Missing file = empty queue. An unreadable/corrupt file is logged and
        also treated as empty rather than crashing the app; the old file is
        overwritten on the next persist.
        """
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
        """Write the whole queue to disk. Caller must hold _lock.

        Writes to a sibling .tmp file and os.replace()s it over the real one so
        a crash or power cut mid-write can never leave a truncated queue file
        behind (the previous complete version survives instead).
        """
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
        # Called by POST /api/upload_instance on a Flask thread.
        #
        # Args:
        #   assessment_id / instance_no: cloud identity of this delivery; together
        #       they form the queue key.
        #   recording_dir: e.g. "sessions/session_.../recording_3", relative to
        #       BASE_DIR so the entry stays valid after a restart rotates the
        #       session dir.
        #   files: {"side_view": "sessions/.../cam1_sync_side.mp4", ...,
        #       "hr_file": ..., "insole_file": ...} - the multipart field names the
        #       upload-instance API uses, mapped to BASE_DIR-relative paths.
        #       Files are NOT checked here; they are validated in _upload().
        #   parameters / activity_type / total_instances: forwarded unchanged to
        #       /rig/instances/init.
        #
        # Returns the live entry dict (existing one if the instance is already
        # active). Only a local disk write happens here, so this works offline
        # and returns in milliseconds.
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
        """Re-queue a 'failed' entry for an immediate attempt (POST /api/upload_retry).

        Returns the entry, or None if no entry exists for that key. Entries in
        any state other than 'failed' are returned untouched (a retry on an
        in-flight or done entry is a harmless no-op).

        Note: attempts is deliberately not reset, so a manual retry gets exactly
        one more attempt; if that fails the entry parks as 'failed' again
        straight away instead of restarting the automatic backoff ladder.
        """
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
        """All entries, oldest-first, for GET /api/upload_queue.

        The list is new but the dicts are the live entry objects; do not mutate.
        """
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e["enqueued_at"])

    def _set(self, entry: dict, **updates: Any) -> None:
        """Apply field updates to an entry, stamp updated_at, persist.

        The single choke-point for state changes made by the worker thread.
        """
        with self._lock:
            entry.update(updates)
            entry["updated_at"] = _now_iso()
            self._persist_locked()

    # ------------------------------------------------------------------ HTTP

    def _api(self, method: str, path: str, payload: dict) -> dict:
        """JSON call to the Forgeon API with the device Bearer token.

        Returns the decoded response body ({} for an empty body).
        Raises urllib.error.HTTPError on any non-2xx status, URLError when the
        host is unreachable, and socket.timeout after HTTP_TIMEOUT_SECONDS. All
        of these propagate to _run()'s catch-all and count as one failed attempt.
        """
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
        """Upload one file's bytes to its GCS resumable session URL in a single PUT.

        The whole file is read into memory first (recordings are tens of MB,
        fine on the rig laptop). content_type is echoed exactly as the init
        response declared it for that session. The rig does no chunking of its
        own: if this PUT fails, the entire attempt is redone from a fresh init
        (see _upload).
        """
        data = file_path.read_bytes()
        req = urllib.request.Request(
            upload_url, data=data, method="PUT", headers={"Content-Type": content_type}
        )
        with urllib.request.urlopen(req, timeout=PUT_TIMEOUT_SECONDS):
            pass

    # ------------------------------------------------------------------ worker

    def _next_entry(self) -> Optional[dict]:
        """Oldest 'queued' entry whose backoff deadline has passed, or None.

        FIFO by enqueue time, so balls upload in delivery order. An entry that
        is still inside its backoff window is skipped, so one failing ball does
        not block the ones queued behind it.
        """
        now = time.time()
        with self._lock:
            for entry in sorted(self._entries.values(), key=lambda e: e["enqueued_at"]):
                if entry["state"] == "queued" and entry.get("next_retry_at", 0.0) <= now:
                    return entry
        return None

    def _run(self) -> None:
        """Worker thread main loop: pick an eligible entry, upload it, repeat.

        Retry policy (design section 3): a failed attempt n (1-based) requeues the
        entry with next_retry_at = now + min(30 * 2**(n-1), cap) seconds while
        n < MAX_AUTO_ATTEMPTS; the MAX_AUTO_ATTEMPTS-th failure flips the entry
        to 'failed', where it stays visible until retry() is called. The
        exception text (truncated to 500 chars) is stored as last_error so the
        record page can show the reason.
        """
        while True:
            entry = self._next_entry()
            if entry is None:
                # Idle poll every 10 s even without a wake: that is how an
                # expired backoff deadline (next_retry_at) gets noticed, since
                # nothing signals _wake when a timer runs out.
                self._wake.wait(timeout=10.0)
                self._wake.clear()
                continue
            try:
                self._upload(entry)
            except Exception as exc:
                attempts = entry["attempts"] + 1
                # attempts counts failed attempts so far, including this one.
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
        """One complete upload attempt for an entry (worker thread only).

        Stages, each reflected in entry["progress"] for the UI:
          1. resolve files on disk (raises if any is missing/empty)
          2. POST /rig/instances/init with the file manifest -> per-field
             {upload_url, content_type} plus a context_token
          3. PUT each file to its upload_url
          4. POST /rig/instances/complete with the context_token -> the cloud
             verifies the objects and creates the instance row
          5. write the .uploaded marker into the recording dir, mark 'done'

        Any exception at any stage aborts the attempt and is handled by _run().
        """
        # Resolve + sanity-check files from disk NOW (they may be gone).
        resolved: Dict[str, Path] = {}
        for field, rel in entry["files"].items():
            p = self.base_dir / rel
            if not p.is_file() or p.stat().st_size == 0:
                raise RuntimeError(f"file for {field} missing or empty on disk: {rel}")
            resolved[field] = p

        self._set(entry, state="uploading", progress="init")
        # Init-at-upload: signed URLs are minted now, not at enqueue time, so an
        # entry that sat in the queue for hours (no internet) never presents an
        # expired URL. A retry after a failure also starts from a fresh init;
        # the partially-used GCS session from the previous attempt is simply
        # abandoned.
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
        # Iterate the API's answer rather than our manifest: the field set,
        # upload URLs and content types are all dictated by the init response,
        # and the same key list is echoed back to /complete below.
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
        # A marker failure is logged, not raised: the cloud already holds the
        # instance, so failing the entry here would only trigger a re-upload.
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
