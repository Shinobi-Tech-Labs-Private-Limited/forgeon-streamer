#!/usr/bin/env bash
# One docs/test-matrix.md run, unattended: launch the rig app with the run's
# env, select Wide-angle Sport, install the calibration, record N takes of
# T seconds through the same HTTP routes the UI uses (so stop_to_ready_s is
# the operator's number), grab `top` mid-take and mid-stop, stop the app and
# print one summary line per take.
#
#   tools/rig_matrix_run.sh <run> <takes> <seconds> [VAR=value ...]
#
#   tools/rig_matrix_run.sh R9  3 15                                # GPU one-encode
#   tools/rig_matrix_run.sh R9b 2 15 RIG_REMAP_OVERSAMPLE=1
#   tools/rig_matrix_run.sh R10 3 15 DISABLE_CUDA_RECORD=1 RIG_SYNC_PRESET=ultrafast RIG_SYNC_THREADS=3
#   tools/rig_matrix_run.sh R0  2 15 FORCE_CUDA_RECORD=1 REQUIRE_CUDA_RECORD=1   # on test/low-res
#
# Environment (optional):
#   RIG_DIR        repo checkout (default: the directory above this script)
#   CAL_JSON       calibration_cam1.json to install (default: newest one under
#                  sessions/*/calibration/)
#   CAM_WAIT_S     max seconds to wait, before every take, for all cameras to
#                  report preview_healthy on /api/camera/status (120). The Pi
#                  streams restart after each stop; a fixed gap is not enough.
#   GAP_S          minimum seconds between takes before that wait starts (5)
#   OUT_DIR        where console log, snapshots and the bundle go (/tmp/rig-runs)
#
# Needs: bash, curl, python3 (for the summary), the app's .venv. The rig must
# already be paired (the UI recorded fine, so it is). Results:
#   $OUT_DIR/<run>/            console log + top snapshots
#   $OUT_DIR/<run>.tgz         the session folder minus videos, plus the above
set -u

RUN=${1:?run name, e.g. R9}
TAKES=${2:?number of takes}
TAKE_S=${3:?seconds per take}
shift 3
RUN_ENV=("$@")

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RIG_DIR=${RIG_DIR:-$(dirname "$HERE")}
OUT_DIR=${OUT_DIR:-/tmp/rig-runs}
CAM_WAIT_S=${CAM_WAIT_S:-120}
GAP_S=${GAP_S:-5}
BASE=http://127.0.0.1:5000
RUN_DIR="$OUT_DIR/$RUN"
mkdir -p "$RUN_DIR"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
snap() { top -bn1 | head -20 > "$1"; }

# Camera readiness as the UI sees it: every camera preview_healthy (fresh
# preview frames, which implies the Pi's RTSP server is up again after the
# post-stop restart). Prints "ok" or the per-camera state.
cam_state() {
  curl -sf "$BASE/api/camera/status" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("no-json"); sys.exit(0)
cams = d.get("cameras", {})
bad = ["%s:%s/rtsp=%d/preview=%d" % (k, v.get("state"), bool(v.get("rtsp_server_running")), bool(v.get("preview_healthy")))
       for k, v in cams.items() if not v.get("preview_healthy")]
print("ok" if cams and not bad else " ".join(bad) or "no-cameras")
'
}

wait_cameras() {
  local deadline=$(( $(date +%s) + CAM_WAIT_S )) state last=""
  while :; do
    state=$(cam_state)
    [ "$state" = "ok" ] && { log "cameras ready"; return 0; }
    [ "$state" != "$last" ] && { log "waiting for cameras: $state"; last=$state; }
    [ "$(date +%s)" -ge "$deadline" ] && { log "cameras not ready after ${CAM_WAIT_S}s: $state"; return 1; }
    sleep 2
  done
}

cd "$RIG_DIR" || die "no such dir: $RIG_DIR"
[ -x .venv/bin/python ] || die "no .venv in $RIG_DIR"
if pgrep -f app35_cam_sole.py >/dev/null; then
  die "an app35_cam_sole.py is already running; stop it first (kill -INT \$(pgrep -f app35_cam_sole.py))"
fi

