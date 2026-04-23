from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data"
_DB_PATH = _DATA_DIR / "analytics.sqlite"

_DDL_PROMO = """
CREATE TABLE IF NOT EXISTS promo_sponsored_shown (
    user_id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL
);
"""

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    msg_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
"""


def _trim_messages(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    n = int(row[0]) if row else 0
    if n <= 5200:
        return
    to_drop = n - 5000
    conn.execute(
        "DELETE FROM messages WHERE id IN (SELECT id FROM messages ORDER BY id ASC LIMIT ?)",
        (to_drop,),
    )


def _connect() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_DDL)
        conn.executescript(_DDL_PROMO)


def sponsored_promo_eligible(user_id: int, cooldown_s: int) -> bool:
    """Можно ли показать платное/спонсорское сообщение после анализа."""
    if cooldown_s <= 0:
        return True
    now = int(time.time())
    with _connect() as conn:
        conn.executescript(_DDL_PROMO)
        row = conn.execute(
            "SELECT ts FROM promo_sponsored_shown WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return True
        return (now - int(row[0])) >= cooldown_s


def sponsored_promo_mark_shown(user_id: int) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.executescript(_DDL_PROMO)
        conn.execute(
            """
            INSERT INTO promo_sponsored_shown (user_id, ts) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET ts = excluded.ts
            """,
            (user_id, now),
        )
        conn.commit()


def record_message(*, user_id: int, username: str | None, chat_id: int, text: str) -> None:
    now = int(time.time())
    t = (text or "").replace("\x00", "")[:4000]
    un = (username or "").replace("\x00", "")[:255]
    with _connect() as conn:
        conn.executescript(_DDL)
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_seen, last_seen, msg_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                last_seen = excluded.last_seen,
                msg_count = msg_count + 1
            """,
            (user_id, un, now, now),
        )
        conn.execute(
            "INSERT INTO messages (ts, user_id, username, chat_id, text) VALUES (?, ?, ?, ?, ?)",
            (now, user_id, un, chat_id, t),
        )
        _trim_messages(conn)
        conn.commit()


def fetch_stats() -> dict[str, int]:
    now = int(time.time())
    week_ago = now - 7 * 86400
    with _connect() as conn:
        conn.executescript(_DDL)
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_msgs = conn.execute("SELECT COALESCE(SUM(msg_count), 0) FROM users").fetchone()[0]
        active_7d = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= ?", (week_ago,)
        ).fetchone()[0]
        msgs_24h = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE ts >= ?", (now - 86400,)
        ).fetchone()[0]
    return {
        "total_users": int(total_users),
        "total_messages": int(total_msgs),
        "active_users_7d": int(active_7d),
        "logged_messages_24h": int(msgs_24h),
    }


def fetch_recent_messages(limit: int = 25) -> list[tuple[int, int, str | None, int, str]]:
    limit = max(1, min(100, limit))
    with _connect() as conn:
        conn.executescript(_DDL)
        rows = conn.execute(
            """
            SELECT ts, user_id, username, chat_id, text
            FROM messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [(int(r[0]), int(r[1]), r[2], int(r[3]), str(r[4])) for r in rows]
