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
from home_monitor.vision import ObjectTracker, Vision, choose_identity

OWNER = {"Tailscale-User-Login": "owner@example.invalid"}
COLLECTOR = {"Authorization": "Bearer synthetic-collector-token"}


class SyntheticVision:
    people = []
    objects = []
    faces = []
    decode = staticmethod(Vision.decode)
    jpeg = staticmethod(Vision.jpeg)

    def analyze(self, frame, samples, threshold, find_faces=True):
        return {"people": self.people, "objects": self.objects, "faces": self.faces if find_faces else [], "elapsed_ms": 12}

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


def test_person_only_mode_skips_faces_and_labels_people(setup, jpeg):
    client, store, engine = setup
    settings = store.settings()
    settings["recognition_enabled"] = False
    assert client.put("/api/settings", json=settings, headers=OWNER).status_code == 200
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.9}]
    engine.faces = [{"box": [1, 1, 50, 50], "usable": True, "embedding": np.array([1.0, 0.0]), "crop": jpeg, "person_id": None, "similarity": None}]
    response = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert response.json()["state"] == "person"
    event = store.events()[0]
    assert event["faces"] == [] and event["state"] == "person"
    assert client.get("/api/timeline", params={"start": 1, "end": 2, "state": "person"}, headers=OWNER).status_code == 200


def person_only(client, store):
    settings = store.settings()
    settings["recognition_enabled"] = False
    client.put("/api/settings", json=settings, headers=OWNER)
    device = client.app.state.accounts.create_invite("Synthetic phone")
    client.post("/api/invites/redeem", json={"code": device["code"]})
    device_id = client.app.state.accounts.devices()[0]["id"]
    store.execute("INSERT INTO push_subscriptions(device_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)", (device_id, "https://fcm.googleapis.com/x", "a", "b", time.time()))
    client.cookies.clear()


def alerts(store):
    return len(store.rows("SELECT id FROM push_jobs"))


def test_reasons_sort_events_into_alerts_and_quiet(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.47}]
    weak = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["event_id"]
    assert store.rows("SELECT alert_reason FROM events WHERE id=?", (weak,))[0]["alert_reason"] == "quiet"
    home = client.get("/api/alerts", headers=OWNER).json()
    assert home["alerts"] == [] and home["quiet"]["total"] == 1 and home["quiet"]["cameras"][0]["id"] == "camera-1"
    time.sleep(0.51)
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.9}]
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["event_id"] == weak
    home = client.get("/api/alerts", headers=OWNER).json()
    assert [event["alert_reason"] for event in home["alerts"]] == ["sent"] and home["quiet"]["total"] == 0


def test_cooldown_keeps_a_real_detection_among_the_alerts(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.9}]
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    store.execute("UPDATE events SET last_seen=last_seen-100")
    time.sleep(0.51)
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    reasons = [event["alert_reason"] for event in client.get("/api/alerts", headers=OWNER).json()["alerts"]]
    assert reasons == ["cooldown", "sent"]


def test_sent_alert_is_never_downgraded(tmp_path):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    eid = store.record_event("camera-1", "person", 1, [], b"x", [])
    store.mark_alert(eid, "sent")
    store.mark_alert(eid, "cooldown")
    store.mark_alert(eid, "quiet")
    assert store.rows("SELECT alert_reason FROM events")[0]["alert_reason"] == "sent"
    store.db.close()


def test_timeline_filters_alerts_and_quiet(setup):
    client, store, engine = setup
    now = time.time()
    alert = store.record_event("camera-1", "person", 1, [], b"x", [], now=now-100)
    quiet = store.record_event("camera-1", "person", 1, [], b"x", [], now=now-50)
    store.mark_alert(alert, "sent")
    store.mark_alert(quiet, "quiet")
    view = lambda level: [row["id"] for row in client.get("/api/timeline", params={"start": now-1000, "end": now, "level": level}, headers=OWNER).json()["events"]]
    assert view("alerts") == [alert] and view("quiet") == [quiet]
    assert client.get("/api/timeline", params={"start": now-1000, "end": now, "level": "other"}, headers=OWNER).status_code == 422


