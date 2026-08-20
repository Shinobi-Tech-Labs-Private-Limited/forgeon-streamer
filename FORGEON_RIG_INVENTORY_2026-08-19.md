# Forgeon camera-rig application inventory

Initial audit performed read-only on 2026-08-19 (Asia/Kolkata). The working
folder was subsequently cleaned and reorganized with operator approval. This
document was updated after that cleanup on 2026-08-19. Secrets, credentials,
tokens, and RTSP authentication are not printed.

## Current state after cleanup (authoritative update)

The detailed audit in sections 1–7 below records the machine as originally
found. Where it conflicts with this section, this current-state section takes
precedence.

### Current application layout

- Working folder:
  `/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app`
- Active entry point: `app35_cam_sole_V13.py`
- Active template: `templates/active/index35_cam_sole.html`
- The V13 template reference was updated to the unambiguous `active/` path.
- Required root support modules retained beside the active entry point:
  `heartbeat_manager.py`, `intrinsic_calibrate_charuco.py`,
  `mic_capture_manager.py`, and `remote_inmp441_capture.py`.
- Older `app35` versions are under `legacy/` and are documented in
  `legacy/README.md`.
- Older `app.py`/`app2.py`-style Flask experiments are under
  `legacy/camera_flask_experiments/`.
- Old streaming experiments are under `legacy/streaming_experiments/`.
- Standalone maintenance/diagnostic programs are under `tools/` and documented
  in `tools/README.md`.
- Templates are split into `templates/active/` and `templates/legacy/`, with
  their purpose documented in `templates/README.md`.
- `requirements.lock.txt` records the current Python environment packages.

### Files deliberately removed from the working folder

- The accidental approximately 107 GB `.git` object database was deleted. The
  folder is **not currently a Git repository**. An empty environment-managed
  `.git` placeholder may be visible, but it is not repository metadata.
- `GDPS_Log_20260220_160843.json` and
  `GDPS_Log_20260222_135900.json` were deleted. They were left/right
  eight-channel pressure-insole logs and were not required by the camera app.
- Local `credentials.json`, `client_secrets.json`, and `rclone.conf` were
  deleted only after byte-for-byte comparison with their secure backup copies.
  No current application source referenced those files. The backup copies
  remain sensitive and must not be committed.
- Generated Python bytecode/cache files were removed during cleanup where
  identified; caches can be regenerated.

### Git inclusion policy

The current `.gitignore` excludes:

- `sessions/`, `heartbeat_sessions/`, and `calibration_cam*/` capture data
- `*.mp4` and `*.jsonl`
- Python environments (`env/`, `venv/`, `.venv/`) and caches
- Flutter/Dart build output and IDE metadata
- `credentials.json`, `client_secrets.json`, `rclone.conf`, and `.env*`
- local backup archives, logs, OS/editor metadata, and Codex/agent state

Source, templates, documentation, `legacy/`, `tools/`, calibration source and
`calibration/config.yaml` remain eligible for the future repository.

### Backup status

Verified-backup folder:

`/media/shikhar/DATA/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/rig-backups/verified-2026-08-19`

Known contents include:

- `forgeon-rig-source-backup-2026-08-19.tar.gz`: valid pre-reorganization
  source snapshot (146 archive entries)
- `calibration_cam1/`: calibration capture set (208 files), including source
  images/videos, detection JSON, and calibration JSON/NPZ outputs
- `requirements.lock.txt`
- `heartbeat_sessions/`
- Matching secure copies of `credentials.json`, `client_secrets.json`, and
  `rclone.conf`
- `sessions.zip`: **not a valid completed backup**. It is only about 372 KB,
  starts with a ZIP header, but has no ZIP central directory/end record and
  cannot be listed or extracted. It must be recreated from the real sessions
  source before relying on it.

At the time of this update, local `calibration_cam1/` and local `sessions.zip`
were not present in the working folder. The calibration copy above is present
on the DATA volume. The selected recording previously identified by the
operator is located at:

`/media/shikhar/DATA/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/sessions/session_2026-08-19_12-42-47`

### Repository decision

This rig application should become a **small, separate repository** under the
Shinobi Tech Lab Pvt Ltd organization, rather than cloning the large monorepo
onto the rig. The repository has not yet been initialized: the intention is to
preserve and clean the current files first, then create the repository.

