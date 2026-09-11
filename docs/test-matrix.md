# Rig pipeline test matrix (CPU-only goal)

Goal: pick a recording pipeline that does not need a GPU, keeps 90 fps, and
meets the stop-to-upload latency target. Three branches, each one variable:

| Branch | What changes | Env knobs |
|---|---|---|
| `main` | Today's pipeline: live MJPEG to H.264 encode, sync re-encode, OpenCV undistort loop plus a third encode for cam1. Writes no timing file, so it is not used for the runs below. | `FORCE_CUDA_RECORD` / `REQUIRE_CUDA_RECORD` unset (CPU) |
| `test/low-res` | Same pipeline as `main` at the default 1280x720 (the only additions are the resolution knob, calibration rescale and timing output), so it doubles as the baseline. | `RIG_CAPTURE_RES=1280x720` / `960x540` / `854x480` |
| `test/one-encode` | Recorder stream-copies MJPEG (no encode during the take). One ffmpeg pass per camera at stop does trim, CFR, undistort (cam1) and the only H.264 encode. Includes the low-res knob. | `RIG_RECORD_MODE=copy` (default) / `encode`; `RIG_UNDISTORT_BACKEND=ffmpeg` (default) / `opencv`; `RIG_REMAP_OVERSAMPLE=2`; `RIG_KEEP_RAW=1` to keep the MJPEG |

All runs below are CPU only unless marked: launch with `DISABLE_CUDA_RECORD=1`
and `FORCE_CUDA_RECORD` / `REQUIRE_CUDA_RECORD` unset. Unsetting the two
launcher flags is not enough: the app probes CUDA on its own and uses NVENC
whenever the driver answers (it did on 2026-09-11, driver 595.84). The rig's
launcher sets both flags to `1`; use a separate shell for these tests.

## Before the first run (once)

- [ ] On each camera Pi: `v4l2-ctl -d /dev/video0 --list-formats-ext` and read the whole MJPG block (a `grep -A4` shows only the first size). Only sizes listed at 90 fps are valid for `RIG_CAPTURE_RES`, and only same-aspect sizes keep the calibration valid. Result 2026-09-11, all three Pis: 1280x800, 1280x720, 800x600, 640x480, 320x240 at 90 fps. No 16:9 size below 720p exists, so R2, R3, R6 and R7 cannot be run as written; a downstream scale in the sync pass is the alternative if size still matters after R4.
- [ ] Note the rig controller's CPU (`lscpu | grep "Model name"`, `nproc`) and free disk (`df -h` on the sessions volume). Copy mode writes about 0.7 GB per minute per camera at 720p until the take is validated.
- [ ] Use the same athlete, drill, lighting and take length (60 s suggested) for every run so the numbers compare.

## Runs

Do each run twice. Record everything from the "what to collect" list.

| # | Branch | Env | Purpose |
|---|---|---|---|
| R0 | `test/low-res` | `FORCE_CUDA_RECORD=1 REQUIRE_CUDA_RECORD=1`, 720p | Reference only: what production ships today (NVENC when the driver answers). Not CPU only. |
| R1 | `test/low-res` | CPU only, defaults (720p) | Baseline, identical pipeline to `main`. Does the live encode even keep up on the rig CPU? |
| R2 | `test/low-res` | `RIG_CAPTURE_RES=960x540` | Resolution alone. |
| R3 | `test/low-res` | `RIG_CAPTURE_RES=854x480` | Resolution alone, lower. |
| R4 | `test/one-encode` | defaults (`copy`, `ffmpeg`, 720p) | One encode alone. |
| R5 | `test/one-encode` | `RIG_RECORD_MODE=encode RIG_UNDISTORT_BACKEND=ffmpeg` | Isolates the fused undistort from the copy recorder (two encodes per camera, no Python loop). |
| R6 | `test/one-encode` | `RIG_CAPTURE_RES=960x540` | One encode plus resolution. The expected candidate. |
| R7 | `test/one-encode` | `RIG_CAPTURE_RES=854x480` | Same, lower. |
| R8 | `test/one-encode` | `RIG_REMAP_OVERSAMPLE=1` | Only if analysis accuracy on R4 is worse than R1: checks whether the nearest-neighbour remap is the cause. |
| R9 | `test/one-encode` | GPU: `DISABLE_CUDA_RECORD` unset, defaults | One encode with NVENC: copy recorder, single NVENC pass per camera, no Python loop. Not CPU only; the fastest option while the driver works. |
| R10 | `test/one-encode` | `DISABLE_CUDA_RECORD=1 RIG_SYNC_PRESET=ultrafast RIG_SYNC_THREADS=3` | CPU levers, quality kept at 720p: cheaper x264 preset, no thread oversubscription, preview paused (default). |
| R11 | `test/one-encode` | R10 + `RIG_OUTPUT_RES=960x540` | CPU levers plus a smaller output. Needs the cloud accuracy check. |
| R12 | `test/one-encode` | R11 + `RIG_REMAP_OVERSAMPLE=1` | Everything: cam1 was the slowest encode (0.34x vs 0.49x) because of the 2x oversampled remap. |

