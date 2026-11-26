"""Core schemas, validators, anti-spoiler redaction, and v2->iso upgraders for iso_engine."""

from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from collections import Counter, deque
from typing import Any

ID_RE = re.compile(r"^[a-z0-9_]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

STAT_KEYS = ("charm", "logic", "stealth", "will")
POSES = ("stand", "sit", "lie", "hide_inside")
TONE_RATINGS = ("safe", "mature_drama", "adult_nsfw")
MOUNTS = ("floor", "surface", "wall")
ROOM_KINDS = (
    "foyer",
    "living",
    "bedroom",
    "kitchen",
    "bathroom",
    "study",
    "hallway",
    "storage",
    "other",
)
CHOICE_STYLES = ("normal", "whisper", "skill_check", "use_item", "retreat")
PRESENTATION_MODES = ("standard", "cinematic_focus")
CLOCK_MODES = ("calendar", "day_counter", "timeless")
PRONOUN_CONTEXTS = ("close_peer", "acquaintance", "elder_formal", "angry")

DEFAULT_PRONOUN_DEFAULTS: dict[str, dict[str, str]] = {
    "close_peer": {"self": "tui", "call": "ông/bà"},
    "acquaintance": {"self": "mình", "call": "bạn"},
    "elder_formal": {"self": "em", "call": "anh/chị/thầy/cô"},
    "angry": {"self": "tôi", "call": "ông/bà"},
}


class SchemaError(ValueError):
    """Raised when an iso-char-v1, iso-world-v1, MissingPropSpec, Storylet, or v2 input fails validation."""


def _is_strict_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _slug(text: str) -> str:
    norm = unicodedata.normalize("NFKD", str(text or ""))
    ascii_str = norm.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", ascii_str).strip("_")
    return cleaned or "item"


def _str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    raise SchemaError(f"Expected list of strings, got {type(val).__name__}")


def _validate_id(val: Any, label: str = "id") -> str:
    if not isinstance(val, str) or not ID_RE.match(val):
        raise SchemaError(f"Invalid {label}: {val!r} (must match ^[a-z0-9_]+$)")
    return val


def _validate_hex(val: Any, label: str) -> str:
    if not isinstance(val, str) or not HEX_COLOR_RE.match(val):
        raise SchemaError(f"Invalid hex color for {label}: {val!r}")
    return val.lower()


def _normalize_pronoun_defaults(pd_raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(pd_raw, dict) or not pd_raw:
        raise SchemaError("voice.pronoun_defaults must be a non-empty dict")
    out: dict[str, dict[str, str]] = {}
    for k, pair in pd_raw.items():
        if not isinstance(pair, dict):
            raise SchemaError(f"pronoun_defaults['{k}'] must be a dict")
        s_val = str(pair.get("self") or "").strip()
        c_val = str(pair.get("call") or "").strip()
        if not s_val or not c_val:
            raise SchemaError(f"pronoun_defaults['{k}'] requires non-empty 'self' and 'call'")
        out[str(k)] = {"self": s_val, "call": c_val}
    for ctx in PRONOUN_CONTEXTS:
        if ctx not in out:
            out[ctx] = dict(DEFAULT_PRONOUN_DEFAULTS[ctx])
    return out


def validate_iso_char(c: dict) -> dict:
    """Validate and return a normalized deep copy of an iso-char-v1 character dict."""
    if not isinstance(c, dict):
        raise SchemaError("Character must be a dict")
    if c.get("format") != "iso-char-v1":
        raise SchemaError(f"Expected format 'iso-char-v1', got {c.get('format')!r}")

    cid = _validate_id(c.get("id"), "character id")

    meta = c.get("meta")
    if not isinstance(meta, dict):
        raise SchemaError("Character 'meta' must be a dict")
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SchemaError("Character 'meta.name' must be a non-empty string")
    if meta.get("fictional") is not True:
        raise SchemaError("Character 'meta.fictional' must be strictly True")
    age = meta.get("age")
    if age is not None:
        if not _is_strict_int(age) or age < 0:
            raise SchemaError(f"Invalid character 'meta.age': {age!r}")
    aliases = _str_list(meta.get("aliases", []))
    gender = str(meta.get("gender") or "không rõ").strip()

    core = c.get("core")
    if not isinstance(core, dict):
        raise SchemaError("Character 'core' must be a dict")
    one_liner = core.get("one_liner")
    if not isinstance(one_liner, str) or not one_liner.strip():
        raise SchemaError("Character 'core.one_liner' must be a non-empty string")
    personality = _str_list(core.get("personality"))
    if not personality:
        raise SchemaError("Character 'core.personality' must be non-empty")
    values = str(core.get("values") or "")
    boundaries = _str_list(core.get("boundaries", []))

    voice = c.get("voice")
    if not isinstance(voice, dict):
        raise SchemaError("Character 'voice' must be a dict")
    pronoun_defaults = _normalize_pronoun_defaults(voice.get("pronoun_defaults"))
    modes_raw = voice.get("modes") if isinstance(voice.get("modes"), dict) else {}
    in_person_raw = modes_raw.get("in_person") if isinstance(modes_raw.get("in_person"), dict) else {}
    texting_raw = modes_raw.get("texting") if isinstance(modes_raw.get("texting"), dict) else {}
    modes = {
        "in_person": {
            "style": str(in_person_raw.get("style") or "Văn nói trực tiếp, lồng cử chỉ vào văn xuôi."),
            "tics": _str_list(in_person_raw.get("tics", [])),
            "samples": _str_list(in_person_raw.get("samples", [])),
        },
        "texting": {
            "style": str(texting_raw.get("style") or "Tin nhắn ngắn tự nhiên."),
            "samples": _str_list(texting_raw.get("samples", [])),
        },
    }

    stats_raw = c.get("stats")
    if not isinstance(stats_raw, dict):
        raise SchemaError("Character 'stats' must be a dict")
    stats: dict[str, int] = {}
    for sk in STAT_KEYS:
        val = stats_raw.get(sk)
        if not _is_strict_int(val) or not (0 <= val <= 20):
            raise SchemaError(f"Character 'stats.{sk}' must be an int in 0..20, got {val!r}")
        stats[sk] = val

    vl_raw = c.get("voxel_look")
    if not isinstance(vl_raw, dict):
        raise SchemaError("Character 'voxel_look' must be a dict")
    hair_style = vl_raw.get("hair_style")
    if not isinstance(hair_style, str) or not hair_style.strip():
        raise SchemaError("Character 'voxel_look.hair_style' must be a non-empty string")
    h_scale = vl_raw.get("height_scale")
    if not _is_number(h_scale) or not (0.7 <= float(h_scale) <= 1.3):
        raise SchemaError(f"Character 'voxel_look.height_scale' must be in [0.7, 1.3], got {h_scale!r}")
    voxel_look = {
        "skin": _validate_hex(vl_raw.get("skin"), "voxel_look.skin"),
        "hair": _validate_hex(vl_raw.get("hair"), "voxel_look.hair"),
        "hair_style": hair_style.strip(),
        "outfit_top": _validate_hex(vl_raw.get("outfit_top"), "voxel_look.outfit_top"),
        "outfit_bottom": _validate_hex(vl_raw.get("outfit_bottom"), "voxel_look.outfit_bottom"),
        "accent": _validate_hex(vl_raw.get("accent"), "voxel_look.accent"),
        "height_scale": round(float(h_scale), 3),
    }

    sched_raw = c.get("schedule", [])
    if not isinstance(sched_raw, list):
        raise SchemaError("Character 'schedule' must be a list")
    schedule: list[dict[str, Any]] = []
    for idx, entry in enumerate(sched_raw):
        if not isinstance(entry, dict):
            raise SchemaError(f"schedule[{idx}] must be a dict")
        st = entry.get("start")
        en = entry.get("end")
        if not isinstance(st, str) or not TIME_HHMM_RE.match(st):
            raise SchemaError(f"schedule[{idx}].start invalid HH:MM: {st!r}")
        if not isinstance(en, str) or not TIME_HHMM_RE.match(en):
            raise SchemaError(f"schedule[{idx}].end invalid HH:MM: {en!r}")
        room_id = entry.get("room_id")
        if not isinstance(room_id, str) or not room_id.strip():
            raise SchemaError(f"schedule[{idx}].room_id must be a non-empty string")
        pose = entry.get("pose")
        if pose not in POSES:
            raise SchemaError(f"schedule[{idx}].pose must be one of {POSES}, got {pose!r}")
        target_prop = entry.get("target_prop")
        if target_prop is not None and not isinstance(target_prop, str):
            raise SchemaError(f"schedule[{idx}].target_prop must be str or None")
        schedule.append(
            {
                "start": st,
                "end": en,
                "room_id": room_id.strip(),
                "activity": str(entry.get("activity") or ""),
                "pose": pose,
                "target_prop": target_prop,
            }
        )

    inventory = _str_list(c.get("inventory", []))

    secrets_raw = c.get("secrets", [])
    if not isinstance(secrets_raw, list):
        raise SchemaError("Character 'secrets' must be a list")
    secrets: list[dict[str, Any]] = []
    for idx, sec in enumerate(secrets_raw):
        if not isinstance(sec, dict):
            raise SchemaError(f"secrets[{idx}] must be a dict")
        text = str(sec.get("text") or "").strip()
        if not text:
            raise SchemaError(f"secrets[{idx}].text must be non-empty")
        sec_id = str(sec.get("id") or f"sec_{cid}_{idx + 1:02d}").strip()
        secrets.append(
            {
                "id": sec_id,
                "text": text,
                "known_by": _str_list(sec.get("known_by", [cid])),
                "reveal_when": str(sec.get("reveal_when") or ""),
                "keywords": _str_list(sec.get("keywords", [])),
            }
        )

    app_raw = c.get("appearance") if isinstance(c.get("appearance"), dict) else {}
    outfits_raw = app_raw.get("outfits") if isinstance(app_raw.get("outfits"), dict) else {}
    appearance = {
        "summary": str(app_raw.get("summary") or ""),
        "outfits": {str(k): str(v) for k, v in outfits_raw.items()},
    }

    modules_raw = c.get("modules", [])
    if not isinstance(modules_raw, list):
        raise SchemaError("Character 'modules' must be a list")
    modules = [copy.deepcopy(m) for m in modules_raw if isinstance(m, dict)]

    core_out: dict[str, Any] = {
        "one_liner": one_liner.strip(),
        "personality": personality,
        "values": values,
        "boundaries": boundaries,
    }
    if isinstance(core.get("character_sheet"), str) and core["character_sheet"].strip():
        core_out["character_sheet"] = core["character_sheet"].strip()

    return {
        "format": "iso-char-v1",
        "id": cid,
        "meta": {
            "name": name.strip(),
            "aliases": aliases,
            "age": age,
            "gender": gender,
            "fictional": True,
        },
        "core": core_out,
        "voice": {
            "dialect": str(voice.get("dialect") or "Nam Bộ"),
            "tone": str(voice.get("tone") or ""),
            "pronoun_defaults": pronoun_defaults,
            "modes": modes,
        },
        "stats": stats,
        "voxel_look": voxel_look,
        "schedule": schedule,
        "inventory": inventory,
        "secrets": secrets,
        "appearance": appearance,
        "modules": modules,
    }


def validate_missing_prop_spec(spec: dict, room_ids: set[str] | None = None) -> dict:
    """Validate and normalize a MissingPropSpec dict."""
    if not isinstance(spec, dict):
        raise SchemaError("MissingPropSpec must be a dict")
    asset_tag = _validate_id(spec.get("asset_tag"), "missing_prop.asset_tag")
    size = spec.get("size")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or not _is_strict_int(size[0])
        or not _is_strict_int(size[1])
        or size[0] < 1
        or size[1] < 1
        or size[0] > 4
        or size[1] > 4
    ):
        raise SchemaError(f"missing_prop.size must be [w, d] ints in 1..4, got {size!r}")
    mount = spec.get("mount")
    if mount not in MOUNTS:
        raise SchemaError(f"missing_prop.mount must be one of {MOUNTS}, got {mount!r}")

    vb = spec.get("visual_brief")
    if not isinstance(vb, dict):
        raise SchemaError("missing_prop.visual_brief must be a dict")
    title_masked = str(vb.get("title_masked") or "").strip()
    shape_hint = str(vb.get("shape_hint") or "").strip()
    if not title_masked or not shape_hint:
        raise SchemaError("visual_brief requires non-empty 'title_masked' and 'shape_hint'")
    palette_raw = vb.get("palette_hint")
    if not isinstance(palette_raw, list):
        raise SchemaError("visual_brief.palette_hint must be a list of #RRGGBB strings")
    palette_hint = [_validate_hex(c, "visual_brief.palette_hint") for c in palette_raw]

    sm = spec.get("story_meta")
    if not isinstance(sm, dict):
        raise SchemaError("missing_prop.story_meta must be a dict")
    true_name = str(sm.get("true_name") or "").strip()
    room_id = str(sm.get("room_id") or "").strip()
    revealed_flag = str(sm.get("revealed_flag") or "").strip()
    if not true_name or not room_id or not revealed_flag:
        raise SchemaError("story_meta requires non-empty 'true_name', 'room_id', and 'revealed_flag'")
    if room_ids is not None and room_id not in room_ids:
        raise SchemaError(f"story_meta.room_id '{room_id}' does not exist in world rooms")
    owner_id = sm.get("owner_id")
    if owner_id is not None and not isinstance(owner_id, str):
        raise SchemaError("story_meta.owner_id must be str or None")

    return {
        "asset_tag": asset_tag,
        "size": [size[0], size[1]],
        "mount": mount,
        "visual_brief": {
            "title_masked": title_masked,
            "shape_hint": shape_hint,
            "palette_hint": palette_hint,
        },
        "story_meta": {
            "true_name": true_name,
            "room_id": room_id,
            "owner_id": owner_id,
            "revealed_flag": revealed_flag,
        },
    }


def redact_missing_prop(spec: dict, state: dict, built: bool = False) -> dict:
    """Return anti-spoiler MissingProp view for Voxel Studio.

    When unrevealed, strictly omits story_meta, true_name, room_id, owner_id, and revealed_flag.
    """
    story_meta = spec.get("story_meta") or {}
    revealed_flag = str(story_meta.get("revealed_flag") or "")
    flags = (state or {}).get("flags") or {}
    discovered = (state or {}).get("discovered_items") or []
    asset_tag = spec["asset_tag"]
    is_revealed = bool(revealed_flag and flags.get(revealed_flag)) or (asset_tag in discovered)

    base: dict[str, Any] = {
        "asset_tag": asset_tag,
        "size": list(spec["size"]),
        "mount": spec["mount"],
        "visual_brief": copy.deepcopy(spec["visual_brief"]),
        "revealed": is_revealed,
        "built": bool(built),
    }
    if is_revealed:
        base["true_name"] = story_meta.get("true_name", "")
        base["room_id"] = story_meta.get("room_id", "")
        base["owner_id"] = story_meta.get("owner_id")
    return base


def normalize_state_delta(d: dict | None) -> dict:
    """Normalize a StateDelta dict with complete canonical defaults."""
    raw = d if isinstance(d, dict) else {}

    def _int_field(key: str, default: int = 0) -> int:
        v = raw.get(key, default)
        if not _is_strict_int(v):
            raise SchemaError(f"StateDelta.{key} must be an integer, got {v!r}")
        return v

    aff_raw = raw.get("affinity_deltas") or {}
    if not isinstance(aff_raw, dict):
        raise SchemaError("StateDelta.affinity_deltas must be a dict")
    affinity_deltas: dict[str, int] = {}
    for k, v in aff_raw.items():
        if not _is_strict_int(v):
            raise SchemaError(f"StateDelta.affinity_deltas['{k}'] must be int, got {v!r}")
        affinity_deltas[str(k)] = v

    moods_raw = raw.get("npc_moods") or {}
    if not isinstance(moods_raw, dict):
        raise SchemaError("StateDelta.npc_moods must be a dict")
    npc_moods = {str(k): str(v) for k, v in moods_raw.items()}

    pose_raw = raw.get("pose_updates") or {}
    if not isinstance(pose_raw, dict):
        raise SchemaError("StateDelta.pose_updates must be a dict")
    pose_updates: dict[str, dict[str, Any]] = {}
    for actor_id, pdata in pose_raw.items():
        if not isinstance(pdata, dict):
            raise SchemaError(f"StateDelta.pose_updates['{actor_id}'] must be a dict")
        pose = pdata.get("pose")
        if pose not in POSES:
            raise SchemaError(f"StateDelta.pose_updates['{actor_id}'].pose invalid: {pose!r}")
        tp = pdata.get("target_prop")
        if tp is not None and not isinstance(tp, str):
            raise SchemaError(f"StateDelta.pose_updates['{actor_id}'].target_prop must be str or None")
        pose_updates[str(actor_id)] = {"pose": pose, "target_prop": tp}

    set_flags_raw = raw.get("set_flags") or {}
    if not isinstance(set_flags_raw, dict):
        raise SchemaError("StateDelta.set_flags must be a dict")

    traces_raw = raw.get("spawn_traces") or []
    if not isinstance(traces_raw, list):
        raise SchemaError("StateDelta.spawn_traces must be a list")

    lighting_override = raw.get("lighting_override")
    if lighting_override is not None and not isinstance(lighting_override, str):
        raise SchemaError("StateDelta.lighting_override must be str or None")

    trigger_ending = raw.get("trigger_ending")
    if trigger_ending is not None and not isinstance(trigger_ending, str):
        raise SchemaError("StateDelta.trigger_ending must be str or None")

    return {
        "add_items": _str_list(raw.get("add_items", [])),
        "remove_items": _str_list(raw.get("remove_items", [])),
        "set_flags": copy.deepcopy(set_flags_raw),
        "discover_items": _str_list(raw.get("discover_items", [])),
        "unlock_doors": _str_list(raw.get("unlock_doors", [])),
        "energy_delta": _int_field("energy_delta", 0),
        "stress_delta": _int_field("stress_delta", 0),
        "cash_delta": _int_field("cash_delta", 0),
        "time_minutes": _int_field("time_minutes", 0),
        "tension_delta": _int_field("tension_delta", 0),
        "affinity_deltas": affinity_deltas,
        "npc_moods": npc_moods,
        "reveal_secrets": _str_list(raw.get("reveal_secrets", [])),
        "pose_updates": pose_updates,
        "lighting_override": lighting_override,
        "spawn_traces": [copy.deepcopy(t) for t in traces_raw if isinstance(t, dict)],
        "trigger_ending": trigger_ending,
        "advance_act": bool(raw.get("advance_act", False)),
    }


def validate_scene_node_tree(tree: dict) -> dict:
    """Validate and normalize a compiled SceneNodeTree dict."""
    if not isinstance(tree, dict):
        raise SchemaError("SceneNodeTree must be a dict")
    root_node_id = str(tree.get("root_node_id") or "").strip()
    if not root_node_id:
        raise SchemaError("SceneNodeTree.root_node_id must be non-empty")
    nodes_raw = tree.get("nodes")
    if not isinstance(nodes_raw, dict) or not nodes_raw:
        raise SchemaError("SceneNodeTree.nodes must be a non-empty dict")
    if root_node_id not in nodes_raw:
        raise SchemaError(f"SceneNodeTree.root_node_id '{root_node_id}' missing from nodes")

    node_keys = set(str(k) for k in nodes_raw.keys())
    norm_nodes: dict[str, dict[str, Any]] = {}
    for k, node in nodes_raw.items():
        if not isinstance(node, dict):
            raise SchemaError(f"nodes['{k}'] must be a dict")
        nid = str(node.get("node_id") or k).strip()
        if nid != str(k):
            raise SchemaError(f"nodes['{k}'].node_id mismatch: {nid!r}")
        pm = node.get("presentation_mode", "standard")
        if pm not in PRESENTATION_MODES:
            raise SchemaError(f"nodes['{k}'].presentation_mode invalid: {pm!r}")
        pose_raw = node.get("pose_updates") or {}
        if not isinstance(pose_raw, dict):
            raise SchemaError(f"nodes['{k}'].pose_updates must be a dict")
        pose_updates: dict[str, dict[str, Any]] = {}
        for actor_id, pdata in pose_raw.items():
            if not isinstance(pdata, dict) or pdata.get("pose") not in POSES:
                raise SchemaError(f"Invalid pose update in node '{k}' for '{actor_id}'")
            tp = pdata.get("target_prop")
            if tp is not None and not isinstance(tp, str):
                raise SchemaError(f"Invalid target_prop in node '{k}' for '{actor_id}'")
            pose_updates[str(actor_id)] = {"pose": pdata["pose"], "target_prop": tp}

        auto_effects = (
            normalize_state_delta(node["auto_effects"]) if node.get("auto_effects") is not None else None
        )

        choices_raw = node.get("choices", [])
        if not isinstance(choices_raw, list):
            raise SchemaError(f"nodes['{k}'].choices must be a list")
        choices: list[dict[str, Any]] = []
        for idx, ch in enumerate(choices_raw):
            if not isinstance(ch, dict):
                raise SchemaError(f"nodes['{k}'].choices[{idx}] must be a dict")
            cid = str(ch.get("id") or f"c_{idx + 1}").strip()
            label = str(ch.get("label") or "").strip()
            if not label:
                raise SchemaError(f"nodes['{k}'].choices[{idx}].label must be non-empty")
            style = ch.get("style", "normal")
            if style not in CHOICE_STYLES:
                raise SchemaError(f"nodes['{k}'].choices[{idx}].style invalid: {style!r}")
            check_raw = ch.get("check")
            check: dict[str, Any] | None = None
            if check_raw is not None:
                if not isinstance(check_raw, dict):
                    raise SchemaError(f"nodes['{k}'].choices[{idx}].check must be a dict")
                st_name = check_raw.get("stat")
                dc_val = check_raw.get("dc")
                if st_name not in STAT_KEYS or not _is_strict_int(dc_val):
                    raise SchemaError(f"Invalid check in choice '{cid}': {check_raw!r}")
                check = {"stat": st_name, "dc": dc_val}
            next_id = ch.get("next_node_id")
            if next_id is not None and next_id not in node_keys:
                raise SchemaError(f"Choice '{cid}' next_node_id '{next_id}' not in nodes")
            fail_id = ch.get("fail_node_id")
            if fail_id is not None and fail_id not in node_keys:
                raise SchemaError(f"Choice '{cid}' fail_node_id '{fail_id}' not in nodes")

            choices.append(
                {
                    "id": cid,
                    "label": label,
                    "style": style,
                    "check": check,
                    "required_item": ch.get("required_item"),
                    "next_node_id": next_id,
                    "fail_node_id": fail_id,
                    "effects": normalize_state_delta(ch["effects"]) if ch.get("effects") is not None else None,
                    "fail_effects": (
                        normalize_state_delta(ch["fail_effects"]) if ch.get("fail_effects") is not None else None
                    ),
                }
            )

        norm_nodes[nid] = {
            "node_id": nid,
            "text": str(node.get("text") or ""),
            "speaker_id": node.get("speaker_id"),
            "presentation_mode": pm,
            "pose_updates": pose_updates,
            "lighting": node.get("lighting"),
            "auto_effects": auto_effects,
            "choices": choices,
        }

    return {"root_node_id": root_node_id, "nodes": norm_nodes}


def validate_storylet(s: dict) -> dict:
    """Validate and normalize a Storylet dict."""
    if not isinstance(s, dict):
        raise SchemaError("Storylet must be a dict")
    sid = str(s.get("id") or "").strip()
    if not sid:
        raise SchemaError("Storylet.id must be a non-empty string")
    act_index = s.get("act_index", 1)
    if not _is_strict_int(act_index) or act_index not in (1, 2, 3):
        raise SchemaError(f"Storylet.act_index must be 1, 2, or 3, got {act_index!r}")
    title = str(s.get("title") or sid).strip()

    trig_raw = s.get("trigger") if isinstance(s.get("trigger"), dict) else {}
    req_flags = trig_raw.get("required_flags") if isinstance(trig_raw.get("required_flags"), dict) else {}
    trigger = {
        "room_id": trig_raw.get("room_id"),
        "prop_id": trig_raw.get("prop_id"),
        "npc_id": trig_raw.get("npc_id"),
        "min_hour": trig_raw.get("min_hour"),
        "max_hour": trig_raw.get("max_hour"),
        "required_flags": copy.deepcopy(req_flags),
        "required_items": _str_list(trig_raw.get("required_items", [])),
        "one_shot": bool(trig_raw.get("one_shot", False)),
    }

    rk_raw = s.get("relevant_keys") if isinstance(s.get("relevant_keys"), dict) else {}
    relevant_keys = {
        "flags": sorted(set(_str_list(rk_raw.get("flags", [])))),
        "items": sorted(set(_str_list(rk_raw.get("items", [])))),
        "affinity_with": sorted(set(_str_list(rk_raw.get("affinity_with", [])))),
        "extra": sorted(set(_str_list(rk_raw.get("extra", [])))),
    }

    priority = s.get("priority", 50)
    if not _is_strict_int(priority):
        raise SchemaError(f"Storylet.priority must be int, got {priority!r}")

    root_node_id = str(s.get("root_node_id") or "root").strip()
    nodes_raw = s.get("nodes")
    nodes: dict[str, Any] | None = None
    if nodes_raw is not None:
        validated_tree = validate_scene_node_tree({"root_node_id": root_node_id, "nodes": nodes_raw})
        root_node_id = validated_tree["root_node_id"]
        nodes = validated_tree["nodes"]

    return {
        "id": sid,
        "act_index": act_index,
        "title": title,
        "trigger": trigger,
        "relevant_keys": relevant_keys,
        "priority": priority,
        "is_intrusion": bool(s.get("is_intrusion", False)),
        "root_node_id": root_node_id,
        "nodes": nodes,
    }


def validate_iso_world(w: dict) -> dict:
    """Validate and return a normalized deep copy of an iso-world-v1 world dict."""
    if not isinstance(w, dict):
        raise SchemaError("World must be a dict")
    if w.get("format") != "iso-world-v1":
        raise SchemaError(f"Expected format 'iso-world-v1', got {w.get('format')!r}")
    wid = _validate_id(w.get("id"), "world id")

    meta_raw = w.get("meta")
    if not isinstance(meta_raw, dict):
        raise SchemaError("World 'meta' must be a dict")
    w_name = meta_raw.get("name")
    if not isinstance(w_name, str) or not w_name.strip():
        raise SchemaError("World 'meta.name' must be a non-empty string")
    tone_rating = meta_raw.get("tone_rating", "safe")
    if tone_rating not in TONE_RATINGS:
        raise SchemaError(f"World 'meta.tone_rating' invalid: {tone_rating!r}")
    meta: dict[str, Any] = {
        "name": w_name.strip(),
        "genre": str(meta_raw.get("genre") or "Đời thường").strip(),
        "tone_rating": tone_rating,
    }
    if isinstance(meta_raw.get("location_alias_map"), dict):
        meta["location_alias_map"] = {
            str(k): str(v) for k, v in meta_raw["location_alias_map"].items()
        }

    rules_raw = w.get("rules") if isinstance(w.get("rules"), dict) else {}
    rules = {
        "writing_style": str(rules_raw.get("writing_style") or ""),
        "world_rules": _str_list(rules_raw.get("world_rules", [])),
    }

    user_raw = w.get("user") if isinstance(w.get("user"), dict) else {}
    user = {
        "id": str(user_raw.get("id") or "user"),
        "name": str(user_raw.get("name") or "Người chơi"),
        "aliases": _str_list(user_raw.get("aliases", [])),
        "gender": str(user_raw.get("gender") or "không rõ"),
        "persona": str(user_raw.get("persona") or ""),
    }

    pd_raw = w.get("player_defaults")
    if not isinstance(pd_raw, dict):
        raise SchemaError("World 'player_defaults' must be a dict")
    for k in ("energy", "stress", "cash"):
        if not _is_strict_int(pd_raw.get(k)):
            raise SchemaError(f"player_defaults.{k} must be an int, got {pd_raw.get(k)!r}")
    p_stats_raw = pd_raw.get("stats")
    if not isinstance(p_stats_raw, dict):
        raise SchemaError("player_defaults.stats must be a dict")
    p_stats: dict[str, int] = {}
    for sk in STAT_KEYS:
        val = p_stats_raw.get(sk)
        if not _is_strict_int(val) or not (0 <= val <= 20):
            raise SchemaError(f"player_defaults.stats.{sk} must be int in 0..20, got {val!r}")
        p_stats[sk] = val
    player_defaults = {
        "energy": pd_raw["energy"],
        "stress": pd_raw["stress"],
        "cash": pd_raw["cash"],
        "stats": p_stats,
    }

    clock_raw = w.get("clock")
    if not isinstance(clock_raw, dict):
        raise SchemaError("World 'clock' must be a dict")
    c_mode = clock_raw.get("mode")
    if c_mode not in CLOCK_MODES:
        raise SchemaError(f"clock.mode invalid: {c_mode!r}")
    c_start = clock_raw.get("start")
    if not isinstance(c_start, dict):
        raise SchemaError("clock.start must be a dict")
    for ck in ("year", "month", "day", "hour", "minute"):
        if not _is_strict_int(c_start.get(ck)):
            raise SchemaError(f"clock.start.{ck} must be an int")
    if not (0 <= c_start["hour"] <= 23) or not (0 <= c_start["minute"] <= 59):
        raise SchemaError("clock.start hour/minute out of range")
    clock = {
        "mode": c_mode,
        "start": {k: c_start[k] for k in ("year", "month", "day", "hour", "minute")},
    }

    rooms_raw = w.get("rooms")
    if not isinstance(rooms_raw, dict) or not (4 <= len(rooms_raw) <= 8):
        cnt = len(rooms_raw) if isinstance(rooms_raw, dict) else type(rooms_raw).__name__
        raise SchemaError(f"World 'rooms' must be a dict with 4..8 rooms, got {cnt}")
    rooms: dict[str, dict[str, Any]] = {}
    for rkey, rdata in rooms_raw.items():
        rk = _validate_id(rkey, "room key")
        if not isinstance(rdata, dict):
            raise SchemaError(f"rooms['{rk}'] must be a dict")
        rid = str(rdata.get("id") or rk)
        if rid != rk:
            raise SchemaError(f"rooms['{rk}'].id mismatch: {rid!r}")
        kind = rdata.get("kind")
        if kind not in ROOM_KINDS:
            raise SchemaError(f"rooms['{rk}'].kind invalid: {kind!r}")
        privacy = rdata.get("privacy")
        if privacy not in ("public", "private"):
            raise SchemaError(f"rooms['{rk}'].privacy must be 'public' or 'private', got {privacy!r}")
        r_size = rdata.get("size")
        if (
            not isinstance(r_size, list)
            or len(r_size) != 2
            or not _is_strict_int(r_size[0])
            or not _is_strict_int(r_size[1])
            or not (3 <= r_size[0] <= 14)
            or not (3 <= r_size[1] <= 14)
        ):
            raise SchemaError(f"rooms['{rk}'].size must be [w, h] ints in 3..14, got {r_size!r}")
        props_raw = rdata.get("props", [])
        if not isinstance(props_raw, list):
            raise SchemaError(f"rooms['{rk}'].props must be a list")
        props: list[dict[str, Any]] = []
        for pidx, pr in enumerate(props_raw):
            if not isinstance(pr, dict):
                raise SchemaError(f"rooms['{rk}'].props[{pidx}] must be a dict")
            pid = str(pr.get("id") or f"{rk}_prop_{pidx + 1}").strip()
            atag = str(pr.get("asset_tag") or "").strip()
            if not atag:
                raise SchemaError(f"rooms['{rk}'].props[{pidx}].asset_tag required")
            props.append(
                {
                    "id": pid,
                    "asset_tag": atag,
                    "name": str(pr.get("name") or atag),
                    "is_missing": bool(pr.get("is_missing", False)),
                    "interactive": bool(pr.get("interactive", True)),
                    "locked_by": pr.get("locked_by"),
                    "contains_item": pr.get("contains_item"),
                }
            )
        rooms[rk] = {
            "id": rk,
            "name": str(rdata.get("name") or rk).strip(),
            "kind": kind,
            "privacy": privacy,
            "owner_id": rdata.get("owner_id"),
            "size": [r_size[0], r_size[1]],
            "desc": str(rdata.get("desc") or ""),
            "tags": _str_list(rdata.get("tags", [])),
            "lighting": str(rdata.get("lighting") or "warm_day"),
            "props": props,
        }

    room_ids = set(rooms.keys())

    opening_raw = w.get("opening")
    if not isinstance(opening_raw, dict):
        raise SchemaError("World 'opening' must be a dict")
    user_start_room = opening_raw.get("user_start_room") or opening_raw.get("user_start_location")
    if not isinstance(user_start_room, str) or user_start_room not in room_ids:
        raise SchemaError(f"opening.user_start_room '{user_start_room}' not in rooms")
    opening = {
        "user_start_room": user_start_room,
        "tension": int(opening_raw.get("tension", 1)),
        "channel_type": str(opening_raw.get("channel_type") or "in_person"),
        "narration": str(opening_raw.get("narration") or ""),
    }

    doors_raw = w.get("doors")
    if not isinstance(doors_raw, list):
        raise SchemaError("World 'doors' must be a list")
    adj: dict[str, set[str]] = {rk: set() for rk in room_ids}
    doors: list[dict[str, Any]] = []
    for didx, d in enumerate(doors_raw):
        if not isinstance(d, dict):
            raise SchemaError(f"doors[{didx}] must be a dict")
        ra = d.get("room_a")
        rb = d.get("room_b")
        if ra not in room_ids or rb not in room_ids:
            raise SchemaError(f"doors[{didx}] references unknown room: {ra!r}, {rb!r}")
        if ra == rb:
            raise SchemaError(f"doors[{didx}] self-loop on '{ra}'")
        locked = d.get("locked")
        if not isinstance(locked, bool):
            raise SchemaError(f"doors[{didx}].locked must be a bool")
        lock_dc = d.get("lock_dc")
        if lock_dc is not None and not _is_strict_int(lock_dc):
            raise SchemaError(f"doors[{didx}].lock_dc must be int or None")
        adj[ra].add(rb)
        adj[rb].add(ra)
        doors.append(
            {
                "id": str(d.get("id") or f"door_{ra}_{rb}"),
                "room_a": ra,
                "room_b": rb,
                "locked": locked,
                "key_item_id": d.get("key_item_id"),
                "lock_dc": lock_dc,
            }
        )

    # BFS connectivity check
    visited = {user_start_room}
    q = deque([user_start_room])
    while q:
        cur = q.popleft()
        for nxt in adj[cur]:
            if nxt not in visited:
                visited.add(nxt)
                q.append(nxt)
    if len(visited) != len(rooms):
        raise SchemaError("Rooms are not fully connected by doors")

    acts_raw = w.get("acts_spine")
    if not isinstance(acts_raw, list) or len(acts_raw) != 3:
        raise SchemaError("World 'acts_spine' must be a list of exactly 3 acts")
    if [a.get("act_index") for a in acts_raw if isinstance(a, dict)] != [1, 2, 3]:
        raise SchemaError("acts_spine must have act_index [1, 2, 3]")
    acts_spine: list[dict[str, Any]] = []
    for a in acts_raw:
        title = str(a.get("title") or "").strip()
        goal = str(a.get("goal") or "").strip()
        adv = a.get("advance_condition")
        if not title or not goal or not isinstance(adv, dict):
            raise SchemaError(f"Act {a.get('act_index')} requires title, goal, and advance_condition dict")
        acts_spine.append(
            {
                "act_index": a["act_index"],
                "title": title,
                "goal": goal,
                "advance_condition": copy.deepcopy(adv),
                "climax_hint": str(a.get("climax_hint") or ""),
            }
        )

    endings_raw = w.get("endings")
    if not isinstance(endings_raw, list) or not endings_raw:
        raise SchemaError("World 'endings' must be a non-empty list")
    endings: list[dict[str, Any]] = []
    for idx, e in enumerate(endings_raw):
        if not isinstance(e, dict):
            raise SchemaError(f"endings[{idx}] must be a dict")
        eid = str(e.get("id") or "").strip()
        etitle = str(e.get("title") or "").strip()
        cond = e.get("condition")
        if not eid or not etitle or not isinstance(cond, dict):
            raise SchemaError(f"endings[{idx}] requires id, title, condition dict")
        endings.append(
            {
                "id": eid,
                "title": etitle,
                "condition": copy.deepcopy(cond),
                "epilogue_prompt": str(e.get("epilogue_prompt") or ""),
            }
        )

    mp_raw = w.get("missing_props", [])
    if not isinstance(mp_raw, list):
        raise SchemaError("World 'missing_props' must be a list")
    missing_props = [validate_missing_prop_spec(sp, room_ids=room_ids) for sp in mp_raw]

    chars_raw = w.get("characters")
    if not isinstance(chars_raw, dict):
        raise SchemaError("World 'characters' must be a dict")
    characters: dict[str, dict[str, Any]] = {}
    for ckey, cval in chars_raw.items():
        norm_c = validate_iso_char(cval)
        if str(ckey) != norm_c["id"]:
            raise SchemaError(f"characters['{ckey}'] id mismatch: {norm_c['id']!r}")
        characters[norm_c["id"]] = norm_c

    cast_raw = w.get("cast")
    if not isinstance(cast_raw, list):
        raise SchemaError("World 'cast' must be a list")
    cast: list[dict[str, Any]] = []
    for idx, entry in enumerate(cast_raw):
        if not isinstance(entry, dict):
            raise SchemaError(f"cast[{idx}] must be a dict")
        cid = str(entry.get("char_id") or "").strip()
        if cid not in characters:
            raise SchemaError(f"cast[{idx}].char_id '{cid}' missing from characters")
        start_room = entry.get("start_room") or entry.get("start_location")
        if not isinstance(start_room, str) or start_room not in room_ids:
            raise SchemaError(f"cast[{idx}].start_room '{start_room}' not in rooms")
        cast.append(
            {
                "char_id": cid,
                "role_in_world": str(entry.get("role_in_world") or ""),
                "start_room": start_room,
                "start_outfit": str(entry.get("start_outfit") or "default"),
                "goal": str(entry.get("goal") or ""),
                "secrets": copy.deepcopy(entry.get("secrets") or []),
            }
        )

    rel_raw = w.get("relationships") if isinstance(w.get("relationships"), dict) else {}
    relationships = {
        "auto_bootstrap": bool(rel_raw.get("auto_bootstrap", True)),
        "edges": copy.deepcopy(rel_raw.get("edges") if isinstance(rel_raw.get("edges"), dict) else {}),
    }

    init_facts = [
        copy.deepcopy(f) for f in (w.get("initial_facts") or []) if isinstance(f, dict)
    ]
    lore = [copy.deepcopy(l) for l in (w.get("lore") or []) if isinstance(l, dict)]

    return {
        "format": "iso-world-v1",
        "id": wid,
        "meta": meta,
        "rules": rules,
        "user": user,
        "player_defaults": player_defaults,
        "clock": clock,
        "rooms": rooms,
        "doors": doors,
        "acts_spine": acts_spine,
        "endings": endings,
        "missing_props": missing_props,
        "characters": characters,
        "cast": cast,
        "relationships": relationships,
        "initial_facts": init_facts,
        "lore": lore,
        "opening": opening,
    }


def _synthesize_stats(char_v2: dict) -> dict[str, int]:
    existing = char_v2.get("stats")
    if isinstance(existing, dict) and all(
        sk in existing and _is_strict_int(existing[sk]) and 0 <= existing[sk] <= 20
        for sk in STAT_KEYS
    ):
        return {sk: existing[sk] for sk in STAT_KEYS}

    core = char_v2.get("core") or {}
    voice = char_v2.get("voice") or {}
    modules = char_v2.get("modules") or []
    parts = [
        str(core.get("one_liner") or ""),
        " ".join(_str_list(core.get("personality", []))),
        str(core.get("values") or ""),
        str(voice.get("tone") or ""),
    ]
    for m in modules:
        if isinstance(m, dict):
            parts.append(str(m.get("content") or ""))
    corpus = " ".join(parts).lower()

    groups = {
        "charm": [
            ("mồm mép", "lầy lội", "cà khịa"),
            ("duyên", "hài hước", "trêu"),
            ("tinh tế", "thân thiện", "giao tiếp"),
            ("bí thư", "chủ nhiệm", "tập thể"),
            ("nhanh", "ví von"),
        ],
        "logic": [
            ("nghiêm túc", "cầu toàn", "kỹ tính"),
            ("số liệu", "tiến độ", "hạn chót", "deadline"),
            ("code", "lập trình", "cntt", "build"),
            ("lớp trưởng", "kế hoạch", "đồ án"),
            ("thẩm mỹ", "bố cục", "thiết kế"),
        ],
        "stealth": [
            ("trốn", "núp", "lén"),
            ("nói dối", "chột dạ", "giấu"),
            ("ngủ", "lười", "uể oải"),
            ("quan sát", "nheo", "nhiếp ảnh", "máy ảnh"),
            ("bí mật", "tầng 4"),
        ],
        "will": [
            ("chữ tín", "công bằng", "trách nhiệm"),
            ("kỷ luật", "nghiêm khắc", "cứng rắn"),
            ("điềm tĩnh", "tháo vát", "che chở"),
            ("xuyên đêm", "cứu bồ", "nghĩa khí"),
            ("tự ái", "ghét"),
        ],
    }

    stats: dict[str, int] = {}
    for sk in STAT_KEYS:
        bonus = 0
        for grp in groups[sk]:
            if any(kw in corpus for kw in grp):
                bonus += 1
        stats[sk] = max(8, min(16, 10 + min(6, bonus)))
    return stats


def _synthesize_voxel_look(char_v2: dict) -> dict[str, Any]:
    existing = char_v2.get("voxel_look")
    if isinstance(existing, dict):
        try:
            hs = existing.get("height_scale")
            if _is_number(hs) and 0.7 <= float(hs) <= 1.3 and isinstance(existing.get("hair_style"), str):
                return {
                    "skin": _validate_hex(existing.get("skin"), "skin"),
                    "hair": _validate_hex(existing.get("hair"), "hair"),
                    "hair_style": existing["hair_style"].strip() or "short",
                    "outfit_top": _validate_hex(existing.get("outfit_top"), "outfit_top"),
                    "outfit_bottom": _validate_hex(existing.get("outfit_bottom"), "outfit_bottom"),
                    "accent": _validate_hex(existing.get("accent"), "accent"),
                    "height_scale": round(float(hs), 3),
                }
        except SchemaError:
            pass

    app = char_v2.get("appearance") or {}
    summary = str(app.get("summary") or "")
    outfits = app.get("outfits") or {}
    default_outfit = str(outfits.get("default") or "")
    text_app = (summary + " " + default_outfit).lower()
    gender = str((char_v2.get("meta") or {}).get("gender") or "").lower()

    if "ngăm" in text_app:
        skin = "#e0ac69"
    elif "trắng" in text_app and "da" in text_app:
        skin = "#f5cba7"
    else:
        skin = "#f1c27d"

    hair_scan = summary.lower()
    if "tóc" in hair_scan:
        hair_scan = hair_scan[hair_scan.find("tóc") :]
    if "nâu hạt dẻ" in hair_scan or "hạt dẻ" in hair_scan or "nâu" in hair_scan:
        hair = "#6b3e26"
    elif "đen" in hair_scan:
        hair = "#1c1917"
    elif "vàng" in hair_scan:
        hair = "#d4a347"
    elif "đỏ" in hair_scan:
        hair = "#7f1d1d"
    elif "trắng" in hair_scan or "bạch kim" in hair_scan:
        hair = "#e5e7eb"
    else:
        hair = "#1c1917"

    if "đuôi ngựa" in text_app or "buộc" in text_app:
        hair_style = "ponytail"
    elif "nửa đầu" in text_app or "càng cua" in text_app:
        hair_style = "half_up"
    elif "ngắn" in text_app or "uốn cúp" in text_app:
        hair_style = "bob"
    elif "bù xù" in text_app or "rối" in text_app:
        hair_style = "messy"
    elif "dài" in text_app:
        hair_style = "long"
    else:
        hair_style = "short" if gender == "nam" else "long"

    top_scan = (default_outfit + " " + summary).lower()
    if "xanh thanh niên" in top_scan or "xanh chi đoàn" in top_scan:
        outfit_top = "#2563eb"
    elif "xanh" in top_scan:
        outfit_top = "#3b82f6"
    elif "kem" in top_scan or "linen" in top_scan:
        outfit_top = "#f5e6c8"
    elif "trắng" in top_scan:
        outfit_top = "#f8fafc"
    elif "đen" in top_scan:
        outfit_top = "#1f2937"
    elif "đỏ" in top_scan:
        outfit_top = "#b91c1c"
    elif "vàng" in top_scan:
        outfit_top = "#eab308"
    elif "hồng" in top_scan:
        outfit_top = "#ec4899"
    elif "xám" in top_scan or "bạc" in top_scan:
        outfit_top = "#6b7280"
    else:
        outfit_top = "#475569"

    bot_scan = default_outfit.lower()
    for marker in ("quần", "váy", "chân váy"):
        idx = bot_scan.find(marker)
        if idx != -1:
            bot_scan = bot_scan[idx:]
            break
    if "jean" in bot_scan:
        outfit_bottom = "#1d4ed8"
    elif "tây" in bot_scan or "tối màu" in bot_scan or "đen" in bot_scan:
        outfit_bottom = "#1e293b"
    elif "short" in bot_scan or "đùi" in bot_scan or "jogger" in bot_scan:
        outfit_bottom = "#334155"
    elif "váy" in bot_scan or "midi" in bot_scan:
        outfit_bottom = "#78716c"
    else:
        outfit_bottom = "#1e293b"

    accent_scan = (text_app + " " + " ".join(str(m.get("content") or "") for m in (char_v2.get("modules") or []) if isinstance(m, dict))).lower()
    if "máy ảnh" in accent_scan or "fujifilm" in accent_scan:
        accent = "#475569"
    elif "kính" in accent_scan:
        accent = "#94a3b8"
    elif "trà đào" in accent_scan:
        accent = "#fb923c"
    elif "bìa xanh" in accent_scan or "sổ" in accent_scan:
        accent = "#0284c7"
    elif "logo game" in accent_scan or "game" in accent_scan:
        accent = "#22c55e"
    elif "thẻ sinh viên" in accent_scan:
        accent = "#38bdf8"
    else:
        accent = "#f59e0b"

    m_height = re.search(r"1m(\d{2})", summary.lower())
    if m_height:
        cm = 100 + int(m_height.group(1))
        height_scale = round(max(0.85, min(1.15, cm / 165.0)), 2)
    elif "cao ráo" in summary.lower():
        height_scale = 1.04
    elif "nhỏ nhắn" in summary.lower():
        height_scale = 0.92
    else:
        height_scale = 1.0

    return {
        "skin": skin,
        "hair": hair,
        "hair_style": hair_style,
        "outfit_top": outfit_top,
        "outfit_bottom": outfit_bottom,
        "accent": accent,
        "height_scale": height_scale,
    }


def _convert_rplab_v1_char_to_v2(c: dict) -> tuple[dict, list[dict]]:
    if c.get("hu_cau") is not True:
        raise SchemaError("rplab-char-1 'hu_cau' must be strictly True")
    cid = _validate_id(c.get("id"), "rplab-char-1 id")
    ten = str(c.get("ten") or "").strip()
    if not ten:
        raise SchemaError("rplab-char-1 'ten' required")
    tinh_cach = _str_list(c.get("tinh_cach"))
    if not tinh_cach:
        raise SchemaError("rplab-char-1 'tinh_cach' required")
    one_liner = str(c.get("mot_dong") or tinh_cach[0]).strip()[:140]

    noi = c.get("noi") or {}
    xh = noi.get("xung_ho") or {}
    mac_dinh_raw = xh.get("mac_dinh")
    if isinstance(mac_dinh_raw, dict) and mac_dinh_raw.get("self") and mac_dinh_raw.get("call"):
        mac_dinh = {"self": str(mac_dinh_raw["self"]), "call": str(mac_dinh_raw["call"])}
    elif isinstance(mac_dinh_raw, str) and "-" in mac_dinh_raw:
        parts = [p.strip() for p in mac_dinh_raw.split("-", 1)]
        mac_dinh = {"self": parts[0] or "tui", "call": parts[1] or "ông/bà"}
    else:
        mac_dinh = {"self": "tui", "call": "ông/bà"}

    tone = str(noi.get("giong") or "").strip()
    samples = _str_list(noi.get("vi_du", []))[:5]
    tics = _str_list(noi.get("cau_cua_mieng", []))
    them = c.get("them") or {}
    modules: list[dict[str, Any]] = []
    if str(c.get("qua_khu") or "").strip():
        qk = str(c["qua_khu"]).strip()
        modules.append(
            {
                "id": "mod_qua_khu",
                "type": "backstory",
                "activation": "keyword",
                "when": "Khi nhắc tới quá khứ của nhân vật.",
                "keywords": ["quá khứ", "ngày xưa"],
                "content": qk[:300],
            }
        )
    for k, v in them.items():
        if k in ("mo_ta", "huong_dan_cu", "ho_so_day_du") or not str(v or "").strip():
            continue
        modules.append(
            {
                "id": f"mod_{_slug(k)}",
                "type": "habit" if k in ("so_thich", "thoi_quen", "do_vat") else "backstory",
                "activation": "keyword",
                "when": f"Khi nhắc tới {k.replace('_', ' ')}.",
                "keywords": [k.replace("_", " ")],
                "content": str(v).strip()[:300],
            }
        )

    secrets: list[dict[str, Any]] = []
    for idx, b in enumerate(c.get("bi_mat") or []):
        if isinstance(b, dict) and str(b.get("noi_dung") or "").strip():
            secrets.append(
                {
                    "id": f"sec_{cid}_{idx + 1:02d}",
                    "text": str(b["noi_dung"]).strip(),
                    "known_by": [cid],
                    "reveal_when": str(b.get("lo_khi") or "").strip(),
                    "keywords": [],
                }
            )

    v2 = {
        "format": "char-v2",
        "id": cid,
        "meta": {
            "name": ten,
            "aliases": _str_list(c.get("goi") or [ten.split()[-1]]),
            "age": c.get("tuoi"),
            "gender": str(c.get("gioi_tinh") or "không rõ"),
            "fictional": True,
        },
        "core": {
            "one_liner": one_liner,
            "personality": tinh_cach,
            "values": str(c.get("gia_tri") or c.get("muc_tieu") or "").strip(),
            "boundaries": _str_list(c.get("gioi_han", [])),
        },
        "voice": {
            "dialect": "Nam Bộ",
            "tone": tone,
            "pronoun_defaults": {
                "close_peer": mac_dinh,
                "acquaintance": dict(mac_dinh),
                "elder_formal": dict(DEFAULT_PRONOUN_DEFAULTS["elder_formal"]),
                "angry": {"self": "tôi", "call": mac_dinh["call"]},
            },
            "modes": {
                "in_person": {
                    "style": "Văn nói trực tiếp, lồng cử chỉ vào văn xuôi.",
                    "tics": tics,
                    "samples": samples,
                },
                "texting": {
                    "style": "Tin nhắn ngắn tách dòng.",
                    "samples": [samples[0]] if samples else [],
                },
            },
        },
        "appearance": {
            "summary": str(c.get("ngoai_hinh") or "").strip(),
            "outfits": {"default": "Trang phục thường ngày"},
        },
        "modules": modules,
    }
    return v2, secrets


def upgrade_char_v2_to_iso(char_obj: dict) -> dict:
    """Upgrade char-v2 (or rplab-char-1, or pass-through iso-char-v1) into iso-char-v1."""
    if not isinstance(char_obj, dict):
        raise SchemaError("Character input must be a dict")
    fmt = char_obj.get("format")
    if fmt == "iso-char-v1":
        return validate_iso_char(copy.deepcopy(char_obj))

    extra_secrets: list[dict] = []
    if fmt == "rplab-char-1":
        v2, extra_secrets = _convert_rplab_v1_char_to_v2(char_obj)
    elif fmt == "char-v2":
        v2 = copy.deepcopy(char_obj)
        _validate_id(v2.get("id"), "char-v2 id")
        meta = v2.get("meta")
        if not isinstance(meta, dict) or meta.get("fictional") is not True:
            raise SchemaError("char-v2 'meta.fictional' must be strictly True")
        if not isinstance(meta.get("name"), str) or not meta["name"].strip():
            raise SchemaError("char-v2 'meta.name' required")
        core = v2.get("core")
        if not isinstance(core, dict) or not str(core.get("one_liner") or "").strip():
            raise SchemaError("char-v2 'core.one_liner' required")
        if not _str_list(core.get("personality")):
            raise SchemaError("char-v2 'core.personality' required")
    else:
        raise SchemaError(f"Unsupported character format: {fmt!r}")

    voice = copy.deepcopy(v2.get("voice") if isinstance(v2.get("voice"), dict) else {})
    voice["pronoun_defaults"] = _normalize_pronoun_defaults(
        voice.get("pronoun_defaults") or DEFAULT_PRONOUN_DEFAULTS
    )

    stats = _synthesize_stats(v2)
    voxel_look = _synthesize_voxel_look(v2)

    secrets = list(v2.get("secrets") or [])
    seen_sec_ids = {s.get("id") for s in secrets if isinstance(s, dict)}
    for s in extra_secrets:
        if s.get("id") not in seen_sec_ids:
            secrets.append(s)
            seen_sec_ids.add(s.get("id"))

    iso = {
        "format": "iso-char-v1",
        "id": v2["id"],
        "meta": v2["meta"],
        "core": v2["core"],
        "voice": voice,
        "stats": stats,
        "voxel_look": voxel_look,
        "schedule": list(v2.get("schedule") or []),
        "inventory": _str_list(v2.get("inventory", [])),
        "secrets": secrets,
        "appearance": v2.get("appearance") or {"summary": "", "outfits": {}},
        "modules": list(v2.get("modules") or []),
    }
    return validate_iso_char(iso)


def _infer_room_kind(room_id: str, name: str, desc: str, hub_id: str) -> str:
    txt = f"{room_id} {name} {desc}".lower()
    if any(k in txt for k in ("ktx", "phong_ngu", "phòng ngủ", "giường", "phong_3")):
        return "bedroom"
    if any(k in txt for k in ("can_tin", "căn tin", "cafe", "cà phê", "an_vat", "ăn vặt", "bep", "bếp")):
        return "kitchen"
    if any(k in txt for k in ("thu_vien", "thư viện", "tang_4", "tầng 4", "clb", "nha_a", "nhà a", "giang_duong", "giảng đường")):
        return "study"
    if any(k in txt for k in ("tam", "tắm", "wc", "toilet")):
        return "bathroom"
    if any(k in txt for k in ("hanh_lang", "hành lang")):
        return "hallway"
    if room_id == hub_id:
        return "foyer"
    if any(k in txt for k in ("ho_sen", "hồ sen", "san_", "sân")):
        return "living"
    return "other"


def _starter_props_for_kind(room_id: str, kind: str) -> list[dict[str, Any]]:
    templates: dict[str, list[tuple[str, str, bool]]] = {
        "bedroom": [
            ("bed_double_2x2", "Giường ngủ", True),
            ("wardrobe_tall_2x1", "Tủ quần áo", True),
        ],
        "study": [
            ("desk_mahogany_2x1", "Bàn học / làm việc", True),
            ("office_chair_1x1", "Ghế ngồi", True),
            ("bookshelf_tall_2x1", "Kệ sách", True),
        ],
        "kitchen": [
            ("dining_table_2x1", "Bàn dài", True),
            ("dining_chair_1x1", "Ghế gỗ", True),
        ],
        "bathroom": [
            ("sink_marble_1x1", "Chậu rửa", True),
            ("toilet_porcelain_1x1", "Bồn sứ", False),
        ],
        "foyer": [
            ("sofa_velvet_2x1", "Ghế chờ", True),
            ("coffee_table_glass", "Bàn thấp", True),
            ("lamp_floor_warm", "Đèn đứng", True),
        ],
        "living": [
            ("sofa_velvet_2x1", "Ghế dài", True),
            ("coffee_table_glass", "Bàn gỗ / kính", True),
        ],
    }
    specs = templates.get(kind, [("coffee_table_glass", "Bàn nhỏ", True), ("lamp_floor_warm", "Đèn cây", True)])
    props: list[dict[str, Any]] = []
    for idx, (atag, label, interactive) in enumerate(specs):
        props.append(
            {
                "id": f"{room_id}_{atag}_{idx + 1}",
                "asset_tag": atag,
                "name": label,
                "is_missing": False,
                "interactive": interactive,
                "locked_by": None,
                "contains_item": None,
            }
        )
    return props


def _resolve_char_from_dirs(cid: str, search_dirs: list[str]) -> dict | None:
    for d in search_dirs:
        if not d:
            continue
        p = os.path.join(d, f"{cid}.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                return json.load(f)
    return None


def upgrade_world_v2_to_iso(world_obj: dict, search_dirs: list[str] | None = None) -> dict:
    """Upgrade a world-v2 (or pass-through iso-world-v1) dict into validated iso-world-v1."""
    if not isinstance(world_obj, dict):
        raise SchemaError("World input must be a dict")
    fmt = world_obj.get("format")
    if fmt == "iso-world-v1":
        return validate_iso_world(copy.deepcopy(world_obj))
    if fmt != "world-v2":
        raise SchemaError(f"Unsupported world format: {fmt!r}")

    wid = _validate_id(world_obj.get("id"), "world id")
    if search_dirs is None:
        here = os.path.dirname(os.path.abspath(__file__))
        search_dirs = [
            os.path.normpath(os.path.join(here, "..", "samples", "chars")),
            os.path.join(here, "samples", "chars"),
        ]

    # Collect raw characters
    chars_raw: dict[str, dict] = {}
    raw_chars_field = world_obj.get("characters")
    if isinstance(raw_chars_field, dict):
        for k, v in raw_chars_field.items():
            if isinstance(v, dict):
                chars_raw[str(v.get("id") or k)] = v
    elif isinstance(raw_chars_field, list):
        for v in raw_chars_field:
            if isinstance(v, dict) and v.get("id"):
                chars_raw[str(v["id"])] = v

    raw_cast = list(world_obj.get("cast") or [])
    if not raw_cast and chars_raw:
        locs_keys = list((world_obj.get("locations") or {}).keys())
        default_loc = (world_obj.get("opening") or {}).get("user_start_location") or (
            locs_keys[0] if locs_keys else "foyer"
        )
        for cid in chars_raw:
            raw_cast.append(
                {
                    "char_id": cid,
                    "role_in_world": "",
                    "start_location": default_loc,
                    "start_outfit": "default",
                    "goal": "",
                    "secrets": [],
                }
            )

    iso_characters: dict[str, dict[str, Any]] = {}
    for entry in raw_cast:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("char_id") or "").strip()
        if not cid:
            raise SchemaError("cast entry missing char_id")
        c_src = entry.get("char") or entry.get("character") or chars_raw.get(cid)
        if c_src is None:
            c_src = _resolve_char_from_dirs(cid, search_dirs)
        if c_src is None:
            raise SchemaError(f"Character '{cid}' not found in world or search_dirs")
        iso_c = upgrade_char_v2_to_iso(c_src)
        existing_ids = {s["id"] for s in iso_c["secrets"]}
        for idx, sec in enumerate(entry.get("secrets") or []):
            if isinstance(sec, dict) and str(sec.get("text") or "").strip():
                sid = str(sec.get("id") or f"sec_{cid}_w_{idx + 1:02d}").strip()
                if sid not in existing_ids:
                    iso_c["secrets"].append(
                        {
                            "id": sid,
                            "text": str(sec["text"]).strip(),
                            "known_by": _str_list(sec.get("known_by", [cid])),
                            "reveal_when": str(sec.get("reveal_when") or ""),
                            "keywords": _str_list(sec.get("keywords", [])),
                        }
                    )
                    existing_ids.add(sid)
        iso_characters[cid] = iso_c

    locations_raw = world_obj.get("locations") or {}
    if not isinstance(locations_raw, dict) or not locations_raw:
        raise SchemaError("world-v2 'locations' must be a non-empty dict")

    locations: dict[str, dict[str, Any]] = {}
    for lid, ldata in locations_raw.items():
        if not isinstance(ldata, dict):
            continue
        clean_lid = _validate_id(lid, "location id")
        locations[clean_lid] = {
            "id": clean_lid,
            "name": str(ldata.get("name") or clean_lid),
            "parent": ldata.get("parent"),
            "desc": str(ldata.get("desc") or ""),
            "tags": _str_list(ldata.get("tags", [])),
            "travel_min": int(ldata.get("travel_min") or 2),
            "lore_ids": _str_list(ldata.get("lore_ids", [])),
        }

    opening_raw = world_obj.get("opening") if isinstance(world_obj.get("opening"), dict) else {}
    opening_loc = str(opening_raw.get("user_start_location") or opening_raw.get("user_start_room") or "")
    if opening_loc not in locations:
        opening_loc = next(iter(locations.keys()))
    cast_start_locs = [
        str(e.get("start_location") or e.get("start_room") or opening_loc) for e in raw_cast
    ]

    parent_counts = Counter(
        loc["parent"] for loc in locations.values() if loc.get("parent")
    )
    container_roots = {
        lid
        for lid, loc in locations.items()
        if loc.get("parent") is None
        and ("campus" in loc["tags"] or parent_counts[lid] >= 2)
        and lid != opening_loc
        and lid not in cast_start_locs
    }

    non_roots = [lid for lid in locations if lid not in container_roots]
    if len(non_roots) < 4:
        non_roots = list(locations.keys())

    if len(non_roots) <= 8:
        selected_ids = list(non_roots)
        pad_names = [
            ("hanh_lang", "Hành lang chung", "hallway"),
            ("phong_chung", "Phòng sinh hoạt", "living"),
            ("san_truong_phu", "Sân phụ", "other"),
            ("kho_nho", "Kho nhỏ", "storage"),
        ]
        p_idx = 0
        while len(selected_ids) < 4 and p_idx < len(pad_names):
            pid, pname, _ = pad_names[p_idx]
            p_idx += 1
            if pid not in locations:
                locations[pid] = {
                    "id": pid,
                    "name": pname,
                    "parent": None,
                    "desc": pname,
                    "tags": ["public"],
                    "travel_min": 2,
                    "lore_ids": [],
                }
                selected_ids.append(pid)
    else:
        pinned: list[str] = []
        for candidate in [opening_loc] + cast_start_locs:
            if candidate in locations and candidate not in pinned:
                pinned.append(candidate)
        if len(pinned) > 8:
            pinned = pinned[:8]

        corpus_parts: list[str] = []
        for entry in raw_cast:
            corpus_parts.append(str(entry.get("goal") or ""))
            for sec in entry.get("secrets") or []:
                if isinstance(sec, dict):
                    corpus_parts.append(str(sec.get("text") or ""))
                    corpus_parts.append(str(sec.get("reveal_when") or ""))
        for fct in world_obj.get("initial_facts") or []:
            if isinstance(fct, dict):
                corpus_parts.append(str(fct.get("text") or ""))
        corpus = " ".join(corpus_parts).lower()

        scored: list[tuple[int, int, str]] = []
        for idx, lid in enumerate(non_roots):
            if lid in pinned:
                continue
            loc = locations[lid]
            score = 0
            if "private_possible" in loc["tags"]:
                score += 50
            short_name = loc["name"].split("(")[0].strip().lower()
            if lid in corpus or (short_name and short_name in corpus):
                score += 30
            if "indoor" in loc["tags"]:
                score += 20
            score += 2 * len(loc["lore_ids"])
            scored.append((-score, idx, lid))
        scored.sort()
        slots_left = 8 - len(pinned)
        selected_ids = pinned + [lid for _, _, lid in scored[:slots_left]]

    selected_set = set(selected_ids)
    hub_id = opening_loc if opening_loc in selected_set else selected_ids[0]

    # Build children adjacency for downward alias resolution
    children_map: dict[str, list[str]] = {lid: [] for lid in locations}
    for lid, loc in locations.items():
        par = loc.get("parent")
        if par in children_map:
            children_map[par].append(lid)

    alias_map: dict[str, str] = {}
    for lid in locations:
        if lid in selected_set:
            alias_map[lid] = lid
            continue
        # Walk up parents
        cur = locations[lid].get("parent")
        seen_up = {lid}
        mapped = None
        while cur and cur in locations and cur not in seen_up:
            seen_up.add(cur)
            if cur in selected_set:
                mapped = cur
                break
            cur = locations[cur].get("parent")
        # Walk down children BFS
        if mapped is None:
            q = deque(children_map.get(lid, []))
            seen_down = {lid}
            while q:
                ch = q.popleft()
                if ch in seen_down:
                    continue
                seen_down.add(ch)
                if ch in selected_set:
                    mapped = ch
                    break
                q.extend(children_map.get(ch, []))
        alias_map[lid] = mapped or hub_id

    # Convert selected rooms
    cast_by_room: dict[str, list[str]] = {}
    for entry in raw_cast:
        orig_loc = str(entry.get("start_location") or entry.get("start_room") or opening_loc)
        mapped_room = alias_map.get(orig_loc, hub_id)
        cast_by_room.setdefault(mapped_room, []).append(str(entry["char_id"]))

    rooms: dict[str, dict[str, Any]] = {}
    for rid in selected_ids:
        loc = locations[rid]
        tags = loc["tags"]
        privacy = "private" if ("private_possible" in tags and "public" not in tags) else "public"
        owner_id: str | None = None
        if privacy == "private":
            if cast_by_room.get(rid):
                owner_id = cast_by_room[rid][0]
            else:
                desc_lower = loc["desc"].lower()
                for cid, cobj in iso_characters.items():
                    short_c = cobj["meta"]["name"].split()[-1].lower()
                    if cid in desc_lower or short_c in desc_lower:
                        owner_id = cid
                        break
        kind = _infer_room_kind(rid, loc["name"], loc["desc"], hub_id)
        props = _starter_props_for_kind(rid, kind)
        size = [6, 6] if (rid == hub_id or (kind in ("study", "bedroom") and len(props) >= 2)) else [5, 5]
        rooms[rid] = {
            "id": rid,
            "name": loc["name"],
            "kind": kind,
            "privacy": privacy,
            "owner_id": owner_id,
            "size": size,
            "desc": loc["desc"],
            "tags": list(tags),
            "lighting": "warm_day",
            "props": props,
        }

    # Build low-degree connected doors graph
    uf_parent = {rid: rid for rid in selected_ids}

    def uf_find(x: str) -> str:
        while uf_parent[x] != x:
            uf_parent[x] = uf_parent[uf_parent[x]]
            x = uf_parent[x]
        return x

    def uf_union(a: str, b: str) -> None:
        ra, rb = uf_find(a), uf_find(b)
        if ra != rb:
            uf_parent[rb] = ra

    deg = {rid: 0 for rid in selected_ids}
    doors: list[dict[str, Any]] = []
    added_pairs: set[tuple[str, str]] = set()

    def _add_door(a: str, b: str) -> None:
        pair = (min(a, b), max(a, b))
        if pair in added_pairs or a == b:
            return
        added_pairs.add(pair)
        deg[a] += 1
        deg[b] += 1
        uf_union(a, b)
        doors.append(
            {
                "id": f"door_{a}_{b}",
                "room_a": a,
                "room_b": b,
                "locked": False,
                "key_item_id": None,
                "lock_dc": None,
            }
        )

    for rid in selected_ids:
        par = locations[rid].get("parent")
        if par in selected_set and par != rid and deg[rid] < 3 and deg[par] < 3:
            if uf_find(rid) != uf_find(par):
                _add_door(par, rid)

    order_partners = [hub_id] + [r for r in selected_ids if r != hub_id]
    for rid in selected_ids:
        if uf_find(rid) == uf_find(hub_id):
            continue
        partner = next(
            (p for p in order_partners if uf_find(p) == uf_find(hub_id) and deg[p] < 3),
            None,
        )
        if partner is None:
            partner = next(
                (p for p in order_partners if uf_find(p) == uf_find(hub_id) and deg[p] < 4),
                hub_id,
            )
        _add_door(partner, rid)

    # Remap cast and synthesize NPC schedules
    iso_cast: list[dict[str, Any]] = []
    private_rooms = [rid for rid, r in rooms.items() if r["privacy"] == "private"]
    secondary_fallback = private_rooms[0] if private_rooms else (selected_ids[1] if len(selected_ids) > 1 else hub_id)

    for entry in raw_cast:
        cid = str(entry["char_id"])
        orig_loc = str(entry.get("start_location") or entry.get("start_room") or opening_loc)
        mapped_start = alias_map.get(orig_loc, hub_id)
        iso_cast.append(
            {
                "char_id": cid,
                "role_in_world": str(entry.get("role_in_world") or ""),
                "start_room": mapped_start,
                "start_outfit": str(entry.get("start_outfit") or "default"),
                "goal": str(entry.get("goal") or ""),
                "secrets": copy.deepcopy(entry.get("secrets") or []),
            }
        )
        if not iso_characters[cid]["schedule"]:
            sec_room = next(
                (r for r in selected_ids if r != mapped_start and r != hub_id),
                secondary_fallback,
            )
            if mapped_start == sec_room:
                sec_room = hub_id
            start_props = rooms[mapped_start]["props"]
            sec_props = rooms[sec_room]["props"]
            iso_characters[cid]["schedule"] = [
                {
                    "start": "08:00",
                    "end": "12:00",
                    "room_id": mapped_start,
                    "activity": str(entry.get("goal") or "Sinh hoạt thường nhật")[:80],
                    "pose": "stand",
                    "target_prop": start_props[0]["id"] if start_props else None,
                },
                {
                    "start": "12:00",
                    "end": "17:30",
                    "room_id": sec_room,
                    "activity": "Xử lý công việc riêng",
                    "pose": "sit",
                    "target_prop": sec_props[0]["id"] if sec_props else None,
                },
                {
                    "start": "17:30",
                    "end": "22:00",
                    "room_id": mapped_start,
                    "activity": "Quay lại điểm hẹn",
                    "pose": "stand",
                    "target_prop": start_props[0]["id"] if start_props else None,
                },
            ]

    meta_raw = world_obj.get("meta") if isinstance(world_obj.get("meta"), dict) else {}
    world_name = str(meta_raw.get("name") or wid).strip()
    acts_spine = [
        {
            "act_index": 1,
            "title": f"Hồi 1: Khởi đầu tại {world_name}",
            "goal": "Khám phá các khu vực chính, trò chuyện với dàn nhân vật và thu thập manh mối ban đầu.",
            "advance_condition": {"min_turns": 3},
            "climax_hint": "Những bí mật đầu tiên bắt đầu hé lộ.",
        },
        {
            "act_index": 2,
            "title": "Hồi 2: Cao trào & Góc khuất",
            "goal": "Lần theo lịch trình của từng người và giải quyết nút thắt đang diễn ra.",
            "advance_condition": {"min_turns": 6},
            "climax_hint": "Đối mặt trực diện với mâu thuẫn hoặc sự cố.",
        },
        {
            "act_index": 3,
            "title": "Hồi 3: Hồi kết",
            "goal": "Đưa ra lựa chọn cuối cùng định hình kết cục của mọi người.",
            "advance_condition": {"ending_ready": True},
            "climax_hint": "Chốt hạ toàn bộ câu chuyện.",
        },
    ]

    endings = [
        {
            "id": "ending_resolved",
            "title": "Kết cục Viên mãn",
            "condition": {"flags": {"story_resolved": True}},
            "epilogue_prompt": "Tóm tắt cái kết ấm áp khi mọi nút thắt được tháo gỡ.",
        },
        {
            "id": "ending_day_end",
            "title": "Khép lại Một Ngày",
            "condition": {"min_act": 3},
            "epilogue_prompt": "Khép lại câu chuyện trong không khí đời thường sâu lắng.",
        },
    ]

    clock_raw = world_obj.get("clock") if isinstance(world_obj.get("clock"), dict) else {}
    c_start_raw = clock_raw.get("start") if isinstance(clock_raw.get("start"), dict) else {}
    clock = {
        "mode": clock_raw.get("mode") if clock_raw.get("mode") in CLOCK_MODES else "calendar",
        "start": {
            "year": int(c_start_raw.get("year", 2027)),
            "month": int(c_start_raw.get("month", 9)),
            "day": int(c_start_raw.get("day", 15)),
            "hour": int(c_start_raw.get("hour", 8)),
            "minute": int(c_start_raw.get("minute", 45)),
        },
    }

    iso_world = {
        "format": "iso-world-v1",
        "id": wid,
        "meta": {
            "name": world_name,
            "genre": str(meta_raw.get("genre") or "Đời thường").strip(),
            "tone_rating": meta_raw.get("tone_rating") if meta_raw.get("tone_rating") in TONE_RATINGS else "safe",
            "location_alias_map": alias_map,
        },
        "rules": world_obj.get("rules") or {"writing_style": "", "world_rules": []},
        "user": world_obj.get("user") or {"id": "user", "name": "Người chơi", "aliases": [], "gender": "không rõ", "persona": ""},
        "player_defaults": {
            "energy": 100,
            "stress": 10,
            "cash": 500,
            "stats": {"charm": 11, "logic": 11, "stealth": 11, "will": 11},
        },
        "clock": clock,
        "rooms": rooms,
        "doors": doors,
        "acts_spine": acts_spine,
        "endings": endings,
        "missing_props": [],
        "characters": iso_characters,
        "cast": iso_cast,
        "relationships": world_obj.get("relationships") or {"auto_bootstrap": True, "edges": {}},
        "initial_facts": copy.deepcopy(world_obj.get("initial_facts") or []),
        "lore": copy.deepcopy(world_obj.get("lore") or []),
        "opening": {
            "user_start_room": alias_map.get(opening_loc, hub_id),
            "tension": int(opening_raw.get("tension", 1)),
            "channel_type": str(opening_raw.get("channel_type") or "in_person"),
            "narration": str(opening_raw.get("narration") or ""),
        },
    }
    return validate_iso_world(iso_world)