def test_quiet_events_expire_after_two_days_and_alerts_stay(tmp_path):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    now = time.time()
    quiet = store.record_event("camera-1", "person", 1, [], b"x", [], now=now-3*86400)
    alert = store.record_event("camera-1", "person", 1, [], b"x", [], now=now-3*86400+100)
    store.mark_alert(quiet, "quiet")
    store.mark_alert(alert, "sent")
    store.cleanup()
    assert [event["id"] for event in store.events()] == [alert]
    store.db.close()


def test_storage_limit_removes_quiet_events_before_older_alerts(tmp_path):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    now = time.time()
    alert = store.record_event("camera-1", "person", 1, [], b"x"*800_000, [], now=now-1000)
    quiet = store.record_event("camera-1", "person", 1, [], b"y"*800_000, [], now=now-100)
    store.mark_alert(alert, "sent")
    store.mark_alert(quiet, "quiet")
    store.execute("UPDATE settings SET value='1' WHERE key='storage_limit_mb'")
    store.cleanup()
    assert [event["id"] for event in store.events()] == [alert]
    store.db.close()


SPOT = [100, 50, 60, 200]


def event_at(store, box=SPOT, reason="sent", score=0.7, state="person", gap=100):
    store.execute("UPDATE events SET last_seen=last_seen-?", (gap,))
    event_id = store.record_event("camera-1", state, 1, [], b"x", [], peak_score=score, box=box)
    store.mark_alert(event_id, reason)
    return event_id


def teach(client, store, count=2):
    results = []
    for _ in range(count):
        results.append(client.post(f"/api/events/{event_at(store)}/false-positive", headers=OWNER).json())
    return results


def test_marking_moves_an_alert_out_and_undo_restores_it(setup):
    client, store, engine = setup
    eid = event_at(store)
    assert client.post(f"/api/events/{eid}/false-positive", headers=OWNER).json() == {"learned": True, "spot": False}
    home = client.get("/api/alerts", headers=OWNER).json()
    assert home["alerts"] == [] and home["quiet"]["total"] == 1
    assert client.delete(f"/api/events/{eid}/false-positive", headers=OWNER).status_code == 200
    assert [event["alert_reason"] for event in client.get("/api/alerts", headers=OWNER).json()["alerts"]] == ["sent"]
    assert not store.rows("SELECT id FROM false_marks")
    assert client.post(f"/api/events/{'0'*32}/false-positive", headers=OWNER).status_code == 404
    assert client.post(f"/api/events/{eid}/false-positive", headers=COLLECTOR).status_code == 401


def test_one_mark_is_not_a_spot_and_two_are(setup):
    client, store, engine = setup
    assert [result["spot"] for result in teach(client, store)] == [False, True]
    spots = client.get("/api/spots", headers=OWNER).json()
    assert len(spots) == 1 and spots[0]["marks"] == 2 and spots[0]["kind"] == "person"


def test_event_without_a_position_teaches_nothing(setup):
    client, store, engine = setup
    eid = event_at(store, box=None)
    assert client.post(f"/api/events/{eid}/false-positive", headers=OWNER).json() == {"learned": False, "spot": False}
    assert not store.rows("SELECT id FROM false_marks")


def frame(client, jpeg, engine, box, score):
    engine.people = [{"box": box, "score": score}]
    time.sleep(0.51)
    return client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()


