import cv2, os, time
os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp'
cap = cv2.VideoCapture('rtsp://mediamtx:8554/cam3')
saved = 0
deadline = time.time() + 30
while saved < 8 and time.time() < deadline:
    ok, frame = cap.read()
    if not ok:
        time.sleep(0.5); continue
    cv2.imwrite('/tmp/f%02d.jpg' % saved, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    saved += 1
    time.sleep(1.5)
print('saved', saved, 'size', frame.shape)
