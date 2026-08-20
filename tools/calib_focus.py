#!/usr/bin/env python3
"""
Calibration-Grade Focus Checker (STRICT, no preview mode)

Designed for:
- Camera calibration
- Low reprojection RMS (< 1 px)
- Multi-camera consistency

Controls:
- q : quit
- r : reset buffers + learned peaks
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional

import cv2
import numpy as np

# ---------------- RTSP inputs ----------------
CAMS = {
    "cam1_side":  "rtsp://192.168.2.30:8555/video0_side",
    "cam2_front": "rtsp://192.168.2.33:8555/video0_front",
    "cam3_back":  "rtsp://192.168.2.32:8555/video0_back",
    "cam4_top":  "rtsp://192.168.2.34:8555/video0_top",
}

# ---------------- Connection / display ----------------
CONNECT_TIMEOUT_SEC = 8.0
RECONNECT_BACKOFF_SEC = 1.0
DISPLAY_SCALE = 0.75
cv2.setNumThreads(0)

# ---------------- Calibration focus parameters ----------------
ROLLING_FRAMES = 20

CENTER_ROI_FRAC = 0.55
CORNER_ROI_FRAC = 0.22

ABS_CENTER_THRESHOLD = 220.0      # reject obviously soft images
PEAK_RATIO_REQUIRED = 0.90        # must be ≥ 90% of learned peak
MIN_CORNER_RATIO = 0.80           # corners ≥ 80% of center
MAX_CV = 0.06                     # ≤ 6% temporal variation

GAUSS_SIGMA = 1.0                 # MJPEG noise suppression

# ---------------- Utilities ----------------
def beep():
    print("\a", end="", flush=True)

def crop_center(img, frac):
    h, w = img.shape[:2]
    cw, ch = int(w * frac), int(h * frac)
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return img[y0:y0+ch, x0:x0+cw]

def crop_corner(img, which, frac):
    h, w = img.shape[:2]
    cw, ch = int(w * frac), int(h * frac)

    if which == "tl": x0, y0 = 0, 0
    elif which == "tr": x0, y0 = w - cw, 0
    elif which == "bl": x0, y0 = 0, h - ch
    elif which == "br": x0, y0 = w - cw, h - ch
    else: raise ValueError(which)

    return img[y0:y0+ch, x0:x0+cw]

def failure_reasons(gates):
    reasons = []
    if not gates["abs"]:
        reasons.append("LOW_CENTER_SHARPNESS")
    if not gates["peak"]:
        reasons.append("NOT_AT_PEAK_FOCUS")
    if not gates["corners"]:
        reasons.append("SOFT_CORNERS")
    if not gates["stable"]:
        reasons.append("FOCUS_UNSTABLE")
    return reasons

def laplacian_focus(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (0, 0), GAUSS_SIGMA)
    lap = cv2.Laplacian(gray, cv2.CV_64F, ksize=3)
    return float(lap.var())

def rolling_stats(buf):
    arr = np.array(buf, dtype=np.float64)
    mean = arr.mean()
    std = arr.std()
    cv = std / mean if mean > 1e-9 else 0.0
    return mean, std, cv

# ---------------- Camera state ----------------
@dataclass
class FocusState:
    center: deque
    corners: Dict[str, deque]
    peak_center: float
    in_focus_latched: bool
    last_ok: float

class Camera:
    def __init__(self, name, url):
        self.name = name
        self.url = url
        self.cap: Optional[cv2.VideoCapture] = None
        self.state = FocusState(
            center=deque(maxlen=ROLLING_FRAMES),
            corners={k: deque(maxlen=ROLLING_FRAMES) for k in ["tl","tr","bl","br"]},
            peak_center=0.0,
            in_focus_latched=False,
            last_ok=0.0,
        )

    def connect(self):
        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        t0 = time.time()
        while time.time() - t0 < CONNECT_TIMEOUT_SEC:
            ok, _ = self.cap.read()
            if ok:
                self.state.last_ok = time.time()
                return True
            time.sleep(0.1)
        return False

    def read(self):
        if not self.cap or not self.cap.isOpened():
            return False, None
        ok, frame = self.cap.read()
        if ok:
            self.state.last_ok = time.time()
        return ok, frame

# ---------------- Focus evaluation ----------------
def evaluate(frame, st: FocusState):
    c_roi = crop_center(frame, CENTER_ROI_FRAC)
    c_score = laplacian_focus(c_roi)
    st.center.append(c_score)

    corner_means = {}
    for k in st.corners:
        roi = crop_corner(frame, k, CORNER_ROI_FRAC)
        s = laplacian_focus(roi)
        st.corners[k].append(s)
        corner_means[k] = np.mean(st.corners[k])

    c_mean, c_std, c_cv = rolling_stats(st.center)
    st.peak_center = max(st.peak_center, c_mean)

    min_corner = min(corner_means.values())
    corner_ratio = min_corner / c_mean if c_mean > 1e-9 else 0.0

    gates = {
        "abs": c_mean >= ABS_CENTER_THRESHOLD,
        "peak": c_mean >= PEAK_RATIO_REQUIRED * st.peak_center,
        "corners": corner_ratio >= MIN_CORNER_RATIO,
        "stable": c_cv <= MAX_CV,
    }

    enough_history = len(st.center) >= ROLLING_FRAMES // 2
    in_focus = enough_history and all(gates.values())

    return {
        "center": c_mean,
        "cv": c_cv,
        "corner_ratio": corner_ratio,
        "gates": gates,
        "in_focus": in_focus,
        "peak": st.peak_center,
    }

# ---------------- Main loop ----------------
def main():
    cams = [Camera(n,u) for n,u in CAMS.items()]
    print("Connecting cameras...")
    for c in cams:
        print(f"{c.name}: {'OK' if c.connect() else 'FAILED'}")

    while True:
        all_ok = True

        for cam in cams:
            ok, frame = cam.read()
            if not ok:
                all_ok = False
                if time.time() - cam.state.last_ok > 2:
                    cam.connect()
                continue

            m = evaluate(frame, cam.state)
            if not m["in_focus"]:
                all_ok = False

            if m["in_focus"] and not cam.state.in_focus_latched:
                cam.state.in_focus_latched = True
                beep()
                print(f"✅ {cam.name} CALIBRATION-FOCUS OK "
                      f"| center={m['center']:.1f} "
                      f"| ratio={m['corner_ratio']:.2f} "
                      f"| cv={m['cv']*100:.1f}%")

            disp = frame
            if DISPLAY_SCALE != 1.0:
                disp = cv2.resize(disp, None, fx=DISPLAY_SCALE, fy=DISPLAY_SCALE)

            txt = f"{cam.name}  center:{m['center']:.1f}  ratio:{m['corner_ratio']:.2f}  cv:{m['cv']*100:.1f}%"
            cv2.putText(disp, txt, (10,30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
            if m["in_focus"]:
                 status = "IN FOCUS"
            else:
             reasons = failure_reasons(m["gates"])
             status = "REJECT: " + ",".join(reasons)

            cv2.putText(
              disp,
              status,
             (10, 65),
             cv2.FONT_HERSHEY_SIMPLEX,
             0.7,
             (0,255,0) if m["in_focus"] else (0,0,255),
             2
             )

            cv2.imshow(cam.name, disp)

        if all_ok:
            beep()
            print("🎯 ALL CAMERAS CALIBRATION-FOCUSED")

        k = cv2.waitKey(1) & 0xFF
        if k == ord('q'):
            break
        if k == ord('r'):
            for c in cams:
                c.state.center.clear()
                for d in c.state.corners.values():
                    d.clear()
                c.state.peak_center = 0.0
                c.state.in_focus_latched = False
            print("↩️ buffers reset")

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
