# Rig Hardening Checklist — R1–R24

> ## V13 port status (2026-08-20, branch `feat/rig-v13-hardening`)
> The hardened code on forgeon `fix/rig-v11-hardening` (`8588a3b1`) **never reached the rig** —
> the Aug-17 V13 was built from an unhardened V12. This branch re-applies the hardening onto the
> V13 codebase in this repo. Per-finding status **for this repo**:
>
> - **Ported by this branch** (into `app35_cam_sole.py` + `heartbeat_manager.py`):
>   R8 (spawn rollback, recording flag after alive-check) · R9 (BLE 409s) ·
>   R10 both halves (`heartbeat_warning`, sentinel-excluded counts, tail grace, surfaced errors) ·
>   R11 decision-free (per-camera 15 s stop grace, `+faststart` off the live encode) ·
>   R13 (`Path.is_relative_to` at all path-serving sites) ·
>   R14 camera sites (`CAMERA_SSH_OPTS`, `RIG_SSH_STRICT` gate) ·
>   R16 (per-request timeout, mtime prefilter, heartbeat-devices 409) ·
>   R17 V11-half (STALE/503 focus gate on `_preview_is_healthy`; V13's cube/board `measure_frame` kept) ·
>   R18 V11-internal (`CAMERA_HOSTS`/`CAMERA_STREAM_ROLES` single source) ·
>   R19 full (SessionLogHandler, per-subsystem logs, `log_exception`, global error handler, print conversion) ·
>   R7 (BLE insole WAL: fsync-cadence write-ahead log, frozen L/R map, atomic replace,
>   clear-after-durable, startup recovery, `wal_errors`) ·
>   R4 V11-half (validator `header_ok` WAV tolerance check).
> - **Ported but dormant**: R12 inbound token gate (`RIG_API_TOKEN`, default unset = open). Enablement
>   and the rig→cloud auth story are designed together in `docs/direct-upload-design.md` (Decision #9).
> - **Already present in V13 before this branch**: R17 standalone half (normalized scorer,
>   conditional CAP_DSHOW, lazy readers, stale-503 — `codesharpnessmeasure/` carried the hardened
>   lineage and evolved cube/board targets on top).
> - **Deferred as-is (deliberate, 2026-08-20)**: all mic/audio hardening — R1–R6 mic parts,
>   R14 mic sites, R4 Pi half. `mic_capture_manager.py` / `remote_inmp441_capture.py` stay at their
>   rig (unhardened) versions; the hardened implementations remain on forgeon
>   `fix/rig-v11-hardening:backend/app/` for a future pass. The app deliberately does NOT pass
>   `ssh_opts` to `MicCaptureManager` until that pass.
> - **Still open / gated** (unchanged from below): R11-main (fragmented MP4, Decision #10) ·
>   R12 WSGI half + Decision #9 · R13 rig-side `ls` check · R15 (superseded by the direct-upload
>   worker design) · R18 cross-app topology · R20–R24 ⚖. R15/R20/R23 are addressed structurally by
>   `docs/direct-upload-design.md`.
>
> Browser contract note: the Forgeon frontend downloads recordings via HEAD + 8 MB `Range` chunks
> (Chrome LNA workaround, forgeon `fix/rig-download-lna-cache`). `/download_file` must keep serving
> correct `Content-Length` on HEAD, honoring `Range` (206), and exposing `Content-Length` via CORS.


*Tracking sheet for the findings in `09_rig_review_with_gap_column.md` · Created 7 August 2026 · Updated 8 August 2026*
*Implementation branch: `fix/rig-v11-hardening` (off dev). The companion modules (`mic_capture_manager.py`, `heartbeat_manager.py`, `remote_inmp441_capture.py`, `codesharpnessmeasure/`) are now IN the repo under `backend/app/` next to the V11 app — the Downloads copies here are the deploy hand-off, regenerated from the repo.*

**Status: 22 of 24 findings fully or half done.** Remaining open: R11-main (⚖ #10 fragmented MP4), R12 WSGI-server half (⚖ #9 network posture), R13 rig-side `ls` check, R15 (deliberately deferred — changes the stop-response contract), R18 cross-app topology (needs Pi truth), and the ⚖ contract items R20–R24.

> ⚠ **Deploy note:** the rig runs from the recording laptop (manual Flask deploy, not CI). Nothing below takes effect on the rig until the app (`app35_cam_sole_V12.py` in this folder = repo V11) **and** the companion modules (`mic_capture_manager.py`, `heartbeat_manager.py`, `remote_inmp441_capture.py`, `codesharpnessmeasure/`) are copied there together — the app and managers changed in lockstep (new `ssh_opts` constructor arg). The Pi script re-deploys itself on next use via `_ensure_remote_script`.
> ⚠ **Line numbers** in the review were written against a stale 3,903-line V11 drop; the repo file is newer (3,918 lines before these changes) — re-anchor before quoting them.

---

## A. Data loss — "Phase 1b — Rig hardening"

- [x] **R1** (Critical) — Per-recording remote dirs (`rec_<idx>_<utc>/`); pull retried (2× short-timeout after the 45 s first attempt — bounded because R15's background worker doesn't exist yet); size-verified via remote `stat` vs local; only a verified pull writes the `.pulled` sentinel; deletion replaced by `--cleanup` retention (keep last `MIC_REMOTE_KEEP`=3, never deletes unpulled dirs); WAV timeout no longer skips the onset pull; stale `last_onset`/`last_audio_file` cleared at start
- [x] **R2** (High) — Live write stays in `/tmp` (SD write stalls would recreate R3); `--stop --persist-dir` copies the finished take to `MIC_REMOTE_PERSIST_DIR` (default `/home/<ssh_user>/forgeon_mic_captures`, keep 10) so it survives a Pi reboot — *confirm SD headroom before relying on the default*
- [x] **R3** (Critical) — arecord stderr → `arecord_stderr.log` fd next to the WAV (not DEVNULL — tail lands in `onset_data.json.arecord_stderr`); `-q` dropped so overrun forensics actually get written
- [x] **R4 — V11 half** — Validator `audio.header_length` check: declared WAV data-chunk size vs bytes on disk; failure message says the audio is intact and recoverable — do NOT delete. (Correct test per review: file-bigger-than-header, **never** `nframes == 0` — that never occurs.)
- [x] **R4 — Pi half** — `repair_wav_header()` in the manager post-pull (rewrites RIFF + data lengths when the file outgrows the header, same 4096 B tolerance as the validator); capture read is `select`-bounded (SIGTERM observed ≤ 250 ms, no more mid-write SIGKILL); metadata written BEFORE onset detection (`onset_pending: true`, rewritten after) so the 8 s kill deadline can only cost the onset numbers
- [x] **R5** (High) — Growth probe after start (one SSH, `stat -c %s` twice ~300 ms apart, must grow past 44 bytes; NOT `--status`, NOT `_remote_status`); failure pulls the `mic_capture.log` tail and surfaces at start; Pi `"ok"` = running sample counter > 0; manager `ok` requires WAV > 44 bytes AND verified pull; `--cleanup` kills any live orphan pid BEFORE any file is touched
- [x] **R6** (High) — Incremental 5 ms bucket envelope built during capture (~200 floats/s vs three take-length lists ≈ 3.7 MB/s); no `list.pop(0)`; `sample_count`/`duration` from a running counter, never the WAV header; still 100 % stdlib (AST-guarded by `test_mic_onset_parity.py`); parity vs old detector verified to ±1 bucket on synthetic audio
- [x] **R7** (High) — BLE insole write-ahead log:
  - [x] Per-device WAL on disk, appended per notify, fsync on ~1 s cadence (never per-notify — BLE loop thread)
  - [x] Frozen L/R assignment map persisted to disk at `start_logging`, before any samples
  - [x] `stop_logging` decodes from a non-destructive copy; JSON written tmp-file + fsync + atomic `os.replace`
  - [x] Buffers cleared and WALs deleted only AFTER the JSON is durable (old code cleared before serialising)
  - [x] Startup crash recovery: orphan WALs decoded into their own (old) recording folder, `Recovered_From_WAL: true`
  - [x] `indent=2` dropped from the dump; `wal_errors` counter exposed in the BLE snapshot
- [x] **R8** (High) — `is_recording_evt.set()` moved to after the alive check; spawn-failure rollback kills already-launched ffmpeg processes and closes logs (no more phantom "already recording" or orphan encoders)
- [x] **R9** (Med) — 409 while recording on `/api/ble/start_stream`, `/api/ble/stop_stream`, `/api/ble/remove_device`, guarded at the top of each handler. *(Trade-off accepted: operators lose the mid-take re-arm; segment-file merge variant not built.)*
- [x] **R10 — V11 half** — `stop_combined` surfaces `heartbeat_warning` when a non-skipped take returns `sample_count == 0` (distinct field, deliberately NOT `ok:false` — that would flag every no-strap take as a rig error)
- [x] **R10 — manager half** — `sample_count` now excludes `connected:false` sentinel rows (a fully-disconnected take finally triggers the app's zero-samples warning); slice window extended by `HEARTBEAT_TAIL_GRACE_SECONDS` (1.5 s, downstream `_sync_heartbeat_file` trims); `_iter_events` read/parse errors counted and surfaced (`read_errors`/`parse_errors` in the slice meta + WARNING log) instead of silently yielding a partial slice. *Sidecar `POST /recording/*` handshake deliberately NOT built — the continuous-log-slice architecture is on the review's keep-list and the above closes the operator-visible gap without a second source of truth.*
- [x] **R11 — decision-free half** (Med) — Per-camera 15 s stop deadline (was one shared wall-clock stamp for all three); `+faststart` removed from the LIVE recording command, kept on the offline sync re-encode whose outputs are what is actually served/uploaded
- [ ] **R11 ⚖ — main decision** — Fragmented MP4 (`+frag_keyframe+empty_moov`) so a hard kill costs ~1 s of tail, not the file
  — *blocked on Decision #10 (review recommends fragmented MP4; compat objection doesn't apply — sync re-encodes everything anyway)*

## B. Security — "Phase 0.9"

- [x] **R12** (High) — Shared-token gate over `/api/*`, `/download_file/`, `/media/`, `/video_feed/`, `/status`, `/focus/`: set `RIG_API_TOKEN` on the rig; clients send `Authorization: Bearer` / `X-Rig-Token` / `?token=` (query form so `<img>`/`<video>` tags work). Default unset = today's open behavior.
  - [ ] Still open: swap the Flask dev server for a production WSGI server; Decision #9 (network posture / VLAN) unmade
- [x] **R13** (Med) — `Path.is_relative_to` replaces `startswith(BASE_DIR)` at all **three** sites (`/media`, calibration upload path, `/download_file`); resolved parent passed to `send_from_directory` so no `..` survives into `safe_join`
  - [ ] Still open: one `ls` on the rig to confirm whether a prefix-sharing sibling directory actually exists (scopes the real-world exposure)
- [x] **R14 — V11 sites** (Med) — SSH options centralized in `CAMERA_SSH_OPTS`; `RIG_SSH_STRICT=1` + `RIG_SSH_KNOWN_HOSTS` switches to `StrictHostKeyChecking=yes` + `UserKnownHostsFile` + `BatchMode=yes` + `ConnectTimeout=5`. Default remains `no` so nothing breaks before each Pi's host key is baked at imaging time.
- [x] **R14 — mic sites** — All three `mic_capture_manager.py` SSH/scp sites now take `ssh_opts` (the app passes `CAMERA_SSH_OPTS`, so the existing `RIG_SSH_STRICT` gate covers mic SSH with zero new knobs); standalone use falls back to the same env-gated default (incl. the load-bearing `BatchMode=yes` — stdin is piped, strict without it would hang on the prompt)

## C. Reliability & correctness — Phase 2 foundation

- [ ] **R15** (High) — Move stop post-processing (BLE decode, undistort loop, sync re-encodes, validation) off the request thread into a **subprocess** worker; return from stop immediately
  — *deliberately skipped: changes the stop-response contract the cloud UI reads `files` from; GIL-bound steps mean a thread does not fix it. R1's retries shipped in a bounded synchronous form (≈ 15 s worst-case) instead; the full 3×45 s retry budget still wants this worker.*
- [x] **R16** (Med) — mtime prefilter skips whole log files older than the slice start (kills the per-poll full-history parse; `count_samples` runs twice per 1 Hz status); `_request` gained a per-call `timeout` param (mirror of `_post_json`) — the `devices()` instance-wide mutation race is gone; `/api/heartbeat/devices` returns 409 while recording (R9-style guard — a scan would punch a hole in the take's HR data)
- [x] **R17 — V11 half** (Med) — Focus check now refuses stale frames: gated on the same `_preview_is_healthy()` age predicate `/status` uses → `STALE`, amber `#b45309`, HTTP 503 (distinct from the UNAVAILABLE/NO BOARD grey); `focus_measure_available` + `focus_measure_error` exposed in `/status`
- [x] **R17 — standalone half** — `CAP_DSHOW` now Windows-webcam-only (RTSP opens with the default backend on Linux); focus score normalized by ChArUco checker-square scale + contrast² (the V11 focus route passes `square_px` too); readers start lazily (import no longer costs ~9 s); stale-frame 503 gate (`FOCUS_MAX_FRAME_AGE` 5 s); camera URLs env-overridable. *Deployment still unconfirmed; normalized thresholds (`FOCUS_POOR_THRESH`/`FOCUS_OK_THRESH`, defaults 0.02/0.06) are provisional — one on-rig calibration pass needed before trusting the labels.*
- [x] **R18 — V11-internal** (Med) — `CAMERA_HOSTS` + `CAMERA_STREAM_ROLES` single source of truth: `CAMERA_SOURCES` URLs, bootstrap hosts, `stream_path`s and the v4l2rtspserver `-u` arguments all derive from it. **cam2's missing `-s` flag documented in place but deliberately NOT changed** — confirm intended variant against the actual Pi first.
- [ ] **R18 — cross-app** — Shared rig-topology config file; resolve the `/camera` vs `/video0_<role>` and `192.168.1.x` vs `192.168.2.x` conflict against the Pis; V9 as third source of truth
- [x] **R19 — partial** (Med) — Sync re-encode ffmpeg output captured to `sync/{cam}_sync.log` with the command line (was DEVNULL — a failed re-encode was undiagnosable in principle)
- [x] **R19 — full** — `SessionLogHandler` resolves its path per-emit (survives `start_new_session()` reassigning `SESSION_DIR`; file-append only — safe on the BLE asyncio thread); per-subsystem logs in `SESSION_DIR/logs/` (`app`/`sync`/`ble`/`mic`/`heartbeat`/`bootstrap`); all 19 `print()`s converted (the Pi script's JSON protocol prints untouched); `log_exception()` + `@app.errorhandler(Exception)` (HTTPException passes through) with explicit logging at ~15 high-value except sites — deliberately NOT all 94, per the review; BLE logger defaults to WARNING (`RIG_BLE_LOG_LEVEL`); `/status` exposes `log_dir`; no tokens/secrets logged (MAC/SSH-target redaction still deferred to pre-3.2)

## D. Contract-level gaps ⚖ — meeting items, no code yet

- [ ] **R20 ⚖** — Athlete/session identity handshake on the rig (currently: timestamped folder names, linkage manual and out-of-band)
- [ ] **R21 ⚖** — Cross-modality timestamp convention: UTC + monotonic anchor as a contract requirement; NTP discipline across rig and Pis
- [ ] **R22 ⚖** — Port mic capture onto the heartbeat continuous-log/slice model (replayable, loss-tolerant)
- [ ] **R23** — Rescope roadmap 3.2 (rig direct-to-cloud) M → S–M; athlete-ID handshake as prerequisite; uploader in the R15 worker
- [ ] **R24** — 8-module V11 cut list; extract the session state machine first

## Decisions needed (gate the unchecked ⚖ items)

- [ ] **#9 — Rig auth & network posture** (gates full R12, R14 enablement, 3.2) — eng lead
- [ ] **#10 — Fragmented MP4 vs salvage remux** (gates R11 main) — eng, 15 min; review recommends fragmented MP4

## New operator/deploy knobs introduced (all default-off / behavior matches today unless set)

| Env var | Default | Effect |
|---|---|---|
| `RIG_API_TOKEN` | unset (open) | Requires the token on all API/file/status/feed routes |
| `RIG_SSH_STRICT=1` | off | Strict host-key checking + BatchMode on Pi SSH — now covers the mic manager's three SSH/scp sites too (bake keys into known_hosts first) |
| `RIG_SSH_KNOWN_HOSTS` | `/etc/forgeon/known_hosts` | Path to the pinned known_hosts file |
| `MIC_REMOTE_BASE` | `/tmp/forge_mic_capture` | Pi-side live-capture base (live writes stay in /tmp by design) |
| `MIC_REMOTE_PERSIST_DIR` | `/home/<ssh_user>/forgeon_mic_captures` | Reboot-safe copy destination on the Pi; empty string disables persist + sentinel retention |
| `MIC_REMOTE_KEEP` / `MIC_PERSIST_KEEP` | `3` / `10` | Rolling rec-dirs kept in /tmp / persist dir; only `.pulled` dirs beyond the window are pruned |
| `MIC_PULL_RETRIES` / `MIC_PULL_RETRY_TIMEOUT` | `2` / `6` s | Extra scp attempts per file after the 45 s first try (worst-case stop-latency add ≈ 15 s) |
| `MIC_STOP_SSH_TIMEOUT` | `30` s | `--stop` SSH cap (raised from 20 to cover the persist copy) |
| `MIC_START_PROBE` | `1` | WAV growth probe at start; `0` restores fire-and-forget |
| `HEARTBEAT_TAIL_GRACE_SECONDS` | `1.5` | HR slice window extension past stop (downstream trims; `0` disables) |
| `RIG_LOG_LEVEL` / `RIG_BLE_LOG_LEVEL` | `INFO` / `WARNING` | Console level / BLE-subsystem logger level; files land in `SESSION_DIR/logs/` |
| `FOCUS_CAM1..3_URL` | current hardcoded IPs | Standalone focus-app camera overrides |
| `FOCUS_MAX_FRAME_AGE` | `5` s | Standalone stale-frame 503 gate |
| `FOCUS_POOR_THRESH` / `FOCUS_OK_THRESH` | `0.02` / `0.06` | Normalized focus-score class boundaries — provisional until one on-rig calibration |
