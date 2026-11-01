"""Durable, device-scoped Web Push. Payloads never contain faces or images."""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .access import MAX_AGE

ALERT_LIFETIME_SECONDS = 900
OFFLINE_AFTER_SECONDS = 300
PRESENCE_FRESH_SECONDS = 300
OUTAGE_GROUPING_SECONDS = 45
ALERTING_STATES = {"person", "unknown", "unavailable", "vehicle", "animal"}
OBJECT_ALERTS = {"vehicle": ("Vehicle detected", "A vehicle was seen on your cameras. Open Home Monitor to review."), "animal": ("Animal detected", "An animal was seen on your cameras. Open Home Monitor to review.")}


def in_window(start, end, moment):
    """Schedules may cross midnight; identical times mean the whole day."""
    if start == end:
        return True
    return start <= moment < end if start < end else moment >= start or moment < end


def alert_state(settings, now):
    """Why person alerts are on or off right now. Snooze is applied separately."""
    if settings["away_mode"]:
        return {"armed": True, "reason": "away"}
    if settings["auto_away"] and settings["presence_home"] is False and now - settings["presence_at"] < PRESENCE_FRESH_SECONDS:
        return {"armed": True, "reason": "nobody_home"}
    if settings["alert_mode"] == "always":
        return {"armed": True, "reason": "always"}
    if settings["alert_mode"] == "schedule":
        moment = datetime.fromtimestamp(now, ZoneInfo(settings["alert_timezone"])).strftime("%H:%M")
        armed = in_window(settings["alert_schedule_start"], settings["alert_schedule_end"], moment)
        return {"armed": armed, "reason": "schedule" if armed else "outside_schedule"}
    return {"armed": False, "reason": "off"}


