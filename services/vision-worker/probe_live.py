import cv2, os, time
os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp'
os.environ['PHONE_SCAN'] = '1'
from worker.detector import PersonDetector
import worker.main as W
det = PersonDetector('/assets/yolov8n.pt', conf=0.25, imgsz=480, device='cpu')
cap = cv2.VideoCapture('rtsp://mediamtx:8554/cam3')
best = 0.0
for i in range(12):
    ok, frame = cap.read()
    if not ok:
        time.sleep(1); cap = cv2.VideoCapture('rtsp://mediamtx:8554/cam3'); continue
    t0 = time.time()
    dets, phones = det.detect(frame)
    extra = W.scan_person_phones(det, frame, dets) if dets else []
    dt = time.time() - t0
    conf = max([p['conf'] for p in extra], default=0.0)
    best = max(best, conf)
    print(f'frame{i} persons={len(dets)} full={len(phones)} crop={len(extra)} maxconf={conf:.2f} {dt:.1f}s', flush=True)
    time.sleep(1)
print('best', round(best, 2))
