"""CPU-only person detection and face embeddings. No external inference API."""
from pathlib import Path
import threading
import time

import cv2
import numpy as np
import onnxruntime as ort


def choose_identity(embedding, samples, threshold=0.50, margin=0.08):
    """A similarity is not a probability. Require separation from other people."""
    scores = {}
    for sample in samples:
        vector = np.asarray(sample["embedding"], dtype=np.float32)
        score = float(np.dot(embedding, vector))
        scores[sample["person_id"]] = max(scores.get(sample["person_id"], -1.0), score)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return None, None
    winner, score = ranked[0]
    separated = len(ranked) == 1 or score - ranked[1][1] >= margin
    return (winner if score >= threshold and separated else None), round(score, 4)


def photo_score(frame, people, faces=()):
    """Prefer a large, sharp view of the person; a visible face outranks that."""
    boxes = [face["box"] for face in faces] or [person["box"] for person in people]
    if not boxes:
        return 0.0
    h, w = frame.shape[:2]
    x, y, bw, bh = max(boxes, key=lambda box: box[2] * box[3])
    crop = frame[max(0, y):y + bh, max(0, x):x + bw]
    if not crop.size:
        return 0.0
    sharpness = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
    return round(bw * bh / (w * h) * float(np.log1p(sharpness)) + (10 if faces else 0), 4)


class Vision:
    def __init__(self, model_dir: Path):
        cv2.setNumThreads(1)
        self.lock = threading.Lock()
        self.detector = cv2.FaceDetectorYN.create(str(model_dir / "yunet.onnx"), "", (320, 320), 0.85, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(model_dir / "sface.onnx"), "")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.person_model = ort.InferenceSession(str(model_dir / "yolox_nano.onnx"), options, providers=["CPUExecutionProvider"])
        self.input_name = self.person_model.get_inputs()[0].name
        self.size = int(self.person_model.get_inputs()[0].shape[-1])
        grids, strides = [], []
        for stride in (8, 16, 32):
            y, x = np.meshgrid(np.arange(self.size // stride), np.arange(self.size // stride), indexing="ij")
            grids.append(np.stack((x, y), axis=2).reshape(-1, 2))
            strides.append(np.full(((self.size // stride) ** 2, 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    @staticmethod
    def decode(data: bytes):
        if not data or len(data) > 8_000_000:
            raise ValueError("Choose a JPEG or PNG image smaller than 8 MB.")
        # Inspect compressed dimensions before asking the decoder to allocate pixels.
        from PIL import Image
        import io
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError("Image dimensions are too large.")
                if image.format not in {"JPEG", "PNG"}:
                    raise ValueError("Choose a JPEG or PNG image.")
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError("The image could not be read.") from error
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("The image could not be decoded.")
        if max(frame.shape[:2]) > 1920:
            frame = cv2.resize(frame, None, fx=1920 / max(frame.shape[:2]), fy=1920 / max(frame.shape[:2]))
        return frame

    @staticmethod
    def jpeg(frame):
        ok, result = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise ValueError("Could not encode image")
        return result.tobytes()

    def _people(self, frame):
        h, w = frame.shape[:2]
        ratio = min(self.size / h, self.size / w)
        resized = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
        padded = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        padded[:resized.shape[0], :resized.shape[1]] = resized
        tensor = padded.transpose(2, 0, 1).astype(np.float32)[None]
        output = self.person_model.run(None, {self.input_name: tensor})[0][0].copy()
        # Upstream nano ONNX exports raw predictions (decode_in_inference=False).
        output[:, :2] = (output[:, :2] + self.grid) * self.strides
        output[:, 2:4] = np.exp(np.clip(output[:, 2:4], -10, 10)) * self.strides
        scores = output[:, 4] * output[:, 5]  # COCO class 0 is person.
        selected = scores >= 0.45
        output, scores = output[selected], scores[selected]
        boxes = []
        for row in output:
            x, y, bw, bh = row[:4] / ratio
            x1, y1 = max(0, float(x - bw / 2)), max(0, float(y - bh / 2))
            x2, y2 = min(w, float(x + bw / 2)), min(h, float(y + bh / 2))
            boxes.append([x1, y1, max(0, x2 - x1), max(0, y2 - y1)])
        indices = cv2.dnn.NMSBoxes(boxes, scores.tolist(), 0.45, 0.45)
        return [{"box": [round(v) for v in boxes[int(i)]], "score": round(float(scores[int(i)]), 3)} for i in np.asarray(indices).flatten()][:12]

    def _faces(self, frame):
        h, w = frame.shape[:2]
        ratio = min(1.0, 960 / max(h, w))
        small = cv2.resize(frame, None, fx=ratio, fy=ratio) if ratio < 1 else frame
        self.detector.setInputSize((small.shape[1], small.shape[0]))
        _, faces = self.detector.detect(small)
        results = []
        if faces is None:
            return results
        for face in faces[:12]:
            face = face.copy()
            face[:14] /= ratio
            x, y, fw, fh = face[:4]
            x1, y1 = max(0, int(x)), max(0, int(y))
            x2, y2 = min(w, int(x + fw)), min(h, int(y + fh))
            if x2 <= x1 or y2 <= y1:
                continue
            crop = frame[y1:y2, x1:x2]
            blur = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            usable = min(fw, fh) >= 45 and blur >= 35
            feature = None
            if usable:
                aligned = self.recognizer.alignCrop(frame, face)
                feature = self.recognizer.feature(aligned).flatten()
                feature = feature / max(float(np.linalg.norm(feature)), 1e-12)
            results.append({"box": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)], "usable": usable, "embedding": feature, "crop": self.jpeg(crop)})
        return results

    def analyze(self, frame, samples, threshold=0.50, find_faces=True):
        start = time.monotonic()
        with self.lock:
            people = self._people(frame)
            faces = self._faces(frame) if find_faces else []
            for face in faces:
                face["person_id"], face["similarity"] = choose_identity(face["embedding"], samples, threshold) if face["usable"] else (None, None)
        return {"people": people, "faces": faces, "elapsed_ms": round((time.monotonic() - start) * 1000)}

    def enroll(self, data):
        frame = self.decode(data)
        with self.lock:
            faces = self._faces(frame)
        if len(faces) != 1:
            raise ValueError("Choose an image with exactly one visible face.")
        if not faces[0]["usable"]:
            raise ValueError("The face is too small or blurred. Choose a closer, clearer image.")
        return faces[0]
