"""SQLite WAL persistence layer for iso_engine."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "iso_engine.db")

_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()

DEFAULT_UNDO_MAX_DEPTH = 30
VALID_CHECKPOINT_KINDS = frozenset({"act_start", "auto_risk", "manual"})
VALID_ASSET_KINDS = frozenset({"voxel-prop-v1", "glb", "vox"})

DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "openrouter",
    "compile_model": "",
    "prefetch_model": "",
    "base_url": "",
    "max_prefetch_depth": 1,
    "max_workers": 2,
    "tone_rating": "mature_drama",
    "language": "vi",
}

_DDL = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    act_index INTEGER NOT NULL DEFAULT 1,
    world_json TEXT NOT NULL,
    cast_json TEXT NOT NULL,
    floorplan_json TEXT NOT NULL,
    storylets_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_updated
    ON campaigns(updated_at DESC);

CREATE TABLE IF NOT EXISTS scene_cache (
    campaign_id TEXT NOT NULL,
    storylet_id TEXT NOT NULL,
    masked_state_hash TEXT NOT NULL,
    tree_json TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    PRIMARY KEY (campaign_id, storylet_id, masked_state_hash)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'manual',
    act_index INTEGER NOT NULL DEFAULT 1,
    clock_str TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL,
    history_json TEXT NOT NULL,
    storylets_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cp_campaign
    ON checkpoints(campaign_id, created_at DESC);

CREATE TABLE IF NOT EXISTS undo_stack (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    history_json TEXT NOT NULL,
    storylets_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_undo_campaign
    ON undo_stack(campaign_id, id DESC);

CREATE TABLE IF NOT EXISTS custom_assets (
    asset_tag TEXT PRIMARY KEY,
    campaign_id TEXT,
    kind TEXT NOT NULL DEFAULT 'voxel-prop-v1',
    title TEXT NOT NULL DEFAULT '',
    size_json TEXT NOT NULL DEFAULT '[1, 1]',
    mount TEXT NOT NULL DEFAULT 'floor',
    pose_slots_json TEXT NOT NULL DEFAULT '{}',
    data_json TEXT,
    file_path TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_custom_assets_campaign
    ON custom_assets(campaign_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    scope TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _resolve_path(db_path: str | os.PathLike | None = None) -> str:
    raw = os.fspath(db_path) if db_path else DEFAULT_DB_PATH
    return os.path.normcase(os.path.abspath(raw))


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
    """Initialize SQLite WAL database and create all 6 tables if missing."""
    path = _resolve_path(db_path)
    with _LOCK:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=15.0, check_same_thread=False)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_DDL)
            conn.commit()
        _INITIALIZED_PATHS.add(path)
    return path


@contextmanager
def _db(db_path: str | os.PathLike | None = None, *, ensure_init: bool = True):
    path = _resolve_path(db_path)
    if ensure_init and (path not in _INITIALIZED_PATHS or not os.path.exists(path)):
        init_db(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        with conn:
            yield conn
    finally:
        conn.close()


def save_campaign(campaign: dict, db_path: str | None = None) -> dict:
    """Insert or update a campaign record."""
    cid = str((campaign or {}).get("id") or "").strip()
    if not cid:
        raise ValueError("campaign['id'] is required")

    world = campaign.get("world") if isinstance(campaign.get("world"), dict) else {}
    cast = campaign.get("cast") if isinstance(campaign.get("cast"), (dict, list)) else {}
    floorplan = campaign.get("floorplan") if isinstance(campaign.get("floorplan"), dict) else {}
    storylets = campaign.get("storylets") if isinstance(campaign.get("storylets"), list) else []
    state = campaign.get("state") if isinstance(campaign.get("state"), dict) else {}
    history = campaign.get("history") if isinstance(campaign.get("history"), list) else []
    act_index = int(campaign.get("act_index") or state.get("act_index") or 1)
    title = (
        str(campaign.get("title") or (world.get("meta") or {}).get("name") or "Chiến dịch mới").strip()
        or "Chiến dịch mới"
    )
    now = time.time()

    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT created_at FROM campaigns WHERE id = ?", (cid,)).fetchone()
        created_at = float(row["created_at"]) if row else float(campaign.get("created_at") or now)
        conn.execute(
            """
            INSERT INTO campaigns (
                id, title, act_index, world_json, cast_json, floorplan_json,
                storylets_json, state_json, history_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                act_index = excluded.act_index,
                world_json = excluded.world_json,
                cast_json = excluded.cast_json,
                floorplan_json = excluded.floorplan_json,
                storylets_json = excluded.storylets_json,
                state_json = excluded.state_json,
                history_json = excluded.history_json,
                updated_at = excluded.updated_at
            """,
            (
                cid,
                title,
                act_index,
                _dumps(world),
                _dumps(cast),
                _dumps(floorplan),
                _dumps(storylets),
                _dumps(state),
                _dumps(history),
                created_at,
                now,
            ),
        )
    return {
        "ok": True,
        "id": cid,
        "title": title,
        "act_index": act_index,
        "created_at": created_at,
        "updated_at": now,
    }


def get_campaign(campaign_id: str, db_path: str | None = None) -> dict | None:
    """Fetch full deserialized campaign record by ID."""
    cid = (campaign_id or "").strip()
    if not cid:
        return None
    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "act_index": row["act_index"],
            "world": _loads(row["world_json"], {}),
            "cast": _loads(row["cast_json"], {}),
            "floorplan": _loads(row["floorplan_json"], {}),
            "storylets": _loads(row["storylets_json"], []),
            "state": _loads(row["state_json"], {}),
            "history": _loads(row["history_json"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def list_campaigns(db_path: str | None = None, *, limit: int = 100) -> list[dict]:
    """List campaign summaries ordered by most recently updated."""
    lim = max(1, int(limit))
    with _LOCK, _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, act_index, created_at, updated_at
            FROM campaigns
            ORDER BY updated_at DESC, rowid DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_campaign(campaign_id: str, db_path: str | None = None) -> bool:
    """Atomically delete a campaign and its scoped child rows (preserving global custom_assets)."""
    cid = (campaign_id or "").strip()
    if not cid:
        return False
    with _LOCK, _db(db_path) as conn:
        conn.execute("DELETE FROM undo_stack WHERE campaign_id = ?", (cid,))
        conn.execute("DELETE FROM checkpoints WHERE campaign_id = ?", (cid,))
        conn.execute("DELETE FROM scene_cache WHERE campaign_id = ?", (cid,))
        conn.execute("DELETE FROM custom_assets WHERE campaign_id = ?", (cid,))
        cur = conn.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
        return cur.rowcount > 0


def get_cached_scene(
    campaign_id: str,
    storylet_id: str,
    masked_state_hash: str,
    db_path: str | None = None,
) -> dict | None:
    """Lookup cached SceneNodeTree for (campaign_id, storylet_id, masked_state_hash)."""
    cid = (campaign_id or "").strip()
    sid = (storylet_id or "").strip()
    h = (masked_state_hash or "").strip()
    if not cid or not sid or not h:
        return None
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            """
            SELECT tree_json
            FROM scene_cache
            WHERE campaign_id = ? AND storylet_id = ? AND masked_state_hash = ?
            """,
            (cid, sid, h),
        ).fetchone()
        if not row:
            return None
        tree = _loads(row["tree_json"], None)
        return tree if isinstance(tree, dict) else None


def put_cached_scene(
    campaign_id: str,
    storylet_id: str,
    masked_state_hash: str,
    tree: dict,
    model: str = "",
    cost_usd: float = 0.0,
    db_path: str | None = None,
) -> None:
    """Insert or update a cached SceneNodeTree entry."""
    cid = (campaign_id or "").strip()
    sid = (storylet_id or "").strip()
    h = (masked_state_hash or "").strip()
    if not cid or not sid or not h:
        raise ValueError("campaign_id, storylet_id, and masked_state_hash must be non-empty")
    if not isinstance(tree, dict):
        raise ValueError("tree must be a dict")
    now = time.time()
    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scene_cache (
                campaign_id, storylet_id, masked_state_hash, tree_json,
                model, cost_usd, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id, storylet_id, masked_state_hash) DO UPDATE SET
                tree_json = excluded.tree_json,
                model = excluded.model,
                cost_usd = excluded.cost_usd,
                created_at = excluded.created_at
            """,
            (cid, sid, h, _dumps(tree), str(model or ""), float(cost_usd or 0.0), now),
        )


