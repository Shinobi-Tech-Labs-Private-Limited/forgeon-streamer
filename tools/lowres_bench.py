#!/usr/bin/env python3
"""Quantify what a lower capture resolution buys the rig (test/low-res, step 3).

Replays the rig's own post-stop pipeline on one source clip at several
resolutions and reports, per resolution:

  * MJPEG stream size          what the camera would push over the LAN
  * live record  (MJPEG -> H.264, the take-time encoder: libx264 veryfast
                  crf 18 / NVENC p5 cq 19, -bf 0, fps filter)   time + size
  * sync re-encode (H.264 -> H.264, libx264 veryfast crf 20 or NVENC p5
                  cq 20, +faststart)                          time + size
  * undistort loop (OpenCV remap per frame into an mp4v intermediate,
                  exactly like _undistort_video_file; synthetic pinhole
                  intrinsics with mild barrel distortion)        time
  * quality       PSNR / SSIM of the sync output upscaled back to the
                  source size vs the source clip

The encoder arguments are copied from app35_cam_sole.py
(best_record_encoder_args / best_sync_encoder_args) so the numbers track
what the rig actually runs. Absolute times depend on the machine running
this script; use the ratios between resolutions, not the seconds, when
reasoning about the rig. Sizes and PSNR/SSIM transfer directly.

What this does NOT measure: the effect on the downstream analysis
(pose / ball tracking accuracy). It writes the low-res sync outputs to
--out so that analysis can be run on them.

Usage (from the repository root):

    python tools/lowres_bench.py --src path/to/side_view.mp4
    python tools/lowres_bench.py --src clip.mp4 --res 1280x720,960x540,640x360 \\
        --encoder both --out /tmp/lowres --md docs/lowres-benchmark.md

Requires ffmpeg/ffprobe on PATH; OpenCV + numpy are optional (the undistort
row is skipped without them).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_RES = "1280x720,960x540,854x480,640x360"
TARGET_FPS = 90


# --------------------------------------------------------------------------- helpers
def run(cmd: list[str], log: Path | None = None) -> tuple[float, str]:
    """Run cmd, return (wall seconds, stderr text). Raises on non-zero exit."""
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    dt = time.perf_counter() - t0
    if log is not None:
        log.write_text("CMD: " + " ".join(cmd) + "\n\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    return dt, proc.stderr


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration,size",
            "-of", "json", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    ).stdout
    j = json.loads(out)
    st = j["streams"][0]
    fmt = j["format"]
    num, den = (st.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "codec": st.get("codec_name"),
        "width": int(st.get("width") or 0),
        "height": int(st.get("height") or 0),
        "fps": fps,
        "frames": int(st.get("nb_frames") or 0),
        "duration": float(fmt.get("duration") or 0.0),
        "size": int(fmt.get("size") or 0),
    }


def parse_res(spec: str) -> list[tuple[int, int]]:
    out = []
    for item in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", item)
        if not m:
            raise SystemExit(f"bad resolution '{item}', expected WxH")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def mb(n: int) -> float:
    return n / 1_000_000.0


def nvenc_usable() -> bool:
    if platform.system().lower() not in ("linux", "windows"):
        return False
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True).stdout
    if "h264_nvenc" not in enc:
        return False
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.2",
         "-c:v", "h264_nvenc", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    return proc.returncode == 0


# ------------------------------------------------------------- encoder argument sets
# Mirrors best_record_encoder_args() / best_sync_encoder_args() in app35_cam_sole.py.
def record_enc_args(encoder: str, fps: int) -> list[str]:
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "ll", "-b:v", "0", "-cq", "19",
                "-g", str(fps), "-profile:v", "high", "-pix_fmt", "nv12", "-bf", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-bf", "0"]


def sync_enc_args(encoder: str, fps: int) -> list[str]:
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "0", "-cq", "20", "-g", str(fps),
                "-profile:v", "high", "-pix_fmt", "nv12", "-movflags", "+faststart", "-bf", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-bf", "0"]


# ----------------------------------------------------------------------- stages
def make_mjpeg(src: Path, res: tuple[int, int], fps: int, out: Path, log_dir: Path) -> float:
    """Camera stand-in: the source scaled to res and JPEG-compressed per frame."""
    w, h = res
    dt, _ = run(
        ["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-an", "-sn",
         "-vf", f"scale={w}:{h}:flags=lanczos,fps={fps}",
         "-c:v", "mjpeg", "-q:v", "3", "-pix_fmt", "yuvj422p", str(out)],
        log_dir / f"mjpeg_{w}x{h}.log",
    )
    return dt


def live_record(mjpeg: Path, encoder: str, fps: int, out: Path, log: Path) -> float:
    """build_ffmpeg_record_cmd() minus the RTSP input flags."""
    vf = f"settb=AVTB,fps={fps},setpts=N/({fps}*TB)"
    dt, _ = run(
        ["ffmpeg", "-y", "-hide_banner", "-i", str(mjpeg), "-an", "-sn", "-filter:v", vf]
        + record_enc_args(encoder, fps) + ["-video_track_timescale", "90000", str(out)],
        log,
    )
    return dt


def sync_reencode(raw: Path, encoder: str, fps: float, out: Path, log: Path) -> float:
    """run_sync_on_dir()'s per-camera command with offset 0 and full duration."""
    dt, _ = run(
        ["ffmpeg", "-y", "-hide_banner", "-i", str(raw), "-an", "-sn",
         "-vf", f"fps={fps:.6f},setpts=PTS-STARTPTS"]
        + sync_enc_args(encoder, int(round(fps))) + [str(out)],
        log,
    )
    return dt


