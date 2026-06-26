"""SQLite (aiosqlite) database: groups and dedup tables.

A single shared connection is used — aiosqlite executes all queries
sequentially in its own queue, so it is safe to use from multiple coroutines.
"""

import aiosqlite

from app import config

_db: aiosqlite.Connection | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    username    TEXT,
    status      TEXT    NOT NULL DEFAULT 'active',   -- 'active' | 'pending' | 'removed'
    added_by    INTEGER,
    is_target   INTEGER NOT NULL DEFAULT 0,           -- 1 = forwarding target (only one)
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("DB is not initialized. Call init_db() first.")
    return _db


async def init_db() -> None:
    """Connects to the database and creates tables (if they don't exist)."""
    global _db
    _db = await aiosqlite.connect(config.DATABASE_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    # Migration: add is_target column for existing databases
    try:
        await _db.execute(
            "ALTER TABLE groups ADD COLUMN is_target INTEGER NOT NULL DEFAULT 0"
        )
    except Exception:
        pass  # Column already exists
    await _db.commit()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ===== Groups =====

async def set_group_active(
    chat_id: int, title: str | None, username: str | None, added_by: int | None
) -> None:
    """Saves the group in 'active' status (updates if it already exists)."""
    await _conn().execute(
        """
        INSERT INTO groups (chat_id, title, username, status, added_by)
        VALUES (?, ?, ?, 'active', ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title    = excluded.title,
            username = excluded.username,
            status   = 'active',
            added_by = excluded.added_by
        """,
        (chat_id, title, username, added_by),
    )
    await _conn().commit()


async def upsert_pending_group(
    chat_id: int, title: str | None, username: str | None, added_by: int | None
) -> None:
    """Records the group in 'pending' status when the bot is added to it.

    If the group already exists, only the title/username are updated —
    the 'active' status is preserved.
    """
    await _conn().execute(
        """
        INSERT INTO groups (chat_id, title, username, status, added_by)
        VALUES (?, ?, ?, 'pending', ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title    = excluded.title,
            username = excluded.username
        """,
        (chat_id, title, username, added_by),
    )
    await _conn().commit()


async def set_group_status(chat_id: int, status: str) -> bool:
    """Changes the group status. Clears target if not 'active'. Returns True if found."""
    if status != "active":
        await _conn().execute(
            "UPDATE groups SET is_target = 0 WHERE chat_id = ?", (chat_id,)
        )
    cur = await _conn().execute(
        "UPDATE groups SET status = ? WHERE chat_id = ?", (status, chat_id)
    )
    await _conn().commit()
    return cur.rowcount > 0


async def list_groups(statuses: list[str] | None = None) -> list[aiosqlite.Row]:
    """Returns groups (optionally filtered by status)."""
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        cur = await _conn().execute(
            f"SELECT * FROM groups WHERE status IN ({placeholders}) ORDER BY created_at",
            tuple(statuses),
        )
    else:
        cur = await _conn().execute("SELECT * FROM groups ORDER BY created_at")
    return list(await cur.fetchall())


async def get_active_chat_ids() -> list[int]:
    """Returns chat_ids of all 'active' groups."""
    cur = await _conn().execute("SELECT chat_id FROM groups WHERE status = 'active'")
    rows = await cur.fetchall()
    return [row["chat_id"] for row in rows]


async def set_target_group(chat_id: int) -> None:
    """Sets this group as the sole forwarding target (clears all others)."""
    await _conn().execute("UPDATE groups SET is_target = 0")
    await _conn().execute(
        "UPDATE groups SET is_target = 1 WHERE chat_id = ? AND status = 'active'",
        (chat_id,),
    )
    await _conn().commit()


async def get_target_chat_id() -> int | None:
    """Returns the forwarding target group's chat_id, or None if not set."""
    cur = await _conn().execute(
        "SELECT chat_id FROM groups WHERE is_target = 1 AND status = 'active'"
    )
    row = await cur.fetchone()
    return row["chat_id"] if row else None


async def find_group_by_username(username: str) -> aiosqlite.Row | None:
    """Finds a group by its @username."""
    uname = username.lstrip("@").lower()
    cur = await _conn().execute(
        "SELECT * FROM groups WHERE lower(username) = ?", (uname,)
    )
    return await cur.fetchone()


# ===== Dedup (prevent duplicate SMS forwarding) =====

async def is_processed(message_id: str) -> bool:
    cur = await _conn().execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ?", (str(message_id),)
    )
    return await cur.fetchone() is not None


async def mark_processed(message_id: str) -> None:
    await _conn().execute(
        "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
        (str(message_id),),
    )
    await _conn().commit()
