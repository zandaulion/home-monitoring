"""Synthetic fixtures only: access control, recognition decisions, and retention."""
import json
from pathlib import Path
import time

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from home_monitor.app import create_app
from home_monitor.store import Store
from home_monitor.vision import Vision, choose_identity

OWNER = {"Tailscale-User-Login": "owner@example.invalid"}
COLLECTOR = {"Authorization": "Bearer synthetic-collector-token"}


class SyntheticVision:
    people = []
    faces = []
    decode = staticmethod(Vision.decode)
    jpeg = staticmethod(Vision.jpeg)

    def analyze(self, frame, samples, threshold):
        return {"people": self.people, "faces": self.faces, "elapsed_ms": 12}

    def enroll(self, data):
        self.decode(data)
        return {"embedding": np.array([1.0, 0.0]), "crop": data}


@pytest.fixture
def setup(tmp_path):
    config = {"data_dir": str(tmp_path / "data"), "model_dir": str(tmp_path / "models"), "cameras": [{"id": "camera-1", "name": "Test camera"}], "owner_login": "owner@example.invalid", "collector_token": "synthetic-collector-token", "console_admin_token": "synthetic-console-token", "admin_token": "synthetic-diagnostic-token", "public_origin": "https://public.example.invalid", "allowed_origins": ["https://monitor.example.invalid", "https://public.example.invalid"]}
    engine = SyntheticVision()
    app = create_app(config, engine, push_sender=lambda subscription, payload: 201)
    with TestClient(app, base_url="https://monitor.example.invalid") as client:
        yield client, app.state.store, engine


@pytest.fixture
def jpeg():
    return Vision.jpeg(np.full((240, 320, 3), 100, np.uint8))


def test_owner_and_collector_are_separate(setup, jpeg):
    client, store, engine = setup
    assert client.get("/").status_code == 200  # Empty install/activation shell.
    assert client.get("/api/status", headers=COLLECTOR).status_code == 401
    assert client.get("/api/status", headers={"Tailscale-User-Login": "other@example.invalid"}).status_code == 401
    assert client.get("/api/status", headers=OWNER).status_code == 200
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=OWNER).status_code == 401
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).status_code == 200
    assert client.post("/api/ingest/no-such-camera/frame", content=jpeg, headers=COLLECTOR).status_code == 404


def test_cross_origin_writes_blocked(setup):
    client, *_ = setup
    assert client.post("/api/people", json={"name": "Example Person"}, headers={**OWNER, "Origin": "https://untrusted.example.invalid"}).status_code == 403
    assert client.post("/api/people", json={"name": "Example Person"}, headers={**OWNER, "Origin": "https://monitor.example.invalid"}).status_code == 201


def test_no_person_creates_no_event(setup, jpeg):
    client, store, engine = setup
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["people"] == 0
    assert store.events() == []
    assert client.get("/api/cameras/camera-1/image", headers=OWNER).status_code == 200


def test_person_without_face_is_not_unknown(setup, jpeg):
    client, store, engine = setup
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.9}]
    response = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert response.json()["state"] == "unavailable"
    assert store.events()[0]["faces"] == []


def test_sample_enrollment_and_person_deletion(setup, jpeg):
    client, store, engine = setup
    pid = client.post("/api/people", json={"name": "Example Person"}, headers=OWNER).json()["id"]
    response = client.post(f"/api/people/{pid}/samples", content=jpeg, headers=OWNER)
    assert response.status_code == 201
    sid = response.json()["id"]
    assert client.get(f"/api/samples/{sid}/image", headers=OWNER).status_code == 200
    assert len(store.samples()) == 1
    assert client.delete(f"/api/people/{pid}", headers=OWNER).status_code == 200
    assert not store.samples()
    assert not list((store.root / "people").glob("*.jpg"))


def test_reject_invalid_images(setup):
    client, *_ = setup
    response = client.post("/api/ingest/camera-1/frame", content=b"not an image", headers=COLLECTOR)
    assert response.status_code == 422


def test_paused_monitoring_does_not_store_images(setup, jpeg):
    client, store, engine = setup
    settings = store.settings()
    settings["monitoring_enabled"] = False
    assert client.put("/api/settings", json=settings, headers=OWNER).status_code == 200
    response = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert response.json()["paused"]
    assert not list((store.root / "latest").glob("*.jpg"))
    assert store.events() == []


def test_identity_requires_threshold_and_separation():
    vector = np.array([1.0, 0.0])
    assert choose_identity(vector, [dict(person_id="one", embedding=[0.9, 0.1])])[0] == "one"
    assert choose_identity(vector, [dict(person_id="one", embedding=[0.2, 0.9])])[0] is None
    assert choose_identity(vector, [dict(person_id="one", embedding=[0.9, 0.1]), dict(person_id="two", embedding=[0.88, 0.2])])[0] is None
    assert choose_identity(vector, [])[0] is None


