import glob, os, time
os.environ['PHONE_SCAN'] = '1'
from worker.detector import PersonDetector
import worker.main as W
W.PHONE_SCAN_CONF = 0.05
det = PersonDetector('/assets/yolov8n.pt', conf=0.25, imgsz=480, device='cpu')
for f in sorted(glob.glob(os.environ.get('FRAMES_DIR', '/tmp') + '/f*.jpg')):
    import cv2
    frame = cv2.imread(f)
    t0 = time.time()
    dets, phones = det.detect(frame)
    extra = W.scan_person_phones(det, frame, dets) if dets else []
    print(f.split('/')[-1], 'persons', len(dets), 'crop_phones', [(round(p['conf'],2)) for p in extra], '%.1fs' % (time.time()-t0), flush=True)
