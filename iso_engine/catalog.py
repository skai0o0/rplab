"""Hybrid asset catalog, anti-spoiler MissingProp listing, GLB validation, and Studio asset persistence."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import re
import struct
from typing import Any

try:
    from . import db, schemas
except ImportError:
    import db
    import schemas


class CatalogError(ValueError):
    """Raised when voxel blocks, starter catalog, GLB/VOX uploads, or studio assets fail validation."""


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ASSETS_ROOT = os.path.join(HERE, "assets")
DEFAULT_STARTER_PATH = os.path.join(DEFAULT_ASSETS_ROOT, "starter_catalog.json")
DEFAULT_CUSTOM_DIR = os.path.join(DEFAULT_ASSETS_ROOT, "custom")
DEFAULT_GLB_DIR = os.path.join(DEFAULT_ASSETS_ROOT, "glb")

VALID_CATEGORIES = frozenset(
    {"arch", "living", "bedroom", "kitchen", "bathroom", "study", "mystery", "custom"}
)
VALID_MOUNTS = frozenset({"floor", "surface", "wall"})
VALID_POSES = frozenset({"stand", "sit", "lie", "hide_inside"})
VALID_SHAPES = frozenset({"cube", "stud", "cylinder", "slope", "sphere", "torus", "bar"})
VALID_MATERIALS = frozenset({"plastic", "wood", "metal", "fabric", "fabric-leather", "glass", "neon"})
ASSET_TAG_RE = re.compile(r"^[a-z0-9_]{2,64}$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

_GLB_MAGIC = 0x46546C67  # b"glTF"
_GLB_CHUNK_JSON = 0x4E4F534A  # b"JSON"
_GLB_CHUNK_BIN = 0x004E4942  # b"BIN\x00"


def _is_strict_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_pose_slots(raw: Any) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CatalogError("pose_slots must be a dict")
    out: dict[str, dict[str, Any]] = {}
    for pose_name, slot in raw.items():
        if pose_name not in VALID_POSES:
            raise CatalogError(f"Invalid pose slot key: {pose_name!r}")
        if not isinstance(slot, dict):
            raise CatalogError(f"pose_slots['{pose_name}'] must be a dict")
        off = slot.get("offset")
        if not isinstance(off, (list, tuple)) or len(off) != 3 or not all(_is_number(n) for n in off):
            raise CatalogError(f"pose_slots['{pose_name}'].offset must be [x, y, z] numbers")
        rot_y = slot.get("rot_y", 0.0)
        if not _is_number(rot_y):
            raise CatalogError(f"pose_slots['{pose_name}'].rot_y must be a number")
        out[pose_name] = {
            "offset": [float(off[0]), float(off[1]), float(off[2])],
            "rot_y": float(rot_y),
        }
    return out


def _validate_size(raw: Any, default: list[int] | None = None) -> list[int]:
    if raw is None and default is not None:
        return list(default)
    if (
        not isinstance(raw, (list, tuple))
        or len(raw) != 2
        or not _is_strict_int(raw[0])
        or not _is_strict_int(raw[1])
        or not (1 <= raw[0] <= 4)
        or not (1 <= raw[1] <= 4)
    ):
        raise CatalogError(f"size must be [w, d] ints in 1..4, got {raw!r}")
    return [int(raw[0]), int(raw[1])]


def _compute_asset_url(file_path: str | None) -> str | None:
    if not file_path:
        return None
    try:
        assets_real = os.path.realpath(DEFAULT_ASSETS_ROOT)
        target_real = os.path.realpath(file_path)
        if os.path.commonpath([assets_real, target_real]) == assets_real:
            rel = os.path.relpath(target_real, assets_real).replace("\\", "/")
            return f"/assets/{rel}"
    except ValueError:
        pass
    return None


def validate_voxel_blocks(voxels: list[dict]) -> list[dict]:
    """Validate voxel blocks inside a 16x16x16 integer grid."""
    if not isinstance(voxels, list) or len(voxels) < 1:
        raise CatalogError("voxels must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for idx, blk in enumerate(voxels):
        if not isinstance(blk, dict):
            raise CatalogError(f"voxels[{idx}] must be a dict")
        for key in ("x", "y", "z", "w", "h", "d"):
            if not _is_strict_int(blk.get(key)):
                raise CatalogError(f"voxels[{idx}].{key} must be an integer, got {blk.get(key)!r}")
        x, y, z = blk["x"], blk["y"], blk["z"]
        w, h, d = blk["w"], blk["h"], blk["d"]
        if not (0 <= x < 16 and 0 <= y < 16 and 0 <= z < 16):
            raise CatalogError(f"voxels[{idx}] origin ({x},{y},{z}) out of [0, 16) bounds")
        if not (1 <= w <= 16 and 1 <= h <= 16 and 1 <= d <= 16):
            raise CatalogError(f"voxels[{idx}] dimensions ({w},{h},{d}) out of [1, 16] bounds")
        if x + w > 16 or y + h > 16 or z + d > 16:
            raise CatalogError(
                f"voxels[{idx}] exceeds 16x16x16 bounds: ({x}+{w}, {y}+{h}, {z}+{d})"
            )

        shape = blk.get("shape", "cube")
        if shape not in VALID_SHAPES:
            raise CatalogError(f"voxels[{idx}].shape invalid: {shape!r}")
        material = blk.get("material", "plastic")
        if material not in VALID_MATERIALS:
            raise CatalogError(f"voxels[{idx}].material invalid: {material!r}")
        color = blk.get("color")
        if not isinstance(color, str) or not HEX_COLOR_RE.match(color):
            raise CatalogError(f"voxels[{idx}].color invalid hex: {color!r}")

        normalized.append(
            {
                "x": x,
                "y": y,
                "z": z,
                "w": w,
                "h": h,
                "d": d,
                "color": color.lower(),
                "shape": shape,
                "material": material,
            }
        )
    return normalized


def load_starter_catalog(catalog_path: str | None = None) -> dict[str, dict]:
    """Load and validate starter_catalog.json keyed by asset_tag."""
    path = catalog_path or DEFAULT_STARTER_PATH
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)

    if isinstance(raw, dict) and isinstance(raw.get("prefabs"), list):
        items = raw["prefabs"]
    elif isinstance(raw, dict):
        items = [{"asset_tag": k, **v} if isinstance(v, dict) else v for k, v in raw.items()]
    else:
        raise CatalogError("starter_catalog.json must be a dict or {'prefabs': [...]}")

    catalog: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(items):
        if not isinstance(entry, dict):
            raise CatalogError(f"starter prefab[{idx}] must be a dict")
        tag = str(entry.get("asset_tag") or "").strip()
        if not ASSET_TAG_RE.match(tag):
            raise CatalogError(f"starter prefab[{idx}].asset_tag invalid: {tag!r}")
        cat = str(entry.get("category") or "custom").strip()
        if cat not in VALID_CATEGORIES:
            raise CatalogError(f"starter prefab '{tag}' invalid category: {cat!r}")
        size = _validate_size(entry.get("size"))
        height = entry.get("height", 1.0)
        if not _is_number(height) or float(height) <= 0:
            raise CatalogError(f"starter prefab '{tag}' height must be > 0")
        mount = entry.get("mount", "floor")
        if mount not in VALID_MOUNTS:
            raise CatalogError(f"starter prefab '{tag}' invalid mount: {mount!r}")
        pose_slots = _validate_pose_slots(entry.get("pose_slots", {}))
        voxels = validate_voxel_blocks(entry.get("voxels", []))

        catalog[tag] = {
            "asset_tag": tag,
            "kind": str(entry.get("kind") or "voxel-prop-v1"),
            "category": cat,
            "name_vi": str(entry.get("name_vi") or tag),
            "size": size,
            "height": float(height),
            "walkable": bool(entry.get("walkable", False)),
            "mount": mount,
            "pose_slots": pose_slots,
            "voxels": voxels,
        }
    return catalog


def get_merged_catalog(
    db_path: str | None = None,
    starter_path: str | None = None,
) -> dict[str, dict]:
    """Return starter catalog merged with custom_assets from SQLite."""
    catalog = load_starter_catalog(starter_path)
    for row in db.list_custom_assets(db_path=db_path):
        tag = row["asset_tag"]
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        starter_cat = catalog[tag]["category"] if tag in catalog else "custom"
        pose_slots = row.get("pose_slots") or {
            "stand": {"offset": [0.0, 0.0, 0.7], "rot_y": 3.1416}
        }
        fp = row.get("file_path")
        entry: dict[str, Any] = {
            "asset_tag": tag,
            "kind": row["kind"],
            "category": starter_cat,
            "name_vi": row.get("title") or tag,
            "size": list(row.get("size") or [1, 1]),
            "height": float(data.get("height", 1.0)),
            "walkable": bool(data.get("walkable", False)),
            "mount": row.get("mount") or "floor",
            "pose_slots": copy.deepcopy(pose_slots),
            "voxels": copy.deepcopy(data.get("voxels", [])),
            "file_path": fp,
            "asset_url": _compute_asset_url(fp),
        }
        catalog[tag] = entry
    return catalog


def export_llm_tag_index(db_path: str | None = None) -> list[dict]:
    """Export compact catalog metadata (without heavy voxel arrays) for LLM prompts."""
    merged = get_merged_catalog(db_path=db_path)
    out: list[dict[str, Any]] = []
    for tag in sorted(merged.keys()):
        item = merged[tag]
        out.append(
            {
                "asset_tag": tag,
                "category": item["category"],
                "name_vi": item["name_vi"],
                "size": list(item["size"]),
                "mount": item["mount"],
                "walkable": bool(item["walkable"]),
                "pose_slots": sorted((item.get("pose_slots") or {}).keys()),
            }
        )
    return out


def get_pose_slot(
    asset_tag: str,
    pose_name: str,
    db_path: str | None = None,
) -> dict | None:
    """Validate pose_name and return a copy of the prop's pose slot, or None."""
    if pose_name not in VALID_POSES:
        raise CatalogError(f"Invalid pose_name: {pose_name!r}")
    item = get_merged_catalog(db_path=db_path).get(asset_tag)
    if not item:
        return None
    slot = (item.get("pose_slots") or {}).get(pose_name)
    return copy.deepcopy(slot) if isinstance(slot, dict) else None


