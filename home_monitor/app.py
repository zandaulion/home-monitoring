import asyncio
from contextlib import asynccontextmanager
import hmac
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Literal
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import cv2
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .store import MAX_PHOTOS, Store, write_image
from .vision import ObjectTracker, Vision, overlap, photo_score
from .access import Access, COOKIE, MAX_AGE, TTL_DAYS
from .push import ALERTING_STATES, Push, alert_state

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).parent / "static"
LOG = logging.getLogger("home_monitor")
MAX_CLIP_SECONDS = 300
CONFIDENT_PERSON_SCORE = 0.65
SAME_PLACE_OVERLAP = 0.7
SECOND_SIGHTING_SECONDS = 8
RECORDING_WINDOW_SECONDS = 600


class PersonInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CameraInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class SettingsInput(BaseModel):
    retention_days: int = Field(ge=1, le=90)
    storage_limit_mb: int = Field(ge=50, le=10240)
    recognition_threshold: float = Field(ge=0.45, le=0.85)
    monitoring_enabled: bool
    alert_cooldown_minutes: int = Field(default=5, ge=1, le=60)
    recognition_enabled: bool = True
    alert_mode: Literal["off", "schedule", "always"] = "always"
    alert_schedule_start: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    alert_schedule_end: str = Field(default="06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    alert_timezone: str = Field(default="UTC", max_length=64)
    auto_away: bool = False
    offline_alerts: bool = True
    alert_vehicles: bool = False
    alert_animals: bool = False

    @field_validator("alert_timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError) as error:
            raise ValueError("Unknown time zone") from error
        return value


class Assignment(BaseModel):
    person_id: str
    face_index: int = Field(ge=0, le=11)
    add_sample: bool = True
    image_at: float | None = None


def create_app(config=None, vision=None, push_sender=None):
    if config is None:
        config = json.loads(Path(os.environ.get("HOME_MONITOR_CONFIG", ROOT / ".runtime/config.json")).read_text())
    store = Store(Path(config["data_dir"]), config["cameras"])
    accounts = Access(store, config.get("public_base_url", config.get("allowed_origins", ["https://monitor.example.invalid"])[0]))
    push = Push(store, config.get("public_base_url", config.get("allowed_origins", ["https://monitor.example.invalid"])[0]), sender=push_sender)
    engine = vision or Vision(Path(config["model_dir"]))
    ingest_lock = threading.Lock()
    latest_processed = {}
    votes = {}
    unidentified_observations = {}
    tracker = ObjectTracker()
    recorder_seen = {}
    recorder_polled = {}
    storage_bytes = store.cleanup()

    async def cleanup_loop():
        nonlocal storage_bytes
        while True:
            await asyncio.sleep(60)
            try:
                storage_bytes = await run_in_threadpool(store.cleanup)
            except Exception:
                LOG.exception("Retention cleanup failed")
            try:
                await run_in_threadpool(push.check_cameras)
            except Exception:
                LOG.warning("Camera health check temporarily unavailable")
            try:
                await run_in_threadpool(push.check_recorders)
            except Exception:
                LOG.warning("Recorder health check temporarily unavailable")

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(cleanup_loop())
        async def push_loop():
            while True:
                try:
                    worked = await run_in_threadpool(push.deliver_one)
                except Exception:
                    # Subscription endpoints and encryption keys must not enter logs.
                    LOG.warning("Push delivery temporarily unavailable")
                    worked = False
                await asyncio.sleep(0.1 if worked else 2)
        delivery = asyncio.create_task(push_loop())
        yield
        task.cancel()
        delivery.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            await delivery
        except asyncio.CancelledError:
            pass
        store.db.close()

    app = FastAPI(title="Home Monitor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.engine = engine
    app.state.accounts = accounts
    app.state.push = push
    build = hashlib.sha256()
    for file in sorted(STATIC.rglob("*")):
        if file.is_file():
            build.update(str(file.relative_to(STATIC)).encode())
            build.update(file.read_bytes())
    build_version = build.hexdigest()[:12]
    shell_paths = {"/", "/manifest.webmanifest", "/sw.js", "/sw-update.js", "/pwa-update.js", "/bust", "/assets/bust.js", "/assets/app.js", "/assets/timeline.js", "/assets/style.css", "/api/session", "/api/invites/redeem"}
    icon_names = {"icon.svg", "icon-192.png", "icon-512.png", "icon-maskable.png", "apple-touch-icon.png"}
    shell_paths.update("/icons/"+name for name in icon_names)
    camera_ids = {c["id"] for c in config["cameras"]}
    for camera_id in camera_ids:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", camera_id):
            raise ValueError("Invalid camera identifier")

    @app.middleware("http")
    async def access(request: Request, call_next):
        is_ingest = request.url.path.startswith("/api/ingest/")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        peer = request.client.host if request.client else ""
        local = peer in {"127.0.0.1", "::1", "testclient"}
        public_host = urlsplit(config.get("public_origin", "")).hostname
        public_request = bool(public_host and request.url.hostname == public_host)
        owner = local and not public_request and request.headers.get("tailscale-user-login", "") == config["owner_login"]
        admin = config.get("admin_token", "")
        diagnostic = bool(local and not public_request and admin and token and hmac.compare_digest(token, admin))
        cookie = request.cookies.get(COOKIE)
        device = accounts.device(cookie) if cookie else None
        request.state.authorized = bool(device or (not cookie and (owner or diagnostic)))
        request.state.owner = bool(owner or diagnostic)
        request.state.device = device
        if public_request and (is_ingest or request.url.path == "/api/ingest" or request.url.path == "/api/admin" or request.url.path.startswith("/api/admin/")):
            return JSONResponse({"detail": "Not found"}, status_code=404, headers={"Cache-Control": "no-store"})
        if request.url.path == "/api/admin" or request.url.path.startswith("/api/admin/"):
            expected = config.get("console_admin_token", "")
            received = request.headers.get("x-admin-token", "")
            permitted = bool(local and expected and received and hmac.compare_digest(received, expected))
            if not permitted:
                return JSONResponse({"detail": "Not found"}, status_code=404, headers={"Cache-Control": "no-store"})
        elif is_ingest:
            permitted = bool(token and hmac.compare_digest(token, config["collector_token"]))
        elif request.url.path in shell_paths and request.method in {"GET", "HEAD"}:
            permitted = True
        elif request.url.path == "/api/invites/redeem" and request.method == "POST":
            permitted = True
        else:
            permitted = request.state.authorized
        if not permitted:
            return JSONResponse({"detail": "Activate this device with an invitation from your administrator."}, status_code=401, headers={"Cache-Control": "no-store"})
        # Same-origin check supplements Tailscale authentication on browser writes.
        if not is_ingest and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in config.get("allowed_origins", []):
                return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, status_code=400)
        limit = 64_000_000 if is_ingest and request.url.path.endswith('/video') else 9_000_000
        if length > limit:
            return JSONResponse({"detail": "Image is too large"}, status_code=413)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'; media-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; worker-src 'self'; manifest-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def valid_id(value):
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise HTTPException(404, "Not found")
        return value

    def file_response(path):
        if not path.is_file():
            raise HTTPException(404, "Image not available yet")
        return FileResponse(path, media_type="image/jpeg")

    async def image_bytes(request):
        # Bound chunked as well as Content-Length requests before buffering.
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8_000_000:
                raise HTTPException(413, "Image is too large")
        return bytes(data)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/assets/{name}")
    def static(name: str):
        if name not in {"app.js", "timeline.js", "style.css", "bust.js"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icons/{name}")
    def icon(name: str):
        if name not in icon_names:
            raise HTTPException(404)
        return FileResponse(STATIC / "icons" / name, media_type="image/svg+xml" if name.endswith(".svg") else "image/png")

    @app.get("/sw.js")
    def worker():
        body = (STATIC / "sw.js").read_text().replace("__BUILD_VERSION__", build_version)
        return Response(body, media_type="text/javascript", headers={"Service-Worker-Allowed": "/"})

    @app.get("/sw-update.js")
    def worker_updates():
        return FileResponse(STATIC / "sw-update.js", media_type="text/javascript")

    @app.get("/pwa-update.js")
    def page_updates():
        return FileResponse(STATIC / "pwa-update.js", media_type="text/javascript")

    @app.get("/bust")
    def bust():
        return FileResponse(STATIC / "bust.html", headers={"Clear-Site-Data": '"cache"'})

    @app.get("/api/session")
    def session(request: Request, response: Response):
        # Opening the app renews its access, so only unused devices expire.
        if request.state.device:
            response.set_cookie(COOKIE, request.cookies[COOKIE], max_age=MAX_AGE, httponly=True, secure=True, samesite="strict", path="/")
        return {"authorized": request.state.authorized, "owner": request.state.owner, "device": {"id": request.state.device["id"], "label": request.state.device["label"]} if request.state.device else None}

    class Redemption(BaseModel):
        code: str = Field(min_length=1, max_length=40)

    @app.post("/api/invites/redeem")
    def redeem(payload: Redemption, response: Response):
        try:
            device_id, value = accounts.redeem(payload.code)
        except OverflowError as error:
            raise HTTPException(429, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        response.set_cookie(COOKIE, value, max_age=MAX_AGE, httponly=True, secure=True, samesite="strict", path="/")
        return {"ok": True, "device_id": device_id}

    class LabelInput(BaseModel):
        label: str = Field(default="", max_length=60)

    class RevokeInput(BaseModel):
        revoked: bool = True

    @app.get("/api/admin/devices")
    def devices():
        return {"devices": accounts.devices()}

    @app.get("/api/admin/invites")
    def invites():
        return {"invites": accounts.invites(), "ttl_days": TTL_DAYS}

    @app.post("/api/admin/invites")
    def create_invite(payload: LabelInput):
        return accounts.create_invite(payload.label.strip())

    @app.post("/api/admin/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: int):
        result = store.execute("UPDATE access_invites SET revoked=1,code_plain=NULL WHERE id=?", (invite_id,))
        if not result.rowcount:
            raise HTTPException(404, "Invitation not found")
        return {"revoked": invite_id}

    @app.post("/api/admin/devices/{device_id}/revoke")
    def revoke_device(device_id: int, payload: RevokeInput):
        result = store.execute("UPDATE access_devices SET revoked=? WHERE id=?", (payload.revoked, device_id))
        if not result.rowcount:
            raise HTTPException(404, "Device not found")
        if payload.revoked:
            push.unsubscribe(device_id)
        return {"id": device_id}

    @app.post("/api/admin/devices/{device_id}/label")
    def label_device(device_id: int, payload: LabelInput):
        result = store.execute("UPDATE access_devices SET label=? WHERE id=?", (payload.label.strip(), device_id))
        if not result.rowcount:
            raise HTTPException(404, "Device not found")
        return {"id": device_id, "label": payload.label.strip()}

    @app.delete("/api/admin/devices/{device_id}")
    def delete_device(device_id: int):
        result = store.execute("DELETE FROM access_devices WHERE id=?", (device_id,))
        if not result.rowcount:
            raise HTTPException(404, "Device not found")
        return {"deleted": device_id}

    @app.get("/api/push/status")
    def push_status(request: Request):
        device = request.state.device
        return push.status(device["id"] if device else None)

    @app.post("/api/push/device")
    def push_device(request: Request, response: Response):
        if request.state.device:
            return {"device_id": request.state.device["id"]}
        if not request.state.owner:
            raise HTTPException(403, "Activate this device first")
        device_id, value = accounts.owner_device()
        response.set_cookie(COOKIE, value, max_age=MAX_AGE, httponly=True, secure=True, samesite="strict", path="/")
        return {"device_id": device_id}

    @app.post("/api/push/subscription")
    async def subscribe(request: Request):
        if not request.state.device:
            raise HTTPException(403, "Register this device before enabling notifications")
        data = await image_bytes(request)
        if len(data) > 6000:
            raise HTTPException(413, "Subscription is too large")
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("Invalid subscription")
            push.subscribe(request.state.device["id"], payload)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error
        return {"ok": True}

    @app.delete("/api/push/subscription")
    def unsubscribe(request: Request):
        if request.state.device:
            push.unsubscribe(request.state.device["id"])
        return {"ok": True}

    @app.post("/api/push/test")
    def test_push(request: Request):
        if not request.state.device:
            raise HTTPException(403, "Enable notifications on this device first")
        try:
            push.test(request.state.device["id"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"queued": True}

    def recording_health(now):
        rows, problems = push.recording_problems(now)
        return {"configured": bool(rows), "stopped": [row["name"] for row in problems]}

    @app.get("/api/status")
    def status():
        settings = store.settings()
        now = time.time()
        cameras = store.rows("SELECT * FROM cameras ORDER BY id")
        for camera in cameras:
            camera["online"] = bool(camera["last_seen"] and now - camera["last_seen"] < 100 and not camera["error"])
            image = store.root / "latest" / f"{camera['id']}.jpg"
            camera["image_at"] = image.stat().st_mtime if image.exists() else None
        midnight = now - 86400
        counts = store.rows("SELECT state,count(*) AS n FROM events WHERE first_seen>? GROUP BY state", (midnight,))
        disk = shutil.disk_usage(store.root)
        return {"cameras": cameras, "settings": settings, "people": store.people(), "last_24h": {r["state"]: r["n"] for r in counts}, "storage_bytes": storage_bytes, "disk_free_bytes": disk.free, "server_time": now, "recognition_ready": bool(store.samples()), "recorder": recording_health(now), "alerts": alert_state(settings, now), "recognition_model": "SFace", "sampling_seconds": config.get("sampling_seconds", 2), "private_access": "Tailscale", "recording": "Camera SD cards"}

    @app.get("/api/events")
    def events(camera: str | None = None, state: str | None = None, before: float | None = None, limit: int = 60):
        return store.events(camera, state, before, max(1, min(limit, 100)))

    @app.get("/api/timeline")
    def timeline(start: float = Query(ge=0, allow_inf_nan=False), end: float = Query(ge=0, allow_inf_nan=False), camera: str | None = None, state: str | None = None):
        if not 0 < end - start <= 25 * 3600:
            raise HTTPException(422, "Choose a period of up to one day")
        if camera and camera not in camera_ids:
            raise HTTPException(422, "Unknown camera")
        if state and state not in {"person", "vehicle", "animal", "known", "unknown", "unavailable"}:
            raise HTTPException(422, "Unknown recognition filter")
        clauses, params = ["last_seen>=?", "first_seen<?"], [start, end]
        if camera:
            clauses.append("camera_id=?")
            params.append(camera)
        if state:
            clauses.append("state=?")
            params.append(state)
        with store.lock:
            # Compact metadata for the whole day, independent of snapshot pagination.
            rows = store.rows("SELECT id,camera_id,first_seen,last_seen,state,person_count,image_at FROM events WHERE " + " AND ".join(clauses) + " ORDER BY last_seen DESC,id", params)
            notifications = store.rows("SELECT event_id,MIN(sent_at) AS at FROM push_jobs WHERE state='sent' AND sent_at>=? AND sent_at<? AND event_id IS NOT NULL GROUP BY event_id", (start, end))
            coverage = store.rows("SELECT camera_id,state,start,end FROM camera_periods WHERE end>? AND start<?", (start, end))
            snoozes = store.rows("SELECT start,end FROM alert_snoozes WHERE end>? AND start<?", (start, end))
        ids = {row["id"] for row in rows}
        now = time.time()
        return {"start": start, "end": end, "events": rows, "notifications": [row for row in notifications if row["event_id"] in ids], "coverage": [{**row, "end": min(row["end"], now)} for row in coverage if row["start"] <= now and (not camera or row["camera_id"] == camera)], "snoozes": snoozes}

    @app.get("/api/timeline/events")
    def timeline_events(ids: str = Query(max_length=2000)):
        selected = ids.split(",")
        if len(selected) > 60 or any(not re.fullmatch(r"[a-f0-9]{32}", item) for item in selected):
            raise HTTPException(422, "Choose up to 60 events")
        return store.event_details(selected)

    @app.get("/api/cameras/{camera_id}/image")
    def camera_image(camera_id: str):
        if camera_id not in camera_ids:
            raise HTTPException(404)
        return file_response(store.root / "latest" / f"{camera_id}.jpg")

    @app.patch("/api/cameras/{camera_id}")
    def rename(camera_id: str, payload: CameraInput):
        if camera_id not in camera_ids:
            raise HTTPException(404)
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "Enter a camera name")
        store.execute("UPDATE cameras SET name=? WHERE id=?", (name, camera_id))
        return {"ok": True}

    @app.get("/api/events/{event_id}/image")
    def event_image(event_id: str):
        return file_response(store.event_image(valid_id(event_id)))

    @app.get("/api/events/{event_id}/video")
    def event_video(event_id: str):
        path = store.root / "events" / valid_id(event_id) / "alarm.mp4"
        if not path.is_file():
            raise HTTPException(404, "Alarm video is not available")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/events/{event_id}/recording")
    def event_recording(event_id: str):
        valid_id(event_id)
        rows = store.rows("SELECT camera_id,last_seen,clip_complete FROM events WHERE id=?", (event_id,))
        if not rows:
            raise HTTPException(404)
        ready = (store.root / "events" / event_id / "alarm.mp4").is_file()
        now = time.time()
        waiting = now - recorder_polled.get(rows[0]["camera_id"], 0) < 30 and now - rows[0]["last_seen"] < 180
        return {"ready": ready, "complete": bool(rows[0]["clip_complete"]), "preparing": waiting and not ready}

    @app.get("/api/ingest/{camera_id}/recordings")
    def recording_requests(camera_id: str, newest: float = Query(default=0, allow_inf_nan=False)):
        if camera_id not in camera_ids:
            raise HTTPException(404)
        now = time.time()
        if now - recorder_seen.get(camera_id, 0) >= 30:
            push.note_recorder(camera_id, newest, now)
            recorder_seen[camera_id] = now
        recorder_polled[camera_id] = now
        rows = store.rows("SELECT id AS event_id,first_seen AS start,last_seen,clip_through AS through FROM events WHERE camera_id=? AND last_seen>? ORDER BY first_seen", (camera_id, now-RECORDING_WINDOW_SECONDS))
        jobs = [{"event_id": row["event_id"], "start": row["start"], "end": min(row["last_seen"], row["start"]+MAX_CLIP_SECONDS), "through": row["through"]} for row in rows]
        return {"monitoring_enabled": store.settings()["monitoring_enabled"], "requests": [job for job in jobs if job["through"] is None or job["end"] > job["through"]+2]}

    @app.post("/api/ingest/{camera_id}/recordings/{event_id}/video")
    async def upload_recording(camera_id: str, event_id: str, request: Request, through: float = Query(allow_inf_nan=False), complete: bool = False):
        valid_id(event_id)
        if camera_id not in camera_ids or not store.rows("SELECT id FROM events WHERE id=? AND camera_id=?", (event_id, camera_id)):
            raise HTTPException(404)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 64_000_000:
                raise HTTPException(413, "Video too large")
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise HTTPException(422, "Expected MP4 video")

        def save():
            with store.lock:
                if not store.rows("SELECT id FROM events WHERE id=?", (event_id,)):
                    raise HTTPException(404)
                path = store.root / "events" / event_id / "alarm.mp4"
                temp = path.with_suffix('.tmp')
                with open(temp, 'wb') as file:
                    file.write(data)
                os.chmod(temp, 0o600)
                temp.replace(path)
                store.execute("UPDATE events SET clip_through=?,clip_complete=? WHERE id=?", (through, complete, event_id))

        await run_in_threadpool(save)
        return {"ok": True}

    @app.get("/api/events/{event_id}/photos/{index}")
    def event_photo(event_id: str, index: int):
        if not 0 <= index < MAX_PHOTOS:
            raise HTTPException(404)
        return file_response(store.root / "events" / valid_id(event_id) / f"photo-{index}.jpg")

    @app.get("/api/events/{event_id}/faces/{index}")
    def face_image(event_id: str, index: int):
        if not 0 <= index < 12:
            raise HTTPException(404)
        return file_response(store.root / "events" / valid_id(event_id) / f"face-{index}.jpg")

    @app.delete("/api/events/{event_id}")
    def delete_event(event_id: str):
        store.delete_event(valid_id(event_id))
        return {"ok": True}

    @app.get("/api/people")
    def people():
        return store.people()

    @app.post("/api/people", status_code=201)
    def add_person(payload: PersonInput):
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "Enter a name")
        if len(store.people()) >= 100:
            raise HTTPException(422, "The library is limited to 100 people")
        pid = uuid.uuid4().hex
        store.execute("INSERT INTO people VALUES(?,?,?)", (pid, name, time.time()))
        return {"id": pid, "name": name}

    @app.patch("/api/people/{person_id}")
    def rename_person(person_id: str, payload: PersonInput):
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "Enter a name")
        store.execute("UPDATE people SET name=? WHERE id=?", (name, valid_id(person_id)))
        return {"ok": True}

    @app.delete("/api/people/{person_id}")
    def delete_person(person_id: str):
        valid_id(person_id)
        with store.lock:
            for row in store.rows("SELECT path FROM samples WHERE person_id=?", (person_id,)):
                (store.root / row["path"]).unlink(missing_ok=True)
            store.execute("DELETE FROM people WHERE id=?", (person_id,))
            # Forget old identity associations and embeddings for the deleted person.
            for row in store.rows("SELECT id,faces FROM events"):
                faces = json.loads(row["faces"])
                changed = False
                for face in faces:
                    if face.get("person_id") == person_id:
                        face.update(person_id=None, similarity=None, embedding=None)
                        changed = True
                if changed:
                    state = "known" if any(f.get("person_id") for f in faces) else "unknown" if any(f["usable"] for f in faces) else "unavailable"
                    store.execute("UPDATE events SET faces=?,state=? WHERE id=?", (json.dumps(faces), state, row["id"]))
            votes.clear()
        return {"ok": True}

    @app.get("/api/people/{person_id}/samples")
    def samples(person_id: str):
        return store.rows("SELECT id,created FROM samples WHERE person_id=? ORDER BY created DESC", (valid_id(person_id),))

    @app.get("/api/samples/{sample_id}/image")
    def sample_image(sample_id: str):
        return file_response(store.root / "people" / f"{valid_id(sample_id)}.jpg")

    @app.delete("/api/samples/{sample_id}")
    def delete_sample(sample_id: str):
        valid_id(sample_id)
        with store.lock:
            store.execute("DELETE FROM samples WHERE id=?", (sample_id,))
            (store.root / "people" / f"{sample_id}.jpg").unlink(missing_ok=True)
            votes.clear()
        return {"ok": True}

    @app.post("/api/people/{person_id}/samples", status_code=201)
    async def enroll(person_id: str, request: Request):
        valid_id(person_id)
        if not store.rows("SELECT id FROM people WHERE id=?", (person_id,)):
            raise HTTPException(404, "Person not found")
        data = await image_bytes(request)
        try:
            face = await run_in_threadpool(engine.enroll, data)
            sid = store.save_sample(person_id, face["embedding"].tolist(), face["crop"])
            return {"id": sid}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/events/{event_id}/assign")
    def assign(event_id: str, payload: Assignment):
        valid_id(event_id)
        valid_id(payload.person_id)
        if not store.rows("SELECT id FROM people WHERE id=?", (payload.person_id,)):
            raise HTTPException(404, "Person not found")
        with store.lock:
            rows = store.rows("SELECT faces,image_at FROM events WHERE id=?", (event_id,))
            if not rows:
                raise HTTPException(404, "Event not found")
            if payload.image_at is not None and payload.image_at != rows[0]["image_at"]:
                raise HTTPException(409, "This event has a newer snapshot. Reopen it before assigning a face.")
            faces = json.loads(rows[0]["faces"])
            if payload.face_index >= len(faces):
                raise HTTPException(422, "Face not found")
            face = faces[payload.face_index]
            if payload.add_sample:
                if not face["usable"] or not face.get("embedding"):
                    raise HTTPException(422, "This face is not clear enough to train. Add a portrait instead.")
                crop = (store.root / "events" / event_id / f"face-{payload.face_index}.jpg").read_bytes()
                try:
                    store.save_sample(payload.person_id, face["embedding"], crop)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from error
            face.update(person_id=payload.person_id, similarity=None, confirmed=True)
            state = "unknown" if any(f["usable"] and not f.get("person_id") for f in faces) else "known"
            store.execute("UPDATE events SET faces=?,state=?,corrected=1 WHERE id=?", (json.dumps(faces), state, event_id))
            votes.clear()
        return {"ok": True}

    @app.post("/api/alerts/snooze")
    def snooze_alerts(minutes: int = Query(ge=0, le=480)):
        with store.lock:
            now = time.time()
            until = now + minutes * 60 if minutes else 0
            store.execute("UPDATE alert_snoozes SET end=? WHERE end>?", (now, now))
            if minutes:
                store.execute("INSERT INTO alert_snoozes(start,end) VALUES(?,?)", (now, until))
            store.execute("UPDATE settings SET value=? WHERE key='alerts_snoozed_until'", (json.dumps(until),))
            if minutes:
                store.execute("UPDATE push_jobs SET state='cancelled' WHERE state='pending' AND event_id IS NOT NULL")
        return {"alerts_snoozed_until": until}

    @app.post("/api/alerts/away")
    def set_away(enabled: bool = Query()):
        store.execute("UPDATE settings SET value=? WHERE key='away_mode'", (json.dumps(enabled),))
        return {"away_mode": enabled}

    @app.put("/api/settings")
    async def settings(payload: SettingsInput):
        nonlocal storage_bytes
        def apply():
            # Finish in-flight inference before pausing and clearing latest frames.
            with ingest_lock, store.lock:
                for key, value in payload.model_dump(exclude_unset=True).items():
                    store.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), key))
                votes.clear()
                if not payload.monitoring_enabled:
                    for image in (store.root / "latest").glob("*.jpg"):
                        image.unlink(missing_ok=True)
        await run_in_threadpool(apply)
        storage_bytes = await run_in_threadpool(store.cleanup)
        return store.settings()

    def process(camera_id, data, captured_at):
        now = time.time()
        with ingest_lock:
            settings = store.settings()
            if not settings["monitoring_enabled"]:
                store.execute("UPDATE cameras SET last_seen=?,error=NULL WHERE id=?", (now, camera_id))
                store.record_coverage(camera_id, "paused", now)
                return {"ok": True, "paused": True}
            frame = engine.decode(data)
            recognition = settings["recognition_enabled"]
            result = engine.analyze(frame, store.samples() if recognition else [], settings["recognition_threshold"], find_faces=recognition)
            write_image(store.root / "latest" / f"{camera_id}.jpg", engine.jpeg(frame))
            h, w = frame.shape[:2]
            store.execute("UPDATE cameras SET last_seen=?,last_frame=?,error=NULL,inference_ms=?,width=?,height=? WHERE id=?", (now, now, result["elapsed_ms"], w, h, camera_id))
            store.record_coverage(camera_id, "online", now)
            face_rows, crops = [], []
            for face in result["faces"]:
                pid = face["person_id"]
                confirmed = False
                if pid:
                    key = (camera_id, pid)
                    history = votes.setdefault(key, [])
                    history[:] = [t for t in history if now - t < 20]
                    # Multiple faces in one snapshot cannot supply repeated votes.
                    if not history or now - history[-1] > 0.8:
                        history.append(now)
                    confirmed = len(history) >= 2
                face_rows.append({"box": face["box"], "usable": face["usable"], "person_id": pid if confirmed else None, "pending": bool(pid and not confirmed), "similarity": face["similarity"], "confirmed": confirmed, "embedding": face["embedding"].tolist() if face["embedding"] is not None else None})
                crops.append(face["crop"])
            for key in list(votes):
                if not votes[key] or now - votes[key][-1] > 20:
                    del votes[key]
            person_count = max(len(result["people"]), len(face_rows))
            moving = tracker.moving(camera_id, result.get("objects", []), now)
            if not person_count and not moving:
                unidentified_observations.pop(camera_id, None)
                return {"ok": True, "people": 0}
            usable = [f for f in face_rows if f["usable"]]
            state = "unknown" if any(not f["person_id"] and not f["pending"] for f in usable) else "unavailable" if any(f["pending"] for f in usable) else "known" if usable else "unavailable"
            # Do not imply everybody is identified when extra people lack a face.
            if state == "known" and person_count > len(usable):
                state = "unavailable"
            if not recognition:
                state = "person"
            if not person_count:
                state = "vehicle" if any(item["kind"] == "vehicle" for item in moving) else "animal"
            annotated = frame.copy()
            for person in result["people"]:
                x, y, bw, bh = person["box"]
                cv2.rectangle(annotated, (x, y), (x + bw, y + bh), (135, 185, 120), 2)
            for item in moving:
                x, y, bw, bh = item["box"]
                cv2.rectangle(annotated, (x, y), (x + bw, y + bh), (200, 170, 90), 2)
            for face in face_rows:
                x, y, bw, bh = face["box"]
                cv2.rectangle(annotated, (x, y), (x + bw, y + bh), (120, 210, 150) if face["person_id"] else (80, 180, 235), 2)
            score = photo_score(frame, result["people"] or moving, face_rows)
            peak_score = max((person.get("score", 1.0) for person in result["people"]), default=0.0)
            event_id = store.record_event(camera_id, state, person_count, face_rows, engine.jpeg(annotated), crops, now, score, peak_score)
            if state in {"vehicle", "animal"}:
                if settings[f"alert_{state}s"]:
                    push.enqueue_event(camera_id, event_id)
                return {"ok": True, "people": 0, "event_id": event_id, "state": state}
            previous_at, count, previous_box = unidentified_observations.get(camera_id, (0, 0, None))
            if state in ALERTING_STATES:
                box = max((person["box"] for person in result["people"]), key=lambda item: item[2] * item[3], default=None)
                # Trees and posts flicker in one spot at night; a person moves.
                still = not recognition and box and previous_box and overlap(box, previous_box) >= SAME_PLACE_OVERLAP
                count = count+1 if now - previous_at < (20 if recognition else SECOND_SIGHTING_SECONDS) and not still else 1
                unidentified_observations[camera_id] = (now, count, box)
                # A clear person alerts at once because a fast intruder may appear once.
                # A weak detection needs a second sighting somewhere else. Either way
                # the event and its video are recorded.
                confident = not recognition and peak_score >= CONFIDENT_PERSON_SCORE
                if count >= 2 or confident:
                    push.enqueue_event(camera_id, event_id)
            else:
                unidentified_observations.pop(camera_id, None)
            return {"ok": True, "people": person_count, "event_id": event_id, "state": state}

    @app.post("/api/ingest/presence")
    async def presence(request: Request):
        try:
            home = json.loads(await image_bytes(request))["home"]
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "Invalid presence report")
        if not isinstance(home, bool):
            raise HTTPException(422, "Invalid presence report")
        for key, value in (("presence_home", home), ("presence_at", time.time())):
            store.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), key))
        return {"ok": True}

    @app.post("/api/ingest/{camera_id}/frame")
    async def ingest(camera_id: str, request: Request):
        if camera_id not in camera_ids:
            raise HTTPException(404)
        now = time.monotonic()
        # Bound work and never build a backlog if the collector is misconfigured.
        if now - latest_processed.get(camera_id, 0) < 0.5:
            raise HTTPException(429, "Camera sampling too fast")
        latest_processed[camera_id] = now
        data = await image_bytes(request)
        try:
            return await run_in_threadpool(process, camera_id, data, time.time())
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/ingest/{camera_id}/heartbeat")
    async def heartbeat(camera_id: str, request: Request):
        if camera_id not in camera_ids:
            raise HTTPException(404)
        try:
            payload = json.loads(await image_bytes(request))
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid heartbeat")
        # Store generic error codes only, never camera URLs or passwords.
        error = payload.get("error")
        error = error if error in {"camera_unreachable", "camera_auth", "capture_failed"} else None
        store.execute("UPDATE cameras SET last_seen=?,error=? WHERE id=?", (time.time(), error, camera_id))
        store.record_coverage(camera_id, "error" if error else "online" if store.settings()["monitoring_enabled"] else "paused")
        return {"ok": True, "monitoring_enabled": store.settings()["monitoring_enabled"]}

    return app
