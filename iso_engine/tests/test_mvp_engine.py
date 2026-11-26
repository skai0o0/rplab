"""Core automated MVP acceptance suite for iso_engine (tests/test_mvp_engine.py).

Uses Python 3.11 stdlib unittest exclusively (no pytest), isolated TemporaryDirectory
SQLite WAL databases, and MockLLMClient (zero live LLM API calls).
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import unittest


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ISO_ENGINE_ROOT = os.path.dirname(TESTS_DIR)


def _ensure_iso_root_first() -> None:
    norm_root = os.path.normcase(os.path.abspath(ISO_ENGINE_ROOT))
    if not sys.path or os.path.normcase(os.path.abspath(sys.path[0])) != norm_root:
        sys.path[:] = [
            p for p in sys.path if os.path.normcase(os.path.abspath(p)) != norm_root
        ]
        sys.path.insert(0, ISO_ENGINE_ROOT)


_ensure_iso_root_first()

RP_ROOT = os.path.abspath(os.path.join(ISO_ENGINE_ROOT, os.pardir))
SAMPLES_DIR = os.path.join(RP_ROOT, "samples")
CHARS_DIR = os.path.join(SAMPLES_DIR, "chars")
BLENDER_KIT_PATH = os.path.join(ISO_ENGINE_ROOT, "tools", "blender_export_kit.py")


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_location_alias_map(world_obj: dict) -> dict:
    return (
        world_obj.get("location_alias_map")
        or world_obj.get("meta", {}).get("location_alias_map")
        or {}
    )


def _pick_catalog_tag(
    catalog_dict: dict,
    *,
    category: str | None = None,
    pose_slot: str | None = None,
    fallback: str = "mystery_placeholder",
) -> str:
    if not catalog_dict:
        return fallback
    for tag, spec in catalog_dict.items():
        if pose_slot and pose_slot in (spec.get("pose_slots") or {}):
            return tag
    if category:
        for tag, spec in catalog_dict.items():
            if spec.get("category") == category and not spec.get("walkable", False):
                return tag
        for tag, spec in catalog_dict.items():
            if spec.get("category") == category:
                return tag
    return fallback if fallback in catalog_dict else next(iter(catalog_dict))


def _build_6room_mansion_world(catalog_dict: dict | None = None) -> dict:
    """Build a canonical 6-room iso-world-v1 fixture conforming to schemas.validate_iso_world."""
    cat = catalog_dict or {}
    wardrobe_tag = _pick_catalog_tag(cat, category="bedroom", pose_slot="hide_inside", fallback="wardrobe_tall_2x1")
    bed_tag = _pick_catalog_tag(cat, category="bedroom", pose_slot="lie", fallback="bed_double_2x2")
    sofa_tag = _pick_catalog_tag(cat, category="living", pose_slot="sit", fallback="sofa_velvet_2x1")
    kitchen_tag = _pick_catalog_tag(cat, category="kitchen", fallback="dining_table_2x1")
    bath_tag = _pick_catalog_tag(cat, category="bathroom", fallback="sink_marble_1x1")
    desk_tag = _pick_catalog_tag(cat, category="study", fallback="desk_mahogany_2x1")

    def _make_char(cid: str, name: str, gender: str, schedule: list[dict]) -> dict:
        return {
            "format": "iso-char-v1",
            "id": cid,
            "meta": {"name": name, "aliases": [name.split()[-1]], "age": 20, "gender": gender, "fictional": True},
            "core": {
                "one_liner": f"Nhân vật {name} trong Biệt thự Mưa Ngâu.",
                "personality": ["Kín đáo", "Quan sát tốt"],
                "values": "Giữ bí mật.",
                "boundaries": [],
            },
            "voice": {
                "dialect": "Nam Bộ",
                "tone": "Tự nhiên.",
                "pronoun_defaults": {
                    "close_peer": {"self": "tui", "call": "ông/bà"},
                    "acquaintance": {"self": "mình", "call": "bạn"},
                    "elder_formal": {"self": "em", "call": "anh/chị"},
                    "angry": {"self": "tôi", "call": "ông/bà"},
                },
                "modes": {"in_person": {"style": "Trực tiếp", "tics": [], "samples": []}, "texting": {"style": "Ngắn", "samples": []}},
            },
            "stats": {"charm": 12, "logic": 12, "stealth": 12, "will": 12},
            "voxel_look": {
                "skin": "#f1c27d",
                "hair": "#1c1917",
                "hair_style": "short",
                "outfit_top": "#2563eb",
                "outfit_bottom": "#1e293b",
                "accent": "#fb923c",
                "height_scale": 1.0,
            },
            "schedule": schedule,
            "inventory": [],
            "secrets": [],
            "appearance": {"summary": name, "outfits": {"default": "Thường phục"}},
            "modules": [],
        }

    characters = {
        "vy": _make_char(
            "vy",
            "Lê Phương Vy",
            "nữ",
            [
                {"start": "19:00", "end": "21:00", "room_id": "living_room", "activity": "Đọc tin nhắn", "pose": "sit", "target_prop": "prop_sofa_living"},
                {"start": "21:00", "end": "23:30", "room_id": "bedroom_vy", "activity": "Nghỉ ngơi", "pose": "lie", "target_prop": "prop_bed_vy"},
            ],
        ),
        "han": _make_char(
            "han",
            "Trịnh Gia Hân",
            "nữ",
            [
                {"start": "19:00", "end": "22:00", "room_id": "kitchen", "activity": "Pha trà", "pose": "stand", "target_prop": "prop_kitchen_1"},
            ],
        ),
        "tuan": _make_char(
            "tuan",
            "Phạm Minh Tuấn",
            "nam",
            [
                {"start": "19:00", "end": "22:00", "room_id": "kitchen", "activity": "Tranh luận nhỏ", "pose": "stand", "target_prop": "prop_kitchen_1"},
            ],
        ),
    }

    return {
        "format": "iso-world-v1",
        "id": "biet_thu_mua_ngau_test",
        "meta": {
            "name": "Biệt thự Mưa Ngâu",
            "genre": "Bí ẩn tâm lý",
            "tone_rating": "mature_drama",
        },
        "rules": {"writing_style": "Tự nhiên.", "world_rules": ["Mọi nhân vật đều hư cấu."]},
        "user": {"id": "user", "name": "Minh", "aliases": ["Minh"], "gender": "nam", "persona": "Khách mời"},
        "player_defaults": {
            "energy": 100,
            "stress": 10,
            "cash": 500000,
            "stats": {"charm": 12, "logic": 13, "stealth": 14, "will": 11},
        },
        "clock": {
            "mode": "calendar",
            "start": {"year": 2027, "month": 9, "day": 15, "hour": 20, "minute": 0},
        },
        "rooms": {
            "foyer": {
                "id": "foyer",
                "name": "Sảnh chính",
                "kind": "foyer",
                "privacy": "public",
                "owner_id": None,
                "size": [5, 5],
                "props": [{"id": "prop_foyer_1", "asset_tag": sofa_tag, "name": "Ghế chờ sảnh"}],
            },
            "living_room": {
                "id": "living_room",
                "name": "Phòng khách",
                "kind": "living",
                "privacy": "public",
                "owner_id": None,
                "size": [6, 5],
                "props": [{"id": "prop_sofa_living", "asset_tag": sofa_tag, "name": "Ghế sofa lớn"}],
            },
            "kitchen": {
                "id": "kitchen",
                "name": "Phòng bếp",
                "kind": "kitchen",
                "privacy": "public",
                "owner_id": None,
                "size": [5, 5],
                "props": [{"id": "prop_kitchen_1", "asset_tag": kitchen_tag, "name": "Bàn bếp"}],
            },
            "bedroom_vy": {
                "id": "bedroom_vy",
                "name": "Phòng ngủ Vy",
                "kind": "bedroom",
                "privacy": "private",
                "owner_id": "vy",
                "size": [5, 5],
                "props": [
                    {"id": "prop_wardrobe_vy", "asset_tag": wardrobe_tag, "name": "Tủ quần áo lớn"},
                    {"id": "prop_bed_vy", "asset_tag": bed_tag, "name": "Giường ngủ"},
                ],
            },
            "bathroom": {
                "id": "bathroom",
                "name": "Phòng tắm",
                "kind": "bathroom",
                "privacy": "public",
                "owner_id": None,
                "size": [4, 4],
                "props": [{"id": "prop_bath_1", "asset_tag": bath_tag, "name": "Bồn sứ"}],
            },
            "study_locked": {
                "id": "study_locked",
                "name": "Phòng làm việc khóa kín",
                "kind": "study",
                "privacy": "private",
                "owner_id": "han",
                "size": [5, 5],
                "props": [
                    {"id": "prop_desk_study", "asset_tag": desk_tag, "name": "Bàn làm việc gỗ"},
                    {"id": "prop_mystery_01", "asset_tag": "prop_mystery_01", "name": "Vật phẩm bí ẩn", "is_missing": True},
                ],
            },
        },
        "doors": [
            {"id": "door_foyer_living", "room_a": "foyer", "room_b": "living_room", "locked": False},
            {"id": "door_living_kitchen", "room_a": "living_room", "room_b": "kitchen", "locked": False},
            {"id": "door_foyer_bed_vy", "room_a": "foyer", "room_b": "bedroom_vy", "locked": False},
            {"id": "door_living_bath", "room_a": "living_room", "room_b": "bathroom", "locked": False},
            {
                "id": "door_study",
                "room_a": "living_room",
                "room_b": "study_locked",
                "locked": True,
                "key_item_id": "study_key",
                "lock_dc": 15,
            },
        ],
        "acts_spine": [
            {
                "act_index": 1,
                "title": "Hồi 1: Cơn mưa bất chợt",
                "goal": "Tìm hiểu căn biệt thự và manh mối chiếc chìa khóa.",
                "advance_condition": {"flags": {"act1_complete": True}},
                "climax_hint": "Tìm thấy chìa khóa.",
            },
            {
                "act_index": 2,
                "title": "Hồi 2: Bóng tối sau cánh cửa",
                "goal": "Mở khóa phòng làm việc và đối chất bí mật.",
                "advance_condition": {"flags": {"act2_complete": True}},
                "climax_hint": "Phát hiện hộp nhẫn.",
            },
            {
                "act_index": 3,
                "title": "Hồi 3: Sự thật lúc nửa đêm",
                "goal": "Quyết định kết cục của mọi nhân vật.",
                "advance_condition": {"flags": {"act3_complete": True}},
                "climax_hint": "Đối chất cuối cùng.",
            },
        ],
        "endings": [
            {
                "id": "ending_truth_revealed",
                "title": "Sự thật phơi bày",
                "condition": {"flags": {"truth_revealed": True}},
                "epilogue_prompt": "Mọi bí mật trong biệt thự đã được làm sáng tỏ.",
            },
            {
                "id": "ending_quiet_departure",
                "title": "Lặng lẽ rời đi",
                "condition": {"flags": {"quiet_leave": True}},
                "epilogue_prompt": "Bạn rời khỏi biệt thự trước bình minh.",
            },
        ],
        "missing_props": [
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
                    "room_id": "study_locked",
                    "owner_id": "khanh",
                    "revealed_flag": "found_ring_box",
                },
            }
        ],
        "characters": characters,
        "cast": [
            {"char_id": "vy", "role_in_world": "Bạn thân", "start_room": "living_room", "start_outfit": "default", "goal": "Giữ bí mật", "secrets": []},
            {"char_id": "han", "role_in_world": "Lớp trưởng", "start_room": "kitchen", "start_outfit": "default", "goal": "Pha trà", "secrets": []},
            {"char_id": "tuan", "role_in_world": "Bạn cùng phòng", "start_room": "kitchen", "start_outfit": "default", "goal": "Thử nghiệm", "secrets": []},
        ],
        "relationships": {"auto_bootstrap": True, "edges": {}},
        "initial_facts": [],
        "lore": [],
        "opening": {
            "user_start_room": "foyer",
            "tension": 1,
            "channel_type": "in_person",
            "narration": "Mưa bắt đầu nặng hạt bên ngoài hiên biệt thự.",
        },
    }


def _room_center(fp: dict, room_id: str) -> list[int]:
    rooms = fp.get("rooms", {})
    room = rooms[room_id] if isinstance(rooms, dict) else next(r for r in rooms if r["id"] == room_id)
    if "x" in room and "y" in room and "w" in room and "h" in room:
        cx = int(room["x"]) + int(room["w"]) // 2
        cy = int(room["y"]) + int(room["h"]) // 2
        return [cx, cy]
    bounds = room.get("bounds") or [0, 0, 2, 2]
    return [int(bounds[0]) + int(bounds[2]) // 2, int(bounds[1]) + int(bounds[3]) // 2]


def _find_walkable_tile_in_room(fp: dict, room_id: str, state: dict) -> list[int]:
    import engine

    rooms = fp.get("rooms", {})
    room = rooms[room_id] if isinstance(rooms, dict) else next(r for r in rooms if r["id"] == room_id)
    rx = int(room.get("x", 0))
    ry = int(room.get("y", 0))
    rw = int(room.get("w", room.get("width", 4)))
    rh = int(room.get("h", room.get("height", 4)))
    center = _room_center(fp, room_id)
    candidates = [center]
    for y in range(ry, ry + rh):
        for x in range(rx, rx + rw):
            if [x, y] != center:
                candidates.append([x, y])
    for cand in candidates:
        path = engine.find_path(fp, cand, cand, state)
        if path is not None:
            return cand
    return center


def _rooms_rect_list(fp: dict) -> list[tuple[str, int, int, int, int]]:
    rooms = fp.get("rooms", {})
    items = list(rooms.values()) if isinstance(rooms, dict) else list(rooms)
    rects = []
    for r in items:
        rid = r.get("id", "")
        x = int(r["x"])
        y = int(r["y"])
        w = int(r.get("w", r.get("width", 0)))
        h = int(r.get("h", r.get("height", 0)))
        rects.append((rid, x, y, w, h))
    return rects


def _props_list(fp: dict) -> list[dict]:
    props = fp.get("props", [])
    if isinstance(props, dict):
        return list(props.values())
    return list(props)


def _doors_list(fp: dict) -> list[dict]:
    doors = fp.get("doors", [])
    if isinstance(doors, dict):
        return list(doors.values())
    return list(doors)


def _make_canonical_state(
    *,
    hour: int = 20,
    minute: int = 0,
    act_index: int = 1,
    room_id: str = "foyer",
    pos: list[int] | None = None,
    pose: str = "stand",
    target_prop: str | None = None,
    energy: int = 100,
    stress: int = 10,
    cash: int = 500000,
    stats: dict[str, int] | None = None,
    inventory: list[str] | None = None,
    npcs: dict[str, dict] | None = None,
    doors_unlocked: list[str] | None = None,
    flags: dict | None = None,
    discovered_items: list[str] | None = None,
    relationships: dict[str, int] | None = None,
    traces: list[dict] | None = None,
    pending_intrusion: dict | None = None,
    ending: dict | str | None = None,
    free_sandbox_mode: bool = False,
) -> dict:
    """Build a canonical nested WorldState matching TEST_INFRA.md § Canonical Runtime Schema Lock."""
    return {
        "clock": {
            "mode": "calendar",
            "year": 2027,
            "month": 9,
            "day": 15,
            "hour": hour,
            "minute": minute,
        },
        "act_index": act_index,
        "player": {
            "room_id": room_id,
            "pos": list(pos) if pos is not None else [0, 0],
            "pose": pose,
            "target_prop": target_prop,
            "energy": energy,
            "stress": stress,
            "cash": cash,
            "stats": dict(stats or {"charm": 12, "logic": 13, "stealth": 14, "will": 11}),
            "inventory": list(inventory or []),
        },
        "npcs": copy.deepcopy(npcs) if npcs is not None else {},
        "doors_unlocked": list(doors_unlocked or []),
        "flags": dict(flags or {}),
        "discovered_items": list(discovered_items or []),
        "relationships": dict(relationships or {}),
        "traces": list(traces or []),
        "pending_intrusion": copy.deepcopy(pending_intrusion),
        "ending": copy.deepcopy(ending),
        "free_sandbox_mode": free_sandbox_mode,
    }


def _build_minimal_glb_bytes(json_obj: dict | None = None) -> bytes:
    payload = json_obj if json_obj is not None else {"asset": {"version": "2.0"}, "scenes": [{"nodes": []}], "nodes": []}
    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    pad_len = (4 - (len(raw_json) % 4)) % 4
    padded_json = raw_json + (b" " * pad_len)
    total_len = 12 + 8 + len(padded_json)
    header = struct.pack("<III", 0x46546C67, 2, total_len)
    chunk0 = struct.pack("<II", len(padded_json), 0x4E4F534A) + padded_json
    return header + chunk0


class TestMVPEngine(unittest.TestCase):
    """11 core acceptance tests covering schemas, catalog, runtime, prefetch, and GLB."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.db_path = os.path.join(self.tmp_dir, "test_iso_engine.db")
        self.custom_dir = os.path.join(self.tmp_dir, "custom_assets")
        os.makedirs(self.custom_dir, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_01_upgrade_legacy_v2(self) -> None:
        import schemas

        # 1) Upgrade all 4 sample char-v2 cards (vy, han, khanh, tuan)
        for char_name in ("vy", "han", "khanh", "tuan"):
            raw_char = _load_json(os.path.join(CHARS_DIR, f"{char_name}.json"))
            iso_char = schemas.upgrade_char_v2_to_iso(raw_char)
            validated = schemas.validate_iso_char(iso_char)

            self.assertEqual(validated["format"], "iso-char-v1")
            self.assertRegex(validated["id"], r"^[a-z0-9_]+$")
            self.assertTrue(validated["meta"]["fictional"] is True)
            for stat_key in ("charm", "logic", "stealth", "will"):
                self.assertIn(stat_key, validated["stats"])
                self.assertIsInstance(validated["stats"][stat_key], (int, float))
            self.assertIsInstance(validated["voxel_look"], dict)
            hex_values = [
                v for v in validated["voxel_look"].values()
                if isinstance(v, str) and re.match(r"^#[0-9a-fA-F]{6}$", v)
            ]
            self.assertGreaterEqual(len(hex_values), 1, f"{char_name} voxel_look lacks hex colors")
            self.assertIsInstance(validated["schedule"], list)
            pronouns = validated.get("pronoun_defaults") or validated.get("voice", {}).get("pronoun_defaults")
            self.assertIsInstance(pronouns, dict)
            self.assertIn("close_peer", pronouns)

            # Determinism & pass-through idempotency
            iso_char_repeat = schemas.upgrade_char_v2_to_iso(raw_char)
            self.assertEqual(validated["stats"], iso_char_repeat["stats"])
            self.assertEqual(validated["voxel_look"], iso_char_repeat["voxel_look"])
            passthru = schemas.upgrade_char_v2_to_iso(validated)
            self.assertEqual(passthru["id"], validated["id"])

        # 2) Upgrade dai_hoc_ben_nghe_world.json (20 locations -> 4..8 rooms)
        raw_world = _load_json(os.path.join(SAMPLES_DIR, "dai_hoc_ben_nghe_world.json"))
        original_loc_ids = set(raw_world["locations"].keys())
        self.assertEqual(len(original_loc_ids), 20)

        iso_world = schemas.upgrade_world_v2_to_iso(raw_world)
        validated_world = schemas.validate_iso_world(iso_world)
        self.assertEqual(validated_world["format"], "iso-world-v1")
        rooms = validated_world["rooms"]
        self.assertGreaterEqual(len(rooms), 4)
        self.assertLessEqual(len(rooms), 8)
        self.assertGreaterEqual(len(validated_world["doors"]), len(rooms) - 1)
        self.assertEqual(len(validated_world["acts_spine"]), 3)
        self.assertGreaterEqual(len(validated_world["endings"]), 1)
        self.assertIsInstance(validated_world["missing_props"], list)

        room_ids = set(rooms.keys()) if isinstance(rooms, dict) else {r["id"] for r in rooms}
        alias_map = _extract_location_alias_map(validated_world)
        self.assertTrue(alias_map, "location_alias_map must be populated")
        for loc_id in original_loc_ids:
            self.assertIn(loc_id, alias_map, f"Missing original location {loc_id} in location_alias_map")
            self.assertIn(alias_map[loc_id], room_ids, f"Alias {loc_id} -> {alias_map[loc_id]} not in rooms")

        # 3) External search_dirs resolution (world-v2 without embedded characters)
        external_world_v2 = {
            "format": "world-v2",
            "id": "external_cast_test",
            "meta": {"name": "Test Campus", "genre": "Slice of Life"},
            "user": {"id": "user", "name": "Tester", "gender": "nam"},
            "clock": {"mode": "calendar", "start": {"year": 2027, "month": 9, "day": 15, "hour": 9, "minute": 0}},
            "locations": {
                "san_truong": {"name": "Sân trường", "parent": None, "tags": ["public"]},
                "thu_vien": {"name": "Thư viện", "parent": "san_truong", "tags": ["indoor"]},
                "can_tin": {"name": "Căn tin", "parent": "san_truong", "tags": ["public"]},
                "phong_clb": {"name": "Phòng CLB", "parent": "san_truong", "tags": ["indoor"]},
            },
            "cast": [
                {"char_id": "vy", "start_location": "san_truong", "role_in_world": "Bí thư"},
                {"char_id": "han", "start_location": "thu_vien", "role_in_world": "Lớp trưởng"},
            ],
            "opening": {"user_start_location": "san_truong", "narration": "Bắt đầu ngày mới."},
        }
        upgraded_ext = schemas.upgrade_world_v2_to_iso(external_world_v2, search_dirs=[CHARS_DIR])
        validated_ext = schemas.validate_iso_world(upgraded_ext)
        ext_cast_ids = {c.get("id") or c.get("char_id") for c in validated_ext.get("cast", [])}
        self.assertIn("vy", ext_cast_ids)
        self.assertIn("han", ext_cast_ids)

        # 4) Negative SchemaError checks
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_iso_char({"format": "char-v2", "id": "vy"})
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_iso_char({"format": "iso-char-v1", "id": "Invalid ID!", "meta": {"fictional": True}})
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_iso_world({"format": "iso-world-v1", "id": "bad", "rooms": {}})

    def test_02_floorplan_packing_and_astar(self) -> None:
        import catalog
        import compiler
        import engine
        import schemas

        starter = catalog.load_starter_catalog()
        self.assertGreaterEqual(len(starter), 26, "starter_catalog.json must have >= 26 prefabs")

        all_pose_slots = set()
        for spec in starter.values():
            all_pose_slots.update((spec.get("pose_slots") or {}).keys())
        for required_pose in ("sit", "lie", "hide_inside"):
            self.assertIn(required_pose, all_pose_slots, f"Missing pose_slot {required_pose} in catalog")

        mansion = _build_6room_mansion_world(starter)
        raw_bn = _load_json(os.path.join(SAMPLES_DIR, "dai_hoc_ben_nghe_world.json"))
        iso_bn = schemas.upgrade_world_v2_to_iso(raw_bn)

        for label, world_obj in (("6room_mansion", mansion), ("dai_hoc_ben_nghe", iso_bn)):
            fp = compiler.pack_floorplan(world_obj, starter)
            width = int(fp["width"])
            height = int(fp["height"])
            self.assertLessEqual(width, 24, f"{label} width > 24")
            self.assertLessEqual(height, 24, f"{label} height > 24")

            rects = _rooms_rect_list(fp)
            self.assertEqual(len(rects), len(world_obj["rooms"]))
            for rid, x, y, w, h in rects:
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, width)
                self.assertLessEqual(y + h, height)
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)

            for i in range(len(rects)):
                id_a, ax, ay, aw, ah = rects[i]
                for j in range(i + 1, len(rects)):
                    id_b, bx, by, bw, bh = rects[j]
                    overlap_x = ax < bx + bw and ax + aw > bx
                    overlap_y = ay < by + bh and ay + ah > by
                    self.assertFalse(
                        overlap_x and overlap_y,
                        f"{label}: rooms {id_a} and {id_b} overlap",
                    )

            door_tiles = set()
            for d in _doors_list(fp):
                if "xy" in d:
                    door_tiles.add((int(d["xy"][0]), int(d["xy"][1])))
                elif "x" in d and "y" in d:
                    door_tiles.add((int(d["x"]), int(d["y"])))

            occupied_nonwalkable = set()
            for p in _props_list(fp):
                px = int(p.get("x", 0))
                py = int(p.get("y", 0))
                pw, ph = p.get("size", [1, 1])
                walkable = bool(p.get("walkable", False))
                prop_tiles = {(px + dx, py + dy) for dx in range(int(pw)) for dy in range(int(ph))}
                self.assertTrue(
                    prop_tiles.isdisjoint(door_tiles),
                    f"{label}: prop {p.get('id')} overlaps door tile",
                )
                if not walkable:
                    self.assertTrue(
                        prop_tiles.isdisjoint(occupied_nonwalkable),
                        f"{label}: prop {p.get('id')} overlaps another prop",
                    )
                    occupied_nonwalkable.update(prop_tiles)

            adj = fp.get("adjacent_rooms", {})
            self.assertTrue(adj, f"{label}: adjacent_rooms must not be empty")
            for r_a, neighbors in adj.items():
                for r_b in neighbors:
                    self.assertIn(r_a, adj.get(r_b, []), f"{label}: asymmetric adjacency {r_a}<->{r_b}")

        # A* pathfinding on 6-room mansion with locked study door
        fp_mansion = compiler.pack_floorplan(mansion, starter)
        self.assertIsInstance(fp_mansion["rooms"], dict)
        locked_state = _make_canonical_state(
            room_id="foyer",
            doors_unlocked=[],
            flags={"study_unlocked": False},
        )
        unlocked_state = _make_canonical_state(
            room_id="foyer",
            doors_unlocked=["door_study"],
            flags={"study_unlocked": True},
        )

        foyer_xy = _find_walkable_tile_in_room(fp_mansion, "foyer", locked_state)
        living_xy = _find_walkable_tile_in_room(fp_mansion, "living_room", locked_state)
        study_xy = _find_walkable_tile_in_room(fp_mansion, "study_locked", unlocked_state)

        path_open = engine.find_path(fp_mansion, foyer_xy, living_xy, locked_state)
        self.assertIsNotNone(path_open)
        self.assertEqual(path_open[0], foyer_xy)
        self.assertEqual(path_open[-1], living_xy)
        for p0, p1 in zip(path_open, path_open[1:]):
            self.assertEqual(abs(p1[0] - p0[0]) + abs(p1[1] - p0[1]), 1)

        path_blocked = engine.find_path(fp_mansion, foyer_xy, study_xy, locked_state)
        self.assertIsNone(path_blocked, "Locked door must block A* path")

        path_unlocked = engine.find_path(fp_mansion, foyer_xy, study_xy, unlocked_state)
        self.assertIsNotNone(path_unlocked, "Unlocked door must allow A* path")
        self.assertEqual(path_unlocked[-1], study_xy)

        study_props = [p for p in _props_list(fp_mansion) if p.get("room_id") == "study_locked"]
        self.assertTrue(study_props, "Expected at least one packed prop inside study_locked")
        approach_xy = engine.find_approach_tile(fp_mansion, study_props[0], foyer_xy, unlocked_state)
        self.assertIsNotNone(approach_xy)
        self.assertIsNotNone(engine.find_path(fp_mansion, foyer_xy, approach_xy, unlocked_state))

    def test_03_spoiler_masked_missing_assets(self) -> None:
        import catalog
        import db
        import schemas

        db.init_db(db_path=self.db_path)
        world = _build_6room_mansion_world()
        spec = copy.deepcopy(world["missing_props"][0])

        unrevealed = {"flags": {}, "discovered_items": []}
        revealed_by_flag = {"flags": {"found_ring_box": True}, "discovered_items": []}
        revealed_by_item = {"flags": {}, "discovered_items": ["prop_mystery_01"]}

        # Direct redact_missing_prop checks
        redacted = schemas.redact_missing_prop(spec, unrevealed, built=False)
        self.assertFalse(redacted["revealed"])
        self.assertFalse(redacted["built"])
        self.assertEqual(redacted["asset_tag"], "prop_mystery_01")
        self.assertIn("visual_brief", redacted)
        for forbidden_key in ("story_meta", "true_name", "room_id", "owner_id", "revealed_flag"):
            self.assertNotIn(forbidden_key, redacted)
        serialized = json.dumps(redacted, ensure_ascii=False)
        for secret_val in ("Hộp nhẫn đính hôn bị giấu", "study_locked", "khanh", "found_ring_box"):
            self.assertNotIn(secret_val, serialized)

        # Studio missing list before reveal
        missing_list = catalog.get_studio_missing_list(world, unrevealed, db_path=self.db_path)
        self.assertEqual(len(missing_list), 1)
        item = missing_list[0]
        self.assertFalse(item["revealed"])
        self.assertFalse(item["built"])
        for forbidden_key in ("story_meta", "true_name", "room_id", "owner_id", "revealed_flag"):
            self.assertNotIn(forbidden_key, item)
        list_json = json.dumps(missing_list, ensure_ascii=False)
        for secret_val in ("Hộp nhẫn đính hôn bị giấu", "study_locked", "khanh", "found_ring_box"):
            self.assertNotIn(secret_val, list_json)

        # Reveal via flag OR discovered_items
        for rev_state in (revealed_by_flag, revealed_by_item):
            rev_direct = schemas.redact_missing_prop(spec, rev_state, built=False)
            self.assertTrue(rev_direct["revealed"])
            self.assertEqual(rev_direct["true_name"], "Hộp nhẫn đính hôn bị giấu")
            self.assertEqual(rev_direct["room_id"], "study_locked")
            self.assertEqual(rev_direct["owner_id"], "khanh")

            rev_list = catalog.get_studio_missing_list(world, rev_state, db_path=self.db_path)
            self.assertTrue(rev_list[0]["revealed"])
            self.assertEqual(rev_list[0]["true_name"], "Hộp nhẫn đính hôn bị giấu")

        # Save custom asset into isolated TemporaryDirectory + db_path
        saved = catalog.save_studio_asset(
            {
                "asset_tag": "prop_mystery_01",
                "name_vi": "Hộp nhung đỏ",
                "category": "mystery",
                "size": [1, 1],
                "height": 1,
                "mount": "surface",
                "walkable": False,
                "voxels": [
                    {"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1, "color": "#7f1d1d", "shape": "cube", "material": "fabric"}
                ],
            },
            custom_dir=self.custom_dir,
            db_path=self.db_path,
        )
        self.assertEqual(saved["asset_tag"], "prop_mystery_01")

        # Built=True even while still unrevealed, and zero spoiler leak remains
        after_build_unrevealed = catalog.get_studio_missing_list(world, unrevealed, db_path=self.db_path)
        self.assertTrue(after_build_unrevealed[0]["built"])
        self.assertFalse(after_build_unrevealed[0]["revealed"])
        after_json = json.dumps(after_build_unrevealed, ensure_ascii=False)
        for secret_val in ("Hộp nhẫn đính hôn bị giấu", "study_locked", "khanh"):
            self.assertNotIn(secret_val, after_json)

        merged = catalog.get_merged_catalog(db_path=self.db_path)
        self.assertIn("prop_mystery_01", merged)

    def test_04_selective_dependency_mask_cache(self) -> None:
        import db
        import llm_client
        import prefetch

        db.init_db(db_path=self.db_path)
        camp_id = "camp_mask_04"
        db.save_campaign(
            {
                "id": camp_id,
                "title": "Cache Mask Test",
                "act_index": 1,
                "world": _build_6room_mansion_world(),
                "cast": [],
                "floorplan": {},
                "storylets": [],
                "state": {},
                "history": [],
            },
            db_path=self.db_path,
        )

        relevant_keys = {
            "flags": ["desk_searched"],
            "items": ["brass_key"],
            "affinity_with": ["vy"],
            "extra": [],
        }
        state_base = _make_canonical_state(
            hour=8,
            minute=45,
            room_id="study_locked",
            pos=[2, 3],
            flags={"desk_searched": False, "lamp_on": False},
            inventory=["coffee"],
            relationships={"vy": 80, "tuan": 50},
        )
        state_irrelevant = _make_canonical_state(
            hour=8,
            minute=52,
            room_id="study_locked",
            pos=[6, 4],
            pose="sit",
            flags={"desk_searched": False, "lamp_on": True},
            inventory=["coffee", "bread"],
            relationships={"vy": 80, "tuan": 65},
        )
        state_rel_item = copy.deepcopy(state_base)
        state_rel_item["player"]["inventory"].append("brass_key")
        state_rel_flag = copy.deepcopy(state_base)
        state_rel_flag["flags"]["desk_searched"] = True
        state_rel_aff = copy.deepcopy(state_base)
        state_rel_aff["relationships"]["vy"] = 90

        hash_base = prefetch.masked_state_hash(state_base, relevant_keys)
        hash_irr = prefetch.masked_state_hash(state_irrelevant, relevant_keys)
        self.assertEqual(hash_base, hash_irr, "Irrelevant mutations must preserve masked_state_hash")

        for mutated in (state_rel_item, state_rel_flag, state_rel_aff):
            self.assertNotEqual(
                prefetch.masked_state_hash(mutated, relevant_keys),
                hash_base,
                "Relevant key mutation must change masked_state_hash",
            )

        storylet = {
            "id": "st_desk",
            "act_index": 1,
            "title": "Kiểm tra bàn làm việc",
            "trigger": {"room_id": "study_locked", "one_shot": False},
            "relevant_keys": relevant_keys,
            "priority": 50,
            "is_intrusion": False,
            "root_node_id": "n1",
            "nodes": None,
        }
        mock_llm = llm_client.MockLLMClient(
            default_json={
                "storylet_id": "st_desk",
                "root_node_id": "n1",
                "nodes": {
                    "n1": {
                        "node_id": "n1",
                        "text": "Ngăn bàn vẫn còn khóa hờ.",
                        "speaker_id": None,
                        "presentation_mode": "standard",
                        "pose_updates": {},
                        "choices": [{"id": "c1", "label": "Đóng lại", "effects": {}}],
                    }
                },
            }
        )

        # 1) Initial prefetch populates cache (1 LLM call)
        pm1 = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
        try:
            pm1.enqueue(camp_id, storylet, state_base, mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm1.shutdown(wait=True)
        finally:
            pm1.shutdown(wait=False)

        self.assertEqual(mock_llm.call_count, 1)
        cached = db.get_cached_scene(camp_id, "st_desk", hash_irr, db_path=self.db_path)
        self.assertIsNotNone(cached)

        # 2) Enqueue with irrelevant state mutation -> cache hit, 0 new LLM calls
        pm2 = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
        try:
            pm2.enqueue(camp_id, storylet, state_irrelevant, mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm2.shutdown(wait=True)
        finally:
            pm2.shutdown(wait=False)

        self.assertEqual(mock_llm.call_count, 1, "Cache hit on irrelevant mutation must trigger 0 new LLM calls")

        # 3) Cache miss on relevant key mutation -> enqueue triggers regeneration (call_count == 2)
        hash_rel_item = prefetch.masked_state_hash(state_rel_item, relevant_keys)
        self.assertIsNone(db.get_cached_scene(camp_id, "st_desk", hash_rel_item, db_path=self.db_path))
        pm3 = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
        try:
            pm3.enqueue(camp_id, storylet, state_rel_item, mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm3.shutdown(wait=True)
        finally:
            pm3.shutdown(wait=False)

        self.assertEqual(mock_llm.call_count, 2, "Relevant state mutation must trigger LLM regeneration")
        self.assertIsNotNone(db.get_cached_scene(camp_id, "st_desk", hash_rel_item, db_path=self.db_path))

    def test_05_intrusion_warning_and_hide_check(self) -> None:
        import catalog
        import compiler
        import db
        import engine
        import llm_client

        db.init_db(db_path=self.db_path)
        starter = catalog.load_starter_catalog()
        world = _build_6room_mansion_world(starter)
        fp = compiler.pack_floorplan(world, starter)
        mock_llm = llm_client.MockLLMClient()

        intrusion_scene = {
            "room_id": "bedroom_vy",
            "owner_id": "vy",
            "root_node_id": "hide_success",
            "nodes": {
                "hide_success": {
                    "node_id": "hide_success",
                    "text": "Bạn nép gọn vào trong tủ áo ngay khi tay nắm cửa xoay.",
                    "speaker_id": None,
                    "presentation_mode": "standard",
                    "pose_updates": {"user": {"pose": "hide_inside", "target_prop": "prop_wardrobe_vy"}},
                    "choices": [],
                },
                "hide_fail": {
                    "node_id": "hide_fail",
                    "text": "Cánh tủ kẽo kẹt tố cáo chỗ trốn của bạn trước mặt Vy.",
                    "speaker_id": "vy",
                    "presentation_mode": "standard",
                    "pose_updates": {"user": {"pose": "stand", "target_prop": None}},
                    "choices": [],
                },
                "bluff": {
                    "node_id": "bluff",
                    "text": "Bạn bình tĩnh vờ như vừa ghé tìm sạc điện thoại.",
                    "speaker_id": "vy",
                    "presentation_mode": "standard",
                    "pose_updates": {"user": {"pose": "stand", "target_prop": None}},
                    "choices": [],
                },
                "retreat": {
                    "node_id": "retreat",
                    "text": "Bạn kịp lùi ra hành lang trước khi Vy bước vào.",
                    "speaker_id": None,
                    "presentation_mode": "standard",
                    "pose_updates": {"user": {"pose": "stand", "target_prop": None}},
                    "choices": [],
                },
            },
            "branches": {
                "hide_success": {
                    "text": "Bạn nép gọn vào trong tủ áo ngay khi tay nắm cửa xoay.",
                    "pose": "hide_inside",
                },
                "hide_fail": {
                    "text": "Cánh tủ kẽo kẹt tố cáo chỗ trốn của bạn trước mặt Vy.",
                    "pose": "stand",
                },
                "bluff": {
                    "text": "Bạn bình tĩnh vờ như vừa ghé tìm sạc điện thoại.",
                    "pose": "stand",
                },
                "retreat": {
                    "text": "Bạn kịp lùi ra hành lang trước khi Vy bước vào.",
                    "pose": "stand",
                },
            },
        }

        bed_tile = _find_walkable_tile_in_room(fp, "bedroom_vy", _make_canonical_state(room_id="bedroom_vy"))
        safe_state = _make_canonical_state(
            hour=20,  # Vy arrives in bedroom_vy at 21:00
            minute=0,
            room_id="bedroom_vy",
            pos=bed_tile,
            pose="stand",
            stats={"charm": 12, "logic": 13, "stealth": 14, "will": 11},
            npcs={
                "vy": {
                    "room_id": "living_room",
                    "pos": _room_center(fp, "living_room"),
                    "pose": "sit",
                    "target_prop": "prop_sofa_living",
                    "mood": "calm",
                    "activity": "Đọc tin nhắn",
                    "alertness": 0,
                }
            },
            relationships={"vy": 80},
        )
        self.assertIsNone(
            engine.check_intrusion_imminent(world, fp, safe_state, "bedroom_vy", action_minutes=5)
        )

        danger_state = copy.deepcopy(safe_state)
        danger_state["clock"]["hour"] = 20
        danger_state["clock"]["minute"] = 56  # 20:56 -> 4 min <= 5 min before 21:00
        warning = engine.check_intrusion_imminent(world, fp, danger_state, "bedroom_vy", action_minutes=5)
        self.assertIsNotNone(warning)
        self.assertEqual(warning.get("owner_id"), "vy")
        self.assertIn("minutes_until", warning)
        self.assertLessEqual(int(warning["minutes_until"]), 5)
        danger_state["pending_intrusion"] = copy.deepcopy(warning)

        campaign_ok = {
            "id": "camp_intrusion_05",
            "act_index": 1,
            "world": world,
            "cast": world["cast"],
            "floorplan": fp,
            "storylets": [],
            "intrusion_scenes": {"bedroom_vy": intrusion_scene},
            "state": copy.deepcopy(danger_state),
            "history": [],
        }
        db.save_campaign(campaign_ok, db_path=self.db_path)

        res_ok = engine.resolve_intrusion_choice(
            campaign_ok, "hide", forced_roll=19, db_path=self.db_path
        )
        self.assertEqual(mock_llm.call_count, 0)
        self.assertIs(res_ok["success"], True)
        self.assertIs(res_ok["d20"]["success"], True)
        self.assertEqual(res_ok["pose"], "hide_inside")
        self.assertEqual(campaign_ok["state"]["player"]["pose"], "hide_inside")
        self.assertIn("node", res_ok)
        self.assertIsNotNone(res_ok["node"])

        # Auto-risk checkpoint saved for rewind safety
        cps = db.list_checkpoints("camp_intrusion_05", db_path=self.db_path)
        self.assertTrue(
            any(cp.get("kind") == "auto_risk" for cp in cps),
            f"Expected auto_risk checkpoint, got {cps}",
        )

        campaign_fail = copy.deepcopy(campaign_ok)
        campaign_fail["id"] = "camp_intrusion_05_fail"
        campaign_fail["state"] = copy.deepcopy(danger_state)
        db.save_campaign(campaign_fail, db_path=self.db_path)
        res_fail = engine.resolve_intrusion_choice(
            campaign_fail, "hide", forced_roll=2, db_path=self.db_path
        )
        self.assertIs(res_fail["success"], False)
        self.assertIs(res_fail["d20"]["success"], False)
        self.assertNotEqual(campaign_fail["state"]["player"]["pose"], "hide_inside")
        self.assertEqual(mock_llm.call_count, 0)

    def test_06_room_acoustics_and_offscreen_traces(self) -> None:
        import catalog
        import compiler
        import engine

        starter = catalog.load_starter_catalog()
        world = _build_6room_mansion_world(starter)
        secret_dialogue = "SECRET_OFFSCREEN_DIALOGUE_DO_NOT_LEAK"
        world["offscreen_events"] = [
            {
                "id": "off_kitchen_conflict",
                "room_id": "kitchen",
                "trigger_min": 1210,
                "participants": ["han", "tuan"],
                "secret_dialogue": secret_dialogue,
                "sound_hint": "Tiếng ly thủy tinh va mạnh vang qua vách bếp.",
                "trace_label": "Mảnh thủy tinh vỡ dưới gầm bàn bếp",
                "mood_effects": {"han": "angry", "tuan": "nervous"},
            }
        ]
        fp = compiler.pack_floorplan(world, starter)
        # Ensure living_room and kitchen are adjacent in packed floorplan
        self.assertIn("kitchen", fp.get("adjacent_rooms", {}).get("living_room", []))

        state = _make_canonical_state(
            hour=20,
            minute=0,
            room_id="living_room",
            pos=_room_center(fp, "living_room"),
            stats={"charm": 12, "logic": 13, "stealth": 15, "will": 11},
            npcs={
                "vy": {
                    "room_id": "living_room",
                    "pos": _room_center(fp, "living_room"),
                    "pose": "sit",
                    "target_prop": "prop_sofa_living",
                    "mood": "calm",
                    "activity": "Đọc tin nhắn",
                    "alertness": 0,
                },
                "khanh": {
                    "room_id": "living_room",
                    "pos": _room_center(fp, "living_room"),
                    "pose": "stand",
                    "target_prop": None,
                    "mood": "calm",
                    "activity": "Quan sát",
                    "alertness": 0,
                },
                "han": {
                    "room_id": "kitchen",
                    "pos": _room_center(fp, "kitchen"),
                    "pose": "stand",
                    "target_prop": "prop_kitchen_1",
                    "mood": "calm",
                    "activity": "Pha trà",
                    "alertness": 0,
                },
                "tuan": {
                    "room_id": "kitchen",
                    "pos": _room_center(fp, "kitchen"),
                    "pose": "stand",
                    "target_prop": "prop_kitchen_1",
                    "mood": "calm",
                    "activity": "Tranh luận nhỏ",
                    "alertness": 0,
                },
            },
            relationships={"vy": 80, "khanh": 70, "han": 60, "tuan": 65},
            traces=[],
        )

        pub = engine.perform_speech_or_dialogue(
            world, state, "Mọi người nghe rõ kế hoạch tối nay chứ?", whisper=False
        )
        self.assertEqual(set(pub["witnesses"]), {"vy", "khanh"})
        self.assertNotIn("han", pub["witnesses"])
        self.assertNotIn("tuan", pub["witnesses"])

        priv = engine.perform_speech_or_dialogue(
            world,
            state,
            "Chỉ nói riêng với Vy thôi nhé.",
            whisper=True,
            target_npc_id="vy",
            forced_roll=18,
        )
        self.assertEqual(priv["witnesses"], ["vy"])
        self.assertNotIn("khanh", priv["witnesses"])

        events = engine.advance_clock_and_sim(world, fp, state, minutes=15)
        events_json = json.dumps(events, ensure_ascii=False)
        state_json = json.dumps(state, ensure_ascii=False)
        self.assertNotIn(secret_dialogue, events_json, "Off-screen dialogue text must NEVER leak in events")
        self.assertNotIn(secret_dialogue, state_json, "Off-screen dialogue text must NEVER leak in state")

        event_types = {str(ev.get("type") or ev.get("kind") or "") for ev in events}
        self.assertTrue(
            any("sound" in t or "acoustic" in t for t in event_types),
            f"Expected adjacent_sound event, got {events}",
        )
        traces = state["traces"]
        self.assertTrue(
            any(h.get("room_id") == "kitchen" for h in traces)
            or any("trace" in str(ev.get("type") or ev.get("kind") or "") for ev in events),
            f"Expected trace in kitchen, got traces={traces}, events={events}",
        )
        npcs_state = state["npcs"]
        self.assertTrue(
            npcs_state["han"].get("mood") != "calm" or npcs_state["tuan"].get("mood") != "calm",
            f"Expected NPC mood update from off-screen event, got {npcs_state}",
        )

    def test_07_item_use_and_foldback(self) -> None:
        import catalog
        import compiler
        import engine

        starter = catalog.load_starter_catalog()
        world = _build_6room_mansion_world(starter)
        fp = compiler.pack_floorplan(world, starter)
        state = _make_canonical_state(
            hour=20,
            minute=0,
            room_id="living_room",
            pos=_room_center(fp, "living_room"),
            energy=80,
            stress=10,
            inventory=["study_key"],
            doors_unlocked=[],
            flags={"study_unlocked": False, "found_ring_box": False},
            discovered_items=[],
            relationships={"vy": 70},
        )
        state["player"]["pos"] = _find_walkable_tile_in_room(fp, "living_room", state)

        foyer_xy = _find_walkable_tile_in_room(fp, "foyer", state)
        study_xy = _find_walkable_tile_in_room(fp, "study_locked", state)
        self.assertIsNone(engine.find_path(fp, foyer_xy, study_xy, state))

        wrong_res = engine.use_item(world, fp, state, "wrong_key", "door_study")
        self.assertIs(wrong_res["success"], False)
        self.assertNotIn("door_study", state["doors_unlocked"])
        self.assertIsNone(engine.find_path(fp, foyer_xy, study_xy, state))

        ok_res = engine.use_item(world, fp, state, "study_key", "door_study")
        self.assertIs(ok_res["success"], True)
        self.assertIn("door_study", state["doors_unlocked"])
        self.assertIsNotNone(engine.find_path(fp, foyer_xy, study_xy, state))

        delta = {
            "set_flags": {"found_ring_box": True},
            "add_items": ["ring_box"],
            "remove_items": ["study_key"],
            "discover_items": ["prop_mystery_01"],
            "affinity_deltas": {"vy": 5},
            "energy_delta": -10,
        }
        engine.apply_state_delta(world, fp, state, delta)

        self.assertIs(state["flags"]["found_ring_box"], True)
        self.assertIn("ring_box", state["player"]["inventory"])
        self.assertNotIn("study_key", state["player"]["inventory"])
        self.assertIn("prop_mystery_01", state["discovered_items"])
        self.assertEqual(state["relationships"]["vy"], 75)
        self.assertEqual(state["player"]["energy"], 70)

    def test_08_episodic_act_transition_and_ending(self) -> None:
        import catalog
        import db
        import compiler
        import engine
        import llm_client

        db.init_db(db_path=self.db_path)
        starter = catalog.load_starter_catalog()
        world = _build_6room_mansion_world(starter)

        act1_resp = {
            "storylets": [
                {
                    "id": "st_act1_foyer",
                    "act_index": 1,
                    "title": "Dấu chân ướt ở sảnh",
                    "trigger": {"room_id": "foyer", "one_shot": True},
                    "relevant_keys": {"flags": ["act1_complete"], "items": [], "affinity_with": [], "extra": []},
                    "priority": 50,
                    "is_intrusion": False,
                    "root_node_id": "n1",
                    "nodes": {
                        "n1": {
                            "node_id": "n1",
                            "text": "Dấu chân ướt dẫn vào hành lang.",
                            "speaker_id": None,
                            "presentation_mode": "standard",
                            "pose_updates": {},
                            "choices": [{"id": "c1", "label": "Tiếp tục", "effects": None}],
                        }
                    },
                }
            ],
            "intrusion_scenes": {},
            "initial_cache": [],
        }
        act2_resp = {
            "storylets": [
                {
                    "id": "st_act2_study",
                    "act_index": 2,
                    "title": "Bí mật sau cánh cửa phòng làm việc",
                    "trigger": {"room_id": "study_locked", "one_shot": True},
                    "relevant_keys": {"flags": ["act2_complete"], "items": [], "affinity_with": [], "extra": []},
                    "priority": 50,
                    "is_intrusion": False,
                    "root_node_id": "n1",
                    "nodes": {
                        "n1": {
                            "node_id": "n1",
                            "text": "Ánh đèn bàn hắt lên ngăn kéo khóa kín.",
                            "speaker_id": None,
                            "presentation_mode": "standard",
                            "pose_updates": {},
                            "choices": [{"id": "c1", "label": "Kiểm tra", "effects": None}],
                        }
                    },
                }
            ]
        }
        act3_resp = {
            "storylets": [
                {
                    "id": "st_act3_finale",
                    "act_index": 3,
                    "title": "Đối chất lúc nửa đêm",
                    "trigger": {"room_id": "living_room", "one_shot": True},
                    "relevant_keys": {"flags": ["truth_revealed"], "items": [], "affinity_with": [], "extra": []},
                    "priority": 50,
                    "is_intrusion": False,
                    "root_node_id": "n1",
                    "nodes": {
                        "n1": {
                            "node_id": "n1",
                            "text": "Mọi người tập trung tại phòng khách.",
                            "speaker_id": None,
                            "presentation_mode": "standard",
                            "pose_updates": {},
                            "choices": [{"id": "c1", "label": "Công bố sự thật", "effects": None}],
                        }
                    },
                }
            ]
        }

        mock_llm = llm_client.MockLLMClient(responses=[act1_resp, act2_resp, act3_resp])

        campaign = compiler.compile_new_campaign(
            world, llm=mock_llm, catalog=starter, campaign_id="camp_acts_08", db_path=self.db_path
        )
        self.assertEqual(campaign["act_index"], 1)

        status_1 = engine.check_act_or_ending(campaign["world"], campaign["state"], campaign["storylets"])
        self.assertIs(status_1["can_advance_act"], False)
        self.assertIsNone(status_1["next_act"])
        self.assertIsNone(status_1["ending"])

        # Act 1 -> Act 2
        campaign["state"].setdefault("flags", {})["act1_complete"] = True
        status_1_ready = engine.check_act_or_ending(campaign["world"], campaign["state"], campaign["storylets"])
        self.assertIs(status_1_ready["can_advance_act"], True)
        self.assertEqual(status_1_ready["next_act"], 2)

        calls_before_act2 = mock_llm.call_count
        campaign = compiler.compile_next_act(campaign, mock_llm, db_path=self.db_path)
        self.assertEqual(campaign["act_index"], 2)
        self.assertGreater(mock_llm.call_count, calls_before_act2)
        self.assertTrue(any(s.get("id") == "st_act2_study" for s in campaign["storylets"]))

        # Act 2 -> Act 3
        campaign["state"]["flags"]["act2_complete"] = True
        status_2_ready = engine.check_act_or_ending(campaign["world"], campaign["state"], campaign["storylets"])
        self.assertIs(status_2_ready["can_advance_act"], True)
        self.assertEqual(status_2_ready["next_act"], 3)
        campaign = compiler.compile_next_act(campaign, mock_llm, db_path=self.db_path)
        self.assertEqual(campaign["act_index"], 3)
        self.assertTrue(any(s.get("id") == "st_act3_finale" for s in campaign["storylets"]))

        # Trigger ending + enable Free Sandbox mode
        campaign["state"]["flags"]["truth_revealed"] = True
        ending_status = engine.check_act_or_ending(campaign["world"], campaign["state"], campaign["storylets"])
        self.assertIsNotNone(ending_status["ending"])
        ending_id = ending_status["ending"]["id"] if isinstance(ending_status["ending"], dict) else ending_status["ending"]
        self.assertEqual(ending_id, "ending_truth_revealed")

        sandbox_state = engine.enable_free_sandbox(campaign["state"])
        self.assertIs(sandbox_state["free_sandbox_mode"], True)
        self.assertIs(sandbox_state["flags"]["truth_revealed"], True)

    def test_09_timeline_checkpoint_rewind_shares_cache(self) -> None:
        import db
        import llm_client
        import prefetch

        db.init_db(db_path=self.db_path)
        camp_id = "camp_timeline_09"
        state_t0 = _make_canonical_state(
            hour=8,
            minute=20,
            room_id="foyer",
            flags={"a": False},
            inventory=["key_0"],
        )
        db.save_campaign(
            {
                "id": camp_id,
                "title": "Timeline Rewind",
                "act_index": 1,
                "world": _build_6room_mansion_world(),
                "cast": [],
                "floorplan": {},
                "storylets": [],
                "state": copy.deepcopy(state_t0),
                "history": [],
            },
            db_path=self.db_path,
        )

        st_0 = {
            "id": "st_0",
            "act_index": 1,
            "title": "Sảnh chính lúc 08:20",
            "trigger": {"room_id": "foyer", "one_shot": False},
            "relevant_keys": {"flags": ["a"], "items": ["key_0"], "affinity_with": [], "extra": []},
            "priority": 50,
            "is_intrusion": False,
            "root_node_id": "S0",
            "nodes": None,
        }
        st_1 = {
            "id": "st_1",
            "act_index": 1,
            "title": "Phòng khách nhánh sai",
            "trigger": {"room_id": "living_room", "one_shot": False},
            "relevant_keys": {"flags": ["wrong_branch"], "items": ["clue_1"], "affinity_with": [], "extra": []},
            "priority": 50,
            "is_intrusion": False,
            "root_node_id": "S1",
            "nodes": None,
        }
        scene_s0 = {
            "storylet_id": "st_0",
            "root_node_id": "S0",
            "nodes": {"S0": {"node_id": "S0", "text": "Sảnh chính lúc 08:20.", "choices": []}},
        }
        scene_s1 = {
            "storylet_id": "st_1",
            "root_node_id": "S1",
            "nodes": {"S1": {"node_id": "S1", "text": "Phòng khách nhánh sai.", "choices": []}},
        }
        mock_llm = llm_client.MockLLMClient(responses=[scene_s0, scene_s1])

        cp0 = db.create_checkpoint(
            camp_id,
            label="Đầu Hồi 1",
            kind="act_start",
            act_index=1,
            clock_str="08:20",
            state=state_t0,
            history=[{"turn": 0}],
            storylets=[st_0],
            db_path=self.db_path,
        )
        cp0_id = cp0["id"] if isinstance(cp0, dict) else cp0
        pm_seed = prefetch.PrefetchManager(max_workers=1, max_prefetch_depth=1)
        try:
            pm_seed.enqueue(camp_id, st_0, state_t0, mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm_seed.shutdown(wait=True)
        finally:
            pm_seed.shutdown(wait=False)
        self.assertEqual(mock_llm.call_count, 1)

        # Advance to T1 and push undo frame
        db.push_undo(camp_id, state_t0, [{"turn": 0}], [st_0], db_path=self.db_path)
        state_t1 = _make_canonical_state(
            hour=9,
            minute=0,
            room_id="living_room",
            flags={"a": True, "wrong_branch": True},
            inventory=["key_0", "clue_1"],
        )
        cp1 = db.create_checkpoint(
            camp_id,
            label="Nhánh sai",
            kind="manual",
            act_index=1,
            clock_str="09:00",
            state=state_t1,
            history=[{"turn": 0}, {"turn": 1}],
            storylets=[st_0, st_1],
            db_path=self.db_path,
        )
        cp1_id = cp1["id"] if isinstance(cp1, dict) else cp1
        pm_branch = prefetch.PrefetchManager(max_workers=1, max_prefetch_depth=1)
        try:
            pm_branch.enqueue(camp_id, st_1, state_t1, mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm_branch.shutdown(wait=True)
        finally:
            pm_branch.shutdown(wait=False)
        self.assertEqual(mock_llm.call_count, 2)

        # Pop undo restores T0 cleanly
        undone = db.pop_undo(camp_id, db_path=self.db_path)
        self.assertIsNotNone(undone)
        self.assertEqual(undone["state"]["clock"]["hour"], 8)
        self.assertEqual(undone["state"]["clock"]["minute"], 20)
        self.assertEqual(undone["state"]["player"]["room_id"], "foyer")
        self.assertIs(undone["state"]["flags"]["a"], False)
        self.assertNotIn("wrong_branch", undone["state"]["flags"])

        # Rewind via checkpoint cp0 and cp1; shared scene_cache remains 100% intact
        loaded_cp0 = db.get_checkpoint(cp0_id, db_path=self.db_path)
        loaded_cp1 = db.get_checkpoint(cp1_id, db_path=self.db_path)
        self.assertEqual(loaded_cp0["state"]["clock"]["hour"], 8)
        self.assertEqual(loaded_cp0["state"]["clock"]["minute"], 20)
        self.assertEqual(loaded_cp1["state"]["clock"]["hour"], 9)
        self.assertEqual(loaded_cp1["state"]["clock"]["minute"], 0)

        hash_0 = prefetch.masked_state_hash(loaded_cp0["state"], st_0["relevant_keys"])
        hash_1 = prefetch.masked_state_hash(loaded_cp1["state"], st_1["relevant_keys"])
        self.assertIsNotNone(db.get_cached_scene(camp_id, "st_0", hash_0, db_path=self.db_path))
        self.assertIsNotNone(db.get_cached_scene(camp_id, "st_1", hash_1, db_path=self.db_path))

        # Replay/prefetch after rewind hits shared scene_cache with 0 new LLM calls
        calls_before_replay = mock_llm.call_count
        pm_replay = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
        try:
            pm_replay.enqueue(camp_id, st_0, loaded_cp0["state"], mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm_replay.enqueue(camp_id, st_1, loaded_cp1["state"], mock_llm, priority=prefetch.PRIO_P0, db_path=self.db_path)
            pm_replay.shutdown(wait=True)
        finally:
            pm_replay.shutdown(wait=False)
        self.assertEqual(mock_llm.call_count, calls_before_replay)

    def test_10_prefetch_priority_and_cancel(self) -> None:
        import db
        import llm_client
        import prefetch

        db.init_db(db_path=self.db_path)
        camp_id = "camp_prefetch_10"
        db.save_campaign(
            {
                "id": camp_id,
                "title": "Prefetch Priority Test",
                "act_index": 1,
                "world": _build_6room_mansion_world(),
                "cast": [],
                "floorplan": {},
                "storylets": [],
                "state": {},
                "history": [],
            },
            db_path=self.db_path,
        )

        gate_release = threading.Event()
        both_workers_gated = threading.Event()
        lock = threading.Lock()
        gate_hits = 0
        active_workers = 0
        peak_workers = 0
        executed_prompts: list[str] = []

        class GatedMockLLMClient(llm_client.MockLLMClient):
            def complete_json(self, messages, **kwargs):
                nonlocal gate_hits, active_workers, peak_workers
                joined = json.dumps(messages, ensure_ascii=False)
                with lock:
                    active_workers += 1
                    if active_workers > peak_workers:
                        peak_workers = active_workers
                    executed_prompts.append(joined)
                    is_gate = "st_gate_" in joined
                    if is_gate:
                        gate_hits += 1
                        if gate_hits >= 2:
                            both_workers_gated.set()
                try:
                    if "st_gate_" in joined:
                        gate_release.wait(timeout=5.0)
                    return super().complete_json(messages, **kwargs)
                finally:
                    with lock:
                        active_workers -= 1

        gated_llm = GatedMockLLMClient(
            default_json={
                "root_node_id": "n0",
                "nodes": {
                    "n0": {
                        "node_id": "n0",
                        "text": "Compiled",
                        "speaker_id": None,
                        "presentation_mode": "standard",
                        "pose_updates": {},
                        "choices": [],
                    }
                },
            }
        )
        base_state = _make_canonical_state()
        rk = {"flags": [], "items": [], "affinity_with": [], "extra": []}

        pm = prefetch.PrefetchManager(max_workers=2, max_prefetch_depth=1)
        try:
            # Occupy both worker threads deterministically before queuing Branch B & Branch A
            for idx in (1, 2):
                pm.enqueue(
                    camp_id,
                    {
                        "id": f"st_gate_{idx}",
                        "act_index": 1,
                        "title": f"Gate {idx}",
                        "trigger": {"room_id": "foyer"},
                        "relevant_keys": rk,
                    },
                    base_state,
                    gated_llm,
                    priority=prefetch.PRIO_P0,
                    branch_group="gate",
                    db_path=self.db_path,
                )

            self.assertTrue(
                both_workers_gated.wait(timeout=5.0),
                "Expected 2 worker threads to enter gate simultaneously",
            )

            pm.enqueue(
                camp_id,
                {
                    "id": "st_branch_b_1",
                    "act_index": 1,
                    "title": "Branch B1",
                    "trigger": {"room_id": "foyer"},
                    "relevant_keys": rk,
                },
                base_state,
                gated_llm,
                priority=prefetch.PRIO_P1,
                branch_group="branch_b",
                db_path=self.db_path,
            )
            pm.enqueue(
                camp_id,
                {
                    "id": "st_branch_a_1",
                    "act_index": 1,
                    "title": "Branch A1",
                    "trigger": {"room_id": "foyer"},
                    "relevant_keys": rk,
                },
                base_state,
                gated_llm,
                priority=prefetch.PRIO_P1,
                branch_group="branch_a",
                db_path=self.db_path,
            )

            pm.promote_branch("branch_a", cancel_other_groups=["branch_b"])
            gate_release.set()
            pm.shutdown(wait=True)
        finally:
            gate_release.set()
            pm.shutdown(wait=False)

        h = prefetch.masked_state_hash(base_state, rk)
        self.assertIsNotNone(db.get_cached_scene(camp_id, "st_branch_a_1", h, db_path=self.db_path))
        self.assertIsNone(db.get_cached_scene(camp_id, "st_branch_b_1", h, db_path=self.db_path))
        self.assertFalse(any("st_branch_b_1" in p for p in executed_prompts), "Cancelled branch_b executed")
        self.assertTrue(any("st_branch_a_1" in p for p in executed_prompts), "Promoted branch_a did not execute")
        self.assertLessEqual(peak_workers, 2)
        status = pm.get_status()
        self.assertIn("max_prefetch_depth", status)
        self.assertEqual(status["max_prefetch_depth"], 1)

    def test_11_glb_validation_and_blender_export_kit(self) -> None:
        import catalog

        # 1) Module imports cleanly without bpy
        _ensure_iso_root_first()
        kit_mod = importlib.import_module("tools.blender_export_kit")
        self.assertIsNotNone(kit_mod)
        kit_file = os.path.normcase(os.path.abspath(getattr(kit_mod, "__file__", "")))
        self.assertTrue(
            kit_file.startswith(os.path.normcase(os.path.abspath(ISO_ENGINE_ROOT)) + os.sep)
        )

        # 2) Subprocess CLI --create-minimal-glb and --validate-glb succeed without Blender
        glb_path = os.path.join(self.tmp_dir, "minimal_prop.glb")
        create_proc = subprocess.run(
            [sys.executable, BLENDER_KIT_PATH, "--create-minimal-glb", glb_path],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            create_proc.returncode,
            0,
            f"--create-minimal-glb failed: stdout={create_proc.stdout} stderr={create_proc.stderr}",
        )
        self.assertTrue(os.path.isfile(glb_path))

        with open(glb_path, "rb") as fh:
            glb_bytes = fh.read()
        self.assertGreaterEqual(len(glb_bytes), 20)
        magic, version, total_len = struct.unpack("<III", glb_bytes[:12])
        self.assertEqual(glb_bytes[:4], b"glTF")
        self.assertEqual(magic, 0x46546C67)
        self.assertEqual(version, 2)
        self.assertEqual(total_len, len(glb_bytes))

        val_proc = subprocess.run(
            [sys.executable, BLENDER_KIT_PATH, "--validate-glb", glb_path],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            val_proc.returncode,
            0,
            f"--validate-glb failed: stdout={val_proc.stdout} stderr={val_proc.stderr}",
        )

        # 3) catalog.validate_glb_bytes positive & negative cases
        valid_res = catalog.validate_glb_bytes(glb_bytes)
        self.assertIsInstance(valid_res, dict)
        self.assertIs(valid_res["valid"], True)

        # Synthetic valid GLB fixture also accepted
        synth_glb = _build_minimal_glb_bytes()
        synth_res = catalog.validate_glb_bytes(synth_glb)
        self.assertIsInstance(synth_res, dict)
        self.assertIs(synth_res["valid"], True)

        bad_magic = b"XXXX" + synth_glb[4:]
        bad_version = struct.pack("<III", 0x46546C67, 1, len(synth_glb)) + synth_glb[12:]
        truncated = synth_glb[:10]
        bad_json_payload = b"{not_json"
        bad_json_glb = (
            struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(bad_json_payload))
            + struct.pack("<II", len(bad_json_payload), 0x4E4F534A)
            + bad_json_payload
        )
        declared_len_mismatch = struct.pack("<III", 0x46546C67, 2, len(synth_glb) + 64) + synth_glb[12:]

        for label, bad_bytes in (
            ("empty", b""),
            ("truncated", truncated),
            ("bad_magic", bad_magic),
            ("bad_version", bad_version),
            ("bad_json", bad_json_glb),
            ("length_mismatch", declared_len_mismatch),
        ):
            try:
                res = catalog.validate_glb_bytes(bad_bytes)
            except ValueError:
                pass
            else:
                self.assertIsInstance(res, dict, f"{label}: expected dict when not raising ValueError")
                self.assertIs(res["valid"], False, f"validate_glb_bytes unexpectedly accepted {label}")


if __name__ == "__main__":
    unittest.main()