def get_studio_missing_list(
    world_or_campaign: dict,
    state: dict,
    db_path: str | None = None,
) -> list[dict]:
    """Return anti-spoiler redacted MissingProp list for Voxel Studio."""
    if not isinstance(world_or_campaign, dict):
        return []
    if isinstance(world_or_campaign.get("missing_props"), list):
        missing_props = world_or_campaign["missing_props"]
    elif isinstance(world_or_campaign.get("world"), dict):
        missing_props = world_or_campaign["world"].get("missing_props") or []
    else:
        missing_props = []

    custom_tags = {a["asset_tag"] for a in db.list_custom_assets(db_path=db_path)}
    out: list[dict[str, Any]] = []
    for spec in missing_props:
        if not isinstance(spec, dict) or "asset_tag" not in spec:
            continue
        built = spec["asset_tag"] in custom_tags
        redacted = schemas.redact_missing_prop(spec, state, built=built)
        if not redacted.get("revealed"):
            for secret_key in ("story_meta", "true_name", "room_id", "owner_id", "revealed_flag"):
                redacted.pop(secret_key, None)
        out.append(redacted)
    return out


def validate_glb_bytes(data: bytes) -> dict:
    """Validate binary glTF 2.0 (.glb) header, JSON chunk, optional BIN chunk, and metadata."""
    if not isinstance(data, (bytes, bytearray)):
        raise CatalogError("GLB data must be bytes")
    total_bytes = len(data)
    if total_bytes < 20 or total_bytes > MAX_UPLOAD_BYTES:
        raise CatalogError(f"GLB size {total_bytes} out of valid range [20, {MAX_UPLOAD_BYTES}]")

    magic, version, total_length = struct.unpack_from("<3I", data, 0)
    if magic != _GLB_MAGIC:
        raise CatalogError(f"Invalid GLB magic: 0x{magic:08x}")
    if version != 2:
        raise CatalogError(f"Unsupported GLB version: {version}")
    if total_length != total_bytes or total_length % 4 != 0:
        raise CatalogError(
            f"GLB length mismatch or unaligned: header={total_length}, actual={total_bytes}"
        )

    chunk0_len, chunk0_type = struct.unpack_from("<2I", data, 12)
    if chunk0_type != _GLB_CHUNK_JSON:
        raise CatalogError(f"GLB chunk 0 must be JSON (0x4E4F534A), got 0x{chunk0_type:08x}")
    if chunk0_len <= 0 or chunk0_len % 4 != 0 or 20 + chunk0_len > total_length:
        raise CatalogError(f"Invalid GLB JSON chunk length: {chunk0_len}")

    raw_json = bytes(data[20 : 20 + chunk0_len]).rstrip(b" \x00")
    try:
        gltf_json = json.loads(raw_json.decode("utf-8", errors="strict"))
    except Exception as exc:
        raise CatalogError(f"Invalid GLB JSON chunk: {exc}") from exc

    if not isinstance(gltf_json, dict):
        raise CatalogError("GLB JSON root must be an object")
    asset_obj = gltf_json.get("asset")
    if not isinstance(asset_obj, dict) or not str(asset_obj.get("version", "")).startswith("2."):
        raise CatalogError("GLB asset.version must be glTF 2.x")

    has_bin = False
    bin_len = 0
    offset = 20 + chunk0_len
    while offset < total_length:
        if offset + 8 > total_length:
            raise CatalogError("Truncated GLB chunk header")
        c_len, c_type = struct.unpack_from("<2I", data, offset)
        if c_len % 4 != 0 or offset + 8 + c_len > total_length:
            raise CatalogError(f"Invalid GLB chunk bounds at offset {offset}: len={c_len}")
        if c_type == _GLB_CHUNK_BIN:
            if has_bin:
                raise CatalogError("GLB contains multiple BIN chunks")
            has_bin = True
            bin_len = c_len
        offset += 8 + c_len

    buffers = gltf_json.get("buffers") or []
    if isinstance(buffers, list) and buffers and isinstance(buffers[0], dict):
        buf0 = buffers[0]
        if "uri" not in buf0 and int(buf0.get("byteLength", 0)) > 0:
            if not has_bin or bin_len < int(buf0["byteLength"]):
                raise CatalogError("GLB BIN chunk missing or smaller than buffer[0].byteLength")

    return {
        "valid": True,
        "version": 2,
        "byte_length": total_length,
        "gltf_version": str(asset_obj["version"]),
        "has_bin": has_bin,
        "bin_byte_length": bin_len,
        "mesh_count": len(gltf_json.get("meshes") or []),
        "node_count": len(gltf_json.get("nodes") or []),
        "scene_count": len(gltf_json.get("scenes") or []),
    }