def create_checkpoint(
    campaign_id: str,
    label: str,
    kind: str,
    act_index: int,
    clock_str: str,
    state: dict,
    history: list,
    storylets: list,
    db_path: str | None = None,
    *,
    checkpoint_id: str | None = None,
) -> dict:
    """Save a timeline checkpoint without touching scene_cache."""
    cid = (campaign_id or "").strip()
    if not cid:
        raise ValueError("campaign_id is required")
    clean_kind = (kind or "manual").strip()
    if clean_kind not in VALID_CHECKPOINT_KINDS:
        raise ValueError(f"Unsupported checkpoint kind: {clean_kind}")
    cp_id = (checkpoint_id or "").strip() or f"cp_{uuid.uuid4().hex[:12]}"
    clean_label = (label or "").strip() or f"Mốc Hồi {int(act_index)}"
    now = time.time()

    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO checkpoints (
                id, campaign_id, label, kind, act_index, clock_str,
                state_json, history_json, storylets_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cp_id,
                cid,
                clean_label,
                clean_kind,
                int(act_index),
                str(clock_str or ""),
                _dumps(state if isinstance(state, dict) else {}),
                _dumps(history if isinstance(history, list) else []),
                _dumps(storylets if isinstance(storylets, list) else []),
                now,
            ),
        )
    return {
        "id": cp_id,
        "campaign_id": cid,
        "label": clean_label,
        "kind": clean_kind,
        "act_index": int(act_index),
        "clock_str": str(clock_str or ""),
        "created_at": now,
    }