## Executive findings

- Rig application directory: `/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app`
- Last demonstrably launched version: `app35_cam_sole_V13.py`
- Exact shell launch command:

  ```bash
  REQUIRE_CUDA_RECORD=1 FORCE_CUDA_RECORD=1 env/bin/python app35_cam_sole_V13.py
  ```

- It uses the local virtual environment, ultimately based on `/usr/bin/python3.12`, Python 3.12.3.
- V13 was compiled/executed at `2026-08-17 21:39:51 +05:30`, but its source was modified later at `2026-08-19 17:04:21 +05:30`.
- The post-install changes are material: one-camera synchronization/upload support and cube/board focus-target selection.
- Almost all operational V4–V13 code is untracked, tracked files are modified/deleted, and `.git` consumes approximately 102 GB.
- Only 3.7 GB is free on a 233 GB filesystem (99% used). A several-GB clone/LFS pull is not currently safe.
- Recommended deployment method: a small, checksummed zip/installer after freeing disk and capturing the current rig state into a clean repository.

## 1. Location and files

### Location

```text
/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app
```

It contains the expected `sessions/`, `templates/`, `codesharpnessmeasure/`, `calibration/`, `calibration_cam1/`, and `env/` directories.

### Source/config tree

Useful source/configuration tree, excluding `sessions/`, `__pycache__`, `.git` internals, the virtual environment, generated Flutter build data, recordings, calibration images, `*.mp4`, `*.jsonl`, and `*.npz`:

```text
camera_app/
├── README-RIG-UPDATE-2026-08-17.md
├── app.py
├── app13.py
├── app14.py
├── app2.py
├── app35_cam_sole.py
├── app35_cam_sole_V0.py … app35_cam_sole_V13.py
├── app6.py
├── app7.py
├── app8_API_Sync_V1.py
├── app8_API_V0.py
├── app8_API_V1.py
├── app8_API_V1_demo.py
├── app8_API_V2.py
├── app8_API_V3_LED_SYNC.py
├── app8_JJ.py
├── app8_mjpeg90_cached.py
├── app8_old_OG.py
├── app9.py
├── calib_focus.py
├── calibration/
│   ├── README.md
│   ├── __init__.py
│   ├── boards.py
│   ├── config.py
│   ├── config.yaml
│   ├── cube_geometry.py
│   ├── detection.py
│   ├── intrinsics.py
│   ├── multicam.py
│   ├── pose.py
│   ├── print_assets.py
│   ├── rendering.py
│   ├── requirements.txt
│   ├── simulate.py
│   └── visualize.py
├── calibration_cam1/
│   ├── calibration_cam1.json
│   └── session_*/{calibration_cam1.json,detections_cam1.json}
├── codesharpnessmeasure/
│   ├── app.py
│   ├── focus/{__init__.py,charuco.py,scorer.py,stream_reader.py}
│   ├── templates/index.html
│   └── tests/test_focus.py
├── flask_integration_bundle/
│   ├── CODEX_INTEGRATION_PROMPT.md
│   ├── README.md
│   ├── heartbeat_blueprint.py
│   ├── heartbeat_client.py
│   └── requirements.txt
├── focus.py
├── heartbeat_manager.py
├── hls.py
├── hls_l.py
├── intrinsic_calibrate_charuco.py
├── led.py
├── mic_capture_manager.py
├── multicam_snap.py
├── pressur_sole_flutter/
├── remote_inmp441_capture.py
└── templates/
    ├── index.html
    ├── index2.html
    ├── legacy/index35_cam_sole.html
    ├── legacy/index35_cam_sole_V8.html
    ├── legacy/index35_cam_sole_V9.html
    └── new/index35_cam_sole.html
```

Sensitive files present but not inspected or reproduced:

```text
client_secrets.json
credentials.json
rclone.conf
```

They must not be committed; add them to `.gitignore` and replace them with documented examples/schemas.

### All `app35_cam_sole_V*.py` versions

Times are local (`+05:30`).

