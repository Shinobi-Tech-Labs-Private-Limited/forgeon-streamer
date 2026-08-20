# Forgeon camera rig

This repository contains the production Flask application used by the Forgeon
camera rig. Version 13 records three RTSP cameras, pressure-insole BLE streams,
remote microphone audio, and Polar heartbeat data; it also provides calibration,
motorized-lens, snapshots, focus scoring, synchronization, validation, and file
download APIs.

The previous Electron tray wrapper is preserved on branch
`archive/electron-tray` and tag `electron-v0.1-wip`. It is not part of the
current production tree.

## Production entry point

```text
app35_cam_sole.py
```

`VERSION` contains the release number. Release tags use `rig-vNN`, for example
`rig-v13`.

The application currently expects three rig-specific LAN camera addresses in
`app35_cam_sole.py`. They contain no credentials, but changing cameras or LAN
addresses requires a reviewed source/config change. Do not place RTSP passwords,
tokens, or real credentials in this repository.

## Layout

```text
app35_cam_sole.py          production Flask application
templates/active/          production browser UI
heartbeat_manager.py       heartbeat sidecar lifecycle/client
mic_capture_manager.py     remote microphone deployment and capture
remote_inmp441_capture.py  remote ALSA capture helper
intrinsic_calibrate_charuco.py
codesharpnessmeasure/      cube/board focus scoring and tests
calibration/               vendored ChArUco cube model and as-built config
tools/                     standalone operator and diagnostic utilities
legacy/                    superseded applications retained for recovery
pressur_sole_flutter/      companion pressure-sole Flutter source
flask_integration_bundle/  historical heartbeat integration reference
```

Runtime recordings are written under `sessions/` and are intentionally ignored.
Heartbeat session logs, calibration captures, environments, caches, credentials,
and backup archives are also ignored.

## Main HTTP routes

The Forgeon recording page uses these route groups:

- Recording: `POST /start_recording`, `POST /stop_recording`,
  `POST /new_session`
- API recording: `POST /api/start_recording`,
  `POST /api/stop_recording`, `POST /api/new_session`
- Files: `GET /api/get_recording_files/<index>`,
  `POST /api/validate_recording/<index>`, `GET /api/list_recordings`,
  `GET /download_file/<path>`
- Preview: `GET /video_feed/<camera>`
- Focus: `GET /focus/<camera>`, `GET /focus/all`,
  `POST /focus/reset`
- Lens: `POST /lens/<camera>/move`, `POST /lens/<camera>/reset`,
  `GET /lens/status`
- Calibration/snapshots: `GET /api/calibration/status`,
  `POST /api/calibration/capture`, `POST /api/calibration/run`,
  `POST /api/calibration/upload_json`, `POST /api/snapshots`
- Camera/microphone: `GET /api/camera/status`, `GET /api/mic/status`,
  `POST /api/mic/assign`, waveform/onset endpoints
- Heartbeat: status, service start, device discovery/connect/disconnect, and
  session start/stop under `/api/heartbeat/*`
- Pressure insoles: scan, connect, assignment, LED, frequency, and streaming
  under `/api/ble/*`

The Forgeon frontend downloads recordings in 8 MB range chunks. Browsers may
use `HEAD` because `Content-Range` is not currently exposed through CORS.

## Development and operation

See [README-INSTALL.md](README-INSTALL.md) for environment creation, launch,
external services, focus workflow, and smoke checks. See
[CHANGELOG.md](CHANGELOG.md) for the V13 hand edits recovered from the rig.

Never commit:

- `sessions/`, heartbeat recordings, calibration captures, video/audio output
- `.env`, OAuth credentials, tokens, RTSP credentials, or `rclone.conf`
- virtual environments, Python caches, Flutter build output, or local agent state
