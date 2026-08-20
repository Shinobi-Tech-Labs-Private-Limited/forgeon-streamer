# Legacy `app35_cam_sole` applications

This directory preserves superseded Forgeon camera-rig application entry points.
They remain here for comparison and recovery; they are not the current launch
target.

## Current application

The current application remains one directory above this folder:

```text
../app35_cam_sole.py
```

Last evidenced launch command:

```bash
REQUIRE_CUDA_RECORD=1 FORCE_CUDA_RECORD=1 .venv/bin/python app35_cam_sole.py
```

The production checkout uses an unversioned filename; `VERSION` records release
13. Do not launch a file from this legacy directory unless deliberately testing
an older implementation.

## File inventory

Times use the machine's local timezone (`+05:30`). Route/function counts are a
simple static count and are included only to show relative application growth.

| File | Bytes | Modified | Routes | Functions | SHA-256 |
|---|---:|---|---:|---:|---|
| `app35_cam_sole.py` | 95,281 | 2026-08-18 14:27:24 | 32 | 82 | `d63a6151e6663c40fcc951bb6f50b197cd7e76b21732f5060bfe6610075bc978` |
| `app35_cam_sole_V0.py` | 58,117 | 2026-02-27 15:25:34 | 22 | 50 | `98ac52c1bbdb5b7a8223f4194331f733587530d4334931212aa3187665c51a65` |
| `app35_cam_sole_V1.py` | 58,456 | 2026-02-28 11:09:53 | 22 | 50 | `94a931b014411691a634ad60d11f4d12a43d910ee8033bd5b820e454bf865d86` |
| `app35_cam_sole_V2.py` | 59,103 | 2026-04-21 18:46:54 | 22 | 51 | `e60eb110f247186fe7bf3220bd10c6751b3726c4779085bb56308f1ccda1a9c6` |
| `app35_cam_sole_V3.py` | 78,439 | 2026-08-18 14:27:24 | 23 | 69 | `03ec3ef68a7ba3cb36a353a2f2d8f86e752723d5f2ff1101ea9b089b1e1c975b` |
| `app35_cam_sole_V4.py` | 82,615 | 2026-08-18 14:27:24 | 23 | 71 | `d7dc56e86020892dfd1eba4bebb71dc61b4196101dc543038503ac408533dfe7` |
| `app35_cam_sole_V5.py` | 84,893 | 2026-08-18 14:27:24 | 25 | 75 | `c2e620e239c02cbafadd07e534902c6d986cbe51a803f101e4f26f7a007e2dfb` |
| `app35_cam_sole_V6.py` | 90,020 | 2026-08-18 14:27:24 | 26 | 79 | `8604e45b2b22b02a03ae9802f66e7373bba3625d8b472cb5761317a8286ddd2c` |
| `app35_cam_sole_V7.py` | 86,204 | 2026-08-18 14:27:24 | 25 | 76 | `6a82f4145e4434d545c18eae2ff1ebee312070050954e55a0f4614333c0e418d` |
| `app35_cam_sole_V8.py` | 106,639 | 2026-08-18 14:27:24 | 33 | 104 | `beffdec694ade42a7e6aebd64efbc06c47c7825ea92e7e316de714997b8b2165` |
| `app35_cam_sole_V9.py` | 106,643 | 2026-08-18 14:27:24 | 33 | 104 | `5abb501409048b4ddb0a720a163ac30652f7b3b36053869c1e6c1406de329a12` |
| `app35_cam_sole_V10.py` | 117,237 | 2026-08-18 14:27:24 | 47 | 120 | `8f6fc98237f68def27b57f8ce46e10ac21baa2e948da7169cfcdcc5069960ade` |
| `app35_cam_sole_V11.py` | 149,983 | 2026-08-18 14:27:24 | 50 | 130 | `c2a832e84d6fefae068ae2b629bae0e38c539d55141594c6acb2acefa62b7fdd` |
| `app35_cam_sole_V12.py` | 155,617 | 2026-08-18 14:27:24 | 53 | 136 | `c0fd6169814d6c9cc2b819feffc6497e6e1c18ddf65a0df333c346192acec090` |
| `../app35_cam_sole.py` **(current V13 checkout)** | 159,535 | 2026-08-19 | 55 | 138 | `e7bfc3bd4d7a63d260fe3f97dd1f777f443f4bb23dc7702603e114b3d03103a8` |

## Known evolution

- V0-V2 are the earliest preserved three-camera/sole application line.
- V3-V7 incrementally expanded camera, recording, BLE and related rig behavior.
- V8 changed the recording target to 90 FPS and substantially expanded the API.
- V9 changed the recording target from 90 FPS to 120 FPS.
- V10 returned to 90 FPS and added microphone and heartbeat APIs.
- V11 added BLE/heartbeat synchronization, recording validation, processing
  status, and new-session APIs.
- V12 added remote motorized-lens movement, reset, and status support.
- V13 added cube-aware focus scoring, focus reset, camera snapshots, localhost
  CORS support, single-camera synchronization/upload fallback, and selectable
  `cube`/`board` focus targets.
- The unversioned `app35_cam_sole.py` is a separate older branch containing LED
  serial synchronization and an earlier focus integration. It is not an alias
  for V13.

## Supporting files

These legacy entry points expect supporting modules and templates relative to
the application root. Moving them here makes them archival references rather
than drop-in launch commands. The current V13 dependencies remain at the root:

- `heartbeat_manager.py`
- `mic_capture_manager.py`
- `intrinsic_calibrate_charuco.py`
- `codesharpnessmeasure/`
- `calibration/`
- `templates/active/index35_cam_sole.html`

The pre-reorganization, source-only snapshot is preserved externally under
`rig-backups/verified-2026-08-19/forgeon-rig-source-backup-2026-08-19.tar.gz`.

## Other camera application experiments

Earlier Flask/camera applications that are not imported by V13 are preserved
under `camera_flask_experiments/`. See its README for their purpose and status.

The unrelated HLS/FastAPI streaming experiments are preserved under
`streaming_experiments/`.

## Legacy templates

Templates used only by older `app35` versions are preserved with their original
subdirectory distinction:

- `templates/legacy/index35_cam_sole.html`
- `templates/legacy/index35_cam_sole_V9.html`
- `templates/new/index35_cam_sole.html`

The production V13 template is intentionally not here. It remains at
`../templates/active/index35_cam_sole.html`.