| File | Bytes | Last modified | SHA-256 |
|---|---:|---|---|
| V0 | 58,117 | 2026-02-27 15:25:34 | `98ac52c1bbdb5b7a8223f4194331f733587530d4334931212aa3187665c51a65` |
| V1 | 58,456 | 2026-02-28 11:09:53 | `94a931b014411691a634ad60d11f4d12a43d910ee8033bd5b820e454bf865d86` |
| V2 | 59,103 | 2026-04-21 18:46:54 | `e60eb110f247186fe7bf3220bd10c6751b3726c4779085bb56308f1ccda1a9c6` |
| V3 | 78,439 | 2026-08-18 14:27:24 | `03ec3ef68a7ba3cb36a353a2f2d8f86e752723d5f2ff1101ea9b089b1e1c975b` |
| V4 | 82,615 | 2026-08-18 14:27:24 | `d7dc56e86020892dfd1eba4bebb71dc61b4196101dc543038503ac408533dfe7` |
| V5 | 84,893 | 2026-08-18 14:27:24 | `c2e620e239c02cbafadd07e534902c6d986cbe51a803f101e4f26f7a007e2dfb` |
| V6 | 90,020 | 2026-08-18 14:27:24 | `8604e45b2b22b02a03ae9802f66e7373bba3625d8b472cb5761317a8286ddd2c` |
| V7 | 86,204 | 2026-08-18 14:27:24 | `6a82f4145e4434d545c18eae2ff1ebee312070050954e55a0f4614333c0e418d` |
| V8 | 106,639 | 2026-08-18 14:27:24 | `beffdec694ade42a7e6aebd64efbc06c47c7825ea92e7e316de714997b8b2165` |
| V9 | 106,643 | 2026-08-18 14:27:24 | `5abb501409048b4ddb0a720a163ac30652f7b3b36053869c1e6c1406de329a12` |
| V10 | 117,237 | 2026-08-18 14:27:24 | `8f6fc98237f68def27b57f8ce46e10ac21baa2e948da7169cfcdcc5069960ade` |
| V11 | 149,983 | 2026-08-18 14:27:24 | `c2a832e84d6fefae068ae2b629bae0e38c539d55141594c6acb2acefa62b7fdd` |
| V12 | 155,617 | 2026-08-18 14:27:24 | `c0fd6169814d6c9cc2b819feffc6497e6e1c18ddf65a0df333c346192acec090` |
| **V13** | **159,547** | **2026-08-19 17:04:21** | **`7224a77d5cc3084c7cbd5271e6a037b2c95cb8352ccc0e2297b0fed5ca839f4e`** |

Other candidate: `app35_cam_sole.py`, 95,281 bytes, modified 2026-08-18 14:27:24, SHA-256 `d63a6151e6663c40fcc951bb6f50b197cd7e76b21732f5060bfe6610075bc978`.

### Principal support files

