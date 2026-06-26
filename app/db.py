"""SQLite (aiosqlite) ma'lumotlar bazasi: guruhlar va dedup jadvallari.

Bitta umumiy ulanish ishlatiladi — aiosqlite barcha so'rovlarni o'z navbatida
ketma-ket bajaradi, shuning uchun bir nechta coroutine'dan foydalanish xavfsiz.
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
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("DB ishga tushirilmagan. Avval init_db() chaqiring.")
    return _db


async def init_db() -> None:
    """Bazaga ulanadi va jadvallarni yaratadi (agar yo'q bo'lsa)."""
    global _db
    _db = await aiosqlite.connect(config.DATABASE_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    await _db.commit()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ===== Guruhlar =====

async def set_group_active(
    chat_id: int, title: str | None, username: str | None, added_by: int | None
) -> None:
    """Guruhni 'active' holatda saqlaydi (mavjud bo'lsa yangilaydi)."""
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
    """Bot guruhga qo'shilganda 'pending' holatda yozadi.

    Agar guruh allaqachon mavjud bo'lsa, faqat sarlavha/username yangilanadi —
    'active' holati saqlanib qoladi.
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
    """Guruh holatini o'zgartiradi. Topilsa True qaytaradi."""
    cur = await _conn().execute(
        "UPDATE groups SET status = ? WHERE chat_id = ?", (status, chat_id)
    )
    await _conn().commit()
    return cur.rowcount > 0


async def list_groups(statuses: list[str] | None = None) -> list[aiosqlite.Row]:
    """Guruhlarni qaytaradi (ixtiyoriy holat bo'yicha filtrlangan)."""
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
    """SMS uzatiladigan barcha 'active' guruhlar chat_id'larini qaytaradi."""
    cur = await _conn().execute("SELECT chat_id FROM groups WHERE status = 'active'")
    rows = await cur.fetchall()
    return [row["chat_id"] for row in rows]


async def find_group_by_username(username: str) -> aiosqlite.Row | None:
    """@username bo'yicha guruhni topadi."""
    uname = username.lstrip("@").lower()
    cur = await _conn().execute(
        "SELECT * FROM groups WHERE lower(username) = ?", (uname,)
    )
    return await cur.fetchone()


# ===== Dedup (takror SMS'larni oldini olish) =====

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
