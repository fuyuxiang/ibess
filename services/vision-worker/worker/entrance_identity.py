"""Local YuNet/SFace entrance matching. No embeddings leave this process or enter events."""
import hashlib
import json
from pathlib import Path
import queue
import threading

import cv2
import numpy as np
from sqlalchemy import create_engine, text

MODEL_FILES = {
    'face_detection_yunet_2023mar.onnx': '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4',
    'face_recognition_sface_2021dec.onnx': '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79',
}
MODEL_VERSION = 'yunet-sface:' + ':'.join(x[:16] for x in MODEL_FILES.values())


def select_identity(probe, profiles, allowed, snapshot_id, threshold=.363, margin=.05):
    scored = []
    for profile in profiles:
        if profile['model'] != MODEL_VERSION:
            continue
        vector = profile['embedding']
        try:
            vector = np.asarray(json.loads(vector) if isinstance(vector, str) else vector, dtype=np.float32)
        except (ValueError, TypeError):
            continue
        if vector.shape != (128,) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
            continue
        scored.append((float(np.dot(probe, vector)/np.linalg.norm(vector)), profile['person_id']))
    scored.sort(reverse=True)
    if not snapshot_id or not scored or scored[0][0] < threshold or (len(scored) > 1 and scored[0][0]-scored[1][0] < margin):
        return {'status': 'unverified', 'authorized': None}
    similarity, person = scored[0]
    return {'status': 'matched', 'person_id': person, 'similarity': similarity,
            'authorized': person in allowed, 'authorization_snapshot_id': snapshot_id}


class EntranceIdentity:
    def __init__(self, database_url, model_dir):
        self.url, self.directory = database_url, Path(model_dir)
        self.jobs, self.done = queue.Queue(maxsize=2), queue.Queue(maxsize=8)
        self.status = 'model_not_configured'
        self.detector = self.recognizer = self.engine = None
        if all((self.directory/name).is_file() for name in MODEL_FILES):
            self.status = 'ready'
        threading.Thread(target=self._loop, daemon=True, name='entrance-identity').start()

    def check(self, frame, crossings, scope):
        generation = tuple(scope.get(k) for k in ('session_id', 'room_id', 'stage_snapshot_id'))
        for track in crossings:
            if not self.jobs.full():
                self.jobs.put_nowait((frame.copy(), dict(track), dict(scope), generation))
        results = []
        while not self.done.empty():
            old_generation, result = self.done.get_nowait()
            if generation == old_generation and scope['occurred_at']-(result.get('occurred_at') or scope['occurred_at']) <= 30:
                results.append(result)
        return results

    def _load(self):
        if self.detector is not None:
            return
        for name, digest in MODEL_FILES.items():
            if hashlib.sha256((self.directory/name).read_bytes()).hexdigest() != digest:
                raise ValueError('face model hash mismatch')
        self.detector = cv2.FaceDetectorYN.create(str(self.directory/next(iter(MODEL_FILES))), '', (320, 320), .85, .3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(self.directory/'face_recognition_sface_2021dec.onnx'), '')
        from sqlalchemy.engine import make_url
        original_options = make_url(self.url).query.get('options', '')
        args = {'connect_timeout':2, 'options':(original_options+' -c statement_timeout=2000').strip()} if self.url.startswith('postgresql') else {}
        self.engine = create_engine(self.url, pool_pre_ping=True, pool_size=1, max_overflow=0, connect_args=args)

    def _loop(self):
        while True:
            frame, track, scope, generation = self.jobs.get()
            result = {'track_id': track['track_id'], 'status': 'unverified', 'authorized': None,
                      'occurred_at': scope.get('occurred_at'), 'identity_model': MODEL_VERSION}
            try:
                self._load()
                h, w = frame.shape[:2]
                x1, y1, x2, y2 = track['bbox']
                crop = frame[max(0, int(y1*h)):min(h, int(y2*h)), max(0, int(x1*w)):min(w, int(x2*w))]
                self.detector.setInputSize((crop.shape[1], crop.shape[0]))
                _, faces = self.detector.detect(crop)
                if faces is not None and len(faces) == 1 and min(faces[0][2:4]) >= 80:
                    aligned = self.recognizer.alignCrop(crop, faces[0])
                    if cv2.Laplacian(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var() >= 20:
                        probe = self.recognizer.feature(aligned).reshape(-1).astype(np.float32)
                        probe /= max(1e-8, np.linalg.norm(probe))
                        with self.engine.connect() as db:
                            snapshot = db.execute(text('''SELECT snapshot_id FROM business_authorization_snapshots
                                WHERE room_id=:room AND project_id=:project AND created_at<=to_timestamp(:at)
                                ORDER BY created_at DESC LIMIT 1'''), {'room': scope['room_id'], 'project': scope['project_id'], 'at': scope['occurred_at']}).scalar()
                            profiles = db.execute(text('SELECT person_id,model,embedding FROM business_face_profiles')).mappings().all()
                            allowed = set(db.execute(text('SELECT person_id FROM business_authorization_entries WHERE snapshot_id=:id'), {'id': snapshot}).scalars()) if snapshot else set()
                            threshold = db.execute(text('SELECT threshold_same FROM face_verify_config WHERE id=1')).scalar() or .363
                        result.update(select_identity(probe, profiles, allowed, snapshot, float(threshold)))
                        if result['status']=='matched' and result['authorized'] is False:
                            from worker.visual_client import encode_jpeg
                            result['_capture_jpeg'] = encode_jpeg(frame, max_side=1600)
                self.status = 'ready' if result['status'] == 'matched' else 'face_or_authorization_unverified'
            except Exception:
                self.status = 'model_or_database_unavailable'
            finally:
                if not self.done.full():
                    self.done.put_nowait((generation, result))
                self.jobs.task_done()
