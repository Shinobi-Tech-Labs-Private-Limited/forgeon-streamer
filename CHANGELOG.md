# Changelog

## V14-pre — pairing enrollment (2026-08-20, branch `feat/rig-upload-worker`)

Phase 5: `/pair` page + `POST /api/pairing/claim` exchange an admin-minted
one-time code (forgeon `/admin/rigs`) for this rig's tokens, stored in
`rig_device.json` (git-ignored, 0600, owned by the app). Credential order:
env override -> rig_device.json -> not paired (upload routes 503).
No terminal, no .env, no password on the rig.

## V14-pre — direct-upload worker (2026-08-20, branch `feat/rig-upload-worker`)

Phase 3 of docs/direct-upload-design.md: `upload_worker.py` (persistent queue at
`BASE_DIR/upload_queue.json`, background thread, init→PUT→complete against the
phase-2 API, bounded auto-retry, `.uploaded` marker) + three routes in the app:
`POST /api/upload_instance` (enqueue, idempotent per assessment+instance),
`GET /api/upload_queue`, `POST /api/upload_retry`. Configured by
`FORGEON_API_URL` + `FORGEON_DEVICE_TOKEN`; unset = routes 503, nothing else
changes. New per-subsystem log: `logs/upload.log`.

## V14-pre — hardening port (2026-08-20, branch `feat/rig-v13-hardening`)

Re-applied the R1–R24 review hardening (forgeon `fix/rig-v11-hardening` @ 8588a3b1)
onto the V13 codebase — the hardened V12 hand-off was never deployed to the rig, so
V13 descended from an unhardened lineage. Non-audio findings only; all mic/audio
hardening is deferred as-is by decision (see docs/rig-hardening-checklist.md for the
per-finding status, docs/rig-review.md for the original review). R12's inbound token
gate is present but dormant (RIG_API_TOKEN unset = open). New docs/direct-upload-design.md
covers the rig→cloud direct upload (R12 enablement, R15 worker, R20 identity, R23).
No VERSION bump yet — that happens at rig deploy.

## V13 — recovered from the production rig (2026-08-19)

The authoritative comparison baseline was
`rig-update-2026-08-17.zip` (`app35_cam_sole_V12.py`). Hand-edit patches were
captured before repository construction.

### Application

- Allow synchronization and upload when only one camera is available.
- Copy a lone raw recording into the normal synchronized output layout and
  write a complete synchronization manifest.
- Synchronize available BLE and heartbeat data for the single-camera case.
- Support explicit `cube` and `board` focus targets.
- Track/reset focus best values independently per camera and target.
- Use the unversioned active template path in the cleaned repository layout.

### UI and focus package

- Add a persistent cube/flat-board focus target selector.
- Send the selected target to focus and reset endpoints.
- Restrict detection to an explicitly selected target, retaining `auto` for
  callers that want cube-first board fallback.
- Report `NO BOARD` in board mode and retain distinct tracker keys.
- Make focus tests work in both the Forgeon backend layout and the standalone
  rig layout.

### Audit result

- `calibration/config.yaml` and the rest of `calibration/` are byte-identical
  to the August 17 update archive.
- CORS, snapshots, and the basic focus routes were already present in that V12
  archive; they are not additional post-archive V13 edits.
- No credential, upload-destination, or destructive-data hand edit was found.

### Validation note

- Syntax compilation passed for all 29 active, calibration, focus, and tool
  Python files.
- Focus tests report 5 passed, 2 skipped, and 1 failure: the synthetic board is
  no longer detected at Gaussian blur sigma 1.9. The same failure reproduces in
  the untouched rig source, so it is recorded as pre-existing behavior rather
  than changed during migration.