def test_spot_quiets_detections_there_but_not_clear_or_elsewhere(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    teach(client, store)
    store.execute("UPDATE events SET last_seen=last_seen-200")
    frame(client, jpeg, engine, SPOT, 0.72)
    assert alerts(store) == 0 and store.events()[0]["alert_reason"] == "quiet"
    store.execute("UPDATE events SET last_seen=last_seen-200")
    frame(client, jpeg, engine, SPOT, 0.85)
    assert alerts(store) == 1
    store.execute("DELETE FROM push_cooldown")
    store.execute("UPDATE events SET last_seen=last_seen-200")
    frame(client, jpeg, engine, [400, 50, 60, 200], 0.72)
    assert alerts(store) == 2


def test_person_beside_the_spot_is_not_hidden_by_it(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    teach(client, store)
    store.execute("UPDATE events SET last_seen=last_seen-200")
    engine.people = [{"box": SPOT, "score": 0.7}, {"box": [400, 50, 60, 200], "score": 0.72}]
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert alerts(store) == 1


def test_person_arriving_after_a_mark_is_a_new_event(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    eid = frame(client, jpeg, engine, SPOT, 0.7)["event_id"]
    client.post(f"/api/events/{eid}/false-positive", headers=OWNER)
    assert frame(client, jpeg, engine, [400, 50, 60, 200], 0.7)["event_id"] != eid


def test_marking_cancels_a_queued_notification(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    eid = frame(client, jpeg, engine, SPOT, 0.9)["event_id"]
    client.post(f"/api/events/{eid}/false-positive", headers=OWNER)
    assert store.rows("SELECT state FROM push_jobs")[0]["state"] == "cancelled"


def test_marks_expire_and_a_spot_can_be_removed(setup):
    client, store, engine = setup
    teach(client, store)
    spot = client.get("/api/spots", headers=OWNER).json()[0]
    assert client.delete("/api/spots/99999", headers=OWNER).status_code == 404
    assert client.delete(f"/api/spots/{spot['id']}", headers=OWNER).status_code == 200
    assert client.get("/api/spots", headers=OWNER).json() == []
    teach(client, store)
    assert store.ignored_spots(time.time()+31*86400) == []
    store.execute("UPDATE false_marks SET created=?", (time.time()-31*86400,))
    store.cleanup()
    assert not store.rows("SELECT id FROM false_marks")


def test_spots_are_separate_for_each_kind_and_survive_their_events(setup):
    client, store, engine = setup
    teach(client, store)
    assert not store.in_ignored_spot("camera-1", "vehicle", SPOT)
    assert store.in_ignored_spot("camera-1", "person", SPOT)
    for event in store.events():
        store.delete_event(event["id"])
    assert store.in_ignored_spot("camera-1", "person", SPOT)


def test_confident_person_alerts_on_the_first_frame(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.72}]
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert alerts(store) == 1
    assert store.events()[0]["peak_score"] == 0.72


def test_weak_lone_detection_is_recorded_without_an_alert(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.47}]
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert alerts(store) == 0
    assert len(store.events()) == 1


def test_weak_person_who_moves_alerts_on_the_second_sighting(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.47}]
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    time.sleep(0.51)
    engine.people = [{"box": [120, 10, 50, 100], "score": 0.47}]
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    assert alerts(store) == 1


def test_weak_detection_flickering_in_one_spot_never_alerts(setup, jpeg):
    client, store, engine = setup
    person_only(client, store)
    engine.people = [{"box": [341, 135, 32, 88], "score": 0.52}]
    for _ in range(4):
        client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
        time.sleep(0.51)
    assert alerts(store) == 0
    assert store.events()


def test_invalid_alert_settings_are_rejected(setup):
    client, store, engine = setup
    settings = store.settings()
    assert client.put("/api/settings", json={**settings, "alert_mode": "sometimes"}, headers=OWNER).status_code == 422
    assert client.put("/api/settings", json={**settings, "alert_schedule_start": "25:00"}, headers=OWNER).status_code == 422
    assert client.put("/api/settings", json={**settings, "alert_timezone": "Mars/Base"}, headers=OWNER).status_code == 422
    assert client.put("/api/settings", json={**settings, "alert_timezone": "Etc/GMT-3", "alert_mode": "schedule"}, headers=OWNER).status_code == 200
    assert store.settings()["alert_mode"] == "schedule"


def test_away_button_arms_alerts_and_presence_is_collector_only(setup):
    client, store, engine = setup
    store.execute("UPDATE settings SET value='\"off\"' WHERE key='alert_mode'")
    assert client.get("/api/status", headers=OWNER).json()["alerts"] == {"armed": False, "reason": "off"}
    assert client.post("/api/alerts/away?enabled=true", headers=OWNER).status_code == 200
    assert client.get("/api/status", headers=OWNER).json()["alerts"]["reason"] == "away"
    assert client.post("/api/ingest/presence", json={"home": False}, headers=OWNER).status_code == 401
    assert client.post("/api/ingest/presence", json={"home": "no"}, headers=COLLECTOR).status_code == 422
    assert client.post("/api/ingest/presence", json={"home": False}, headers=COLLECTOR).status_code == 200
    assert store.settings()["presence_home"] is False


def test_event_keeps_a_spread_of_photos_and_the_best_as_snapshot(tmp_path):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    start = time.time()
    scores = [1, 5, 2, 3, 4, 9, 0.5]
    for index, score in enumerate(scores):
        eid = store.record_event("camera-1", "person", 1, [], f"frame-{index}".encode(), [], now=start+index*3, score=score)
    event = store.events()[0]
    assert len(event["photos"]) == 5
    assert store.event_image(eid).read_bytes() == b"frame-5"
    assert not (tmp_path / "events" / eid / "snapshot.jpg").exists()
    assert sorted(photo["score"] for photo in event["photos"]) == [2, 3, 4, 5, 9]
    assert event["image_at"] == start+15
    assert event["last_seen"] == start+18
    store.db.close()


def test_frames_too_close_together_extend_the_event_without_a_photo(tmp_path):
    store = Store(tmp_path, [{"id": "camera-1", "name": "Example"}])
    start = time.time()
    store.record_event("camera-1", "person", 1, [], b"one", [], now=start, score=1)
    store.record_event("camera-1", "person", 1, [], b"two", [], now=start+1, score=9)
    event = store.events()[0]
    assert len(event["photos"]) == 1 and event["last_seen"] == start+1
    store.db.close()


def test_event_photos_require_device_access(setup, jpeg):
    client, store, engine = setup
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.9}]
    eid = client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["event_id"]
    assert client.get(f"/api/events/{eid}/photos/0", headers=COLLECTOR).status_code == 401
    assert client.get(f"/api/events/{eid}/photos/0", headers=OWNER).status_code == 200
    assert client.get(f"/api/events/{eid}/photos/5", headers=OWNER).status_code == 404


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


