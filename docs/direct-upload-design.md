# Design — rig → cloud direct upload

*Status: DESIGN, no code yet. 2026-08-20. Review before implementing any phase.*
*Absorbs checklist items R12 (auth enablement), R15 (stop-path worker), R20 (identity handshake), R23 (direct-to-cloud rescope).*

## Problem

Today an instance travels: **rig disk → browser fetch (`http://localhost:5000/download_file`) →
browser memory → `POST {api}/upload-instance` → GCS + MySQL**. The browser hop is the fragile
link:

- Chrome's Local Network Access enforcement broke it outright on 2026-08-19 (large loopback
  bodies aborted mid-transfer). The current fix — HEAD + 8 MB `Range` chunks in
  `frontend/src/lib/api.ts` (forgeon `fix/rig-download-lna-cache`) — is a workaround, and
  Chrome keeps tightening; browser updates on rig laptops are outside our control.
- Every byte crosses the laptop twice; 30–60 MB Blobs per view sit in tab memory.
- The tab must stay open until the upload finishes; a refresh loses the upload (files survive on
  the rig, but the operator must notice and retry).

Target: **the rig uploads recordings itself; the browser only orchestrates and observes.**

Hard constraint (standing project rule): the rig NEVER writes MySQL directly. All DB writes go
through Forgeon API endpoints — the rig is a client like any other.

## 1. Identity & auth (Decision #9 becomes concrete here)

- New forgeon table `rig_devices` (SQL migration): `id`, `name` (e.g. "office-rig"),
  `token_hash` (sha256 of the device token, never plaintext), `lan_token` (the inbound
  browser→rig token, retrievable — see §1b), `organization_id`/site,
  `created_at`, `last_seen_at`, `revoked_at`.
- Admin API to issue/rotate/revoke device tokens (admin-authenticated; token plaintext shown
  once at issue time).
- The rig stores its token in git-ignored config (`.env`: `FORGEON_API_URL`,
  `FORGEON_DEVICE_TOKEN`) and sends `Authorization: Bearer <token>` on every call to the API.
- Forgeon side: FastAPI dependency `require_rig_device` (hash the presented token, look up
  active device) used by the new `/rig/*` endpoints — parallel to the existing
  `get_current_admin_user` dependency, never replacing it.
- Inbound-to-rig auth is separate and out of scope here: the V13 app already carries the
  (dormant) `RIG_API_TOKEN` gate from the hardening port for browser→rig calls on the LAN;
  enabling it is an operator decision once the record page can send the token.

## 1b. Login & tokens (who logs in where — answer: only into forgeon)

Three legs, no new logins anywhere:

1. **Operator → forgeon frontend**: unchanged — the existing admin login/session. The operator
   NEVER logs into the rig separately; the forgeon login is the only login in the system.
2. **Browser → rig (LAN, `localhost:5000`)**: open today; the V13 app carries the dormant
   `RIG_API_TOKEN` gate from the hardening port. When it is enabled, the record page must present
   that token — delivery path: the token is stored in forgeon as `rig_devices.lan_token` and
   handed to the record page by an admin-authenticated forgeon API call after login. The
   operator's forgeon session therefore transitively authorizes rig access; nothing is typed on
   the rig, nothing is hardcoded in the frontend. (Until the gate is enabled, this leg keeps
   working with no token, exactly as today.)
3. **Rig → forgeon API**: the device token (`FORGEON_DEVICE_TOKEN`, Bearer) per §1.

**Two separate tokens, deliberately not one shared secret.** The inbound LAN token and the
outbound device token differ in direction, blast radius, and rotation story: compromising the
record page must not yield cloud-upload capability, and rotating the device token must not lock
operators out of the rig page. Both are managed under the same `rig_devices` row so the admin UI
shows one card per rig. The `lan_token` is retrievable by design (the browser must present it
verbatim to the rig, and the rig compares it against its env) — acceptable because it only
guards LAN access to the rig; the device token stays hash-only.

## 2. Upload path (reuse `upload-instance` semantics, don't fork them)

The existing browser flow ends at `POST {api}/upload-instance`
(`backend/app/dataapis.py:2273` — multipart: `assessment_id`, `instance_no`, `front/back/side/top_view`,
`parameters`, `activity_type`, `insole_file`, `hr_file`, `total_instances`; writes GCS via
`_get_gcs_bucket()`, inserts `technical_instances`, queues background processing).

Two rig-facing endpoints on the forgeon API:

- `POST /rig/instances/init` — body: `assessment_id`, `instance_no`, `parameters`,
  `activity_type`, `total_instances`, file manifest `[{field: "side_view", filename, size_bytes}]`.
  Returns per-file **GCS resumable signed upload URLs** (API-issued; the rig never holds GCS
  credentials) plus an `upload_id`.
- Rig `PUT`s each file straight to GCS (resumable protocol → per-chunk retry, survives network
  blips; 60 MB uploads no longer transit the API process at all).
- `POST /rig/instances/complete` — body: `upload_id` + per-file GCS object names + checksums.
  The API verifies the objects exist/size-match, then runs THE SAME code the browser path runs
  (instance row insert + background processing kickoff) — factored out of `upload_instance`
  into a shared function, not duplicated.

