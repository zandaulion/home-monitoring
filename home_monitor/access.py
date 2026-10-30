"""Console-compatible device invitations. No token is embedded in the PWA."""
from collections import deque
from datetime import datetime, timezone
import hashlib
import secrets
import time

COOKIE = "__Host-home_monitor"
MAX_AGE = 400 * 86400
TTL_DAYS = 7
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value else None


def normalize_code(value):
    value = "".join(c for c in value.upper() if c not in " -\t\r\n")
    if len(value) != 12 or any(c not in ALPHABET for c in value):
        raise ValueError("Enter the 12-character invitation code.")
    return "-".join(value[i:i+4] for i in range(0, 12, 4))


class Access:
    def __init__(self, store, base_url):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.attempts = deque()
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS access_devices(
                    id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    label TEXT NOT NULL, created_at REAL NOT NULL,
                    last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS access_invites(
                    id INTEGER PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
                    code_plain TEXT, label TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL, used_at REAL, revoked INTEGER NOT NULL DEFAULT 0,
                    device_id INTEGER REFERENCES access_devices(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS push_subscriptions(
                    device_id INTEGER PRIMARY KEY REFERENCES access_devices(id) ON DELETE CASCADE,
                    endpoint TEXT UNIQUE NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                    created_at REAL NOT NULL, last_sent REAL, last_error TEXT
                );
            """)

    def device(self, token):
        if not token or len(token) > 200:
            return None
        with self.store.lock:
            rows = self.store.rows("SELECT id,label,last_seen FROM access_devices WHERE token_hash=? AND revoked=0 AND created_at>?", (digest(token), time.time()-MAX_AGE))
            if not rows:
                return None
            device = rows[0]
            if not device["last_seen"] or time.time()-device["last_seen"] > 60:
                self.store.execute("UPDATE access_devices SET last_seen=? WHERE id=?", (time.time(), device["id"]))
            return device

    def create_invite(self, label):
        code = "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))
        now = time.time()
        self.store.execute("INSERT INTO access_invites(code_hash,code_plain,label,created_at,expires_at) VALUES(?,?,?,?,?)", (digest(code), code, label, now, now+TTL_DAYS*86400))
        return {"code": code, "url": self.base_url+"/#invite="+code, "expires_in_days": TTL_DAYS, "label": label}

    def invites(self):
        self.store.execute("UPDATE access_invites SET code_plain=NULL WHERE expires_at<? OR revoked=1", (time.time(),))
        result = self.store.rows("SELECT id,label,created_at,expires_at,used_at,revoked,device_id,code_plain FROM access_invites ORDER BY id DESC")
        for row in result:
            for key in ("created_at", "expires_at", "used_at"):
                row[key] = iso(row[key])
            row["code"] = row.pop("code_plain") if not row["used_at"] and not row["revoked"] else None
            row.pop("code_plain", None)
            row["url"] = self.base_url+"/#invite="+row["code"] if row["code"] else None
        return result

    def devices(self):
        rows = self.store.rows("SELECT id,label,created_at,last_seen,revoked FROM access_devices ORDER BY id DESC")
        for row in rows:
            row["created_at"], row["last_seen"] = iso(row["created_at"]), iso(row["last_seen"])
            row["revoked"] = bool(row["revoked"])
            row["has_push"] = bool(self.store.rows("SELECT device_id FROM push_subscriptions WHERE device_id=?", (row["id"],)))
        return rows

    def redeem(self, raw):
        with self.store.lock:
            now = time.time()
            while self.attempts and now-self.attempts[0] > 60:
                self.attempts.popleft()
            if len(self.attempts) >= 20:
                raise OverflowError("Too many attempts. Wait a minute before trying again.")
            self.attempts.append(now)
            code = normalize_code(raw)
            rows = self.store.rows("SELECT * FROM access_invites WHERE code_hash=?", (digest(code),))
            if not rows:
                raise ValueError("This invitation is not valid.")
            row = rows[0]
            if row["revoked"] or row["expires_at"] <= now:
                raise ValueError("This invitation was cancelled or has expired. Ask for a new one.")
            token = secrets.token_urlsafe(40)
            with self.store.db:
                if row["used_at"]:
                    if now-row["used_at"] > 3600 or not row["device_id"]:
                        raise ValueError("This invitation has already been used.")
                    device = self.store.rows("SELECT revoked FROM access_devices WHERE id=?", (row["device_id"],))
                    if not device or device[0]["revoked"]:
                        raise ValueError("This device's access was revoked. Ask for a new invitation.")
                    device_id = row["device_id"]
                    self.store.db.execute("UPDATE access_devices SET token_hash=?,last_seen=? WHERE id=?", (digest(token), now, device_id))
                    self.store.db.execute("DELETE FROM push_subscriptions WHERE device_id=?", (device_id,))
                else:
                    cursor = self.store.db.execute("INSERT INTO access_devices(token_hash,label,created_at,last_seen) VALUES(?,?,?,?)", (digest(token), row["label"], now, now))
                    device_id = cursor.lastrowid
                    self.store.db.execute("UPDATE access_invites SET used_at=?,device_id=?,code_plain=NULL WHERE id=?", (now, device_id, row["id"]))
            return device_id, token

    def owner_device(self):
        token = secrets.token_urlsafe(40)
        now = time.time()
        cursor = self.store.execute("INSERT INTO access_devices(token_hash,label,created_at,last_seen) VALUES(?,?,?,?)", (digest(token), "Owner device", now, now))
        return cursor.lastrowid, token