| Path | Bytes | Modified | SHA-256 |
|---|---:|---|---|
| `README-RIG-UPDATE-2026-08-17.md` | 4,051 | 2026-08-17 21:37:43 | `186a2c87aac2745a3f7f53cb7c0c95cf83a7aecebf34538d81f062029f935465` |
| `heartbeat_manager.py` | 17,364 | 2026-07-20 12:26:32 | `b3dcfc7970a0188a52d585d0f21062b260dbe0af5881cd30e5d21c744fe4298b` |
| `mic_capture_manager.py` | 16,878 | 2026-07-17 21:12:03 | `1ac98cd21c773de83adf780bea86102b40dd78d2a9fbc90c206b892350fbb3f0` |
| `remote_inmp441_capture.py` | 9,969 | 2026-07-17 21:11:51 | `0f225e6636c8868f473d3e05dc6161e117d5ecdce51e160a9c823d07486bff00` |
| `intrinsic_calibrate_charuco.py` | 21,060 | 2026-06-10 20:57:38 | `2adb57296d7c3ba92931dd8f29bec1fba9b87b44d1854f14a2f2c70a47dba62f` |
| `codesharpnessmeasure/app.py` | 2,675 | 2026-08-17 21:08:08 | `43e3cda271dc916f8057f46e09d0b08fe027b9356e9ae5b38f4c5a32a7d85cb9` |
| `codesharpnessmeasure/focus/charuco.py` | 7,043 | 2026-08-19 12:34:57 | `940bb83033e64162a3fa4d25d69064ce482e88265400cd3007766d4ba64bdbd3` |
| `codesharpnessmeasure/focus/scorer.py` | 5,469 | 2026-08-19 12:34:57 | `e94a3c7d8b4c8f38c42cb7da9e658f4f5b7691c13d42dba4aff096fe09805881` |
| `codesharpnessmeasure/focus/stream_reader.py` | 1,444 | 2026-08-17 21:08:08 | `002373ba5005237c0b9dde17ffd2c6c563b079a74e0592c33c6ca6f4a9f8a823` |
| `codesharpnessmeasure/templates/index.html` | 7,368 | 2026-08-17 21:08:08 | `87be66ab130807af1031e539ad7891bddc013182bfe0a0f69d86d38e3f393433` |
| `codesharpnessmeasure/tests/test_focus.py` | 6,394 | 2026-08-17 21:45:02 | `96da822cc9e4ca0bcbf17c0ddd5b049e3593838eff4c435df3d8054d26303fd9` |
| `calibration/config.yaml` | 4,184 | 2026-08-17 21:08:09 | `6eec6f62f3e2352a00731a6157ab7308e1e7e497d6b9804e9d23b2c0c726a710` |
| `calibration/README.md` | 31,554 | 2026-08-17 21:08:09 | `467855883e0b48d6b9f0680a34756ba713a9ea0fd6b9cdce3ff5023cb1141a2e` |
| `calibration/boards.py` | 3,806 | 2026-08-17 21:08:08 | `35458413cbf5378b4ff2fa052564bcdf05f027569bea8609cde4296b7e9f904e` |
| `calibration/config.py` | 7,515 | 2026-08-17 21:08:08 | `db7ce5e50da31ad3b9a88005655f1efaa29084c164793bafe4038e2b60e02ba0` |
| `calibration/cube_geometry.py` | 9,836 | 2026-08-17 21:08:08 | `618a4669400ade50d512b9f3e99f9d3a441eb7cd1b1ba5f04cfedc3496a859e0` |
| `calibration/detection.py` | 9,380 | 2026-08-17 21:08:08 | `ac16981f51f3a1e3cf56a25685fd0e718d230601eb96af2bc6c9b08d1d7f4bb` |
| `calibration/intrinsics.py` | 11,197 | 2026-08-17 21:08:08 | `e4f6f34dfb6dd66a746104c6de23768060f24908f7e9d3b513daff258b178b45` |
| `calibration/multicam.py` | 12,015 | 2026-08-17 21:08:09 | `027aafa241b3b6f79c002a39d0a5944bbabc1f63c5918c05712f0d404e10c0ae` |
| `calibration/pose.py` | 8,175 | 2026-08-17 21:08:09 | `6ba9794878be0bbb98f96f61d4714246b6365a90e949abdb7be275195b9f8b78` |
| `calibration/rendering.py` | 4,158 | 2026-08-17 21:08:09 | `a51be4deb8befcd2592bd73f57827d1ae87eafb6eebffcf8c8136b00c64ae96b` |
| `calibration/requirements.txt` | 503 | 2026-08-17 21:08:09 | `8085d8db9a294fc595d1ed524359569804038732ddaaa2d07f3188587976a8fe` |
| `templates/legacy/index35_cam_sole_V8.html` | 64,737 | 2026-08-19 12:34:57 | `5ef93910bc9282628d0a410b99fa9de37eca660f65df6cb5ea92386aff30faa1` |
| `templates/legacy/index35_cam_sole_V9.html` | 36,588 | 2026-07-15 14:02:03 | `73cdda5e0450f34c7014a2c65c2356efddc1286e75ce40b6c9daa92aa22ad443` |
| `templates/new/index35_cam_sole.html` | 31,399 | 2026-06-02 12:01:22 | `93bf4730468d89243eba6c06cc08506d44b73bb75c119d83584b1f299e033962` |

The virtual environment contains thousands of installed dependency files. Its reproducible inventory is the `pip freeze` below rather than treating `site-packages` as application source.

## 2. How it is started

Shell history records the following V13 command three times, after V12 testing:

```bash
REQUIRE_CUDA_RECORD=1 FORCE_CUDA_RECORD=1 env/bin/python app35_cam_sole_V13.py
```