# Calibration to install: newest one under sessions/, unless CAL_JSON is set.
if [ -z "${CAL_JSON:-}" ]; then
  CAL_JSON=$(ls -t sessions/*/calibration/calibration_cam1.json 2>/dev/null | head -1)
fi
[ -n "$CAL_JSON" ] && [ -f "$CAL_JSON" ] || die "no calibration_cam1.json found; set CAL_JSON=<path>"
case "$CAL_JSON" in
  /*) CAL_REL=$(python3 -c "import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))" "$CAL_JSON" "$RIG_DIR") ;;
  *)  CAL_REL=$CAL_JSON ;;
esac

log "run $RUN: $TAKES x ${TAKE_S}s, branch $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
log "env: ${RUN_ENV[*]:-(none)}"
log "calibration: $CAL_REL"

# ---- launch --------------------------------------------------------------
BEFORE=$(ls -d sessions/session_* 2>/dev/null | sort | tail -1)
env "${RUN_ENV[@]}" .venv/bin/python app35_cam_sole.py > "$RUN_DIR/console.log" 2>&1 &
APP_PID=$!
cleanup() {
  if kill -0 "$APP_PID" 2>/dev/null; then
    log "stopping app (pid $APP_PID)"
    kill -INT "$APP_PID" 2>/dev/null
    for _ in $(seq 1 30); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 1; done
    kill -0 "$APP_PID" 2>/dev/null && kill -KILL "$APP_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  curl -sf -o /dev/null "$BASE/status" && break
  kill -0 "$APP_PID" 2>/dev/null || die "app exited during startup; see $RUN_DIR/console.log"
  sleep 1
done
curl -sf -o /dev/null "$BASE/status" || die "app did not answer on $BASE"

SESSION=$(ls -d sessions/session_* | sort | tail -1)
[ "$SESSION" != "$BEFORE" ] || die "no new session folder appeared"
log "session: $SESSION"

# ---- sport + calibration ---------------------------------------------------
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -d sport=wide_angle "$BASE/select_sport")
[ "$code" = "302" ] || [ "$code" = "200" ] || die "select_sport returned $code"
resp=$(curl -s -X POST "$BASE/api/calibration/upload_json" -H 'Content-Type: application/json' \
       -d "{\"path\":\"$CAL_REL\"}")
echo "$resp" | grep -q '"json_path"' || die "calibration upload failed: $resp"
[ -f "$SESSION/calibration/calibration_cam1.json" ] || die "calibration not in $SESSION/calibration"
log "sport=wide_angle, calibration installed"

sleep 3
wait_cameras || die "cameras never became healthy after launch; see $RUN_DIR/console.log"

# ---- takes -----------------------------------------------------------------
for i in $(seq 1 "$TAKES"); do
  REC="$SESSION/recording_$i"
  log "take $i/$TAKES: start"
  for attempt in 1 2 3; do
    resp=$(curl -s -X POST "$BASE/api/start_recording")
    echo "$resp" | grep -q '"recording_started"' && break
    log "start_recording attempt $attempt: $resp"
    [ "$attempt" = 3 ] && die "could not start take $i"
    sleep 10
  done
  half=$(( TAKE_S / 2 ))
  sleep "$half"
  snap "$RUN_DIR/take${i}_top.txt"
  ls -la "$REC" > "$RUN_DIR/take${i}_ls_midtake.txt" 2>&1
  # Copy mode writes ~12 MB/s per camera; a raw file still under 1 MB at
  # mid-take means that camera's stream never delivered.
  for f in "$REC"/cam*.mkv "$REC"/cam*.mp4; do
    [ -f "$f" ] || continue
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    [ "$sz" -lt 1000000 ] && log "WARNING take $i: $(basename "$f") is only ${sz} bytes at mid-take"
  done
  sleep $(( TAKE_S - half ))

  log "take $i: stop (the stop request blocks until the take is ready)"
  ( sleep 10; snap "$RUN_DIR/take${i}_stop_top.txt" ) &
  SNAP_PID=$!
  t0=$(date +%s.%N)
  resp=$(curl -s -X POST "$BASE/api/stop_recording")
  t1=$(date +%s.%N)
  wait "$SNAP_PID" 2>/dev/null
  echo "$resp" > "$RUN_DIR/take${i}_stop_response.json"
  wall=$(python3 -c "print(round($t1 - $t0, 1))")
  if [ -f "$REC/pipeline_timing.json" ]; then
    log "take $i: ready after ${wall}s wall (stop request)"
  else
    log "take $i: NO pipeline_timing.json in $REC; stop response: $resp"
  fi
  if [ "$i" -lt "$TAKES" ]; then
    sleep "$GAP_S"
    wait_cameras || log "WARNING: starting take $((i + 1)) with cameras not all healthy"
  fi
done

# ---- wind down -------------------------------------------------------------
cleanup
trap - EXIT
grep '\[stop\]' logs/rig.log 2>/dev/null | tail -n "$TAKES" > "$RUN_DIR/rig_stop_lines.txt"
grep -E 'Enqueued|Uploaded' logs/upload.log 2>/dev/null | tail -n $(( TAKES * 4 )) > "$RUN_DIR/upload_lines.txt"
tar czf "$OUT_DIR/$RUN.tgz" --exclude='*.mp4' --exclude='*.mkv' --exclude='*.avi' --exclude='*.pgm' \
    -C "$RIG_DIR" "$SESSION" -C "$OUT_DIR" "$RUN" 2>/dev/null
log "bundle: $OUT_DIR/$RUN.tgz"

# ---- summary ---------------------------------------------------------------
python3 - "$SESSION" "$RUN" <<'PY'
import glob, json, os, sys
session, run = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.join(session, "recording_*", "pipeline_timing.json")),
               key=lambda p: int(p.split("recording_")[1].split(os.sep)[0]))
if not files:
    print("no pipeline_timing.json written"); sys.exit(0)
cfg = json.load(open(files[0]))["config"]
keys = ("cuda_usable", "record_mode", "sync_decoder", "sync_encoder", "undistort_backend", "remap_oversample",
        "sync_preset", "sync_threads", "output_res", "pause_preview_on_stop", "capture_res")
print("config:", ", ".join(f"{k}={cfg[k]}" for k in keys if k in cfg))
print(f"{'take':>4} {'take_s':>7} {'stop_s':>7} {'s/s':>5} {'sync_s':>7} {'post_s':>7}  sync speed c1/c2/c3   frames c1/c2/c3   MB   validation")
for p in files:
    t = json.load(open(p)); st = t.get("stages", {})
    dur = t.get("sync_duration_s") or 0.0; stop = t.get("stop_to_ready_s") or 0.0
    se = t.get("sync_encodes", {})
    sp = "/".join(f"{se.get(c, {}).get('speed', 0):.2f}" for c in ("cam1", "cam2", "cam3"))
    fr = "/".join(str(se.get(c, {}).get("frame", "-")) for c in ("cam1", "cam2", "cam3"))
    mb = sum(t.get("sync_files_mb", {}).values())
    n = p.split("recording_")[1].split(os.sep)[0]
    print(f"{n:>4} {dur:7.1f} {stop:7.1f} {stop / dur if dur else 0:5.2f} {st.get('sync_s', 0):7.1f} {st.get('postprocess_s', 0):7.1f}  {sp:<20} {fr:<17} {mb:5.0f}  {t.get('validation_status')}")
PY
