"""SQLite persistence: decision and audit event always commit together."""

from contextlib import contextmanager
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4


REVIEW_NOTICE = "The creator has contested this assessment. It is under review."


class ContentNotFound(Exception):
    pass


class AppealExists(Exception):
    pass


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    with connect(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS contents (
                content_id TEXT PRIMARY KEY,
                creator_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('classified', 'under_review')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decision_json TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id TEXT NOT NULL REFERENCES contents(content_id),
                event_type TEXT NOT NULL CHECK (event_type IN ('decision', 'appeal')),
                timestamp TEXT NOT NULL,
                event_json TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL UNIQUE REFERENCES contents(content_id),
                creator_reasoning TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS verification_requests (
                request_id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL REFERENCES contents(content_id),
                draft TEXT NOT NULL, explanation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                reviewed_at TEXT, reviewer TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS certificates (
                certificate_id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL UNIQUE REFERENCES contents(content_id),
                request_id TEXT NOT NULL UNIQUE REFERENCES verification_requests(request_id),
                issued_at TEXT NOT NULL, reviewer TEXT NOT NULL,
                text_sha256 TEXT NOT NULL
            )
        """)


def save_decision(path, decision):
    event = {
        **decision,
        "event_type": "decision",
        "timestamp": decision["created_at"],
        "appeal_id": None,
        "original_decision_event_id": None,
        "appeal_reasoning": None,
    }
    with connect(path) as db:
        db.execute(
            "INSERT INTO contents VALUES (?, ?, ?, ?, ?, ?)",
            (
                decision["content_id"], decision["creator_id"], decision["status"],
                decision["created_at"], decision["created_at"],
                json.dumps(decision, allow_nan=False),
            ),
        )
        db.execute(
            """INSERT INTO audit_events
               (content_id, event_type, timestamp, event_json) VALUES (?, ?, ?, ?)""",
            (decision["content_id"], "decision", decision["created_at"],
             json.dumps(event, allow_nan=False)),
        )


def read_events(path, limit, offset):
    with connect(path) as db:
        rows = db.execute(
            "SELECT event_id, event_json FROM audit_events ORDER BY event_id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [{**json.loads(payload), "event_id": event_id} for event_id, payload in rows]


def get_content(path, content_id):
    with connect(path) as db:
        row = db.execute(
            """SELECT c.decision_json, c.status, c.updated_at,
                      a.appeal_id, a.creator_reasoning, a.created_at
               FROM contents c LEFT JOIN appeals a ON a.content_id = c.content_id
               WHERE c.content_id = ?""", (content_id,),
        ).fetchone()
    if row is None:
        raise ContentNotFound()
    decision, status, updated_at, appeal_id, reasoning, appealed_at = row
    from .provenance import get_certificate
    return {
        "certificate": get_certificate(path, content_id),
        **json.loads(decision), "status": status, "updated_at": updated_at,
        "appeal": {"appeal_id": appeal_id, "creator_reasoning": reasoning,
                   "created_at": appealed_at} if appeal_id else None,
        "review_notice": REVIEW_NOTICE if status == "under_review" else None,
    }


def create_appeal(path, content_id, reasoning):
    """Serialize competing appeals and commit status, reasoning and audit together."""
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT decision_json FROM contents WHERE content_id = ?", (content_id,)).fetchone()
        if row is None:
            raise ContentNotFound()
        if db.execute("SELECT 1 FROM appeals WHERE content_id = ?", (content_id,)).fetchone():
            raise AppealExists()
        original = db.execute(
            "SELECT event_id FROM audit_events WHERE content_id = ? AND event_type = 'decision' ORDER BY event_id LIMIT 1",
            (content_id,),
        ).fetchone()
        if original is None:
            raise sqlite3.DatabaseError("Missing original decision audit event")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        appeal_id = str(uuid4())
        db.execute("INSERT INTO appeals VALUES (?, ?, ?, ?)", (appeal_id, content_id, reasoning, now))
        db.execute("UPDATE contents SET status = 'under_review', updated_at = ? WHERE content_id = ?", (now, content_id))
        event = {
            **json.loads(row[0]), "event_type": "appeal", "timestamp": now,
            "status": "under_review", "appeal_id": appeal_id,
            "original_decision_event_id": original[0], "appeal_reasoning": reasoning,
        }
        db.execute(
            "INSERT INTO audit_events (content_id, event_type, timestamp, event_json) VALUES (?, ?, ?, ?)",
            (content_id, "appeal", now, json.dumps(event, allow_nan=False)),
        )
    return {"appeal_id": appeal_id, "content_id": content_id,
            "status": "under_review", "created_at": now,
            "message": "Appeal received. This assessment is under review."}
