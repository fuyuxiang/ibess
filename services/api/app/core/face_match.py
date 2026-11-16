"""YuNet + SFace CPU 推理。分数为余弦相似度，不是概率。"""
import hashlib
import io
import os
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 12_000_000
MAX_SIDE = 4096
MODEL_FILES = {
    'face_detection_yunet_2023mar.onnx': '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4',
    'face_recognition_sface_2021dec.onnx': '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79',
}
MODEL_VERSION = 'yunet-sface:' + ':'.join(x[:16] for x in MODEL_FILES.values())


class FaceMatchError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class FaceSample:
    embedding: np.ndarray
    photo: bytes


def decode_image(data):
    if len(data) > MAX_FILE_BYTES:
        raise FaceMatchError(413, '图片不能超过 5 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ('JPEG', 'PNG'):
                    raise FaceMatchError(422, '请上传 JPG 或 PNG 图片')
                w, h = source.size
                if w * h > MAX_PIXELS or max(w, h) > MAX_SIDE:
                    raise FaceMatchError(422, '图片尺寸过大：最长边不超过 4096 像素，总像素不超过 1200 万')
                if min(w, h) < 80:
                    raise FaceMatchError(422, '图片太小，请使用清晰正面照片')
                source.load()
                rgb = np.asarray(ImageOps.exif_transpose(source).convert('RGB'))
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except FaceMatchError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise FaceMatchError(422, '图片无法读取，请重新上传完整的 JPG 或 PNG') from None


class FaceEngine:
    def __init__(self, model_dir=None):
        directory = Path(model_dir or os.environ.get('FACE_MODEL_DIR', '/face-models'))
        try:
            for filename, expected in MODEL_FILES.items():
                if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != expected:
                    raise ValueError('model hash mismatch')
            cv2.setNumThreads(2)
            self.detector = cv2.FaceDetectorYN.create(str(directory / next(iter(MODEL_FILES))), '', (320, 320), 0.85, 0.3, 5000)
            self.recognizer = cv2.FaceRecognizerSF.create(str(directory / 'face_recognition_sface_2021dec.onnx'), '')
            self.threshold = float(os.environ.get('FACE_MATCH_THRESHOLD', '0.363'))
            if not np.isfinite(self.threshold) or not -1 <= self.threshold <= 1:
                raise ValueError('invalid threshold')
        except (OSError, ValueError, cv2.error):
            raise FaceMatchError(503, '人脸模型未就绪，请检查模型文件与配置') from None
        self.model = MODEL_VERSION
        self.lock = threading.Lock()

    def extract(self, data):
        image = decode_image(data)
        h, w = image.shape[:2]
        scale = min(1.0, 1280 / max(w, h))
        working = cv2.resize(image, (round(w * scale), round(h * scale))) if scale < 1 else image
        # OpenCV 的模型实例有内部可变状态，同一实例不得并发推理。
        with self.lock:
            try:
                self.detector.setInputSize((working.shape[1], working.shape[0]))
                _, faces = self.detector.detect(working)
                if faces is None or len(faces) == 0:
                    raise FaceMatchError(422, '未检测到清晰人脸，请正对镜头并保证光线充足')
                if len(faces) != 1:
                    raise FaceMatchError(422, '检测到多张人脸，请仅保留待匹配人员')
                face = faces[0].copy()
                face[:14] /= scale
                if min(face[2], face[3]) < 80:
                    raise FaceMatchError(422, '人脸太小，请靠近镜头；人脸宽高至少 80 像素')
                aligned = self.recognizer.alignCrop(image, face)
                gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                if cv2.Laplacian(gray, cv2.CV_64F).var() < 20:
                    raise FaceMatchError(422, '人脸图片模糊，请重新拍摄清晰照片')
                feature = self.recognizer.feature(aligned).reshape(-1).astype(np.float32)
                norm = np.linalg.norm(feature)
                if feature.size != 128 or not np.isfinite(feature).all() or norm < 1e-8:
                    raise FaceMatchError(422, '无法提取有效人脸特征，请重新拍摄')
                ok, jpeg = cv2.imencode('.jpg', aligned, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if not ok:
                    raise FaceMatchError(422, '人脸照片处理失败')
                return FaceSample(feature / norm, jpeg.tobytes())
            except cv2.error:
                raise FaceMatchError(422, '人脸图片无法处理，请更换清晰正面照片') from None

    def compare(self, reference, probe):
        a, b = np.asarray(reference, dtype=np.float32), np.asarray(probe, dtype=np.float32)
        if a.shape != (128,) or b.shape != (128,) or not np.isfinite(a).all() or not np.isfinite(b).all():
            raise FaceMatchError(409, '登记特征无效，请重新登记人脸')
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        if norm < 1e-8:
            raise FaceMatchError(409, '登记特征无效，请重新登记人脸')
        return float(np.clip(np.dot(a, b) / norm, -1, 1))


_engine = None
_init_lock = threading.Lock()


def get_face_engine():
    global _engine
    with _init_lock:
        if _engine is None:
            _engine = FaceEngine()
        return _engine