## What to collect per run

Both branches write `recording_N/pipeline_timing.json` at stop. It holds
almost everything below, so the minimum per run is that one file plus the
two manual items (8 and 10). Send the whole `recording_N/` folder minus the
videos if in doubt: `sync/*.log`, `cam*.log`, the three JSON reports and
`pipeline_timing.json` together are a few hundred KB.

From `pipeline_timing.json`:

1. `stages`: `sync_s`, `postprocess_s`, `validate_s` (and `discard_raw_s` on one-encode), plus `pipeline_s` and `stop_to_ready_s`. `stop_to_ready_s` is the operator-visible stop latency: Stop pressed to the stop response.
2. `recorders.cam*`: the live recorder's last ffmpeg progress line. `speed` below 1.0x or `drop` above 0 means the take-time stage did not keep up. Missing `drop`/`dup` keys mean zero (ffmpeg only prints them when non-zero); in copy mode there is no fps filter, so they never appear.
3. `sync_encodes.cam*`: each sync encode's `speed` (real-time factor on the rig CPU) and frame count. Frame counts must match across the three cameras.
4. `undistort_step`: backend and, for the OpenCV path, `loop_seconds` and `encode_seconds`.
5. `sync_files_mb`: the uploaded set.
6. `validation_status` / `validation_usable`, and `config`: capture res, encoders, decoder, CUDA state and the branch knobs. Check `config.cuda_usable` is `false`, `config.disable_cuda` is `true` and `config.sync_encoder` is `libx264` on every run except R0; otherwise the run was not CPU-only.

Still manual:

7. `rig.log` has the same summary on one `[stop]` line per take, useful for a quick scan across runs.
8. CPU load during the take: `top -bn1 | head -15` once mid-take. Note the ffmpeg processes' CPU percentages.
9. Upload time for the take from `logs/upload.log` (the `Enqueued` and `Uploaded` lines carry timestamps).
10. Analysis result on the uploaded take, from the cloud side. This is the accuracy number and the only thing the rig cannot measure.

## Results (rig, 2026-09-11)

Rig: Acer Nitro AN515-55, i5-10300H (4 cores / 8 threads), 23 GB, NVMe, ffmpeg
6.1.1, NVIDIA driver 595.84 (GPU alive again). Wide-angle sport, cam1
undistorted, no insoles attached (so every take validates `unusable` on the
required BLE check; ignore that column). Takes were not all the same length;
the per-second column is the comparable figure because every pipeline so far
has been linear in take length.

| Run | take s | live recorder speed | sync s | postprocess s | stop_to_ready s | s per take-second | sync MB | notes |
|---|---|---|---|---|---|---|---|---|
| R0 (archery, no undistort) | 65.5 | 1.01 | 59.2 | 0.0 | 61.4 | 0.94 | 599 | NVENC sync at ~1.15x |
| R0 (archery, no undistort) | 32.2 | 1.01 | 27.2 | 0.0 | 29.2 | 0.90 | 293 | frame counts 2901/2902/2902 |
| R0 | 39.0 | 1.01 | 35.5 | 53.5 (loop 46.3 + encode 7.1) | 90.8 | 2.33 | 330 | production reference |
| R0 | 76.3 | 1.00 | 70.8 | 105.7 (loop 92.0 + encode 13.6) | 178.5 | 2.34 | 635 | frame counts 6869/6869/6870 |
| R1 | 60 wall | **0.49 to 0.51** | 57.1 | 52.7 | 111.8 | 3.80 per content-second | 57 | only 29.4 s of content captured: live libx264 could not keep up, CPU at 98% |
| R4 | 16.9 | 1.01 (copy) | 51.2 | 0.0 | 53.0 | 3.13 | 45 | sync encodes 0.34x (cam1) / 0.49x / 0.49x; frame counts 1524/1524/1525; x264 opened 12 threads per process |
| R4 | 22.3 | 1.01 (copy) | 65.7 | 0.0 | 68.4 | 3.07 | 59 | mid-stop `top`: preview 1.4 cores, Chrome 0.5, encoders starved |
| R9 (old commit: software decode, preview live) | 23.2 | 1.01 (copy) | 37.6 | 0.0 | 39.5 | 1.70 | 228 | NVENC sync 0.63x (cam1) / 1.0x / 0.96x; capped by the software MJPEG decode |
| R9 | 41.6 | 1.01 (copy) | 37.6 | 0.0 | 39.8 | 0.96 | 220 | mjpeg_cuvid + preview paused; 1.10x (cam1) / 2.12x / 2.07x; frame counts equal |
| R9 | 15.5 | 1.01 (copy) | 11.2 | 0.0 | 12.0 | 0.78 | 124 | 1.50x / 5.10x / 2.73x (script run, take 1) |
| R9 | 30.4 | 1.01 (copy) | 32.9 | 0.0 | 34.8 | 1.14 | | 0.92x / 1.84x / 1.78x |
| R9 | 15.7 | 1.01 (copy) | 15.4 | 0.0 | 17.3 | 1.10 | | 1.04x / 2.00x / 1.91x; 6 s into the stop only cam1's ffmpeg was left, at 2.9 cores |
| R9b (`RIG_REMAP_OVERSAMPLE=1`) | 39.2 | 1.01 (copy) | 33.5 | 0.0 | 35.4 | 0.90 | | 1.18x / 1.99x / 1.94x: dropping the oversample barely moves cam1 |