def undistort_loop(raw: Path, res: tuple[int, int], fps: float, out: Path) -> tuple[float, int] | None:
    """_undistort_video_file()'s Python loop with synthetic intrinsics."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    w, h = res
    f = 0.9 * w
    K = np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1]], dtype=np.float64)
    dist = np.array([-0.25, 0.08, 0.0, 0.0, 0.0], dtype=np.float64).reshape(-1, 1)
    newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), 0.0, (w, h))
    map1, map2 = cv2.initUndistortRectifyMap(K, dist, None, newK, (w, h), cv2.CV_16SC2)

    cap = cv2.VideoCapture(str(raw))
    if not cap.isOpened():
        return None
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        cap.release()
        return None
    n = 0
    t0 = time.perf_counter()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(cv2.remap(frame, map1, map2, cv2.INTER_LINEAR))
        n += 1
    dt = time.perf_counter() - t0
    cap.release()
    writer.release()
    return dt, n


def quality(src: Path, candidate: Path, src_res: tuple[int, int], fps: float, log_dir: Path,
            tag: str) -> dict:
    """PSNR / SSIM of candidate (upscaled to src_res) against src."""
    w, h = src_res
    lavfi = (
        f"[1:v]fps={fps:.6f},setpts=PTS-STARTPTS,scale={w}:{h}:flags=lanczos,format=yuv420p[c];"
        f"[0:v]fps={fps:.6f},setpts=PTS-STARTPTS,format=yuv420p[r];"
        f"[c][r]{{metric}}"
    )
    result = {}
    for metric in ("psnr", "ssim"):
        filt = lavfi.format(metric=metric)
        _, err = run(
            ["ffmpeg", "-hide_banner", "-i", str(src), "-i", str(candidate),
             "-lavfi", filt, "-f", "null", "-"],
            log_dir / f"{metric}_{tag}.log",
        )
        if metric == "psnr":
            m = re.search(r"average:([\d.]+|inf)", err)
            result["psnr_db"] = float("inf") if m and m.group(1) == "inf" else (float(m.group(1)) if m else None)
        else:
            m = re.search(r"All:([\d.]+)", err)
            result["ssim"] = float(m.group(1)) if m else None
    return result


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="source clip (ideally a rig 1280x720 90 fps take)")
    ap.add_argument("--res", default=DEFAULT_RES, help=f"comma list of WxH (default {DEFAULT_RES})")
    ap.add_argument("--fps", type=int, default=TARGET_FPS, help=f"constant frame rate to force (default {TARGET_FPS})")
    ap.add_argument("--encoder", choices=("cpu", "nvenc", "both"), default="cpu",
                    help="libx264 (what the rig falls back to), NVENC (what it prefers), or both")
    ap.add_argument("--out", type=Path, default=Path("lowres_bench_out"), help="scratch + output directory")
    ap.add_argument("--json", type=Path, default=None, help="write results JSON here")
    ap.add_argument("--md", type=Path, default=None, help="write a Markdown report here")
    ap.add_argument("--repeat", type=int, default=1, help="repeat timed stages N times, keep the fastest")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")
    if not args.src.is_file():
        raise SystemExit(f"source not found: {args.src}")

    encoders = ["cpu", "nvenc"] if args.encoder == "both" else [args.encoder]
    if "nvenc" in encoders and not nvenc_usable():
        print("NVENC requested but not usable on this machine; dropping it", file=sys.stderr)
        encoders = [e for e in encoders if e != "nvenc"]
        if not encoders:
            return 2

    src_meta = probe(args.src)
    src_res = (src_meta["width"], src_meta["height"])
    fps = float(args.fps)
    duration = src_meta["duration"]
    print(f"source: {args.src.name} {src_meta['codec']} {src_res[0]}x{src_res[1]} "
          f"{src_meta['fps']:.2f} fps {duration:.2f} s {mb(src_meta['size']):.2f} MB")
    print(f"encoders: {', '.join(encoders)}   forced fps: {args.fps}   repeat: {args.repeat}")

    args.out.mkdir(parents=True, exist_ok=True)
    log_dir = args.out / "logs"
    log_dir.mkdir(exist_ok=True)

    def best(fn, *a):
        vals = [fn(*a) for _ in range(max(1, args.repeat))]
        return min(vals)

    rows = []
    for res in parse_res(args.res):
        w, h = res
        tag = f"{w}x{h}"
        print(f"\n== {tag}")
        mjpeg = args.out / f"cam_{tag}.mkv"
        make_mjpeg(args.src, res, args.fps, mjpeg, log_dir)
        mjpeg_meta = probe(mjpeg)
        print(f"   mjpeg stream        {mb(mjpeg_meta['size']):8.2f} MB  ({mb(mjpeg_meta['size']) / duration * 60:.0f} MB/min)")

        for enc in encoders:
            etag = f"{tag}_{enc}"
            raw = args.out / f"raw_{etag}.mp4"
            sync = args.out / f"sync_{etag}.mp4"
            und = args.out / f"undist_{etag}_opencv_tmp.mp4"

            t_rec = best(live_record, mjpeg, enc, args.fps, raw, log_dir / f"record_{etag}.log")
            raw_meta = probe(raw)
            t_sync = best(sync_reencode, raw, enc, fps, sync, log_dir / f"sync_{etag}.log")
            sync_meta = probe(sync)
            u = undistort_loop(raw, res, fps, und)
            q = quality(args.src, sync, src_res, fps, log_dir, etag)

            row = {
                "res": tag, "width": w, "height": h, "encoder": enc,
                "pixels_rel": (w * h) / (src_res[0] * src_res[1]),
                "clip_s": duration,
                "mjpeg_mb": mb(mjpeg_meta["size"]),
                "record_s": t_rec, "record_x_realtime": duration / t_rec if t_rec else None,
                "raw_mb": mb(raw_meta["size"]),
                "sync_s": t_sync, "sync_x_realtime": duration / t_sync if t_sync else None,
                "sync_mb": mb(sync_meta["size"]),
                "sync_mb_per_min": mb(sync_meta["size"]) / duration * 60 if duration else None,
                "undistort_s": u[0] if u else None,
                "undistort_frames": u[1] if u else None,
                "undistort_x_realtime": (duration / u[0]) if u and u[0] else None,
                "psnr_db": q.get("psnr_db"), "ssim": q.get("ssim"),
                "sync_path": str(sync),
            }
            rows.append(row)
            print(f"   [{enc:5}] record {t_rec:6.2f}s ({row['record_x_realtime']:.1f}x rt) raw {row['raw_mb']:.2f} MB | "
                  f"sync {t_sync:6.2f}s ({row['sync_x_realtime']:.1f}x rt) {row['sync_mb']:.2f} MB "
                  f"({row['sync_mb_per_min']:.0f} MB/min) | "
                  + (f"undistort {u[0]:6.2f}s ({row['undistort_x_realtime']:.1f}x rt) | " if u else "undistort n/a | ")
                  + f"PSNR {q.get('psnr_db')} dB SSIM {q.get('ssim')}")
            try:
                und.unlink()
            except OSError:
                pass

    result = {
        "source": {"path": str(args.src), **src_meta},
        "machine": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                    "python": platform.python_version()},
        "forced_fps": args.fps, "encoders": encoders, "repeat": args.repeat,
        "rows": rows,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\njson -> {args.json}")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_md(result), encoding="utf-8")
        print(f"markdown -> {args.md}")
    return 0


def render_md(result: dict) -> str:
    src = result["source"]
    base = {}
    for r in result["rows"]:
        base.setdefault(r["encoder"], r if r["width"] == src["width"] else base.get(r["encoder"]))
    lines = [
        "# Low-resolution capture benchmark (test/low-res, step 3)",
        "",
        f"Source: `{Path(src['path']).name}` {src['codec']} {src['width']}x{src['height']} "
        f"{src['fps']:.2f} fps, {src['duration']:.2f} s, {mb(src['size']):.2f} MB.  ",
        f"Machine: {result['machine']['platform']}, {result['machine']['cpu_count']} logical CPUs.  ",
        f"Forced frame rate: {result['forced_fps']} fps. Repeats: {result['repeat']} (fastest kept).",
        "",
        "Times are for this machine; compare rows against each other, not against the rig's clock. "
        "Sizes, PSNR and SSIM transfer directly. `x rt` = clip seconds per wall-clock second "
        "(above 1.0 keeps up with real time).",
        "",
        "| res | enc | pixels | MJPEG MB/min | record s (x rt) | raw MB | sync s (x rt) | sync MB/min | undistort s (x rt) | PSNR dB | SSIM |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    d = src["duration"] or 1.0
    for r in result["rows"]:
        und = (f"{r['undistort_s']:.2f} ({r['undistort_x_realtime']:.1f})" if r["undistort_s"] else "n/a")
        psnr = "inf" if r["psnr_db"] == float("inf") else (f"{r['psnr_db']:.2f}" if r["psnr_db"] is not None else "n/a")
        ssim = f"{r['ssim']:.4f}" if r["ssim"] is not None else "n/a"
        lines.append(
            f"| {r['res']} | {r['encoder']} | {r['pixels_rel'] * 100:.0f}% | {r['mjpeg_mb'] / d * 60:.0f} | "
            f"{r['record_s']:.2f} ({r['record_x_realtime']:.1f}) | {r['raw_mb']:.2f} | "
            f"{r['sync_s']:.2f} ({r['sync_x_realtime']:.1f}) | {r['sync_mb_per_min']:.0f} | "
            f"{und} | {psnr} | {ssim} |"
        )
    lines += [
        "",
        "Relative to the full-resolution row for the same encoder:",
        "",
        "| res | enc | record time | sync time | undistort time | sync size |",
        "|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        b = base.get(r["encoder"])
        if not b:
            continue
        def rel(k):
            return f"{r[k] / b[k] * 100:.0f}%" if r.get(k) and b.get(k) else "n/a"
        lines.append(f"| {r['res']} | {r['encoder']} | {rel('record_s')} | {rel('sync_s')} | "
                     f"{rel('undistort_s')} | {rel('sync_mb')} |")
    lines += [
        "",
        "PSNR/SSIM are measured after upscaling the sync output back to the source size, so they "
        "combine downscale loss and codec loss. The full-resolution row is codec loss alone.",
        "",
        "Not measured here: the effect on the cloud analysis. Run it on the `sync_<res>_<enc>.mp4` "
        "files in the output directory to get that number.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