Fallback shape (if signed-URL plumbing is blocked): the rig POSTs the same multipart form to
`upload-instance` guarded by `require_rig_device` — identical semantics, heavier API path.
Decide at implementation time; the rig-side worker is agnostic (it targets an "uploader"
interface with two implementations).

## 3. Rig-side upload worker (delivers the useful part of R15)

In `app35_cam_sole.py`:

- A persistent upload queue at the APP level — `BASE_DIR/upload_queue.json` — NOT under
  `SESSION_DIR`: a new session dir is stamped at every app launch, so a session-scoped queue
  would orphan the previous session's pending balls on restart. Entries reference recording
  dirs by path relative to `BASE_DIR`, so balls from any past session remain uploadable.
  One background worker thread drains it (uploads are I/O-bound; the GIL objection to R15's
  *processing* worker doesn't apply to pure uploads).
- New routes (browser → rig, LAN):
  - `POST /api/upload_instance` — "upload recording N as assessment X / instance M with these
    parameters". Enqueues and returns immediately.
  - `GET /api/upload_queue` — per-item state: `queued | uploading (pct) | verifying | done | failed (error, attempts)`.
  - `POST /api/upload_retry` — re-enqueue a failed item.
- Retry policy: per-file resumable retries, then per-instance exponential backoff (bounded);
  failures stay visible in the queue, never silently dropped. `.uploaded` marker per recording
  dir once complete — the retention job (V14) may only delete recordings that carry it.

## 3b. Recovery & resume (stuck balls are waiting, never lost)

- The queue file + the recording folders on disk are the source of truth. A "stuck" ball is a
  queue entry in `failed`/`queued` — its files and its assessment/instance linkage are on disk,
  so it can be uploaded hours or days later without the original browser session.
- **Auto-resume is the default**: the worker reloads the queue at app start and drains it on a
  bounded backoff loop. Internet restored / laptop rebooted → uploads continue with no operator
  action.
- **Manual doors** when needed:
  - the rig's own page (`localhost:5000`) gets an upload-queue panel — per-ball state, attempts,
    last error, Retry / Retry-all-failed (works standing at the machine, no cloud needed);
  - the record page shows the same via `GET /api/upload_queue` polling.
- Init-at-upload means a late retry simply re-inits for fresh signed URLs; within one attempt,
  GCS resumable sessions continue a partially-transferred file.
- Safety rule: retention/cleanup may only ever delete a recording dir bearing the `.uploaded`
  marker — un-uploaded balls are immune to cleanup regardless of age.

## 4. Identity handshake (closes R20)

The record page passes `assessment_id`, `athlete_id`, `instance_no` to the rig at
`/api/upload_instance` time (and optionally at record-start for labeling). Recordings stop being
linked out-of-band: `recording_N ↔ assessment/instance` is recorded in the queue entry and in
`recording_N/upload.json`.

## 5. Browser flow after this

Record page: stop recording → collect parameters → `POST rig /api/upload_instance` → poll
`GET /api/upload_queue` and render per-instance progress (button contract in §5b). The current
download-then-upload path (chunked) STAYS as a manual fallback button until the new path has run
a full real session cleanly; then it demotes to a debug tool.

## 5b. Record-page upload-button contract

Per instance on the record page:

- Click **Upload** → `POST rig /api/upload_instance` with the parameters + assessment/instance
  identity. Enqueue is a local-disk write on the rig → returns ≲100 ms, even with no internet.
- On 200 (queued) the button for THAT instance disables immediately and becomes a state chip —
  the operator moves straight to the next delivery, zero wait.
- Chip states come from the `GET /api/upload_queue` poll, keyed by
  `(assessment_id, instance_no)`:
  `Queued → Uploading (pct) → Verifying → Uploaded ✓`, and `Failed (reason)` which re-enables
  the control as **Retry**.
- Because state is keyed on identity and read from the rig's queue — not held in component
  state — a page refresh, or a different laptop on the LAN, re-syncs every button. The tab is
  no longer the source of truth for upload progress.
- Double-click safety: enqueue is idempotent per `(assessment_id, instance_no)` — re-posting an
  already-queued/uploading instance returns the existing entry, never a duplicate.
- Re-record: re-recording an instance whose upload completed goes through the existing re-record
  path (new recording → new enqueue, overwriting per today's upload-instance semantics); a
  queued-but-not-started entry for that instance is replaced in the queue.

## Rollout phases (each its own PR, in order)

1. This design reviewed (user sign-off).
2. forgeon: migration (`rig_devices`) + token admin API + `require_rig_device` + `/rig/instances/init|complete`
   (+ the shared-code refactor of `upload_instance`). PR → dev; testable with `curl` end-to-end.
3. streamer: upload worker + queue + routes; config gains `FORGEON_API_URL` + `FORGEON_DEVICE_TOKEN`.
4. forgeon frontend: record page delegate + poll UI; fallback button kept.
5. One full live session on the office rig via the new path → then enable retention (V14) and
   consider flipping `RIG_API_TOKEN` on (browser sends it from the record page).

## Non-goals / explicitly out

- No direct MySQL writes from the rig, ever (standing rule).
- No change to processing pipelines — `complete` triggers the same background processing as today.
- Mic/audio hardening remains deferred (see `rig-hardening-checklist.md`).
- R11-main (fragmented MP4) and the WSGI-server swap stay separate decisions.
