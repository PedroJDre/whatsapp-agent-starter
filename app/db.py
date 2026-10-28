"""SQLite storage: conversations, messages, bookings and an event log for the dashboard."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    wa_id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'bot',          -- 'bot' or 'human'
    handoff_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT NOT NULL,
    role TEXT NOT NULL,                          -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    code TEXT PRIMARY KEY,
    wa_id TEXT NOT NULL,
    car_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    extras TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    total INTEGER NOT NULL,
    status TEXT NOT NULL,                        -- 'pending_confirmation', 'confirmed', 'cancelled'
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT,
    type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_wa ON messages (wa_id, id);
CREATE INDEX IF NOT EXISTS idx_bookings_car ON bookings (car_id, start_date, end_date);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = self._memory_conn or sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            if conn is not self._memory_conn:
                conn.close()

    # --- conversations -------------------------------------------------
    def get_conversation(self, wa_id: str, name: str | None = None) -> sqlite3.Row:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (wa_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (wa_id, name, now(), now()),
            )
            if name:
                conn.execute("UPDATE conversations SET name = ? WHERE wa_id = ?", (name, wa_id))
            return conn.execute("SELECT * FROM conversations WHERE wa_id = ?", (wa_id,)).fetchone()

    def set_status(self, wa_id: str, status: str, reason: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, handoff_reason = ?, updated_at = ? WHERE wa_id = ?",
                (status, reason, now(), wa_id),
            )

    def conversations_by_status(self, status: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM conversations WHERE status = ? ORDER BY updated_at DESC", (status,)
            ).fetchall()

    # --- messages ------------------------------------------------------
    def add_message(self, wa_id: str, role: str, content: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO messages (wa_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (wa_id, role, content, now()),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE wa_id = ?", (now(), wa_id))

    def history(self, wa_id: str, limit: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE wa_id = ? ORDER BY id DESC LIMIT ?", (wa_id, limit)
            ).fetchall()
        return list(reversed(rows))

    def mark_processed(self, message_id: str) -> bool:
        """Return False if this WhatsApp message id was already handled (Meta retries webhooks)."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, created_at) VALUES (?, ?)",
                (message_id, now()),
            )
            return cur.rowcount == 1

    # --- bookings ------------------------------------------------------
    def overlapping_bookings(self, car_id: str, start: date, end: date) -> int:
        """Active bookings for this car whose dates overlap [start, end)."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM bookings
                   WHERE car_id = ? AND status != 'cancelled' AND start_date < ? AND end_date > ?""",
                (car_id, end.isoformat(), start.isoformat()),
            ).fetchone()
        return row[0]

    def add_booking(self, *, code, wa_id, car_id, start, end, extras, customer_name, total) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO bookings (code, wa_id, car_id, start_date, end_date, extras, customer_name,
                                         total, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_confirmation', ?)""",
                (
                    code,
                    wa_id,
                    car_id,
                    start.isoformat(),
                    end.isoformat(),
                    json.dumps(extras),
                    customer_name,
                    total,
                    now(),
                ),
            )

    def get_booking(self, code: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM bookings WHERE code = ?", (code.upper(),)).fetchone()

    def recent_bookings(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    # --- events --------------------------------------------------------
    def log_event(self, wa_id: str | None, type_: str, detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events (wa_id, type, detail, created_at) VALUES (?, ?, ?, ?)",
                (wa_id, type_, detail, now()),
            )

    def stats(self) -> dict:
        with self.connect() as conn:
            one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "conversations": one("SELECT COUNT(*) FROM conversations"),
                "messages": one("SELECT COUNT(*) FROM messages WHERE role = 'user'"),
                "bookings": one("SELECT COUNT(*) FROM bookings"),
                "handoffs": one("SELECT COUNT(*) FROM events WHERE type = 'handoff'"),
                "blocked_replies": one("SELECT COUNT(*) FROM events WHERE type = 'guardrail_blocked'"),
                "tool_calls": conn.execute(
                    "SELECT detail, COUNT(*) AS n FROM events WHERE type = 'tool_call' GROUP BY detail ORDER BY n DESC"
                ).fetchall(),
                "recent_events": conn.execute(
                    "SELECT * FROM events WHERE type != 'tool_call' ORDER BY id DESC LIMIT 15"
                ).fetchall(),
            }
