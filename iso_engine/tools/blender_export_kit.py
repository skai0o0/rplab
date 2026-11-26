"""Blender bpy exporter + standalone GLB v2 validator and minimal GLB builder for iso_engine."""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from typing import Any

try:
    import bpy  # type: ignore
    from mathutils import Vector  # type: ignore

    HAS_BLENDER = True
except ImportError:
    bpy = None
    Vector = None
    HAS_BLENDER = False

_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

try:
    import catalog  # type: ignore
except ImportError:
    catalog = None


_GLB_MAGIC = 0x46546C67
_GLB_CHUNK_JSON = 0x4E4F534A
_GLB_CHUNK_BIN = 0x004E4942


def validate_glb_bytes(data: bytes) -> dict[str, Any]:
    """Validate GLB v2 bytes (delegates to catalog.validate_glb_bytes when available)."""
    if catalog is not None:
        return catalog.validate_glb_bytes(data)

    if not isinstance(data, (bytes, bytearray)) or len(data) < 20:
        raise ValueError("GLB data too short")
    magic, version, total_length = struct.unpack_from("<3I", data, 0)
    if magic != _GLB_MAGIC or version != 2 or total_length != len(data) or total_length % 4 != 0:
        raise ValueError("Invalid GLB v2 header")
    c0_len, c0_type = struct.unpack_from("<2I", data, 12)
    if c0_type != _GLB_CHUNK_JSON or c0_len <= 0 or c0_len % 4 != 0 or 20 + c0_len > total_length:
        raise ValueError("Invalid GLB JSON chunk")
    gltf = json.loads(bytes(data[20 : 20 + c0_len]).rstrip(b" \x00").decode("utf-8"))
    if not isinstance(gltf, dict) or not str((gltf.get("asset") or {}).get("version", "")).startswith("2."):
        raise ValueError("Missing glTF 2.x asset.version")
    has_bin = False
    bin_len = 0
    off = 20 + c0_len
    while off < total_length:
        clen, ctype = struct.unpack_from("<2I", data, off)
        if clen % 4 != 0 or off + 8 + clen > total_length:
            raise ValueError("Invalid GLB chunk alignment")
        if ctype == _GLB_CHUNK_BIN:
            has_bin = True
            bin_len = clen
        off += 8 + clen
    return {
        "valid": True,
        "version": 2,
        "byte_length": total_length,
        "gltf_version": str(gltf["asset"]["version"]),
        "has_bin": has_bin,
        "bin_byte_length": bin_len,
        "mesh_count": len(gltf.get("meshes") or []),
        "node_count": len(gltf.get("nodes") or []),
        "scene_count": len(gltf.get("scenes") or []),
    }


