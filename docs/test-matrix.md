# Rig pipeline test matrix (CPU-only goal)

Goal: pick a recording pipeline that does not need a GPU, keeps 90 fps, and
meets the stop-to-upload latency target. Three branches, each one variable:

| Branch | What changes | Env knobs |
|---|---|---|
| `main` | Baseline. Live MJPEG to H.264 encode, sync re-encode, OpenCV undistort loop plus a third encode for cam1. | `FORCE_CUDA_RECORD` / `REQUIRE_CUDA_RECORD` unset (CPU) |
| `test/low-res` | Capture resolution knob. Calibration rescales to the capture size. | `RIG_CAPTURE_RES=1280x720` / `960x540` / `854x480` |
| `test/one-encode` | Recorder stream-copies MJPEG (no encode during the take). One ffmpeg pass per camera at stop does trim, CFR, undistort (cam1) and the only H.264 encode. Includes the low-res knob. | `RIG_RECORD_MODE=copy` (default) / `encode`; `RIG_UNDISTORT_BACKEND=ffmpeg` (default) / `opencv`; `RIG_REMAP_OVERSAMPLE=2`; `RIG_KEEP_RAW=1` to keep the MJPEG |

All runs below are CPU only: leave `FORCE_CUDA_RECORD` and `REQUIRE_CUDA_RECORD`
unset (or `0`). The rig's launcher currently sets both to `1`; use a separate
shell for these tests.

## Before the first run (once)

- [ ] On each camera Pi: `v4l2-ctl -d /dev/video0 --list-formats-ext | grep -A4 MJPG`. Note which sizes are listed at 90 fps. Only those sizes are valid for `RIG_CAPTURE_RES`.
- [ ] Note the rig controller's CPU (`lscpu | grep "Model name"`, `nproc`) and free disk (`df -h` on the sessions volume). Copy mode writes about 0.7 GB per minute per camera at 720p until the take is validated.
- [ ] Use the same athlete, drill, lighting and take length (60 s suggested) for every run so the numbers compare.

## Runs

Do each run twice. Record everything from the "what to collect" list.

| # | Branch | Env | Purpose |
|---|---|---|---|
| R1 | `main` | CPU only, defaults | Baseline. Does the live encode even keep up on the rig CPU? |
| R2 | `test/low-res` | `RIG_CAPTURE_RES=960x540` | Resolution alone. |
| R3 | `test/low-res` | `RIG_CAPTURE_RES=854x480` | Resolution alone, lower. |
| R4 | `test/one-encode` | defaults (`copy`, `ffmpeg`, 720p) | One encode alone. |
| R5 | `test/one-encode` | `RIG_RECORD_MODE=encode RIG_UNDISTORT_BACKEND=ffmpeg` | Isolates the fused undistort from the copy recorder (two encodes per camera, no Python loop). |
| R6 | `test/one-encode` | `RIG_CAPTURE_RES=960x540` | One encode plus resolution. The expected candidate. |
| R7 | `test/one-encode` | `RIG_CAPTURE_RES=854x480` | Same, lower. |
| R8 | `test/one-encode` | `RIG_REMAP_OVERSAMPLE=1` | Only if analysis accuracy on R4 is worse than R1: checks whether the nearest-neighbour remap is the cause. |

## What to collect per run

From the recording folder (`sessions/session_*/recording_N/`):

1. `sync/sync_manifest.json`: `fps`, `duration_s`, `successful_cameras`, `warnings`, and in one-encode runs `record_mode` and `undistort`.
2. `sync/cam*_sync.log`: the last `frame=... speed=...x` line per camera. `speed` is the encode's real-time factor on the rig CPU.
3. `cam*.log` (raw recorder log): the last `frame=... fps=... drop=... speed=` line. Any `drop=` above 0 or `speed` below 1.0x means the live stage did not keep up.
4. `processing_status.json`: the `undistort_side` step (backend, and time if present) or `skip_undistort`.
5. `validation_report.json`: overall `ok` and any failed checks. Frame counts must match across the three cameras.
6. File sizes in `sync/` (the uploaded set), in MB.
7. Wall-clock stop latency: time from pressing Stop to the stop response arriving in the UI. Use a stopwatch or the `rig.log` timestamps around `stop_combined`.
8. CPU load during the take: `top -bn1 | head -15` once mid-take. Note the ffmpeg processes' CPU percentages.
9. Upload time for the take from `logs/upload.log` (enqueue to done).
10. Analysis result on the uploaded take, from the cloud side. This is the accuracy number and the only thing the rig cannot measure.

## Results template

Copy one row per run.

| Run | live drop / speed | sync speed (cam1 / cam2 / cam3) | stop latency s | sync MB (cam1+cam2+cam3) | upload s | validation ok | analysis accuracy | notes |
|---|---|---|---|---|---|---|---|---|
| R1 | | | | | | | | |
| R2 | | | | | | | | |
| R3 | | | | | | | | |
| R4 | | | | | | | | |
| R5 | | | | | | | | |
| R6 | | | | | | | | |
| R7 | | | | | | | | |
| R8 | | | | | | | | |

## Decision rules

- If R1 shows live drops or speed below 1.0x, the copy recorder is required regardless of anything else.
- If R4 passes validation and accuracy matches R1, one encode is adopted. Resolution then becomes a size question only.
- If R6 or R7 accuracy holds, adopt that resolution. If not, stay at 720p with one encode.
- If R4 accuracy is below R1 and R8 fixes it, oversample was the cause. If R8 does not fix it, switch `RIG_UNDISTORT_BACKEND=opencv` and accept the extra pass for cam1.
- Stop latency that is still too long after R4 or R6 is a scheduling problem, not an encoding one: the fix is moving the sync pass off the stop request into a background process, which is independent of everything above.

## Bench numbers to compare against

`docs/lowres-benchmark.md` has laptop numbers for the same stages. Expect rig
times to be several times longer, but the ratios between runs should hold.