def _decode_binary_payload(payload: dict) -> bytes:
    raw = payload.get("bytes_data")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    b64 = payload.get("data_base64") or payload.get("glb_base64") or payload.get("vox_base64")
    if isinstance(b64, str) and b64.strip():
        s = b64.strip()
        if s.startswith("data:") and ";base64," in s:
            s = s.split(";base64,", 1)[1]
        try:
            return base64.b64decode(s, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CatalogError(f"Invalid base64 binary payload: {exc}") from exc
    raise CatalogError("Binary asset requires 'bytes_data' or 'data_base64'")


def save_studio_asset(
    payload: dict,
    custom_dir: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Validate and persist a custom voxel-prop-v1, .glb, or .vox asset."""
    if not isinstance(payload, dict):
        raise CatalogError("Asset payload must be a dict")
    asset_tag = str(payload.get("asset_tag") or "").strip()
    if not ASSET_TAG_RE.match(asset_tag):
        raise CatalogError(
            f"Invalid asset_tag {asset_tag!r}: must match ^[a-z0-9_]{{2,64}}$"
        )

    kind = str(payload.get("kind") or "voxel-prop-v1").strip()
    if kind not in db.VALID_ASSET_KINDS:
        raise CatalogError(f"Unsupported asset kind: {kind!r}")

    size = _validate_size(payload.get("size"), default=[1, 1])
    mount = str(payload.get("mount") or "floor").strip()
    if mount not in VALID_MOUNTS:
        raise CatalogError(f"Invalid mount: {mount!r}")
    height = payload.get("height", 1.0)
    if not _is_number(height) or float(height) <= 0:
        raise CatalogError(f"Invalid height: {height!r}")
    height_f = float(height)

    title = str(payload.get("title") or payload.get("name_vi") or asset_tag).strip()[:120] or asset_tag
    if payload.get("pose_slots") is not None:
        pose_slots = _validate_pose_slots(payload.get("pose_slots"))
    else:
        pose_slots = {"stand": {"offset": [0.0, 0.0, 0.7], "rot_y": 3.1416}}

    if custom_dir is not None:
        base_dir = os.path.realpath(custom_dir)
    elif kind == "glb":
        base_dir = os.path.realpath(DEFAULT_GLB_DIR)
    else:
        base_dir = os.path.realpath(DEFAULT_CUSTOM_DIR)
    os.makedirs(base_dir, exist_ok=True)

    ext = ".json" if kind == "voxel-prop-v1" else f".{kind}"
    target_real = os.path.realpath(os.path.join(base_dir, f"{asset_tag}{ext}"))
    if os.path.commonpath([base_dir, target_real]) != base_dir:
        raise CatalogError("Path traversal detected in asset target path")

    voxels: list[dict[str, Any]] = []
    if kind == "voxel-prop-v1":
        voxels = validate_voxel_blocks(payload.get("voxels", []))
        walkable = bool(payload.get("walkable", False))
        data_dict: dict[str, Any] = {
            "voxels": voxels,
            "height": height_f,
            "walkable": walkable,
        }
        canonical_json = {
            "asset_tag": asset_tag,
            "kind": kind,
            "title": title,
            "size": size,
            "height": height_f,
            "walkable": walkable,
            "mount": mount,
            "pose_slots": pose_slots,
            "voxels": voxels,
        }
        with open(target_real, "w", encoding="utf-8") as f:
            json.dump(canonical_json, f, ensure_ascii=False, indent=2)
    elif kind == "glb":
        raw_bytes = _decode_binary_payload(payload)
        glb_meta = validate_glb_bytes(raw_bytes)
        with open(target_real, "wb") as f:
            f.write(raw_bytes)
        data_dict = {"height": height_f, "glb_meta": glb_meta}
    else:  # vox
        raw_bytes = _decode_binary_payload(payload)
        if len(raw_bytes) < 8 or len(raw_bytes) > MAX_UPLOAD_BYTES or raw_bytes[:4] != b"VOX ":
            raise CatalogError("Invalid MagicaVoxel .vox binary (expected 'VOX ' header)")
        with open(target_real, "wb") as f:
            f.write(raw_bytes)
        data_dict = {"height": height_f, "vox_bytes": len(raw_bytes)}

    saved = db.save_custom_asset(
        {
            "asset_tag": asset_tag,
            "campaign_id": payload.get("campaign_id"),
            "kind": kind,
            "title": title,
            "size": size,
            "mount": mount,
            "pose_slots": pose_slots,
            "data": data_dict,
            "file_path": target_real,
        },
        db_path=db_path,
    )
    saved["asset_url"] = _compute_asset_url(target_real)
    saved["height"] = height_f
    if kind == "voxel-prop-v1":
        saved["voxels"] = voxels
    return saved
