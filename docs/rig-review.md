# 09 — Rig Source Code Review — Findings & Proposed Changes

*Companion to docs 00–08 and the roadmap (doc 01) · Reviewed 2 August 2026 · Detailed edition, 3 August 2026*

Reviewed from the rig code drop: `app35_cam_sole_V9.py` (15 Jul) → `V11.py` (21 Jul), `heartbeat_manager.py`, `mic_capture_manager.py`, `remote_inmp441_capture.py`, `codesharpnessmeasure/`, and the legacy operator UIs (base / V8 / V9). Docs 00–08 covered the cloud side; this is the software running the capture station itself.

**Part 1** is the scannable finding table for the meeting. **Part 2** gives the mechanism, failure sequence and implementation traps for each item, verified line-by-line against the code.

**How to read:** every finding carries a gap, a proposed change, and the roadmap slot it lands in. Items marked ⚖ need a meeting decision; everything else is mechanical. Severity: Critical = data destruction possible today · High = data loss or wrong state under a common failure · Med = hardening.

Verdict stamps in Part 2: **Confirmed** = as written · **Nuanced** = true, but the detail changes the fix or the severity · **Corrected** = the finding as written is wrong.

## The verdict in one paragraph

V11 is materially better than the slice docs assumed: it already writes a real `sync_manifest.json` (per-camera epochs, offsets, warnings) and a multimodal `validation_report.json`, freezes insole L/R assignments per recording, and has extracted two proper manager modules (mic, heartbeat). The gaps are equally clear: no upload path (the browser pulls files), no athlete identity anywhere on the rig (sessions are timestamped folder names), no authentication on the HTTP routes, and a handful of failure modes that destroy data — concentrated in the mic and BLE-insole paths.

## Framing added by the detailed pass — read before prioritising

`validate_recording()` (`app35_cam_sole_V11.py:1950`) is a genuinely strong safety net. It checks every modality at stop — audio decodability, audio-vs-video duration coverage, audio peak level, BLE channel statics, heart-rate timestamp alignment and plausible BPM range — and rolls the result up to `usable` / `usable_with_warnings` / `unusable`.

