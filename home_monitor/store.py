import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid


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
            CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen REAL, error TEXT, inference_ms INTEGER, width INTEGER, height INTEGER);
            CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, embedding TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, camera_id TEXT NOT NULL REFERENCES cameras(id), first_seen REAL NOT NULL, last_seen REAL NOT NULL, state TEXT NOT NULL, person_count INTEGER NOT NULL, faces TEXT NOT NULL, corrected INTEGER NOT NULL DEFAULT 0, image_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_recent ON events(last_seen DESC);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        for camera in cameras:
            self.db.execute("INSERT OR IGNORE INTO cameras(id,name) VALUES(?,?)", (camera["id"], camera["name"]))
        for key, value in {"retention_days": 7, "storage_limit_mb": 2048, "recognition_threshold": 0.50, "monitoring_enabled": True, "alert_cooldown_minutes": 5, "alerts_snoozed_until": 0}.items():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, json.dumps(value)))
        self.db.commit()

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
        names = {p["id"]: p["name"] for p in self.people()}
        for row in rows:
            row["faces"] = json.loads(row["faces"])
            for face in row["faces"]:
                face.pop("embedding", None)
                face["name"] = names.get(face.get("person_id"))
        return rows

    def record_event(self, camera_id, state, people_count, faces, image, face_images, now=None):
        now = now or time.time()
        signature = lambda items: sorted((f.get("person_id") or ("unknown" if f["usable"] else "unavailable")) for f in items)
        with self.lock:
            recent = self.rows("SELECT * FROM events WHERE camera_id=? ORDER BY last_seen DESC LIMIT 1", (camera_id,))
            merge = bool(recent and not recent[0]["corrected"] and now - recent[0]["last_seen"] < 45 and recent[0]["state"] == state and signature(json.loads(recent[0]["faces"])) == signature(faces))
            event_id = recent[0]["id"] if merge else uuid.uuid4().hex
            folder = self.root / "events" / event_id
            if merge and now - recent[0]["image_at"] < 15:
                self.execute("UPDATE events SET last_seen=?,person_count=? WHERE id=?", (now, people_count, event_id))
                return event_id
            folder.mkdir(exist_ok=True, mode=0o700)
            write_image(folder / "snapshot.jpg", image)
            for i, crop in enumerate(face_images):
                write_image(folder / f"face-{i}.jpg", crop)
            if merge:
                self.execute("UPDATE events SET last_seen=?,person_count=?,faces=?,image_at=? WHERE id=?", (now, people_count, json.dumps(faces), now, event_id))
            else:
                self.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (event_id, camera_id, now, now, state, people_count, json.dumps(faces), 0, now))
            return event_id

    def delete_event(self, event_id):
        self.execute("DELETE FROM events WHERE id=?", (event_id,))
        shutil.rmtree(self.root / "events" / event_id, ignore_errors=True)

    def cleanup(self):
        """Expire event media and metadata together; enrolled faces are independent."""
        with self.lock:
            settings = self.settings()
            cutoff = time.time() - settings["retention_days"] * 86400
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
