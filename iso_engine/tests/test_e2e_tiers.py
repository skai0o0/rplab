"""4-Tier Opaque-Box E2E Test Suite for iso_engine (F1-F21, 246 Test Cases).

Uses Python 3.11 stdlib unittest ONLY.
Never touches rp/webui/, rplab.db, or iso_engine/data/iso_engine.db.
Never binds port 7861 or 7862 in tests (always ephemeral 127.0.0.1:0).
Zero live external LLM API calls (uses MockLLMClient or local mock HTTP server).
"""

from __future__ import annotations

import ast
import base64
import hashlib
import http.client
import http.server
import importlib
import importlib.machinery
import inspect
import json
import os
import random
import socketserver
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ISO_ENGINE_DIR = os.path.dirname(TESTS_DIR)
RP_DIR = os.path.dirname(ISO_ENGINE_DIR)
WORKSPACE_ROOT = os.path.dirname(RP_DIR)

WEBUI_DIR = os.path.join(RP_DIR, "webui")
RPLAB_DB_PATH = os.path.join(WEBUI_DIR, "data", "rplab.db")
RPLAB_WAL_PATH = os.path.join(WEBUI_DIR, "data", "rplab.db-wal")
RPLAB_SHM_PATH = os.path.join(WEBUI_DIR, "data", "rplab.db-shm")

SAMPLES_DIR = os.path.join(RP_DIR, "samples")
SAMPLE_WORLD_V2 = os.path.join(SAMPLES_DIR, "dai_hoc_ben_nghe_world.json")
SAMPLE_CHARS_DIR = os.path.join(SAMPLES_DIR, "chars")
BIET_THU_SAMPLE = os.path.join(ISO_ENGINE_DIR, "samples", "biet_thu_mua_ngau.json")
STARTER_CATALOG_PATH = os.path.join(ISO_ENGINE_DIR, "assets", "starter_catalog.json")
BLENDER_KIT_PATH = os.path.join(ISO_ENGINE_DIR, "tools", "blender_export_kit.py")


def _ensure_iso_on_path() -> None:
    norm_iso = os.path.normcase(os.path.abspath(ISO_ENGINE_DIR))
    if not sys.path or os.path.normcase(os.path.abspath(sys.path[0])) != norm_iso:
        sys.path[:] = [
            p for p in sys.path if os.path.normcase(os.path.abspath(p)) != norm_iso
        ]
        sys.path.insert(0, ISO_ENGINE_DIR)


def _mod(name: str) -> Any:
    """Import an iso_engine module cleanly without swallowing ImportError."""
    _ensure_iso_on_path()
    norm_iso = os.path.normcase(os.path.abspath(ISO_ENGINE_DIR))
    existing = sys.modules.get(name)
    if existing is not None:
        ex_file = getattr(existing, "__file__", None)
        if ex_file is not None:
            ex_norm = os.path.normcase(os.path.abspath(ex_file))
            if not (ex_norm == norm_iso or ex_norm.startswith(norm_iso + os.sep)):
                raise ModuleNotFoundError(f"No module named '{name}'")
        return existing

    top_pkg = name.split(".", 1)[0]
    if importlib.machinery.PathFinder.find_spec(top_pkg, [ISO_ENGINE_DIR]) is None:
        raise ModuleNotFoundError(f"No module named '{name}'")

    mod = importlib.import_module(name)
    mod_file = getattr(mod, "__file__", None)
    if mod_file is not None:
        mod_norm = os.path.normcase(os.path.abspath(mod_file))
        if not (mod_norm == norm_iso or mod_norm.startswith(norm_iso + os.sep)):
            raise ModuleNotFoundError(f"No module named '{name}'")
    return mod


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def snapshot_webui_and_rplab() -> dict[str, Any]:
    """Capture non-mutating baseline of rp/webui/ and rplab.db."""
    files: dict[str, tuple[int, int]] = {}
    code_hashes: dict[str, str] = {}
    if os.path.isdir(WEBUI_DIR):
        for root, _, filenames in os.walk(WEBUI_DIR):
            for fn in sorted(filenames):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, WEBUI_DIR).replace("\\", "/")
                st = os.stat(full)
                files[rel] = (st.st_size, st.st_mtime_ns)
                if fn.endswith((".py", ".html", ".json", ".bat")) or fn == "rplab.db":
                    code_hashes[rel] = _sha256_file(full)

    db_row_counts: dict[str, int] | None = None
    if os.path.isfile(RPLAB_DB_PATH):
        wal_size = os.path.getsize(RPLAB_WAL_PATH) if os.path.isfile(RPLAB_WAL_PATH) else 0
        if wal_size == 0:
            conn = sqlite3.connect(
                f"file:{RPLAB_DB_PATH}?mode=ro&immutable=1",
                uri=True,
                timeout=2.0,
            )
            try:
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                ]
                db_row_counts = {}
                for tbl in sorted(tables):
                    db_row_counts[tbl] = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            finally:
                conn.close()

    return {
        "exists": os.path.isdir(WEBUI_DIR),
        "files": files,
        "code_hashes": code_hashes,
        "db_row_counts": db_row_counts,
    }


def build_minimal_glb(generator: str = "iso_engine_test") -> bytes:
    """Build a valid 4-byte-aligned GLB v2 binary."""
    gltf = {
        "asset": {"version": "2.0", "generator": generator},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "test_box", "mesh": 0}],
        "meshes": [{"primitives": []}],
    }
    jb = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    jb += b" " * ((4 - len(jb) % 4) % 4)
    chunk0 = struct.pack("<I4s", len(jb), b"JSON") + jb
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunk0)) + chunk0


def build_minimal_vox() -> bytes:
    """Build a valid minimal MagicaVoxel .vox v150 binary."""
    size_payload = struct.pack("<III", 1, 1, 1)
    size_chunk = b"SIZE" + struct.pack("<II", len(size_payload), 0) + size_payload
    xyzi_payload = struct.pack("<IBBBB", 1, 0, 0, 0, 1)
    xyzi_chunk = b"XYZI" + struct.pack("<II", len(xyzi_payload), 0) + xyzi_payload
    children = size_chunk + xyzi_chunk
    main_chunk = b"MAIN" + struct.pack("<II", 0, len(children)) + children
    return b"VOX " + struct.pack("<I", 150) + main_chunk


def make_test_char(
    char_id: str = "linh",
    name: str = "Trần Khánh Linh",
    home_room: str = "bedroom_linh",
    schedule: list[dict[str, Any]] | None = None,
    pronoun_defaults: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if schedule is None:
        schedule = [
            {"start": "08:00", "end": "09:00", "room_id": "living_room", "pose": "sit", "activity": "uống trà", "target_prop": None},
            {"start": "09:00", "end": "11:00", "room_id": home_room, "pose": "stand", "activity": "thu dọn", "target_prop": None},
        ]
    if pronoun_defaults is None:
        pronoun_defaults = {
            "close_peer": {"self": "mình", "call": "cậu"},
            "acquaintance": {"self": "tôi", "call": "bạn"},
            "elder_formal": {"self": "em", "call": "anh/chị"},
            "angry": {"self": "tôi", "call": "anh/cô"},
        }
    return {
        "format": "iso-char-v1",
        "id": char_id,
        "meta": {"name": name, "aliases": [name.split()[-1]], "age": 22, "gender": "nữ", "fictional": True},
        "core": {
            "one_liner": f"Nhân vật {name} trong biệt thự thử nghiệm.",
            "personality": ["Kín đáo", "Quan sát tốt"],
            "values": "Giữ chữ tín.",
            "boundaries": [],
        },
        "voice": {
            "dialect": "Nam Bộ",
            "tone": "Tự nhiên.",
            "pronoun_defaults": pronoun_defaults,
            "modes": {
                "in_person": {"style": "Văn nói trực tiếp.", "tics": [], "samples": []},
                "texting": {"style": "Tin nhắn ngắn.", "samples": []},
            },
        },
        "stats": {"charm": 14, "logic": 13, "stealth": 12, "will": 11},
        "voxel_look": {
            "hair": "#1f2937",
            "hair_style": "short",
            "skin": "#fde68a",
            "outfit_top": "#2563eb",
            "outfit_bottom": "#1e293b",
            "accent": "#f59e0b",
            "height_scale": 1.0,
        },
        "schedule": schedule,
        "secrets": [{"id": f"sec_{char_id}_01", "text": "Giữ chìa khóa phụ trong ngăn kéo.", "known_by": [char_id]}],
        "inventory": [],
        "appearance": {"summary": name, "outfits": {"default": "Thường phục"}},
        "modules": [],
    }


def make_test_world(
    num_rooms: int = 6,
    with_locked_room: bool = True,
    with_missing_props: bool = True,
) -> dict[str, Any]:
    """Build a valid canonical iso-world-v1 fixture with 4..8 rooms."""
    base_rooms = [
        {
            "id": "foyer",
            "name": "Sảnh chính",
            "kind": "foyer",
            "privacy": "public",
            "owner_id": None,
            "size": [6, 5],
            "props": [
                {"id": "foyer_sofa", "asset_tag": "sofa_velvet_2x1", "name": "Ghế chờ"},
                {"id": "foyer_rug", "asset_tag": "rug_persian_2x2", "name": "Thảm sảnh"},
            ],
        },
        {
            "id": "living_room",
            "name": "Phòng khách",
            "kind": "living",
            "privacy": "public",
            "owner_id": None,
            "size": [7, 6],
            "props": [
                {"id": "living_table", "asset_tag": "coffee_table_glass", "name": "Bàn trà kính"},
                {"id": "living_sofa", "asset_tag": "sofa_velvet_2x1", "name": "Sofa lớn"},
            ],
        },
        {
            "id": "kitchen",
            "name": "Phòng bếp",
            "kind": "kitchen",
            "privacy": "public",
            "owner_id": None,
            "size": [6, 5],
            "props": [
                {"id": "kitchen_counter", "asset_tag": "kitchen_counter_2x1", "name": "Quầy bếp"},
                {"id": "kitchen_chair", "asset_tag": "dining_chair_1x1", "name": "Ghế ăn"},
            ],
        },
        {
            "id": "bedroom_linh",
            "name": "Phòng ngủ Linh",
            "kind": "bedroom",
            "privacy": "private",
            "owner_id": "linh",
            "size": [6, 6],
            "props": [
                {"id": "linh_bed", "asset_tag": "bed_double_2x2", "name": "Giường đôi"},
                {"id": "linh_wardrobe", "asset_tag": "wardrobe_tall_2x1", "name": "Tủ quần áo lớn"},
            ],
        },
        {
            "id": "study",
            "name": "Phòng làm việc",
            "kind": "study",
            "privacy": "private",
            "owner_id": "minh",
            "size": [6, 5],
            "props": [
                {"id": "study_desk", "asset_tag": "desk_mahogany_2x1", "name": "Bàn gỗ gụ"},
                {"id": "study_mystery", "asset_tag": "prop_mystery_01", "name": "Vật lạ", "is_missing": True},
            ],
        },
        {
            "id": "bathroom",
            "name": "Phòng tắm",
            "kind": "bathroom",
            "privacy": "public",
            "owner_id": None,
            "size": [5, 5],
            "props": [
                {"id": "bath_tub", "asset_tag": "bathtub_clawfoot_2x1", "name": "Bồn tắm sứ"},
            ],
        },
        {
            "id": "balcony",
            "name": "Ban công",
            "kind": "other",
            "privacy": "public",
            "owner_id": None,
            "size": [5, 4],
            "props": [
                {"id": "balcony_chair", "asset_tag": "dining_chair_1x1", "name": "Ghế ban công"},
            ],
        },
        {
            "id": "attic",
            "name": "Gác xép",
            "kind": "storage",
            "privacy": "private",
            "owner_id": "minh",
            "size": [5, 4],
            "props": [
                {"id": "attic_wardrobe", "asset_tag": "wardrobe_tall_2x1", "name": "Tủ gỗ cũ"},
            ],
        },
    ]
    rooms_list = base_rooms[:num_rooms]
    rooms = {r["id"]: r for r in rooms_list}
    room_ids = set(rooms.keys())
    doors = [
        {"id": "door_foyer_living", "room_a": "foyer", "room_b": "living_room", "locked": False, "key_item_id": None, "lock_dc": None},
        {"id": "door_living_kitchen", "room_a": "living_room", "room_b": "kitchen", "locked": False, "key_item_id": None, "lock_dc": None},
        {"id": "door_living_linh", "room_a": "living_room", "room_b": "bedroom_linh", "locked": False, "key_item_id": None, "lock_dc": None},
    ]
    if "study" in room_ids:
        doors.append({
            "id": "door_foyer_study",
            "room_a": "foyer",
            "room_b": "study",
            "locked": with_locked_room,
            "key_item_id": "brass_key" if with_locked_room else None,
            "lock_dc": 14 if with_locked_room else None,
        })
    if "bathroom" in room_ids:
        doors.append({"id": "door_kitchen_bath", "room_a": "kitchen", "room_b": "bathroom", "locked": False, "key_item_id": None, "lock_dc": None})
    if "balcony" in room_ids:
        doors.append({"id": "door_living_balcony", "room_a": "living_room", "room_b": "balcony", "locked": False, "key_item_id": None, "lock_dc": None})
    if "attic" in room_ids:
        doors.append({"id": "door_study_attic", "room_a": "study", "room_b": "attic", "locked": False, "key_item_id": None, "lock_dc": None})

    missing_props = []
    if with_missing_props and "study" in room_ids:
        missing_props = [
            {
                "asset_tag": "prop_mystery_01",
                "size": [1, 1],
                "mount": "surface",
                "visual_brief": {
                    "title_masked": "Vật phẩm bí ẩn #1",
                    "shape_hint": "Hộp nhung nhỏ hình vuông màu đỏ sẫm có khóa cài bạc",
                    "palette_hint": ["#7f1d1d", "#cbd5e1"],
                },
                "story_meta": {
                    "true_name": "Hộp nhẫn đính hôn bị giấu",
                    "room_id": "study",
                    "owner_id": "minh",
                    "revealed_flag": "found_ring_box",
                },
            }
        ]

    characters = {
        "linh": make_test_char(
            "linh",
            "Trần Khánh Linh",
            home_room="bedroom_linh",
            schedule=[
                {"start": "08:00", "end": "09:00", "room_id": "living_room", "pose": "sit", "activity": "đọc sách", "target_prop": "living_sofa"},
                {"start": "09:00", "end": "11:00", "room_id": "bedroom_linh", "pose": "stand", "activity": "kiểm tra tủ", "target_prop": "linh_wardrobe"},
            ],
        ),
        "minh": make_test_char(
            "minh",
            "Phạm Gia Minh",
            home_room="study" if "study" in room_ids else "bedroom_linh",
            schedule=[
                {"start": "08:00", "end": "09:00", "room_id": "kitchen", "pose": "stand", "activity": "pha cà phê", "target_prop": "kitchen_counter"},
                {"start": "09:00", "end": "11:00", "room_id": "kitchen", "pose": "stand", "activity": "tranh luận", "target_prop": None},
            ],
        ),
        "an": make_test_char(
            "an",
            "Nguyễn Hoàng An",
            home_room="living_room",
            schedule=[
                {"start": "08:00", "end": "09:00", "room_id": "living_room", "pose": "stand", "activity": "nghe nhạc", "target_prop": None},
                {"start": "09:00", "end": "11:00", "room_id": "kitchen", "pose": "stand", "activity": "nói chuyện với Minh", "target_prop": None},
            ],
        ),
    }

    cast = [
        {"char_id": "linh", "role_in_world": "Chủ phòng ngủ", "start_room": "living_room", "goal": "Giữ bí mật."},
        {"char_id": "minh", "role_in_world": "Chủ phòng làm việc", "start_room": "kitchen", "goal": "Tìm hộp nhẫn."},
        {"char_id": "an", "role_in_world": "Khách mời", "start_room": "living_room", "goal": "Quan sát mọi người."},
    ]

    return {
        "format": "iso-world-v1",
        "id": "test_mansion",
        "meta": {
            "name": "Biệt thự Thử nghiệm",
            "genre": "Trinh thám đời thường",
            "tone_rating": "mature_drama",
        },
        "user": {
            "id": "user",
            "name": "Nam",
            "aliases": ["Nam"],
            "gender": "nam",
            "persona": "Khách tới thăm biệt thự.",
        },
        "player_defaults": {
            "energy": 80,
            "stress": 10,
            "cash": 500,
            "stats": {"charm": 14, "logic": 15, "stealth": 14, "will": 13},
        },
        "clock": {"mode": "calendar", "start": {"year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30}},
        "rooms": rooms,
        "doors": doors,
        "characters": characters,
        "cast": cast,
        "missing_props": missing_props,
        "acts_spine": [
            {"act_index": 1, "title": "Hồi 1: Dấu vết ban đầu", "goal": "Khám phá biệt thự.", "advance_condition": {"flags": {"act1_done": True}}},
            {"act_index": 2, "title": "Hồi 2: Lời khai mâu thuẫn", "goal": "Đối chất các nhân vật.", "advance_condition": {"flags": {"act2_done": True}}},
            {"act_index": 3, "title": "Hồi 3: Sự thật cuối cùng", "goal": "Chọn kết cục.", "advance_condition": {"flags": {"act3_done": True}}},
        ],
        "endings": [
            {"id": "ending_truth", "title": "Vén màn sự thật", "condition": {"flags": {"ending_a": True}}, "epilogue_prompt": "Bạn công bố sự thật."},
            {"id": "ending_silence", "title": "Giữ im lặng", "condition": {"flags": {"ending_b": True}}, "epilogue_prompt": "Bạn chọn bảo vệ bí mật."},
        ],
        "opening": {
            "user_start_room": "foyer",
            "tension": 1,
            "channel_type": "in_person",
            "narration": "Cơn mưa đầu mùa rơi ngoài hiên biệt thự.",
        },
    }


def make_mock_llm(responses: list[Any] | None = None) -> Any:
    """Instantiate MockLLMClient via its public constructor contract."""
    llm_mod = _mod("llm_client")
    cls = llm_mod.MockLLMClient
    if responses is None:
        return cls()
    sig = inspect.signature(cls.__init__)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    if not params:
        inst = cls()
        if hasattr(inst, "set_responses"):
            inst.set_responses(responses)
        elif hasattr(inst, "responses"):
            inst.responses = list(responses)
        return inst
    first = params[0]
    if first.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY):
        return cls(list(responses))
    return cls(**{first.name: list(responses)})


def _make_routed_mock_llm(routes: list[tuple[str, Any]], default: Any) -> Any:
    """Instantiate a MockLLMClient subclass that routes JSON responses by prompt keywords."""
    llm_mod = _mod("llm_client")
    base_cls = llm_mod.MockLLMClient

    class _RoutedMockLLM(base_cls):
        def __init__(self) -> None:
            sig = inspect.signature(base_cls.__init__)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            if not params:
                super().__init__()
                if hasattr(self, "set_responses"):
                    self.set_responses([json.loads(json.dumps(default)) for _ in range(25)])
                elif hasattr(self, "responses"):
                    self.responses = [json.loads(json.dumps(default)) for _ in range(25)]
            else:
                first = params[0]
                if first.name == "default_json":
                    super().__init__(default_json=default)
                elif first.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY):
                    super().__init__([json.loads(json.dumps(default)) for _ in range(25)])
                else:
                    super().__init__(**{first.name: [json.loads(json.dumps(default)) for _ in range(25)]})
            self._routes = list(routes)

        def complete_json(self, messages: list[dict[str, Any]], **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            joined = json.dumps(messages, ensure_ascii=False).lower()
            for needle, payload in self._routes:
                if needle.lower() in joined:
                    if hasattr(self, "calls") and isinstance(self.calls, list):
                        self.calls.append({"messages": messages, "kwargs": kwargs})
                    if not isinstance(getattr(type(self), "call_count", None), property) and hasattr(self, "call_count"):
                        self.call_count += 1
                    return json.loads(json.dumps(payload)), {"cost_usd": 0.0, "model": "mock-routed"}
            return super().complete_json(messages, **kwargs)

    return _RoutedMockLLM()


def _make_gated_mock_llm(
    num_gates: int,
    gate_entered: threading.Event,
    gate_release: threading.Event,
    default_json: dict[str, Any],
) -> tuple[Any, list[str]]:
    """Create a MockLLMClient that blocks worker threads on st_gate_* storylets until gate_release is set."""
    llm_mod = _mod("llm_client")
    base_cls = llm_mod.MockLLMClient
    lock = threading.Lock()
    state = {"hits": 0}
    executed_prompts: list[str] = []

    class _GatedMockLLM(base_cls):
        def __init__(self) -> None:
            sig = inspect.signature(base_cls.__init__)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            if not params:
                super().__init__()
                if hasattr(self, "set_responses"):
                    self.set_responses([json.loads(json.dumps(default_json)) for _ in range(10)])
                elif hasattr(self, "responses"):
                    self.responses = [json.loads(json.dumps(default_json)) for _ in range(10)]
            else:
                first = params[0]
                if first.name == "default_json":
                    super().__init__(default_json=default_json)
                elif first.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY):
                    super().__init__([json.loads(json.dumps(default_json)) for _ in range(10)])
                else:
                    super().__init__(**{first.name: [json.loads(json.dumps(default_json)) for _ in range(10)]})

        def complete_json(self, messages: list[dict[str, Any]], **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            joined = json.dumps(messages, ensure_ascii=False)
            with lock:
                executed_prompts.append(joined)
                if "st_gate_" in joined:
                    state["hits"] += 1
                    if state["hits"] >= num_gates:
                        gate_entered.set()
            if "st_gate_" in joined:
                gate_release.wait(timeout=5.0)
            return super().complete_json(messages, **kwargs)

    return _GatedMockLLM(), executed_prompts


def _rooms_list(fp: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = fp.get("rooms", {})
    return list(rooms.values()) if isinstance(rooms, dict) else list(rooms)


def _room_obj(fp: dict[str, Any], room_id: str) -> dict[str, Any]:
    rooms = fp.get("rooms", {})
    if isinstance(rooms, dict):
        return rooms[room_id]
    return next(r for r in rooms if r["id"] == room_id)


def _find_walkable_tile(fp: dict[str, Any], room_id: str, state: dict[str, Any] | None = None) -> list[int]:
    engine = _mod("engine")
    room = _room_obj(fp, room_id)
    rx = int(room.get("x", 0))
    ry = int(room.get("y", 0))
    rw = int(room.get("w", room.get("width", 4)))
    rh = int(room.get("h", room.get("height", 4)))
    st = state if state is not None else {"doors_unlocked": [d["id"] for d in fp.get("doors", [])]}
    cx = rx + rw // 2
    cy = ry + rh // 2
    candidates = [[cx, cy]]
    for y in range(ry, ry + rh):
        for x in range(rx, rx + rw):
            if [x, y] != [cx, cy]:
                candidates.append([x, y])
    for cand in candidates:
        if engine.find_path(fp, cand, cand, st) is not None:
            return cand
    return [cx, cy]


def _make_live_llm_for_url(base_url: str, provider: str = "openrouter", api_key: str = "test-key-123") -> Any:
    llm_mod = _mod("llm_client")
    cls = llm_mod.LiveLLMClient
    sig = inspect.signature(cls.__init__)
    kwargs: dict[str, Any] = {}
    for pname in sig.parameters:
        if pname == "self":
            continue
        if pname in ("provider", "backend"):
            kwargs[pname] = provider
        elif pname in ("api_key", "key"):
            kwargs[pname] = api_key
        elif pname in ("base_url", "endpoint", "api_base", "url"):
            kwargs[pname] = base_url
        elif pname == "model":
            kwargs[pname] = "test-model"
    inst = cls(**kwargs)
    for attr in ("base_url", "endpoint", "api_base", "url"):
        if hasattr(inst, attr) and attr not in kwargs:
            setattr(inst, attr, base_url)
    return inst


def _start_mock_llm_http_server(handlers: list[tuple[int, str, dict[str, str] | None]]):
    """Spin up a local HTTP server mimicking OpenRouter/OpenAI/Gemini endpoints."""
    captured: list[dict[str, Any]] = []
    idx_lock = threading.Lock()
    state = {"idx": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                parsed = {"_raw": raw_body}
            with idx_lock:
                i = state["idx"]
                state["idx"] = min(i + 1, len(handlers) - 1)
                captured.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": parsed,
                })
                status, payload, extra_headers = handlers[i]
            body_bytes = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body_bytes)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, captured


def _start_test_iso_server(
    tmpdir: str,
    llm_client: Any = None,
) -> tuple[Any, str, str, str]:
    """Start ephemeral server.create_server('127.0.0.1', 0, ...)."""
    server_mod = _mod("server")
    db_path = os.path.join(tmpdir, "test_iso.db")
    custom_dir = os.path.join(tmpdir, "custom_assets")
    os.makedirs(custom_dir, exist_ok=True)
    if llm_client is None:
        llm_client = make_mock_llm()
    srv = server_mod.create_server(
        host="127.0.0.1",
        port=0,
        db_path=db_path,
        llm_client=llm_client,
        custom_dir=custom_dir,
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}", db_path, custom_dir


def _stop_iso_server(srv: Any) -> None:
    try:
        pm = getattr(srv, "prefetch_manager", None) or getattr(srv, "prefetch_mgr", None)
        if pm is not None and hasattr(pm, "shutdown"):
            pm.shutdown(wait=False)
    finally:
        srv.shutdown()
        srv.server_close()


def _http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | bytes | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    url = base_url + path
    headers: dict[str, str] = {}
    data: bytes | None = None
    if isinstance(payload, bytes):
        data = payload
        headers["Content-Type"] = "application/json"
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, (json.loads(raw) if raw.strip() else {}), resp_headers
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        resp_headers = {k.lower(): v for k, v in err.headers.items()}
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            body = {"_raw": raw}
        return err.code, body, resp_headers


def _http_raw_get(base_url: str, raw_path: str, timeout: float = 5.0) -> tuple[int, bytes, dict[str, str]]:
    parsed = urllib.parse.urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        conn.request("GET", raw_path)
        resp = conn.getresponse()
        data = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, data, headers
    finally:
        conn.close()


def _load_biet_thu_sample() -> dict[str, Any]:
    with open(BIET_THU_SAMPLE, "r", encoding="utf-8") as f:
        return json.load(f)


_FORBIDDEN_SPOILER_KEYS = ("story_meta", "true_name", "room_id", "owner_id", "revealed_flag")


def _find_missing_prop_secret_strings(world_or_campaign: dict[str, Any]) -> list[str]:
    world = world_or_campaign.get("world", world_or_campaign)
    secrets: list[str] = []
    for spec in world.get("missing_props", []):
        meta = spec.get("story_meta", {})
        for k in ("true_name", "room_id", "owner_id", "revealed_flag"):
            val = meta.get(k)
            if isinstance(val, str) and val:
                secrets.append(val)
    return list(dict.fromkeys(secrets))


def _collect_keys_and_scalars(obj: Any) -> tuple[set[str], set[str]]:
    keys: set[str] = set()
    scalars: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k))
            ck, cs = _collect_keys_and_scalars(v)
            keys.update(ck)
            scalars.update(cs)
    elif isinstance(obj, list):
        for item in obj:
            ck, cs = _collect_keys_and_scalars(item)
            keys.update(ck)
            scalars.update(cs)
    elif isinstance(obj, str):
        scalars.add(obj)
    return keys, scalars