That means **most failures below are detected, not silent.** What the validator cannot do is un-destroy data. So the ranking that matters is not "how visible is it" but **"is the take recoverable once it happens"** — which puts R1, R2, R7 and R11 (data destroyed) genuinely ahead of R5, R10 and R17 (wrong state, but caught before the athlete's session is written off).

---

# PART 1 — Finding tables

## A. Data-loss findings → proposed new roadmap cluster "Phase 1b — Rig hardening" (~1 dev-week)

| # | Finding | Gap | Sev | Proposed change | Effort | Roadmap |
|---|---|---|---|---|---|---|
| R1 | Mic: `rm -f` deletes the previous recording's remote WAV before the new start; the pull is a single scp attempt (45 s cap). A failed pull + next start = athlete audio gone (mic_capture_manager.py:170–172, :235–240) | The previous take's audio can be permanently erased before anyone has confirmed it was safely copied off the Pi | Critical | Delete remote files only after a pull verified by size/checksum; retry pull 3× with backoff; keep the last 3 recordings remote as a rolling backup. Needs **per-recording remote paths** (the current path is fixed and truncated on every capture), and the retries must run in the background worker of R15 — 3×45 s cannot sit on the stop request | XS–S | 1b.1 |
| R2 | Mic files staged under /tmp on the Pi — that location is **cleared on reboot**, so a power cycle before the pull loses the capture (mic_capture_manager.py:26–28; the cited :312 is an unused argparse default) | A Pi reboot or power cut before the pull wipes the recording, and the rig holds only one capture slot — so the in-flight take and the last unpulled take go together | High | Keep capturing to /tmp, then copy to a persistent path (e.g. /home/pi/forgeon_captures/) at stop; clean up only after a verified pull (pairs with R1). **Do not** move the live write path onto the SD card — write stalls there cause R3 | XS | 1b.1 |
| R3 | arecord stderr piped but never drained (read only at :230, after the loop ends); **any** sustained stderr output fills the 64 KiB pipe and deadlocks capture mid-recording. `-q` suppresses arecord's own overrun spam but not libasound's | Recording can silently freeze mid-take once the undrained error pipe fills up — and the stall then produces the damaged header of R4 | Critical | Redirect stderr to a log file next to the WAV via an **open file descriptor**; drop `-q` if we want the overrun forensics. Not DEVNULL — `arecord_stderr` is our only ALSA diagnostic. Cheap detector meanwhile: `live_waveform.json` stops updating during a stall | XS | 1b.2 |
| R4 | When the capture is force-killed — the R3 deadlock, an OOM, or a power cut — the WAV header is left declaring **512 frames (10.7 ms)**, the size of the first write, while the file on disk holds the whole recording. **It never says 0 frames.** Our validator tests `frame_count > 0`, so the file **passes** as "decodable". Separately, the manager's 8 s SIGKILL usually destroys `onset_data.json`, not the WAV, because `wave.close()` runs before the onset pass (remote :306; validator app35:2135) | A killed capture looks like a complete 10-millisecond recording. The audio is entirely present on disk, but nothing — including our own quality check — reports a problem, so the take is written off as "athlete barely made a sound" | High | **1.** Repair the header on pull: read the data-chunk size the header declares, compare it with the real file size minus the 44-byte header, and if the file is bigger, rewrite both length fields from the actual size. **2.** Make the capture loop interruptible — replace the blocking `proc.stdout.read()` with a timeout-bounded read so SIGTERM is always observed and the kill never lands mid-write. **3.** Write the metadata *before* running onset detection, so a slow onset pass can no longer cost us `onset_data.json` (pairs with R6). — **Do not** write the check as `nframes == 0`: that never occurs, so the repair would never run. **Do not** delete or reject short WAVs: the audio is intact and recoverable | S | 1b.2 |
| R5 | **Start verifies nothing at all.** The success check reads the exit status of `echo $!`, which is always 0 — so it catches only a broken SSH connection, never a failed capture. The process ID it captures is never looked at again. And the Pi writes `"ok": true` into the result regardless of how many samples it actually recorded (mic_capture_manager.py:179–200; remote :241) | If the mic device is busy or the process dies at launch, the operator is told recording started, then told it succeeded — and the session ends with a 44-byte empty file nobody looked at until after the athlete left | High | **1.** Verify the WAV is growing: after start, `stat` the file twice about 300 ms apart and require it to be growing past 44 bytes. **2.** Make the two `ok` flags mean something — on the Pi replace the hardcoded `"ok": true` with `len(samples) > 0`; in the manager require a WAV larger than the 44-byte header. **3.** Kill before deleting: the pre-start cleanup removes `mic_capture.pid` without killing the process it names, orphaning any earlier capture that is still alive — it keeps the mic locked and can no longer be stopped, which is what *creates* the "device busy" failure this finding describes. **4.** Show the failure in the operator UI, not only in the stop response. — **Do not** probe via `--status`: it only checks that the pid file exists, which is still true when the device is busy | XS | 1b.2 |
| R6 | Capture + onset math held in RAM on the Pi at **~3.7 MB per second of audio** (~1.1 GB for a 5-minute take) → OOM-kill risk. The spike lands during the onset math, after the WAV is already closed (remote :171, :84–99) | Long takes can exhaust the Pi's memory; the usual casualty is the onset data rather than the audio, and the rig then serves the previous take's onset | High | Stream to disk; keep only a **decimated envelope** (~48× less memory, no loss of onset resolution); `deque` instead of `list.pop(0)`, which currently costs 11.5M element moves per second of audio. **Stdlib only** — the script is scp'd with no dependency install, so numpy would fail silently | S | 1b.2 |
| R7 | BLE insole samples held entirely in RAM until stop; crash/OOM/power cut loses 100% of pressure data while videos survive (app35_cam_sole_V11.py:2450, :2816) | One crash mid-session loses every insole pressure sample of the take, with no partial recovery | High | Append raw frames to an on-disk WAL as they arrive; decode at stop from the file; add crash-recovery on next start. **Persist the frozen L/R assignment map alongside the WAL** (it lives only in RAM today, so recovery would otherwise mislabel feet), and keep the WAL until the JSON is fsynced | S | 1b.3 |
| R8 | Partial ffmpeg start failure leaves is_recording_evt set → rig stuck reporting "already recording". A mid-loop failure **also leaves live encoders running**, whose handles are then dropped on the next start (app35:1474, :3065, :1476) | One failed start blocks all new takes, and can leave orphaned camera processes that only a shell can clear — an engineer-at-the-venue problem, not a press-stop problem | High | Call `stop_recording_all(process_outputs=False)` in the rollback — clearing the flag alone leaves the orphans. Better still, only set the flag *after* the alive check. Longer-term, one session state machine owns every lifecycle flag (feeds 4.6) | XS | 1b.4 |
| R9 | /api/ble/stop_stream can truncate a live recording's insole log — and **none of the eleven BLE routes take the session lock**. `start_stream` is worse: re-arming mid-take overwrites the log at its fixed filename, losing the start as well as the middle (app35:3759, :3741, :2811) | A stray call can cut insole data short mid-recording; the neighbouring re-arm control destroys the whole take's pressure data | Med | Refuse with 409 while recording on `stop_stream`, `start_stream` *and* `remove_device`, guarding at the top of each handler. Scope the lock to mutating routes only — `session_state_lock` is held across minutes of post-processing | XS | 1b.4 |
| R10 | Heartbeat reports `ok:true` **unconditionally**, so a take with zero HR samples passes as success and the caller's error check is dead code. (The sidecar flushes per line — there is no unflushed tail; the per-take window gap is already covered by the video-teardown delay) (heartbeat_manager.py:266; caller app35:3111–3115) | A take with no heart-rate data at all is reported to the operator as a successful capture | High | Surface `sample_count==0` as a **distinct status field, not `ok:false`** — the caller treats falsy `ok` as a rig error, which would flag every strapless take. Extend the slice window rather than delaying the slice; exclude `connected:false` sentinel rows from the count; add an "HR expected" signal | XS–S | 1b.5 |
| R11 ⚖ | ffmpeg SIGKILL after 15 s can corrupt the MP4 during the +faststart moov write; no salvage step (app35_cam_sole_V11.py:2199) | A slow encoder shutdown at the wrong moment can corrupt the entire video file, not just its tail | Med | Either lengthen graceful stop + add a salvage remux, or record fragmented MP4 so a kill loses seconds, not the file. **Decision:** fragmented MP4 — and the compat cost is near zero, since every camera is re-encoded afterwards anyway. **Separately, make the 15 s deadline per camera** — it is currently shared across all three, so one slow camera gets the others killed | S | 1b.6 |

## B. Security findings → fold into the Phase 0 batch

| # | Finding | Gap | Sev | Proposed change | Effort | Roadmap |
|---|---|---|---|---|---|---|
| R12 | No authentication on any of the **50** rig routes, served on 0.0.0.0:5000 via the Flask dev server. Anyone on the venue LAN can start/stop recordings, roll the rig onto a new session (orphaning an in-progress capture), download every athlete's files, watch the live camera feeds, and **cause the rig to SSH into the Pis and execute commands** — including via an unauthenticated *GET* on /api/mic/status, which copies a script onto a Pi and runs it. CORS only restrains browsers | Anyone on the venue network has full, anonymous control of the rig and access to every athlete's files — and /status + /api/list_recordings hand them exactly the paths the download route expects | High | Shared-token middleware on all /api/* + file routes now (the same service-account credential planned for 3.2 later); switch to a production WSGI server | S | new 0.9 |
| R13 | Path check uses startswith(BASE_DIR) with **no trailing separator** — bypassable via a sibling that shares the directory-name prefix. **Three sites**, not two. Ordinary `../` traversal *is* correctly blocked (app35_cam_sole_V11.py:3267, :3888, :3343) | Crafted paths can read files outside the recordings directory; the 403-vs-404 difference also lets an attacker map the surrounding filesystem | Med | Replace with Path.is_relative_to / os.path.commonpath, **and** pass the *resolved* parent to send_from_directory. Confirm with one `ls` on the rig whether a prefix-sharing sibling actually exists | XS | 0.9 |
| R14 | StrictHostKeyChecking=no on all five SSH/scp call sites to the camera Pis → MITM-able control + data channel on the LAN | A machine on the LAN can impersonate a camera Pi and intercept or tamper with commands and pulled files — and a swapped or reflashed Pi connects silently instead of raising an alarm | Med | Bake known_hosts at rig provisioning; **replace** the flag with StrictHostKeyChecking=yes + UserKnownHostsFile + BatchMode=yes — simply removing it makes ssh prompt interactively and hang until timeout | XS | 0.9 |

## C. Reliability & correctness → schedule with the Phase 2 foundation

| # | Finding | Gap | Sev | Proposed change | Effort | Roadmap |
|---|---|---|---|---|---|---|
| R15 | Stop-recording runs minutes of BLE decode + re-encode + **a per-frame Python undistort loop** + validation synchronously inside the HTTP request. The cloud UI times out, and the GIL-bound steps stall the capture threads and every preview stream | The rig is unresponsive for minutes after every stop, the calling UI times out before the work finishes, and /status reports "not recording" for the whole window | High | Return from stop immediately; run sync/validation in a **separate process** — a background *thread* does not relieve the GIL-bound BLE decode and undistort loop. Add a status endpoint with an explicit *started* marker, and capture `current_recording_dir` by value | M | with 2.3 |
| R16 | Heartbeat manager: project path defaults to a developer's home directory (already env-overridable); re-reads **every historical log on each 1 Hz status poll**; mutates the shared timeout outside the lock, which two overlapping calls can leave permanently raised | Status polling gets slower with every session ever recorded — and stop latency with it — while a device scan can permanently raise the HTTP timeout until restart | Med | Skip whole log files by mtime (removes the growth term in 3 lines); add the `timeout` parameter to `_request` — the POST path already has this pattern — instead of mutating shared state. Refuse device scans while recording | XS–S | 1b.5 |
| R17 | V11 **records frame age but the focus path ignores it**, so a frozen stream still reports SHARP; thresholds (50/150) are neither scale- nor contrast-invariant and are applied to two different pixel pipelines; UNAVAILABLE renders in the same grey as NO BOARD and is absent from /status. The standalone app uses a **Windows-only capture backend** and cannot open an RTSP stream on Linux at all | The operator can be told a dead or frozen camera is sharp — while /status on the same page says the preview is unhealthy — or focus checking can be off without anyone noticing | Med | Use the frame age V11 already records (one line, not a reader rewrite); normalise for board scale **and** contrast; give UNAVAILABLE a distinct colour and expose the flag in /status. **Confirm whether codesharpnessmeasure/ is deployed at all** before investing in its half | S | 1b.6 |
| R18 | Two conflicting hardcoded camera-IP sets (192.168.1.101–103 in codesharpnessmeasure vs 192.168.2.30/.33/.32 in V11, with different stream paths); each IP also appears **twice within V11** and a third time in V9 — and **cam2's bootstrap command omits the `-s` flag that cam1 and cam3 use** | The two apps disagree about which cameras exist, one camera is genuinely configured differently from the others today, and re-homing the rig means hunting constants through a 3,903-line file with no config file anywhere | Med | One rig-topology config consumed by both apps, carrying role + host + port + stream path + bootstrap args, with an assertion that the stream path and the `-u` argument agree. **Resolve the cam2 discrepancy first**, or the shared file freezes the wrong variant | XS | with 4.6 |
| R19 | No logging framework anywhere on the rig (zero `logging` usage, 13 `print`s): **94 of 95 exception handlers leave no trace**, 18 are bare `except: pass`, clustered in BLE, process signalling and stop. The sync step discards ffmpeg's stderr outright | When a capture fails there is no record of why — and a failed sync re-encode is undiagnosable *in principle*, not merely unlogged | Med | **Extend the existing camera-bootstrap logging pattern** — which already does per-subsystem logs, traceback capture and a state ring buffer — to BLE, mic and the ffmpeg paths, with files inside the session dir. Stop DEVNULL-ing ffmpeg stderr. Settle log redaction before 3.2 ships them | S | with 4.6 |

## D. Contract-level gaps — the important discussion items ⚖

**R20 ⚖ — No athlete/session identity on the rig.** No UI field, no metadata, in any version — linkage from recording to athlete is manual and out-of-band. *Proposal:* the rig fetches the active session/athlete from the backend at session start (or the capture wizard pushes it), and stamps it into every session folder, manifest, and validation report. Explicit prerequisite of 3.2 — uploading anonymous folders to the cloud is not useful. Also the natural place to introduce server-issued recording sequence numbers (3.3).

**R21 ⚖ — No cross-modality timestamp convention.** Heartbeat writes ISO-8601 UTC; the mic onset is published in the `time.monotonic()` domain, which is meaningless off-device (and biased by ALSA startup latency). **Revised by the detailed pass:** video, BLE and heart-rate *are* already on a shared UTC timeline — the mic is the only orphan, and the anchor needed to fix it is already being written. *Proposal:* every stream records UTC wall time + a monotonic anchor pair at start; all derived timestamps emitted in UTC. Add NTP discipline across rig + Pis. Write into the 2.2 contract requirements.

**R22 ⚖ — Mic path should adopt the heartbeat model.** Heartbeat = continuous local log, sliced post-hoc by time window (replayable, loss-tolerant). Mic = single remote copy, start/stop, pull-and-pray (loss-prone; source of R1/R2). *Proposal:* when 2.1 extracts the shared ingestion pattern, port mic capture onto record-continuously-locally / slice-by-window / pull-with-verification rather than averaging the two designs.

**R23 — 3.2 (rig direct-to-cloud) is smaller than estimated.** The manifest and validation artifacts the cloud needs already exist, and everything is durably staged per `recording_n/` directory — so the uploader is a thin module, and offline buffering comes almost free. *Proposed rescope:* effort M → S–M, but add R20 as a hard dependency, and require the uploader to run in a background worker (R15), never in a request handler.

**R24 — 4.6 (rig modularization) now has a concrete cut list.** Proposed split of the 3,903-line V11: ① camera-bootstrap SSH supervisor ② preview/stream server ③ ffmpeg recorder ④ sync/post-process/undistort pipeline ⑤ validation QA ⑥ BLE coordinator ⑦ session state machine ⑧ thin Flask API layer + config module. **Extract the state machine first** — it eliminates the R8/R9 race class and is the anchor everything else hangs off.

## E. What the rig already does well (keep, don't rewrite)

Sync manifest + validation report generation (the future ingestion contract, half-built) · insole assignment freezing per recording · sensor-brackets-video start/stop ordering with rollback · heartbeat's continuous-log-slice architecture · atomic tmp+rename writes in the remote mic script · the manager-module extraction pattern · **added:** `start_new_session()` — correct lock discipline, refuses to roll over while any modality is live, collision-safe `mkdir(exist_ok=False)`. This is the model the rest of the lifecycle should copy.

## F. Proposed edits to the roadmap (doc 01) — walk this list in the meeting

1. **Phase 0:** add **0.9** — rig route auth + path-traversal fix + host-key pinning (R12–R14).
2. **New Phase 1b — Rig hardening** (~1 dev-week, parallel to Phase 1): **1b.1** mic delete-after-verified-pull + persistent staging (R1, R2) · **1b.2** mic stall/salvage/start-verification (R3–R6) · **1b.3** BLE on-disk WAL (R7) · **1b.4** lifecycle race fixes (R8, R9) · **1b.5** heartbeat flush + manager hygiene (R10, R16) · **1b.6** ffmpeg salvage + focus reliability (R11, R17).
3. **2.1:** add "port mic capture onto the continuous-log/slice model" (R22).
4. **2.2:** add the timestamp convention (UTC + monotonic anchor) as a contract requirement (R21), plus NTP discipline across rig and Pis.
5. **3.2:** rescope M → S–M; add the athlete/session-ID handshake (R20) as a prerequisite; uploader runs in the background worker (R15).
6. **4.6:** replace the vague note with the 8-module cut list; state machine first (R24).
7. **Decisions list:** add **#9 Rig auth & network posture** (gates 0.9, 3.2) — eng lead · **#10 Fragmented MP4 vs salvage remux** (gates 1b.6) — eng, 15 min.

---

# PART 2 — Detailed findings

## A. Data-loss findings — mechanism & failure sequence

### Read first — how the mic path actually works (R1–R6 all depend on it)

`arecord` runs with `-t raw` (`remote_inmp441_capture.py:153–154`) and **Python re-wraps the stream** via the `wave` module — 48 kHz mono S32_LE, **192 KB/s = 11.5 MB/min**. The WAV file object is never flushed or fsynced; the header is finalised only by `wave.close()`.

**The ordering detail that splits R4 from R6:** `wav.close()` fires when the `with` block exits at `:183–220` — **before** the `finally:` at `:221` and **before** `detect_onset()` at `:238`. On a clean SIGTERM the header is therefore correct and only `onset_data.json` is at risk.

**And note what consumes what:** `onset_data.json` is *not* read by the sync pipeline at all — `run_sync_on_dir` and `postprocess_recording_for_upload` never touch `audio/`. Onset is surfaced only through `/api/mic/onset`. Today the onset math is **display-only**.

### R1 — Confirmed, and the blast radius is wider than stated

**The previous take's audio is deleted by the *next* start, with nothing having verified the pull.** Line refs corrected: `rm -f` at `:170–172`, pull at `:235–240`, 45 s cap at `:240`.

*Failure sequence.* Take 7 ends → the Pi's wifi drops or the transfer exceeds 45 s → the stop payload carries a `mic_error`, but nothing blocks the operator. **The WAV is still on the Pi and fully recoverable at this point.** They press Start for take 8 → `:170` executes → take 7's audio is gone permanently.

Three things the review missed:

- **There is no retry anywhere in the mic path.** The rig has retry/backoff for RTSP and BLE — mic is the exception.
- **One slow transfer loses both artifacts.** The 45 s cap raises `TimeoutExpired`, which propagates into the blanket `except Exception` at `:267` — so a WAV timeout *skips the `onset_data.json` pull entirely*. Over a Pi's wifi (~0.5–1 MB/s effective for scp), 45 s buys roughly **2–4 minutes of audio**. Long takes are structurally at risk of the cap.
- **A truncated pull passes validation.** An interrupted scp leaves a partial local WAV — which **opens without error and reports the header's full `nframes`**. `validate_recording` reads frame count and duration from the header, so the truncated file passes both decodability and coverage checks and is silently marked usable. Nothing compares size or checksum against the remote.

One thing better than stated: the deletion is not immediate — the file survives on the Pi for the whole gap between takes. A real recovery window exists; it is simply undocumented and unguarded.

*Implementation traps.* **Moving `rm -f` after a verified pull does nothing on its own** — `wave.open(path, "wb")` truncates, so the next capture clobbers the file regardless. A rolling backup requires per-recording *remote* paths, and `remote_output_dir` is a fixed constant referenced in six places, so it must be threaded through four remote verbs. **"Verified by size/checksum" has no data source today** — verification needs a new remote command and an extra SSH round trip. **Retry ×3 sits on the synchronous stop path** — three 45 s attempts plus backoff can add over two minutes to Stop, so schedule this with R15's background worker.

*Adjacent integrity bug worth fixing in the same change:* `last_audio_file` and `last_onset` are updated *only* on a successful pull, so after a failure `/api/mic/waveform` and `/api/mic/onset` serve **the previous take's data as if it were current**, with no staleness marker.

### R2 — Partially correct; the conclusion holds but the stated reason is rebuttable

The cited `remote_inmp441_capture.py:312` is only an **argparse default that is never used** in the rig flow — the manager always passes `--output-dir` explicitly. The authoritative source is `mic_capture_manager.py:26–28`, which also puts **the deployed script itself** under `/tmp`.

**Change the wording before the meeting.** Nothing in the repo configures the Pis' `fstab`, and on stock Raspberry Pi OS **`/tmp` is usually on the SD card, not in RAM.** The conclusion survives either way — Debian-derived systems clear `/tmp` at boot regardless of backing store — but say **"`/tmp` is not durable across reboot"**, not "tmpfs lives in RAM". The latter is easy to rebut on the spot, and losing the argument would sink a finding that is correct.

*Worse than stated:* the system holds **one** capture slot, so a reboot loses the in-flight take *and* the last completed-but-unpulled take simultaneously.

*Implementation trap — the obvious fix can cause R3.* Pointing `remote_dir` at `/home/pi/forgeon_captures/` moves the **live write path** onto the SD card. Write-latency spikes would block `wav.writeframesraw` inside the capture loop, stalling the drain of `proc.stdout` — **inducing exactly the ALSA overruns R3 is about.** Safer: keep capturing to `/tmp`, then `cp` to persistent storage at stop before `--stop` returns. Also, there is no disk-space guard anywhere.

### R3 — Partially correct; the deadlock is real, the specific trigger is partly suppressed

`proc.stderr` is read exactly once, at `:230`, **inside the `finally` block after the capture loop has already ended**. For the whole take nothing drains that pipe. Linux's default pipe capacity is 64 KiB; once `arecord` fills it, it blocks in `write(2)`, stops draining the ALSA ring buffer, and stops producing stdout. Python then blocks forever in `proc.stdout.read()` at `:184`.

**The detail the review missed:** `-q` is passed to `arecord` at `:155`, and upstream `aplay.c` emits the xrun message inside a `if (!quiet_mode)` guard — so arecord's *own* overrun spam is suppressed. `libasound`'s diagnostics and the `plughw:` plugin chain's warnings still reach stderr independently. Phrase it as: **the pipe is structurally undrained and any sustained stderr output deadlocks the capture; `-q` reduces but does not eliminate the sources.**

**R3's real severity is via R4.** On Stop, SIGTERM sets `stop_requested` — but the loop **never reaches the `while` check**, because it is parked in a blocking `read()`. The 8 s deadline expires, SIGKILL lands mid-loop, and `wav.close()` never runs.

*Cheap detector, already available:* the freeze is already observable — `live_waveform.json` is only rewritten inside the loop and carries `sample_count`. A staleness check on that endpoint detects the stall with **no remote change at all**.

*Implementation trap:* `stderr=subprocess.DEVNULL` would delete the **only** ALSA diagnostic that reaches the rig — `"arecord_stderr"` at `:257` is embedded in `onset_data.json`, the file that gets pulled back. Pass an **open file descriptor** instead. And to get the promised "overrun forensics for free" you must also **drop `-q`**, or the log stays empty and the fix looks like it did nothing.

### R4 — CORRECTED. Right hazard, wrong number, wrong trigger, and the proposed fix would never fire

**This is the most important correction in the detailed pass.** Running the exact write pattern through CPython's `wave` module shows `_ensure_header_written` fixes `nframes` from the size of the **first** write:

```
bytes actually on disk: 204844
nframes seen by reader: 512      (= 10.67 ms at 48 kHz)
after a clean close:    51200 frames
```

The proposed fix — *"if `nframes=0` but the data chunk is non-empty, rewrite the header"* — **can never trigger**, because `nframes` is 512, not 0. The correct test is **"does the file contain more bytes than the header says it does?"** — i.e. compare the declared data-chunk size against `os.path.getsize(wav) - 44`, and repair whenever the real file is larger. All the audio is physically present and recoverable by rewriting two length fields (the RIFF size and the data-chunk size).

**And it is worse downstream than claimed.** `validate_recording`'s decodability check is `frame_count > 0` — and 512 > 0. So the file is reported as **"Audio is decodable (0.011s, 48000Hz, 1 channel(s))" — a *passing* check.**

**Second correction: the 8 s SIGKILL does not normally damage the WAV at all.** Because `wav.close()` fires before `detect_onset`, a healthy SIGTERM patches the header correctly. What the 8 s deadline actually destroys is **`onset_data.json`**, since `write_metadata` runs at `:259`, after the onset math.

And that is not an edge case — it is the *normal* case for anything but a short take. `detect_onset` costs roughly **0.15–0.3 s of Pi CPU per second of audio**, so a 60-second take needs around 9–18 s of post-processing, **past the 8 s deadline**. Expected symptom on every long take: the WAV pulls fine, `onset_data.json` is missing, and per R1 the rig keeps serving the *previous* take's onset. *(The Pi figure is extrapolated from a measured x86 baseline — time it on real hardware before quoting externally.)*

The genuine header-damage triggers are cases where SIGKILL lands while the loop is still running: the R3 deadlock, an OOM-kill during capture (R6), or a power cut. A SIGKILL also drops up to ~8 KiB (~43 ms) of tail audio still in the userspace buffer.

*Implementation traps.* **Do not delete or reject short WAVs** — the payload is intact, so a "clean up broken files" reflex would destroy recoverable athlete audio. **"Lengthen the SIGTERM grace period" does not help the deadlock case** (the process never observes SIGTERM at all), and it makes Stop take tens of seconds on the synchronous request path; if you do lengthen it you must *also* raise the manager's `timeout=20` SSH cap at `:227`, or the SSH call times out first and discards the result you waited for. The two real fixes are separable: make the read timeout-bounded, and move `detect_onset` off the stop-critical path.

### R5 — Confirmed, and understated in three ways

**The success check is vacuous.** The SSH exit status is the status of the last command in the script — `echo $!` — which is always 0. It catches only SSH transport failure (255). The captured pid is stored for display and **never inspected again**.

A preflight does exist and refuses to start if `mic_detected` is false — but that flag comes from `arecord -l`, i.e. the card is *enumerated*. That says nothing about whether it can be **opened**. EBUSY, a wedged dmic driver, or a leftover capture process all sail through.

*Full trace of a silent no-record.* ALSA busy → `arecord` exits immediately → `read()` returns `b""` → the loop breaks with zero writes → `wave.close()` writes a valid 44-byte header with `nframes=0` → `detect_onset([])` returns nothing → and the payload has **`"ok": True` hardcoded at `:241`**, regardless of sample count. Both files pull cleanly, `result["errors"]` is empty, `result["ok"]` is True. **The mic subsystem reports full success for a take with no audio.**

**The self-inflicted part — this belongs in the finding.** The cleanup at `:170–172` deletes `mic_capture.pid` **without killing anything**. If a previous capture is still alive — exactly the R3 stall case — removing its pid file **orphans it**: `status()` then reports `active: false` while the orphan still holds `plughw:` open, and `stop()` can never kill it because `read_pid` returns `None`. That is the mechanism that *manufactures* the ALSA-busy condition R5 describes.

*Also missed:* `mic_capture.log` is written at `:178` and **never read, pulled, or displayed anywhere in the tree**. Any Python-level failure dies on the Pi. This matters directly for R6: if that fix introduces a dependency, this is how it fails silently.

*Implementation traps.* **The probe must not be `--status`** — in the EBUSY case the pid file exists, so it returns `active: true`, a false pass. Use `stat -c %s` on the WAV twice ~300 ms apart and require growth beyond 44 bytes. **Do not route the probe through `_remote_status`**, which re-scps the whole script plus two more SSH commands. **Fix the two `ok` literals in the same change:** `"ok": True` at remote `:241` should be `len(samples) > 0`, and `result["ok"]` at manager `:260` should require a WAV larger than 44 bytes.

### R6 — Confirmed; magnitude understated, and the R6→R4 chain is not the dominant failure

Three structures are live simultaneously: `samples` (0.19 MB/s), `abs_samples` (1.57 MB/s) and `envelope` (1.95 MB/s) — all plain Python lists, no numpy. That is **~3.7 MB of RAM per second of audio**:

| Take length | Peak RSS (approx) |
|---|---|
| 1 min | ~220 MB |
| 3 min | ~660 MB |
| 5 min | **~1.1 GB** |

**The review conflates two different OOM windows.** *During the capture loop*, only `samples` is live at 0.19 MB/s — reaching 700 MB takes roughly an hour of audio. A kill here lands before `wav.close()` and does produce R4 header damage. *During `detect_onset`*, the 3.7 MB/s spike is where the memory actually goes — and by then **the WAV is already closed and correct.** What is lost is `onset_data.json`. So the *dominant* R6 failure mode does not corrupt audio at all.

**The cost the review missed:** `queue.pop(0)` at `:95` on a 240-element list is an **O(240) memmove once per sample** — 11.5 million element moves per second of audio. That is the main reason `detect_onset` is slow, which drives the 8 s deadline problem in R4. Swapping in `collections.deque` is a one-line O(1) win.

*Implementation traps.* **numpy is almost certainly not the answer** — the script is deliberately stdlib-only, deployed by a raw `scp` with **no dependency install step anywhere**. An `import numpy` would fail on any Pi lacking it, and because `mic_capture.log` is never read, the operator sees precisely the R5 symptom. **`detect_onset` is genuinely two-pass** (the threshold depends on the global peak), so the cheap correct fix is a **decimated envelope** — it is already a 5 ms moving average, so one value per millisecond is ~48× less memory with no loss of onset resolution. **If you drop `samples`, watch the R4 interaction:** `len(samples)` feeds `sample_count` and `duration_seconds`, and those must come from a running counter, **not** the WAV header, which is exactly what is wrong in R4.

### R7 — Confirmed, but High for durability, not for the OOM reason given

Samples accumulate in an unbounded `bytearray` (`:2259`) as 28-byte records. The callback does **not** consult `logging_active` — buffering runs continuously from the moment BLE streaming is armed. There is no periodic flush, no timer, no size threshold. The only durability point is `:2878`.

**The destroy-before-persist window — worse than "lost on crash".** `get_raw_data_and_clear()` empties the buffer at `:2846` **before** the JSON is serialised at `:2878`. Any exception in between — MemoryError, a corrupt timestamp, disk full — destroys the RAM copy with nothing on disk. And `stop_combined` swallows it and still returns `{"ok": True}`, with the failure buried in an `out["ble_error"]` string. **The operator sees a green "recording stopped".** The write is also non-atomic and never fsynced.

**Correct the OOM framing.** Steady-state accumulation is only 5.6 KB/s per sole — about 40 MB/hour for a pair. The genuine memory event is **at stop**: a ten-minute two-sole take becomes ~240,000 Python dicts (order 300–500 MB), plus a ~100 MB `json.dumps` string, plus its UTF-8 encoding again at write. That transient lands *after* the buffers were cleared.

**The strongest argument for the fix is already inside this app.** Heart rate runs a sidecar that writes continuous JSONL to disk, and stop merely *slices a window* out of the already-durable file. **Heart rate is crash-safe; insoles are not — same rig, same session, same crash.**

*Two incidental finds:* `data_buffer = deque(maxlen=2000)` is appended on every sample and **never read anywhere**. And `remove_device` during a recording pops the device from `self.devices`, so `stop_logging`'s lookup continues past it and that foot's data **vanishes with no error**.

*Implementation traps — the WAL has five sharp edges.* **Do not fsync per notification** — the callback runs on the BLE asyncio loop thread and fsync latency will drop notifications; buffer in the page cache and fsync on a ~1 s cadence. **Pre-roll discard must become a mark, not a clear**, or every log gets polluted with hours of idle pre-roll. **Per-device WAL files** — the 28-byte frame carries no device identity. **Recovery must not guess L/R** — the frozen assignments live only in RAM and die with the crash, so write the assignment map to disk at `start_logging`, before any samples. **Recovery timing** — the orphan WAL belongs to the *previous* `SESSION_DIR` and must be written into the old recording folder, or `_sync_ble_file` will never find it. Finally: keep the WAL until the JSON is durable (write → fsync → *then* unlink), make `:2878` a temp-file + `os.replace`, and drop `indent=2`.

### R8 — Confirmed, and the orphaned-process variant is materially worse than the stuck flag

`is_recording_evt.set()` at `:1474` is the file's **only** set; `stop_recording_all:2187` is its **only** clear. The rollback at `:3060–3070` stops BLE, mic and heartbeat, then re-raises — never touching the event.

*Be precise about which failures stick.* Anything before `:1474` is clean. Only three paths leave the flag set: `open(log_path)` at `:1484`, `Popen` at `:1498`, and the `RuntimeError` at `:1506` when every camera exits immediately — **the common field case**.

*The phantom state.* `/status` reports `recording: true`; retrying Start returns **HTTP 200 `already_recording`** — a success-shaped response that hides the problem; `/select_sport`, `/api/new_session` and validation all 409; preview quality is degraded rig-wide; and `recording_index` has already advanced. Recovery is to press Stop — but Stop then runs the full sync/validate chain **on the empty folder**.

**Two things the review missed.** *Orphaned ffmpeg processes:* if the exception comes on camera 3 of 4, cameras 1–2 already have live children. The rollback does not call `stop_recording_all()`, so they keep running indefinitely — and the next Start reassigns `record_procs = {}` at `:1476`, **dropping the last handle to those PIDs**; they can then only be killed from a shell. *The `if not alive` guard is too weak in the other direction:* with 1 of 4 cameras alive, no exception is raised at all — the rig reports a healthy start while capturing a single camera.

*Implementation traps.* **Clearing the event alone leaves the orphans** — the rollback must call `stop_recording_all(process_outputs=False)`, and `process_outputs=False` is essential. **Do not roll back `recording_index`** — decrementing it lets the next start overwrite the phantom folder. **Better shape:** move `is_recording_evt.set()` to after the `alive` check at `:1509`, eliminating the window entirely rather than fixing one route out of it.

### R9 — Confirmed, but `start_stream` is strictly more destructive and was not mentioned

Every other state-mutating route takes `session_state_lock` — six sites. **All eleven `/api/ble/*` routes take nothing**, and `stop_stream` additionally has no `is_recording_evt` check.

**The worse sibling — `/api/ble/start_stream` at `:3741`.** Re-arming mid-recording clears the buffer, discards pre-roll again, and recomputes **the same deterministic path** `insole_log_recording_{n}.json`. The dump at stop therefore **overwrites** the truncated first segment with only the post-restart segment: the middle of the take is gone *and* the beginning is gone. This route *does* check `is_recording_evt` — but it uses the check to **re-arm logging** rather than to refuse, which is what makes it dangerous.

**Why Med is nonetheless right.** `validate_recording` computes BLE coverage against the synced video duration and **fails below 95%**. A truncated take is flagged unusable rather than shipped as good data — so the impact is "this take must be re-shot", not "corrupt data enters the dataset".

*Implementation traps.* **Fix all three routes or none** — a 409 on `stop_stream` alone leaves `start_stream` and `remove_device` wide open. **Do not blanket-lock the BLE routes** — `session_state_lock` is held across the entire `stop_combined` including minutes of post-processing, so the UI would freeze for that whole window. **Mind the new lock ordering** (`session_state_lock` → `BleCoordinator._lock` → asyncio loop, with 45 s timeouts on `connect_all`). **A bare 409 is a regression for legitimate recovery** — if insoles drop mid-take the operator can currently re-arm; either accept the loss or make `start_logging` append to a *new segment file* and teach `_sync_ble_file` to merge. **Guard at the top of the handler** — `stop_streaming` is a no-op when already stopped, so a check placed after it still leaves `stop_logging` reachable.

### R10 — CORRECTED. Right symptom, wrong mechanism, and the per-take case is already mitigated

**The stated mechanism does not exist.** `SessionLogger.append()` does `write()` followed by `flush()` — **per line**. On a local filesystem a flushed write is immediately visible to the Flask process. **There is no unflushed userspace tail to lose.** The claim would only hold on a client-cached network mount, which is not the default.

**The real mechanism.** The BLE Heart Rate characteristic notifies at ~1 Hz, each notification carrying the RR intervals accumulated *since the previous one*, with the timestamp assigned at **receipt**. So the window systematically excludes the notification carrying the last ≤1 s of RR data — **one row, containing 1–3 RR intervals**. Symmetrically, the head is *over*-inclusive, and nothing corrects for it.

**And for per-recording takes this is already absorbed — deliberately.** `stop_combined` calls `stop_recording_all()` first, blocking up to **15 s** for ffmpeg, then stops BLE, and only then slices HR. The comment at `:3094–3096` says this is intentional: "BLE and heart-rate capture remain active during this short shutdown so their data brackets the video end." **For snippets the deficit is effectively nil.** The residual exposure is the *session-level* slice, cut immediately on the button press.

**What is genuinely broken is the `ok` flag.** `ok: True` is a hardcoded literal at `:266`; `sample_count` never gates it. That makes the caller's error check **dead code** — `app35:3111–3115` tests `not ok`, which can only be true if an exception fired. Empty HR is not entirely invisible (sync adds a warning, validation flags it), but every HR check is `required=False`, so the recording still resolves to `usable_with_warnings`.

*Widening the scope:* `_iter_events` swallows per-file `OSError` and per-line `JSONDecodeError` with a bare `continue`. **A truncated or permission-denied log yields a partial slice reported as fully successful.** A sidecar restart mid-session is likewise invisible.

*Implementation traps — including a fix that already exists.* **The sidecar already exposes the API this needs, and nothing calls it** — `POST /recording/start|stop` returns `sample_count` from a per-line-flushed writer, and the manager even has a `sidecar_recording()` method with zero callers. **Prefer extending the window over delaying the slice** — slicing `[start, stop + tail_grace]` and letting `_sync_heartbeat_file` trim costs nothing and is retroactively safe. **If you build a handshake, bound it hard** — `GET /hr` returns the last event *forever*, so a disconnected strap means the poll never advances. **Do not naively set `ok: False` on zero samples** — the caller treats falsy `ok` as an error, so every strapless take would look like a rig failure. And decide whether `connected:false` sentinel rows count toward `sample_count` — today they do, so a fully-disconnected take can report `sample_count: 1` and dodge the check.

### R11 ⚖ — Confirmed, plus a separate deadline bug the finding does not name

*Why a kill corrupts the file.* An MP4 keeps its index — the `moov` atom — separate from the data, and ffmpeg can only write it once recording ends. `-movflags +faststart` (`:901`) then adds a second pass that **rewrites the entire file** to move `moov` to the front. For three 1280×720/90fps streams that is hundreds of megabytes rewritten *after* the operator presses stop.

**The 15 s budget is shared by all three cameras, not granted to each.** `t_end = time.time() + 15.0` at `:2199` is an absolute wall-clock timestamp computed **once**, before the loop. If cam1 takes 13 s, cam2 and cam3 split the remaining two and are then SIGKILLed. And because the SIGINTs all went out together, the three rewrites run *concurrently*, competing for the same disk — so they slow each other down precisely when the budget is tightest.

*The Linux path is otherwise sound.* ffmpeg is spawned as a direct `subprocess.Popen` with an argv list and no `shell=True`, so `SIGINT` reaches ffmpeg itself. (The `os.name == "nt"` branch uses `terminate()`, which on Windows is a hard kill — moot on a Linux-only rig, but a trap if anyone ports it.)

*Why salvage is weaker here than for the WAV.* An MP4 with no `moov` has no field to patch — the index must be *reconstructed* by walking the H.264 bitstream. Tools like `untrunc` need a known-good reference and are best-effort. One mercy: `-bf 0` disables B-frames, so decode order equals display order.

**Recommendation for Decision #10.** Take fragmented MP4 (`+frag_keyframe+empty_moov`). It writes a small `moov` up front and emits self-contained fragments, each complete as it lands. With `-g 90` at `TARGET_FPS_WRITE = 90`, a keyframe interval is one second — so a hard kill costs **about a second of tail, not the file**. There is also no rewrite pass, which shortens Stop and chips away at R15. **And the compatibility objection does not apply:** `run_sync_on_dir` (`:1200–1217`) **unconditionally re-encodes every camera** using `best_sync_encoder_args()`, which itself sets `+faststart` (`:938`). The recorded file is an *intermediate* that only ffmpeg ever reads. Fold in two fixes regardless of which option wins: make the deadline **per camera**, and keep `+faststart` only on the offline sync re-encode.

## B. Security findings — mechanism & exploit path

### R12 — Confirmed, every clause

**Correct the count: exactly 50 `@app.route` decorators**, not "~40". No blueprints, no `add_url_rule`, no SocketIO.

`app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)` at `:3897–3903` — bound on every interface, Werkzeug development server, no WSGI, no reverse proxy, no TLS. `debug=False`, so there is *no* Werkzeug console RCE; the review correctly did not claim one.

**CORS is not access control.** Three origins are allowed app-wide (`:198–206`). That is a browser same-origin relaxation — `curl`, `requests` or Postman on the LAN ignore it entirely.

**Authentication: none, verified exhaustively.** Searches for `login_required`, `before_request`, `Authorization`, `Bearer`, `api_key`, `token`, `secret_key`, `session[`, `remote_addr` and allowlist patterns return zero relevant hits. The only three `403`s in the whole file are the path-prefix checks of R13.

**The routes to name in the meeting:**

1. **The reconnaissance-then-exfiltration chain.** `GET /status` (`:3276`) and `GET /api/list_recordings` (`:3463`) return every recording's `"path"` already expressed as `relative_to(BASE_DIR)` — **they hand the attacker exactly the strings `/download_file/` expects.** Two unauthenticated GETs, then bulk download of every athlete's video, insole log, heart-rate JSONL and mic audio.
2. **`GET /api/mic/status` (`:3516`).** A *GET* that SSHes to a Pi, SCPs a script onto it, `chmod +x`, and executes it. Unauthenticated code execution on the camera Pis, mediated by the rig's SSH key.
3. **`POST /api/stop_recording` (`:3402`).** Kills a live capture mid-run. During a competition the athlete's run is not repeatable.
4. **`GET /video_feed/<cam_key>` (`:3197`).** Live MJPEG of the athlete to any LAN client — a privacy exposure needing no file access at all.
5. **`POST /api/calibration/upload_json` (`:3334`).** Persists an arbitrary body as session calibration, silently corrupting the undistortion applied to every subsequent wide-angle recording.

**Two corrections to the finding's wording.** First, **no route deletes local files** — there is no `shutil.rmtree` and no route-driven `unlink` with an attacker-controlled path. Say "**silently roll the rig onto a new empty session, orphaning the in-progress capture**", not "wipe session state". Second, **there is no camera/SSH control route group** — camera bootstrap SSH runs from startup and `atexit`, not from a route. The accurate claim is "unauthenticated requests cause the rig to SSH into the Pis and execute commands", via the mic routes.

### R13 — Confirmed as a real bypass; one extra site the review missed

Both cited lines are right (`:3267`, `:3888`). **A third instance was missed:** `:3343`, the same pattern inside `POST /api/calibration/upload_json`.

**What is *not* broken.** `resolve()` *is* called, so ordinary `../../etc/passwd` traversal is correctly blocked. The absolute-path variant is also unreachable: Werkzeug's `path` converter regex forbids a leading slash, and a `%2F`-smuggled one triggers a `merge_slashes` redirect. **The single defect is the missing `os.sep`.**

*The concrete exploit.* Say the app resolves to `/home/rig/recording`:

```
GET /download_file/%2e%2e/recording_backup/sessions/…/cam1_sync_side.mp4
```

The WSGI layer percent-decodes `PATH_INFO`, so `%2e%2e` arrives as `..` — the encoding matters because browsers and `curl` normalise a literal `../` out of the path before sending. It resolves to `/home/rig/recording_backup/…`, and that string **does start with** `/home/rig/recording`. Check passes; the file is served. The minimal form needs no directory at all: `%2e%2e/recording.env` reads a sibling *file*.

**It is also a filesystem oracle.** The 403 check runs *before* the 404 existence check, so responses are distinguishable: 403 = outside the prefix, 404 = inside but absent, 200 = served. An attacker can enumerate the siblings of `BASE_DIR` by response code alone.

**Honest scoping — why Med is right.** Whether this yields data on *our* rig depends on whether a prefix-sharing sibling actually exists next to the app directory. **One `ls` on the rig settles it** — worth doing before the meeting. Med is independently justified regardless: even with the check fixed, **every file under `BASE_DIR` is downloadable by anyone on the LAN**, because nothing restricts these routes to `sessions/`.

*Fix, and the part that is easy to miss.* Use `Path.is_relative_to(BASE_DIR)`, `os.path.commonpath`, or at minimum `startswith(str(BASE_DIR) + os.sep)`. Then also pass the **resolved** parent to `send_from_directory` — `:3271` and `:3892` currently hand it a path still containing `..`, and Werkzeug's `safe_join` validates only the filename argument.

### R14 — Confirmed

**Terminology first, because it inverts.** In SSH, "host" means the machine being connected *to*. The host key belongs to **each Pi**; the GPU workstation is the *client*. Each Pi generated its host key on first boot; the rig keeps a `known_hosts` address book. `StrictHostKeyChecking` is a client-side policy — which is why it appears in rig code even though the identity verified is the Pi's.

**What the setting does.** Per OpenSSH, `no` is the weakest of the four values: it auto-adds unknown host keys *and* permits connections to hosts whose key has **changed**. So the tripwire is not merely unarmed on day one — it is permanently disabled. (`accept-new` is the middle setting: trusts first sight, refuses later changes.)

**Five call sites, all load-bearing:** `app35_cam_sole_V11.py:489` (arbitrary `bash -s` on the Pi), `:512` (connectivity probe), `mic_capture_manager.py:60` (mic commands and the R1 `rm -f`), `:90` (**pushes** the capture script), `:73` (**pulls** the athlete's WAV).

Targets are bare IPs with no DNS, user `pi`, no `-i` keyfile and no `sshpass` — passwordless public-key auth with the rig's default identity. **Identity is therefore "whoever answers at this IP", and nothing else is checked.**

**What an attacker does and does not gain.** *Not your key* — public-key auth signs a challenge and the private key never leaves the client, which is why this is Med, not High. **But** by ARP-spoofing one of those IPs — or simply claiming it while a Pi is powered off — an attacker can (a) **fake camera health**, since the rig decides a camera is up by parsing stdout and printing `[bootstrap] started pid=…` with exit 0 is enough; (b) **substitute the audio**, since `_scp_from` pulls from whoever answers; (c) **receive your capture script**, scp'd to them on every start.

**The non-adversarial case is likelier.** Reflash an SD card, swap a Pi, or let DHCP hand `192.168.2.30` to a different device, and the host key changes — with this flag the rig connects anyway and reports success.

*Implementation trap — do not simply delete the flag.* With strict checking restored and no `known_hosts` entry, `ssh` **prompts interactively**. These calls run under `subprocess.run` with stdin piped, so instead of failing cleanly they stall until the 20 s timeout. Replace, don't remove:

```
"-o", "StrictHostKeyChecking=yes",
"-o", "UserKnownHostsFile=/etc/forgeon/known_hosts",
"-o", "BatchMode=yes",      # fail fast, never prompt
"-o", "ConnectTimeout=5",
```

`BatchMode=yes` is the load-bearing addition. Note also that `ssh-keyscan` is itself trust-on-first-use — copy each Pi's `/etc/ssh/ssh_host_ed25519_key.pub` at imaging time over a link you trust.

*Fix in one place.* The flag appears at five sites (plus V9). This belongs in the shared SSH options of R18's rig-topology config and R24's ① camera-bootstrap SSH supervisor.

*Incidental defect found alongside.* `_build_camera_ssh_command()` (`:468`) builds an SSH command **without** the flag and is never executed — `:570` calls it only to write `"CMD: "` into the bootstrap log, while the real connection goes through `:585`. The logs record a command that differs from the one that ran.

*Scope note for Decision #9.* The RTSP video (`:49–51`) uses the same three IPs and has *no* host-verification mechanism at all. Fixing R14 closes the one channel that can be cryptographically verified; the control that protects all three is putting the rig and Pis on a dedicated switch or VLAN.

## C. Reliability & correctness — mechanism & failure sequence

### R15 — Confirmed on duration, but the stated cause is wrong, and that changes the fix

`stop_combined()` runs, in order and all inside `session_state_lock`: ffmpeg teardown (15 s budget) → BLE stop → **BLE decode + JSON build** → heartbeat slice → **mic SSH stop plus two 45 s scp pulls** → `run_sync_on_dir` (three parallel full re-encodes) → `postprocess_recording_for_upload` → `validate_recording` → payload assembly.

**The step the review never names — and it is the worst one.** `_undistort_video_file` (`:1830`) does **two full passes over cam1**: first a **Python-level per-frame loop** running `cv2.remap` and writing an `mp4v` intermediate, then a complete ffmpeg transcode of that intermediate. At `TARGET_FPS_WRITE = 90` and 1280×720, **one minute of recording is 5,400 frames through an interpreted loop.** Total shape: roughly **three sequential full-length video passes plus one parallel one**, plus a GIL-bound JSON build — plausibly **longer than the recording itself** on CPU-only encode. (Minor waste alongside: `_ffmpeg_has_encoder` is not cached, so each call spawns `ffmpeg -encoders` — four extra process spawns per stop.)

**Correction that changes the fix.** "Every other endpoint blocks" is not true at the lock level — only **six** call sites take `session_state_lock`. `/status`, `/focus/*`, `/video_feed/*` and every BLE, mic and heartbeat route **never take it**. The real starvation is **(a)** the GIL-bound BLE decode and the Python `cv2.remap` loop, which stall the three capture threads and every MJPEG generator, and **(b)** CPU/GPU saturation from three concurrent ffmpegs. **The consequence: a background *thread* does not fix (a).** The BLE decode and the remap loop have to move to a *subprocess*, or be rewritten. Scope 2.3 accordingly.

**A misinformation bug the review missed.** `is_recording_evt.clear()` fires at `:2187` — at the *very start* of teardown. So for the entire multi-minute window `/status` reports `"recording": false` while nothing has finished. Worse, a retried stop blocks on the lock, then returns **`{"status": "not_recording"}`**.

*Client experience.* The legacy UI is a plain form POST — the browser sits on a blank page, and a reload **re-POSTs**. The cloud UI issues an XHR; Werkzeug imposes no server-side timeout, so the work always completes server-side, but the browser or proxy gives up first and **the client never learns the result**.

*Implementation traps.* **`current_recording_dir` is a module global** that the post-processing steps read — if stop returns early and a new recording starts, the worker post-processes the **wrong directory**; capture it by value. **Nothing gates "busy post-processing"** — `is_recording_evt` is already cleared, so a start would launch three new encoders on a GPU already running three. **The status endpoint is nearly free but cannot currently distinguish failure** — `_get_processing_status`, `_get_sync_status` and `_get_validation_report` are all `try/except → return {}`, so "not started", "in progress" and "crashed" look identical; add an explicit *started* marker. **No crash recovery today**, and `atexit` does not stop workers.

### R16 — Three claims: one already fixed, two confirmed, one with a one-line fix

**(a) The hardcoded path — already solved.** `/home/shikhar/…` is a **default, not a value**: `HEARTBEAT_PROJECT_DIR` already overrides it. The proposed fix "config var for the path" **is already implemented**; the actual defect is that the default is a developer's machine. Severity is lower than "breaks on any other machine": `start_sidecar` degrades cleanly and the rig keeps recording without HR.

**(b) Re-reading all history on every poll — confirmed.** `count_samples` is called **twice per status payload**, and each call globs *every* `hr_*.jsonl` with no time filtering, then runs `json.loads` + `fromisoformat` on **every line of every file**. The UI polls at **1 Hz**, a new log file is created per sidecar launch, and **nothing prunes the directory**. After 50 cumulative hours that is **~180,000 lines re-parsed once or twice every second** inside a Flask request thread. Symptom: the HR panel's latency climbs steadily across the rig's lifetime. Second-order — this is also the read path for `_write_slice`, so **stop latency grows with history too**.

*Missed detail:* `_source_logs` sorts by `st_mtime` and `stat()`s every file on every poll. Because the *live* file's mtime advances continuously, ordering is mtime-based rather than chronological, and the slice is written in file-iteration order — **not sorted by timestamp**. Downstream is accidentally protected (`_sync_heartbeat_file` sorts by `video_time_s`), but the raw artifact is not guaranteed monotonic.

**(c) The timeout mutation — confirmed, and it corrupts permanently.** `devices()` sets the **instance-wide** `timeout_seconds` to 8.0 inside a try/finally. The concurrent 1 Hz poller reads it at call time and now waits 8 s instead of 0.8 s — and since the BLE scan monopolises the sidecar's event loop for 5 s, those polls really do hang. The status panel freezes for up to ~16 s. **And two overlapping `devices()` calls interleave such that the instance is left at 8 s permanently**, silently, until restart.

**The fix is one line, and it already exists on the other code path.** `_post_json` already takes a per-request `timeout` parameter, used correctly by `connect_device` and `disconnect_device`. Only `_request` lacks it — which is why `devices()` resorts to mutation. Add the parameter and the whole try/finally disappears. **Strictly better than adding a lock.** *Do not* put `timeout_seconds` under `self._lock`: `_request` is called from `session_status` *after* the lock is released, and re-acquiring it would re-couple status polls to `stop_session`'s critical section.

*One more, raised by neither report:* `devices()` triggers a real 5-second BLE scan that **suspends notification delivery** on some backends. Running it during an active take can itself manufacture the HR gap R10 is about. `/api/heartbeat/devices` should be refused while recording.

### R17 — Confirmed for the standalone app, but V11 already has the fix ingredients

**Found while verifying — the standalone focus app is non-functional on this rig.** `focus/stream_reader.py:14` opens with `cv2.VideoCapture(self.source, cv2.CAP_DSHOW)`. **`CAP_DSHOW` is the Windows DirectShow backend** — an inline comment even says "Remove `, cv2.CAP_DSHOW` if we are using pis and not laptop". On the Linux workstation this **will not open an `rtsp://` URL at all**. Confirm whether `codesharpnessmeasure/` is actually deployed before spending effort on R17's standalone half.

**The frozen-frame claim splits in two.** For the standalone reader the review is exactly right: no timestamp, no age, no handling of a false `ret`, no reopen. (Its caller even retries five times for a `None` frame — logic that can never help, since the frame is never set back to `None`.)

**V11 is different, and better.** Its own `capture_frames` has a staleness break and a reconnect loop, records `frame_ts[cam_key]` at `:1326`, and there is already an age predicate `_preview_is_healthy()` at `:340`. **The bug is purely that the focus path ignores all of it** — `latest_frame_copy` never looks at `frame_ts`, and `capture_frames` never sets `frames[cam_key] = None` on disconnect. **That materially lowers the fix cost: one line in the focus path, not a reader rewrite.**

*The failure the operator actually sees.* A Pi's `v4l2rtspserver` wedges — process alive, stream stalled. V11 starts reopening, but the stale frame remains. The operator clicks Check Focus, charuco detects the board that *was* in view, and the badge reads **SHARP, green** — while the same page's `/status` block correctly shows `preview_healthy: false`. **Two widgets contradict each other and the green badge is the one that gets trusted.**

*Why the thresholds are not scale-invariant.* Laplacian variance is per-pixel edge energy: it scales with how many checker edges fall per pixel (distance and zoom), with **contrast squared**, and with the resampling kernel. And because the crop is an axis-aligned `boundingRect` of a convex *hull*, a tilted board drags flat background into the crop and **lowers** the score with no optical change.

*A cross-app discrepancy that ties R17 to R18.* V11 downscales every frame to 1280×720 with `INTER_AREA` before storing it, and scores *that*. The standalone app scores the **native** RTSP frame. Same camera, different score, same 50/150 boundary.

*The "fragile import", precisely.* There is **no `codesharpnessmeasure/__init__.py`**, so it resolves only as a PEP 420 implicit namespace package requiring the parent directory on `sys.path`. That holds when launched as `python app35_cam_sole_V11.py` from its own directory — and breaks under systemd with a different `WorkingDirectory`, under `python -m`, under gunicorn, or in any packaged deployment. The `except Exception` also catches failures at *charuco module-import time*, since that module builds `cv2.aruco` objects at import: with plain `opencv-python` that is an `AttributeError`, swallowed identically.

*"Silently disables" is partly wrong.* On failure the endpoint returns HTTP 503 with `label: "UNAVAILABLE"`, and the legacy UI *does* render it — but the badge colour `#888888` is **the same grey as `NO BOARD`**, and the reason string prints under a field labelled "Corners:". Meanwhile `FOCUS_MEASURE_AVAILABLE` appears **nowhere in `/status`**. Accurate wording: **visible-but-illegible in the legacy UI, genuinely invisible to the cloud UI.**

*Implementation traps.* **A naive age gate flaps** — `frames[cam_key]` is never reset on reconnect, so one frame landing mid-refocus jumps the badge STALE→SHARP. **"Area-normalised score" is the wrong normalisation** — Laplacian variance is already per-pixel, so dividing by `w*h` largely cancels; what varies with distance is the board's spatial frequency *in pixels*, so use `ch_corners` to get the median checker-square side and resample to a fixed squares-per-pixel scale, **and also normalise contrast**. **Pick one canonical pipeline before re-deriving thresholds.** **Making "disabled" visible takes two changes** — expose the flag in `/status` *and* give UNAVAILABLE a distinct colour. **Do not narrow `except Exception` to `ImportError`** — you would start hard-crashing on the `cv2.aruco` cases; keep it broad but capture `traceback.format_exc()`.

### R18 — Confirmed; the duplication is worse and is hiding a live inconsistency

The two conflicting sets differ in **subnet, host octet and stream path**: `192.168.1.101–103` with path `/camera` in the standalone app, versus `192.168.2.30/.33/.32` with `/video0_<role>` in V11. Note V11's ordering is non-monotonic — cam2 is `.33`, cam3 is `.32`.

Within V11 every IP appears **twice**, in two structures that must be hand-synchronised: `CAMERA_SOURCES` (`:49–51`) and `CAMERA_BOOTSTRAP[…]["host"]` (`:65`, `:92`, `:118`). There is a third implicit coupling with **no assertion anywhere**: the RTSP path must match the `-u` argument in the bootstrap command. And the role→camera binding is independently re-encoded in `CAMERA_NAME_MAPPING` (`:966`) and again in `CALIBRATION_CAMERA` (`:215`).

**A live inconsistency the duplication is concealing — fix this before writing the config.** cam1 (`:86–87`) and cam3 (`:139–140`) pass `-s /dev/video0`. **cam2 (`:113`) passes a bare `/dev/video0` with no `-s` flag.** Lifting this into a shared file will silently freeze whichever variant happens to get copied.

**There is no config file of any kind in the tree** — a search for `*.json`, `*.toml`, `*.yaml`, `*.cfg` and `*.env` returns nothing, and **camera IPs have no env override at all**. Validation thresholds are likewise inline inside `validate_recording`: 95% BLE coverage, ±25% sample-rate tolerance, 30–240 BPM, audio peak > 8, and the blank-frame heuristics.

*Implementation traps.* **The two apps do not agree on what a camera *is*** — the standalone keys are role-free; V11 binds role, host, stream path, bootstrap args and calibration target across four separate places, and something must **assert** that the `-u <name>` argument and the RTSP path agree. **V9 is a third source of truth** (same IPs, plus a verbatim copy of the BLE UUIDs). **The standalone app builds its readers at import**, each with a 3-second sleep — a ~9-second import. **One of `/camera` and `/video0_<role>` is simply wrong**; the code cannot tell you which, so confirm against the actual Pis.

### R19 — Confirmed and quantified, but drop the "bootstrap" example

Parsed from the AST rather than grepped, so these are exact:

| Metric | V11 |
|---|---|
| Total `except` handlers | **95** |
| Handlers leaving no trace at all | **94** |
| Literally `except …: pass` | **18** |
| `logging` / `getLogger` / `app.logger` usage | **0** — across all six modules |
| `print()` statements | **13** |

The 18 bare `pass` handlers cluster where the review said: **ten in BLE**, three in the record-process signalling path (SIGINT, SIGKILL, log-file close), two in bootstrap socket cleanup, three miscellaneous. So a camera whose ffmpeg refused SIGINT, got SIGKILLed and therefore produced a truncated MP4 — the R11 scenario — **leaves no record of that fact anywhere.**

**Correct the finding: bootstrap is the counter-example, not an example.** The camera-bootstrap subsystem is the *best*-instrumented part of the app: per-camera log files under `SESSION_DIR/camera_bootstrap/`, full stdout/stderr/exit-code capture, traceback capture, and a structured state machine with a 20-event ring buffer surfaced through `/status`. Its two `except: pass` sites are socket-close cleanup at shutdown. **Drop "bootstrap" from R19's list and reframe the fix as "extend this existing pattern to BLE, mic and the ffmpeg paths."** That makes the estimate *more* credible, not less.

**One finding that logging alone cannot fix.** `run_sync_on_dir` throws away ffmpeg's stderr **by design** — `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL` at `:1217`. Only the return code survives. **A failed sync re-encode is unexplainable in principle**, not merely unlogged. Relatedly, `traceback` is imported at `:14` and used **exactly once**, at `:682`.

*Implementation traps.* **Convert the 13 `print()`s rather than adding logging beside them** — they are currently the *only* trace of sync and BLE failures. **"Logs inside the session dir" fights the module-level `SESSION_DIR`** — it is fixed at import but reassigned at runtime by `start_new_session()`, so a `FileHandler` bound at import points at the first session forever; there is an existing pattern to copy *and* an existing race to avoid. **Two of the BLE handlers run inside the asyncio loop thread** — do not attach a network handler there. **Watch volume** — at 200 Hz with per-packet CRC checks, keep the BLE logger at WARNING and surface the existing counters through `snapshot()`. **Do not rewrite 94 handlers** — the ~67 that already carry a message to the client need only a `log_exception()` helper plus a Flask `@app.errorhandler`. **If logs ship with the upload (3.2), settle redaction first** — they will contain SSH targets, LAN topology, and BLE MAC addresses.

## D. Contract-level gaps — the evidence behind the discussion items

### R20 ⚖ — Confirmed, absolutely

A case-insensitive search for `athlete`, `player_id`, `subject` and `participant` across all four Python modules *and* the three legacy operator UIs returns **zero matches**. This is not "thin" identity — it is absent.

**What identifies a recording today** is two derived strings and nothing else:

```python
SESSION_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")      # :209
SESSION_DIR = BASE_DIR / "sessions" / f"session_{SESSION_TIMESTAMP}"  # :210
current_recording_dir = SESSION_DIR / f"recording_{recording_index}"  # :1471
```

Note the session identifier is **local time with no timezone**, and it is computed at *module import* — so a session is defined by when the Flask process was launched, not by any operator action. Restart the app mid-shoot and the session silently splits in two.

*Consequence for 3.2.* An uploader would be shipping `session_2026-08-03_14-22-07/recording_3/` with no way to say whose it is. This is why R20 is a hard prerequisite rather than a nice-to-have.

*Where to put it.* `start_new_session()` is already the correct chokepoint — it holds `session_state_lock`, refuses to roll over while any modality is live, and allocates a collision-free directory. A handshake dropped in there would be stamped into the folder name, the sync manifest and the validation report from one place.

### R21 ⚖ — Nuanced. The finding is real but narrower, and cheaper, than written

**Three of four modalities are already on a shared UTC timeline:**

- **Video** — `sync_manifest.json` emits `sync_start_epoch` / `sync_start_utc` / `sync_end_utc` and per-camera `start_epoch` + `start_utc` (`:1239–1253`).
- **BLE insole** — `_sync_ble_file()` stamps every retained sample with `Timestamp_UTC` (`:1072`).
- **Heart rate** — `_sync_heartbeat_file()` stamps `timestamp_utc` *and* `video_time_s` (`:1125–1126`).

**The mic gets neither.** In the sync manifest the microphone appears only as two booleans:

```python
"microphone_expected": bool(mic.assigned_camera_key),
"microphone_camera":   mic.assigned_camera_key,        # :1255-1256
```

No onset, no epoch, no offset. The onset instead lives in a separate `audio/onset_data.json`, computed in the monotonic domain:

```python
stream_start = time.monotonic()                        # :168
wall_start   = time.time()                             # :169
onset_timestamp = None if onset_index is None else stream_start + (onset_index / RATE)   # :239
```

**And here is the part that changes the fix.** `wall_start` — a POSIX epoch — **is already written out**, as `stream_start_wall_time`, in all three payloads (`:213`, `:243`, `:266`). The anchor pair R21 asks for is *already being sampled one line apart and already published*. The correct UTC onset is derivable today as `wall_start + onset_index / RATE`. What is missing is that `onset_timestamp` is published in the wrong domain, and **nothing downstream consumes the anchor**.

**The real remaining hazard — not in the original finding.** `start_epoch` for video comes from the **rig's** clock; `stream_start_wall_time` comes from the **Pi's** clock. Comparing them is only as good as the clock discipline between them, and **nothing in the repo enforces NTP** on the rig or the Pis. A few hundred milliseconds of drift is invisible in the data and fatal to onset-based sync. Add chrony/NTP to rig provisioning — or measure the offset during the SSH handshake and record it in the manifest — as part of the 2.2 contract.

*The punchline for the meeting.* Onset detection exists precisely to align audio with video. The one modality that onset detection is *for* is the one not placed on the shared timeline — while the ingredients to do it sit unused in the same JSON file.

### R22 ⚖ — Confirmed; the contrast is sharper than the finding states

**What the heartbeat model actually is.** A sidecar process owns the BLE connection and appends one JSON object per event to `hr_<timestamp>.jsonl`, **flushed per line**. Flask never tells it about takes at all — it records wall-clock bookmarks, and at stop it *slices a window* out of a file that was already durable. The authoritative record is the continuous log; the per-recording artifact is a derived view, and any window can be re-cut later.

**What the mic model is.** One remote slot at a fixed path, start/stop over SSH, a single `scp` with a hard 45 s cap, and deletion driven by the *next* start rather than by a verified pull. No continuous record, no second copy, no window to re-cut — which is precisely why R1 and R2 exist. R7's insole path has the same shape: one in-memory buffer, one write, one chance.

**The argument to make in the meeting.** These are not three subsystems with three risk profiles — they are **one architecture that works and two that do not, inside the same application.** A power cut mid-session leaves heart rate fully intact and loses 100% of the insole data and, depending on timing, the audio too. That asymmetry is the cheapest possible justification for 2.1: the target design is already running in production on the same rig.

*Sequencing note.* Porting the mic onto record-continuously / slice-by-window / pull-with-verification **subsumes R1 and R2** rather than sitting alongside them. If 2.1 is close on the calendar, doing the 1b.1 patches first risks paying twice. If it is not, do 1b.1 now anyway — R1 is the one Critical where a single operator action destroys an athlete's take.

*One caution carried over from R10.* The heartbeat model is the right target but is not flawless as implemented: the slice reports `ok: True` unconditionally, swallows parse errors silently, and re-reads all history on every status poll. **Port the architecture, not the implementation details.**

### R23 — Confirmed; the ingestion contract really is half-built

`validate_recording()` (`:1950`) is not a stub. It runs per-modality checks and rolls them up:

- **video/sync** — manifest validity, duration, fps, post-processing success
- **ble** — sample statistics, per-channel ranges, static-channel detection
- **heart_rate** — file presence, malformed-line count, samples inside the video window, timestamp alignment, plausible BPM range
- **audio** — decodability, duration coverage vs video, sample width and peak level

Each check emits `passed` / `warning` / `failed`; each modality is rolled up; the recording is finally graded `usable` / `usable_with_warnings` / `unusable`. Combined with `sync_manifest.json`, that is a real ingestion manifest — the uploader genuinely is a thin module over an already-durable `recording_n/` directory.

*Two hard conditions on the rescope.* R20 must land first (an anonymous folder is not worth uploading), and the uploader must run in the background worker of R15 — never inside a request handler, or it inherits exactly the timeout behaviour that makes stop unusable today.

### R24 — Confirmed; extract the state machine first, and a working prototype already exists

V11 is **3,903 lines**; the extracted managers are 432 (heartbeat), 400 (mic) and 329 (remote capture), which is the proof that the extraction pattern works on this codebase.

**Why the state machine goes first, concretely.** The lock discipline the rig needs is not missing — it exists and is correct in exactly one place:

```python
with session_state_lock:
    if is_recording_evt.is_set():
        raise RuntimeError("Stop the active recording before starting a new session.")
    if ble.logging_active:      raise RuntimeError(...)
    if heartbeat.current_snippet is not None: raise RuntimeError(...)
    ...
    while new_dir.exists():                       # collision-safe
        new_dir = ... f"session_{timestamp}_{suffix:02d}"
    new_dir.mkdir(parents=True, exist_ok=False)
```

R8 and R9 are not a different *kind* of problem — they are the same lifecycle invariants, enforced nowhere. The rig has **four independent, unsynchronised recording flags** (`is_recording_evt`, `ble.logging_active`, `heartbeat.current_snippet`, and the mic manager's state) that are expected to move together but are coupled only by the straight-line code inside `start_combined` and `stop_combined`. Three defects fall out of that one structural fact: torn start (R8), out-of-band submodality transitions (R9), and unguarded reads (`/status` can render a torn composite even absent a bug).

An explicit enum (`IDLE / STARTING / RECORDING / STOPPING / FAULTED`) owned by one guarded object, with every start/stop path an all-or-nothing transition and every submodality route declaring whether it is legal in the current state, eliminates all three at once — and R9 becomes a 409 for free, for every route, rather than one hand-added check per endpoint.

The extraction is therefore mostly **generalising `start_new_session()`'s discipline to every route that touches a lifecycle flag**, not inventing a new design. That is a cheaper and lower-risk first module than it appears.

---

*Part 2 verified line-by-line against the code drop on 3 August 2026. Items flagged as needing confirmation on real hardware: the Pi timing figure in R4, the `/tmp` backing store in R2, whether a prefix-sharing sibling exists next to `BASE_DIR` in R13, and which of `/camera` or `/video0_<role>` is the live RTSP path in R18.*
