"""SQLite persistent storage for RP Lab v2 (sessions, settings, lorebooks, editor worlds, traces)."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
import sqlite3
import threading
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA_DIR, "rplab.db")

_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()


def _resolve_path(db_path: str | None = None) -> str:
    return os.path.abspath(db_path or DB_PATH)


@contextmanager
def _db(db_path: str | None = None, *, ensure_init: bool = True):
    path = _resolve_path(db_path)
    if ensure_init and path not in _INITIALIZED_PATHS:
        init_db(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn
    finally:
        conn.close()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def init_db(db_path: str | None = None) -> str:
    """Create SQLite schema and enable WAL mode for the target database path."""
    path = _resolve_path(db_path)
    with _LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with closing(sqlite3.connect(path, timeout=15.0, check_same_thread=False)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        pack_json TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        history_json TEXT NOT NULL,
                        turn_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_mode_updated
                        ON sessions(mode, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS settings (
                        scope TEXT PRIMARY KEY,
                        data_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS lorebooks (
                        scope TEXT PRIMARY KEY,
                        data_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS editor_worlds (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        world_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS traces (
                        session_id TEXT NOT NULL,
                        turn_id INTEGER NOT NULL,
                        prefix_hash TEXT NOT NULL DEFAULT '',
                        trace_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (session_id, turn_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_traces_sid_turn
                        ON traces(session_id, turn_id DESC);
                    """
                )
        _INITIALIZED_PATHS.add(path)
    return path