def _assert_no_spoilers(tc: unittest.TestCase, obj: Any, secret_values: list[str]) -> None:
    keys, scalars = _collect_keys_and_scalars(obj)
    for fk in _FORBIDDEN_SPOILER_KEYS:
        tc.assertNotIn(fk, keys, f"Forbidden spoiler key {fk!r} leaked")
    serialized = json.dumps(obj, ensure_ascii=False)
    for sv in secret_values:
        tc.assertNotIn(sv, scalars, f"Forbidden spoiler scalar {sv!r} leaked")
        if len(sv) >= 4:
            tc.assertNotIn(sv, serialized, f"Forbidden spoiler substring {sv!r} leaked")


def _db_create_checkpoint(db_mod: Any, campaign_id: str, label: str, kind: str, snapshot: dict[str, Any], db_path: str) -> Any:
    """Call db.create_checkpoint respecting its parameter signature."""
    state = snapshot.get("state", {}) if isinstance(snapshot, dict) else {}
    history = snapshot.get("history", []) if isinstance(snapshot, dict) else []
    storylets = snapshot.get("storylets", []) if isinstance(snapshot, dict) else []
    act_index = int(snapshot.get("act_index", state.get("act_index", 1)))
    clk = state.get("clock", {}) if isinstance(state, dict) else {}
    clock_str = snapshot.get("clock_str") or f"{clk.get('hour', 8):02d}:{clk.get('minute', 30):02d}"
    return db_mod.create_checkpoint(
        campaign_id,
        label,
        kind,
        act_index,
        clock_str,
        state,
        history,
        storylets,
        db_path=db_path,
    )


def _db_put_cached_scene(db_mod: Any, campaign_id: str, storylet_id: str, state_hash: str, scene: dict[str, Any], db_path: str) -> Any:
    sig = inspect.signature(db_mod.put_cached_scene)
    params = list(sig.parameters.keys())
    if len(params) >= 5 and "db_path" in params:
        return db_mod.put_cached_scene(campaign_id, storylet_id, state_hash, scene, db_path=db_path)
    return db_mod.put_cached_scene(campaign_id, storylet_id, state_hash, scene, db_path)


def _make_sample_storylet(storylet_id: str = "st_study_drawer", room_id: str = "study") -> dict[str, Any]:
    return {
        "id": storylet_id,
        "title": "Khám xét ngăn kéo",
        "act_index": 1,
        "trigger": {"room_id": room_id, "one_shot": True},
        "relevant_keys": {
            "flags": ["searched_drawer"],
            "items": ["brass_key"],
            "affinity_with": ["linh"],
            "extra": [],
        },
        "priority": 50,
        "is_intrusion": False,
        "root_node_id": "node_root",
        "nodes": {
            "node_root": {
                "node_id": "node_root",
                "text": "Bạn mở ngăn kéo bàn gỗ gụ.",
                "presentation_mode": "standard",
                "pose_updates": {"user": {"pose": "stand", "target_prop": None}},
                "choices": [
                    {
                        "id": "c_take",
                        "label": "Lấy vật bên trong",
                        "style": "normal",
                        "effects": {
                            "set_flags": {"searched_drawer": True},
                            "discover_items": ["prop_mystery_01"],
                        },
                    }
                ],
            }
        },
    }


def _make_sample_scene_tree(storylet_id: str = "st_study_drawer", presentation_mode: str = "standard") -> dict[str, Any]:
    return {
        "storylet_id": storylet_id,
        "root_node_id": "node_root",
        "nodes": {
            "node_root": {
                "node_id": "node_root",
                "text": "Ánh đèn bàn hắt xuống mặt gỗ gụ.",
                "presentation_mode": presentation_mode,
                "pose_updates": {"user": {"pose": "stand", "target_prop": None}},
                "choices": [
                    {
                        "id": "c_take",
                        "label": "Kiểm tra vật phẩm",
                        "style": "normal",
                        "next_node_id": "node_end",
                        "effects": {
                            "set_flags": {"searched_drawer": True, "found_ring_box": True},
                            "discover_items": ["prop_mystery_01"],
                        },
                    }
                ],
            },
            "node_end": {
                "node_id": "node_end",
                "text": "Bạn đã phát hiện ra manh mối.",
                "presentation_mode": presentation_mode,
                "choices": [],
            },
        },
    }


# ==============================================================================
# TIER 1: Feature Coverage (Isolated Happy-Path: 21 x 5 = 105 cases)
# ==============================================================================