def test_small_face_flag_can_be_stored_as_json(tmp_path):
    class Detector:
        def setInputSize(self, size):
            pass

        def detect(self, frame):
            return 1, np.array([[10, 10, 20, 20] + [0] * 11], dtype=np.float32)

    engine = Vision.__new__(Vision)
    engine.detector = Detector()
    faces = engine._faces(np.full((240, 320, 3), 100, np.uint8))
    assert faces[0]["usable"] is False
    json.dumps({"usable": faces[0]["usable"]})


CAR = {"kind": "vehicle", "box": [10, 10, 100, 60], "score": 0.8}
CAT = {"kind": "animal", "box": [200, 150, 40, 30], "score": 0.8}


def boxes(*lefts):
    return [{**CAR, "box": [left, 40, 100, 60]} for left in lefts]


def test_tracker_treats_first_view_as_scenery_and_reports_only_movement():
    tracker = ObjectTracker()
    assert tracker.moving("camera", [CAR], 0) == []
    assert tracker.moving("camera", [CAR], 15) == []
    assert tracker.moving("camera", [CAR, *boxes(150)], 30) == []
    assert tracker.moving("camera", [CAR, *boxes(150, 300)], 31) == boxes(300)
    assert tracker.moving("camera", [CAR, *boxes(150, 300)], 45) == []


def test_parked_car_hidden_by_darkness_stays_scenery():
    tracker = ObjectTracker()
    tracker.moving("camera", [], 0)
    for now in range(100, 800, 15):
        tracker.moving("camera", [CAR], now)
    # Twelve dark hours, then the flickers that caused false events overnight.
    for now in (43200, 44040, 45960, 46860, 47640):
        assert tracker.moving("camera", [CAR], now) == []
        assert tracker.moving("camera", [CAR], now + 1) == []


