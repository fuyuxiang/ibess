"""Drain RTSP continuously so slow inference never timestamps old buffered frames as live."""
import threading
import time
import cv2


class LatestFrameReader:
    def __init__(self, source, *, opener=None, wait_seconds=1):
        self.source, self.opener = source, opener or cv2.VideoCapture
        self.wait_seconds = wait_seconds
        self.condition, self.stopped = threading.Condition(), threading.Event()
        self.sequence = self.delivered = 0
        self.frame = None
        self.captured_at = self.last_captured_at = None
        threading.Thread(target=self._loop, daemon=True, name='camera-reader').start()

    def isOpened(self):
        return not self.stopped.is_set()

    def grab(self):
        return self.isOpened()  # the producer already drains all buffered frames

    def read(self):
        with self.condition:
            self.condition.wait_for(lambda:self.sequence>self.delivered or self.stopped.is_set(), timeout=self.wait_seconds)
            if self.sequence<=self.delivered or self.frame is None:
                return False, None
            self.delivered = self.sequence
            self.last_captured_at = self.captured_at
            return True, self.frame.copy()

    def release(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()

    def _loop(self):
        while not self.stopped.is_set():
            cap = None
            try:
                cap = self.opener(self.source)
                while not self.stopped.is_set() and cap.isOpened():
                    ok, frame = cap.read()
                    captured = time.time()
                    if not ok:
                        break
                    with self.condition:
                        self.frame, self.captured_at = frame, captured
                        self.sequence += 1
                        self.condition.notify_all()
            except Exception:
                pass  # reconnect; the consumer reports offline using the last capture time
            finally:
                if cap is not None:
                    cap.release()
            self.stopped.wait(1)