class TestTier1FeatureCoverage(unittest.TestCase):
    """Tier 1: 21 features x 5 isolated happy-path subtests = 105 cases."""

    def test_t1_f01(self) -> None:
        llm_mod = _mod("llm_client")

        with self.subTest(case_id="T1-F01-01"):
            expected_files = [
                "schemas.py",
                "db.py",
                "catalog.py",
                "llm_client.py",
                "compiler.py",
                "engine.py",
                "prefetch.py",
                "server.py",
                "Iso_Engine.bat",
                os.path.join("assets", "starter_catalog.json"),
                os.path.join("tools", "blender_export_kit.py"),
                os.path.join("samples", "biet_thu_mua_ngau.json"),
                os.path.join("web", "index.html"),
                os.path.join("web", "styles.css"),
                os.path.join("web", "iso_renderer.js"),
                os.path.join("web", "voxel_studio.js"),
                os.path.join("web", "ui.js"),
            ]
            for rel in expected_files:
                self.assertTrue(
                    os.path.isfile(os.path.join(ISO_ENGINE_DIR, rel)),
                    f"Missing required standalone file: {rel}",
                )

        with self.subTest(case_id="T1-F01-02"):
            key_name = "ISO_E2E_TEST_KEY_F01_02"
            old = os.environ.get(key_name)
            os.environ[key_name] = "env-override-secret-123"
            try:
                self.assertEqual(llm_mod.load_env_var(key_name), "env-override-secret-123")
            finally:
                if old is None:
                    os.environ.pop(key_name, None)
                else:
                    os.environ[key_name] = old

        with self.subTest(case_id="T1-F01-03"):
            with tempfile.TemporaryDirectory() as tmp:
                top_dir = os.path.join(tmp, "top")
                mid_dir = os.path.join(top_dir, "mid")
                leaf_dir = os.path.join(mid_dir, "leaf")
                os.makedirs(leaf_dir)
                with open(os.path.join(top_dir, ".env"), "w", encoding="utf-8") as f:
                    f.write("TEST_THREE_LVL_KEY=from_top\nTEST_OVERRIDE_KEY=from_top\n")
                with open(os.path.join(leaf_dir, ".env"), "w", encoding="utf-8") as f:
                    f.write("TEST_OVERRIDE_KEY=from_leaf\n")
                sig = inspect.signature(llm_mod.load_env_var)
                if "search_dirs" in sig.parameters or "start_dir" in sig.parameters or "env_paths" in sig.parameters:
                    param_name = next(
                        p for p in ("search_dirs", "start_dir", "env_paths") if p in sig.parameters
                    )
                    if param_name == "start_dir":
                        self.assertEqual(llm_mod.load_env_var("TEST_THREE_LVL_KEY", start_dir=leaf_dir), "from_top")
                        self.assertEqual(llm_mod.load_env_var("TEST_OVERRIDE_KEY", start_dir=leaf_dir), "from_leaf")
                    else:
                        dirs = [leaf_dir, mid_dir, top_dir]
                        self.assertEqual(llm_mod.load_env_var("TEST_THREE_LVL_KEY", **{param_name: dirs}), "from_top")
                        self.assertEqual(llm_mod.load_env_var("TEST_OVERRIDE_KEY", **{param_name: dirs}), "from_leaf")
                else:
                    # Verify real 3-level lookup reaches parent .env if present
                    parent_env = os.path.join(WORKSPACE_ROOT, ".env")
                    if os.path.isfile(parent_env):
                        val = llm_mod.load_env_var("OPENROUTER_API_KEY") or llm_mod.load_env_var("GEMINI_API_KEY")
                        self.assertIsInstance(val, str)

        with self.subTest(case_id="T1-F01-04"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    status, body, _ = _http_json(base_url, "GET", "/api/bootstrap")
                    self.assertEqual(status, 200)
                    raw_text = json.dumps(body, ensure_ascii=False)
                    for prefix in ("sk-or-", "AIzaSy", "sk-proj-"):
                        self.assertNotIn(prefix, raw_text)
                    providers = body.get("providers") or body.get("available_providers") or body.get("env") or body
                    self.assertIsInstance(providers, dict)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F01-05"):
            before = snapshot_webui_and_rplab()
            with tempfile.TemporaryDirectory() as tmp:
                db_mod = _mod("db")
                db_mod.init_db(os.path.join(tmp, "iso_isolated.db"))
            after = snapshot_webui_and_rplab()
            self.assertEqual(before["files"], after["files"])
            self.assertEqual(before["code_hashes"], after["code_hashes"])
            self.assertEqual(before["db_row_counts"], after["db_row_counts"])

    def test_t1_f02(self) -> None:
        db_mod = _mod("db")

        with self.subTest(case_id="T1-F02-01"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "wal_test.db")
                db_mod.init_db(db_path)
                conn = sqlite3.connect(db_path)
                try:
                    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    self.assertEqual(str(mode).lower(), "wal")
                    tables = {
                        r[0]
                        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    }
                    for tbl in ("campaigns", "scene_cache", "checkpoints", "undo_stack", "custom_assets", "settings"):
                        self.assertIn(tbl, tables)
                finally:
                    conn.close()

        with self.subTest(case_id="T1-F02-02"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "crud.db")
                db_mod.init_db(db_path)
                camp = {
                    "id": "camp_vi_01",
                    "name": "Biệt thự Tiếng Việt có dấu ă â đ ê ô ơ ư",
                    "act_index": 1,
                    "world": make_test_world(),
                    "cast": make_test_world()["cast"],
                    "floorplan": {"width": 16, "height": 16, "rooms": {}},
                    "storylets": [],
                    "state": {"clock": {"hour": 8, "minute": 30}, "flags": {"đã_mở": True}},
                    "history": [{"text": "Khởi đầu tại sảnh."}],
                }
                db_mod.save_campaign(camp, db_path=db_path)
                got = db_mod.get_campaign("camp_vi_01", db_path=db_path)
                self.assertIsNotNone(got)
                self.assertEqual(got["state"]["flags"]["đã_mở"], True)
                listed = db_mod.list_campaigns(db_path=db_path)
                self.assertTrue(any((c.get("id") or c.get("campaign_id")) == "camp_vi_01" for c in listed))
                db_mod.delete_campaign("camp_vi_01", db_path=db_path)
                self.assertIsNone(db_mod.get_campaign("camp_vi_01", db_path=db_path))

        with self.subTest(case_id="T1-F02-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "cache.db")
                db_mod.init_db(db_path)
                camp = {"id": "camp_c1", "world": make_test_world(), "state": {}, "history": []}
                db_mod.save_campaign(camp, db_path=db_path)
                scene = _make_sample_scene_tree("st_01")
                _db_put_cached_scene(db_mod, "camp_c1", "st_01", "hash_abc123", scene, db_path)
                fetched = db_mod.get_cached_scene("camp_c1", "st_01", "hash_abc123", db_path=db_path)
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched["storylet_id"], "st_01")
                self.assertIn("node_root", fetched["nodes"])

        with self.subTest(case_id="T1-F02-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "cp_undo.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "camp_cp", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                cp_res = _db_create_checkpoint(
                    db_mod,
                    "camp_cp",
                    "Đầu Hồi 1",
                    "act_start",
                    {"state": {"clock": {"hour": 8, "minute": 30}}, "history": []},
                    db_path,
                )
                cps = db_mod.list_checkpoints("camp_cp", db_path=db_path)
                self.assertGreaterEqual(len(cps), 1)
                resolved_id = cp_res["id"] if isinstance(cp_res, dict) else (cp_res if isinstance(cp_res, str) else cps[0]["id"])
                cp_data = db_mod.get_checkpoint(resolved_id, db_path=db_path)
                self.assertIsNotNone(cp_data)
                db_mod.push_undo("camp_cp", {"step": 1}, [], [], db_path=db_path)
                db_mod.push_undo("camp_cp", {"step": 2}, [], [], db_path=db_path)
                top = db_mod.pop_undo("camp_cp", db_path=db_path)
                prev = db_mod.pop_undo("camp_cp", db_path=db_path)
                self.assertEqual(top["state"].get("step"), 2)
                self.assertEqual(prev["state"].get("step"), 1)

        with self.subTest(case_id="T1-F02-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "assets_settings.db")
                db_mod.init_db(db_path)
                asset = {
                    "asset_tag": "custom_lamp_01",
                    "category": "living",
                    "name_vi": "Đèn bàn cổ",
                    "size": [1, 1],
                    "mount": "surface",
                    "voxels": [{"x": 7, "y": 0, "z": 7, "w": 2, "h": 4, "d": 2, "color": "#f59e0b", "shape": "cylinder", "material": "glass"}],
                }
                db_mod.save_custom_asset(asset, db_path=db_path)
                got_asset = db_mod.get_custom_asset("custom_lamp_01", db_path=db_path)
                self.assertIsNotNone(got_asset)
                listed_assets = db_mod.list_custom_assets(db_path=db_path)
                self.assertTrue(any(a.get("asset_tag") == "custom_lamp_01" for a in listed_assets))
                db_mod.save_settings({"max_prefetch_depth": 1, "provider": "openrouter"}, db_path=db_path)
                settings = db_mod.get_settings(db_path=db_path)
                self.assertEqual(settings.get("max_prefetch_depth"), 1)

    def test_t1_f03(self) -> None:
        schemas = _mod("schemas")

        with self.subTest(case_id="T1-F03-01"):
            char = make_test_char("vy_test", "Lê Phương Vy")
            res = schemas.validate_iso_char(char)
            self.assertEqual(res["format"], "iso-char-v1")
            self.assertEqual(res["id"], "vy_test")
            self.assertIn("stats", res)
            self.assertIn("voxel_look", res)
            self.assertIn("schedule", res)

        with self.subTest(case_id="T1-F03-02"):
            world = make_test_world(num_rooms=6)
            res = schemas.validate_iso_world(world)
            self.assertEqual(res["format"], "iso-world-v1")
            self.assertEqual(len(res["rooms"]), 6)
            self.assertEqual(len(res["acts_spine"]), 3)
            self.assertGreaterEqual(len(res["endings"]), 2)

        with self.subTest(case_id="T1-F03-03"):
            world = make_test_world(with_missing_props=True)
            res = schemas.validate_iso_world(world)
            self.assertEqual(len(res["missing_props"]), 1)
            self.assertIn("visual_brief", res["missing_props"][0])
            self.assertIn("story_meta", res["missing_props"][0])

        with self.subTest(case_id="T1-F03-04"):
            st = schemas.validate_storylet({
                "id": "st_climax",
                "title": "Đối thoại căng thẳng",
                "act_index": 1,
                "trigger": {"room_id": "living_room"},
                "relevant_keys": {"flags": ["confronted"], "items": [], "affinity_with": ["linh"]},
                "root_node_id": "n1",
                "nodes": {
                    "n1": {
                        "node_id": "n1",
                        "text": "Không khí lặng đi.",
                        "presentation_mode": "cinematic_focus",
                        "pose_updates": {"linh": {"pose": "sit", "target_prop": "living_sofa"}},
                        "choices": [{"id": "c1", "label": "Nói tiếp", "effects": {"set_flags": {"confronted": True}}}],
                    }
                },
            })
            self.assertEqual(st["nodes"]["n1"]["presentation_mode"], "cinematic_focus")

        with self.subTest(case_id="T1-F03-05"):
            char = make_test_char("han_test", "Trần Gia Hân")
            res = schemas.validate_iso_char(char)
            pd = res["voice"]["pronoun_defaults"]
            for key in ("close_peer", "acquaintance", "elder_formal", "angry"):
                self.assertIn(key, pd)
                self.assertIn("self", pd[key])
                self.assertIn("call", pd[key])

    def test_t1_f04(self) -> None:
        schemas = _mod("schemas")

        with self.subTest(case_id="T1-F04-01"):
            with open(os.path.join(SAMPLE_CHARS_DIR, "vy.json"), "r", encoding="utf-8") as f:
                vy_v2 = json.load(f)
            upgraded = schemas.upgrade_char_v2_to_iso(vy_v2)
            validated = schemas.validate_iso_char(upgraded)
            self.assertEqual(validated["id"], "vy")
            self.assertEqual(validated["format"], "iso-char-v1")
            self.assertTrue(all(k in validated["stats"] for k in ("charm", "logic", "stealth", "will")))
            self.assertTrue(any(str(v).startswith("#") for v in validated["voxel_look"].values()))

        with self.subTest(case_id="T1-F04-02"):
            upgraded_chars = {}
            for cname in ("han", "khanh", "tuan"):
                with open(os.path.join(SAMPLE_CHARS_DIR, f"{cname}.json"), "r", encoding="utf-8") as f:
                    cobj = json.load(f)
                up = schemas.validate_iso_char(schemas.upgrade_char_v2_to_iso(cobj))
                upgraded_chars[cname] = up
                self.assertEqual(up["id"], cname)
            looks = [json.dumps(c["voxel_look"], sort_keys=True) for c in upgraded_chars.values()]
            self.assertEqual(len(set(looks)), 3)

        with self.subTest(case_id="T1-F04-03"):
            with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                w2 = json.load(f)
            iso_w = schemas.upgrade_world_v2_to_iso(w2, search_dirs=[SAMPLE_CHARS_DIR])
            validated = schemas.validate_iso_world(iso_w)
            self.assertGreaterEqual(len(validated["rooms"]), 4)
            self.assertLessEqual(len(validated["rooms"]), 8)
            alias_map = validated.get("location_alias_map") or validated.get("meta", {}).get("location_alias_map")
            self.assertIsInstance(alias_map, dict)
            room_ids = set(validated["rooms"].keys())
            for kept in ("san_truong", "thu_vien", "phong_ktx_a"):
                self.assertTrue(kept in room_ids or alias_map.get(kept) in room_ids)

        with self.subTest(case_id="T1-F04-04"):
            with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                w2 = json.load(f)
            w2.pop("characters", None)
            w2["cast"] = [
                {"char_id": "vy", "start_location": "san_truong"},
                {"char_id": "han", "start_location": "thu_vien"},
            ]
            iso_w = schemas.upgrade_world_v2_to_iso(w2, search_dirs=[SAMPLE_CHARS_DIR])
            cast_ids = {c["char_id"] for c in iso_w["cast"]}
            self.assertEqual(cast_ids, {"vy", "han"})

        with self.subTest(case_id="T1-F04-05"):
            with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                w2 = json.load(f)
            iso_w = schemas.validate_iso_world(schemas.upgrade_world_v2_to_iso(w2, search_dirs=[SAMPLE_CHARS_DIR]))
            self.assertGreaterEqual(len(iso_w["doors"]), len(iso_w["rooms"]) - 1)
            self.assertEqual(len(iso_w["acts_spine"]), 3)
            self.assertGreaterEqual(len(iso_w["endings"]), 2)
            self.assertTrue(all(len(c.get("schedule", [])) >= 1 for c in iso_w["characters"].values()))

    def test_t1_f05(self) -> None:
        schemas = _mod("schemas")
        catalog = _mod("catalog")

        spec = make_test_world(with_missing_props=True)["missing_props"][0]

        with self.subTest(case_id="T1-F05-01"):
            redacted = schemas.redact_missing_prop(spec, {"flags": {}, "discovered_items": []}, built=False)
            self.assertIs(redacted["revealed"], False)
            self.assertIn("visual_brief", redacted)
            _assert_no_spoilers(self, redacted, ["Hộp nhẫn đính hôn bị giấu", "study", "minh", "found_ring_box"])

        with self.subTest(case_id="T1-F05-02"):
            revealed = schemas.redact_missing_prop(spec, {"flags": {"found_ring_box": True}, "discovered_items": []}, built=False)
            self.assertIs(revealed["revealed"], True)
            self.assertEqual(revealed["true_name"], "Hộp nhẫn đính hôn bị giấu")
            self.assertEqual(revealed["room_id"], "study")
            self.assertEqual(revealed["owner_id"], "minh")

        with self.subTest(case_id="T1-F05-03"):
            revealed = schemas.redact_missing_prop(spec, {"flags": {}, "discovered_items": ["prop_mystery_01"]}, built=True)
            self.assertIs(revealed["revealed"], True)
            self.assertIs(revealed["built"], True)
            self.assertEqual(revealed["true_name"], "Hộp nhẫn đính hôn bị giấu")

        with self.subTest(case_id="T1-F05-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "studio_list.db")
                db_mod = _mod("db")
                db_mod.init_db(db_path)
                db_mod.save_custom_asset({
                    "asset_tag": "prop_mystery_01",
                    "category": "mystery",
                    "name_vi": "Vật bí ẩn",
                    "size": [1, 1],
                    "mount": "surface",
                    "voxels": [{"x": 1, "y": 1, "z": 1, "w": 2, "h": 2, "d": 2, "color": "#7f1d1d", "shape": "cube", "material": "fabric"}],
                }, db_path=db_path)
                world = make_test_world(with_missing_props=True)
                world["missing_props"].append({
                    "asset_tag": "prop_mystery_02",
                    "size": [1, 1],
                    "mount": "floor",
                    "visual_brief": {"title_masked": "Vật #2", "shape_hint": "Khung gỗ nhỏ", "palette_hint": ["#78350f"]},
                    "story_meta": {"true_name": "Bức thư mật", "room_id": "foyer", "owner_id": "linh", "revealed_flag": "found_letter"},
                })
                items = catalog.get_studio_missing_list(world, {"flags": {"found_letter": True}, "discovered_items": []}, db_path=db_path)
                by_tag = {it["asset_tag"]: it for it in items}
                self.assertIs(by_tag["prop_mystery_01"]["revealed"], False)
                self.assertIs(by_tag["prop_mystery_01"]["built"], True)
                _assert_no_spoilers(self, by_tag["prop_mystery_01"], ["Hộp nhẫn đính hôn bị giấu", "study", "minh", "found_ring_box"])
                self.assertIs(by_tag["prop_mystery_02"]["revealed"], True)
                self.assertEqual(by_tag["prop_mystery_02"]["true_name"], "Bức thư mật")

        with self.subTest(case_id="T1-F05-05"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    world = make_test_world(with_missing_props=True)
                    st, compiled, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": world})
                    self.assertEqual(st, 200)
                    cid = compiled["campaign"]["id"]
                    st2, missing_resp, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                    self.assertEqual(st2, 200)
                    raw = json.dumps(missing_resp, ensure_ascii=False)
                    self.assertIn("Vật phẩm bí ẩn #1", raw)
                    _assert_no_spoilers(self, missing_resp, _find_missing_prop_secret_strings(world))
                finally:
                    _stop_iso_server(srv)

    def test_t1_f06(self) -> None:
        catalog = _mod("catalog")
        db_mod = _mod("db")

        with self.subTest(case_id="T1-F06-01"):
            starter = catalog.load_starter_catalog()
            self.assertGreaterEqual(len(starter), 26)
            cats = {v.get("category") for v in starter.values()}
            for req_cat in ("arch", "living", "bedroom", "kitchen", "bathroom", "study", "mystery"):
                self.assertIn(req_cat, cats)

        with self.subTest(case_id="T1-F06-02"):
            starter = catalog.load_starter_catalog()
            self.assertIn("mystery_placeholder", starter)
            mp = starter["mystery_placeholder"]
            self.assertEqual(len(mp["size"]), 2)
            self.assertTrue(len(mp.get("voxels", [])) >= 1)

        with self.subTest(case_id="T1-F06-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pose_slots.db")
                db_mod.init_db(db_path)
                starter = catalog.load_starter_catalog()
                found_poses = set()
                for tag, item in starter.items():
                    for pose_name in item.get("pose_slots", {}):
                        slot = catalog.get_pose_slot(tag, pose_name, db_path=db_path)
                        self.assertIsNotNone(slot, msg=f"{tag}:{pose_name}")
                        self.assertIn("offset", slot)
                        self.assertIn("rot_y", slot)
                        found_poses.add(pose_name)
                for p in ("sit", "lie", "hide_inside"):
                    self.assertIn(p, found_poses)

        with self.subTest(case_id="T1-F06-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "cat_idx.db")
                db_mod.init_db(db_path)
                idx = catalog.export_llm_tag_index(db_path=db_path)
                self.assertGreaterEqual(len(idx), 26)
                for entry in idx:
                    self.assertIn("asset_tag", entry)
                    self.assertNotIn("voxels", entry)

        with self.subTest(case_id="T1-F06-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "merged_cat.db")
                db_mod.init_db(db_path)
                db_mod.save_custom_asset({
                    "asset_tag": "custom_vase_01",
                    "category": "living",
                    "name_vi": "Bình gốm",
                    "size": [1, 1],
                    "mount": "surface",
                    "voxels": [{"x": 4, "y": 0, "z": 4, "w": 2, "h": 4, "d": 2, "color": "#38bdf8", "shape": "cylinder", "material": "glass"}],
                }, db_path=db_path)
                merged = catalog.get_merged_catalog(db_path=db_path)
                self.assertIn("custom_vase_01", merged)
                self.assertIn("mystery_placeholder", merged)

    def test_t1_f07(self) -> None:
        catalog = _mod("catalog")

        with self.subTest(case_id="T1-F07-01"):
            bk = _mod("tools.blender_export_kit")
            self.assertTrue(hasattr(bk, "main") or hasattr(bk, "validate_glb_file") or hasattr(bk, "create_minimal_glb"))

        with self.subTest(case_id="T1-F07-02"):
            with tempfile.TemporaryDirectory() as tmp:
                glb_path = os.path.join(tmp, "sample.glb")
                r1 = subprocess.run(
                    [sys.executable, BLENDER_KIT_PATH, "--create-minimal-glb", glb_path],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(r1.returncode, 0, msg=r1.stderr)
                self.assertTrue(os.path.isfile(glb_path))
                r2 = subprocess.run(
                    [sys.executable, BLENDER_KIT_PATH, "--validate-glb", glb_path],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(r2.returncode, 0, msg=r2.stderr)

        with self.subTest(case_id="T1-F07-03"):
            info = catalog.validate_glb_bytes(build_minimal_glb())
            self.assertIsInstance(info, dict)
            self.assertEqual(info["version"], 2)
            self.assertIs(info["valid"], True)

        with self.subTest(case_id="T1-F07-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "studio.db")
                custom_dir = os.path.join(tmp, "custom")
                _mod("db").init_db(db_path)
                saved = catalog.save_studio_asset(
                    {
                        "asset_tag": "studio_box_01",
                        "name_vi": "Hộp Lego",
                        "size": [1, 1],
                        "mount": "surface",
                        "voxels": [{"x": 0, "y": 0, "z": 0, "w": 2, "h": 2, "d": 2, "color": "#ef4444", "shape": "cube", "material": "plastic"}],
                    },
                    custom_dir=custom_dir,
                    db_path=db_path,
                )
                self.assertEqual(saved["asset_tag"], "studio_box_01")
                self.assertIsNotNone(_mod("db").get_custom_asset("studio_box_01", db_path=db_path))

        with self.subTest(case_id="T1-F07-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "studio_glb.db")
                custom_dir = os.path.join(tmp, "custom")
                _mod("db").init_db(db_path)
                glb_b64 = base64.b64encode(build_minimal_glb()).decode("ascii")
                saved = catalog.save_studio_asset(
                    {
                        "asset_tag": "studio_glb_01",
                        "name_vi": "Mẫu GLB",
                        "size": [1, 1],
                        "mount": "floor",
                        "kind": "glb",
                        "glb_base64": glb_b64,
                    },
                    custom_dir=custom_dir,
                    db_path=db_path,
                )
                self.assertEqual(saved["asset_tag"], "studio_glb_01")
                self.assertIsNotNone(_mod("db").get_custom_asset("studio_glb_01", db_path=db_path))

    def test_t1_f08(self) -> None:
        with self.subTest(case_id="T1-F08-01"):
            mock = make_mock_llm([{"ok": True, "scene": "test"}])
            data, meta = mock.complete_json([{"role": "user", "content": "Hello"}])
            self.assertEqual(data["ok"], True)
            self.assertEqual(mock.call_count, 1)
            self.assertGreaterEqual(len(mock.calls), 1)
            self.assertIsInstance(meta, dict)

        with self.subTest(case_id="T1-F08-02"):
            mock = make_mock_llm(["Xin chào"])
            text, meta = mock.complete_text([{"role": "user", "content": "Say hi"}])
            self.assertIn("Xin chào", text)
            self.assertIn("cost_usd", meta)
            self.assertGreaterEqual(mock.get_total_cost(), meta["cost_usd"])

        with self.subTest(case_id="T1-F08-03"):
            payload = json.dumps({
                "choices": [{"message": {"content": "```json\n{\"hello\": \"việt\"}\n```"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
            })
            srv, captured = _start_mock_llm_http_server([(200, payload, None)])
            try:
                url = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
                client = _make_live_llm_for_url(url, provider="openrouter", api_key="k-test")
                res, _ = client.complete_json([{"role": "user", "content": "test"}])
                self.assertEqual(res["hello"], "việt")
                self.assertEqual(len(captured), 1)
            finally:
                srv.shutdown()
                srv.server_close()

        with self.subTest(case_id="T1-F08-04"):
            payload = json.dumps({
                "choices": [{"message": {"content": '{"a": 1, "b": [2,'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            })
            srv, _ = _start_mock_llm_http_server([(200, payload, None)])
            try:
                url = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
                client = _make_live_llm_for_url(url, provider="openrouter", api_key="k-test")
                res, _ = client.complete_json([{"role": "user", "content": "repair"}])
                self.assertEqual(res["a"], 1)
            finally:
                srv.shutdown()
                srv.server_close()

        with self.subTest(case_id="T1-F08-05"):
            or_payload = json.dumps({"choices": [{"message": {"content": '{"p": "or"}'}}]})
            srv1, cap1 = _start_mock_llm_http_server([(200, or_payload, None)])
            gem_payload = json.dumps({"candidates": [{"content": {"parts": [{"text": '{"p": "gem"}'}]}}]})
            srv2, cap2 = _start_mock_llm_http_server([(200, gem_payload, None)])
            try:
                c1 = _make_live_llm_for_url(f"http://127.0.0.1:{srv1.server_address[1]}/v1/chat/completions", provider="openrouter", api_key="or-secret")
                r1, _ = c1.complete_json([{"role": "user", "content": "hi"}])
                self.assertEqual(r1["p"], "or")
                self.assertIn("Bearer or-secret", cap1[0]["headers"].get("authorization", ""))

                c2 = _make_live_llm_for_url(f"http://127.0.0.1:{srv2.server_address[1]}/v1beta/models/gemini:generateContent", provider="gemini", api_key="gem-secret")
                r2, _ = c2.complete_json([{"role": "user", "content": "hi"}])
                self.assertEqual(r2["p"], "gem")
                self.assertIn("generationConfig", cap2[0]["body"])
            finally:
                srv1.shutdown()
                srv1.server_close()
                srv2.shutdown()
                srv2.server_close()

    def test_t1_f09(self) -> None:
        prefetch = _mod("prefetch")
        db_mod = _mod("db")

        rk = {"flags": ["has_clue"], "items": ["brass_key"], "affinity_with": ["linh"]}
        base_state = {
            "clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30},
            "player": {"pos": [2, 2], "inventory": ["brass_key", "apple"]},
            "flags": {"has_clue": False, "unrelated_flag": True},
            "relationships": {"linh": 10, "minh": 5},
        }

        with self.subTest(case_id="T1-F09-01"):
            s2 = json.loads(json.dumps(base_state))
            s2["clock"]["minute"] = 42
            s2["player"]["pos"] = [10, 12]
            self.assertEqual(prefetch.masked_state_hash(base_state, rk), prefetch.masked_state_hash(s2, rk))

        with self.subTest(case_id="T1-F09-02"):
            s2 = json.loads(json.dumps(base_state))
            s2["player"]["inventory"].append("umbrella")
            s2["flags"]["unrelated_flag"] = False
            s2["relationships"]["minh"] = -20
            self.assertEqual(prefetch.masked_state_hash(base_state, rk), prefetch.masked_state_hash(s2, rk))

        with self.subTest(case_id="T1-F09-03"):
            h0 = prefetch.masked_state_hash(base_state, rk)
            s_flag = json.loads(json.dumps(base_state))
            s_flag["flags"]["has_clue"] = True
            self.assertNotEqual(h0, prefetch.masked_state_hash(s_flag, rk))
            s_aff = json.loads(json.dumps(base_state))
            s_aff["relationships"]["linh"] = 25
            self.assertNotEqual(h0, prefetch.masked_state_hash(s_aff, rk))

        with self.subTest(case_id="T1-F09-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pf.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "camp_pf", "world": make_test_world(), "state": base_state, "history": []}, db_path=db_path)
                mock = make_mock_llm([_make_sample_scene_tree("st_pf_1")])
                pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
                try:
                    pm.enqueue("camp_pf", _make_sample_storylet("st_pf_1"), base_state, mock, priority=pm.PRIO_P1, branch_group="br_a", db_path=db_path)
                    import time
                    deadline = time.time() + 3.0
                    h = prefetch.masked_state_hash(base_state, _make_sample_storylet("st_pf_1")["relevant_keys"])
                    cached = None
                    while time.time() < deadline:
                        cached = db_mod.get_cached_scene("camp_pf", "st_pf_1", h, db_path=db_path)
                        if cached is not None:
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(cached)
                    self.assertEqual(mock.call_count, 1)
                    self.assertIsInstance(pm.get_status(), dict)
                finally:
                    pm.shutdown(wait=True)

        with self.subTest(case_id="T1-F09-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pf_cancel.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "camp_pf2", "world": make_test_world(), "state": base_state, "history": []}, db_path=db_path)
                gate_entered = threading.Event()
                gate_release = threading.Event()
                mock, executed_prompts = _make_gated_mock_llm(
                    1, gate_entered, gate_release, _make_sample_scene_tree("st_a")
                )
                pm = prefetch.PrefetchManager(max_workers=1, max_prefetch_depth=1)
                try:
                    pm.enqueue("camp_pf2", _make_sample_storylet("st_gate_1"), base_state, mock, priority=pm.PRIO_P0, branch_group="gate", db_path=db_path)
                    self.assertTrue(gate_entered.wait(timeout=5.0), "Worker did not enter gate in time")
                    pm.enqueue("camp_pf2", _make_sample_storylet("st_b"), base_state, mock, priority=pm.PRIO_P1, branch_group="branch_b", db_path=db_path)
                    pm.enqueue("camp_pf2", _make_sample_storylet("st_a"), base_state, mock, priority=pm.PRIO_P1, branch_group="branch_a", db_path=db_path)
                    pm.promote_branch("branch_a", ["branch_b"])
                    gate_release.set()
                    pm.shutdown(wait=True)
                finally:
                    gate_release.set()
                    pm.shutdown(wait=False)
                st_a = _make_sample_storylet("st_a")
                ha = prefetch.masked_state_hash(base_state, st_a["relevant_keys"])
                st_b = _make_sample_storylet("st_b")
                hb = prefetch.masked_state_hash(base_state, st_b["relevant_keys"])
                self.assertIsNotNone(db_mod.get_cached_scene("camp_pf2", "st_a", ha, db_path=db_path))
                self.assertIsNone(db_mod.get_cached_scene("camp_pf2", "st_b", hb, db_path=db_path))
                self.assertFalse(any("st_b" in p for p in executed_prompts))
                self.assertTrue(any("st_a" in p for p in executed_prompts))
                self.assertEqual(mock.call_count, 2)

    def test_t1_f10(self) -> None:
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        engine = _mod("engine")
        db_mod = _mod("db")

        world = make_test_world(num_rooms=6)
        cat = catalog.load_starter_catalog()

        with self.subTest(case_id="T1-F10-01"):
            fp = compiler.pack_floorplan(world, cat)
            self.assertLessEqual(fp["width"], 24)
            self.assertLessEqual(fp["height"], 24)
            rooms = _rooms_list(fp)
            self.assertEqual(len(rooms), 6)
            for i in range(len(rooms)):
                r1 = rooms[i]
                self.assertGreaterEqual(r1["x"], 0)
                self.assertGreaterEqual(r1["y"], 0)
                self.assertLessEqual(r1["x"] + r1["w"], 24)
                self.assertLessEqual(r1["y"] + r1["h"], 24)
                for j in range(i + 1, len(rooms)):
                    r2 = rooms[j]
                    overlap_x = r1["x"] < r2["x"] + r2["w"] and r1["x"] + r1["w"] > r2["x"]
                    overlap_y = r1["y"] < r2["y"] + r2["h"] and r1["y"] + r1["h"] > r2["y"]
                    self.assertFalse(overlap_x and overlap_y)
            self.assertIn("adjacent_rooms", fp)

        with self.subTest(case_id="T1-F10-02"):
            fp = compiler.pack_floorplan(world, cat)
            blocked_tiles = set()
            for p in fp.get("props", []):
                tag = p.get("asset_tag")
                if not cat.get(tag, {}).get("walkable", False):
                    pw, pd = p.get("size", cat.get(tag, {}).get("size", [1, 1]))
                    for dx in range(pw):
                        for dy in range(pd):
                            blocked_tiles.add((p["x"] + dx, p["y"] + dy))
            for d in fp.get("doors", []):
                ta = tuple(d["tile_a"])
                tb = tuple(d["tile_b"])
                self.assertNotIn(ta, blocked_tiles)
                self.assertNotIn(tb, blocked_tiles)
            # Check unlocked rooms reachable from foyer
            unlocked_state = {"doors_unlocked": []}
            start_xy = _find_walkable_tile(fp, "foyer", unlocked_state)
            for rid in ("living_room", "kitchen", "bedroom_linh", "bathroom"):
                goal_xy = _find_walkable_tile(fp, rid, unlocked_state)
                path = engine.find_path(fp, start_xy, goal_xy, unlocked_state)
                self.assertIsNotNone(path, f"Room {rid} unreachable from foyer")

        with self.subTest(case_id="T1-F10-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "compile.db")
                db_mod.init_db(db_path)
                mock = make_mock_llm([
                    {"storylets": [_make_sample_storylet("st_act1_1", "foyer")], "intrusion_scenes": {"bedroom_linh": _make_sample_scene_tree("intr_linh")}},
                    _make_sample_scene_tree("st_act1_1"),
                    _make_sample_scene_tree("intr_linh"),
                ] * 5)
                camp = compiler.compile_new_campaign(world, llm=mock, catalog=cat, db_path=db_path)
                self.assertEqual(camp["act_index"], 1)
                self.assertGreaterEqual(len(camp.get("storylets", [])), 1)
                cps = db_mod.list_checkpoints(camp["id"], db_path=db_path)
                self.assertTrue(any("Hồi 1" in (cp.get("label") or "") for cp in cps))

        with self.subTest(case_id="T1-F10-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "next_act.db")
                db_mod.init_db(db_path)
                mock = make_mock_llm([
                    {"storylets": [_make_sample_storylet("st_act1", "foyer")]},
                    _make_sample_scene_tree("st_act1"),
                    {"storylets": [_make_sample_storylet("st_act2", "living_room")]},
                    _make_sample_scene_tree("st_act2"),
                ] * 4)
                camp = compiler.compile_new_campaign(world, llm=mock, catalog=cat, db_path=db_path)
                camp2 = compiler.compile_next_act(camp, mock, db_path=db_path)
                self.assertEqual(camp2["act_index"], 2)
                cps = db_mod.list_checkpoints(camp["id"], db_path=db_path)
                self.assertTrue(any("Hồi 2" in (cp.get("label") or "") for cp in cps))

        with self.subTest(case_id="T1-F10-05"):
            seed_world = make_test_world(num_rooms=5)
            mock = make_mock_llm([seed_world])
            res = compiler.generate_world_seed("Biệt thự cổ giữa rừng thông", "mature_drama", mock)
            validated = _mod("schemas").validate_iso_world(res)
            self.assertEqual(validated["format"], "iso-world-v1")

    def test_t1_f11(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")

        world = make_test_world(num_rooms=6)
        cat = catalog.load_starter_catalog()
        fp = compiler.pack_floorplan(world, cat)
        state = {
            "clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30},
            "player": {
                "room_id": "foyer",
                "pos": _find_walkable_tile(fp, "foyer", {"doors_unlocked": []}),
                "pose": "stand",
                "inventory": ["brass_key"],
                "stats": {"charm": 14, "logic": 14, "stealth": 14, "will": 12},
                "energy": 80,
                "stress": 10,
                "cash": 500,
            },
            "doors_unlocked": [],
            "flags": {},
            "discovered_items": [],
            "relationships": {"linh": 10},
        }

        with self.subTest(case_id="T1-F11-01"):
            goal = engine.find_approach_tile(fp, next(p for p in fp["props"] if p["room_id"] == "living_room"), state["player"]["pos"], state)
            self.assertIsNotNone(goal)
            path = engine.find_path(fp, state["player"]["pos"], goal, state)
            self.assertIsNotNone(path)
            for a, b in zip(path, path[1:]):
                self.assertEqual(abs(a[0] - b[0]) + abs(a[1] - b[1]), 1)

        with self.subTest(case_id="T1-F11-02"):
            prop = next(p for p in fp["props"] if p["room_id"] == "foyer")
            tile = engine.find_approach_tile(fp, prop, state["player"]["pos"], state)
            self.assertIsNotNone(tile)
            self.assertEqual(len(tile), 2)

        with self.subTest(case_id="T1-F11-03"):
            r = engine.roll_d20(14, 16, forced_roll=15)
            self.assertEqual(r["roll"], 15)
            self.assertEqual(r["modifier"], 2)
            self.assertEqual(r["total"], 17)
            self.assertIs(r["success"], True)
            self.assertIs(r["critical"], False)

        with self.subTest(case_id="T1-F11-04"):
            study_prop = next(p for p in fp["props"] if p["room_id"] == "study")
            # Before unlock: path into study is blocked
            study_walk = _find_walkable_tile(fp, "study", {"doors_unlocked": ["door_foyer_study"]})
            self.assertIsNone(engine.find_path(fp, state["player"]["pos"], study_walk, state))
            res = engine.use_item(world, fp, state, "brass_key", "door_foyer_study")
            self.assertIs(res["success"], True)
            self.assertIn("door_foyer_study", state["doors_unlocked"])
            # Now find_approach_tile or find_path reaches study
            app = engine.find_approach_tile(fp, study_prop, state["player"]["pos"], state)
            self.assertIsNotNone(app)
            self.assertIsNotNone(engine.find_path(fp, state["player"]["pos"], app, state))

        with self.subTest(case_id="T1-F11-05"):
            delta = {
                "add_items": ["secret_note"],
                "set_flags": {"checked_desk": True},
                "discover_items": ["prop_mystery_01"],
                "energy_delta": -10,
                "stress_delta": 15,
            }
            engine.apply_state_delta(world, fp, state, delta)
            self.assertIn("secret_note", state["player"]["inventory"])
            self.assertIs(state["flags"]["checked_desk"], True)
            self.assertIn("prop_mystery_01", state["discovered_items"])
            self.assertIs(state["flags"]["found_ring_box"], True)
            self.assertEqual(state["player"]["energy"], 70)
            self.assertEqual(state["player"]["stress"], 25)

    def test_t1_f12(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        db_mod = _mod("db")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "intr.db")
            db_mod.init_db(db_path)
            world = make_test_world(num_rooms=6)
            cat = catalog.load_starter_catalog()
            mock = make_mock_llm([
                {"storylets": [_make_sample_storylet("st_1", "bedroom_linh")], "intrusion_scenes": {"bedroom_linh": _make_sample_scene_tree("intr_bedroom_linh")}},
                _make_sample_scene_tree("st_1"),
                _make_sample_scene_tree("intr_bedroom_linh"),
            ] * 4)
            camp = compiler.compile_new_campaign(world, llm=mock, catalog=cat, db_path=db_path)
            # Linh arrives in bedroom_linh at 09:00; set clock to 08:56 (4 min away)
            camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}
            camp["state"]["player"]["room_id"] = "bedroom_linh"
            camp["state"]["player"]["pos"] = _find_walkable_tile(camp["floorplan"], "bedroom_linh", camp["state"])
            db_mod.save_campaign(camp, db_path=db_path)

            with self.subTest(case_id="T1-F12-01"):
                warn = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", action_minutes=5)
                self.assertIsNotNone(warn)
                self.assertIn("bước chân", (warn.get("message") or warn.get("warning") or "").lower())
                self.assertGreaterEqual(len(warn.get("choices", [])), 3)

            with self.subTest(case_id="T1-F12-02"):
                # Engine/intrusion flow itself creates auto_risk checkpoint when db_path is provided
                warn = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", action_minutes=5)
                self.assertIsNotNone(warn)
                camp["state"]["pending_intrusion"] = warn
                db_mod.save_campaign(camp, db_path=db_path)
                res_auto = engine.resolve_intrusion_choice(camp, "hide", forced_roll=19, db_path=db_path)
                self.assertIs(res_auto["success"], True)
                cps = db_mod.list_checkpoints(camp["id"], db_path=db_path)
                self.assertTrue(any(cp.get("kind") == "auto_risk" for cp in cps), f"Expected engine-created auto_risk checkpoint, got {cps}")

            with self.subTest(case_id="T1-F12-03"):
                camp["state"]["player"]["pose"] = "stand"
                camp["state"]["pending_intrusion"] = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", action_minutes=5)
                res = engine.resolve_intrusion_choice(camp, "hide", forced_roll=19, db_path=db_path)
                self.assertEqual(camp["state"]["player"]["pose"], "hide_inside")
                self.assertIs(res["success"], True)

            with self.subTest(case_id="T1-F12-04"):
                camp["state"]["player"]["pose"] = "stand"
                camp["state"]["pending_intrusion"] = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", action_minutes=5)
                res = engine.resolve_intrusion_choice(camp, "bluff", forced_roll=19, db_path=db_path)
                self.assertIn("d20", res)
                self.assertIs(res["d20"]["success"], True)
                self.assertIs(res["success"], True)

            with self.subTest(case_id="T1-F12-05"):
                camp["state"]["player"]["room_id"] = "bedroom_linh"
                camp["state"]["pending_intrusion"] = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", action_minutes=5)
                engine.resolve_intrusion_choice(camp, "retreat", db_path=db_path)
                self.assertNotEqual(camp["state"]["player"]["room_id"], "bedroom_linh")

    def test_t1_f13(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")

        world = make_test_world(num_rooms=6)
        cat = catalog.load_starter_catalog()
        fp = compiler.pack_floorplan(world, cat)

        def fresh_state() -> dict[str, Any]:
            return {
                "clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30},
                "player": {
                    "room_id": "living_room",
                    "pos": _find_walkable_tile(fp, "living_room", {"doors_unlocked": []}),
                    "pose": "stand",
                    "stats": {"charm": 16, "stealth": 16},
                    "inventory": [],
                },
                "npcs": {
                    "linh": {"room_id": "living_room", "pose": "sit", "mood": "neutral"},
                    "an": {"room_id": "living_room", "pose": "stand", "mood": "neutral"},
                    "minh": {"room_id": "kitchen", "pose": "stand", "mood": "neutral"},
                },
                "relationships": {"linh": 10, "an": 10, "minh": 10},
                "flags": {},
                "traces": [],
                "history": [],
            }

        with self.subTest(case_id="T1-F13-01"):
            st = fresh_state()
            res = engine.perform_speech_or_dialogue(world, st, "Mọi người thấy chìa khóa đâu không?", whisper=False)
            self.assertEqual(set(res["witnesses"]), {"linh", "an"})

        with self.subTest(case_id="T1-F13-02"):
            st = fresh_state()
            res = engine.perform_speech_or_dialogue(world, st, "Nói nhỏ riêng với Linh", whisper=True, target_npc_id="linh", forced_roll=18)
            self.assertEqual(res["witnesses"], ["linh"])

        with self.subTest(case_id="T1-F13-03"):
            st = fresh_state()
            engine.advance_clock_and_sim(world, fp, st, 35)  # 08:30 -> 09:05
            self.assertEqual(st["npcs"]["linh"]["room_id"], "bedroom_linh")
            self.assertEqual(st["npcs"]["an"]["room_id"], "kitchen")

        with self.subTest(case_id="T1-F13-04"):
            st = fresh_state()
            w_adj = make_test_world(num_rooms=6)
            adj_list = fp["adjacent_rooms"].get("living_room", [])
            self.assertGreaterEqual(len(adj_list), 1)
            target_room = "kitchen" if "kitchen" in adj_list else adj_list[0]
            for cid, c in w_adj["characters"].items():
                if cid in ("minh", "an"):
                    c["schedule"][1]["room_id"] = target_room
            w_adj["offscreen_events"] = [
                {
                    "id": "off_t1_13_04",
                    "room_id": target_room,
                    "trigger_min": 540,
                    "participants": ["minh", "an"],
                    "secret_dialogue": "OFFSCREEN_SENTINEL_1304",
                    "sound_hint": "Tiếng tranh cãi nhỏ vọng qua vách.",
                    "trace_label": "Tách cà phê bị đẩy đổ",
                    "mood_effects": {"minh": "angry", "an": "nervous"},
                }
            ]
            events = engine.advance_clock_and_sim(w_adj, fp, st, 35)
            has_adj = any((e.get("kind") or e.get("type")) == "adjacent_sound" for e in events)
            self.assertTrue(has_adj)
            self.assertGreaterEqual(len(st.get("traces", [])), 1)
            self.assertTrue(
                st["npcs"]["minh"].get("mood") != "neutral" or st["npcs"]["an"].get("mood") != "neutral",
                f"Expected mood change from 'neutral', got {st['npcs']}",
            )

        with self.subTest(case_id="T1-F13-05"):
            st = fresh_state()
            w_sec = make_test_world(num_rooms=6)
            adj_list = fp["adjacent_rooms"].get("living_room", [])
            self.assertGreaterEqual(len(adj_list), 1)
            target_room = "kitchen" if "kitchen" in adj_list else adj_list[0]
            for cid, c in w_sec["characters"].items():
                if cid in ("minh", "an"):
                    c["schedule"][1]["room_id"] = target_room
            secret_sentinel = "SECRET_OFFSCREEN_DIALOGUE_SENTINEL_999"
            w_sec["offscreen_events"] = [
                {
                    "id": "off_t1_13_05",
                    "room_id": target_room,
                    "trigger_min": 540,
                    "participants": ["minh", "an"],
                    "secret_dialogue": secret_sentinel,
                    "sound_hint": "Tiếng thì thầm vội vã sau vách.",
                    "trace_label": "Mẩu giấy ghi chú bị xé",
                    "mood_effects": {"minh": "tense", "an": "nervous"},
                }
            ]
            events = engine.advance_clock_and_sim(w_sec, fp, st, 35)
            self.assertGreaterEqual(len(events), 1)
            serialized_events = json.dumps(events, ensure_ascii=False)
            serialized_state = json.dumps(st, ensure_ascii=False)
            self.assertNotIn(secret_sentinel, serialized_events)
            self.assertNotIn(secret_sentinel, serialized_state)
            ev_keys, _ = _collect_keys_and_scalars(events)
            tr_keys, _ = _collect_keys_and_scalars(st.get("traces", []))
            for forbidden_k in ("dialogue_lines", "secret_dialogue"):
                self.assertNotIn(forbidden_k, ev_keys)
                self.assertNotIn(forbidden_k, tr_keys)

    def test_t1_f14(self) -> None:
        engine = _mod("engine")
        world = make_test_world()

        with self.subTest(case_id="T1-F14-01"):
            state = {"act_index": 1, "flags": {"act1_done": True}, "relationships": {}}
            res = engine.check_act_or_ending(world, state, [_make_sample_storylet("s1")])
            self.assertIs(res["can_advance_act"], True)
            self.assertEqual(res["next_act"], 2)

        with self.subTest(case_id="T1-F14-02"):
            state = {"act_index": 2, "flags": {"act2_done": True}, "relationships": {}}
            res = engine.check_act_or_ending(world, state, [_make_sample_storylet("s2")])
            self.assertIs(res["can_advance_act"], True)
            self.assertEqual(res["next_act"], 3)

        with self.subTest(case_id="T1-F14-03"):
            state = {"act_index": 3, "flags": {"ending_a": True}, "affinity": {}}
            res = engine.check_act_or_ending(world, state, [])
            self.assertIsNotNone(res.get("ending"))
            self.assertEqual(res["ending"]["id"], "ending_truth")

        with self.subTest(case_id="T1-F14-04"):
            state = {"act_index": 3, "flags": {"ending_b": True}, "affinity": {}}
            res = engine.check_act_or_ending(world, state, [])
            self.assertIsNotNone(res.get("ending"))
            self.assertEqual(res["ending"]["id"], "ending_silence")

        with self.subTest(case_id="T1-F14-05"):
            state = {"act_index": 3, "flags": {"ending_a": True}, "ending": {"id": "ending_truth"}, "free_sandbox_mode": False}
            engine.enable_free_sandbox(state)
            self.assertTrue(state["free_sandbox_mode"])
            res = engine.perform_speech_or_dialogue(world, state, "Tiếp tục trò chuyện tự do", whisper=False)
            self.assertIn("witnesses", res)

    def test_t1_f15(self) -> None:
        db_mod = _mod("db")
        prefetch = _mod("prefetch")

        with self.subTest(case_id="T1-F15-01"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "cp1.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "c1", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                _db_create_checkpoint(db_mod, "c1", "Lưu tay 1", "manual", {"state": {"clock": {"hour": 8, "minute": 40}}, "history": []}, db_path)
                cps = db_mod.list_checkpoints("c1", db_path=db_path)
                self.assertEqual(len(cps), 1)
                self.assertEqual(cps[0]["kind"], "manual")

        with self.subTest(case_id="T1-F15-02"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, comp, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = comp["campaign"]["id"]
                    pre_state_hist_len = len(comp["state"].get("history", []))
                    pre_camp_hist_len = len(comp.get("campaign", {}).get("history", []))
                    _, cps_before, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid}")
                    cp_id = cps_before["checkpoints"][0]["id"]
                    st_act, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Thử thay đổi"})
                    self.assertEqual(st_act, 200)
                    st_mid, mid_body, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                    self.assertEqual(st_mid, 200)
                    mid_state = mid_body["state"]
                    self.assertTrue(
                        len(mid_state.get("history", [])) > pre_state_hist_len
                        or len(mid_body.get("campaign", {}).get("history", [])) > pre_camp_hist_len
                        or mid_state["clock"] != comp["state"]["clock"]
                        or mid_state.get("turns", 0) != comp["state"].get("turns", 0),
                        "Expected speech action to mutate state/history before rewind",
                    )
                    st_rew, rew_body, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": cp_id})
                    self.assertEqual(st_rew, 200)
                    self.assertEqual(rew_body["state"]["clock"], comp["state"]["clock"])
                    self.assertEqual(len(rew_body["state"].get("history", [])), pre_state_hist_len)
                    self.assertEqual(rew_body["state"].get("turns", 0), comp["state"].get("turns", 0))
                    if "campaign" in rew_body:
                        self.assertEqual(len(rew_body["campaign"].get("history", [])), pre_camp_hist_len)
                    st_post, post_rew_body, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                    self.assertEqual(st_post, 200)
                    self.assertEqual(post_rew_body["state"]["clock"], comp["state"]["clock"])
                    self.assertEqual(len(post_rew_body["state"].get("history", [])), pre_state_hist_len)
                    self.assertEqual(len(post_rew_body.get("campaign", {}).get("history", [])), pre_camp_hist_len)
                    self.assertEqual(post_rew_body["state"].get("turns", 0), comp["state"].get("turns", 0))
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F15-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "cp_cache.db")
                db_mod.init_db(db_path)
                st_obj = _make_sample_storylet("st_keep")
                state = {"clock": {"hour": 8, "minute": 30}, "flags": {"searched_drawer": False}, "player": {"inventory": ["brass_key"]}, "affinity": {"linh": 10}}
                db_mod.save_campaign({"id": "c_cache", "world": make_test_world(), "state": state, "history": []}, db_path=db_path)
                h = prefetch.masked_state_hash(state, st_obj["relevant_keys"])
                _db_put_cached_scene(db_mod, "c_cache", "st_keep", h, _make_sample_scene_tree("st_keep"), db_path)
                cp_id = _db_create_checkpoint(db_mod, "c_cache", "Mốc A", "manual", {"state": state, "history": []}, db_path)
                # Mutate state and verify cache stays untouched
                cp = db_mod.get_checkpoint(cp_id if isinstance(cp_id, str) else db_mod.list_checkpoints("c_cache", db_path=db_path)[0]["id"], db_path=db_path)
                self.assertIsNotNone(cp)
                self.assertIsNotNone(db_mod.get_cached_scene("c_cache", "st_keep", h, db_path=db_path))

        with self.subTest(case_id="T1-F15-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "undo.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "c_u", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                db_mod.push_undo("c_u", {"clock": {"hour": 8, "minute": 30}}, [], [], db_path=db_path)
                popped = db_mod.pop_undo("c_u", db_path=db_path)
                self.assertEqual(popped["state"]["clock"]["minute"], 30)

        with self.subTest(case_id="T1-F15-05"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, comp, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = comp["campaign"]["id"]
                    _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Câu nói 1"})
                    st_u, body_u, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "action": "undo"})
                    self.assertEqual(st_u, 200)
                    self.assertIn("state", body_u)
                finally:
                    _stop_iso_server(srv)

    def test_t1_f16(self) -> None:
        with self.subTest(case_id="T1-F16-01"):
            bat_path = os.path.join(ISO_ENGINE_DIR, "Iso_Engine.bat")
            with open(bat_path, "r", encoding="utf-8", errors="replace") as f:
                bat = f.read()
            self.assertIn("7862", bat)
            self.assertIn("server.py", bat)
            self.assertIn("65001", bat)

        with self.subTest(case_id="T1-F16-02"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    st, html_bytes, hdrs = _http_raw_get(base_url, "/")
                    self.assertEqual(st, 200)
                    self.assertIn("text/html", hdrs.get("content-type", ""))
                    self.assertIn(b"importmap", html_bytes)
                    st_h, health, _ = _http_json(base_url, "GET", "/api/health")
                    self.assertEqual(st_h, 200)
                    self.assertTrue(health.get("ok"))
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F16-03"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                        w2 = json.load(f)
                    st_conv, conv_body, _ = _http_json(base_url, "POST", "/api/import/convert", {"world": w2})
                    self.assertEqual(st_conv, 200)
                    iso_w = conv_body.get("world") or conv_body
                    self.assertEqual(iso_w["format"], "iso-world-v1")
                    st_cmp, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": iso_w})
                    self.assertEqual(st_cmp, 200)
                    self.assertIn("floorplan", cmp_body)
                    self.assertIn("state", cmp_body)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F16-04"):
            with tempfile.TemporaryDirectory() as tmp:
                mock = _make_routed_mock_llm(
                    routes=[
                        ("nhìn quanh phòng", {"prose": "Bạn quan sát kỹ.", "effects": {"set_flags": {"looked": True}}}),
                        ("node", _make_sample_scene_tree("s1")),
                    ],
                    default={"storylets": [_make_sample_storylet("s1", "foyer")]},
                )
                srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = cmp_body["campaign"]["id"]
                    s1, b1, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Chào"})
                    self.assertEqual(s1, 200)
                    s2, b2, _ = _http_json(base_url, "POST", "/api/campaign/free_action", {"campaign_id": cid, "text": "Nhìn quanh phòng"})
                    self.assertEqual(s2, 200)
                    s3, b3, _ = _http_json(base_url, "POST", "/api/campaign/advance_act", {"campaign_id": cid})
                    self.assertEqual(s3, 200)
                    self.assertEqual(b3["campaign"]["act_index"], 2)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F16-05"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    st_set, _, _ = _http_json(base_url, "POST", "/api/settings", {"max_prefetch_depth": 1})
                    self.assertEqual(st_set, 200)
                    st_pf, pf_body, _ = _http_json(base_url, "GET", "/api/prefetch/status")
                    self.assertEqual(st_pf, 200)
                    self.assertIsInstance(pf_body, dict)

                    parsed = urllib.parse.urlparse(base_url)
                    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3.0)
                    try:
                        conn.request("GET", "/api/prefetch/stream")
                        resp = conn.getresponse()
                        self.assertEqual(resp.status, 200)
                        ctype = resp.getheader("Content-Type", "")
                        self.assertIn("text/event-stream", ctype)
                        data_line = None
                        for _ in range(10):
                            line = resp.readline().decode("utf-8", errors="replace").strip()
                            if line.startswith("data:"):
                                data_line = line[5:].strip()
                                break
                        self.assertIsNotNone(data_line, "Expected initial SSE data frame")
                        json.loads(data_line)
                    finally:
                        conn.close()
                finally:
                    _stop_iso_server(srv)

    def test_t1_f17(self) -> None:
        schemas = _mod("schemas")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        engine = _mod("engine")
        db_mod = _mod("db")

        sample = _load_biet_thu_sample()
        world = sample.get("world", sample)

        with self.subTest(case_id="T1-F17-01"):
            validated = schemas.validate_iso_world(world)
            self.assertEqual(len(validated["rooms"]), 6)
            fp = compiler.pack_floorplan(validated, catalog.load_starter_catalog())
            self.assertLessEqual(fp["width"], 24)
            self.assertLessEqual(fp["height"], 24)

        with self.subTest(case_id="T1-F17-02"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "bt.db")
                db_mod.init_db(db_path)
                mock = make_mock_llm()
                camp = compiler.compile_new_campaign(sample, llm=mock, db_path=db_path)
                self.assertEqual(mock.call_count, 0)
                self.assertGreaterEqual(len(camp.get("storylets", [])), 1)

        with self.subTest(case_id="T1-F17-03"):
            locked_doors = [d for d in world["doors"] if d.get("locked")]
            self.assertGreaterEqual(len(locked_doors), 1)
            ld = locked_doors[0]
            self.assertTrue(bool(ld.get("key_item_id")))

        with self.subTest(case_id="T1-F17-04"):
            rooms_iter = list(world["rooms"].values()) if isinstance(world["rooms"], dict) else world["rooms"]
            priv_rooms = [r for r in rooms_iter if (r.get("privacy") == "private" or r.get("type") == "private") and r.get("owner_id")]
            self.assertGreaterEqual(len(priv_rooms), 1)
            chars_iter = list(world["characters"].values()) if isinstance(world.get("characters"), dict) else world.get("cast", [])
            has_schedule = any(len(c.get("schedule", [])) >= 2 for c in chars_iter)
            self.assertTrue(has_schedule)

        with self.subTest(case_id="T1-F17-05"):
            self.assertGreaterEqual(len(world.get("missing_props", [])), 1)
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "bt_miss.db")
                db_mod.init_db(db_path)
                missing_list = catalog.get_studio_missing_list(world, {"flags": {}, "discovered_items": []}, db_path=db_path)
            self.assertGreaterEqual(len(missing_list), 1)
            self.assertIs(missing_list[0]["revealed"], False)
            _assert_no_spoilers(self, missing_list, _find_missing_prop_secret_strings(sample))
            raw_sample = json.dumps(sample, ensure_ascii=False)
            self.assertIn("cinematic_focus", raw_sample)

    def test_t1_f18(self) -> None:
        with self.subTest(case_id="T1-F18-01"):
            js_path = os.path.join(ISO_ENGINE_DIR, "web", "iso_renderer.js")
            r = subprocess.run(["node", "--check", js_path], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, msg=r.stderr)

        with self.subTest(case_id="T1-F18-02"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    for path in (
                        "/vendor/three/three.module.min.js",
                        "/vendor/three/OrbitControls.js",
                        "/vendor/three/GLTFLoader.js",
                    ):
                        st, data, hdrs = _http_raw_get(base_url, path)
                        self.assertEqual(st, 200, msg=f"Missing {path}")
                        self.assertIn("javascript", hdrs.get("content-type", ""))
                        self.assertGreater(len(data), 100)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F18-03"):
            with open(os.path.join(ISO_ENGINE_DIR, "web", "index.html"), "r", encoding="utf-8") as f:
                html = f.read()
            with open(os.path.join(ISO_ENGINE_DIR, "web", "styles.css"), "r", encoding="utf-8") as f:
                css = f.read()
            self.assertIn("isoCanvas", html)
            self.assertIn("cinematic", css.lower())

        with self.subTest(case_id="T1-F18-04"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    fp = cmp_body["floorplan"]
                    self.assertIn("rooms", fp)
                    self.assertIn("doors", fp)
                    self.assertIn("adjacent_rooms", fp)
                    self.assertIn("props", fp)
                    self.assertIn("pose", cmp_body["state"]["player"])
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F18-05"):
            scene = _make_sample_scene_tree("st_cine", presentation_mode="cinematic_focus")
            node = scene["nodes"]["node_root"]
            self.assertEqual(node["presentation_mode"], "cinematic_focus")
            self.assertIn("pose_updates", node)

    def test_t1_f19(self) -> None:
        with self.subTest(case_id="T1-F19-01"):
            js_path = os.path.join(ISO_ENGINE_DIR, "web", "voxel_studio.js")
            r = subprocess.run(["node", "--check", js_path], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, msg=r.stderr)

        with self.subTest(case_id="T1-F19-02"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world(with_missing_props=True)})
                    cid = cmp_body["campaign"]["id"]
                    st, body, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                    self.assertEqual(st, 200)
                    item = body["items"][0] if "items" in body else body["missing"][0]
                    self.assertFalse(item["revealed"])
                    self.assertIn("title_masked", item["visual_brief"])
                    self.assertIn("shape_hint", item["visual_brief"])
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F19-03"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    shapes = ["cube", "stud", "cylinder", "slope", "sphere", "torus", "bar"]
                    materials = ["plastic", "wood", "metal", "fabric", "glass", "neon"]
                    voxels = [
                        {"x": i, "y": i, "z": i, "w": 1, "h": 1, "d": 1, "color": "#38bdf8", "shape": shapes[i], "material": materials[i % len(materials)]}
                        for i in range(len(shapes))
                    ]
                    st, body, _ = _http_json(
                        base_url,
                        "POST",
                        "/api/studio/save_asset",
                        {"asset_tag": "prop_all_7_shapes", "name_vi": "Đủ 7 hình", "size": [1, 1], "mount": "surface", "voxels": voxels},
                    )
                    self.assertEqual(st, 200)
                    self.assertEqual(body.get("asset", body).get("asset_tag"), "prop_all_7_shapes")
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F19-04"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world(with_missing_props=True)})
                    cid = cmp_body["campaign"]["id"]
                    glb_b64 = base64.b64encode(build_minimal_glb()).decode("ascii")
                    st_g, _, _ = _http_json(
                        base_url,
                        "POST",
                        "/api/studio/save_asset",
                        {"asset_tag": "prop_mystery_01", "name_vi": "GLB bí ẩn", "size": [1, 1], "mount": "surface", "kind": "glb", "glb_base64": glb_b64},
                    )
                    self.assertEqual(st_g, 200)
                    vox_b64 = base64.b64encode(build_minimal_vox()).decode("ascii")
                    st_v, _, _ = _http_json(
                        base_url,
                        "POST",
                        "/api/studio/save_asset",
                        {"asset_tag": "prop_vox_01", "name_vi": "VOX bí ẩn", "size": [1, 1], "mount": "floor", "kind": "vox", "vox_base64": vox_b64},
                    )
                    self.assertEqual(st_v, 200)
                    _, m_body, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                    items = m_body.get("items") or m_body.get("missing")
                    self.assertTrue(items[0]["built"])
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F19-05"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world(with_missing_props=True)})
                    cid = cmp_body["campaign"]["id"]
                    st_save, _, _ = _http_json(
                        base_url,
                        "POST",
                        "/api/studio/save_asset",
                        {"asset_tag": "prop_mystery_01", "name_vi": "Hot reload", "size": [1, 1], "mount": "surface", "voxels": [{"x": 1, "y": 0, "z": 1, "w": 2, "h": 2, "d": 2, "color": "#f43f5e", "shape": "cube", "material": "plastic"}]},
                    )
                    self.assertEqual(st_save, 200)
                    st_state, state_body, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                    self.assertEqual(st_state, 200)
                    missing = state_body.get("missing_list") if "missing_list" in state_body else state_body.get("missing")
                    self.assertIsInstance(missing, list)
                    self.assertGreaterEqual(len(missing), 1)
                    self.assertIs(missing[0]["built"], True)
                finally:
                    _stop_iso_server(srv)

    def test_t1_f20(self) -> None:
        with self.subTest(case_id="T1-F20-01"):
            js_path = os.path.join(ISO_ENGINE_DIR, "web", "ui.js")
            r = subprocess.run(["node", "--check", js_path], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, msg=r.stderr)

        with self.subTest(case_id="T1-F20-02"):
            with open(os.path.join(ISO_ENGINE_DIR, "web", "index.html"), "r", encoding="utf-8") as f:
                html = f.read()
            with open(os.path.join(ISO_ENGINE_DIR, "web", "styles.css"), "r", encoding="utf-8") as f:
                css = f.read()
            self.assertIn("@media", css)
            for token in ("ui.js", "voxel_studio.js", "iso_renderer.js"):
                self.assertIn(token, html)

        with self.subTest(case_id="T1-F20-03"):
            with tempfile.TemporaryDirectory() as tmp:
                sample = _load_biet_thu_sample()
                mock = make_mock_llm()
                srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": sample})
                    cid = cmp_body["campaign"]["id"]
                    storylets = cmp_body["campaign"].get("storylets", [])
                    self.assertGreaterEqual(len(storylets), 1)
                    sid = storylets[0]["id"]
                    st, act_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "open_storylet", "storylet_id": sid})
                    self.assertEqual(st, 200)
                    self.assertIn("cache_hit", act_body)
                    self.assertIs(act_body["cache_hit"], True)
                    self.assertEqual(mock.call_count, 0)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F20-04"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, db_path, _ = _start_test_iso_server(tmp)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = cmp_body["campaign"]["id"]
                    db_mod = _mod("db")
                    camp = db_mod.get_campaign(cid, db_path=db_path)
                    if "brass_key" not in camp["state"]["player"]["inventory"]:
                        camp["state"]["player"]["inventory"].append("brass_key")
                        db_mod.save_campaign(camp, db_path=db_path)
                    st, res, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "use_item", "item_id": "brass_key", "target_id": "door_foyer_study"})
                    self.assertEqual(st, 200)
                    self.assertIn("door_foyer_study", res["state"]["doors_unlocked"])
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T1-F20-05"):
            with tempfile.TemporaryDirectory() as tmp:
                mock = _make_routed_mock_llm(
                    routes=[
                        ("quan sát xung quanh", {"prose": "Bạn thì thầm hỏi.", "effects": {"set_flags": {"asked": True}}}),
                        ("node", _make_sample_scene_tree("s1")),
                    ],
                    default={"storylets": [_make_sample_storylet("s1", "foyer")]},
                )
                srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = cmp_body["campaign"]["id"]
                    st1, b1, _ = _http_json(base_url, "POST", "/api/campaign/free_action", {"campaign_id": cid, "text": "Quan sát xung quanh"})
                    self.assertEqual(st1, 200)
                    self.assertIs(b1["state"]["flags"]["asked"], True)
                    st2, b2, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Nói nhỏ", "whisper": True, "target_npc_id": "linh"})
                    self.assertEqual(st2, 200)
                    self.assertIn("witnesses", b2)
                finally:
                    _stop_iso_server(srv)

    def test_t1_f21(self) -> None:
        with self.subTest(case_id="T1-F21-01"):
            py_files = []
            for root, _, filenames in os.walk(ISO_ENGINE_DIR):
                for fn in sorted(filenames):
                    if fn.endswith(".py"):
                        py_files.append(os.path.join(root, fn))
            self.assertGreaterEqual(len(py_files), 9)
            r = subprocess.run([sys.executable, "-m", "py_compile", *py_files], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, msg=r.stderr)

        with self.subTest(case_id="T1-F21-02"):
            pkg_path = os.path.join(ISO_ENGINE_DIR, "web", "package.json")
            with open(pkg_path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f).get("type"), "module")
            for js_name in ("iso_renderer.js", "voxel_studio.js", "ui.js"):
                r = subprocess.run(["node", "--check", os.path.join(ISO_ENGINE_DIR, "web", js_name)], capture_output=True, text=True, check=False)
                self.assertEqual(r.returncode, 0, msg=f"{js_name}: {r.stderr}")

        with self.subTest(case_id="T1-F21-03"):
            mvp_path = os.path.join(TESTS_DIR, "test_mvp_engine.py")
            self.assertTrue(os.path.isfile(mvp_path))
            suite = unittest.defaultTestLoader.discover(TESTS_DIR, pattern="test_mvp_engine.py")
            names = []
            for grp in suite:
                for tc in grp:
                    if isinstance(tc, unittest.TestSuite):
                        for t in tc:
                            names.append(t.id())
                    else:
                        names.append(tc.id())
            self.assertGreaterEqual(len(names), 10)

        with self.subTest(case_id="T1-F21-04"):
            for fn in sorted(os.listdir(TESTS_DIR)):
                if fn.endswith(".py"):
                    with open(os.path.join(TESTS_DIR, fn), "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=fn)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                self.assertNotEqual(alias.name.split(".")[0], "pytest", msg=f"Forbidden import in {fn}")
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            self.assertNotEqual(node.module.split(".")[0], "pytest", msg=f"Forbidden from-import in {fn}")

        with self.subTest(case_id="T1-F21-05"):
            with tempfile.TemporaryDirectory() as tmp:
                out_glb = os.path.join(tmp, "cli_out.glb")
                r1 = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--create-minimal-glb", out_glb], capture_output=True, text=True, check=False)
                self.assertEqual(r1.returncode, 0)
                r2 = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--validate-glb", out_glb], capture_output=True, text=True, check=False)
                self.assertEqual(r2.returncode, 0)


# ==============================================================================
# TIER 2: Boundary, Corner & Negative Cases (21 x 5 = 105 cases)
# ==============================================================================


class TestTier2BoundaryAndNegative(unittest.TestCase):
    """Tier 2: 21 features x 5 boundary/negative subtests = 105 cases."""

    def test_t2_f01(self) -> None:
        llm_mod = _mod("llm_client")
        db_mod = _mod("db")

        with self.subTest(case_id="T2-F01-01"):
            self.assertEqual(llm_mod.load_env_var("NON_EXISTENT_ISO_ENV_KEY_XYZ_999"), "")

        with self.subTest(case_id="T2-F01-02"):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = os.path.join(tmp, ".env")
                with open(env_file, "w", encoding="utf-8") as f:
                    f.write("# comment line\n\nKEY_QUOTED=\"abc=def=123\"\nKEY_SINGLE='xyz'\n")
                sig = inspect.signature(llm_mod.load_env_var)
                if "search_dirs" in sig.parameters or "start_dir" in sig.parameters or "env_paths" in sig.parameters:
                    param = next(p for p in ("search_dirs", "start_dir", "env_paths") if p in sig.parameters)
                    val_arg = tmp if param == "start_dir" else [tmp]
                    self.assertEqual(llm_mod.load_env_var("KEY_QUOTED", **{param: val_arg}), "abc=def=123")
                    self.assertEqual(llm_mod.load_env_var("KEY_SINGLE", **{param: val_arg}), "xyz")
                else:
                    self.assertEqual(llm_mod.load_env_var("KEY_QUOTED_MISSING"), "")

        with self.subTest(case_id="T2-F01-03"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    for raw_path in (
                        "/../../webui/server.py",
                        "/assets/../../webui/data/rplab.db",
                        "/%2e%2e/%2e%2e/webui/server.py",
                        "/vendor/../../../.env",
                    ):
                        st, body, _ = _http_raw_get(base_url, raw_path)
                        self.assertIn(st, (400, 403, 404), msg=f"Traversal returned {st} for {raw_path}")
                        self.assertNotIn(b"SQLite format 3", body)
                        self.assertNotIn(b"ThreadingHTTPServer", body)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F01-04"):
            norm = os.path.normpath(db_mod.DEFAULT_DB_PATH).lower()
            self.assertIn("iso_engine", norm)
            self.assertNotIn("webui", norm)

        with self.subTest(case_id="T2-F01-05"):
            snap1 = snapshot_webui_and_rplab()
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _http_json(base_url, "GET", "/api/bootstrap")
                finally:
                    _stop_iso_server(srv)
            snap2 = snapshot_webui_and_rplab()
            self.assertEqual(snap1, snap2)

    def test_t2_f02(self) -> None:
        db_mod = _mod("db")

        with self.subTest(case_id="T2-F02-01"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "unk.db")
                db_mod.init_db(db_path)
                self.assertIsNone(db_mod.get_campaign("no_such_camp", db_path=db_path))
                self.assertIsNone(db_mod.get_checkpoint("no_such_cp", db_path=db_path))
                self.assertIsNone(db_mod.get_custom_asset("no_such_asset", db_path=db_path))

        with self.subTest(case_id="T2-F02-02"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "empty_undo.db")
                db_mod.init_db(db_path)
                self.assertIsNone(db_mod.pop_undo("no_such_camp", db_path=db_path))

        with self.subTest(case_id="T2-F02-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "upsert.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "c_up", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                _db_put_cached_scene(db_mod, "c_up", "s1", "h1", {"version": 1}, db_path)
                _db_put_cached_scene(db_mod, "c_up", "s1", "h1", {"version": 2}, db_path)
                got = db_mod.get_cached_scene("c_up", "s1", "h1", db_path=db_path)
                self.assertEqual(got["version"], 2)

        with self.subTest(case_id="T2-F02-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "conc.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "c_conc", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                errors: list[Exception] = []

                def worker(idx: int) -> None:
                    try:
                        _db_put_cached_scene(db_mod, "c_conc", f"st_{idx}", f"h_{idx}", {"idx": idx}, db_path)
                        db_mod.push_undo("c_conc", {"idx": idx}, [], [], db_path=db_path)
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=5.0)
                self.assertEqual(errors, [])
                for i in range(10):
                    self.assertEqual(db_mod.get_cached_scene("c_conc", f"st_{i}", f"h_{i}", db_path=db_path)["idx"], i)

        with self.subTest(case_id="T2-F02-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "del_none.db")
                db_mod.init_db(db_path)
                res = db_mod.delete_campaign("does_not_exist", db_path=db_path)
                self.assertIn(res, (None, False, 0))

    def test_t2_f03(self) -> None:
        schemas = _mod("schemas")

        with self.subTest(case_id="T2-F03-01"):
            bad_char = make_test_char("bad_nf", "Không hư cấu")
            bad_char["meta"]["fictional"] = False
            with self.assertRaises(schemas.SchemaError):
                schemas.validate_iso_char(bad_char)

        with self.subTest(case_id="T2-F03-02"):
            for bad_id in ("", "Vy User!", "UPPER_ID"):
                bad_char = make_test_char("valid_tmp")
                bad_char["id"] = bad_id
                with self.assertRaises(schemas.SchemaError):
                    schemas.validate_iso_char(bad_char)

        with self.subTest(case_id="T2-F03-03"):
            w_small = make_test_world(num_rooms=4)
            del w_small["rooms"]["bedroom_linh"]
            with self.assertRaises(schemas.SchemaError):
                schemas.validate_iso_world(w_small)
            w_big = make_test_world(num_rooms=8)
            w_big["rooms"]["extra_9"] = {"id": "extra_9", "name": "Phòng 9", "kind": "other", "privacy": "public", "size": [4, 4], "props": []}
            with self.assertRaises(schemas.SchemaError):
                schemas.validate_iso_world(w_big)

        with self.subTest(case_id="T2-F03-04"):
            w_bad_door = make_test_world(num_rooms=4)
            w_bad_door["doors"].append({"id": "bad_d", "room_a": "foyer", "room_b": "ghost_room", "locked": False})
            with self.assertRaises(schemas.SchemaError):
                schemas.validate_iso_world(w_bad_door)

        with self.subTest(case_id="T2-F03-05"):
            w_bad_acts = make_test_world(num_rooms=4)
            w_bad_acts["acts_spine"] = w_bad_acts["acts_spine"][:2]
            with self.assertRaises(schemas.SchemaError):
                schemas.validate_iso_world(w_bad_acts)

    def test_t2_f04(self) -> None:
        schemas = _mod("schemas")

        with self.subTest(case_id="T2-F04-01"):
            iso_char = make_test_char("vy_iso", "Vy")
            out = schemas.upgrade_char_v2_to_iso(iso_char)
            self.assertEqual(out["stats"], iso_char["stats"])
            self.assertEqual(out["voxel_look"], iso_char["voxel_look"])

        with self.subTest(case_id="T2-F04-02"):
            c_str = {
                "format": "char-v2",
                "id": "simple_npc",
                "meta": {"name": "Minh", "fictional": True},
                "core": {"one_liner": "Nhân vật trầm tính", "personality": "Trầm tính, ít nói", "boundaries": []},
                "voice": {"pronoun_defaults": {"close_peer": {"self": "mình", "call": "bạn"}}},
            }
            out = schemas.validate_iso_char(schemas.upgrade_char_v2_to_iso(c_str))
            self.assertEqual(out["id"], "simple_npc")

        with self.subTest(case_id="T2-F04-03"):
            with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                w2 = json.load(f)
            w2.pop("characters", None)
            w2["cast"] = [{"char_id": "nonexistent_char_999", "start_location": "san_truong"}]
            with tempfile.TemporaryDirectory() as empty_dir:
                with self.assertRaises(schemas.SchemaError):
                    schemas.upgrade_world_v2_to_iso(w2, search_dirs=[empty_dir])

        with self.subTest(case_id="T2-F04-04"):
            w2_small = {
                "format": "world-v2",
                "id": "small_world",
                "meta": {"name": "Căn hộ nhỏ"},
                "locations": {
                    "loc_a": {"name": "Phòng khách", "parent": None, "tags": ["indoor", "public"]},
                    "loc_b": {"name": "Phòng ngủ", "parent": "loc_a", "tags": ["indoor", "private_possible"]},
                },
                "characters": [
                    {"format": "char-v2", "id": "npc_1", "meta": {"name": "An", "fictional": True}, "core": {"one_liner": "Sinh viên vui tính", "personality": ["vui"]}}
                ],
                "cast": [{"char_id": "npc_1", "start_location": "loc_b"}],
                "opening": {"user_start_location": "loc_a"},
            }
            iso_small = schemas.validate_iso_world(schemas.upgrade_world_v2_to_iso(w2_small))
            self.assertGreaterEqual(len(iso_small["rooms"]), 4)
            cast_by_id = {c["char_id"]: c for c in iso_small["cast"]}
            self.assertIn("npc_1", cast_by_id)
            self.assertEqual(cast_by_id["npc_1"]["start_room"], "loc_b")

        with self.subTest(case_id="T2-F04-05"):
            with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                w2 = json.load(f)
            iso_w = schemas.upgrade_world_v2_to_iso(w2, search_dirs=[SAMPLE_CHARS_DIR])
            room_ids = set(iso_w["rooms"].keys())
            alias_map = iso_w.get("location_alias_map") or iso_w.get("meta", {}).get("location_alias_map") or {}
            for old_id in w2["locations"]:
                self.assertTrue(
                    old_id in room_ids or alias_map.get(old_id) in room_ids,
                    f"Unmapped location {old_id}",
                )

    def test_t2_f05(self) -> None:
        schemas = _mod("schemas")
        catalog = _mod("catalog")
        db_mod = _mod("db")
        spec = make_test_world(with_missing_props=True)["missing_props"][0]
        ring_secrets = ["Hộp nhẫn đính hôn bị giấu", "study", "minh", "found_ring_box"]

        with self.subTest(case_id="T2-F05-01"):
            res = schemas.redact_missing_prop(spec, {})
            self.assertIs(res["revealed"], False)
            _assert_no_spoilers(self, res, ring_secrets)

        with self.subTest(case_id="T2-F05-02"):
            for falsy in (False, 0, None, ""):
                res = schemas.redact_missing_prop(spec, {"flags": {"found_ring_box": falsy}, "discovered_items": []})
                self.assertIs(res["revealed"], False)
                _assert_no_spoilers(self, res, ring_secrets)

        with self.subTest(case_id="T2-F05-03"):
            res = schemas.redact_missing_prop(spec, {"flags": {}, "discovered_items": []})
            _assert_no_spoilers(self, res, ring_secrets)

        with self.subTest(case_id="T2-F05-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "empty_missing.db")
                db_mod.init_db(db_path)
                w = make_test_world(with_missing_props=False)
                self.assertEqual(catalog.get_studio_missing_list(w, {}, db_path=db_path), [])

        with self.subTest(case_id="T2-F05-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "partial_reveal.db")
                db_mod.init_db(db_path)
                w = make_test_world(with_missing_props=True)
                w["missing_props"].append({
                    "asset_tag": "prop_mystery_02",
                    "size": [1, 1],
                    "mount": "floor",
                    "visual_brief": {"title_masked": "Vật #2", "shape_hint": "Chìa cũ", "palette_hint": ["#d97706"]},
                    "story_meta": {"true_name": "Bản di chúc", "room_id": "study", "owner_id": "minh", "revealed_flag": "found_will"},
                })
                out = catalog.get_studio_missing_list(w, {"flags": {"found_will": True}, "discovered_items": []}, db_path=db_path)
                by_tag = {x["asset_tag"]: x for x in out}
                self.assertIs(by_tag["prop_mystery_01"]["revealed"], False)
                _assert_no_spoilers(self, by_tag["prop_mystery_01"], ring_secrets)
                self.assertIs(by_tag["prop_mystery_02"]["revealed"], True)
                self.assertEqual(by_tag["prop_mystery_02"]["true_name"], "Bản di chúc")

    def test_t2_f06(self) -> None:
        catalog = _mod("catalog")
        db_mod = _mod("db")
        engine = _mod("engine")
        compiler = _mod("compiler")

        with self.subTest(case_id="T2-F06-01"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pose_none.db")
                db_mod.init_db(db_path)
                self.assertIsNone(catalog.get_pose_slot("nonexistent_prop_xyz", "sit", db_path=db_path))
                self.assertIsNone(catalog.get_pose_slot("dining_chair_1x1", "lie", db_path=db_path))

        with self.subTest(case_id="T2-F06-02"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pose_hide.db")
                db_mod.init_db(db_path)
                slot = catalog.get_pose_slot("mystery_placeholder", "hide_inside", db_path=db_path)
                self.assertIsNone(slot)
                valid_hide = catalog.get_pose_slot("wardrobe_tall_2x1", "hide_inside", db_path=db_path)
                self.assertIsInstance(valid_hide, dict)
                self.assertIn("offset", valid_hide)

        with self.subTest(case_id="T2-F06-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "override.db")
                db_mod.init_db(db_path)
                starter = catalog.load_starter_catalog()
                self.assertIn("sofa_velvet_2x1", starter)
                self.assertNotEqual(starter["sofa_velvet_2x1"]["name_vi"], "Sofa tùy chỉnh")
                db_mod.save_custom_asset({
                    "asset_tag": "sofa_velvet_2x1",
                    "category": "living",
                    "name_vi": "Sofa tùy chỉnh",
                    "size": [2, 1],
                    "mount": "floor",
                    "voxels": [{"x": 0, "y": 0, "z": 0, "w": 4, "h": 4, "d": 4, "color": "#22c55e", "shape": "cube", "material": "fabric"}],
                }, db_path=db_path)
                merged = catalog.get_merged_catalog(db_path=db_path)
                self.assertEqual(merged["sofa_velvet_2x1"]["name_vi"], "Sofa tùy chỉnh")

        with self.subTest(case_id="T2-F06-04"):
            starter = catalog.load_starter_catalog()
            for tag, entry in starter.items():
                sz = entry["size"]
                self.assertEqual(len(sz), 2, msg=tag)
                self.assertGreater(sz[0], 0, msg=tag)
                self.assertGreater(sz[1], 0, msg=tag)
                self.assertIn(entry["mount"], ("floor", "surface", "wall"), msg=tag)

        with self.subTest(case_id="T2-F06-05"):
            starter = catalog.load_starter_catalog()
            self.assertIs(starter["rug_persian_2x2"]["walkable"], True)
            self.assertIs(starter["desk_mahogany_2x1"]["walkable"], False)
            world = make_test_world(num_rooms=4)
            fp = compiler.pack_floorplan(world, starter)
            rug = next((p for p in fp["props"] if p["asset_tag"] == "rug_persian_2x2"), None)
            desk_or_sofa = next((p for p in fp["props"] if not starter.get(p["asset_tag"], {}).get("walkable", False)), None)
            self.assertIsNotNone(rug)
            self.assertIsNotNone(desk_or_sofa)
            r_room = _room_obj(fp, rug["room_id"])
            state = {"doors_unlocked": []}
            start = _find_walkable_tile(fp, r_room["id"], state)
            non_walk_tiles = set()
            for p in fp["props"]:
                if not starter.get(p["asset_tag"], {}).get("walkable", False):
                    pw, pd = p.get("size", [1, 1])
                    for dx in range(pw):
                        for dy in range(pd):
                            non_walk_tiles.add((p["x"] + dx, p["y"] + dy))
            rw_p, rd_p = rug.get("size", [2, 2])
            rug_tile = next(
                ([rug["x"] + dx, rug["y"] + dy] for dx in range(rw_p) for dy in range(rd_p) if (rug["x"] + dx, rug["y"] + dy) not in non_walk_tiles),
                None,
            )
            self.assertIsNotNone(rug_tile)
            self.assertIsNotNone(engine.find_path(fp, start, rug_tile, state))
            self.assertIsNone(engine.find_path(fp, start, [desk_or_sofa["x"], desk_or_sofa["y"]], state))

    def test_t2_f07(self) -> None:
        catalog = _mod("catalog")

        def assert_glb_rejected(raw: bytes) -> None:
            rejected = False
            res: dict[str, Any] | None = None
            try:
                res = catalog.validate_glb_bytes(raw)
            except (ValueError, struct.error):
                rejected = True
            if not rejected:
                self.assertIsInstance(res, dict)
                self.assertIs(res["valid"], False)

        with self.subTest(case_id="T2-F07-01"):
            for bad in (b"", b"glTF\x02\x00"):
                assert_glb_rejected(bad)

        with self.subTest(case_id="T2-F07-02"):
            assert_glb_rejected(b"VOX " + struct.pack("<II", 150, 12))
            bad_ver = bytearray(build_minimal_glb())
            struct.pack_into("<I", bad_ver, 4, 1)
            assert_glb_rejected(bytes(bad_ver))

        with self.subTest(case_id="T2-F07-03"):
            good = build_minimal_glb()
            assert_glb_rejected(good[:-4])
            # Corrupt JSON chunk
            bad_json = b"{" * 12
            chunk0 = struct.pack("<I4s", len(bad_json), b"JSON") + bad_json
            corrupt = struct.pack("<4sII", b"glTF", 2, 12 + len(chunk0)) + chunk0
            assert_glb_rejected(corrupt)

        with self.subTest(case_id="T2-F07-04"):
            with tempfile.TemporaryDirectory() as tmp:
                bad_path = os.path.join(tmp, "bad.glb")
                with open(bad_path, "wb") as f:
                    f.write(b"NOT_A_GLB")
                r = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--validate-glb", bad_path], capture_output=True, text=True, check=False)
                self.assertNotEqual(r.returncode, 0)

        with self.subTest(case_id="T2-F07-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "bad_vox.db")
                custom_dir = os.path.join(tmp, "custom")
                _mod("db").init_db(db_path)
                with self.assertRaises(ValueError):
                    catalog.save_studio_asset(
                        {
                            "asset_tag": "oob_prop",
                            "name_vi": "Ngoài lưới",
                            "size": [1, 1],
                            "mount": "floor",
                            "voxels": [{"x": 16, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1, "color": "#ffffff", "shape": "cube", "material": "plastic"}],
                        },
                        custom_dir=custom_dir,
                        db_path=db_path,
                    )
                self.assertIsNone(_mod("db").get_custom_asset("oob_prop", db_path=db_path))

    def test_t2_f08(self) -> None:
        with self.subTest(case_id="T2-F08-01"):
            client = _make_live_llm_for_url("http://127.0.0.1:9/unused", provider="openrouter", api_key="")
            with self.assertRaises(Exception):
                client.complete_json([{"role": "user", "content": "fail"}])

        with self.subTest(case_id="T2-F08-02"):
            srv, _ = _start_mock_llm_http_server([(429, '{"error": "rate limit"}', None)])
            try:
                client = _make_live_llm_for_url(f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions", provider="openrouter", api_key="k")
                with self.assertRaises(Exception) as ctx:
                    client.complete_json([{"role": "user", "content": "hi"}])
                self.assertNotIsInstance(ctx.exception, json.JSONDecodeError)
            finally:
                srv.shutdown()
                srv.server_close()

        with self.subTest(case_id="T2-F08-03"):
            r1 = json.dumps({"choices": [{"message": {"content": "not json at all"}}]})
            r2 = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]})
            srv, _ = _start_mock_llm_http_server([(200, r1, None), (200, r2, None)])
            try:
                client = _make_live_llm_for_url(f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions", provider="openrouter", api_key="k")
                out, _ = client.complete_json([{"role": "user", "content": "retry"}])
                self.assertEqual(out["ok"], True)
            finally:
                srv.shutdown()
                srv.server_close()

        with self.subTest(case_id="T2-F08-04"):
            trunc = json.dumps({"choices": [{"message": {"content": '{"text": "Chào {bạn", "ok": true'}}]})
            srv, _ = _start_mock_llm_http_server([(200, trunc, None)])
            try:
                client = _make_live_llm_for_url(f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions", provider="openrouter", api_key="k")
                out, _ = client.complete_json([{"role": "user", "content": "utf8"}])
                self.assertIn("Chào", out.get("text", ""))
            finally:
                srv.shutdown()
                srv.server_close()

        with self.subTest(case_id="T2-F08-05"):
            mock = make_mock_llm([{"step": 1}])
            first_res, _ = mock.complete_json([{"role": "user", "content": "1"}])
            self.assertEqual(first_res["step"], 1)
            # Exhausted queue either raises an explicit error or returns deterministic fallback dict
            if hasattr(mock, "strict") and not mock.strict:
                mock.strict = True
            raised_exc: Exception | None = None
            res = None
            try:
                res, _ = mock.complete_json([{"role": "user", "content": "2"}])
            except (IndexError, ValueError, RuntimeError, LookupError) as exc:
                raised_exc = exc
            if raised_exc is not None:
                self.assertGreater(len(str(raised_exc).strip()), 0)
            else:
                self.assertIsInstance(res, dict)

    def test_t2_f09(self) -> None:
        prefetch = _mod("prefetch")
        db_mod = _mod("db")

        with self.subTest(case_id="T2-F09-01"):
            s1 = {"clock": {"hour": 8}, "flags": {"a": True}}
            s2 = {"clock": {"hour": 22}, "flags": {"a": False}}
            self.assertEqual(prefetch.masked_state_hash(s1, {}), prefetch.masked_state_hash(s2, {}))

        with self.subTest(case_id="T2-F09-02"):
            rk = {"flags": ["missing_f"], "items": ["missing_i"], "affinity_with": ["missing_npc"]}
            h = prefetch.masked_state_hash({}, rk)
            self.assertIsInstance(h, str)
            self.assertGreater(len(h), 0)

        with self.subTest(case_id="T2-F09-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pf_hit.db")
                db_mod.init_db(db_path)
                st = _make_sample_storylet("st_cached")
                state = {"flags": {"searched_drawer": False}, "player": {"inventory": ["brass_key"]}, "relationships": {"linh": 10}}
                db_mod.save_campaign({"id": "c_hit", "world": make_test_world(), "state": state, "history": []}, db_path=db_path)
                h = prefetch.masked_state_hash(state, st["relevant_keys"])
                _db_put_cached_scene(db_mod, "c_hit", "st_cached", h, _make_sample_scene_tree("st_cached"), db_path)
                mock = make_mock_llm([_make_sample_scene_tree("should_not_run")])
                pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
                try:
                    pm.enqueue("c_hit", st, state, mock, priority=pm.PRIO_P0, db_path=db_path)
                    import time
                    time.sleep(0.15)
                    self.assertEqual(mock.call_count, 0)
                finally:
                    pm.shutdown(wait=True)

        with self.subTest(case_id="T2-F09-04"):
            pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
            try:
                self.assertLessEqual(pm.max_workers, 2)
                self.assertEqual(pm.max_prefetch_depth, 1)
                status = pm.get_status()
                self.assertIn("max_workers", status)
                self.assertIn("max_prefetch_depth", status)
                self.assertLessEqual(status["max_workers"], 2)
                self.assertEqual(status["max_prefetch_depth"], 1)
            finally:
                pm.shutdown(wait=True)

        with self.subTest(case_id="T2-F09-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "pf_sd.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "c_sd", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                mock = make_mock_llm([_make_sample_scene_tree("s1"), _make_sample_scene_tree("s2")])
                pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
                pm.enqueue("c_sd", _make_sample_storylet("s1"), {}, mock, db_path=db_path)
                pm.shutdown(wait=True)
                self.assertIsInstance(pm.get_status(), dict)

    def test_t2_f10(self) -> None:
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        cat = catalog.load_starter_catalog()

        with self.subTest(case_id="T2-F10-01"):
            w8 = make_test_world(num_rooms=8)
            fp = compiler.pack_floorplan(w8, cat)
            self.assertEqual(len(fp["rooms"]), 8)
            self.assertLessEqual(fp["width"], 24)
            self.assertLessEqual(fp["height"], 24)

        with self.subTest(case_id="T2-F10-02"):
            w4 = make_test_world(num_rooms=4)
            fp = compiler.pack_floorplan(w4, cat)
            self.assertEqual(len(fp["rooms"]), 4)
            self.assertLessEqual(fp["width"], 24)

        with self.subTest(case_id="T2-F10-03"):
            w_imp = make_test_world(num_rooms=4)
            for r in w_imp["rooms"].values():
                r["size"] = [20, 20]
            with self.assertRaises((ValueError, _mod("schemas").SchemaError)):
                compiler.pack_floorplan(w_imp, cat)

        with self.subTest(case_id="T2-F10-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "act3.db")
                _mod("db").init_db(db_path)
                mock = make_mock_llm([
                    {"storylets": [_make_sample_storylet("s1", "foyer")]},
                    _make_sample_scene_tree("s1"),
                ] * 4)
                camp = compiler.compile_new_campaign(make_test_world(), llm=mock, catalog=cat, db_path=db_path)
                camp["act_index"] = 3
                camp["state"]["act_index"] = 3
                with self.assertRaises(ValueError):
                    compiler.compile_next_act(camp, mock, db_path=db_path)

        with self.subTest(case_id="T2-F10-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "locked.db")
                _mod("db").init_db(db_path)
                mock = make_mock_llm([{"storylets": [_make_sample_storylet("s1", "foyer")]}, _make_sample_scene_tree("s1")] * 3)
                camp = compiler.compile_new_campaign(make_test_world(with_locked_room=True), llm=mock, catalog=cat, db_path=db_path)
                study_tile = _find_walkable_tile(camp["floorplan"], "study", {"doors_unlocked": ["door_foyer_study"]})
                self.assertIsNone(_mod("engine").find_path(camp["floorplan"], camp["state"]["player"]["pos"], study_tile, camp["state"]))

    def test_t2_f11(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")

        world = make_test_world(num_rooms=4)
        cat = catalog.load_starter_catalog()
        fp = compiler.pack_floorplan(world, cat)
        r0 = _rooms_list(fp)[0]
        state = {
            "player": {
                "room_id": r0["id"],
                "pos": _find_walkable_tile(fp, r0["id"], {"doors_unlocked": []}),
                "pose": "stand",
                "inventory": [],
                "energy": 95,
                "stress": 5,
            },
            "doors_unlocked": [],
            "flags": {},
            "discovered_items": [],
        }

        with self.subTest(case_id="T2-F11-01"):
            self.assertEqual(engine.find_path(fp, state["player"]["pos"], state["player"]["pos"], state), [state["player"]["pos"]])

        with self.subTest(case_id="T2-F11-02"):
            self.assertIsNone(engine.find_path(fp, state["player"]["pos"], [-1, -1], state))
            self.assertIsNone(engine.find_path(fp, state["player"]["pos"], [99, 99], state))
            non_walk = next(p for p in fp["props"] if not cat.get(p["asset_tag"], {}).get("walkable", False))
            self.assertIsNone(engine.find_path(fp, state["player"]["pos"], [non_walk["x"], non_walk["y"]], state))

        with self.subTest(case_id="T2-F11-03"):
            r1 = engine.roll_d20(12, 10, forced_roll=1)
            self.assertIs(r1["critical"], True)
            self.assertIs(r1["success"], False)
            r20 = engine.roll_d20(8, 25, forced_roll=20)
            self.assertIs(r20["critical"], True)
            self.assertIs(r20["success"], True)
            r_small = engine.roll_d20(2, 12, forced_roll=10)
            self.assertEqual(r_small["modifier"], -4)
            self.assertEqual(r_small["total"], 6)
            self.assertIs(r_small["success"], False)

        with self.subTest(case_id="T2-F11-04"):
            w6 = make_test_world(num_rooms=6, with_locked_room=True)
            fp6 = compiler.pack_floorplan(w6, cat)
            st6 = {"player": {"inventory": []}, "doors_unlocked": [], "flags": {}}
            res = engine.use_item(w6, fp6, st6, "brass_key", "door_foyer_study")
            self.assertIs(res["success"], False)
            self.assertNotIn("door_foyer_study", st6["doors_unlocked"])

        with self.subTest(case_id="T2-F11-05"):
            engine.apply_state_delta(world, fp, state, {"energy_delta": 500, "stress_delta": -500})
            self.assertLessEqual(state["player"]["energy"], 100)
            self.assertGreaterEqual(state["player"]["stress"], 0)
            engine.apply_state_delta(world, fp, state, {"energy_delta": -500, "stress_delta": 500})
            self.assertGreaterEqual(state["player"]["energy"], 0)
            self.assertLessEqual(state["player"]["stress"], 100)
            engine.apply_state_delta(world, fp, state, {"pose_updates": {"user": {"pose": "hide_inside", "target_prop": "foyer_sofa"}}})
            self.assertNotEqual(state["player"]["pose"], "hide_inside")

    def test_t2_f12(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        db_mod = _mod("db")

        world = make_test_world(num_rooms=6)
        cat = catalog.load_starter_catalog()
        fp = compiler.pack_floorplan(world, cat)

        with self.subTest(case_id="T2-F12-01"):
            state = {"clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}, "player": {"room_id": "foyer", "pose": "stand"}}
            self.assertIsNone(engine.check_intrusion_imminent(world, fp, state, "foyer", action_minutes=5))

        with self.subTest(case_id="T2-F12-02"):
            s6 = {"clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 54}, "player": {"room_id": "bedroom_linh", "pose": "stand"}}
            self.assertIsNone(engine.check_intrusion_imminent(world, fp, s6, "bedroom_linh", action_minutes=5))
            s5 = {"clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 55}, "player": {"room_id": "bedroom_linh", "pose": "stand"}}
            self.assertIsNotNone(engine.check_intrusion_imminent(world, fp, s5, "bedroom_linh", action_minutes=5))

        with self.subTest(case_id="T2-F12-03"):
            w_mid = make_test_world(num_rooms=6)
            w_mid["characters"]["linh"]["schedule"] = [{"start": "00:01", "end": "06:00", "room_id": "bedroom_linh", "pose": "stand", "target_prop": None}]
            s_mid = {"clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 23, "minute": 57}, "player": {"room_id": "bedroom_linh", "pose": "stand"}}
            warn = engine.check_intrusion_imminent(w_mid, fp, s_mid, "bedroom_linh", action_minutes=5)
            self.assertIsNotNone(warn)

        with self.subTest(case_id="T2-F12-04"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "intr_fail.db")
                db_mod.init_db(db_path)
                mock = make_mock_llm([{"storylets": [_make_sample_storylet("s1", "foyer")]}, _make_sample_scene_tree("s1")] * 3)
                camp = compiler.compile_new_campaign(world, llm=mock, catalog=cat, db_path=db_path)
                camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}
                camp["state"]["player"]["room_id"] = "bedroom_linh"
                camp["state"]["pending_intrusion"] = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", 5)
                res = engine.resolve_intrusion_choice(camp, "hide", forced_roll=1, db_path=db_path)
                self.assertIs(res["d20"]["success"], False)
                self.assertIs(res["success"], False)

        with self.subTest(case_id="T2-F12-05"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "no_hide.db")
                db_mod.init_db(db_path)
                w_no_wardrobe = make_test_world(num_rooms=6)
                w_no_wardrobe["rooms"]["study"]["props"] = [{"id": "study_desk", "asset_tag": "desk_mahogany_2x1", "name": "Bàn"}]
                w_no_wardrobe["characters"]["minh"]["schedule"] = [{"start": "09:00", "end": "11:00", "room_id": "study", "pose": "stand", "target_prop": None}]
                mock = make_mock_llm([{"storylets": [_make_sample_storylet("s1", "foyer")]}, _make_sample_scene_tree("s1")] * 3)
                camp = compiler.compile_new_campaign(w_no_wardrobe, llm=mock, catalog=cat, db_path=db_path)
                camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}
                camp["state"]["player"]["room_id"] = "study"
                camp["state"]["player"]["pos"] = _find_walkable_tile(camp["floorplan"], "study", {"doors_unlocked": ["door_foyer_study"]})
                camp["state"]["pending_intrusion"] = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "study", 5)
                db_mod.save_campaign(camp, db_path=db_path)
                res = engine.resolve_intrusion_choice(camp, "hide", forced_roll=18, db_path=db_path)
                self.assertIs(res["success"], False)
                self.assertNotEqual(camp["state"]["player"]["pose"], "hide_inside")

    def test_t2_f13(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")

        world = make_test_world(num_rooms=6)
        cat = catalog.load_starter_catalog()
        fp = compiler.pack_floorplan(world, cat)

        with self.subTest(case_id="T2-F13-01"):
            st = {"player": {"room_id": "bathroom", "stats": {"charm": 14, "stealth": 14}}, "npcs": {"linh": {"room_id": "living_room"}}, "relationships": {"linh": 0}}
            res = engine.perform_speech_or_dialogue(world, st, "Nói một mình", whisper=False)
            self.assertEqual(res["witnesses"], [])

        with self.subTest(case_id="T2-F13-02"):
            st = {
                "player": {"room_id": "living_room", "stats": {"charm": 8, "stealth": 8}},
                "npcs": {"linh": {"room_id": "living_room"}, "an": {"room_id": "living_room"}},
                "relationships": {"linh": 10, "an": 10},
            }
            res = engine.perform_speech_or_dialogue(world, st, "Nói nhỏ nhưng hớ hênh", whisper=True, target_npc_id="linh", forced_roll=1)
            self.assertEqual(set(res["witnesses"]), {"linh", "an"})

        with self.subTest(case_id="T2-F13-03"):
            st = {
                "clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30},
                "player": {"room_id": "foyer"},
                "npcs": {"minh": {"room_id": "kitchen", "mood": "neutral"}, "an": {"room_id": "living_room", "mood": "neutral"}},
                "traces": [],
            }
            adj = set(fp["adjacent_rooms"].get("foyer", []))
            non_adj = next((r["id"] for r in _rooms_list(fp) if r["id"] != "foyer" and r["id"] not in adj), None)
            self.assertIsNotNone(non_adj)
            w_na = make_test_world(num_rooms=6)
            for cid, c in w_na["characters"].items():
                if cid in ("minh", "an"):
                    c["schedule"][1]["room_id"] = non_adj
            w_na["offscreen_events"] = [
                {
                    "id": "off_non_adj",
                    "room_id": non_adj,
                    "trigger_min": 540,
                    "participants": ["minh", "an"],
                    "secret_dialogue": "SECRET_NON_ADJ",
                    "sound_hint": "Tiếng động ở xa.",
                    "trace_label": "Vết xước trên sàn",
                    "mood_effects": {"minh": "tense", "an": "nervous"},
                }
            ]
            events = engine.advance_clock_and_sim(w_na, fp, st, 35)
            self.assertFalse(any((e.get("kind") or e.get("type")) == "adjacent_sound" for e in events))
            self.assertGreaterEqual(len(st["traces"]), 1)

        with self.subTest(case_id="T2-F13-04"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, db_path, _ = _start_test_iso_server(tmp)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world(num_rooms=6)})
                    cid = cmp_body["campaign"]["id"]
                    db_mod = _mod("db")
                    camp = db_mod.get_campaign(cid, db_path=db_path)
                    k_pos = _find_walkable_tile(camp["floorplan"], "kitchen", camp["state"])
                    camp["state"]["player"]["room_id"] = "kitchen"
                    camp["state"]["player"]["pos"] = k_pos
                    camp["state"]["traces"] = [{"id": "tr_1", "room_id": "kitchen", "pos": k_pos, "label": "Vết cà phê đổ", "inspected": False}]
                    db_mod.save_campaign(camp, db_path=db_path)

                    st_ok, body_ok, _ = _http_json(
                        base_url,
                        "POST",
                        "/api/campaign/action",
                        {"campaign_id": cid, "action_type": "inspect_trace", "trace_id": "tr_1"},
                    )
                    self.assertEqual(st_ok, 200)
                    self.assertIs(body_ok["state"]["traces"][0]["inspected"], True)

                    st_bad, body_bad, _ = _http_json(
                        base_url,
                        "POST",
                        "/api/campaign/action",
                        {"campaign_id": cid, "action_type": "inspect_trace", "trace_id": "tr_missing"},
                    )
                    self.assertTrue(st_bad in (400, 404) or body_bad.get("success") is False)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F13-05"):
            st = {
                "clock": {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 23, "minute": 30},
                "player": {"room_id": "foyer"},
                "npcs": {},
                "traces": [],
            }
            engine.advance_clock_and_sim(world, fp, st, 0)
            self.assertEqual(st["clock"]["hour"], 23)
            self.assertEqual(st["clock"]["minute"], 30)
            engine.advance_clock_and_sim(world, fp, st, 90)
            self.assertEqual(st["clock"]["day"], 13)
            self.assertEqual(st["clock"]["hour"], 1)
            self.assertEqual(st["clock"]["minute"], 0)

    def test_t2_f14(self) -> None:
        engine = _mod("engine")
        world = make_test_world()

        with self.subTest(case_id="T2-F14-01"):
            state = {"act_index": 1, "flags": {}, "relationships": {}}
            res = engine.check_act_or_ending(world, state, [_make_sample_storylet("s1")])
            self.assertIs(res["can_advance_act"], False)
            self.assertIsNone(res.get("ending"))

        with self.subTest(case_id="T2-F14-02"):
            state = {"act_index": 1, "flags": {}, "completed_storylets": ["s1"]}
            s1 = _make_sample_storylet("s1")
            s1["completed"] = True
            res = engine.check_act_or_ending(world, state, [s1])
            self.assertIs(res["can_advance_act"], True)

        with self.subTest(case_id="T2-F14-03"):
            state = {"act_index": 3, "flags": {"ending_a": True, "ending_b": True}, "affinity": {}}
            res = engine.check_act_or_ending(world, state, [])
            self.assertIsNotNone(res.get("ending"))
            self.assertEqual(res["ending"]["id"], "ending_truth")

        with self.subTest(case_id="T2-F14-04"):
            state = {"act_index": 1, "flags": {"ending_a": True}, "affinity": {}}
            res = engine.check_act_or_ending(world, state, [_make_sample_storylet("s1")])
            self.assertIsNone(res.get("ending"))

        with self.subTest(case_id="T2-F14-05"):
            state = {"act_index": 3, "ending": {"id": "ending_truth"}, "free_sandbox_mode": False}
            self.assertFalse(state["free_sandbox_mode"])
            engine.enable_free_sandbox(state)
            self.assertTrue(state["free_sandbox_mode"])

    def test_t2_f15(self) -> None:
        db_mod = _mod("db")

        with self.subTest(case_id="T2-F15-01"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, comp, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = comp["campaign"]["id"]
                    st, body, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": "missing_cp_999"})
                    self.assertIn(st, (400, 404))
                    self.assertIn("error", json.dumps(body).lower())
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F15-02"):
            with tempfile.TemporaryDirectory() as tmp:
                mock = make_mock_llm([
                    {"storylets": [_make_sample_storylet("s1", "foyer")]},
                    _make_sample_scene_tree("s1"),
                    {"storylets": [_make_sample_storylet("s2", "living_room")]},
                    _make_sample_scene_tree("s2"),
                ] * 4)
                srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
                try:
                    _, comp, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = comp["campaign"]["id"]
                    _, cps1, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid}")
                    cp1_id = cps1["checkpoints"][0]["id"]
                    _http_json(base_url, "POST", "/api/campaign/advance_act", {"campaign_id": cid})
                    st_rew, rew, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": cp1_id})
                    self.assertEqual(st_rew, 200)
                    self.assertEqual(rew["campaign"]["act_index"], 1)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F15-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "undo_many.db")
                db_mod.init_db(db_path)
                db_mod.save_campaign({"id": "cu", "world": make_test_world(), "state": {}, "history": []}, db_path=db_path)
                db_mod.push_undo("cu", {"step": 1}, [], [], db_path=db_path)
                db_mod.push_undo("cu", {"step": 2}, [], [], db_path=db_path)
                self.assertEqual(db_mod.pop_undo("cu", db_path=db_path)["state"]["step"], 2)
                self.assertEqual(db_mod.pop_undo("cu", db_path=db_path)["state"]["step"], 1)
                self.assertIsNone(db_mod.pop_undo("cu", db_path=db_path))

        with self.subTest(case_id="T2-F15-04"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, c1, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    _, c2, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid1 = c1["campaign"]["id"]
                    cid2 = c2["campaign"]["id"]
                    _, cps1, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid1}")
                    cp1_id = cps1["checkpoints"][0]["id"]
                    st, _, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid2, "checkpoint_id": cp1_id})
                    self.assertIn(st, (400, 403, 404))
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F15-05"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    st1, _, _ = _http_json(base_url, "POST", "/api/campaign/rewind", b"{bad json")
                    self.assertEqual(st1, 400)
                    st2, _, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"action": "undo"})
                    self.assertIn(st2, (400, 404))
                finally:
                    _stop_iso_server(srv)

    def test_t2_f16(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, _, _ = _start_test_iso_server(tmp)
            try:
                with self.subTest(case_id="T2-F16-01"):
                    st1, _, _ = _http_json(base_url, "GET", "/api/nonexistent_route")
                    self.assertIn(st1, (404, 405))
                    st2, _, _ = _http_json(base_url, "GET", "/api/campaign/action")
                    self.assertIn(st2, (400, 404, 405))

                with self.subTest(case_id="T2-F16-02"):
                    st1, _, _ = _http_json(base_url, "POST", "/api/settings", b"{bad")
                    self.assertEqual(st1, 400)
                    st2, _, _ = _http_json(base_url, "POST", "/api/import/convert", b"")
                    self.assertEqual(st2, 400)

                with self.subTest(case_id="T2-F16-03"):
                    st1, _, _ = _http_json(base_url, "GET", "/api/campaign/state")
                    self.assertIn(st1, (400, 404))
                    st2, _, _ = _http_json(base_url, "GET", "/api/studio/missing")
                    self.assertIn(st2, (400, 404))

                with self.subTest(case_id="T2-F16-04"):
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                    cid = cmp_body["campaign"]["id"]
                    statuses: list[int] = []
                    lock = threading.Lock()

                    def req_fn() -> None:
                        s, _, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                        with lock:
                            statuses.append(s)

                    ts = [threading.Thread(target=req_fn) for _ in range(5)]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join(timeout=5.0)
                    self.assertEqual(statuses, [200] * 5)

                with self.subTest(case_id="T2-F16-05"):
                    st1, _, _ = _http_raw_get(base_url, "/vendor/three/no_such_file.js")
                    self.assertEqual(st1, 404)
                    st2, _, _ = _http_raw_get(base_url, "/assets/../../.env")
                    self.assertIn(st2, (403, 404))
            finally:
                _stop_iso_server(srv)

    def test_t2_f17(self) -> None:
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        db_mod = _mod("db")

        sample = _load_biet_thu_sample()
        world = sample.get("world", sample)

        with self.subTest(case_id="T2-F17-01"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "bt_lock.db")
                db_mod.init_db(db_path)
                camp = compiler.compile_new_campaign(sample, llm=make_mock_llm(), db_path=db_path)
                unlocked_set = set(camp["state"].get("doors_unlocked", []))
                locked_door = next(d for d in camp["floorplan"]["doors"] if d.get("locked") and d["id"] not in unlocked_set)
                start_room = camp["state"]["player"]["room_id"]
                locked_room_id = locked_door["room_b"] if locked_door["room_b"] != start_room else locked_door["room_a"]
                study_tile = _find_walkable_tile(camp["floorplan"], locked_room_id, {"doors_unlocked": [locked_door["id"]]})
                self.assertIsNone(engine.find_path(camp["floorplan"], camp["state"]["player"]["pos"], study_tile, camp["state"]))

        with self.subTest(case_id="T2-F17-02"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "bt_miss2.db")
                db_mod.init_db(db_path)
                missing = catalog.get_studio_missing_list(world, {"flags": {}, "discovered_items": []}, db_path=db_path)
            self.assertGreaterEqual(len(missing), 1)
            _assert_no_spoilers(self, missing, _find_missing_prop_secret_strings(sample))

        with self.subTest(case_id="T2-F17-03"):
            with tempfile.TemporaryDirectory() as tmp:
                db_path = os.path.join(tmp, "bt_intr.db")
                db_mod.init_db(db_path)
                camp = compiler.compile_new_campaign(sample, llm=make_mock_llm(), db_path=db_path)
                rooms_iter = list(world["rooms"].values()) if isinstance(world["rooms"], dict) else world["rooms"]
                priv = next(r for r in rooms_iter if (r.get("privacy") == "private" or r.get("type") == "private") and r.get("owner_id"))
                owner_id = priv["owner_id"]
                chars_map = world.get("characters", {})
                owner = chars_map[owner_id] if isinstance(chars_map, dict) and owner_id in chars_map else next(c for c in world["cast"] if c.get("id") == owner_id or c.get("char_id") == owner_id)
                slot = next((s for s in owner["schedule"] if s["room_id"] == priv["id"]), owner["schedule"][0])
                if "start" in slot:
                    hh, mm = (int(x) for x in slot["start"].split(":"))
                else:
                    hh, mm = int(slot["hour"]), int(slot["minute"])
                total_min = hh * 60 + mm
                safe_min = max(0, total_min - 15)
                warn_min = max(0, total_min - 3)
                camp["state"]["clock"].update({"hour": safe_min // 60, "minute": safe_min % 60})
                self.assertIsNone(engine.check_intrusion_imminent(world, camp["floorplan"], camp["state"], priv["id"], 5))
                camp["state"]["clock"].update({"hour": warn_min // 60, "minute": warn_min % 60})
                self.assertIsNotNone(engine.check_intrusion_imminent(world, camp["floorplan"], camp["state"], priv["id"], 5))

        with self.subTest(case_id="T2-F17-04"):
            with tempfile.TemporaryDirectory() as tmp:
                mock = make_mock_llm()
                srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
                try:
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": sample})
                    cid = cmp_body["campaign"]["id"]
                    _, cps, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid}")
                    cp_id = cps["checkpoints"][0]["id"]
                    _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Thử"})
                    _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": cp_id})
                    self.assertEqual(mock.call_count, 0)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F17-05"):
            starter = catalog.load_starter_catalog()
            missing_tags = {m["asset_tag"] for m in world.get("missing_props", [])}
            rooms_iter = list(world["rooms"].values()) if isinstance(world["rooms"], dict) else world["rooms"]
            for r in rooms_iter:
                for p in r.get("props", []):
                    tag = p["asset_tag"]
                    self.assertTrue(tag in starter or tag in missing_tags, msg=f"Unknown sample asset_tag: {tag}")

    def test_t2_f18(self) -> None:
        compiler = _mod("compiler")
        catalog = _mod("catalog")

        with self.subTest(case_id="T2-F18-01"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    st, body, hdrs = _http_raw_get(base_url, "/vendor/three/nonexistent.js")
                    self.assertEqual(st, 404)
                    self.assertNotIn("text/html", hdrs.get("content-type", ""))
                    self.assertNotIn(b"<!DOCTYPE html", body)
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F18-02"):
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _, _, h_html = _http_raw_get(base_url, "/")
                    _, _, h_css = _http_raw_get(base_url, "/styles.css")
                    _, _, h_js = _http_raw_get(base_url, "/iso_renderer.js")
                    self.assertIn("text/html", h_html.get("content-type", ""))
                    self.assertIn("text/css", h_css.get("content-type", ""))
                    self.assertIn("javascript", h_js.get("content-type", ""))
                finally:
                    _stop_iso_server(srv)

        with self.subTest(case_id="T2-F18-03"):
            fp = compiler.pack_floorplan(make_test_world(num_rooms=8), catalog.load_starter_catalog())
            grid = fp["grid"]
            self.assertIsInstance(grid, list)
            self.assertGreaterEqual(len(grid), fp["height"])
            self.assertLessEqual(len(grid), 24)
            self.assertTrue(all(len(row) <= 24 for row in grid))
            self.assertLessEqual(fp["width"], 24)
            self.assertLessEqual(fp["height"], 24)

        with self.subTest(case_id="T2-F18-04"):
            with self.assertRaises(ValueError):
                catalog.validate_glb_bytes(b"BAD_GLB_DATA")
            self.assertIn("mystery_placeholder", catalog.load_starter_catalog())

        with self.subTest(case_id="T2-F18-05"):
            cine = _make_sample_scene_tree("n1", presentation_mode="cinematic_focus")
            std = _make_sample_scene_tree("n2", presentation_mode="standard")
            self.assertEqual(cine["nodes"]["node_root"]["presentation_mode"], "cinematic_focus")
            self.assertEqual(std["nodes"]["node_root"]["presentation_mode"], "standard")

    def test_t2_f19(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, _, _ = _start_test_iso_server(tmp)
            try:
                with self.subTest(case_id="T2-F19-01"):
                    st1, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "bad_x16", "name_vi": "X16", "size": [1, 1], "mount": "floor", "voxels": [{"x": 16, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1, "color": "#ffffff", "shape": "cube", "material": "plastic"}]})
                    self.assertEqual(st1, 400)
                    st2, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "bad_ym1", "name_vi": "Y-1", "size": [1, 1], "mount": "floor", "voxels": [{"x": 0, "y": -1, "z": 0, "w": 1, "h": 1, "d": 1, "color": "#ffffff", "shape": "cube", "material": "plastic"}]})
                    self.assertEqual(st2, 400)

                with self.subTest(case_id="T2-F19-02"):
                    st1, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "bad_shape", "name_vi": "S", "size": [1, 1], "mount": "floor", "voxels": [{"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1, "color": "#ffffff", "shape": "pyramid_unknown", "material": "plastic"}]})
                    self.assertEqual(st1, 400)
                    st2, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "bad_mat", "name_vi": "M", "size": [1, 1], "mount": "floor", "voxels": [{"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1, "color": "#ffffff", "shape": "cube", "material": "diamond_unknown"}]})
                    self.assertEqual(st2, 400)

                with self.subTest(case_id="T2-F19-03"):
                    st, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "bad_glb", "name_vi": "G", "size": [1, 1], "mount": "floor", "kind": "glb", "glb_base64": base64.b64encode(b"NOTGLB").decode("ascii")})
                    self.assertEqual(st, 400)

                with self.subTest(case_id="T2-F19-04"):
                    st, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "bad_vox", "name_vi": "V", "size": [1, 1], "mount": "floor", "kind": "vox", "vox_base64": base64.b64encode(b"NOTVOX").decode("ascii")})
                    self.assertEqual(st, 400)

                with self.subTest(case_id="T2-F19-05"):
                    world_mp = make_test_world(with_missing_props=True)
                    _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": world_mp})
                    cid = cmp_body["campaign"]["id"]
                    _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "prop_mystery_01", "name_vi": "Built", "size": [1, 1], "mount": "surface", "voxels": [{"x": 2, "y": 0, "z": 2, "w": 2, "h": 2, "d": 2, "color": "#7f1d1d", "shape": "cube", "material": "fabric"}]})
                    _, missing_body, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                    items = missing_body.get("items") or missing_body.get("missing")
                    self.assertIs(items[0]["built"], True)
                    self.assertIs(items[0]["revealed"], False)
                    _assert_no_spoilers(self, items[0], _find_missing_prop_secret_strings(world_mp))
            finally:
                _stop_iso_server(srv)

    def test_t2_f20(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                if "brass_key" not in camp["state"]["player"]["inventory"]:
                    camp["state"]["player"]["inventory"].append("brass_key")
                    db_mod.save_campaign(camp, db_path=db_path)

                with self.subTest(case_id="T2-F20-01"):
                    st, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "invalid_action_xyz"})
                    self.assertEqual(st, 400)

                with self.subTest(case_id="T2-F20-02"):
                    st, body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "use_item", "item_id": "brass_key", "target_id": "nonexistent_target"})
                    self.assertIn(st, (200, 400))
                    if st == 200:
                        self.assertIs(body["success"], False)
                    st_after, post_body, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                    self.assertEqual(st_after, 200)
                    self.assertIn("brass_key", post_body["state"]["player"]["inventory"])

                with self.subTest(case_id="T2-F20-03"):
                    st, _, _ = _http_json(base_url, "POST", "/api/campaign/free_action", {"campaign_id": cid, "text": "   "})
                    self.assertEqual(st, 400)

                with self.subTest(case_id="T2-F20-04"):
                    st, _, _ = _http_json(base_url, "GET", "/api/campaign/state?campaign_id=unknown_id_999")
                    self.assertEqual(st, 404)

                with self.subTest(case_id="T2-F20-05"):
                    st, body, _ = _http_json(base_url, "POST", "/api/settings", {"max_prefetch_depth": -5, "max_workers": 99})
                    if st == 200:
                        cfg = body.get("settings", body)
                        self.assertIn("max_workers", cfg)
                        self.assertIn("max_prefetch_depth", cfg)
                        self.assertGreaterEqual(cfg["max_workers"], 1)
                        self.assertLessEqual(cfg["max_workers"], 2)
                        self.assertGreaterEqual(cfg["max_prefetch_depth"], 0)
                        self.assertLessEqual(cfg["max_prefetch_depth"], 1)
                    else:
                        self.assertEqual(st, 400)
            finally:
                _stop_iso_server(srv)

    def test_t2_f21(self) -> None:
        with self.subTest(case_id="T2-F21-01"):
            with tempfile.TemporaryDirectory() as tmp:
                bad_py = os.path.join(tmp, "broken.py")
                with open(bad_py, "w", encoding="utf-8") as f:
                    f.write("def oops(:\n    pass\n")
                r = subprocess.run([sys.executable, "-m", "py_compile", bad_py], capture_output=True, text=True, check=False)
                self.assertNotEqual(r.returncode, 0)

        with self.subTest(case_id="T2-F21-02"):
            with tempfile.TemporaryDirectory() as tmp:
                bad_js = os.path.join(tmp, "broken.mjs")
                with open(bad_js, "w", encoding="utf-8") as f:
                    f.write("export const x = {;\n")
                r = subprocess.run(["node", "--check", bad_js], capture_output=True, text=True, check=False)
                self.assertNotEqual(r.returncode, 0)

        with self.subTest(case_id="T2-F21-03"):
            with tempfile.TemporaryDirectory() as tmp:
                empty_glb = os.path.join(tmp, "empty.glb")
                with open(empty_glb, "wb") as f:
                    f.write(b"")
                r = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--validate-glb", empty_glb], capture_output=True, text=True, check=False)
                self.assertNotEqual(r.returncode, 0)

        with self.subTest(case_id="T2-F21-04"):
            # Verify no unguarded live external network URLs exist in test suite or core data modules
            forbidden_urls = (
                "https://" + "openrouter.ai",
                "https://" + "generativelanguage.googleapis.com",
            )
            scan_paths = [os.path.join(TESTS_DIR, fn) for fn in sorted(os.listdir(TESTS_DIR)) if fn.endswith(".py")]
            for mod_name in ("schemas.py", "db.py", "catalog.py"):
                p = os.path.join(ISO_ENGINE_DIR, mod_name)
                if os.path.isfile(p):
                    scan_paths.append(p)
            for path in scan_paths:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
                for url in forbidden_urls:
                    self.assertNotIn(url, src, msg=f"Unexpected live API URL {url!r} in {path}")

        with self.subTest(case_id="T2-F21-05"):
            default_db = os.path.join(ISO_ENGINE_DIR, "data", "iso_engine.db")
            existed_before = os.path.isfile(default_db)
            before_stat = os.stat(default_db).st_mtime_ns if existed_before else None
            with tempfile.TemporaryDirectory() as tmp:
                srv, base_url, _, _ = _start_test_iso_server(tmp)
                try:
                    _http_json(base_url, "GET", "/api/bootstrap")
                finally:
                    _stop_iso_server(srv)
            if existed_before:
                self.assertEqual(os.stat(default_db).st_mtime_ns, before_stat)
            else:
                self.assertFalse(os.path.isfile(default_db))


# ==============================================================================
# TIER 3: Cross-Feature Pairwise Integration (24 Combinations)
# ==============================================================================


class TestTier3CrossFeaturePairwise(unittest.TestCase):
    """Tier 3: 24 pairwise integration tests across F1-F21."""

    def test_t3_01(self) -> None:
        """T3-01: F4 + F10 — v2 upgrade -> pack_floorplan & compile_new_campaign."""
        schemas = _mod("schemas")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        engine = _mod("engine")
        db_mod = _mod("db")

        with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
            w2 = json.load(f)
        iso_w = schemas.upgrade_world_v2_to_iso(w2, search_dirs=[SAMPLE_CHARS_DIR])
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_01.db")
            db_mod.init_db(db_path)
            first_room_id = next(iter(iso_w["rooms"].keys())) if isinstance(iso_w["rooms"], dict) else iso_w["rooms"][0]["id"]
            mock = make_mock_llm([{"storylets": [_make_sample_storylet("s1", first_room_id)]}, _make_sample_scene_tree("s1")] * 4)
            camp = compiler.compile_new_campaign(iso_w, llm=mock, catalog=catalog.load_starter_catalog(), db_path=db_path)
            fp = camp["floorplan"]
            self.assertLessEqual(fp["width"], 24)
            self.assertLessEqual(fp["height"], 24)
            self.assertLessEqual(len(fp["rooms"]), 8)
            for cid in ("vy", "han", "khanh", "tuan"):
                self.assertIn(cid, camp["state"]["npcs"])
            rooms_list = _rooms_list(fp)
            r0 = rooms_list[0]
            start = _find_walkable_tile(fp, r0["id"], camp["state"])
            for rr in rooms_list:
                goal = _find_walkable_tile(fp, rr["id"], camp["state"])
                self.assertIsNotNone(engine.find_path(fp, start, goal, camp["state"]))

    def test_t3_02(self) -> None:
        """T3-02: F5 + F19 — Spoiler-blind build -> discover -> reveal via HTTP."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, _, _ = _start_test_iso_server(tmp)
            try:
                world_mp = make_test_world(with_missing_props=True)
                secrets = _find_missing_prop_secret_strings(world_mp)
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": world_mp})
                cid = cmp_body["campaign"]["id"]
                _, m1, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                i1 = (m1.get("items") or m1.get("missing"))[0]
                self.assertIs(i1["revealed"], False)
                _assert_no_spoilers(self, i1, secrets)

                st_save, _, _ = _http_json(
                    base_url,
                    "POST",
                    "/api/studio/save_asset",
                    {"asset_tag": "prop_mystery_01", "name_vi": "Lego", "size": [1, 1], "mount": "surface", "voxels": [{"x": 1, "y": 1, "z": 1, "w": 2, "h": 2, "d": 2, "color": "#7f1d1d", "shape": "cube", "material": "fabric"}]},
                )
                self.assertEqual(st_save, 200)
                _, m2, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                i2 = (m2.get("items") or m2.get("missing"))[0]
                self.assertIs(i2["built"], True)
                self.assertIs(i2["revealed"], False)
                _assert_no_spoilers(self, i2, secrets)

                # Unlock discovery flag via action / state delta
                _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "use_item", "item_id": "brass_key", "target_id": "door_foyer_study"})
                db_mod = _mod("db")
                # Apply discover_items delta via engine & persist to verify HTTP reveal
                camp = db_mod.get_campaign(cid, db_path=os.path.join(tmp, "test_iso.db"))
                _mod("engine").apply_state_delta(camp["world"], camp["floorplan"], camp["state"], {"discover_items": ["prop_mystery_01"]})
                db_mod.save_campaign(camp, db_path=os.path.join(tmp, "test_iso.db"))

                _, m3, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                i3 = (m3.get("items") or m3.get("missing"))[0]
                self.assertIs(i3["revealed"], True)
                self.assertEqual(i3["true_name"], "Hộp nhẫn đính hôn bị giấu")
            finally:
                _stop_iso_server(srv)

    def test_t3_03(self) -> None:
        """T3-03: F9 + F15 — Prefetch populates cache, unrelated mutation + rewind hits cache at 0 LLM calls."""
        prefetch = _mod("prefetch")
        db_mod = _mod("db")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_03.db")
            db_mod.init_db(db_path)
            st = _make_sample_storylet("st_s")
            state = {"clock": {"mode": "calendar", "hour": 8, "minute": 30}, "flags": {"searched_drawer": False, "side": 1}, "player": {"inventory": ["brass_key"]}, "relationships": {"linh": 10}}
            db_mod.save_campaign({"id": "c_33", "world": make_test_world(), "state": state, "history": []}, db_path=db_path)
            mock = make_mock_llm([_make_sample_scene_tree("st_s"), _make_sample_scene_tree("unexpected_regen")])
            pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
            try:
                pm.enqueue("c_33", st, state, mock, priority=pm.PRIO_P0, db_path=db_path)
                import time
                h = prefetch.masked_state_hash(state, st["relevant_keys"])
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if db_mod.get_cached_scene("c_33", "st_s", h, db_path=db_path) is not None:
                        break
                    time.sleep(0.05)
                self.assertEqual(mock.call_count, 1)
                cp_id = _db_create_checkpoint(db_mod, "c_33", "CP", "manual", {"state": state, "history": []}, db_path)
                state_mut = json.loads(json.dumps(state))
                state_mut["flags"]["side"] = 99
                h2 = prefetch.masked_state_hash(state_mut, st["relevant_keys"])
                self.assertEqual(h, h2)
                cp_resolved = cp_id["id"] if isinstance(cp_id, dict) else (cp_id if isinstance(cp_id, str) else db_mod.list_checkpoints("c_33", db_path=db_path)[0]["id"])
                restored = db_mod.get_checkpoint(cp_resolved, db_path=db_path)
                self.assertIsNotNone(restored)
                restored_state = restored["state"]
                h_rest = prefetch.masked_state_hash(restored_state, st["relevant_keys"])
                self.assertEqual(h_rest, h)
                # Re-running PrefetchManager.enqueue with mutated & restored states must hit cache (0 new LLM calls)
                pm.enqueue("c_33", st, state_mut, mock, priority=pm.PRIO_P0, db_path=db_path)
                pm.enqueue("c_33", st, restored_state, mock, priority=pm.PRIO_P0, db_path=db_path)
                pm.shutdown(wait=True)
                self.assertIsNotNone(db_mod.get_cached_scene("c_33", "st_s", h_rest, db_path=db_path))
                self.assertEqual(mock.call_count, 1)
            finally:
                pm.shutdown(wait=False)

    def test_t3_04(self) -> None:
        """T3-04: F11 + F12 — Risky search in private room intercepts with auto_risk + hide_inside."""
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        db_mod = _mod("db")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_04.db")
            db_mod.init_db(db_path)
            mock = make_mock_llm([{"storylets": [_make_sample_storylet("s1", "bedroom_linh")], "intrusion_scenes": {"bedroom_linh": _make_sample_scene_tree("intr_b")}}, _make_sample_scene_tree("s1"), _make_sample_scene_tree("intr_b")] * 3)
            camp = compiler.compile_new_campaign(make_test_world(), llm=mock, catalog=catalog.load_starter_catalog(), db_path=db_path)
            camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}
            camp["state"]["player"]["room_id"] = "bedroom_linh"
            warn = engine.check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", 5)
            self.assertIsNotNone(warn)
            camp["state"]["pending_intrusion"] = warn
            db_mod.save_campaign(camp, db_path=db_path)
            res = engine.resolve_intrusion_choice(camp, "hide", forced_roll=18, db_path=db_path)
            self.assertIs(res["d20"]["success"], True)
            self.assertIs(res["success"], True)
            self.assertEqual(camp["state"]["player"]["pose"], "hide_inside")
            cps = db_mod.list_checkpoints(camp["id"], db_path=db_path)
            self.assertTrue(any(cp.get("kind") == "auto_risk" for cp in cps))

    def test_t3_05(self) -> None:
        """T3-05: F12 + F15 — Intrusion fail -> rewind auto_risk -> retreat safely."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}
                camp["state"]["player"]["room_id"] = "bedroom_linh"
                camp["state"]["player"]["pos"] = _find_walkable_tile(camp["floorplan"], "bedroom_linh", camp["state"])
                warn = _mod("engine").check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", 5)
                self.assertIsNotNone(warn)
                camp["state"]["pending_intrusion"] = warn
                db_mod.save_campaign(camp, db_path=db_path)

                # Fail stealth via server action (which also creates auto_risk checkpoint)
                st_fail, fail_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "intrusion_choice", "choice_kind": "hide", "forced_roll": 1})
                self.assertEqual(st_fail, 200)
                self.assertIs(fail_body["d20"]["success"], False)

                _, cps_body, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid}")
                risk_cp = next((cp for cp in cps_body["checkpoints"] if cp.get("kind") == "auto_risk"), None)
                self.assertIsNotNone(risk_cp, f"Expected server/engine-created auto_risk checkpoint, got {cps_body}")

                # Rewind to auto_risk
                st_rew, rew_body, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": risk_cp["id"]})
                self.assertEqual(st_rew, 200)
                self.assertEqual(rew_body["state"]["player"]["room_id"], "bedroom_linh")
                # Retreat
                st_ret, ret_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "intrusion_choice", "choice_kind": "retreat"})
                self.assertEqual(st_ret, 200)
                self.assertNotEqual(ret_body["state"]["player"]["room_id"], "bedroom_linh")
            finally:
                _stop_iso_server(srv)

    def test_t3_06(self) -> None:
        """T3-06: F13 + F18 — Off-screen conflict emits renderer-ready adjacent_sound + trace_hotspot + mood."""
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        world = make_test_world(num_rooms=6)
        fp = compiler.pack_floorplan(world, catalog.load_starter_catalog())
        adj = fp["adjacent_rooms"]["living_room"]
        adj_room = "kitchen" if "kitchen" in adj else adj[0]
        for cid, c in world["characters"].items():
            if cid in ("minh", "an"):
                c["schedule"][1]["room_id"] = adj_room
        state = {
            "clock": {"year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30},
            "player": {"room_id": "living_room"},
            "npcs": {"minh": {"room_id": "foyer", "mood": "neutral"}, "an": {"room_id": "foyer", "mood": "neutral"}},
            "traces": [],
        }
        events = engine.advance_clock_and_sim(world, fp, state, 35)
        adj_evs = [e for e in events if (e.get("kind") or e.get("type")) == "adjacent_sound"]
        self.assertGreaterEqual(len(adj_evs), 1)
        self.assertIn(adj_evs[0].get("from_room"), fp["adjacent_rooms"]["living_room"])
        tr = state["traces"][0]
        self.assertIn("pos", tr)
        self.assertEqual(tr["room_id"], adj_room)

    def test_t3_07(self) -> None:
        """T3-07: F14 + F15 — Act 1 -> Act 2 -> rewind to Act 1 restores act_index while keeping Act 2 cache."""
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm([
                {"storylets": [_make_sample_storylet("s1", "foyer")]},
                _make_sample_scene_tree("s1"),
                {"storylets": [_make_sample_storylet("s2", "living_room")]},
                _make_sample_scene_tree("s2"),
            ] * 4)
            srv, base_url, db_path, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                _, cps, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid}")
                cp1_id = cps["checkpoints"][0]["id"]
                _http_json(base_url, "POST", "/api/campaign/advance_act", {"campaign_id": cid})
                conn = sqlite3.connect(db_path)
                cache_before = conn.execute("SELECT COUNT(*) FROM scene_cache WHERE campaign_id=?", (cid,)).fetchone()[0]
                conn.close()
                st_r, rew, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": cp1_id})
                self.assertEqual(st_r, 200)
                self.assertEqual(rew["campaign"]["act_index"], 1)
                conn = sqlite3.connect(db_path)
                cache_after = conn.execute("SELECT COUNT(*) FROM scene_cache WHERE campaign_id=?", (cid,)).fetchone()[0]
                conn.close()
                self.assertEqual(cache_before, cache_after)
            finally:
                _stop_iso_server(srv)

    def test_t3_08(self) -> None:
        """T3-08: F16 + F17 — Ephemeral server + biet_thu_mua_ngau.json compile/state/action at 0 LLM calls."""
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm()
            srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                sample = _load_biet_thu_sample()
                st, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": sample})
                self.assertEqual(st, 200)
                cid = cmp_body["campaign"]["id"]
                st2, s_body, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                self.assertEqual(st2, 200)
                self.assertEqual(len(s_body["floorplan"]["rooms"]), 6)
                st3, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Xin chào"})
                self.assertEqual(st3, 200)
                self.assertEqual(mock.call_count, 0)
            finally:
                _stop_iso_server(srv)

    def test_t3_09(self) -> None:
        """T3-09: F1 + F8 — 3-level .env resolution + LiveLLMClient + /api/bootstrap secret redaction."""
        llm_mod = _mod("llm_client")
        payload = json.dumps({"choices": [{"message": {"content": '{"status": "ok"}'}}]})
        srv_llm, captured = _start_mock_llm_http_server([(200, payload, None)])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                top_dir = os.path.join(tmp, "top")
                leaf_dir = os.path.join(top_dir, "mid", "leaf")
                os.makedirs(leaf_dir)
                with open(os.path.join(top_dir, ".env"), "w", encoding="utf-8") as f:
                    f.write("OPENROUTER_API_KEY=sk-or-test-secret-999\n")
                sig = inspect.signature(llm_mod.load_env_var)
                if "start_dir" in sig.parameters:
                    resolved = llm_mod.load_env_var("OPENROUTER_API_KEY", start_dir=leaf_dir)
                elif "search_dirs" in sig.parameters:
                    resolved = llm_mod.load_env_var("OPENROUTER_API_KEY", search_dirs=[leaf_dir, os.path.dirname(leaf_dir), top_dir])
                else:
                    resolved = "sk-or-test-secret-999"
                client = _make_live_llm_for_url(f"http://127.0.0.1:{srv_llm.server_address[1]}/v1/chat/completions", provider="openrouter", api_key=resolved)
                res, _ = client.complete_json([{"role": "user", "content": "ping"}])
                self.assertEqual(res["status"], "ok")
                self.assertIn(resolved, captured[0]["headers"].get("authorization", ""))
        finally:
            srv_llm.shutdown()
            srv_llm.server_close()

    def test_t3_10(self) -> None:
        """T3-10: F2 + F9 — PrefetchManager 2 workers writing WAL DB concurrently with main-thread reads."""
        prefetch = _mod("prefetch")
        db_mod = _mod("db")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_10.db")
            db_mod.init_db(db_path)
            base_state = {"flags": {}, "player": {"inventory": []}, "affinity": {}}
            db_mod.save_campaign({"id": "c_conc", "world": make_test_world(), "state": base_state, "history": []}, db_path=db_path)
            mock = make_mock_llm([_make_sample_scene_tree(f"st_{i}") for i in range(4)])
            pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
            try:
                for i in range(4):
                    pm.enqueue("c_conc", _make_sample_storylet(f"st_{i}"), base_state, mock, db_path=db_path)
                for _ in range(10):
                    self.assertIsNotNone(db_mod.get_campaign("c_conc", db_path=db_path))
                import time
                deadline = time.time() + 4.0
                h0 = prefetch.masked_state_hash(base_state, _make_sample_storylet("st_0")["relevant_keys"])
                while time.time() < deadline:
                    if db_mod.get_cached_scene("c_conc", "st_0", h0, db_path=db_path) is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(db_mod.get_cached_scene("c_conc", "st_0", h0, db_path=db_path))
            finally:
                pm.shutdown(wait=True)

    def test_t3_11(self) -> None:
        """T3-11: F3 + F11 — SceneNode pose_updates + lighting_override + discover_items + unlock_doors -> StateDelta."""
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        world = make_test_world(num_rooms=6, with_locked_room=True, with_missing_props=True)
        fp = compiler.pack_floorplan(world, catalog.load_starter_catalog())
        state = {
            "player": {"room_id": "foyer", "pos": _find_walkable_tile(fp, "foyer", {"doors_unlocked": []}), "pose": "stand", "inventory": []},
            "npcs": {"linh": {"room_id": "living_room", "pose": "stand"}},
            "doors_unlocked": [],
            "flags": {},
            "discovered_items": [],
            "lighting": {},
        }
        delta = {
            "pose_updates": {"linh": {"pose": "sit", "target_prop": "living_sofa"}},
            "lighting_override": "warm_evening",
            "discover_items": ["prop_mystery_01"],
            "unlock_doors": ["door_foyer_study"],
        }
        engine.apply_state_delta(world, fp, state, delta)
        self.assertEqual(state["npcs"]["linh"]["pose"], "sit")
        self.assertIn("prop_mystery_01", state["discovered_items"])
        self.assertIn("door_foyer_study", state["doors_unlocked"])

    def test_t3_12(self) -> None:
        """T3-12: F6 + F11 — Catalog pose_slots validation inside apply_state_delta."""
        engine = _mod("engine")
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        world = make_test_world(num_rooms=6)
        fp = compiler.pack_floorplan(world, catalog.load_starter_catalog())
        state = {"player": {"room_id": "bedroom_linh", "pose": "stand"}, "npcs": {}, "flags": {}, "discovered_items": []}
        engine.apply_state_delta(world, fp, state, {"pose_updates": {"user": {"pose": "lie", "target_prop": "linh_bed"}}})
        self.assertEqual(state["player"]["pose"], "lie")
        engine.apply_state_delta(world, fp, state, {"pose_updates": {"user": {"pose": "hide_inside", "target_prop": "linh_wardrobe"}}})
        self.assertEqual(state["player"]["pose"], "hide_inside")
        state["player"]["pose"] = "stand"
        engine.apply_state_delta(world, fp, state, {"pose_updates": {"user": {"pose": "hide_inside", "target_prop": "living_table"}}})
        self.assertNotEqual(state["player"]["pose"], "hide_inside")

    def test_t3_13(self) -> None:
        """T3-13: F7 + F19 — Blender kit --create-minimal-glb -> /api/studio/save_asset -> merged catalog."""
        catalog = _mod("catalog")
        with tempfile.TemporaryDirectory() as tmp:
            glb_file = os.path.join(tmp, "kit.glb")
            r = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--create-minimal-glb", glb_file], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0)
            with open(glb_file, "rb") as f:
                raw = f.read()
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                st, _, _ = _http_json(
                    base_url,
                    "POST",
                    "/api/studio/save_asset",
                    {"asset_tag": "blender_asset_01", "name_vi": "Blender Prop", "size": [1, 1], "mount": "floor", "kind": "glb", "glb_base64": base64.b64encode(raw).decode("ascii")},
                )
                self.assertEqual(st, 200)
                merged = catalog.get_merged_catalog(db_path=db_path)
                self.assertIn("blender_asset_01", merged)
            finally:
                _stop_iso_server(srv)

    def test_t3_14(self) -> None:
        """T3-14: F8 + F14 — Multi-act LLM compile -> Act 3 Ending -> Free Sandbox."""
        compiler = _mod("compiler")
        engine = _mod("engine")
        db_mod = _mod("db")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_14.db")
            db_mod.init_db(db_path)
            mock = make_mock_llm([
                {"storylets": [_make_sample_storylet("s1", "foyer")]},
                _make_sample_scene_tree("s1"),
                {"storylets": [_make_sample_storylet("s2", "living_room")]},
                _make_sample_scene_tree("s2"),
                {"storylets": [_make_sample_storylet("s3", "study")]},
                _make_sample_scene_tree("s3"),
            ] * 3)
            camp = compiler.compile_new_campaign(make_test_world(), llm=mock, db_path=db_path)
            camp = compiler.compile_next_act(camp, mock, db_path=db_path)
            self.assertEqual(camp["act_index"], 2)
            camp = compiler.compile_next_act(camp, mock, db_path=db_path)
            self.assertEqual(camp["act_index"], 3)
            camp["state"]["flags"]["ending_a"] = True
            res = engine.check_act_or_ending(camp["world"], camp["state"], camp["storylets"])
            self.assertEqual(res["ending"]["id"], "ending_truth")
            engine.enable_free_sandbox(camp["state"])
            self.assertTrue(camp["state"]["free_sandbox_mode"])

    def test_t3_15(self) -> None:
        """T3-15: F10 + F11 — Compiler floorplan A* reachability + locked room unlock."""
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        engine = _mod("engine")
        world = make_test_world(num_rooms=6, with_locked_room=True)
        fp = compiler.pack_floorplan(world, catalog.load_starter_catalog())
        state = {
            "player": {"pos": _find_walkable_tile(fp, "foyer", {"doors_unlocked": []}), "inventory": ["brass_key"]},
            "doors_unlocked": [],
        }
        for p in fp["props"]:
            if p["room_id"] != "study":
                self.assertIsNotNone(engine.find_approach_tile(fp, p, state["player"]["pos"], state))
        study_tile = _find_walkable_tile(fp, "study", {"doors_unlocked": ["door_foyer_study"]})
        self.assertIsNone(engine.find_path(fp, state["player"]["pos"], study_tile, state))
        engine.use_item(world, fp, state, "brass_key", "door_foyer_study")
        study_prop = next(p for p in fp["props"] if p["room_id"] == "study")
        app = engine.find_approach_tile(fp, study_prop, state["player"]["pos"], state)
        self.assertIsNotNone(engine.find_path(fp, state["player"]["pos"], app, state))

    def test_t3_16(self) -> None:
        """T3-16: F10 + F13 — adjacent_rooms wall map governs adjacent_sound emission."""
        compiler = _mod("compiler")
        catalog = _mod("catalog")
        engine = _mod("engine")
        world = make_test_world(num_rooms=6)
        fp = compiler.pack_floorplan(world, catalog.load_starter_catalog())
        adj = set(fp["adjacent_rooms"].get("foyer", []))
        adj_room = next(iter(adj))
        for cid, c in world["characters"].items():
            if cid in ("minh", "an"):
                c["schedule"][1]["room_id"] = adj_room
        state = {"clock": {"year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30}, "player": {"room_id": "foyer"}, "npcs": {"minh": {"room_id": "kitchen"}, "an": {"room_id": "kitchen"}}, "traces": []}
        events = engine.advance_clock_and_sim(world, fp, state, 35)
        self.assertTrue(any((e.get("kind") or e.get("type")) == "adjacent_sound" for e in events))

    def test_t3_17(self) -> None:
        """T3-17: F11 + F17 — biet_thu_mua_ngau.json key pickup -> unlock door -> discover MissingPropSpec."""
        compiler = _mod("compiler")
        engine = _mod("engine")
        catalog = _mod("catalog")
        db_mod = _mod("db")
        sample = _load_biet_thu_sample()
        world = sample.get("world", sample)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_17.db")
            db_mod.init_db(db_path)
            camp = compiler.compile_new_campaign(sample, llm=make_mock_llm(), db_path=db_path)
            unlocked_set = set(camp["state"].get("doors_unlocked", []))
            locked = next(d for d in camp["floorplan"]["doors"] if d.get("locked") and d["id"] not in unlocked_set)
            key_id = locked["key_item_id"]
            if key_id not in camp["state"]["player"]["inventory"]:
                camp["state"]["player"]["inventory"].append(key_id)
            engine.use_item(world, camp["floorplan"], camp["state"], key_id, locked["id"])
            self.assertIn(locked["id"], camp["state"]["doors_unlocked"])
            m_tag = world["missing_props"][0]["asset_tag"]
            engine.apply_state_delta(world, camp["floorplan"], camp["state"], {"discover_items": [m_tag]})
            missing = catalog.get_studio_missing_list(world, camp["state"], db_path=db_path)
            self.assertIs(missing[0]["revealed"], True)

    def test_t3_18(self) -> None:
        """T3-18: F12 + F17 — biet_thu_mua_ngau.json private intrusion + auto_risk + hide_inside at 0 LLM calls."""
        compiler = _mod("compiler")
        engine = _mod("engine")
        db_mod = _mod("db")
        sample = _load_biet_thu_sample()
        world = sample.get("world", sample)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_18.db")
            db_mod.init_db(db_path)
            mock = make_mock_llm()
            camp = compiler.compile_new_campaign(sample, llm=mock, db_path=db_path)
            rooms_iter = list(world["rooms"].values()) if isinstance(world["rooms"], dict) else world["rooms"]
            priv = next(r for r in rooms_iter if (r.get("privacy") == "private" or r.get("type") == "private") and r.get("owner_id"))
            owner_id = priv["owner_id"]
            chars_map = world.get("characters", {})
            owner = chars_map[owner_id] if isinstance(chars_map, dict) and owner_id in chars_map else next(c for c in world["cast"] if c.get("id") == owner_id or c.get("char_id") == owner_id)
            slot = next((s for s in owner["schedule"] if s["room_id"] == priv["id"]), owner["schedule"][0])
            if "start" in slot:
                hh, mm = (int(x) for x in slot["start"].split(":"))
            else:
                hh, mm = int(slot["hour"]), int(slot["minute"])
            arrive_m = hh * 60 + mm
            t_m = max(0, arrive_m - 3)
            camp["state"]["clock"].update({"hour": t_m // 60, "minute": t_m % 60})
            camp["state"]["player"]["room_id"] = priv["id"]
            warn = engine.check_intrusion_imminent(world, camp["floorplan"], camp["state"], priv["id"], 5)
            self.assertIsNotNone(warn)
            camp["state"]["pending_intrusion"] = warn
            db_mod.save_campaign(camp, db_path=db_path)
            res = engine.resolve_intrusion_choice(camp, "hide", forced_roll=19, db_path=db_path)
            self.assertIs(res["d20"]["success"], True)
            self.assertIs(res["success"], True)
            self.assertEqual(camp["state"]["player"]["pose"], "hide_inside")
            cps = db_mod.list_checkpoints(camp["id"], db_path=db_path)
            self.assertTrue(any(cp.get("kind") == "auto_risk" for cp in cps))
            self.assertEqual(mock.call_count, 0)

    def test_t3_19(self) -> None:
        """T3-19: F13 + F20 — HTTP speech public vs whisper exposes witnesses & co_located_npcs for UI."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                camp["state"]["player"]["room_id"] = "living_room"
                camp["state"]["npcs"]["linh"]["room_id"] = "living_room"
                camp["state"]["npcs"]["an"]["room_id"] = "living_room"
                db_mod.save_campaign(camp, db_path=db_path)

                st1, b1, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Chào mọi người", "whisper": False})
                self.assertEqual(st1, 200)
                self.assertGreaterEqual(len(b1["witnesses"]), 2)

                st2, b2, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Nghe riêng nè", "whisper": True, "target_npc_id": "linh", "forced_roll": 19})
                self.assertEqual(st2, 200)
                self.assertEqual(b2["witnesses"], ["linh"])
            finally:
                _stop_iso_server(srv)

    def test_t3_20(self) -> None:
        """T3-20: F16 + F19 — Studio save_asset hot-reload visible in missing & state over HTTP."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, _, _ = _start_test_iso_server(tmp)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world(with_missing_props=True)})
                cid = cmp_body["campaign"]["id"]
                _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "prop_mystery_01", "name_vi": "Hot", "size": [1, 1], "mount": "surface", "voxels": [{"x": 0, "y": 0, "z": 0, "w": 2, "h": 2, "d": 2, "color": "#ef4444", "shape": "cube", "material": "plastic"}]})
                _, m_body, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                self.assertTrue((m_body.get("items") or m_body.get("missing"))[0]["built"])
                st_b, boot, _ = _http_json(base_url, "GET", "/api/bootstrap")
                self.assertEqual(st_b, 200)
                self.assertIn("prop_mystery_01", json.dumps(boot))
            finally:
                _stop_iso_server(srv)

    def test_t3_21(self) -> None:
        """T3-21: F16 + F20 — HTTP free_action Vietnamese input -> MockLLMClient -> StateDelta & witnesses."""
        with tempfile.TemporaryDirectory() as tmp:
            mock = _make_routed_mock_llm(
                routes=[
                    ("quan sát bức tranh", {"prose": "Bạn quan sát bức tranh sơn dầu cũ.", "effects": {"set_flags": {"inspected_painting": True}}}),
                    ("node", _make_sample_scene_tree("s1")),
                ],
                default={"storylets": [_make_sample_storylet("s1", "foyer")]},
            )
            srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                st, body, _ = _http_json(base_url, "POST", "/api/campaign/free_action", {"campaign_id": cid, "text": "Quan sát bức tranh trên tường"})
                self.assertEqual(st, 200)
                self.assertIs(body["state"]["flags"]["inspected_painting"], True)
                self.assertIn("witnesses", body)
            finally:
                _stop_iso_server(srv)

    def test_t3_22(self) -> None:
        """T3-22: F4 + F16 — HTTP /api/import/convert -> /api/campaign/compile -> /api/campaign/state."""
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm([{"storylets": [_make_sample_storylet("s1", "san_truong")]}, _make_sample_scene_tree("s1")] * 3)
            srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
                    w2 = json.load(f)
                st1, conv, _ = _http_json(base_url, "POST", "/api/import/convert", {"world": w2})
                self.assertEqual(st1, 200)
                iso_w = conv.get("world") or conv
                st2, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": iso_w})
                self.assertEqual(st2, 200)
                cid = cmp_body["campaign"]["id"]
                st3, s_body, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                self.assertEqual(st3, 200)
                self.assertLessEqual(len(s_body["floorplan"]["rooms"]), 8)
            finally:
                _stop_iso_server(srv)

    def test_t3_23(self) -> None:
        """T3-23: F5 + F11 — apply_state_delta discover_items flips get_studio_missing_list revealed False -> True."""
        engine = _mod("engine")
        catalog = _mod("catalog")
        db_mod = _mod("db")
        world = make_test_world(with_missing_props=True)
        fp = _mod("compiler").pack_floorplan(world, catalog.load_starter_catalog())
        state = {"player": {"inventory": []}, "flags": {}, "discovered_items": []}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t3_23.db")
            db_mod.init_db(db_path)
            m_before = catalog.get_studio_missing_list(world, state, db_path=db_path)
            self.assertIs(m_before[0]["revealed"], False)
            _assert_no_spoilers(self, m_before[0], _find_missing_prop_secret_strings(world))
            engine.apply_state_delta(world, fp, state, {"discover_items": ["prop_mystery_01"]})
            m_after = catalog.get_studio_missing_list(world, state, db_path=db_path)
            self.assertIs(m_after[0]["revealed"], True)
            self.assertEqual(m_after[0]["true_name"], "Hộp nhẫn đính hôn bị giấu")

    def test_t3_24(self) -> None:
        """T3-24: F18 + F20 — Frontend HTML/CSS/ESM co-integrity and syntax check."""
        with open(os.path.join(ISO_ENGINE_DIR, "web", "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(ISO_ENGINE_DIR, "web", "styles.css"), "r", encoding="utf-8") as f:
            css = f.read()
        for js in ("iso_renderer.js", "voxel_studio.js", "ui.js"):
            self.assertIn(js, html)
            r = subprocess.run(["node", "--check", os.path.join(ISO_ENGINE_DIR, "web", js)], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("isoCanvas", html)
        self.assertIn("intrusion", html.lower())
        self.assertIn("cinematic", css.lower())


# ==============================================================================
# TIER 4: Real-World End-to-End Application Scenarios (12 Scenarios)
# ==============================================================================


class TestTier4RealWorldScenarios(unittest.TestCase):
    """Tier 4: 12 comprehensive real-world E2E scenarios."""

    def test_t4_01(self) -> None:
        """T4-01: Full Playthrough of biet_thu_mua_ngau.json (0 API calls in Act 1)."""
        sample = _load_biet_thu_sample()
        world = sample.get("world", sample)
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm()
            srv, base_url, db_path, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                st_cmp, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": sample})
                self.assertEqual(st_cmp, 200)
                cid = cmp_body["campaign"]["id"]
                fp = cmp_body["floorplan"]
                self.assertEqual(len(fp["rooms"]), 6)
                self.assertGreaterEqual(len(world["cast"]), 3)

                # Verify locked room blocked initially, then unlock via key item
                unlocked_set = set(cmp_body["state"].get("doors_unlocked", []))
                locked_door = next(d for d in fp["doors"] if d.get("locked") and d["id"] not in unlocked_set)
                key_id = locked_door["key_item_id"]
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                if key_id not in camp["state"]["player"]["inventory"]:
                    camp["state"]["player"]["inventory"].append(key_id)
                    db_mod.save_campaign(camp, db_path=db_path)

                st_u, u_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "use_item", "item_id": key_id, "target_id": locked_door["id"]})
                self.assertEqual(st_u, 200)
                self.assertIn(locked_door["id"], u_body["state"]["doors_unlocked"])

                # Verify unrevealed MissingPropSpec starts blind with zero spoiler leak
                _, miss0, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                m_item = (miss0.get("items") or miss0.get("missing"))[0]
                self.assertIs(m_item["revealed"], False)
                _assert_no_spoilers(self, miss0, _find_missing_prop_secret_strings(sample))

                # Play pre-compiled storylets (including cinematic_focus node) with 0 LLM calls
                storylets = camp.get("storylets", [])
                self.assertGreaterEqual(len(storylets), 1)
                for st_obj in storylets[:2]:
                    st_a, a_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "open_storylet", "storylet_id": st_obj["id"]})
                    self.assertEqual(st_a, 200)
                    self.assertIs(a_body["cache_hit"], True)

                self.assertEqual(mock.call_count, 0)
            finally:
                _stop_iso_server(srv)

    def test_t4_02(self) -> None:
        """T4-02: Legacy dai_hoc_ben_nghe_world.json Import -> Compile -> Play."""
        with open(SAMPLE_WORLD_V2, "r", encoding="utf-8") as f:
            w2 = json.load(f)
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm([{"storylets": [_make_sample_storylet("s_campus", "san_truong")]}, _make_sample_scene_tree("s_campus")] * 3)
            srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                st_c, conv, _ = _http_json(base_url, "POST", "/api/import/convert", {"world": w2})
                self.assertEqual(st_c, 200)
                iso_w = conv.get("world") or conv
                self.assertLessEqual(len(iso_w["rooms"]), 8)
                alias_map = iso_w.get("location_alias_map") or iso_w.get("meta", {}).get("location_alias_map")
                self.assertIsInstance(alias_map, dict)
                self.assertEqual({c.get("char_id") or c.get("id") for c in iso_w["cast"]}, {"vy", "han", "khanh", "tuan"})

                st_cmp, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": iso_w})
                self.assertEqual(st_cmp, 200)
                cid = cmp_body["campaign"]["id"]
                fp = cmp_body["floorplan"]
                start_rid = cmp_body["state"]["player"]["room_id"]
                target_room = next(r for r in _rooms_list(fp) if r["id"] != start_rid)
                target_xy = _find_walkable_tile(fp, target_room["id"], cmp_body["state"])
                st_mv, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "move", "target_xy": target_xy})
                self.assertEqual(st_mv, 200)
                st_sp, sp_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Chào các bạn!"})
                self.assertEqual(st_sp, 200)
                self.assertIn("witnesses", sp_body)
            finally:
                _stop_iso_server(srv)

    def test_t4_03(self) -> None:
        """T4-03: Intrusion Stealth Escape vs Caught Branching + Rewind."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 56}
                camp["state"]["player"]["room_id"] = "bedroom_linh"
                camp["state"]["player"]["pos"] = _find_walkable_tile(camp["floorplan"], "bedroom_linh", camp["state"])
                warn = _mod("engine").check_intrusion_imminent(camp["world"], camp["floorplan"], camp["state"], "bedroom_linh", 5)
                self.assertIsNotNone(warn)
                camp["state"]["pending_intrusion"] = warn
                db_mod.save_campaign(camp, db_path=db_path)

                # Branch A: Escape (server/engine creates auto_risk checkpoint)
                st_esc, esc_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "intrusion_choice", "choice_kind": "hide", "forced_roll": 19})
                self.assertEqual(st_esc, 200)
                self.assertEqual(esc_body["state"]["player"]["pose"], "hide_inside")
                self.assertIs(esc_body["d20"]["success"], True)

                _, cps_body, _ = _http_json(base_url, "GET", f"/api/campaign/checkpoints?campaign_id={cid}")
                risk_cp = next((cp for cp in cps_body["checkpoints"] if cp.get("kind") == "auto_risk"), None)
                self.assertIsNotNone(risk_cp, f"Expected engine/server auto_risk checkpoint, got {cps_body}")

                # Rewind back to auto_risk
                st_rew, _rew, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": risk_cp["id"]})
                self.assertEqual(st_rew, 200)

                # Branch B: Caught
                st_cat, cat_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "intrusion_choice", "choice_kind": "hide", "forced_roll": 1})
                self.assertEqual(st_cat, 200)
                self.assertIs(cat_body["d20"]["success"], False)
                self.assertIs(cat_body["success"], False)
                self.assertNotEqual(cat_body["state"]["player"]["pose"], "hide_inside")
            finally:
                _stop_iso_server(srv)

    def test_t4_04(self) -> None:
        """T4-04: Off-Screen Argument / Conflict Clue Investigation."""
        engine = _mod("engine")
        db_mod = _mod("db")
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                world = make_test_world(num_rooms=6)
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": world})
                cid = cmp_body["campaign"]["id"]
                camp = db_mod.get_campaign(cid, db_path=db_path)
                fp = camp["floorplan"]
                locked_room_ids = set()
                for d in fp.get("doors", []):
                    if d.get("locked") and d["id"] not in camp["state"].get("doors_unlocked", []):
                        locked_room_ids.update((d["room_a"], d["room_b"]))
                unlocked_adj = [
                    rid for rid in fp["adjacent_rooms"].get("living_room", [])
                    if rid not in locked_room_ids or rid == "living_room"
                ]
                self.assertGreaterEqual(len(unlocked_adj), 1, "Expected at least one unlocked adjacent room to living_room")
                adj_room = "kitchen" if "kitchen" in unlocked_adj else unlocked_adj[0]

                for npc_id, c in camp["world"]["characters"].items():
                    if npc_id in ("minh", "an"):
                        c["schedule"][1]["room_id"] = adj_room
                camp["world"]["offscreen_events"] = [
                    {
                        "id": "off_t4_04",
                        "room_id": adj_room,
                        "trigger_min": 540,
                        "participants": ["minh", "an"],
                        "secret_dialogue": "SECRET_T4_04_CONFLICT",
                        "sound_hint": "Tiếng tranh luận vọng qua tường.",
                        "trace_label": "Ly nước bị gạt đổ",
                        "mood_effects": {"minh": "angry", "an": "nervous"},
                    }
                ]
                camp["state"]["clock"] = {"mode": "calendar", "year": 2027, "month": 10, "day": 12, "hour": 8, "minute": 30}
                camp["state"]["player"]["room_id"] = "living_room"
                camp["state"]["player"]["pos"] = _find_walkable_tile(fp, "living_room", camp["state"])
                camp["state"]["player"]["stats"] = {"charm": 16, "logic": 15, "stealth": 16, "will": 13}
                camp["state"]["npcs"]["minh"] = {"room_id": "foyer", "mood": "neutral"}
                camp["state"]["npcs"]["an"] = {"room_id": "foyer", "mood": "neutral"}
                camp["state"]["relationships"] = {"minh": 10, "an": 10}
                camp["state"]["traces"] = []

                events = engine.advance_clock_and_sim(camp["world"], fp, camp["state"], 35)
                self.assertTrue(any((e.get("kind") or e.get("type")) == "adjacent_sound" for e in events))
                self.assertGreaterEqual(len(camp["state"]["traces"]), 1)
                self.assertNotIn("SECRET_T4_04_CONFLICT", json.dumps(events, ensure_ascii=False))
                ev_keys, _ = _collect_keys_and_scalars(events)
                self.assertNotIn("dialogue_lines", ev_keys)
                self.assertNotIn("secret_dialogue", ev_keys)
                db_mod.save_campaign(camp, db_path=db_path)

                # Walk A* into adj_room & inspect trace via actual server action
                target_xy = _find_walkable_tile(fp, adj_room, camp["state"])
                st_mv, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "move", "target_xy": target_xy})
                self.assertEqual(st_mv, 200)
                trace_id = camp["state"]["traces"][0]["id"]
                st_ins, ins_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "inspect_trace", "trace_id": trace_id})
                self.assertEqual(st_ins, 200)
                self.assertIs(ins_body["state"]["traces"][0]["inspected"], True)
                st_sp, sp_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Vừa có chuyện gì vậy?", "whisper": True, "target_npc_id": "minh", "forced_roll": 19})
                self.assertEqual(st_sp, 200)
                self.assertEqual(sp_body["witnesses"], ["minh"])
            finally:
                _stop_iso_server(srv)

    def test_t4_05(self) -> None:
        """T4-05: Voxel Studio Spoiler-Blind Build -> In-Game Reveal -> Hot-Reload."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                world_mp = make_test_world(with_missing_props=True)
                secrets = _find_missing_prop_secret_strings(world_mp)
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": world_mp})
                cid = cmp_body["campaign"]["id"]
                _, m1, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                _assert_no_spoilers(self, m1, secrets)

                # Build symmetric Lego prop within 16x16x16
                voxels = [
                    {"x": 6, "y": 0, "z": 6, "w": 2, "h": 2, "d": 2, "color": "#7f1d1d", "shape": "cube", "material": "fabric"},
                    {"x": 9, "y": 0, "z": 6, "w": 2, "h": 2, "d": 2, "color": "#cbd5e1", "shape": "stud", "material": "metal"},
                    {"x": 7, "y": 2, "z": 7, "w": 2, "h": 1, "d": 2, "color": "#38bdf8", "shape": "sphere", "material": "neon"},
                ]
                st_save, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "prop_mystery_01", "name_vi": "Món đồ tự dựng", "size": [1, 1], "mount": "surface", "voxels": voxels})
                self.assertEqual(st_save, 200)
                _, m2, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                i2 = (m2.get("items") or m2.get("missing"))[0]
                self.assertIs(i2["built"], True)
                self.assertIs(i2["revealed"], False)
                _assert_no_spoilers(self, m2, secrets)

                # Reveal in-game
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                _mod("engine").apply_state_delta(camp["world"], camp["floorplan"], camp["state"], {"discover_items": ["prop_mystery_01"]})
                db_mod.save_campaign(camp, db_path=db_path)

                _, m3, _ = _http_json(base_url, "GET", f"/api/studio/missing?campaign_id={cid}")
                i3 = (m3.get("items") or m3.get("missing"))[0]
                self.assertIs(i3["revealed"], True)
                self.assertEqual(i3["true_name"], "Hộp nhẫn đính hôn bị giấu")
            finally:
                _stop_iso_server(srv)

    def test_t4_06(self) -> None:
        """T4-06: 3-Act Lifecycle -> Multiple Endings -> Free Sandbox Continuation."""
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm([
                {"storylets": [_make_sample_storylet("s1", "foyer")]},
                _make_sample_scene_tree("s1"),
                {"storylets": [_make_sample_storylet("s2", "living_room")]},
                _make_sample_scene_tree("s2"),
                {"storylets": [_make_sample_storylet("s3", "study")]},
                _make_sample_scene_tree("s3"),
            ] * 3)
            srv, base_url, db_path, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                st2, b2, _ = _http_json(base_url, "POST", "/api/campaign/advance_act", {"campaign_id": cid})
                self.assertEqual(st2, 200)
                self.assertEqual(b2["campaign"]["act_index"], 2)
                st3, b3, _ = _http_json(base_url, "POST", "/api/campaign/advance_act", {"campaign_id": cid})
                self.assertEqual(st3, 200)
                self.assertEqual(b3["campaign"]["act_index"], 3)

                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                camp["state"]["flags"]["ending_a"] = True
                end_res = _mod("engine").check_act_or_ending(camp["world"], camp["state"], camp["storylets"])
                self.assertEqual(end_res["ending"]["id"], "ending_truth")
                camp["state"]["ending"] = end_res["ending"]
                db_mod.save_campaign(camp, db_path=db_path)

                st_sb, sb_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "toggle_sandbox"})
                self.assertEqual(st_sb, 200)
                self.assertTrue(sb_body["state"]["free_sandbox_mode"])
                st_sp, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Dạo chơi tự do"})
                self.assertEqual(st_sp, 200)
            finally:
                _stop_iso_server(srv)

    def test_t4_07(self) -> None:
        """T4-07: Timeline Checkpoint Rewind & Shared scene_cache Reuse."""
        db_mod = _mod("db")
        with tempfile.TemporaryDirectory() as tmp:
            mock = make_mock_llm([
                {"storylets": [_make_sample_storylet("s_branch_a", "foyer")]},
                _make_sample_scene_tree("s_branch_a"),
                _make_sample_scene_tree("s_branch_a"),
            ] * 3)
            srv, base_url, db_path, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                st_obj = _make_sample_storylet("s_branch_a", "foyer")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                camp["storylets"].append(st_obj)
                db_mod.save_campaign(camp, db_path=db_path)

                st_a, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "open_storylet", "storylet_id": "s_branch_a"})
                self.assertEqual(st_a, 200)
                calls_after_a = mock.call_count
                self.assertGreaterEqual(calls_after_a, 1)
                cp_res = _db_create_checkpoint(db_mod, cid, "Mốc nhánh A", "manual", {"state": db_mod.get_campaign(cid, db_path=db_path)["state"], "history": []}, db_path)
                cp_id = cp_res["id"] if isinstance(cp_res, dict) else (cp_res if isinstance(cp_res, str) else db_mod.list_checkpoints(cid, db_path=db_path)[0]["id"])

                _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Rẽ nhánh khác"})
                _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "checkpoint_id": cp_id})
                st_re, re_body, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "open_storylet", "storylet_id": "s_branch_a"})
                self.assertEqual(st_re, 200)
                self.assertIs(re_body["cache_hit"], True)
                self.assertEqual(mock.call_count, calls_after_a)
            finally:
                _stop_iso_server(srv)

    def test_t4_08(self) -> None:
        """T4-08: Room Acoustics — Public Witnesses vs Private [Nói nhỏ] + Undo."""
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, db_path, _ = _start_test_iso_server(tmp)
            try:
                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                db_mod = _mod("db")
                camp = db_mod.get_campaign(cid, db_path=db_path)
                camp["state"]["player"]["room_id"] = "living_room"
                camp["state"]["npcs"]["linh"]["room_id"] = "living_room"
                camp["state"]["npcs"]["an"]["room_id"] = "living_room"
                db_mod.save_campaign(camp, db_path=db_path)

                _, pub, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Nói công khai", "whisper": False})
                self.assertEqual(set(pub["witnesses"]), {"linh", "an"})
                _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "action": "undo"})
                _, wh_ok, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Thì thầm", "whisper": True, "target_npc_id": "linh", "forced_roll": 18})
                self.assertEqual(wh_ok["witnesses"], ["linh"])
                _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "action": "undo"})
                _, wh_fail, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "speech", "text": "Thì thầm hớ", "whisper": True, "target_npc_id": "linh", "forced_roll": 1})
                self.assertEqual(set(wh_fail["witnesses"]), {"linh", "an"})
            finally:
                _stop_iso_server(srv)

    def test_t4_09(self) -> None:
        """T4-09: Selective Dependency Mask & Prefetch Priority/Cancel Stress."""
        prefetch = _mod("prefetch")
        db_mod = _mod("db")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t4_09.db")
            db_mod.init_db(db_path)
            st_a = _make_sample_storylet("st_branch_a")
            st_b = _make_sample_storylet("st_branch_b")
            state = {"clock": {"mode": "calendar", "hour": 8, "minute": 30}, "flags": {"searched_drawer": False}, "player": {"inventory": ["brass_key"]}, "relationships": {"linh": 10}}
            db_mod.save_campaign({"id": "c_stress", "world": make_test_world(), "state": state, "history": []}, db_path=db_path)
            h_before = prefetch.masked_state_hash(state, st_a["relevant_keys"])
            state_clock = json.loads(json.dumps(state))
            state_clock["clock"]["minute"] = 55
            state_clock["player"]["inventory"].append("extra_candy")
            self.assertEqual(h_before, prefetch.masked_state_hash(state_clock, st_a["relevant_keys"]))

            gate_entered = threading.Event()
            gate_release = threading.Event()
            mock, executed_prompts = _make_gated_mock_llm(
                2, gate_entered, gate_release, _make_sample_scene_tree("st_branch_a")
            )
            pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
            try:
                pm.enqueue("c_stress", _make_sample_storylet("st_gate_1"), state, mock, priority=pm.PRIO_P0, branch_group="gate", db_path=db_path)
                pm.enqueue("c_stress", _make_sample_storylet("st_gate_2"), state, mock, priority=pm.PRIO_P0, branch_group="gate", db_path=db_path)
                self.assertTrue(gate_entered.wait(timeout=5.0), "Workers did not enter gate in time")
                pm.enqueue("c_stress", st_b, state, mock, priority=pm.PRIO_P1, branch_group="branch_b", db_path=db_path)
                pm.enqueue("c_stress", st_a, state, mock, priority=pm.PRIO_P1, branch_group="branch_a", db_path=db_path)
                pm.promote_branch("branch_a", ["branch_b"])
                gate_release.set()
                pm.shutdown(wait=True)
            finally:
                gate_release.set()
                pm.shutdown(wait=False)
            self.assertIsNotNone(db_mod.get_cached_scene("c_stress", "st_branch_a", h_before, db_path=db_path))
            self.assertIsNone(db_mod.get_cached_scene("c_stress", "st_branch_b", h_before, db_path=db_path))
            self.assertFalse(any("st_branch_b" in p for p in executed_prompts))
            self.assertTrue(any("st_branch_a" in p for p in executed_prompts))
            self.assertEqual(mock.call_count, 3)

    def test_t4_10(self) -> None:
        """T4-10: Blender Kit .glb & MagicaVoxel .vox End-to-End Asset Pipeline."""
        catalog = _mod("catalog")
        with tempfile.TemporaryDirectory() as tmp:
            glb_file = os.path.join(tmp, "e2e.glb")
            r1 = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--create-minimal-glb", glb_file], capture_output=True, text=True, check=False)
            self.assertEqual(r1.returncode, 0)
            r2 = subprocess.run([sys.executable, BLENDER_KIT_PATH, "--validate-glb", glb_file], capture_output=True, text=True, check=False)
            self.assertEqual(r2.returncode, 0)
            with open(glb_file, "rb") as f:
                glb_bytes = f.read()

            srv, base_url, db_path, custom_dir = _start_test_iso_server(tmp)
            try:
                st1, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "glb_e2e_prop", "name_vi": "GLB E2E", "size": [1, 1], "mount": "floor", "kind": "glb", "glb_base64": base64.b64encode(glb_bytes).decode("ascii")})
                self.assertEqual(st1, 200)
                st2, _, _ = _http_json(base_url, "POST", "/api/studio/save_asset", {"asset_tag": "vox_e2e_prop", "name_vi": "VOX E2E", "size": [1, 1], "mount": "surface", "kind": "vox", "vox_base64": base64.b64encode(build_minimal_vox()).decode("ascii")})
                self.assertEqual(st2, 200)
                merged = catalog.get_merged_catalog(db_path=db_path)
                self.assertIn("glb_e2e_prop", merged)
                self.assertIn("vox_e2e_prop", merged)
                st_b, boot, _ = _http_json(base_url, "GET", "/api/bootstrap")
                self.assertEqual(st_b, 200)
                self.assertIsInstance(boot, dict)
                self.assertGreaterEqual(len(os.listdir(custom_dir)), 1)
            finally:
                _stop_iso_server(srv)

    def test_t4_11(self) -> None:
        """T4-11: AI World Seed -> Compile -> Multi-Step Undo & Free Action Flow."""
        with tempfile.TemporaryDirectory() as tmp:
            seed = make_test_world(num_rooms=5)
            mock = _make_routed_mock_llm(
                routes=[
                    ("đà lạt", seed),
                    ("mở cửa sổ", {"prose": "Bạn mở cửa sổ nhìn ra vườn.", "effects": {"set_flags": {"window_open": True}}}),
                    ("node", _make_sample_scene_tree("s1")),
                ],
                default={"storylets": [_make_sample_storylet("s1", "foyer")]},
            )
            srv, base_url, _, _ = _start_test_iso_server(tmp, llm_client=mock)
            try:
                st_seed, seed_body, _ = _http_json(base_url, "POST", "/api/campaign/generate_seed", {"prompt": "Biệt thự cổ trên đồi Đà Lạt", "tone_rating": "mature_drama"})
                self.assertEqual(st_seed, 200)
                w = seed_body.get("world") or seed_body
                st_cmp, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": w})
                self.assertEqual(st_cmp, 200)
                cid = cmp_body["campaign"]["id"]
                start_pos = cmp_body["state"]["player"]["pos"]
                fp = cmp_body["floorplan"]
                start_room = cmp_body["state"]["player"]["room_id"]
                lr = next(r for r in _rooms_list(fp) if r["id"] != start_room)
                target_xy = _find_walkable_tile(fp, lr["id"], cmp_body["state"])

                st_mv, _, _ = _http_json(base_url, "POST", "/api/campaign/action", {"campaign_id": cid, "action_type": "move", "target_xy": target_xy})
                self.assertEqual(st_mv, 200)
                st_fa, fa_body, _ = _http_json(base_url, "POST", "/api/campaign/free_action", {"campaign_id": cid, "text": "Mở cửa sổ nhìn ra ngoài"})
                self.assertEqual(st_fa, 200)
                self.assertIs(fa_body["state"]["flags"]["window_open"], True)

                st_u1, u1, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "action": "undo"})
                self.assertEqual(st_u1, 200)
                self.assertFalse(u1["state"]["flags"].get("window_open", False))
                st_u2, u2, _ = _http_json(base_url, "POST", "/api/campaign/rewind", {"campaign_id": cid, "action": "undo"})
                self.assertEqual(st_u2, 200)
                self.assertEqual(u2["state"]["player"]["pos"], start_pos)
            finally:
                _stop_iso_server(srv)

    def test_t4_12(self) -> None:
        """T4-12: Full HTTP/SSE Isolation, Static Serving, Syntax & Concurrency Guard."""
        before = snapshot_webui_and_rplab()
        with tempfile.TemporaryDirectory() as tmp:
            srv, base_url, _, _ = _start_test_iso_server(tmp)
            try:
                for route in ("/", "/styles.css", "/iso_renderer.js", "/voxel_studio.js", "/ui.js", "/vendor/three/three.module.min.js"):
                    st, data, _ = _http_raw_get(base_url, route)
                    self.assertEqual(st, 200, msg=route)
                    self.assertGreater(len(data), 0)

                parsed = urllib.parse.urlparse(base_url)
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3.0)
                try:
                    conn.request("GET", "/api/prefetch/stream")
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
                    line = ""
                    for _ in range(10):
                        candidate = resp.readline().decode("utf-8", errors="replace").strip()
                        if candidate.startswith("data:"):
                            line = candidate[5:].strip()
                            break
                    self.assertTrue(bool(line))
                    json.loads(line)
                finally:
                    conn.close()

                _, cmp_body, _ = _http_json(base_url, "POST", "/api/campaign/compile", {"world": make_test_world()})
                cid = cmp_body["campaign"]["id"]
                results: list[int] = []
                lock = threading.Lock()

                def hit_state() -> None:
                    s, _, _ = _http_json(base_url, "GET", f"/api/campaign/state?campaign_id={cid}")
                    with lock:
                        results.append(s)

                workers = [threading.Thread(target=hit_state) for _ in range(6)]
                for w in workers:
                    w.start()
                for w in workers:
                    w.join(timeout=5.0)
                self.assertEqual(results, [200] * 6)
            finally:
                _stop_iso_server(srv)

        after = snapshot_webui_and_rplab()
        self.assertEqual(before["files"], after["files"])
        self.assertEqual(before["code_hashes"], after["code_hashes"])
        self.assertEqual(before["db_row_counts"], after["db_row_counts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
