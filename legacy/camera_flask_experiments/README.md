# Legacy camera/Flask experiments

These files are earlier camera-streaming and recording experiments. None is
imported by or required to run `../../app35_cam_sole_V13.py`.

| File | Purpose/status |
|---|---|
| `app.py` | Early local USB multi-camera Flask recorder (camera indices 0-4). |
| `app2.py` | Early three-USB-camera recorder using multiprocessing/FFmpeg-style process management. |
| `app6.py` | Early three-camera RTSP rig recorder. |
| `app7.py` | RTSP recorder derived from app6, adding photo capture. |
| `app8_old_OG.py` | Original app8 RTSP/API development baseline. |
| `app8_mjpeg90_cached.py` | Standalone cached 90 FPS MJPEG preview server; does not record. |
| `app8_JJ.py` | App8 API variant. |
| `app8_API_V0.py` | Early frontend-facing recording API. |
| `app8_API_V1.py` | Expanded app8 recording API. |
| `app8_API_V2.py` | Later app8 API experiment. |
| `app8_API_V3_LED_SYNC.py` | App8 API experiment with LED synchronization. |
| `app8_API_Sync_V1.py` | App8 synchronization/API variant. |
| `app8_API_V1_demo.py` | Demonstration variant used during development and CUDA testing. |
| `app9.py` | Empty placeholder file (zero bytes). |

These files are retained for historical comparison only. Their relative paths
have changed, and they should not be used as the production rig launcher.

## Templates

The two matching early templates are stored beside these experiments:

- `templates/index.html` — shared by the early Flask camera applications.
- `templates/index2.html` — used by `app2.py`.

`app.py` also references `playback.html`, but no such file was present in the
source directory during the 2026-08-19 inventory.
