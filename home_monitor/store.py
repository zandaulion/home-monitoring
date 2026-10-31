import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

MAX_PHOTOS = 5
PHOTO_SPACING_SECONDS = 2


def write_image(path, data):
    """Readers see a complete frame even while the next snapshot arrives."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
        file.write(data)
    temporary.replace(path)


class Store:
    def __init__(self, root: Path, cameras):
        self.root = root
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "events").mkdir(exist_ok=True, mode=0o700)
        (root / "people").mkdir(exist_ok=True, mode=0o700)
        (root / "latest").mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "monitor.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen REAL, error TEXT, inference_ms INTEGER, width INTEGER, height INTEGER, last_frame REAL);
            CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, embedding TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, camera_id TEXT NOT NULL REFERENCES cameras(id), first_seen REAL NOT NULL, last_seen REAL NOT NULL, state TEXT NOT NULL, person_count INTEGER NOT NULL, faces TEXT NOT NULL, corrected INTEGER NOT NULL DEFAULT 0, image_at REAL NOT NULL, photos TEXT NOT NULL DEFAULT '[]', clip_through REAL, clip_complete INTEGER NOT NULL DEFAULT 0, peak_score REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS events_recent ON events(last_seen DESC);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS camera_periods(id INTEGER PRIMARY KEY, camera_id TEXT NOT NULL REFERENCES cameras(id), state TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS camera_periods_recent ON camera_periods(camera_id,end DESC);
            CREATE TABLE IF NOT EXISTS alert_snoozes(id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL);
        """)
        for table, column, definition in (("cameras", "last_frame", "REAL"), ("events", "photos", "TEXT NOT NULL DEFAULT '[]'"), ("events", "clip_through", "REAL"), ("events", "clip_complete", "INTEGER NOT NULL DEFAULT 0"), ("events", "peak_score", "REAL NOT NULL DEFAULT 0")):
            if column not in {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        for camera in cameras:
            self.db.execute("INSERT OR IGNORE INTO cameras(id,name) VALUES(?,?)", (camera["id"], camera["name"]))
        for key, value in {"retention_days": 7, "storage_limit_mb": 2048, "recognition_threshold": 0.50, "monitoring_enabled": True, "alert_cooldown_minutes": 5, "alerts_snoozed_until": 0, "recognition_enabled": True, "alert_mode": "always", "alert_schedule_start": "22:00", "alert_schedule_end": "06:00", "alert_timezone": "UTC", "away_mode": False, "auto_away": False, "offline_alerts": True, "alert_vehicles": False, "alert_animals": False, "presence_home": None, "presence_at": 0}.items():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, json.dumps(value)))
        until = self.settings()["alerts_snoozed_until"]
        now = time.time()
        if until > now and not self.rows("SELECT id FROM alert_snoozes WHERE end>?", (now,)):
            # On upgrade, only the remaining snooze is known, not its original start.
            self.db.execute("INSERT INTO alert_snoozes(start,end) VALUES(?,?)", (now, until))
        self.db.commit()

    def record_coverage(self, camera_id, state, now=None):
        """Heartbeats expire after 100s; gaps remain unknown, never assumed quiet."""
        now = time.time() if now is None else now
        with self.lock:
            recent = self.rows("SELECT * FROM camera_periods WHERE camera_id=? ORDER BY end DESC LIMIT 1", (camera_id,))
            if recent and recent[0]["state"] == state and recent[0]["end"] >= now:
                self.execute("UPDATE camera_periods SET end=? WHERE id=?", (now + 100, recent[0]["id"]))
            else:
                if recent and recent[0]["end"] > now:
                    self.execute("UPDATE camera_periods SET end=? WHERE id=?", (now, recent[0]["id"]))
                self.execute("INSERT INTO camera_periods(camera_id,state,start,end) VALUES(?,?,?,?)", (camera_id, state, now, now + 100))

    def event_details(self, ids):
        if not ids:
            return []
        rows = self.rows("SELECT * FROM events WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY last_seen DESC", ids)
        return self.describe_events(rows)

    def describe_events(self, rows):
        names = {p["id"]: p["name"] for p in self.people()}
        for row in rows:
            row["faces"] = json.loads(row["faces"])
            row["photos"] = sorted(json.loads(row["photos"]), key=lambda photo: photo["at"])
            for face in row["faces"]:
                face.pop("embedding", None)
                face["name"] = names.get(face.get("person_id"))
        return rows

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self.lock, self.db:
            return self.db.execute(sql, params)

    def settings(self):
        return {row["key"]: json.loads(row["value"]) for row in self.rows("SELECT * FROM settings")}

    def samples(self):
        return [{**row, "embedding": json.loads(row["embedding"])} for row in self.rows("SELECT id,person_id,embedding FROM samples")]

    def people(self):
        return self.rows("SELECT p.*,count(s.id) AS sample_count,(SELECT id FROM samples WHERE person_id=p.id ORDER BY created LIMIT 1) AS cover FROM people p LEFT JOIN samples s ON p.id=s.person_id GROUP BY p.id ORDER BY p.name COLLATE NOCASE")

    def save_sample(self, person_id, embedding, crop):
        if not self.rows("SELECT id FROM people WHERE id=?", (person_id,)):
            raise ValueError("Person not found")
        count = self.rows("SELECT count(*) AS n FROM samples WHERE person_id=?", (person_id,))[0]["n"]
        if count >= 50:
            raise ValueError("This person already has 50 face samples. Remove one before adding another.")
        sid = uuid.uuid4().hex
        relative = f"people/{sid}.jpg"
        write_image(self.root / relative, crop)
        self.execute("INSERT INTO samples VALUES(?,?,?,?,?)", (sid, person_id, json.dumps(embedding), relative, time.time()))
        return sid

    def events(self, camera=None, state=None, before=None, limit=60):
        clauses, params = ["1=1"], []
        for field, value in (("camera_id", camera), ("state", state)):
            if value:
                clauses.append(f"{field}=?")
                params.append(value)
        if before:
            clauses.append("last_seen<?")
            params.append(before)
        params.append(limit)
        rows = self.rows("SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY last_seen DESC LIMIT ?", params)
        return self.describe_events(rows)

    @staticmethod
    def photo_slot(photos, now, score):
        """Keep a spread of frames; once full, only a better frame replaces one."""
        if photos and now - max(photo["at"] for photo in photos) < PHOTO_SPACING_SECONDS:
            return None
        if len(photos) < MAX_PHOTOS:
            return min(set(range(MAX_PHOTOS)) - {photo["n"] for photo in photos})
        weakest = min(photos, key=lambda photo: photo["score"])
        return weakest["n"] if score >= weakest["score"] else None

    def record_event(self, camera_id, state, people_count, faces, image, face_images, now=None, score=0.0, peak_score=0.0):
        now = now or time.time()
        signature = lambda items: sorted((f.get("person_id") or ("unknown" if f["usable"] else "unavailable")) for f in items)
        with self.lock:
            recent = self.rows("SELECT * FROM events WHERE camera_id=? ORDER BY last_seen DESC LIMIT 1", (camera_id,))
            merge = bool(recent and not recent[0]["corrected"] and now - recent[0]["last_seen"] < 45 and recent[0]["state"] == state and signature(json.loads(recent[0]["faces"])) == signature(faces))
            event_id = recent[0]["id"] if merge else uuid.uuid4().hex
            folder = self.root / "events" / event_id
            if merge:
                self.execute("UPDATE events SET peak_score=MAX(peak_score,?) WHERE id=?", (peak_score, event_id))
            photos = json.loads(recent[0]["photos"]) if merge else []
            slot = self.photo_slot(photos, now, score)
            if slot is None:
                self.execute("UPDATE events SET last_seen=?,person_count=? WHERE id=?", (now, people_count, event_id))
                return event_id
            folder.mkdir(exist_ok=True, mode=0o700)
            write_image(folder / f"photo-{slot}.jpg", image)
            photos = [photo for photo in photos if photo["n"] != slot] + [{"n": slot, "at": now, "score": score}]
            best = not merge or score >= max(photo["score"] for photo in photos)
            if best:
                # Face crops always describe the frame shown as the main photo.
                for i, crop in enumerate(face_images):
                    write_image(folder / f"face-{i}.jpg", crop)
            if not merge:
                self.execute("INSERT INTO events(id,camera_id,first_seen,last_seen,state,person_count,faces,corrected,image_at,photos,peak_score) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (event_id, camera_id, now, now, state, people_count, json.dumps(faces), 0, now, json.dumps(photos), peak_score))
            elif best:
                self.execute("UPDATE events SET last_seen=?,person_count=?,faces=?,image_at=?,photos=? WHERE id=?", (now, people_count, json.dumps(faces), now, json.dumps(photos), event_id))
            else:
                self.execute("UPDATE events SET last_seen=?,person_count=?,photos=? WHERE id=?", (now, people_count, json.dumps(photos), event_id))
            return event_id

    def event_image(self, event_id):
        """The best photo; events from before photo selection keep one snapshot."""
        rows = self.rows("SELECT photos FROM events WHERE id=?", (event_id,))
        photos = json.loads(rows[0]["photos"]) if rows else []
        folder = self.root / "events" / event_id
        if not photos:
            return folder / "snapshot.jpg"
        return folder / f"photo-{max(photos, key=lambda photo: (photo['score'], photo['at']))['n']}.jpg"

    def delete_event(self, event_id):
        self.execute("DELETE FROM events WHERE id=?", (event_id,))
        shutil.rmtree(self.root / "events" / event_id, ignore_errors=True)

    def cleanup(self):
        """Expire event media and metadata together; enrolled faces are independent."""
        with self.lock:
            settings = self.settings()
            cutoff = time.time() - settings["retention_days"] * 86400
            self.execute("DELETE FROM camera_periods WHERE end<?", (cutoff,))
            self.execute("UPDATE camera_periods SET start=? WHERE start<?", (cutoff, cutoff))
            self.execute("DELETE FROM alert_snoozes WHERE end<?", (cutoff,))
            for row in self.rows("SELECT id FROM events WHERE last_seen<?", (cutoff,)):
                self.delete_event(row["id"])
            # Remove orphan media left by a crash between file and database writes.
            existing = {row["id"] for row in self.rows("SELECT id FROM events")}
            for folder in (self.root / "events").iterdir():
                if folder.is_dir() and folder.name not in existing:
                    shutil.rmtree(folder, ignore_errors=True)
            event_sizes = {}
            for folder in (self.root / "events").iterdir():
                if folder.is_dir():
                    event_sizes[folder.name] = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
            total = sum(event_sizes.values())
            limit = settings["storage_limit_mb"] * 1024 * 1024
            for row in self.rows("SELECT id FROM events ORDER BY last_seen"):
                if total <= limit:
                    break
                self.delete_event(row["id"])
                total -= event_sizes.get(row["id"], 0)
            # A single latest frame is overwritten per camera; hide stale images.
            for image in (self.root / "latest").glob("*.jpg"):
                if time.time() - image.stat().st_mtime > 3600:
                    image.unlink(missing_ok=True)
            return total