def test_retention_deletes_images_and_metadata_but_keeps_enrollment(tmp_path, jpeg):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    store.execute("INSERT INTO people VALUES(?,?,?)", ("synthetic-person", "Example Person", time.time()))
    store.save_sample("synthetic-person", [1.0, 0.0], jpeg)
    eid = store.record_event("camera-1", "unavailable", 1, [], jpeg, [], now=time.time() - 8*86400)
    store.cleanup()
    assert not store.events()
    assert not (tmp_path / "events" / eid).exists()
    assert len(store.samples()) == 1
    store.db.close()


def test_storage_cap_removes_oldest_first(tmp_path):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    first = store.record_event("camera-1", "unavailable", 1, [], b"x"*800_000, [], now=time.time()-100)
    second = store.record_event("camera-1", "unavailable", 1, [], b"y"*800_000, [], now=time.time())
    store.execute("UPDATE settings SET value='1' WHERE key='storage_limit_mb'")
    store.cleanup()
    assert not (tmp_path / "events" / first).exists()
    assert (tmp_path / "events" / second).exists()
    store.db.close()


def test_event_correction_teaches_only_when_requested(setup, jpeg):
    client, store, engine = setup
    pid = client.post("/api/people", json={"name": "Example Person"}, headers=OWNER).json()["id"]
    faces = [{"box": [1, 1, 50, 50], "usable": True, "person_id": None, "embedding": [1.0, 0.0], "similarity": None}]
    eid = store.record_event("camera-1", "unknown", 1, faces, jpeg, [jpeg])
    assert client.post(f"/api/events/{eid}/assign", json={"person_id": pid, "face_index": 0, "add_sample": False}, headers=OWNER).status_code == 200
    assert len(store.samples()) == 0
    assert store.events()[0]["faces"][0]["name"] == "Example Person"
    assert "embedding" not in store.events()[0]["faces"][0]
    assert client.post(f"/api/events/{eid}/assign", json={"person_id": pid, "face_index": 0}, headers=OWNER).status_code == 200
    assert len(store.samples()) == 1


def test_heartbeat_error_and_recovery(setup):
    client, *_ = setup
    assert client.post("/api/ingest/camera-1/heartbeat", json={"error": "camera_auth"}, headers=COLLECTOR).status_code == 200
    assert client.get("/api/status", headers=OWNER).json()["cameras"][0]["error"] == "camera_auth"
    client.post("/api/ingest/camera-1/heartbeat", json={}, headers=COLLECTOR)
    assert client.get("/api/status", headers=OWNER).json()["cameras"][0]["online"]


def test_known_identity_requires_repeated_observations(setup, jpeg):
    client, store, engine = setup
    pid = client.post("/api/people", json={"name": "Example Person"}, headers=OWNER).json()["id"]
    engine.faces = [{"box": [20, 20, 60, 60], "usable": True, "person_id": pid, "similarity": 0.8, "embedding": np.array([1.0, 0.0]), "crop": jpeg}]
    first = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()
    assert first["state"] == "unavailable"
    assert not store.events()[0]["faces"][0]["person_id"]
    time.sleep(0.9)
    second = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()
    assert second["state"] == "known"
    assert store.events()[0]["faces"][0]["name"] == "Example Person"


def test_good_unmatched_face_is_unknown(setup, jpeg):
    client, store, engine = setup
    engine.faces = [{"box": [20, 20, 60, 60], "usable": True, "person_id": None, "similarity": None, "embedding": np.array([1.0, 0.0]), "crop": jpeg}]
    response = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert response.json()["state"] == "unknown"
    assert store.events()[0]["faces"][0]["usable"]


def test_changed_snapshot_cannot_be_used_for_stale_training(setup, jpeg):
    client, store, engine = setup
    pid = client.post("/api/people", json={"name": "Example Person"}, headers=OWNER).json()["id"]
    faces = [{"box": [1, 1, 50, 50], "usable": True, "person_id": None, "embedding": [1.0, 0.0], "similarity": None}]
    eid = store.record_event("camera-1", "unknown", 1, faces, jpeg, [jpeg])
    image_at = store.events()[0]["image_at"]
    store.execute("UPDATE events SET image_at=? WHERE id=?", (image_at+20,eid))
    response = client.post(f"/api/events/{eid}/assign", json={"person_id": pid, "face_index": 0, "image_at": image_at}, headers=OWNER)
    assert response.status_code == 409
    assert not store.samples()


CONSOLE = {"X-Admin-Token": "synthetic-console-token"}


