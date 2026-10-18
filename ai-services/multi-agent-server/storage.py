import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from contracts import WorkflowError

logger = logging.getLogger(__name__)


class InstanceLock:
    def __init__(self, directory):
        self.path = Path(directory) / "server.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another server owns this data directory") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "workflows.db"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY, record TEXT NOT NULL, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    sequence INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_workflow ON history(workflow_id, sequence);
                CREATE INDEX IF NOT EXISTS events_workflow ON events(workflow_id, sequence);
            """)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Workflow database integrity check failed")
            count = db.execute("SELECT count(*) FROM workflows").fetchone()[0]
            if not count and (self.directory / "workflow_history.jsonl").exists():
                raise RuntimeError(
                    "Legacy JSONL history detected: use a fresh MULTI_AGENT_LOG_DIR; retain old logs as evidence"
                )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, workflow_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT record, revision FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
        if not row:
            raise WorkflowError(404, "Unknown workflow", "workflow_not_found")
        return json.loads(row[0]), row[1]

    def save(self, record, events, revision=None):
        encoded = json.dumps(record, allow_nan=False)
        with self.connect() as db:
            if revision is None:
                db.execute(
                    "INSERT INTO workflows VALUES (?, ?, 0)",
                    (record["workflow_id"], encoded),
                )
            else:
                updated = db.execute(
                    "UPDATE workflows SET record=?, revision=revision+1 WHERE id=? AND revision=?",
                    (encoded, record["workflow_id"], revision),
                )
                if updated.rowcount != 1:
                    raise WorkflowError(
                        409,
                        "Workflow changed; reload before retrying",
                        "workflow_conflict",
                    )
            db.execute(
                "INSERT INTO history(workflow_id, record) VALUES (?, ?)",
                (record["workflow_id"], encoded),
            )
            for event in events:
                db.execute(
                    "INSERT INTO events(workflow_id, record) VALUES (?, ?)",
                    (record["workflow_id"], json.dumps(event)),
                )

    def all(self, feature=None):
        with self.connect() as db:
            records = [
                json.loads(row[0])
                for row in db.execute("SELECT record FROM workflows ORDER BY rowid")
            ]
        return [r for r in records if feature is None or r["feature"] == feature]

    def event_count(self, feature=None):
        with self.connect() as db:
            if feature is None:
                return db.execute("SELECT count(*) FROM events").fetchone()[0]
            return db.execute(
                "SELECT count(*) FROM events e JOIN workflows w ON e.workflow_id=w.id WHERE json_extract(w.record, '$.feature')=?",
                (feature,),
            ).fetchone()[0]

    def export(self, workflow_id=None):
        with self.connect() as db:
            db.execute("BEGIN")
            result = {}
            for table, filename in (
                ("history", "workflow_history.jsonl"),
                ("events", "coordination_audit.jsonl"),
            ):
                query = f"SELECT record FROM {table}"
                params = ()
                if workflow_id is not None:
                    query += " WHERE workflow_id=?"
                    params = (workflow_id,)
                result[filename] = [
                    json.loads(row[0])
                    for row in db.execute(query + " ORDER BY sequence", params)
                ]
        return result