def list_checkpoints(campaign_id: str, db_path: str | None = None) -> list[dict]:
    """List checkpoints for a campaign newest first."""
    cid = (campaign_id or "").strip()
    if not cid:
        return []
    with _LOCK, _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, campaign_id, label, kind, act_index, clock_str, created_at
            FROM checkpoints
            WHERE campaign_id = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (cid,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_checkpoint(checkpoint_id: str, db_path: str | None = None) -> dict | None:
    """Retrieve a checkpoint snapshot by ID without touching scene_cache."""
    cp_id = (checkpoint_id or "").strip()
    if not cp_id:
        return None
    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (cp_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "campaign_id": row["campaign_id"],
            "label": row["label"],
            "kind": row["kind"],
            "act_index": row["act_index"],
            "clock_str": row["clock_str"],
            "state": _loads(row["state_json"], {}),
            "history": _loads(row["history_json"], []),
            "storylets": _loads(row["storylets_json"], []),
            "created_at": row["created_at"],
        }


def push_undo(
    campaign_id: str,
    state: dict,
    history: list,
    storylets: list,
    db_path: str | None = None,
    *,
    max_depth: int = DEFAULT_UNDO_MAX_DEPTH,
) -> int:
    """Push an undo snapshot for campaign_id, trimming oldest beyond max_depth. Never touches scene_cache."""
    cid = (campaign_id or "").strip()
    if not cid:
        raise ValueError("campaign_id is required")
    cap = max(1, int(max_depth))
    now = time.time()

    with _LOCK, _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO undo_stack (campaign_id, state_json, history_json, storylets_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                cid,
                _dumps(state if isinstance(state, dict) else {}),
                _dumps(history if isinstance(history, list) else []),
                _dumps(storylets if isinstance(storylets, list) else []),
                now,
            ),
        )
        conn.execute(
            """
            DELETE FROM undo_stack
            WHERE campaign_id = ?
              AND id NOT IN (
                  SELECT id FROM undo_stack
                  WHERE campaign_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (cid, cid, cap),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM undo_stack WHERE campaign_id = ?",
            (cid,),
        ).fetchone()
        return int(row["cnt"]) if row else 0


def pop_undo(campaign_id: str, db_path: str | None = None) -> dict | None:
    """Pop the latest undo snapshot for campaign_id without touching scene_cache."""
    cid = (campaign_id or "").strip()
    if not cid:
        return None
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, campaign_id, state_json, history_json, storylets_json, created_at
            FROM undo_stack
            WHERE campaign_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (cid,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM undo_stack WHERE id = ?", (row["id"],))
        return {
            "id": row["id"],
            "campaign_id": row["campaign_id"],
            "state": _loads(row["state_json"], {}),
            "history": _loads(row["history_json"], []),
            "storylets": _loads(row["storylets_json"], []),
            "created_at": row["created_at"],
        }


def count_undo(campaign_id: str, db_path: str | None = None) -> int:
    """Return number of available undo snapshots for campaign_id."""
    cid = (campaign_id or "").strip()
    if not cid:
        return 0
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM undo_stack WHERE campaign_id = ?",
            (cid,),
        ).fetchone()
        return int(row["cnt"]) if row else 0


def _row_to_custom_asset(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "asset_tag": row["asset_tag"],
        "campaign_id": row["campaign_id"],
        "kind": row["kind"],
        "title": row["title"],
        "size": _loads(row["size_json"], [1, 1]),
        "mount": row["mount"],
        "pose_slots": _loads(row["pose_slots_json"], {}),
        "data": _loads(row["data_json"], None),
        "file_path": row["file_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_custom_asset(asset_dict: dict, db_path: str | None = None) -> dict:
    """Insert or update a custom asset record."""
    asset_tag = str((asset_dict or {}).get("asset_tag") or "").strip()
    if not asset_tag:
        raise ValueError("asset_tag is required")

    raw_cid = asset_dict.get("campaign_id")
    campaign_id = str(raw_cid).strip() or None if raw_cid else None
    kind = str(asset_dict.get("kind") or "voxel-prop-v1").strip()
    if kind not in VALID_ASSET_KINDS:
        raise ValueError(f"Unsupported asset kind: {kind}")

    title = str(
        asset_dict.get("title") or asset_dict.get("name_vi") or asset_dict.get("name") or asset_tag
    ).strip()
    raw_size = asset_dict.get("size")
    if isinstance(raw_size, (list, tuple)) and len(raw_size) == 2:
        size = [int(raw_size[0]), int(raw_size[1])]
    else:
        size = [1, 1]
    mount = str(asset_dict.get("mount") or "floor").strip()
    pose_slots = asset_dict.get("pose_slots") if isinstance(asset_dict.get("pose_slots"), dict) else {}
    raw_fp = asset_dict.get("file_path")
    file_path = str(raw_fp).strip() or None if raw_fp else None

    if asset_dict.get("data") is not None:
        data = asset_dict["data"]
    else:
        extracted = {
            k: asset_dict[k]
            for k in ("category", "name_vi", "height", "walkable", "voxels", "visual_brief")
            if k in asset_dict
        }
        data = extracted or None

    now = time.time()
    with _LOCK, _db(db_path) as conn:
        existing = conn.execute(
            "SELECT created_at FROM custom_assets WHERE asset_tag = ?",
            (asset_tag,),
        ).fetchone()
        created_at = float(existing["created_at"]) if existing else now
        conn.execute(
            """
            INSERT INTO custom_assets (
                asset_tag, campaign_id, kind, title, size_json, mount,
                pose_slots_json, data_json, file_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_tag) DO UPDATE SET
                campaign_id = excluded.campaign_id,
                kind = excluded.kind,
                title = excluded.title,
                size_json = excluded.size_json,
                mount = excluded.mount,
                pose_slots_json = excluded.pose_slots_json,
                data_json = excluded.data_json,
                file_path = excluded.file_path,
                updated_at = excluded.updated_at
            """,
            (
                asset_tag,
                campaign_id,
                kind,
                title,
                _dumps(size),
                mount,
                _dumps(pose_slots),
                _dumps(data) if data is not None else None,
                file_path,
                created_at,
                now,
            ),
        )
    return {
        "asset_tag": asset_tag,
        "campaign_id": campaign_id,
        "kind": kind,
        "title": title,
        "size": size,
        "mount": mount,
        "pose_slots": pose_slots,
        "data": data,
        "file_path": file_path,
        "created_at": created_at,
        "updated_at": now,
    }


def list_custom_assets(db_path: str | None = None, *, campaign_id: str | None = None) -> list[dict]:
    """List custom assets (optionally filtered to global + campaign_id)."""
    with _LOCK, _db(db_path) as conn:
        if campaign_id is None or not str(campaign_id).strip():
            rows = conn.execute(
                "SELECT * FROM custom_assets ORDER BY updated_at DESC, asset_tag ASC"
            ).fetchall()
        else:
            cid = str(campaign_id).strip()
            rows = conn.execute(
                """
                SELECT * FROM custom_assets
                WHERE campaign_id IS NULL OR campaign_id = ?
                ORDER BY updated_at DESC, asset_tag ASC
                """,
                (cid,),
            ).fetchall()
        return [_row_to_custom_asset(r) for r in rows]


def get_custom_asset(asset_tag: str, db_path: str | None = None) -> dict | None:
    """Get a custom asset by asset_tag."""
    tag = (asset_tag or "").strip()
    if not tag:
        return None
    with _LOCK, _db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM custom_assets WHERE asset_tag = ?",
            (tag,),
        ).fetchone()
        return _row_to_custom_asset(row) if row else None


def get_settings(scope: str = "global", db_path: str | None = None) -> dict:
    """Get settings for scope; merges DEFAULT_SETTINGS for 'global'."""
    sc = (scope or "").strip()
    if not sc:
        return {}
    with _LOCK, _db(db_path) as conn:
        row = conn.execute("SELECT data_json FROM settings WHERE scope = ?", (sc,)).fetchone()
        val = _loads(row["data_json"], {}) if row else {}
        if not isinstance(val, dict):
            val = {}
        if sc == "global":
            return {**DEFAULT_SETTINGS, **val}
        return val


def save_settings(
    scope: str | dict = "global",
    data: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Save settings for scope (supports save_settings(dict) shorthand for 'global')."""
    if isinstance(scope, dict) and data is None:
        payload = scope
        sc = "global"
    else:
        sc = str(scope or "").strip()
        payload = data if isinstance(data, dict) else {}
    if not sc:
        raise ValueError("scope is required")
    now = time.time()
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
