"""Camera collector. Only Pillow and the Python standard library required.

Credentials live in an owner-readable config outside source control. Camera
snapshots are held in RAM; the cameras retain their own continuous recordings.
"""
import argparse
import concurrent.futures
import io
import json
import logging
import signal
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageChops

LOG = logging.getLogger("collector")
STOP = threading.Event()


def motion_fraction(previous, current):
    diff = ImageChops.difference(previous, current)
    histogram = diff.histogram()
    return sum(histogram[13:]) / (current.width * current.height)


def run_camera(camera, config):
    camera_id = camera["id"]
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, camera["snapshot_url"], camera["username"], camera["password"])
    camera_client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPDigestAuthHandler(manager), urllib.request.HTTPBasicAuthHandler(manager))
    upstream = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = config["server_url"].rstrip("/") + "/api/ingest/" + camera_id
    previous = None
    sent_at = heartbeat_at = active_until = 0.0
    failures = 0
    enabled = True

    def send(path, body, content_type):
        req = urllib.request.Request(base + path, data=body, headers={"Authorization": "Bearer " + config["collector_token"], "Content-Type": content_type})
        with upstream.open(req, timeout=12) as response:
            return json.loads(response.read(65536))

    while not STOP.is_set():
        started = time.monotonic()
        error = None
        try:
            if started - heartbeat_at >= 15:
                result = send("/heartbeat", b"{}", "application/json")
                enabled = result.get("monitoring_enabled", True)
                heartbeat_at = started
            if enabled:
                with camera_client.open(camera["snapshot_url"], timeout=6) as response:
                    data = response.read(8_000_001)
                if len(data) > 8_000_000:
                    raise ValueError("Snapshot too large")
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > 20_000_000:
                        raise ValueError("Snapshot dimensions too large")
                    image = image.convert("L").resize((96, 54))
                    image = image.crop((0, 5, 96, 49))
                    current = image.copy()
                if previous is None or motion_fraction(previous, current) >= config.get("motion_fraction", 0.015):
                    active_until = started + 12
                previous = current
                if started < active_until or started - sent_at >= 15:
                    send("/frame", data, "image/jpeg")
                    sent_at = started
            else:
                previous = None
            if failures:
                LOG.info("%s connection restored", camera_id)
            failures = 0
        except urllib.error.HTTPError as exception:
            failures += 1
            # Never log exception strings: they can contain credential-bearing URLs.
            error = "camera_auth" if exception.code in {401, 403} and exception.url.startswith(camera["snapshot_url"]) else "capture_failed"
            if failures == 1 or failures % 10 == 0:
                LOG.warning("%s request failed (HTTP %s)", camera_id, exception.code)
        except Exception:
            failures += 1
            error = "camera_unreachable"
            if failures == 1 or failures % 10 == 0:
                LOG.warning("%s temporarily unreachable", camera_id)
        if error:
            try:
                send("/heartbeat", json.dumps({"error": error}).encode(), "application/json")
            except Exception:
                pass
        interval = config.get("interval_seconds", 2) if not failures else min(30, 2 ** min(failures, 5))
        STOP.wait(max(0.1, interval - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config) as file:
        config = json.load(file)
    if not config["server_url"].startswith("https://"):
        raise SystemExit("The upstream server must use HTTPS")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    LOG.info("Starting %s cameras; continuous video stays on camera cards", len(config["cameras"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(config["cameras"])) as pool:
        futures = [pool.submit(run_camera, camera, config) for camera in config["cameras"]]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
