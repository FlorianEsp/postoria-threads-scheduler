from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Any
from utils import caption_hash

DB_PATH = Path("postoria_threads.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def parse_media_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            import json

            data = json.loads(text)
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        except Exception:
            pass
    return [item.strip() for item in text.replace("\n", ",").split(",") if item.strip()]


def serialize_media_ids(value: Any) -> str:
    import json

    return json.dumps(parse_media_ids(value), ensure_ascii=False)


def parse_json_map(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        import json

        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def serialize_json_map(value: Any) -> str:
    import json

    return json.dumps(parse_json_map(value), ensure_ascii=False)


def parse_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def serialize_lines(value: Any) -> str:
    import json

    return json.dumps(parse_lines(value), ensure_ascii=False)


def parse_json_list(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            import json

            data = json.loads(text)
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        except Exception:
            pass
    return parse_lines(text)


def _hydrate_media(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["media_ids"] = parse_media_ids(data.get("media_ids"))
    data["media_count"] = len(data["media_ids"])
    data["variables"] = parse_json_map(data.get("variables_json"))
    data["reply_chain"] = parse_json_list(data.get("reply_chain"))
    data["chain_replies"] = parse_json_list(data.get("chain_replies"))
    return data


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS post_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caption TEXT NOT NULL,
                caption_hash TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT,
                total_used INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                media_ids TEXT DEFAULT '',
                photo_note TEXT DEFAULT '',
                media_folder TEXT DEFAULT '',
                variables_json TEXT DEFAULT '',
                reply_chain TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                network TEXT NOT NULL,
                url TEXT,
                username TEXT,
                avatar_url TEXT,
                group_name TEXT DEFAULT 'tous',
                active_for_day INTEGER DEFAULT 1,
                consecutive_failures INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS account_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                offset_minutes INTEGER DEFAULT 0,
                color TEXT DEFAULT '#8b5cf6',
                strategy TEXT DEFAULT 'default'
            );

            CREATE TABLE IF NOT EXISTS group_accounts (
                group_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                PRIMARY KEY (group_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS media_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                media_ids TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_post_id INTEGER NOT NULL,
                caption_hash TEXT NOT NULL,
                caption TEXT NOT NULL,
                account_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                group_name TEXT,
                scheduled_time_local TEXT NOT NULL,
                scheduled_time_utc TEXT NOT NULL,
                media_ids TEXT DEFAULT '',
                content_type TEXT DEFAULT 'text',
                variables_json TEXT DEFAULT '',
                chain_replies TEXT DEFAULT '',
                postoria_post_id INTEGER,
                status TEXT DEFAULT 'preview',
                preview_batch_id TEXT,
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS preview_batches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _ensure_column(conn, "post_library", "media_ids", "TEXT DEFAULT ''")
        _ensure_column(conn, "post_library", "photo_note", "TEXT DEFAULT ''")
        _ensure_column(conn, "post_library", "media_folder", "TEXT DEFAULT ''")
        _ensure_column(conn, "post_library", "variables_json", "TEXT DEFAULT ''")
        _ensure_column(conn, "post_library", "reply_chain", "TEXT DEFAULT ''")
        _ensure_column(conn, "accounts", "username", "TEXT")
        _ensure_column(conn, "accounts", "avatar_url", "TEXT")
        _ensure_column(conn, "accounts", "group_name", "TEXT DEFAULT 'tous'")
        _ensure_column(conn, "account_groups", "color", "TEXT DEFAULT '#8b5cf6'")
        _ensure_column(conn, "scheduled_posts", "media_ids", "TEXT DEFAULT ''")
        _ensure_column(conn, "scheduled_posts", "content_type", "TEXT DEFAULT 'text'")
        _ensure_column(conn, "scheduled_posts", "variables_json", "TEXT DEFAULT ''")
        _ensure_column(conn, "scheduled_posts", "chain_replies", "TEXT DEFAULT ''")
        _ensure_column(conn, "scheduled_posts", "preview_batch_id", "TEXT")
        _ensure_column(conn, "preview_batches", "name", "TEXT DEFAULT ''")
        _ensure_column(conn, "preview_batches", "status", "TEXT DEFAULT 'active'")
        _ensure_column(conn, "preview_batches", "updated_at", "TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO account_groups (name, offset_minutes) VALUES ('tous', 0)"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO preview_batches (id, name, status)
            SELECT
                preview_batch_id,
                'Preview ' || preview_batch_id,
                CASE
                    WHEN SUM(CASE WHEN status = 'preview' THEN 1 ELSE 0 END) > 0
                    THEN 'active'
                    ELSE 'archived'
                END
            FROM scheduled_posts
            WHERE preview_batch_id IS NOT NULL AND preview_batch_id != ''
            GROUP BY preview_batch_id
            """
        )


def add_posts_with_ids(posts: Iterable[str | dict[str, Any]]) -> tuple[int, int, list[int]]:
    added = 0
    skipped = 0
    ids: list[int] = []
    with connect() as conn:
        for raw in posts:
            if isinstance(raw, dict):
                caption = str(raw.get("caption", "")).strip()
                media_ids = serialize_media_ids(raw.get("media_ids", ""))
                photo_note = str(raw.get("photo_note") or raw.get("photo") or "").strip()
                media_folder = str(raw.get("media_folder") or "").strip()
                variables_json = serialize_json_map(raw.get("variables") or raw.get("variables_json") or {})
                reply_chain = serialize_lines(raw.get("reply_chain") or "")
            else:
                caption = str(raw).strip()
                media_ids = ""
                photo_note = ""
                media_folder = ""
                variables_json = ""
                reply_chain = ""
            if not caption:
                skipped += 1
                continue
            h = caption_hash(caption)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO post_library
                    (caption, caption_hash, media_ids, photo_note, media_folder, variables_json, reply_chain)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (caption, h, media_ids, photo_note, media_folder, variables_json, reply_chain),
                )
                ids.append(int(cursor.lastrowid))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
                row = conn.execute("SELECT id FROM post_library WHERE caption_hash = ?", (h,)).fetchone()
                if row:
                    ids.append(int(row["id"]))
    return added, skipped, ids


def add_posts(posts: Iterable[str | dict[str, Any]]) -> tuple[int, int]:
    added, skipped, _ = add_posts_with_ids(posts)
    return added, skipped


def update_post_metadata(
    post_id: int,
    media_ids: Any,
    photo_note: str,
    is_active: bool,
    media_folder: str = "",
    variables: Any = None,
    reply_chain: Any = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE post_library
            SET media_ids = ?, photo_note = ?, is_active = ?, media_folder = ?, variables_json = ?, reply_chain = ?
            WHERE id = ?
            """,
            (
                serialize_media_ids(media_ids),
                str(photo_note or "").strip(),
                int(bool(is_active)),
                str(media_folder or "").strip(),
                serialize_json_map(variables or {}),
                serialize_lines(reply_chain or ""),
                int(post_id),
            ),
        )


def list_posts(active_only: bool = True) -> list[dict[str, Any]]:
    query = "SELECT * FROM post_library"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY id DESC"
    with connect() as conn:
        return [_hydrate_media(r) for r in conn.execute(query).fetchall()]


def _pick_first(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return None


def upsert_accounts(accounts: list[dict[str, Any]]) -> None:
    with connect() as conn:
        for a in accounts:
            username = _pick_first(a, ("username", "handle", "slug", "provider_username"))
            avatar_url = _pick_first(a, ("avatar_url", "profile_picture_url", "picture", "image_url", "photo_url"))
            conn.execute(
                """
                INSERT INTO accounts (id, name, network, url, username, avatar_url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    network=excluded.network,
                    url=excluded.url,
                    username=COALESCE(excluded.username, accounts.username),
                    avatar_url=COALESCE(excluded.avatar_url, accounts.avatar_url)
                """,
                (a["id"], a.get("name", ""), a.get("network", ""), a.get("url"), username, avatar_url),
            )


def update_account_preferences(account_id: int, group_name: str, active_for_day: bool) -> None:
    clean_group = str(group_name or "tous").strip()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO account_groups (name, offset_minutes) VALUES (?, 0)",
            (clean_group,),
        )
        conn.execute(
            "UPDATE accounts SET group_name=?, active_for_day=? WHERE id=?",
            (clean_group, int(bool(active_for_day)), int(account_id)),
        )


def list_accounts() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY name COLLATE NOCASE").fetchall()]


def list_groups() -> list[dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO account_groups (name, offset_minutes)
            SELECT DISTINCT COALESCE(NULLIF(group_name, ''), 'tous'), 0
            FROM accounts
            """
        )
        return [
            dict(r)
            for r in conn.execute(
                """
                SELECT g.name, g.offset_minutes, g.color, COUNT(a.id) AS account_count
                FROM account_groups g
                LEFT JOIN accounts a ON COALESCE(NULLIF(a.group_name, ''), 'tous') = g.name
                GROUP BY g.id, g.name, g.offset_minutes, g.color
                ORDER BY g.name COLLATE NOCASE
                """
            ).fetchall()
        ]


def upsert_group(name: str, offset_minutes: int = 0, color: str = "#8b5cf6") -> bool:
    clean_name = str(name or "").strip()
    if not clean_name:
        return False
    clean_color = str(color or "#8b5cf6").strip()
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO account_groups (name, offset_minutes, color) VALUES (?, ?, ?)",
                (clean_name, int(offset_minutes), clean_color),
            )
            return True
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE account_groups SET color=? WHERE name=?",
                (clean_color, clean_name),
            )
            return False


def upsert_media_folder(name: str, media_ids: Any, note: str = "") -> bool:
    clean_name = str(name or "").strip()
    if not clean_name:
        return False
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO media_folders (name, media_ids, note) VALUES (?, ?, ?)",
                (clean_name, serialize_media_ids(media_ids), str(note or "").strip()),
            )
            return True
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE media_folders SET media_ids=?, note=? WHERE name=?",
                (serialize_media_ids(media_ids), str(note or "").strip(), clean_name),
            )
            return False


def list_media_folders() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = []
        for row in conn.execute("SELECT * FROM media_folders ORDER BY name COLLATE NOCASE").fetchall():
            data = dict(row)
            data["media_ids"] = parse_media_ids(data.get("media_ids"))
            data["media_count"] = len(data["media_ids"])
            rows.append(data)
        return rows


def media_folder_map() -> dict[str, list[str]]:
    return {folder["name"]: folder["media_ids"] for folder in list_media_folders()}


def save_preview(rows: list[dict[str, Any]], name: str | None = None) -> str:
    from datetime import datetime

    if not rows:
        clear_preview()
        return "empty"

    batch_id = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    batch_name = str(name or "").strip() or f"Preview {batch_id}"
    with connect() as conn:
        active_batches = [
            str(row["preview_batch_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT preview_batch_id
                FROM scheduled_posts
                WHERE status = 'preview' AND preview_batch_id IS NOT NULL
                """
            ).fetchall()
            if row["preview_batch_id"]
        ]
        for active_batch_id in active_batches:
            conn.execute(
                "UPDATE preview_batches SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (active_batch_id,),
            )
        conn.execute(
            "UPDATE scheduled_posts SET status = 'preview_saved' WHERE status = 'preview'"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO preview_batches (id, name, status, updated_at)
            VALUES (?, ?, 'active', CURRENT_TIMESTAMP)
            """,
            (batch_id, batch_name),
        )
        for r in rows:
            conn.execute(
                """
                INSERT INTO scheduled_posts
                (library_post_id, caption_hash, caption, account_id, account_name, group_name,
                 scheduled_time_local, scheduled_time_utc, media_ids, content_type, variables_json, chain_replies, status, preview_batch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preview', ?)
                """,
                (
                    r["library_post_id"], r["caption_hash"], r["caption"], r["account_id"],
                    r["account_name"], r.get("group_name"), r["scheduled_time_local"], r["scheduled_time_utc"],
                    serialize_media_ids(r.get("media_ids", [])), r.get("content_type", "text"),
                    serialize_json_map(r.get("variables", {})), serialize_lines(r.get("chain_replies", [])),
                    batch_id,
                ),
            )
    return batch_id


def clear_preview() -> None:
    with connect() as conn:
        active_batches = [
            str(row["preview_batch_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT preview_batch_id
                FROM scheduled_posts
                WHERE status = 'preview' AND preview_batch_id IS NOT NULL
                """
            ).fetchall()
            if row["preview_batch_id"]
        ]
        conn.execute("DELETE FROM scheduled_posts WHERE status = 'preview'")
        for batch_id in active_batches:
            remaining = conn.execute(
                "SELECT COUNT(*) AS count FROM scheduled_posts WHERE preview_batch_id=?",
                (batch_id,),
            ).fetchone()
            if remaining and int(remaining["count"] or 0):
                conn.execute(
                    "UPDATE preview_batches SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (batch_id,),
                )
            else:
                conn.execute("DELETE FROM preview_batches WHERE id=?", (batch_id,))


def clear_all_scheduled_local() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM scheduled_posts").fetchone()
        deleted = int(row["count"] or 0) if row else 0
        conn.execute("DELETE FROM scheduled_posts")
        conn.execute("DELETE FROM preview_batches")
        return deleted


def list_preview_batches() -> list[dict[str, Any]]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    b.id,
                    b.name,
                    b.status,
                    b.created_at,
                    b.updated_at,
                    COUNT(s.id) AS post_count,
                    SUM(CASE WHEN s.status = 'preview' THEN 1 ELSE 0 END) AS preview_count,
                    SUM(CASE WHEN s.status = 'preview_saved' THEN 1 ELSE 0 END) AS saved_count,
                    SUM(CASE WHEN s.status NOT IN ('preview', 'preview_saved') THEN 1 ELSE 0 END) AS sent_or_scheduled_count,
                    MIN(s.scheduled_time_local) AS first_post,
                    MAX(s.scheduled_time_local) AS last_post
                FROM preview_batches b
                LEFT JOIN scheduled_posts s ON s.preview_batch_id = b.id
                GROUP BY b.id
                ORDER BY b.created_at DESC, b.id DESC
                """
            ).fetchall()
        ]


def update_preview_batch_name(batch_id: str, name: str) -> None:
    clean_name = str(name or "").strip()
    if not clean_name:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE preview_batches SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (clean_name, str(batch_id)),
        )


def restore_preview_batch(batch_id: str) -> int:
    with connect() as conn:
        active_batches = [
            str(row["preview_batch_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT preview_batch_id
                FROM scheduled_posts
                WHERE status = 'preview' AND preview_batch_id IS NOT NULL
                """
            ).fetchall()
            if row["preview_batch_id"]
        ]
        conn.execute("UPDATE scheduled_posts SET status='preview_saved' WHERE status='preview'")
        for active_batch_id in active_batches:
            conn.execute(
                "UPDATE preview_batches SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (active_batch_id,),
            )
        cursor = conn.execute(
            "UPDATE scheduled_posts SET status='preview' WHERE preview_batch_id=? AND status='preview_saved'",
            (str(batch_id),),
        )
        restored = int(cursor.rowcount or 0)
        if restored:
            conn.execute(
                "UPDATE preview_batches SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(batch_id),),
            )
        return restored


def delete_preview_batch(batch_id: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM scheduled_posts WHERE preview_batch_id=? AND status IN ('preview', 'preview_saved')",
            (str(batch_id),),
        )
        deleted = int(cursor.rowcount or 0)
        remaining = conn.execute(
            "SELECT COUNT(*) AS count FROM scheduled_posts WHERE preview_batch_id=?",
            (str(batch_id),),
        ).fetchone()
        if remaining and int(remaining["count"] or 0):
            conn.execute(
                "UPDATE preview_batches SET status='locked', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(batch_id),),
            )
        else:
            conn.execute("DELETE FROM preview_batches WHERE id=?", (str(batch_id),))
        return deleted


def update_scheduled_result(local_id: int, postoria_post_id: int | None, status: str, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE scheduled_posts SET postoria_post_id=?, status=?, error=? WHERE id=?",
            (postoria_post_id, status, error, local_id),
        )


def list_scheduled(status: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM scheduled_posts"
    params: tuple[Any, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY scheduled_time_local ASC"
    with connect() as conn:
        return [_hydrate_media(r) for r in conn.execute(query, params).fetchall()]