The most recent version-specific history sequence ends with V13. Its bytecode confirms execution:

```text
__pycache__/app35_cam_sole_V13.cpython-312.pyc
mtime: 2026-08-17 21:39:51 +05:30
```

No active app process was visible in the audit process namespace. No matching `.desktop`, autostart entry, shell script, or user systemd unit was found for this directory. Current evidence supports a manual terminal launch.

There is separate Forgeon Streamer/AppImage history involving a bundled executable under `/tmp/.mount_ForgeO.../resources/python/`. That is a separate packaged launcher and is not evidence that current V13 is launched by it.

### Python and virtual environment

```text
Launcher: /home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/env/bin/python
Resolved base interpreter: /usr/bin/python3.12
Version: Python 3.12.3
Venv: yes
include-system-site-packages: false
Venv size: approximately 451 MB
```

### `pip freeze`

```text
annotated-types==0.7.0
anyio==4.10.0
bcrypt==5.0.0
bleak==2.1.1
blinker==1.9.0
boto3==1.40.24
botocore==1.40.24
certifi==2025.8.3
cffi==2.0.0
charset-normalizer==3.4.3
click==8.2.1
cryptography==46.0.6
dbus-fast==4.0.0
fastapi==0.116.1
Flask==3.1.2
flask-cors==6.0.2
h11==0.16.0
idna==3.10
iniconfig==2.3.0
invoke==2.2.1
itsdangerous==2.2.0
Jinja2==3.1.6
jmespath==1.0.1
loguru==0.7.3
MarkupSafe==3.0.2
numpy==2.2.6
opencv-contrib-python==4.12.0.88
packaging==26.3
pandas==3.0.1
paramiko==4.0.0
pluggy==1.6.0
pycparser==3.0
pydantic==2.11.7
pydantic_core==2.33.2
Pygments==2.21.0
PyNaCl==1.6.2
pyserial==3.5
pytest==9.1.1
python-dateutil==2.9.0.post0
PyYAML==6.0.3
requests==2.32.5
s3transfer==0.13.1
six==1.17.0
sniffio==1.3.1
starlette==0.47.3
typing-inspection==0.4.1
typing_extensions==4.15.0
urllib3==2.5.0
uvicorn==0.35.0
websockets==16.0
Werkzeug==3.1.3
```

### Environment-variable keys

| File | Keys |
|---|---|
| `app35_cam_sole_V13.py` | `PI_SSH_USER`, `CAMERA_BOOTSTRAP_ENABLED`, `FORCE_CUDA_RECORD`, `REQUIRE_CUDA_RECORD`, `APP_USE_CASE`, `MIC_CAMERA_KEY` |
| `heartbeat_manager.py` | `HEARTBEAT_SERVICE_URL`, `HEARTBEAT_SESSIONS_DIR`, `HEARTBEAT_TIMEOUT_SECONDS`, `HEARTBEAT_AUTOSTART`, `HEARTBEAT_PROJECT_DIR`, `HEARTBEAT_PYTHON`, `HEARTBEAT_PORT`, `HEARTBEAT_DEVICE` |
| `codesharpnessmeasure/app.py` | `FOCUS_MAX_FRAME_AGE`, `FOCUS_CAM1_URL`, `FOCUS_CAM2_URL`, `FOCUS_CAM3_URL` |
| `codesharpnessmeasure/focus/scorer.py` | `FOCUS_POOR_THRESH`, `FOCUS_OK_THRESH`, `FOCUS_PEAK_RATIO`, `FOCUS_NEAR_RATIO` |
| Test only | `FORGEON_BACKEND` |

No `.env` loader was found. The actual launch explicitly supplies only `REQUIRE_CUDA_RECORD` and `FORCE_CUDA_RECORD`.

## 3. Dependencies outside the folder

### ffmpeg / CUDA

```text
Path: /usr/bin/ffmpeg
Version: ffmpeg version 6.1.1-3ubuntu5+esm10
```

Compiled NVENC encoders: `av1_nvenc`, `h264_nvenc`, `hevc_nvenc`.