def list_sessions(mode: str | None = None, *, limit: int = 200, db_path: str | None = None) -> list[dict]:
    """Return session metadata ordered by most recently updated first."""
    with _LOCK, _db(db_path) as conn:
        if mode:
            rows = conn.execute(
                """
                SELECT id, mode, title, turn_count, created_at, updated_at
                FROM sessions
                WHERE mode = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (mode, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, mode, title, turn_count, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: str, *, db_path: str | None = None) -> dict | None:
    """Load a full session payload by ID, or None if missing."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, mode, title, pack_json, state_json, history_json, turn_count, created_at, updated_at
            FROM sessions WHERE id = ?
            """,
            (sid,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "mode": row["mode"],
        "title": row["title"],
        "pack": _loads(row["pack_json"], None),
        "state": _loads(row["state_json"], {}),
        "history": _loads(row["history_json"], []),
        "turn_count": row["turn_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def upsert_session(
    session_id: str,
    mode: str,
    title: str,
    pack: Any,
    state: Any,
    history: Any,
    *,
    db_path: str | None = None,
) -> dict:
    """Insert or update a single or group RP session atomically."""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    m = (mode or "single").strip()
    hist_list = history if isinstance(history, list) else []
    now = time.time()
    clean_title = (title or "").strip() or ("Cuộc trò chuyện mới" if m == "single" else "Ván group mới")
    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT created_at FROM sessions WHERE id = ?", (sid,)).fetchone()
        created_at = float(row["created_at"]) if row else now
        conn.execute(
            """
            INSERT INTO sessions (id, mode, title, pack_json, state_json, history_json, turn_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mode = excluded.mode,
                title = excluded.title,
                pack_json = excluded.pack_json,
                state_json = excluded.state_json,
                history_json = excluded.history_json,
                turn_count = excluded.turn_count,
                updated_at = excluded.updated_at
            """,
            (
                sid,
                m,
                clean_title,
                _dumps(pack),
                _dumps(state if state is not None else {}),
                _dumps(hist_list),
                len(hist_list),
                created_at,
                now,
            ),
        )
    return {
        "ok": True,
        "id": sid,
        "mode": m,
        "title": clean_title,
        "turn_count": len(hist_list),
        "created_at": created_at,
        "updated_at": now,
    }


def rename_session(session_id: str, title: str, *, db_path: str | None = None) -> dict | None:
    """Rename an existing session; returns updated metadata or None if not found."""
    sid = (session_id or "").strip()
    clean_title = (title or "").strip()
    if not sid or not clean_title:
        return None
    now = time.time()
    with _LOCK, _db(db_path) as conn:
        cur = conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (clean_title, now, sid),
        )
        if cur.rowcount == 0:
            return None
    return {"ok": True, "id": sid, "title": clean_title, "updated_at": now}


def delete_session(session_id: str, *, db_path: str | None = None) -> bool:
    """Delete a session and its associated traces; returns True if a session existed."""
    sid = (session_id or "").strip()
    if not sid:
        return False
    with _LOCK, _db(db_path) as conn:
        conn.execute("DELETE FROM traces WHERE session_id = ?", (sid,))
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        return cur.rowcount > 0


def get_settings(scope: str, *, db_path: str | None = None) -> dict:
    """Load saved settings dictionary for a scope ('single', 'group', etc.)."""
    sc = (scope or "").strip()
    if not sc:
        return {}
    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT data_json FROM settings WHERE scope = ?", (sc,)).fetchone()
    if not row:
        return {}
    val = _loads(row["data_json"], {})
    return val if isinstance(val, dict) else {}


def save_settings(scope: str, data: dict, *, db_path: str | None = None) -> dict:
    """Persist settings dictionary for a scope."""
    sc = (scope or "").strip()
    if not sc:
        raise ValueError("scope is required")
    now = time.time()
    payload = data if isinstance(data, dict) else {}
    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO settings (scope, data_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            """,
            (sc, _dumps(payload), now),
        )
    return {"ok": True, "scope": sc, "updated_at": now}


def get_lorebooks(scope: str, *, db_path: str | None = None) -> list:
    """Load saved lorebooks list for a scope ('single' or 'group')."""
    sc = (scope or "").strip()
    if not sc:
        return []
    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT data_json FROM lorebooks WHERE scope = ?", (sc,)).fetchone()
    if not row:
        return []
    val = _loads(row["data_json"], [])
    return val if isinstance(val, list) else []


def save_lorebooks(scope: str, data: list, *, db_path: str | None = None) -> dict:
    """Persist lorebooks list for a scope."""
    sc = (scope or "").strip()
    if not sc:
        raise ValueError("scope is required")
    now = time.time()
    payload = data if isinstance(data, list) else []
    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO lorebooks (scope, data_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            """,
            (sc, _dumps(payload), now),
        )
    return {"ok": True, "scope": sc, "updated_at": now}


def get_editor_world(world_id: str = "current", *, db_path: str | None = None) -> dict | None:
    """Load saved world draft from editor_worlds, or None if not saved yet."""
    wid = (world_id or "current").strip()
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, world_json, updated_at FROM editor_worlds WHERE id = ?",
            (wid,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "world": _loads(row["world_json"], None),
        "updated_at": row["updated_at"],
    }


def save_editor_world(world_data: dict, world_id: str = "current", *, db_path: str | None = None) -> dict:
    """Save editor world-v2 JSON draft into SQLite."""
    wid = (world_id or "current").strip()
    payload = world_data if isinstance(world_data, dict) else {}
    title = str(((payload.get("meta") or payload.get("world") or {}).get("name") or payload.get("title") or "")).strip()
    now = time.time()
    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO editor_worlds (id, title, world_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                world_json = excluded.world_json,
                updated_at = excluded.updated_at
            """,
            (wid, title, _dumps(payload), now),
        )
    return {"ok": True, "id": wid, "title": title, "updated_at": now}


def save_trace(
    session_id: str,
    turn_id: int,
    trace_obj: dict,
    prefix_hash: str = "",
    *,
    db_path: str | None = None,
) -> dict:
    """Persist an X-Ray prompt trace record into SQLite."""
    sid = (session_id or "default").strip() or "default"
    tid = int(turn_id or 0)
    now = time.time()
    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO traces (session_id, turn_id, prefix_hash, trace_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id, turn_id) DO UPDATE SET
                prefix_hash = excluded.prefix_hash,
                trace_json = excluded.trace_json,
                created_at = excluded.created_at
            """,
            (sid, tid, prefix_hash or "", _dumps(trace_obj), now),
        )
    return {"ok": True, "session_id": sid, "turn_id": tid}


def load_trace(session_id: str, turn_id: int, *, db_path: str | None = None) -> dict | None:
    """Load an X-Ray trace record from SQLite by session_id and turn_id."""
    sid = (session_id or "default").strip() or "default"
    tid = int(turn_id or 0)
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            "SELECT trace_json FROM traces WHERE session_id = ? AND turn_id = ?",
            (sid, tid),
        ).fetchone()
    if not row:
        return None
    val = _loads(row["trace_json"], None)
    return val if isinstance(val, dict) else None


def get_last_prefix_hash(session_id: str, *, db_path: str | None = None) -> str:
    """Return the most recent prefix_hash recorded for session_id."""
    sid = (session_id or "default").strip() or "default"
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            "SELECT prefix_hash FROM traces WHERE session_id = ? ORDER BY turn_id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    return str(row["prefix_hash"]) if row and row["prefix_hash"] else ""
