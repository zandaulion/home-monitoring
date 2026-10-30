import base64
import json
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from home_monitor.access import Access
from home_monitor.push import Push, validate_subscription
from home_monitor.store import Store


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/synthetic-test-endpoint"):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    return {"endpoint": endpoint, "keys": {"p256dh": encode(key), "auth": encode(b"0123456789abcdef")}}


@pytest.fixture
def push_setup(tmp_path):
    store = Store(tmp_path, [{"id": "camera-test", "name": "Synthetic camera"}])
    accounts = Access(store, "https://example.invalid")
    device_id, _ = accounts.owner_device()
    deliveries = []
    def sender(sub, payload):
        deliveries.append((sub, payload))
        return 201
    push = Push(store, "https://example.invalid", sender)
    # Exactly 16 bytes of Web Push authentication material.
    sub = subscription()
    sub["keys"]["auth"] = base64.urlsafe_b64encode(b"0123456789abcdef").rstrip(b"=").decode()
    push.subscribe(device_id, sub)
    yield store, accounts, push, device_id, deliveries
    store.db.close()


def event(store):
    store.execute("INSERT OR IGNORE INTO people VALUES('synthetic','Example Person',?)", (time.time(),))
    if not store.samples():
        store.save_sample("synthetic", [1.0, 0.0], b"synthetic-jpeg")
    return store.record_event("camera-test", "unknown", 1, [], b"synthetic-jpeg", [])


def test_durable_queue_deduplicates_and_sends_generic_payload(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    eid = event(store)
    push.enqueue_event("camera-test", eid)
    push.enqueue_event("camera-test", eid)
    assert len(store.rows("SELECT * FROM push_jobs")) == 1
    restored = Push(store, "https://example.invalid", push.sender)
    assert restored.public_key == push.public_key
    assert restored.deliver_one()
    assert len(deliveries) == 1
    text = json.dumps(deliveries[0][1])
    assert "Example Person" not in text and "camera-test" not in text and "image" not in text
    assert store.rows("SELECT state FROM push_jobs")[0]["state"] == "sent"
    assert not restored.deliver_one()


def test_revoked_device_cannot_receive_pending_alert(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    push.test(device_id)
    store.execute("UPDATE access_devices SET revoked=1 WHERE id=?", (device_id,))
    assert not push.deliver_one()
    assert not deliveries


def test_expired_subscription_is_removed(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    push.test(device_id)
    push.sender = lambda *_: 410
    assert push.deliver_one()
    assert not push.status(device_id)["subscribed"]
    assert not store.rows("SELECT * FROM push_jobs")


def test_transient_failure_retries_without_duplicate_sent_jobs(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    push.test(device_id)
    push.sender = lambda *_: 503
    push.deliver_one()
    row = store.rows("SELECT * FROM push_jobs")[0]
    assert row["state"] == "pending" and row["attempts"] == 1
    assert not push.deliver_one()
    store.execute("UPDATE push_jobs SET next_try=0")
    push.sender = lambda *_: 201
    push.deliver_one()
    assert store.rows("SELECT state FROM push_jobs")[0]["state"] == "sent"


def test_old_alerts_expire_instead_of_being_delivered(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    push.test(device_id)
    store.execute("UPDATE push_jobs SET created_at=?", (time.time()-121,))
    assert not push.deliver_one()
    assert not deliveries
    assert store.rows("SELECT state FROM push_jobs")[0]["state"] == "expired"


def test_disabling_or_correcting_an_event_cancels_pending_alert(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    eid = event(store)
    push.enqueue_event("camera-test", eid)
    store.execute("UPDATE events SET state='known' WHERE id=?", (eid,))
    push.deliver_one()
    assert not deliveries
    assert store.rows("SELECT state FROM push_jobs")[0]["state"] == "cancelled"


def test_unsubscribe_cascades_jobs(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    push.test(device_id)
    push.unsubscribe(device_id)
    assert not store.rows("SELECT * FROM push_jobs")
    assert not push.deliver_one()


def test_rebinding_removes_old_browser_subscription(push_setup):
    store, accounts, push, device_id, deliveries = push_setup
    invite = accounts.create_invite("Synthetic device")
    new_device, _ = accounts.redeem(invite["code"])
    sub = subscription("https://web.push.apple.com/synthetic")
    sub["keys"]["auth"] = base64.urlsafe_b64encode(b"0123456789abcdef").rstrip(b"=").decode()
    push.subscribe(new_device, sub)
    accounts.redeem(invite["code"])
    assert not push.status(new_device)["subscribed"]


def test_real_encryption_and_vapid_signing_without_external_network(push_setup, monkeypatch):
    import requests
    from pywebpush import webpush as real_webpush
    store, accounts, push, device_id, deliveries = push_setup
    outgoing = []
    class Transport:
        def post(self, *args, **kwargs):
            outgoing.append(kwargs)
            response = requests.Response()
            response.status_code = 201
            return response
    def encrypted_send(**kwargs):
        return real_webpush(**kwargs, requests_session=Transport())
    monkeypatch.setattr("home_monitor.push.webpush", encrypted_send)
    push.sender = push._send
    push.test(device_id)
    assert push.deliver_one()
    assert store.rows("SELECT state FROM push_jobs")[0]["state"] == "sent"
    assert outgoing and b"Notifications are working" not in outgoing[0]["data"]
    assert outgoing[0]["timeout"] == 6
    assert any(key.lower() == "authorization" for key in outgoing[0]["headers"])


def test_snooze_blocks_new_and_pending_alerts_then_expires(push_setup):
    store, _, push, _, deliveries = push_setup
    eid = event(store)
    push.enqueue_event('camera-test', eid)
    store.execute("UPDATE settings SET value=? WHERE key='alerts_snoozed_until'", (json.dumps(time.time()+3600),))
    assert push.deliver_one()
    assert not deliveries
    assert store.rows('SELECT state FROM push_jobs')[0]['state'] == 'cancelled'
    store.execute('DELETE FROM push_jobs')
    store.execute('DELETE FROM push_cooldown')
    push.enqueue_event('camera-test', eid)
    assert not store.rows('SELECT * FROM push_jobs')
    store.execute("UPDATE settings SET value=? WHERE key='alerts_snoozed_until'", (json.dumps(time.time()-1),))
    push.enqueue_event('camera-test', eid)
    assert push.deliver_one()
    assert len(deliveries) == 1


def test_configurable_cooldown_survives_restart(push_setup):
    store, _, push, _, _ = push_setup
    first = event(store)
    push.enqueue_event('camera-test', first)
    second = store.record_event('camera-test', 'unknown', 1, [], b'synthetic', [], now=time.time()+46)
    store.execute("UPDATE settings SET value='15' WHERE key='alert_cooldown_minutes'")
    store.execute('UPDATE push_cooldown SET last_queued=?', (time.time()-899,))
    restored = Push(store, 'https://example.invalid', push.sender)
    restored.enqueue_event('camera-test', second)
    assert len(store.rows('SELECT * FROM push_jobs')) == 1
    store.execute('UPDATE push_cooldown SET last_queued=?', (time.time()-901,))
    restored.enqueue_event('camera-test', second)
    assert len(store.rows('SELECT * FROM push_jobs')) == 2


def test_sent_alert_records_relay_acceptance_time(push_setup):
    store, _, push, _, _ = push_setup
    eid = event(store)
    push.enqueue_event('camera-test',eid)
    before = time.time()
    assert push.deliver_one()
    assert before <= store.rows('SELECT sent_at FROM push_jobs')[0]['sent_at'] <= time.time()