Compiled CUVID decoders include `av1_cuvid`, `h264_cuvid`, `hevc_cuvid`, `mjpeg_cuvid`, `mpeg1_cuvid`, `mpeg2_cuvid`, `mpeg4_cuvid`, `vc1_cuvid`, `vp8_cuvid`, and `vp9_cuvid`.

`nvidia-smi` could not communicate with the NVIDIA driver during the audit. NVENC/CUVID are compiled into ffmpeg, but current GPU runtime usability was not proven.

### Camera sources

The app uses three RTSP MJPEG sources on a private LAN:

```text
cam1: rtsp://192.168.2.[redacted]:8555/[side stream]
cam2: rtsp://192.168.2.[redacted]:8555/[front stream]
cam3: rtsp://192.168.2.[redacted]:8555/[back stream]
```

Each source is a Raspberry Pi-style node running `v4l2rtspserver` against `/dev/video0`, configured for MJPEG, 1280×720, 90 FPS. The controller uses SSH as the user selected by `PI_SSH_USER` to start/restart those remote stream servers.

### Local imports and services

Direct local imports include `heartbeat_manager.py`, `intrinsic_calibrate_charuco.py`, `mic_capture_manager.py`, `codesharpnessmeasure/focus/*`, and `calibration/*`. No evidence was found that V13 manipulates `sys.path` to load arbitrary code elsewhere.

- Heartbeat sidecar: default `http://127.0.0.1:8000`, controlled by `HeartbeatManager`.
- BLE pressure insoles: controlled directly through `bleak`.
- Remote microphone: managed by `mic_capture_manager.py` and `remote_inmp441_capture.py`.
- Motorized lens: controlled remotely over SSH using configured GPIO pins.

## 4. Local modifications and Git

### Repository state

```text
Top level: /home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app
Branch: main
Upstream: origin/main
Remote: https://github.com/shikhar-777/Recording-Flask-App.git
```

Only one visible commit:

```text
13df9a5 2026-04-08 16:51:06 +0530 Initial commit
```

Only V0–V3 are tracked. V4–V13 and most current operational support files are untracked. Material state includes a modified `app35_cam_sole.py`, deleted `templates/index35_cam_sole.html`, and untracked V4–V13, calibration, focus, heartbeat, microphone and recent-session files.

No second older `forgeon/` repository was found. Other unrelated repositories exist under `~/blackfly_flask_ui`, `~/AuTerm`, and tool installations.

### Abnormal `.git` size

```text
.git size: approximately 102 GB
Loose objects: 76.40 GiB
Pack files: 7.40 GiB
Reported garbage: 17.95 GiB
Garbage/temp objects: 21
```

This looks like interrupted/failed large-object Git activity. Do not run `git gc`, delete temporary packs, or reclone until the current source and untracked rig state are safely backed up.

### Version evolution

| Transition | Principal change |
|---|---|
| V8 → V9 | Recording target changed 90 FPS → 120 FPS |
| V9 → V10 | Returned to 90 FPS; microphone and heartbeat routes added |
| V10 → V11 | BLE/heartbeat synchronization, recording validation and new-session APIs |
| V11 → V12 | Remote motorized-lens movement/reset/status |
| V12 → V13 install bundle | Cube-aware focus scoring, focus reset, snapshots API, localhost CORS |
| Installed V13 → current V13 | Single-camera synchronization/upload and cube-vs-board target selection |

### Was V13 edited after installation?

**Yes.**

Evidence:

- `/home/shikhar/Downloads/rig-update-2026-08-17.zip`
- Archive mtime: `2026-08-17 21:35:09`
- Archive SHA-256: `caeee5a692c4ae7c45148b66b57061d94015974bc00a182764cd22294b873a6d`
- V13 bytecode/execution: `2026-08-17 21:39:51`
- Current V13 source mtime: `2026-08-19 17:04:21`
- The archive app is named V12, but its README says to install it as V13.

Post-install V13 changes:

1. Synchronization accepts one available camera instead of requiring two.
2. A lone raw camera file is copied into synchronized output.
3. BLE and heartbeat synchronization are attempted against the single-camera window.
4. Upload validation requires one synchronized video rather than two.
5. `/focus/<cam>` and `/focus/all` accept `target=cube|board`.
6. `/focus/reset` resets a target-specific tracker.

