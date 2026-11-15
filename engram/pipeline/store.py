"""SQLite record of jobs, their stages and progress events. Artifacts live on disk.

Every call opens its own short-lived connection, so stages running in worker threads and
the web app's read-only pages can use the store at the same time (WAL mode).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

JobStatus = Literal["queued", "running", "done", "failed"]
StageStatus = Literal["pending", "running", "done", "failed", "blocked"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_sha TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_source ON jobs(source_sha);
CREATE TABLE IF NOT EXISTS stages (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  input_key TEXT,
  output_digest TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at REAL,
  finished_at REAL,
  detail TEXT,
  error TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  cached INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, name)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  stage TEXT,
  at REAL NOT NULL,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_job ON events(job_id, id);
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL,
  filename TEXT NOT NULL,
  title TEXT NOT NULL,
  size INTEGER NOT NULL,
  content_type TEXT NOT NULL,
  created_at REAL NOT NULL,
  job_id TEXT,
  code_id TEXT NOT NULL DEFAULT ''
);
"""

UPLOAD_SECONDS = 3600  # an upload link is good for an hour


@dataclass(frozen=True)
class Job:
    id: str
    title: str
    source_path: Path
    source_sha: str
    status: JobStatus
    error: str | None
    created_at: float
    listed: bool = False  # shown in the home page's sample courses
    source_key: str | None = None  # the upload's key in R2; source_path is then a local cache


@dataclass(frozen=True)
class Upload:
    id: str
    key: str
    filename: str
    title: str
    size: int
    content_type: str
    created_at: float
    job_id: str | None
    code_id: str = ""  # which access code paid for it (a hash, never the code)


@dataclass(frozen=True)
class StageRow:
    name: str
    status: StageStatus
    input_key: str | None
    output_digest: str | None
    attempts: int
    started_at: float | None
    finished_at: float | None
    detail: str | None
    error: str | None
    cost_usd: float
    cached: bool

    @property
    def seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
            if "listed" not in columns:  # added in M6
                db.execute("ALTER TABLE jobs ADD COLUMN listed INTEGER NOT NULL DEFAULT 0")
            if "source_key" not in columns:  # added with R2 storage
                db.execute("ALTER TABLE jobs ADD COLUMN source_key TEXT")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(uploads)")}
            if "code_id" not in columns:  # added with daily limits (M8)
                db.execute("ALTER TABLE uploads ADD COLUMN code_id TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # ---- jobs ------------------------------------------------------------
    def create_job(
        self, title: str, source_path: Path, source_sha: str, source_key: str | None = None
    ) -> Job:
        now = time.time()
        job_id = uuid.uuid4().hex[:12]
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs (id, title, source_path, source_sha, status, error, created_at,"
                " updated_at, source_key) VALUES (?, ?, ?, ?, 'queued', NULL, ?, ?, ?)",
                (job_id, title, str(source_path), source_sha, now, now, source_key),
            )
        return self.job(job_id)

    # ---- uploads: an access code buys a one-time upload slot ----------------
    def create_upload(
        self, key: str, filename: str, title: str, size: int, content_type: str,
        code_id: str = "",
    ) -> Upload:  # fmt: skip
        upload_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO uploads (id, key, filename, title, size, content_type, created_at,"
                " job_id, code_id) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (upload_id, key.format(id=upload_id), filename, title, size, content_type,
                 time.time(), code_id),
            )  # fmt: skip
        found = self.upload(upload_id)
        assert found is not None
        return found

    def upload(self, upload_id: str) -> Upload | None:
        """A pending, unexpired upload, or None."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM uploads WHERE id = ? AND created_at > ?",
                (upload_id, time.time() - UPLOAD_SECONDS),
            ).fetchone()
        return Upload(**dict(row)) if row else None

    def uploads_since(self, code_id: str, since: float) -> list[float]:
        """When each finished upload paid for by this code started, oldest first."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT created_at FROM uploads WHERE code_id = ? AND job_id IS NOT NULL"
                " AND created_at > ? ORDER BY created_at",
                (code_id, since),
            ).fetchall()
        return [r["created_at"] for r in rows]

    def finish_upload(self, upload_id: str, job_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE uploads SET job_id = ? WHERE id = ?", (job_id, upload_id))

    def job(self, job_id: str) -> Job:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job(row)

    def find_by_source(self, source_sha: str) -> Job | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE source_sha = ? ORDER BY created_at DESC LIMIT 1",
                (source_sha,),
            ).fetchone()
        return _job(row) if row else None

    def jobs(self) -> list[Job]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [_job(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        """Forgets a job: its stages and events too. Upload records stay, as they count
        toward their access code's daily limit."""
        with self._connect() as db:
            db.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            db.execute("DELETE FROM stages WHERE job_id = ?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def next_queued(self) -> Job | None:
        """The oldest job waiting for the worker."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return _job(row) if row else None

    def requeue_running(self, busy: Callable[[str], bool] = lambda _: False) -> list[str]:
        """Jobs a stopped process left 'running' go back in the queue (they resume).

        `busy` says which are still being run by a live process: those are left alone."""
        with self._connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
            stale = [r["id"] for r in rows if not busy(r["id"])]
            db.executemany("UPDATE jobs SET status = 'queued' WHERE id = ? AND status = 'running'",
                           [(i,) for i in stale])  # fmt: skip
        return stale

    def set_source_key(self, job_id: str, key: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET source_key = ? WHERE id = ?", (key, job_id))

    def set_listed(self, job_id: str, listed: bool) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET listed = ? WHERE id = ?", (int(listed), job_id))

    def set_job_status(self, job_id: str, status: JobStatus, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, time.time(), job_id),
            )

    # ---- stages ----------------------------------------------------------
    def stages(self, job_id: str) -> dict[str, StageRow]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM stages WHERE job_id = ?", (job_id,)).fetchall()
        return {r["name"]: _stage(r) for r in rows}

    def update_stage(self, job_id: str, name: str, **fields: Any) -> None:
        if not fields:
            return
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO stages (job_id, name, status) VALUES (?, ?, 'pending')",
                (job_id, name),
            )
            assignments = ", ".join(f"{k} = ?" for k in fields)
            db.execute(
                f"UPDATE stages SET {assignments} WHERE job_id = ? AND name = ?",  # noqa: S608
                (*fields.values(), job_id, name),
            )

    def add_cost(self, job_id: str, name: str, usd: float) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE stages SET cost_usd = cost_usd + ? WHERE job_id = ? AND name = ?",
                (usd, job_id, name),
            )

    # ---- events ----------------------------------------------------------
    def add_event(self, job_id: str, stage: str | None, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO events (job_id, stage, at, message) VALUES (?, ?, ?, ?)",
                (job_id, stage, time.time(), message),
            )

    def events(self, job_id: str, after_id: int = 0) -> list[tuple[int, str | None, float, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, stage, at, message FROM events WHERE job_id = ? AND id > ? ORDER BY id",
                (job_id, after_id),
            ).fetchall()
        return [(r["id"], r["stage"], r["at"], r["message"]) for r in rows]


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        title=row["title"],
        source_path=Path(row["source_path"]),
        source_sha=row["source_sha"],
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        listed=bool(row["listed"]),
        source_key=row["source_key"],
    )


def _stage(row: sqlite3.Row) -> StageRow:
    return StageRow(
        name=row["name"],
        status=row["status"],
        input_key=row["input_key"],
        output_digest=row["output_digest"],
        attempts=row["attempts"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        detail=row["detail"],
        error=row["error"],
        cost_usd=row["cost_usd"],
        cached=bool(row["cached"]),
    )
