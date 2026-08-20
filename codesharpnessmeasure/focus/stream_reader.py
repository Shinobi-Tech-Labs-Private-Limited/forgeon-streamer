import sys
import threading
import time

import cv2


class RTSPReader:
    def __init__(self, source):
        self.source = source
        self.frame = None
        self.frame_ts = None
        self.started = False
        self._lock = threading.Lock()

    def start(self):
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._loop, daemon=True).start()
        time.sleep(3)  # wait for camera to warm up

    def _loop(self):
        # CAP_DSHOW is a Windows-only backend and cannot open RTSP on Linux;
        # use it only for local webcam indices on Windows.
        if sys.platform == "win32" and isinstance(self.source, int):
            cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW)
        else:
            cap = cv2.VideoCapture(self.source)
        print("Camera opened:", cap.isOpened())
        for _ in range(10):
            cap.read()
        while True:
            ret, frame = cap.read()
            if ret:
                with self._lock:
                    self.frame = frame
                    self.frame_ts = time.monotonic()
            time.sleep(0.03)

    def get_frame(self):
        with self._lock:
            return self.frame.copy() if self.frame is not None else None

    def get_frame_age(self):
        with self._lock:
            if self.frame_ts is None:
                return None
            return time.monotonic() - self.frame_ts
