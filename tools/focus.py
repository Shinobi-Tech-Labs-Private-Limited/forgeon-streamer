#!/usr/bin/env python3
"""
RTSP Focus Checker (3 cams)
- Computes a sharpness score per camera (variance of Laplacian)
- Shows live scores + IN FOCUS / SOFT
- Beeps + prints a message when a camera first becomes "in focus"
- Press 'q' to quit, 'r' to reset "focus achieved" flags

Requirements:
  pip install opencv-python numpy
"""

import time
from collections import deque
import cv2
import numpy as np

CAMS = {
    "cam1_side":  "rtsp://192.168.2.30:8555/video0_side",
    "cam2_front": "rtsp://192.168.2.33:8555/video0_front",
    "cam3_back":  "rtsp://192.168.2.32:8555/video0_back",
}

# --- Tuning knobs ---
# Focus metric: Var(Laplacian). Higher = sharper.
# Threshold depends heavily on: resolution, compression, lighting, shutter speed, scene texture.
FOCUS_THRESHOLD = 140.0          # start here; adjust after you see typical scores
ROLLING_AVG_FRAMES = 15          # smooth noisy scores
ROI_FRACTION = 0.55              # use center ROI to avoid edge shading/vignetting (0.4–0.7 typical)
CONNECT_TIMEOUT_SEC = 8.0
RECONNECT_BACKOFF_SEC = 1.0
DISPLAY_SCALE = 0.75             # resize windows for convenience

# OpenCV RTSP options (helpful for lower latency / fewer stalls)
cv2.setNumThreads(0)

def beep():
    # Terminal beep (works on most terminals)
    print("\a", end="", flush=True)

def focus_score_bgr(frame_bgr: np.ndarray, roi_fraction: float) -> float:
    h, w = frame_bgr.shape[:2]
    rf = float(np.clip(roi_fraction, 0.2, 1.0))
    rw, rh = int(w * rf), int(h * rf)
    x0 = (w - rw) // 2
    y0 = (h - rh) // 2
    roi = frame_bgr[y0:y0+rh, x0:x0+rw]

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    # Light denoise helps with MJPEG noise
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    lap = cv2.Laplacian(gray, cv2.CV_64F, ksize=3)
    return float(lap.var())

class CamWatcher:
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url
        self.cap = None
        self.last_ok = 0.0
        self.scores = deque(maxlen=ROLLING_AVG_FRAMES)
        self.in_focus_once = False

    def connect(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass

        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)

        # Some OpenCV builds honor these; harmless if ignored:
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        t0 = time.time()
        while time.time() - t0 < CONNECT_TIMEOUT_SEC:
            ok, _ = self.cap.read()
            if ok:
                self.last_ok = time.time()
                return True
            time.sleep(0.1)
        return False

    def read_frame(self):
        if self.cap is None or not self.cap.isOpened():
            return False, None

        ok, frame = self.cap.read()
        if ok and frame is not None and frame.size > 0:
            self.last_ok = time.time()
            return True, frame
        return False, None

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

def main():
    watchers = [CamWatcher(n, u) for n, u in CAMS.items()]

    print("Connecting to cameras...")
    for w in watchers:
        ok = w.connect()
        print(f"  {w.name}: {'OK' if ok else 'FAILED'} ({w.url})")

    print("\nControls: 'q' quit | 'r' reset focus flags\n")
    time.sleep(0.2)

    while True:
        all_in_focus = True

        for w in watchers:
            ok, frame = w.read_frame()
            if not ok:
                all_in_focus = False
                # Reconnect if stale
                if time.time() - w.last_ok > 2.0:
                    print(f"[{w.name}] stream stalled, reconnecting...")
                    w.connect()
                    time.sleep(RECONNECT_BACKOFF_SEC)
                continue

            score = focus_score_bgr(frame, ROI_FRACTION)
            w.scores.append(score)
            avg_score = float(np.mean(w.scores)) if w.scores else score

            status = "IN FOCUS" if avg_score >= FOCUS_THRESHOLD else "SOFT"
            if avg_score < FOCUS_THRESHOLD:
                all_in_focus = False

            # One-time prompt when it first crosses threshold
            if (avg_score >= FOCUS_THRESHOLD) and (not w.in_focus_once) and (len(w.scores) >= w.scores.maxlen // 2):
                w.in_focus_once = True
                beep()
                print(f"✅ {w.name} now IN FOCUS (avg={avg_score:.1f}, thr={FOCUS_THRESHOLD:.1f})")

            # Draw overlay
            disp = frame
            if DISPLAY_SCALE != 1.0:
                disp = cv2.resize(disp, (0, 0), fx=DISPLAY_SCALE, fy=DISPLAY_SCALE, interpolation=cv2.INTER_AREA)

            text1 = f"{w.name}  score(avg): {avg_score:.1f}   thr: {FOCUS_THRESHOLD:.1f}"
            text2 = f"STATUS: {status}"
            cv2.putText(disp, text1, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2, cv2.LINE_AA)
            cv2.putText(disp, text2, (12, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0,255,0) if status=="IN FOCUS" else (0,0,255), 2, cv2.LINE_AA)

            cv2.imshow(w.name, disp)

        # Global prompt when all are in focus
        if all_in_focus:
            beep()
            print("🎯 ALL CAMERAS IN FOCUS (based on rolling average).")

            # Optional: avoid spamming by sleeping a bit
            time.sleep(0.6)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        if key == ord('r'):
            for w in watchers:
                w.in_focus_once = False
                w.scores.clear()
            print("↩️ reset focus flags + score buffers")

    for w in watchers:
        w.close()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