def test_single_frame_flicker_never_counts_as_movement():
    tracker = ObjectTracker()
    tracker.moving("camera", [], 0)
    for now in (100, 1000, 2000, 3000, 4000):
        assert tracker.moving("camera", [CAR], now) == []


def test_a_driven_path_is_movement_again_later():
    tracker = ObjectTracker()
    tracker.moving("camera", [], 0)
    assert tracker.moving("camera", boxes(100), 10) == []
    assert tracker.moving("camera", boxes(200), 11) == boxes(200)
    assert tracker.moving("camera", boxes(100), 90000) == []
    assert tracker.moving("camera", boxes(200), 90001) == boxes(200)


def test_parked_vehicle_makes_no_events_but_a_new_one_does(setup, jpeg):
    client, store, engine = setup
    engine.objects = [CAR]
    for _ in range(2):
        time.sleep(0.51)
        assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["people"] == 0
    assert store.events() == []
    engine.objects = [CAR, {**CAR, "box": [150, 40, 100, 60]}]
    time.sleep(0.51)
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["people"] == 0
    engine.objects = [CAR, {**CAR, "box": [300, 40, 100, 60]}]
    time.sleep(0.51)
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["state"] == "vehicle"
    event = store.events()[0]
    assert event["state"] == "vehicle" and event["person_count"] == 0
    assert client.get("/api/timeline", params={"start": 1, "end": 2, "state": "vehicle"}, headers=OWNER).status_code == 200


def test_person_takes_priority_over_a_moving_object(setup, jpeg):
    client, store, engine = setup
    client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR)
    time.sleep(0.51)
    engine.objects = [CAT]
    engine.people = [{"box": [10, 10, 50, 100], "score": 0.9}]
    assert client.post("/api/ingest/camera-1/frame", content=jpeg, headers=COLLECTOR).json()["state"] == "unavailable"


def activate(client, store):
    code = client.app.state.accounts.create_invite("Synthetic phone")["code"]
    client.post("/api/invites/redeem", json={"code": code})
    return client.app.state.accounts.devices()[0]["id"]


def test_opening_the_app_renews_access(setup):
    client, store, engine = setup
    activate(client, store)
    response = client.get("/api/session")
    assert response.json()["device"]
    cookie = response.headers["set-cookie"]
    assert f"Max-Age={400*86400}" in cookie and "HttpOnly" in cookie and "Secure" in cookie


def test_session_without_a_device_sets_no_cookie(setup):
    client, store, engine = setup
    assert "set-cookie" not in client.get("/api/session", headers=OWNER).headers


def test_expiry_follows_use_not_activation_date(setup):
    client, store, engine = setup
    device = activate(client, store)
    store.execute("UPDATE access_devices SET created_at=?,last_seen=? WHERE id=?", (time.time()-500*86400, time.time()-30*86400, device))
    assert client.get("/api/status").status_code == 200
    store.execute("UPDATE access_devices SET last_seen=? WHERE id=?", (time.time()-401*86400, device))
    assert client.get("/api/status").status_code == 401


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


def test_timeline_includes_whole_day_and_crossing_events_without_private_faces(setup):
    client, store, _ = setup
    start = time.time() - 86400
    for index in range(125):
        store.record_event('camera-1', 'unknown' if index % 2 else 'known', 1, [], b'synthetic', [], now=start+index*650)
    crossing = store.record_event('camera-1', 'unavailable', 1, [], b'synthetic', [], now=start-20)
    store.execute('UPDATE events SET last_seen=? WHERE id=?', (start+10, crossing))
    result = client.get('/api/timeline', params={'start':start, 'end':start+86400}, headers=OWNER)
    assert result.status_code == 200
    body = result.json()
    assert len(body['events']) == 126
    assert all('faces' not in row for row in body['events'])
    filtered = client.get('/api/timeline', params={'start':start,'end':start+86400,'state':'unknown'}, headers=OWNER).json()
    assert len(filtered['events']) == 62
    ids = ','.join(row['id'] for row in body['events'][:12])
    details = client.get('/api/timeline/events', params={'ids':ids}, headers=OWNER)
    assert details.status_code == 200
    assert len(details.json()) == 12
    assert client.get('/api/timeline',params={'start':start,'end':start+90001},headers=OWNER).status_code == 422
    assert client.get('/api/timeline',params={'start':start,'end':start},headers=OWNER).status_code == 422
    assert client.get('/api/timeline',params={'start':'nan','end':start},headers=OWNER).status_code == 422
    assert client.get('/api/timeline',params={'start':start,'end':start+60,'state':'bad'},headers=OWNER).status_code == 422
    assert client.get('/api/timeline/events',params={'ids':'bad'},headers=OWNER).status_code == 422
    assert client.get('/api/timeline',params={'start':start,'end':start+60}).status_code == 401
    assert client.get('/assets/timeline.js').status_code == 200