def build_minimal_glb_bytes() -> bytes:
    """Build a minimal spec-compliant glTF 2.0 .glb with a single 1m triangle at bottom-center (0,0,0)."""
    # 3 vertices: (-0.5, 0.0, 0.0), (0.5, 0.0, 0.0), (0.0, 1.0, 0.0)
    bin_raw = struct.pack(
        "<9f",
        -0.5,
        0.0,
        0.0,
        0.5,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
    )
    while len(bin_raw) % 4 != 0:
        bin_raw += b"\x00"

    gltf_doc = {
        "asset": {"version": "2.0", "generator": "iso_engine.blender_export_kit"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "minimal_prop"}],
        "meshes": [
            {
                "name": "minimal_mesh",
                "primitives": [{"attributes": {"POSITION": 0}, "material": 0}],
            }
        ],
        "materials": [
            {
                "name": "default_mat",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.65, 0.55, 0.45, 1.0],
                    "metallicFactor": 0.1,
                    "roughnessFactor": 0.7,
                },
            }
        ],
        "buffers": [{"byteLength": len(bin_raw)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(bin_raw), "target": 34962}
        ],
        "accessors": [
            {
                "bufferView": 0,
                "byteOffset": 0,
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
                "min": [-0.5, 0.0, 0.0],
                "max": [0.5, 1.0, 0.0],
            }
        ],
    }

    json_raw = json.dumps(gltf_doc, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    while len(json_raw) % 4 != 0:
        json_raw += b" "

    total_len = 12 + 8 + len(json_raw) + 8 + len(bin_raw)
    header = struct.pack("<3I", _GLB_MAGIC, 2, total_len)
    chunk0 = struct.pack("<2I", len(json_raw), _GLB_CHUNK_JSON) + json_raw
    chunk1 = struct.pack("<2I", len(bin_raw), _GLB_CHUNK_BIN) + bin_raw
    glb = header + chunk0 + chunk1
    validate_glb_bytes(glb)
    return glb


def _compute_world_aabb(objects: list[Any]) -> tuple[float, float, float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for obj in objects:
        for corner in obj.bound_box:
            wc = obj.matrix_world @ Vector(corner)
            xs.append(wc.x)
            ys.append(wc.y)
            zs.append(wc.z)
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def export_selection_to_glb(
    output_path: str,
    size_tiles: tuple[int, int] = (1, 1),
    tile_meters: float = 1.0,
    apply_transforms: bool = True,
) -> dict[str, Any]:
    """Scale & align selected Blender meshes (Z-up) to bottom-center (0,0,0) and export Y-up GLB."""
    if not HAS_BLENDER or bpy is None or Vector is None:
        raise RuntimeError("Blender bpy environment is not available")

    meshes = [o for o in bpy.context.selected_objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("No MESH objects selected in Blender")

    min_x, max_x, min_y, max_y, _, _ = _compute_world_aabb(meshes)
    cur_x = max(max_x - min_x, 1e-6)
    cur_y = max(max_y - min_y, 1e-6)
    target_x = float(size_tiles[0]) * float(tile_meters)
    target_y = float(size_tiles[1]) * float(tile_meters)
    scale_f = min(target_x / cur_x, target_y / cur_y)

    for obj in meshes:
        obj.scale = (obj.scale.x * scale_f, obj.scale.y * scale_f, obj.scale.z * scale_f)
        obj.location = (obj.location.x * scale_f, obj.location.y * scale_f, obj.location.z * scale_f)

    bpy.context.view_layer.update()
    min_x, max_x, min_y, max_y, min_z, _ = _compute_world_aabb(meshes)
    center_x = (min_x + max_x) * 0.5
    center_y = (min_y + max_y) * 0.5
    for obj in meshes:
        obj.location = (
            obj.location.x - center_x,
            obj.location.y - center_y,
            obj.location.z - min_z,
        )

    bpy.context.view_layer.update()
    if apply_transforms:
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=output_path,
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_apply=True,
    )
    with open(output_path, "rb") as f:
        return validate_glb_bytes(f.read())


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw_argv:
        raw_argv = raw_argv[raw_argv.index("--") + 1 :]

    parser = argparse.ArgumentParser(
        description="iso_engine Blender GLB exporter & standalone GLB v2 validator"
    )
    parser.add_argument("--validate-glb", metavar="PATH", help="Validate an existing .glb file")
    parser.add_argument(
        "--create-minimal-glb",
        metavar="PATH",
        help="Write a minimal spec-compliant glTF 2.0 .glb file",
    )
    parser.add_argument(
        "--export-glb",
        metavar="PATH",
        help="Export selected Blender MESH objects to .glb (requires Blender bpy)",
    )
    parser.add_argument(
        "--size",
        nargs=2,
        type=int,
        default=[1, 1],
        metavar=("W", "D"),
        help="Target tile footprint W D (default: 1 1)",
    )
    parser.add_argument(
        "--tile-meters",
        type=float,
        default=1.0,
        help="Meters per tile (default: 1.0)",
    )

    args = parser.parse_args(raw_argv)

    try:
        if args.create_minimal_glb:
            glb_bytes = build_minimal_glb_bytes()
            out_dir = os.path.dirname(os.path.abspath(args.create_minimal_glb))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(args.create_minimal_glb, "wb") as f:
                f.write(glb_bytes)
            summary = validate_glb_bytes(glb_bytes)
            print(json.dumps({"created": args.create_minimal_glb, **summary}, ensure_ascii=False))
            return 0

        if args.validate_glb:
            with open(args.validate_glb, "rb") as f:
                summary = validate_glb_bytes(f.read())
            print(json.dumps(summary, ensure_ascii=False))
            return 0

        if args.export_glb:
            if not HAS_BLENDER:
                print("Error: --export-glb requires running inside Blender (bpy not found)", file=sys.stderr)
                return 1
            summary = export_selection_to_glb(
                args.export_glb,
                size_tiles=(int(args.size[0]), int(args.size[1])),
                tile_meters=float(args.tile_meters),
            )
            print(json.dumps(summary, ensure_ascii=False))
            return 0

        parser.print_help()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
