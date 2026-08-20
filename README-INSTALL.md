# Rig installation and operation

## System requirements

- Linux rig laptop with Python 3.12
- FFmpeg and FFprobe (audited rig: FFmpeg 6.1.1)
- SSH access to the camera/microphone hosts
- Camera Pis running `v4l2rtspserver`
- ALSA `arecord` on the microphone host
- Bluetooth access for pressure insoles and the heartbeat sidecar

## Python environment

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.lock.txt` records the complete environment found on the rig. Use
it when exact reproduction is required; `requirements.txt` contains the direct
application/test/tool dependencies.

## Environment variables

Copy `.env.example` values into the launcher environment as required. The app
does not automatically load `.env`; the shell, systemd unit, or operator must
export the variables.

The production rig was last evidenced with:

```bash
cd /home/shikhar/forgeon-streamer
REQUIRE_CUDA_RECORD=1 FORCE_CUDA_RECORD=1 .venv/bin/python app35_cam_sole.py
```

Do not update the existing rig launcher until this checkout has passed review
and hardware testing.

## Heartbeat sidecar

`heartbeat_manager.py` communicates with a separate heartbeat service. Its
historical rig location is `/home/shikhar/Downloads/heartbeat/heartbeat`.
Configure `HEARTBEAT_PROJECT_DIR` and `HEARTBEAT_PYTHON` if it is elsewhere.
Exactly one sidecar should own the Polar connection.

## Camera and microphone behavior

Camera LAN addresses and RTSP paths are currently defined in
`app35_cam_sole.py`. The app can bootstrap `v4l2rtspserver` over SSH. The
microphone helper is copied to the selected camera host and executed there with
`arecord`. Passwordless/key-based SSH is expected; never add passwords to source.

## Focus workflow

1. Start the application and open the calibration page.
2. Put the ChArUco cube roughly 2–3 m from the selected camera with one or two
   faces visible.
3. Select `Calibration cube` or `Flat ChArUco board` in the UI.
4. Reset the best score for that camera and target.
5. Adjust focus through the peak, then return until the badge shows `PEAK`.
6. Reset whenever the target or camera moves. Scores are relative to one
   camera/target and should not be compared across cameras or distances.

## Smoke checks

With the app running on port 5000:

```bash
curl http://localhost:5000/status
curl http://localhost:5000/focus/all
curl -X POST http://localhost:5000/focus/reset
curl -X POST http://localhost:5000/api/snapshots
curl http://localhost:5000/api/heartbeat/status
```

Hardware acceptance also requires one real recording from
`https://dev.forgelabs.in`, successful synchronization/validation, and a
confirmed upload/download cycle.