Standalone decode/encode bench on the rig (R9b's 39 s raw files, three streams in
parallel, `-f null`): GPU decode alone 3.7-4.0x per stream, CPU decode alone
3.3-3.5x, GPU decode + NVENC 2.8-2.9x, CPU decode + NVENC 1.5x, mixed (cam1 CPU)
1.8/2.6/2.6x. So the GPU decoder is not a shared bottleneck and all-GPU is the
right assignment; cam2/cam3 lose ~30% to the trim, fps filter and moov rewrite.
cam1's ~1.1x is the remap path: ffmpeg's remap only takes 4:4:4 (or RGB), so
every frame is converted NV12 -> yuv444p -> remap -> NV12 on the CPU, about one
core-second per second of video, independent of the oversample.

Decisions so far: R1 rules out today's pipeline on the CPU (half the frames).
R4 proves the copy recorder and the fused undistort on hardware, and its upload
is 7x smaller than R0's, but three parallel 720p90 x264 encodes on four cores
are 3.1 s per take-second, slower than the GPU path. R9 (copy recorder, GPU
decode, one NVENC pass, preview paused) is the candidate: 0.8-1.15 s per
take-second, a 15 s take ready in 12-17 s against 35 s in production. R9b
(oversample 1) buys nothing, so oversample 2 stays for quality. The remaining
lever is cam1's remap: a per-plane remap (luma full size, chroma half size,
no 4:4:4 round trip) should bring cam1 near the other cameras (~8 s for a 15 s
take); moving undistortion to the cloud would remove the stage entirely
(~6 s). R10-R12 (CPU levers) are the fallback if the driver goes away again.
Still open: cloud analysis accuracy for one R0, one R9 and one R9b take; the
cam2 Pi that delivered no stream after a stop and never became healthy on a
later launch (its RTSP server reported running).

## Results template

Copy one row per run.

| Run | live drop / speed (`recorders`) | sync speed cam1 / cam2 / cam3 (`sync_encodes`) | `stop_to_ready_s` | `sync_s` / `postprocess_s` | sync MB total (`sync_files_mb`) | upload s | `validation_status` | analysis accuracy | notes |
|---|---|---|---|---|---|---|---|---|---|
| R0 | | | | | | | | | |
| R1 | | | | | | | | | |
| R2 | | | | | | | | | |
| R3 | | | | | | | | | |
| R4 | | | | | | | | | |
| R5 | | | | | | | | | |
| R6 | | | | | | | | | |
| R7 | | | | | | | | | |
| R8 | | | | | | | | | |
| R9 | | | | | | | | | |
| R10 | | | | | | | | | |
| R11 | | | | | | | | | |
| R12 | | | | | | | | | |

## Decision rules

- If R1 shows live drops or speed below 1.0x, the copy recorder is required regardless of anything else.
- If R4 passes validation and accuracy matches R1, one encode is adopted. Resolution then becomes a size question only.
- If R6 or R7 accuracy holds, adopt that resolution. If not, stay at 720p with one encode.
- If R4 accuracy is below R1 and R8 fixes it, oversample was the cause. If R8 does not fix it, switch `RIG_UNDISTORT_BACKEND=opencv` and accept the extra pass for cam1.
- Stop latency that is still too long after R4 or R6 is a scheduling problem, not an encoding one: the fix is moving the sync pass off the stop request into a background process, which is independent of everything above.
- Every take is short (about 15 s in production), so fixed costs count: run the decisive rows at 15 s, three takes each.
- If the GPU stays reliable, R9 is the candidate: it removes the live encode and the Python loop and keeps NVENC for the one pass. CPU-only (R10 to R12) is the fallback if the driver goes away again.

## Bench numbers to compare against

`docs/lowres-benchmark.md` has laptop numbers for the same stages. Expect rig
times to be several times longer, but the ratios between runs should hold.
