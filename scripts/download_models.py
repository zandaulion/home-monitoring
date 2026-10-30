"""Download upstream models into untracked runtime storage."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / ".runtime" / "models"
MODELS = {
    "yunet.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "sface.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "yolox_nano.onnx": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx",
}
SHA256 = {
    "yunet.onnx": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "sface.onnx": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    "yolox_nano.onnx": "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
}

if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = {}
    for name, url in MODELS.items():
        path = ROOT / name
        if not path.exists():
            temporary = path.with_suffix(".download")
            with urllib.request.urlopen(url, timeout=90) as response:
                temporary.write_bytes(response.read())
            if temporary.stat().st_size < 100_000:
                raise RuntimeError(f"Invalid model download: {name}")
            temporary.replace(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != SHA256[name]:
            raise RuntimeError(f"Model checksum mismatch: {name}")
        manifest[name] = {"source": url, "sha256": digest, "bytes": path.stat().st_size}
        print(name, manifest[name]["bytes"], "bytes")
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
