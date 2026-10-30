"""Isolated loopback demo: generated artwork, fake inference, no live config."""
import argparse
from contextlib import contextmanager
import io
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
from home_monitor.app import create_app
from home_monitor.store import write_image


class DemoVision:
    """Never initialize inference models or contact cameras."""

    def enroll(self, data):
        raise ValueError("Use a real installation to enrol faces; this is a synthetic preview.")

    def decode(self, data):
        raise ValueError("Camera ingestion is disabled in the synthetic preview.")


def artwork(index=0, portrait=False):
    """Draw invented scenes and faceless avatars; no external image inputs."""
    if portrait:
        image = Image.new("RGB", (180, 180), (225, 231, 216))
        d = ImageDraw.Draw(image)
        d.ellipse((60, 27, 120, 87), fill=(109, 133, 108))
        d.rounded_rectangle((33, 100, 147, 190), 45, fill=(109, 133, 108))
    else:
        image = Image.new("RGB", (960, 540), (210, 224, 218))
        d = ImageDraw.Draw(image)
        d.rectangle((0, 275, 960, 540), fill=(141, 162, 125))
        d.polygon([(0, 540), (385, 305), (590, 305), (960, 540)], fill=(205, 199, 176))
        d.rectangle((265, 125, 700, 340), fill=(232, 222, 199))
        d.polygon([(225, 150), (475, 30), (740, 150)], fill=(79, 106, 94))
        d.rectangle((455, 205, 535, 340), fill=(88, 117, 103))
        for x in (305, 590):
            d.rectangle((x, 193, x+65, 255), fill=(153, 186, 182))
            d.line((x+32, 193, x+32, 255), fill=(238, 231, 207), width=5)
        for x in (90, 820):
            d.rectangle((x, 185, x+20, 365), fill=(111, 126, 93))
            d.ellipse((x-70, 75, x+90, 245), fill=(104, 139, 102))
        x = 350 + index * 55
        d.ellipse((x, 280, x+35, 315), fill=(70, 93, 83))
        d.rounded_rectangle((x-8, 320, x+43, 405), 18, fill=(70, 93, 83))
        d.line((x+4, 395, x-2, 440), fill=(70, 93, 83), width=11)
        d.line((x+29, 395, x+35, 440), fill=(70, 93, 83), width=11)
        d.rectangle((x-18, 270, x+55, 450), outline=(225, 238, 206), width=3)
        d.rectangle((20, 20, 360, 52), fill=(43, 68, 55))
        d.text((31, 31), "SYNTHETIC PREVIEW / GENERATED ARTWORK", fill="white")
        d.text((24, 511), "DEMO CAMERA / NO REAL LOCATION OR PERSON", fill=(34, 58, 42))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


@contextmanager
def demo_app(port=8870, now=None):
    """Own all storage for this process; delete it when the demo exits."""
    with tempfile.TemporaryDirectory(prefix="home-monitor-demo-") as directory:
        now = time.time() if now is None else now
        root = Path(directory)
        origin = f"http://127.0.0.1:{port}"
        cameras = [{"id": f"camera-{i+1}", "name": name} for i, name in enumerate(("Demo entrance", "Demo garden", "Demo driveway"))]
        app = create_app({
            "data_dir": str(root / "data"), "model_dir": str(root / "unused-models"),
            "cameras": cameras, "owner_login": "demo@example.invalid",
            "collector_token": "synthetic-demo-collector", "console_admin_token": "synthetic-demo-console",
            "public_base_url": origin, "allowed_origins": [origin],
        }, vision=DemoVision(), push_sender=lambda *_: 201)
        store = app.state.store
        for i in range(2):
            person_id = f"{i+1:032x}"
            store.execute("INSERT INTO people VALUES(?,?,?)", (person_id, f"Demo person {i+1}", now-86400))
            store.save_sample(person_id, [1.0, 0.0] if i == 0 else [0.0, 1.0], artwork(portrait=True))
        for i, camera in enumerate(cameras):
            picture = artwork(i)
            write_image(store.root / "latest" / (camera["id"]+".jpg"), picture)
            store.execute("UPDATE cameras SET last_seen=?,width=960,height=540,inference_ms=35 WHERE id=?", (now, camera["id"]))
            for j in reversed(range(8)):
                stamp = now - (j*1800+i*90+120)
                state = ("known", "unknown", "unavailable")[(i+j) % 3]
                face = {"box": [350, 280, 35, 35], "usable": state != "unavailable", "person_id": f"{i%2+1:032x}" if state == "known" else None, "similarity": 0.74 if state == "known" else None}
                store.record_event(camera["id"], state, 1, [face], picture, [artwork(portrait=True)], now=stamp)
            store.execute("INSERT INTO camera_periods(camera_id,state,start,end) VALUES(?,?,?,?)", (camera["id"], "online", now-5*3600, now))
        # This is a separate app instance. Production authentication is untouched.
        @app.middleware("http")
        async def synthetic_identity(request, call_next):
            if request.url.hostname != "127.0.0.1":
                from fastapi.responses import Response
                return Response("Demo is available only on 127.0.0.1", status_code=400)
            if request.url.path.startswith(("/api/ingest", "/api/admin", "/api/push", "/api/invites")) and request.method != "GET":
                from fastapi.responses import Response
                return Response("Disabled in the synthetic preview", status_code=403)
            request.scope["headers"] = [(key, value) for key, value in request.scope["headers"] if key != b"tailscale-user-login"] + [(b"tailscale-user-login", b"demo@example.invalid")]
            response = await call_next(request)
            response.headers["X-Home-Monitor-Demo"] = "synthetic"
            return response
        try:
            yield app
        finally:
            store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8870)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Choose a valid port")
    import uvicorn
    print(f"Synthetic preview: http://127.0.0.1:{args.port} — temporary data; no camera connection", flush=True)
    with demo_app(args.port) as app:
        uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