def test_timeline_tracks_snooze_coverage_and_retention(setup):
    client, store, _ = setup
    now = time.time()
    store.record_coverage('camera-1','online',now-400)
    store.record_coverage('camera-1','online',now-350)
    store.record_coverage('camera-1','error',now-300)
    store.record_coverage('camera-1','online',now-50)
    periods = store.rows('SELECT * FROM camera_periods ORDER BY start')
    assert len(periods) == 3
    assert periods[0]['end'] == now-300
    assert periods[1]['end'] == now-200  # A missing heartbeat is unknown, not an extended outage.
    assert client.post('/api/alerts/snooze?minutes=60',headers=OWNER).status_code == 200
    assert client.post('/api/alerts/snooze?minutes=0',headers=OWNER).status_code == 200
    response = client.get('/api/timeline',params={'start':now-500,'end':now+3600},headers=OWNER).json()
    assert len(response['coverage']) == 3
    assert all(period['end'] <= time.time() for period in response['coverage'])
    assert len(response['snoozes']) == 1
    assert response['snoozes'][0]['end'] < now+10
    store.execute('INSERT INTO alert_snoozes(start,end) VALUES(?,?)',(now-10*86400,now-9*86400))
    store.record_coverage('camera-1','online',now-10*86400)
    store.cleanup()
    assert all(period['end'] > now-7*86400 for period in store.rows('SELECT * FROM camera_periods'))
    assert len(store.rows('SELECT * FROM alert_snoozes')) == 1


def test_timeline_alert_marker_hides_subscription_data(setup):
    client, store, _ = setup
    now = time.time()
    eid = store.record_event('camera-1','unknown',1,[],b'synthetic',[],now=now-10)
    device, _ = client.app.state.accounts.owner_device()
    store.execute('INSERT INTO push_subscriptions(device_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)',(device,'https://example.invalid/synthetic','synthetic-key','synthetic-auth',now))
    store.execute("INSERT INTO push_jobs(device_id,event_id,payload,created_at,next_try,state,sent_at) VALUES(?,?,?,?,?,'sent',?)",(device,eid,'{}',now-5,now,now))
    result = client.get('/api/timeline',params={'start':now-60,'end':now+60},headers=OWNER).json()
    assert result['notifications'] == [{'event_id':eid,'at':now}]
    assert 'endpoint' not in json.dumps(result)
    assert 'synthetic-key' not in json.dumps(result)


def test_no_visible_face_queues_after_repeated_person_observations(setup, jpeg, monkeypatch):
    client, store, engine = setup
    queued=[]
    monkeypatch.setattr(client.app.state.push,'enqueue_event',lambda camera,event:queued.append(event))
    engine.people=[{'box':[10,10,50,100],'score':0.9}]
    assert client.post('/api/ingest/camera-1/frame',content=jpeg,headers=COLLECTOR).json()['state']=='unavailable'
    assert queued==[]
    time.sleep(.85)
    response=client.post('/api/ingest/camera-1/frame',content=jpeg,headers=COLLECTOR)
    assert response.status_code==200
    assert queued==[response.json()['event_id']]


