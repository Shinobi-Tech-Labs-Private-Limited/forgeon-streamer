import cv2
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from copy import deepcopy

# ========= USER CONFIG =========
CAM_SOURCES = [
    # TIP: adding FFmpeg params can improve stability:
    # e.g., "...:8555/video0_side?rtsp_transport=tcp&stimeout=5000000"
    "rtsp://192.168.2.30:8555/video0_side",
    "rtsp://192.168.2.31:8555/video0_front",
    "rtsp://192.168.2.32:8555/video0_bacK",

]

WIDTH = 1280
HEIGHT = 720
FPS = 30

# Locking exposure etc. only applies to USB cams; RTSP sources ignore
LOCK_CAMERA_SETTINGS = False

OUT_DIR = "calib_snaps"
BASENAME = "charuco"

# UI controls
SNAP_KEYS = {13, 10, ord('s'), ord(' ')}  # Enter, Return, 's', Space
QUIT_KEYS = {ord('q'), 27}                # 'q', ESC
USE_TERMINAL_TRIGGER = False              # press ENTER in terminal to snap (no window focus)

# Robustness
READ_RECONNECT_THRESHOLD = 90    # if we get this many consecutive read fails, we try to reopen the stream
TRIGGER_WAIT_TIMEOUT = 2.0       # seconds to wait for a fresh frame on trigger
# ===============================

snap_request = {"flag": False}
def on_mouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        snap_request["flag"] = True

def set_basic_props(cap, is_rtsp):
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)

    if LOCK_CAMERA_SETTINGS and not is_rtsp:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
        cap.set(cv2.CAP_PROP_EXPOSURE, -6)
        cap.set(cv2.CAP_PROP_GAIN, 0)
        cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        cap.set(cv2.CAP_PROP_WB_TEMPERATURE, 4500)

def open_capture(src):
    is_rtsp = isinstance(src, str) and (src.startswith("rtsp://") or src.startswith("rtsps://"))
    cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG if is_rtsp else cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera source: {src}")
    set_basic_props(cap, is_rtsp)
    return cap

class CamWorker(threading.Thread):
    """
    Continuous reader:
      - Continuously cap.read() in a loop and store the latest good frame + timestamp.
      - On trigger, copies the latest good frame (within a timeout).
      - Reconnects if too many consecutive read failures.
    """
    def __init__(self, src, name):
        super().__init__(daemon=True)
        self.src = src
        self.name = name
        self.cap = None
        self.latest_frame = None
        self.latest_ts = 0.0
        self.lock = threading.Lock()

        self.trigger = threading.Event()
        self.done = threading.Event()
        self.snap_frame = None
        self.ok = False

        self.stopped = False
        self.fail_count = 0

    def reopen(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        time.sleep(0.25)
        self.cap = open_capture(self.src)
        self.fail_count = 0

    def run(self):
        self.reopen()

        # Warm up
        t0 = time.time()
        while time.time() - t0 < 0.75 and not self.stopped:
            ok, _ = self.cap.read()
            if ok:
                self.fail_count = 0
            else:
                self.fail_count += 1

        while not self.stopped:
            # Non-blocking read loop (keeps latest frame fresh)
            ok, frame = self.cap.read()
            if ok and frame is not None:
                with self.lock:
                    self.latest_frame = frame
                    self.latest_ts = time.time()
                self.fail_count = 0
            else:
                self.fail_count += 1
                if self.fail_count >= READ_RECONNECT_THRESHOLD:
                    # try to reopen the stream
                    try:
                        self.reopen()
                    except Exception as e:
                        # backoff a bit
                        time.sleep(0.5)

            # Check for trigger without blocking the read loop
            if self.trigger.is_set():
                # We want the freshest frame; wait briefly if we don't have one yet
                start = time.time()
                grabbed = False
                while time.time() - start <= TRIGGER_WAIT_TIMEOUT:
                    with self.lock:
                        if self.latest_frame is not None:
                            # copy to avoid race with next reads
                            self.snap_frame = self.latest_frame.copy()
                            grabbed = True
                            break
                    time.sleep(0.005)
                self.ok = grabbed
                self.done.set()
                self.trigger.clear()

    def stop(self):
        self.stopped = True
        self.trigger.set()
        time.sleep(0.01)
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass

def main():
    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)

    # Start workers
    workers = []
    for i, src in enumerate(CAM_SOURCES, start=1):
        try:
            w = CamWorker(src, f"cam{i}")
            w.start()
            workers.append(w)
            print(f"[OK] Started {w.name}: {src}")
        except Exception as e:
            print(f"[ERR] Failed to start Cam-{i} ({src}): {e}")
            # stop previously started
            for ww in workers:
                ww.stop()
                ww.join(timeout=0.5)
            return

    # UI windows
    show_preview = True
    if show_preview:
        for i in range(len(workers)):
            win = f"Cam {i+1}"
            cv2.namedWindow(win, cv2.WINDOW_NORMAL)
            cv2.setMouseCallback(win, on_mouse)

    print("\nControls:")
    print("  Click any preview window to focus it, then:")
    print("   • ENTER / RETURN  -> snap all")
    print("   • SPACE or 's'    -> snap all")
    print("   • Left mouse click in any preview window -> snap all")
    print("   • 'q' or ESC      -> quit")
    if USE_TERMINAL_TRIGGER:
        print("Terminal mode ON: press ENTER in the terminal to snap.\n")

    snap_idx = 1

    try:
        while True:
            # Live preview (latest frames)
            if show_preview:
                for i, w in enumerate(workers, start=1):
                    frame = None
                    with w.lock:
                        if w.latest_frame is not None:
                            frame = w.latest_frame
                    if frame is not None:
                        cv2.imshow(f"Cam {i}", frame)
                key = cv2.waitKey(1) & 0xFF

                if snap_request["flag"]:
                    key = 13
                    snap_request["flag"] = False
            else:
                s = input("Press ENTER to snap, or type 'q' + ENTER to quit: ").strip().lower()
                key = ord('q') if s == 'q' else 13

            if key in SNAP_KEYS:
                # Trigger all cams to latch their latest frames
                for w in workers:
                    w.done.clear()
                    w.trigger.set()
                # Wait for all to report done
                for w in workers:
                    w.done.wait(timeout=TRIGGER_WAIT_TIMEOUT + 0.25)

                # Save
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                saved = 0
                for i, w in enumerate(workers, start=1):
                    if w.ok and w.snap_frame is not None:
                        fname = f"{BASENAME}_cam{i}_{snap_idx:04d}_{ts}.png"
                        fpath = os.path.join(OUT_DIR, fname)
                        cv2.imwrite(fpath, w.snap_frame)
                        h, w_, _ = w.snap_frame.shape
                        print(f"[SAVE] {fpath} ({w_}x{h})")
                        saved += 1
                    else:
                        print(f"[WARN] {w.name} did not provide a frame this round "
                              f"(network jitter? reconnecting={w.fail_count>=READ_RECONNECT_THRESHOLD}).")

                if saved == len(workers):
                    snap_idx += 1
                else:
                    print("[PARTIAL] Not all cameras saved—will still work for calibration, "
                          "but try another snap for full 3-view coverage.")

            elif key in QUIT_KEYS:
                break

    finally:
        for w in workers:
            w.stop()
        time.sleep(0.05)
        for w in workers:
            try:
                w.join(timeout=0.6)
            except Exception:
                pass
        cv2.destroyAllWindows()
        print("Closed all cameras.")

if __name__ == "__main__":
    main()