def unbase64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def validate_subscription(value):
    endpoint = value.get("endpoint", "")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError("Invalid push endpoint")
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    permitted = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"} or host.endswith(".notify.windows.com")
    if parsed.scheme != "https" or not permitted or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443) or not parsed.path or any(c.isspace() for c in endpoint):
        raise ValueError("Unsupported push service. Use Chrome, Firefox, Edge, or an installed Safari web app.")
    keys = value.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("Invalid subscription keys")
    try:
        p256dh, auth = keys["p256dh"], keys["auth"]
        if not isinstance(p256dh, str) or not isinstance(auth, str) or len(p256dh) > 100 or len(auth) > 40:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unbase64(p256dh))
        if len(unbase64(auth)) != 16:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid subscription keys") from error
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class Push:
    def __init__(self, store, subject, sender=None):
        self.store = store
        self.subject = subject
        self.sender = sender or self._send
        self.key_path = store.root / "vapid-private.pem"
        if not self.key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            with os.fdopen(os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                file.write(data)
        key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS push_jobs(
                    id INTEGER PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES push_subscriptions(device_id) ON DELETE CASCADE,
                    event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL, created_at REAL NOT NULL,
                    next_try REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending', UNIQUE(device_id,event_id)
                );
                CREATE TABLE IF NOT EXISTS push_cooldown(camera_id TEXT PRIMARY KEY,last_queued REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS camera_outages(camera_id TEXT PRIMARY KEY,since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS recorder_status(camera_id TEXT PRIMARY KEY,polled_at REAL NOT NULL,stream_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS recorder_outages(camera_id TEXT PRIMARY KEY,since REAL NOT NULL);
            """)
            if "sent_at" not in {row["name"] for row in store.rows("PRAGMA table_info(push_jobs)")}:
                store.db.execute("ALTER TABLE push_jobs ADD COLUMN sent_at REAL")

    def subscribe(self, device_id, subscription):
        clean = validate_subscription(subscription)
        with self.store.lock:
            other = self.store.rows("SELECT device_id FROM push_subscriptions WHERE endpoint=? AND device_id<>?", (clean["endpoint"], device_id))
            if other:
                raise ValueError("This browser subscription belongs to another device. Disable notifications, then enable them again.")
            previous = self.store.rows("SELECT endpoint FROM push_subscriptions WHERE device_id=?", (device_id,))
            if previous and previous[0]["endpoint"] != clean["endpoint"]:
                self.unsubscribe(device_id)
            self.store.execute("INSERT INTO push_subscriptions(device_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint,p256dh=excluded.p256dh,auth=excluded.auth,last_error=NULL", (device_id, clean["endpoint"], clean["keys"]["p256dh"], clean["keys"]["auth"], time.time()))

    def unsubscribe(self, device_id):
        self.store.execute("DELETE FROM push_subscriptions WHERE device_id=?", (device_id,))

    def status(self, device_id):
        rows = self.store.rows("SELECT last_sent,last_error FROM push_subscriptions WHERE device_id=?", (device_id,)) if device_id else []
        return {"public_key": self.public_key, "subscribed": bool(rows), "last_sent": rows[0]["last_sent"] if rows else None, "last_error": rows[0]["last_error"] if rows else None}

    @staticmethod
    def payload(test=False, state=None, event_id=None):
        if test:
            title, body = "Home Monitor test", "Notifications are working. You can close Home Monitor and still receive alerts."
        elif state in OBJECT_ALERTS:
            title, body = OBJECT_ALERTS[state]
        elif state == "person":
            title, body = "Person detected", "Someone was seen on your cameras. Open Home Monitor to review."
        else:
            title, body = "Unidentified person detected", "A person could not be identified. Open Home Monitor to review."
        message = {"title": title, "body": body, "tag": "home-monitor-test" if test else "home-monitor-activity", "expires_at": time.time()+ALERT_LIFETIME_SECONDS}
        return {**message, "event_id": event_id} if event_id else message

    def enqueue_event(self, camera_id, event_id):
        with self.store.lock:
            now = time.time()
            settings = self.store.settings()
            if not settings["monitoring_enabled"] or settings["alerts_snoozed_until"] > now or not alert_state(settings, now)["armed"]:
                return
            event = self.store.rows("SELECT camera_id,state FROM events WHERE id=?", (event_id,))
            if not event or event[0]["camera_id"] != camera_id or event[0]["state"] not in ALERTING_STATES:
                return
            devices = self.devices(now)
            recent = self.store.rows("SELECT last_queued FROM push_cooldown WHERE camera_id=?", (camera_id,))
            if not devices or (recent and now-recent[0]["last_queued"] < settings["alert_cooldown_minutes"] * 60):
                return
            inserted = False
            for device in devices:
                cursor = self.store.execute("INSERT OR IGNORE INTO push_jobs(device_id,event_id,payload,created_at,next_try) VALUES(?,?,?,?,?)", (device["device_id"], event_id, json.dumps(self.payload(state=event[0]["state"], event_id=event_id)), now, now))
                inserted = inserted or bool(cursor.rowcount)
            if inserted:
                self.store.execute("INSERT INTO push_cooldown VALUES(?,?) ON CONFLICT(camera_id) DO UPDATE SET last_queued=excluded.last_queued", (camera_id, now))

    def devices(self, now):
        return self.store.rows("SELECT s.device_id FROM push_subscriptions s JOIN access_devices d ON d.id=s.device_id WHERE d.revoked=0 AND COALESCE(d.last_seen,d.created_at)>?", (now-MAX_AGE,))

    def notify_health(self, title, body, tag, now):
        message = {"title": title, "body": body, "tag": tag, "expires_at": now+ALERT_LIFETIME_SECONDS}
        for device in self.devices(now):
            self.store.execute("INSERT INTO push_jobs(device_id,payload,created_at,next_try) VALUES(?,?,?,?)", (device["device_id"], json.dumps(message), now, now))

    def check_cameras(self, now=None):
        """Tell every device once when frames stop, and once when they return."""
        now = time.time() if now is None else now
        with self.store.lock:
            settings = self.store.settings()
            if not settings["monitoring_enabled"] or not settings["offline_alerts"]:
                self.store.execute("DELETE FROM camera_outages")
                return
            cameras = self.store.rows("SELECT id,name,COALESCE(last_frame,last_seen) AS seen FROM cameras")
            known = {row["camera_id"] for row in self.store.rows("SELECT camera_id FROM camera_outages")}
            down = [camera for camera in cameras if camera["seen"] and now-camera["seen"] > OFFLINE_AFTER_SECONDS]
            down_ids = {camera["id"] for camera in down}
            fresh = [camera for camera in down if camera["id"] not in known]
            minutes = OFFLINE_AFTER_SECONDS // 60
            if fresh and len(down) == len(cameras) and len(cameras) > 1:
                self.notify_health("Monitoring has stopped", f"No camera has sent a snapshot for {minutes} minutes. Check the collector and cameras.", "home-monitor-health", now)
            else:
                for camera in fresh:
                    self.notify_health("Camera offline", f"{camera['name']} has not sent a snapshot for {minutes} minutes.", f"home-monitor-camera-{camera['id']}", now)
            for camera in fresh:
                self.store.execute("INSERT INTO camera_outages VALUES(?,?)", (camera["id"], camera["seen"]))
            for camera in cameras:
                if camera["id"] in known and camera["id"] not in down_ids:
                    self.store.execute("DELETE FROM camera_outages WHERE camera_id=?", (camera["id"],))
                    self.notify_health("Camera back online", f"{camera['name']} is sending snapshots again.", f"home-monitor-camera-{camera['id']}", now)

    def note_recorder(self, camera_id, newest, now):
        self.store.execute("INSERT INTO recorder_status VALUES(?,?,?) ON CONFLICT(camera_id) DO UPDATE SET polled_at=excluded.polled_at,stream_at=excluded.stream_at", (camera_id, now, newest))

    def recording_problems(self, now):
        """Cameras whose recorder went quiet, or whose footage stopped arriving."""
        rows = self.store.rows("SELECT c.id,c.name,r.polled_at,r.stream_at FROM recorder_status r JOIN cameras c ON c.id=r.camera_id")
        return rows, [row for row in rows if now-row["polled_at"] > OFFLINE_AFTER_SECONDS or now-row["stream_at"] > OFFLINE_AFTER_SECONDS]

    def check_recorders(self, now=None):
        """Tell every device once when video stops, and once when it returns."""
        now = time.time() if now is None else now
        with self.store.lock:
            settings = self.store.settings()
            if not settings["monitoring_enabled"] or not settings["offline_alerts"]:
                self.store.execute("DELETE FROM recorder_outages")
                return
            rows, problems = self.recording_problems(now)
            if problems:
                # Reports are about 30 seconds apart, so one failure crosses the limit
                # camera by camera. Include the others now to send a single message.
                problems = self.recording_problems(now+OUTAGE_GROUPING_SECONDS)[1]
            known = {row["camera_id"] for row in self.store.rows("SELECT camera_id FROM recorder_outages")}
            camera_down = {row["camera_id"] for row in self.store.rows("SELECT camera_id FROM camera_outages")}
            # A camera that is offline already has its own alert.
            fresh = [row for row in problems if row["id"] not in known and row["id"] not in camera_down]
            minutes = OFFLINE_AFTER_SECONDS // 60
            if fresh and len(problems) == len(rows) and len(rows) > 1:
                self.notify_health("Video recording has stopped", f"No event video has been recorded for {minutes} minutes. Alerts and photos still work.", "home-monitor-recording", now)
            else:
                for row in fresh:
                    self.notify_health("Video recording has stopped", f"{row['name']} has not recorded video for {minutes} minutes. Alerts and photos still work.", f"home-monitor-recording-{row['id']}", now)
            for row in fresh:
                self.store.execute("INSERT INTO recorder_outages VALUES(?,?)", (row["id"], row["polled_at"]))
            recovered = [row for row in rows if row["id"] in known and row["id"] not in {item["id"] for item in problems}]
            for row in recovered:
                self.store.execute("DELETE FROM recorder_outages WHERE camera_id=?", (row["id"],))
            if recovered:
                names = "All cameras" if len(recovered) == len(rows) and len(rows) > 1 else ", ".join(row["name"] for row in recovered)
                self.notify_health("Video recording is back", f"{names}: recording again.", "home-monitor-recording", now)

    def test(self, device_id):
        with self.store.lock:
            if not self.status(device_id)["subscribed"]:
                raise ValueError("Enable notifications on this device first.")
            recent = self.store.rows("SELECT id FROM push_jobs WHERE device_id=? AND event_id IS NULL AND created_at>?", (device_id, time.time()-30))
            if recent:
                raise ValueError("Wait 30 seconds before sending another test.")
            now = time.time()
            self.store.execute("INSERT INTO push_jobs(device_id,payload,created_at,next_try) VALUES(?,?,?,?)", (device_id, json.dumps(self.payload(True)), now, now))

    def _send(self, subscription, payload):
        try:
            response = webpush(subscription_info=subscription, data=json.dumps(payload), vapid_private_key=str(self.key_path), vapid_claims={"sub": self.subject}, ttl=ALERT_LIFETIME_SECONDS, timeout=6, headers={"Urgency": "high"})
            return response.status_code
        except WebPushException as error:
            return error.response.status_code if error.response is not None else None
        except Exception:
            # Exceptions can contain subscription endpoints. Never log their text.
            return None

    def deliver_one(self):
        with self.store.lock:
            now = time.time()
            self.store.execute("UPDATE push_jobs SET state='expired' WHERE state='pending' AND created_at<?", (now-ALERT_LIFETIME_SECONDS,))
            self.store.execute("DELETE FROM push_jobs WHERE event_id IS NULL AND created_at<?", (now-86400,))
            rows = self.store.rows("SELECT j.*,s.endpoint,s.p256dh,s.auth FROM push_jobs j JOIN push_subscriptions s ON s.device_id=j.device_id JOIN access_devices d ON d.id=j.device_id WHERE j.state='pending' AND j.next_try<=? AND d.revoked=0 AND COALESCE(d.last_seen,d.created_at)>? ORDER BY j.id LIMIT 1", (now, now-MAX_AGE))
            if not rows:
                return False
            job = rows[0]
            if job["event_id"]:
                event = self.store.rows("SELECT state FROM events WHERE id=?", (job["event_id"],))
                settings = self.store.settings()
                if not settings["monitoring_enabled"] or settings["alerts_snoozed_until"] > now or not event or event[0]["state"] not in ALERTING_STATES:
                    self.store.execute("UPDATE push_jobs SET state='cancelled' WHERE id=?", (job["id"],))
                    return True
            # Claim before network I/O; a restart retries instead of losing the job.
            self.store.execute("UPDATE push_jobs SET next_try=?,attempts=attempts+1 WHERE id=?", (now+15, job["id"]))
        subscription = {"endpoint": job["endpoint"], "keys": {"p256dh": job["p256dh"], "auth": job["auth"]}}
        code = self.sender(subscription, json.loads(job["payload"]))
        with self.store.lock:
            if code in (404, 410):
                self.unsubscribe(job["device_id"])
            elif code is not None and 200 <= code < 300:
                self.store.execute("UPDATE push_jobs SET state='sent',sent_at=? WHERE id=?", (time.time(), job["id"]))
                self.store.execute("UPDATE push_subscriptions SET last_sent=?,last_error=NULL WHERE device_id=?", (time.time(), job["device_id"]))
            else:
                retry = (code is None or code == 429 or code >= 500) and job["attempts"] < 2
                self.store.execute("UPDATE push_jobs SET state=?,next_try=? WHERE id=?", ("pending" if retry else "failed", time.time()+15*(job["attempts"]+1), job["id"]))
                self.store.execute("UPDATE push_subscriptions SET last_error=? WHERE device_id=?", ("delivery_unavailable" if retry else "delivery_failed", job["device_id"]))
        return True