Related deployed files were also modified after the archive:

| File | Archive SHA-256 | Current SHA-256 |
|---|---|---|
| `codesharpnessmeasure/focus/charuco.py` | `1841cb28b1a7ea287f16640400110a7c6485de83fb15e7ca4547873c0228d9f7` | `940bb83033e64162a3fa4d25d69064ce482e88265400cd3007766d4ba64bdbd3` |
| `codesharpnessmeasure/focus/scorer.py` | `48d1cf530405020c12e01e4a0b9fc7fa8883a3ae81c38a236e421bf2b005745b` | `e94a3c7d8b4c8f38c42cb7da9e658f4f5b7691c13d42dba4aff096fe09805881` |
| `templates/legacy/index35_cam_sole_V8.html` | `5f56f17b873618ba3892b3db50deac457705846380bdc7065d4582eb1a22a02e` | `5ef93910bc9282628d0a410b99fa9de37eca660f65df6cb5ea92386aff30faa1` |

Because these files are untracked, Git cannot identify who made the edits.

## 5. Data and disk

```text
Filesystem: /dev/nvme0n1p2, ext4
Capacity: 233 GB
Used: 218 GB
Available: 3.7 GB
Utilization: 99%

sessions/ size: 5.9 GB
session_* count: 4
Oldest: session_2026-08-19_12-42-47
Newest: session_2026-08-19_17-16-41
```

Representative recording layout:

```text
sessions/session_YYYY-MM-DD_HH-MM-SS/
├── calibration/calibration_cam1.json
├── camera_bootstrap/{cam1.log,cam2.log,cam3.log}
├── heartbeat_sidecar.log
└── recording_N/
    ├── cam1.mp4, cam2.mp4, cam3.mp4
    ├── cam1.log, cam2.log, cam3.log
    ├── audio/
    ├── ble/
    ├── heartbeat/
    ├── distorted/
    ├── sync/
    ├── processing_status.json
    └── validation_report.json
```

Actual files vary with camera/service success. Recent recordings contain missing camera files in some runs, which explains the later single-camera fallback edit.

## 6. Network and Chrome

```text
Executable: /usr/bin/google-chrome
Version: Google Chrome 150.0.7871.128
```

Chrome history contains visits to `https://dev.forgelabs.in/…`, local Flask pages, `https://test.forgelabs.in/…`, and other development frontends. Thus `dev.forgelabs.in` is used, but local pages are also used.

V13 serves Flask locally and its HTML uses relative API routes. Permitted browser origins include `https://dev.forgelabs.in`, `https://test.forgelabs.in`, a Forgeon Cloud Run development origin, `http://localhost:3000`, and `http://127.0.0.1:3000`.

The restricted audit environment prevented reading host interface/routing state, so the controller's current IP and DHCP/static status could not be verified. The camera topology clearly assumes a fixed `192.168.2.0/24` camera LAN.

No reference to `api-dev-new.forgelabs.in` was found in V13, its operational templates, or local managers. Outbound DNS/HTTPS connectivity to that host could not be proven from the restricted environment.

## 7. Repository-shape recommendation

A large monorepo clone/pull is **not acceptable in the machine's current state**:

- Only 3.7 GB is free.
- The filesystem is 99% full.
- The existing `.git` directory is approximately 102 GB.
- It contains 17.95 GiB of reported Git garbage and temporary pack/object files.
- A multi-GB LFS checkout needs room for downloaded objects, the worktree and temporary unpacking.

Recommended approach:

1. Preserve the present rig source, including all untracked V13/support files, using a small checksummed source bundle.
2. Exclude `sessions/`, `env/`, `.git/`, caches, recordings, calibration captures, credentials and generated Flutter output.
3. Create a clean dedicated rig repository containing the selected entry point, templates, support managers, focus/calibration packages, dependency lock, launch definition and sanitized example configuration.
4. Deploy that small repository or a versioned zip/installer.
5. Store recordings outside the source checkout and implement retention/rotation.
6. Only consider a monorepo after recovering substantial disk space and confirming bandwidth.

Who operates the rig and available bandwidth cannot be determined from the filesystem. Shell history shows hands-on terminal operation by local user `shikhar`, so the current operational model appears manual.