def test_console_secret_is_required_and_separate_from_owner(setup):
    client, *_ = setup
    for headers in [{}, OWNER, COLLECTOR, {"X-Admin": "1"}, {"X-Admin-Token": "wrong"}]:
        assert client.get("/api/admin/devices", headers=headers).status_code == 404
    assert client.get("/api/admin/devices", headers=CONSOLE).json() == {"devices": []}


def test_invite_activation_and_revocation_protect_images(setup, jpeg):
    client, store, engine = setup
    invite = client.post("/api/admin/invites", json={"label": "Synthetic test device"}, headers=CONSOLE).json()
    assert "/#invite=" in invite["url"]
    assert not client.get("/api/session").json()["authorized"]
    assert client.get("/api/cameras/camera-1/image").status_code == 401
    response = client.post("/api/invites/redeem", json={"code": invite["code"]})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    assert client.get("/api/session").json()["authorized"]
    assert client.get("/api/status").status_code == 200
    device_id = response.json()["device_id"]
    assert client.post(f"/api/admin/devices/{device_id}/revoke", json={"revoked": True}, headers=CONSOLE).status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/cameras/camera-1/image").status_code == 401
    assert client.post("/api/invites/redeem", json={"code": invite["code"]}).status_code == 400
    assert client.post(f"/api/admin/devices/{device_id}/revoke", json={"revoked": False}, headers=CONSOLE).status_code == 200
    assert client.get("/api/status").status_code == 200


def test_rebinding_rotates_token_without_creating_another_device(setup):
    client, store, engine = setup
    invite = client.post("/api/admin/invites", json={"label": "Example device"}, headers=CONSOLE).json()
    first = client.post("/api/invites/redeem", json={"code": invite["code"]}).json()
    from home_monitor.access import COOKIE
    old_token = client.cookies.get(COOKIE)
    second = client.post("/api/invites/redeem", json={"code": invite["code"]}).json()
    assert first["device_id"] == second["device_id"]
    assert client.app.state.accounts.device(old_token) is None
    assert len(client.get("/api/admin/devices", headers=CONSOLE).json()["devices"]) == 1
    used = client.get("/api/admin/invites", headers=CONSOLE).json()["invites"][0]
    assert used["used_at"] and used["code"] is None and used["url"] is None


def test_invite_expiry_and_cancellation(setup):
    client, store, engine = setup
    invite = client.post("/api/admin/invites", json={}, headers=CONSOLE).json()
    row = store.rows("SELECT id FROM access_invites")[0]
    store.execute("UPDATE access_invites SET expires_at=? WHERE id=?", (time.time()-1,row["id"]))
    assert client.post("/api/invites/redeem", json={"code": invite["code"]}).status_code == 400
    invite2 = client.post("/api/admin/invites", json={}, headers=CONSOLE).json()
    row2 = store.rows("SELECT id FROM access_invites ORDER BY id DESC LIMIT 1")[0]
    assert client.post(f"/api/admin/invites/{row2['id']}/revoke", headers=CONSOLE).status_code == 200
    assert client.post("/api/invites/redeem", json={"code": invite2["code"]}).status_code == 400


def test_console_label_and_device_deletion(setup):
    client, *_ = setup
    invite = client.post("/api/admin/invites", json={}, headers=CONSOLE).json()
    device_id = client.post("/api/invites/redeem", json={"code": invite["code"]}).json()["device_id"]
    assert client.post(f"/api/admin/devices/{device_id}/label", json={"label": "Synthetic renamed device"}, headers=CONSOLE).status_code == 200
    assert client.get("/api/admin/devices", headers=CONSOLE).json()["devices"][0]["label"] == "Synthetic renamed device"
    assert client.delete(f"/api/admin/devices/{device_id}", headers=CONSOLE).status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/invites/redeem", json={"code": invite["code"]}).status_code == 400


def test_activation_throttle(setup):
    client, *_ = setup
    for _ in range(20):
        assert client.post("/api/invites/redeem", json={"code": "AAAA-BBBB-CCCC"}).status_code == 400
    assert client.post("/api/invites/redeem", json={"code": "AAAA-BBBB-CCCC"}).status_code == 429


def test_pwa_shell_and_security_headers(setup):
    client, *_ = setup
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "__BUILD_VERSION__" not in worker.text
    assert "no-store" in worker.headers["cache-control"]
    assert worker.headers["service-worker-allowed"] == "/"
    assert "worker-src 'self'" in worker.headers["content-security-policy"]
    assert client.get("/bust").headers["clear-site-data"] == '"cache"'
    assert client.get("/api/events").status_code == 401