def test_every_event_gets_a_clip_with_auth_playback_and_retention(setup, jpeg):
    client, store, engine = setup
    engine.people = [{'box': [10, 10, 80, 160]}]
    eid = client.post('/api/ingest/camera-1/frame', content=jpeg, headers=COLLECTOR).json()['event_id']
    event = store.events()[0]
    pending = client.get('/api/ingest/camera-1/recordings', headers=COLLECTOR).json()
    assert pending['requests'] == [{'event_id': eid, 'start': event['first_seen'], 'end': event['last_seen'], 'through': None}]
    assert client.get(f'/api/events/{eid}/recording', headers=OWNER).json() == {'ready': False, 'complete': False, 'preparing': True}
    video = b'\x00\x00\x00\x18ftypisom' + b'synthetic-video' * 200
    path = f"/api/ingest/camera-1/recordings/{eid}/video?through={event['last_seen']}&complete=true"
    assert client.post(path, content=video, headers=OWNER).status_code == 401
    assert client.post(path.replace('camera-1', 'other'), content=video, headers=COLLECTOR).status_code == 404
    assert client.post(path, content=b'bad', headers=COLLECTOR).status_code == 422
    assert client.post(path, content=video, headers=COLLECTOR).status_code == 200
    assert client.get(f'/api/events/{eid}/video', headers=COLLECTOR).status_code == 401
    assert client.get(f'/api/events/{eid}/recording', headers=OWNER).json() == {'ready': True, 'complete': True, 'preparing': False}
    response = client.get(f'/api/events/{eid}/video', headers={**OWNER, 'Range': 'bytes=0-11'})
    assert response.status_code == 206
    assert response.content == video[:12]
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/ingest/camera-1/recordings', headers=COLLECTOR).json()['requests'] == []
    assert client.post(path, content=video, headers={**COLLECTOR, 'Content-Length': '64000001'}).status_code == 413
    store.delete_event(eid)
    assert client.get(f'/api/events/{eid}/video', headers=OWNER).status_code == 404


def test_recorder_reports_footage_and_status_shows_it(setup):
    client, store, engine = setup
    assert client.get('/api/status', headers=OWNER).json()['recorder'] == {'configured': False, 'stopped': []}
    assert client.get(f'/api/ingest/camera-1/recordings?newest={time.time()}', headers=COLLECTOR).status_code == 200
    assert client.get('/api/status', headers=OWNER).json()['recorder'] == {'configured': True, 'stopped': []}
    store.execute('UPDATE recorder_status SET stream_at=?', (time.time()-400,))
    assert client.get('/api/status', headers=OWNER).json()['recorder']['stopped'] == ['Test camera']


def test_no_recorder_means_no_preparing_message(setup, jpeg):
    client, store, engine = setup
    engine.people = [{'box': [10, 10, 80, 160]}]
    eid = client.post('/api/ingest/camera-1/frame', content=jpeg, headers=COLLECTOR).json()['event_id']
    assert client.get(f'/api/events/{eid}/recording', headers=OWNER).json()['preparing'] is False


def test_a_visit_that_continues_asks_for_a_longer_clip_up_to_the_limit(setup, jpeg):
    client, store, engine = setup
    engine.people = [{'box': [10, 10, 80, 160]}]
    eid = client.post('/api/ingest/camera-1/frame', content=jpeg, headers=COLLECTOR).json()['event_id']
    event = store.events()[0]
    video = b'\x00\x00\x00\x18ftypisom' + b'x' * 50
    assert client.post(f"/api/ingest/camera-1/recordings/{eid}/video?through={event['last_seen']}", content=video, headers=COLLECTOR).status_code == 200
    assert client.get(f'/api/events/{eid}/recording', headers=OWNER).json()['complete'] is False
    store.execute('UPDATE events SET last_seen=? WHERE id=?', (event['first_seen']+60, eid))
    assert client.get('/api/ingest/camera-1/recordings', headers=COLLECTOR).json()['requests'][0]['through'] == event['last_seen']
    start = time.time()-400
    store.execute('UPDATE events SET first_seen=?,last_seen=?,clip_through=? WHERE id=?', (start, time.time(), start+300, eid))
    assert client.get('/api/ingest/camera-1/recordings', headers=COLLECTOR).json()['requests'] == []
