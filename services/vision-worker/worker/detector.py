"""YOLO 人员检测封装。"""


class PersonDetector:
    def __init__(self, model_path, conf=0.25, imgsz=640, device="cuda"):
        import os
        import torch
        from ultralytics import YOLO
        torch.set_num_threads(int(os.getenv("TORCH_THREADS", "1")))
        self.model = YOLO(model_path)
        self.conf = conf
        self.imgsz = imgsz
        self.device = device

    def scan_phones(self, crop, imgsz=640, conf=0.12):
        """对人身裁剪图单独跑一次手机检测。手机在整帧里只有十几个像素，
        整帧推理必然漏；裁到人身上再放大，召回能从 0 提到可用水平。"""
        results = self.model.predict(
            crop, conf=conf, imgsz=imgsz, device=self.device, verbose=False
        )
        phones = []
        if results and len(results[0].boxes):
            names = results[0].names
            for b in results[0].boxes:
                if names[int(b.cls)] != "cell phone":
                    continue
                x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
                phones.append({"class": "cell phone", "conf": float(b.conf), "bbox": [x1, y1, x2, y2]})
        return phones

    def detect(self, frame):
        """返回 (persons, phones)：person 计数用于既有链路；phone 框供 S02/S08 几何判定。"""
        results = self.model.predict(
            frame, conf=self.conf, imgsz=self.imgsz, device=self.device, verbose=False
        )
        persons, phones = [], []
        if results and len(results[0].boxes):
            names = results[0].names
            for b in results[0].boxes:
                name = names[int(b.cls)]
                if name not in ("person", "cell phone"):
                    continue
                x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
                item = {"class": name, "conf": float(b.conf), "bbox": [x1, y1, x2, y2]}
                (persons if name == "person" else phones).append(item)
        return persons, phones