def test_push_subscription_is_device_scoped_and_revocable(setup):
    from test_push import subscription
    client, store, engine = setup
    assert client.get("/api/push/status").status_code == 401
    assert client.post("/api/push/subscription", json=subscription(), headers=OWNER).status_code == 403
    response = client.post("/api/push/device", headers=OWNER)
    assert response.status_code == 200
    device_id = response.json()["device_id"]
    assert client.post("/api/push/subscription", json=subscription()).status_code == 200
    assert client.get("/api/push/status").json()["subscribed"]
    assert client.get("/api/admin/devices", headers=CONSOLE).json()["devices"][0]["has_push"]
    assert client.post("/api/push/test").json()["queued"]
    assert client.post(f"/api/admin/devices/{device_id}/revoke", json={"revoked": True}, headers=CONSOLE).status_code == 200
    assert not store.rows("SELECT * FROM push_subscriptions")
    assert not store.rows("SELECT * FROM push_jobs")
    assert client.get("/api/push/status").status_code == 401


def test_push_rejects_private_network_endpoints(setup):
    from test_push import subscription
    client, *_ = setup
    client.post("/api/push/device", headers=OWNER)
    for endpoint in ["http://127.0.0.1/", "https://192.168.1.1/", "https://fcm.googleapis.com.evil.invalid/x", "https://fcm.googleapis.com:8443/x"]:
        assert client.post("/api/push/subscription", json=subscription(endpoint)).status_code == 422


def test_camera_alert_waits_for_two_observations(setup, jpeg):
    from test_push import subscription
    client, store, engine = setup
    client.post("/api/push/device", headers=OWNER)
    assert client.post("/api/push/subscription", json=subscription()).status_code == 200
    pid = client.post("/api/people", json={"name": "Example Person"}).json()["id"]
    client.post(f"/api/people/{pid}/samples", content=jpeg)
    engine.faces = [{"box": [20, 20, 60, 60], "usable": True, "person_id": None, "similarity": None, "embedding": np.array([0.0, 1.0]), "crop": jpeg}]
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["state"] == "unknown"
    assert not store.rows("SELECT * FROM push_jobs")
    time.sleep(0.9)
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["state"] == "unknown"
    jobs = store.rows("SELECT payload FROM push_jobs")
    assert len(jobs) == 1 and "Example Person" not in jobs[0]["payload"]


def test_public_origin_cannot_impersonate_private_owner_or_diagnostics(setup):
    client, *_ = setup
    public = "https://public.example.invalid"
    assert client.get(public+"/").status_code == 200
    for headers in [{}, OWNER, {"Authorization": "Bearer synthetic-diagnostic-token"}]:
        assert client.get(public+"/api/status", headers=headers).status_code == 401
        session = client.get(public+"/api/session", headers=headers).json()
        assert not session["authorized"] and not session["owner"]
    assert client.get(public+"/api/admin/devices", headers=CONSOLE).status_code == 404
    assert client.post(public+"/api/ingest/camera-1/frame", content=b"image", headers=COLLECTOR).status_code == 404
    # Private owner access is preserved independently of public authentication.
    assert client.get("/api/status", headers=OWNER).status_code == 200


def test_public_device_activation_revoke_and_origin_protection(setup):
    client, *_ = setup
    public = "https://public.example.invalid"
    invite = client.post("/api/admin/invites", json={"label": "Synthetic public device"}, headers=CONSOLE).json()
    response = client.post(public+"/api/invites/redeem", json={"code": invite["code"]}, headers={"Origin":public})
    assert response.status_code == 200
    device_id = response.json()["device_id"]
    assert client.get(public+"/api/status").status_code == 200
    assert client.post(public+"/api/people", json={"name": "Example Person"}, headers={"Origin":"https://untrusted.example.invalid"}).status_code == 403
    assert client.post(f"/api/admin/devices/{device_id}/revoke", json={"revoked": True}, headers=CONSOLE).status_code == 200
    assert client.get(public+"/api/status").status_code == 401


def test_alert_snooze_and_cooldown_settings(setup):
    client, store, _ = setup
    response = client.post('/api/alerts/snooze?minutes=60', headers=OWNER)
    assert response.status_code == 200
    assert time.time()+3590 < store.settings()['alerts_snoozed_until'] < time.time()+3610
    assert store.settings()['monitoring_enabled'] is True
    assert client.post('/api/alerts/snooze?minutes=481', headers=OWNER).status_code == 422
    assert client.post('/api/alerts/snooze?minutes=-1', headers=OWNER).status_code == 422
    assert client.post('/api/alerts/snooze?minutes=0', headers=OWNER).json()['alerts_snoozed_until'] == 0
    settings = store.settings()
    settings['alert_cooldown_minutes'] = 15
    assert client.put('/api/settings', headers=OWNER, json=settings).status_code == 200
    assert store.settings()['alert_cooldown_minutes'] == 15
    settings['alert_cooldown_minutes'] = 0
    assert client.put('/api/settings', headers=OWNER, json=settings).status_code == 422
    assert client.post('/api/alerts/snooze?minutes=60').status_code == 401
