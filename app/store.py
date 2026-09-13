"""SQLite queue shared safely by the web server and the isolated worker."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import uuid
from contextlib import contextmanager


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "jobs.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, text, settings):
        stamp = now()
        job = {"id": uuid.uuid4().hex, "input": text, "status": "queued", "stage": "queued", "progress": 0, "message": "等待处理", "title": "", "created_at": stamp, "updated_at": stamp, "error": None, "result": None, "settings": dict(settings), "cancel_requested": False}
        with self.connection() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?)", (job["id"], stamp, json.dumps(job, ensure_ascii=False)))
        return job

    def get(self, ident):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 200").fetchall()
        return [json.loads(row[0]) for row in rows]

    def next_queued(self):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY rowid ASC").fetchall()
        return next((job for job in (json.loads(row[0]) for row in rows) if job["status"] == "queued"), None)

    def update(self, ident, **fields):
        return self.update_if(ident, {}, **fields)

    def update_if(self, ident, expected, **fields):
        """Compare and update under one write lock; a stale attempt writes nothing."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            if any(job.get(key) != value for key, value in expected.items()):
                return None
            job.update(fields)
            job["updated_at"] = now()
            db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), ident))
        return job

    def claim(self, ident):
        return self.update_if(
            ident, {"status": "queued"}, status="running", stage="starting", progress=0,
            attempt_id=uuid.uuid4().hex, cancel_requested=False, error=None,
            message="开始处理",
        )

    def request_cancel(self, ident):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            if job["status"] in ("queued", "paused"):
                job.update(status="cancelled", stage="cancelled", cancel_requested=True,
                           message="已取消", error=None)
            elif job["status"] == "running":
                job.update(cancel_requested=True, message="正在取消")
            else:
                return job
            job["updated_at"] = now()
            db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), ident))
        return job

    def recover(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT payload FROM jobs").fetchall()
            for job in (json.loads(row[0]) for row in rows):
                if job["status"] == "running" and job.get("execution_backend") != "ssh":
                    job.update(status="interrupted", stage="interrupted", cancel_requested=True,
                               error="上次处理意外停止，点击重试可重新运行。", message="等待重试", updated_at=now())
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["id"]))


def public_job(job):
    return {key: value for key, value in job.items() if key not in ("settings", "cancel_requested")}
