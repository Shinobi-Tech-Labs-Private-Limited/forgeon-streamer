# Changelog

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
