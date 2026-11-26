"""Deterministic <=24x24 floorplan packer (CSP backtracking + A* verification) and 3-Act campaign compiler."""

from __future__ import annotations

import copy
import json
import uuid
from collections import deque
from typing import Any

try:
    from . import catalog as catalog_mod, db, engine, prefetch, schemas
except ImportError:
    import catalog as catalog_mod
    import db
    import engine
    import prefetch
    import schemas


MAX_GRID = 24


# ---------------------------------------------------------------------------
# Floorplan Packing (CSP Backtracking + Local Room-BFS + Global A* Check)
# ---------------------------------------------------------------------------

def _shared_edge_tiles(
    ra: tuple[int, int, int, int],
    rb: tuple[int, int, int, int],
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Return touching tile pairs ((ax, ay), (bx, by)) along any shared wall between ra and rb."""
    ax, ay, aw, ah = ra
    bx, by, bw, bh = rb
    pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []

    if ax + aw == bx:
        y0, y1 = max(ay, by), min(ay + ah, by + bh)
        for y in range(y0, y1):
            pairs.append(((ax + aw - 1, y), (bx, y)))
    elif bx + bw == ax:
        y0, y1 = max(ay, by), min(ay + ah, by + bh)
        for y in range(y0, y1):
            pairs.append(((ax, y), (bx + bw - 1, y)))
    elif ay + ah == by:
        x0, x1 = max(ax, bx), min(ax + aw, bx + bw)
        for x in range(x0, x1):
            pairs.append(((x, ay + ah - 1), (x, by)))
    elif by + bh == ay:
        x0, x1 = max(ax, bx), min(ax + aw, bx + bw)
        for x in range(x0, x1):
            pairs.append(((x, ay), (x, by + bh - 1)))

    return pairs


def _overlaps(ra: tuple[int, int, int, int], rb: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = ra
    bx, by, bw, bh = rb
    return (ax < bx + bw and ax + aw > bx) and (ay < by + bh and ay + ah > by)


def _order_rooms(
    room_ids: list[str],
    door_adj: dict[str, list[str]],
    start_room: str | None,
) -> list[str]:
    if not room_ids:
        return []
    seed = max(
        room_ids,
        key=lambda rid: (len(door_adj.get(rid, [])), 1 if rid == start_room else 0, -room_ids.index(rid)),
    )
    ordered: list[str] = []
    visited: set[str] = set()
    q = deque([seed])
    while len(ordered) < len(room_ids):
        if not q:
            for rid in room_ids:
                if rid not in visited:
                    q.append(rid)
                    break
        cur = q.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        ordered.append(cur)
        neighbors = sorted(
            door_adj.get(cur, []),
            key=lambda n: (-len(door_adj.get(n, [])), room_ids.index(n) if n in room_ids else 999),
        )
        for nb in neighbors:
            if nb not in visited:
                q.append(nb)
    return ordered


def _candidate_positions_around(
    anchor: tuple[int, int, int, int],
    rw: int,
    rh: int,
) -> list[tuple[int, int]]:
    ax, ay, aw, ah = anchor
    cands: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def _add(x: int, y: int) -> None:
        if (x, y) not in seen:
            seen.add((x, y))
            cands.append((x, y))

    # Prioritize aligned edges on East, South, West, North, then 1-tile shifts
    y_vals = [ay, ay + ah - rh, ay + (ah - rh) // 2] + list(range(ay - rh + 1, ay + ah))
    x_vals = [ax, ax + aw - rw, ax + (aw - rw) // 2] + list(range(ax - rw + 1, ax + aw))

    for y in y_vals:
        if min(ay + ah, y + rh) - max(ay, y) >= 1:
            _add(ax + aw, y)
            _add(ax - rw, y)
    for x in x_vals:
        if min(ax + aw, x + rw) - max(ax, x) >= 1:
            _add(x, ay + ah)
            _add(x, ay - rh)

    return cands


def _solve_room_layout(
    rooms_meta: dict[str, dict[str, Any]],
    order: list[str],
    door_adj: dict[str, list[str]],
    allow_swap: bool,
    door_pair_counts: dict[tuple[str, str], int] | None = None,
) -> dict[str, tuple[int, int, int, int]] | None:
    if not order:
        return {}

    placed: dict[str, tuple[int, int, int, int]] = {}
    r0 = order[0]
    sz0 = rooms_meta[r0]["size"]
    placed[r0] = (12, 12, sz0[0], sz0[1])

    def _backtrack(idx: int) -> bool:
        if idx >= len(order):
            return True
        rid = order[idx]
        rw, rh = rooms_meta[rid]["size"]
        dims_list = [(rw, rh)]
        if allow_swap and rw != rh:
            dims_list.append((rh, rw))

        placed_door_nbs = [nb for nb in door_adj.get(rid, []) if nb in placed]
        anchors = [placed[placed_door_nbs[0]]] if placed_door_nbs else list(placed.values())

        cur_min_x = min(r[0] for r in placed.values())
        cur_min_y = min(r[1] for r in placed.values())
        cur_max_x = max(r[0] + r[2] for r in placed.values())
        cur_max_y = max(r[1] + r[3] for r in placed.values())

        scored: list[tuple[tuple[int, int, int], tuple[int, int, int, int]]] = []
        seen_rects: set[tuple[int, int, int, int]] = set()

        for cw, ch in dims_list:
            for anchor in anchors:
                for cx, cy in _candidate_positions_around(anchor, cw, ch):
                    rect = (cx, cy, cw, ch)
                    if rect in seen_rects:
                        continue
                    seen_rects.add(rect)

                    nmin_x = min(cur_min_x, cx)
                    nmin_y = min(cur_min_y, cy)
                    nmax_x = max(cur_max_x, cx + cw)
                    nmax_y = max(cur_max_y, cy + ch)
                    bw = nmax_x - nmin_x
                    bh = nmax_y - nmin_y
                    if bw > MAX_GRID or bh > MAX_GRID:
                        continue

                    if any(_overlaps(rect, other) for other in placed.values()):
                        continue

                    if placed_door_nbs:
                        if any(
                            len(_shared_edge_tiles(rect, placed[nb]))
                            < (door_pair_counts or {}).get((min(rid, nb), max(rid, nb)), 1)
                            for nb in placed_door_nbs
                        ):
                            continue

                    center_dist = abs((cx + cw / 2.0) - 14.0) + abs((cy + ch / 2.0) - 14.0)
                    score = (bw * bh, max(bw, bh), int(center_dist * 10))
                    scored.append((score, rect))

        scored.sort(key=lambda item: item[0])
        for _, rect in scored[:48]:
            placed[rid] = rect
            if _backtrack(idx + 1):
                return True
            del placed[rid]
        return False

    if _backtrack(1):
        return placed
    return None


def _local_room_walkable_ok(
    rx: int,
    ry: int,
    rw: int,
    rh: int,
    blocked: set[tuple[int, int]],
    room_door_tiles: set[tuple[int, int]],
    nonwalkable_footprints: list[set[tuple[int, int]]] | None = None,
) -> bool:
    walkable = {
        (x, y)
        for x in range(rx, rx + rw)
        for y in range(ry, ry + rh)
        if (x, y) not in blocked
    }
    if not walkable:
        return False
    if not room_door_tiles.issubset(walkable):
        return False
    seeds = list(room_door_tiles) if room_door_tiles else [next(iter(walkable))]
    start = seeds[0]
    visited = {start}
    q = deque([start])
    while q:
        cx, cy = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = (cx + dx, cy + dy)
            if nb in walkable and nb not in visited:
                visited.add(nb)
                q.append(nb)
    if visited != walkable:
        return False
    if nonwalkable_footprints:
        for fp_tiles in nonwalkable_footprints:
            if not any(
                (fx + dx, fy + dy) in visited
                for (fx, fy) in fp_tiles
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ):
                return False
    return True


def pack_floorplan(
    world: dict[str, Any],
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministically pack 4..8 rooms and props into a <=24x24 isometric floorplan."""
    if not isinstance(world, dict):
        raise ValueError("world must be a dict")

    cat = catalog if catalog is not None else catalog_mod.load_starter_catalog()
    raw_rooms = world.get("rooms")
    if isinstance(raw_rooms, dict):
        room_list = list(raw_rooms.values())
    elif isinstance(raw_rooms, list):
        room_list = list(raw_rooms)
    else:
        raise ValueError("world['rooms'] must be a dict or list")

    if not room_list:
        raise ValueError("world['rooms'] cannot be empty")

    rooms_meta: dict[str, dict[str, Any]] = {}
    room_ids: list[str] = []
    total_area = 0
    for r in room_list:
        if not isinstance(r, dict) or not r.get("id"):
            raise ValueError("Each room must be a dict with 'id'")
        rid = str(r["id"])
        sz = r.get("size") or [5, 5]
        if not isinstance(sz, (list, tuple)) or len(sz) != 2:
            raise ValueError(f"Room '{rid}' has invalid size: {sz!r}")
        rw, rh = int(sz[0]), int(sz[1])
        if rw <= 0 or rh <= 0 or rw > MAX_GRID or rh > MAX_GRID:
            raise ValueError(f"Room '{rid}' dimensions {rw}x{rh} exceed 1..{MAX_GRID}")
        total_area += rw * rh
        rooms_meta[rid] = {
            "id": rid,
            "name": str(r.get("name") or rid),
            "kind": str(r.get("kind") or "other"),
            "privacy": str(r.get("privacy") or r.get("type") or "public"),
            "owner_id": r.get("owner_id"),
            "size": [rw, rh],
            "props": list(r.get("props") or []),
        }
        room_ids.append(rid)

    if total_area > MAX_GRID * MAX_GRID:
        raise ValueError(f"Total room area {total_area} exceeds {MAX_GRID}x{MAX_GRID} grid")

    raw_doors = world.get("doors") or []
    door_adj: dict[str, list[str]] = {rid: [] for rid in room_ids}
    door_pair_counts: dict[tuple[str, str], int] = {}
    for d in raw_doors:
        if not isinstance(d, dict):
            continue
        ra = str(d.get("room_a") or "")
        rb = str(d.get("room_b") or "")
        if ra in door_adj and rb in door_adj and ra != rb:
            if rb not in door_adj[ra]:
                door_adj[ra].append(rb)
            if ra not in door_adj[rb]:
                door_adj[rb].append(ra)
            pair = (min(ra, rb), max(ra, rb))
            door_pair_counts[pair] = door_pair_counts.get(pair, 0) + 1

    start_room = (world.get("opening") or {}).get("user_start_room")
    order = _order_rooms(room_ids, door_adj, start_room)

    def _normalize_and_place_doors(
        cand_layout: dict[str, tuple[int, int, int, int]],
    ) -> tuple[
        dict[str, tuple[int, int, int, int]],
        int,
        int,
        list[dict[str, Any]],
        dict[str, set[tuple[int, int]]],
        set[tuple[int, int]],
    ]:
        min_x = min(rect[0] for rect in cand_layout.values())
        min_y = min(rect[1] for rect in cand_layout.values())
        nl: dict[str, tuple[int, int, int, int]] = {
            rid: (x - min_x, y - min_y, w, h)
            for rid, (x, y, w, h) in cand_layout.items()
        }
        w_tot = max(x + w for x, y, w, h in nl.values())
        h_tot = max(y + h for x, y, w, h in nl.values())
        if w_tot > MAX_GRID or h_tot > MAX_GRID:
            raise ValueError(f"Packed floorplan {w_tot}x{h_tot} exceeds {MAX_GRID}x{MAX_GRID}")

        door_entries: list[tuple[int, dict[str, Any], str, str, list[tuple[tuple[int, int], tuple[int, int]]]]] = []
        for idx, d in enumerate(raw_doors):
            if not isinstance(d, dict):
                continue
            ra = str(d.get("room_a") or "")
            rb = str(d.get("room_b") or "")
            if ra not in nl or rb not in nl:
                continue
            pairs = _shared_edge_tiles(nl[ra], nl[rb])
            if not pairs:
                raise ValueError(f"Rooms '{ra}' and '{rb}' share a door but do not touch in floorplan")
            door_entries.append((idx, d, ra, rb, pairs))

        # Assign constrained doors (fewest candidate tiles) first to avoid corner/wall starvation
        door_entries_mrv = sorted(door_entries, key=lambda item: (len(item[4]), item[0]))
        used_tiles: set[tuple[int, int]] = set()
        placed_by_idx: dict[int, dict[str, Any]] = {}
        by_room: dict[str, set[tuple[int, int]]] = {rid: set() for rid in room_ids}
        all_dt: set[tuple[int, int]] = set()

        for idx, d, ra, rb, pairs in door_entries_mrv:
            mid = len(pairs) // 2
            ordered_pairs = sorted(range(len(pairs)), key=lambda k: abs(k - mid))
            chosen: tuple[tuple[int, int], tuple[int, int]] | None = None
            for k in ordered_pairs:
                pa, pb = pairs[k]
                if pa not in used_tiles and pb not in used_tiles:
                    chosen = (pa, pb)
                    break
            if chosen is None:
                raise ValueError(
                    f"Cannot place door '{d.get('id') or idx + 1}' between '{ra}' and '{rb}' without colliding with existing doors"
                )
            ta, tb = chosen
            used_tiles.add(ta)
            used_tiles.add(tb)
            all_dt.add(ta)
            all_dt.add(tb)
            by_room[ra].add(ta)
            by_room[rb].add(tb)
            placed_by_idx[idx] = {
                "id": str(d.get("id") or f"door_{idx + 1}"),
                "room_a": ra,
                "room_b": rb,
                "tile_a": [ta[0], ta[1]],
                "tile_b": [tb[0], tb[1]],
                "xy": [ta[0], ta[1]],
                "x": ta[0],
                "y": ta[1],
                "locked": bool(d.get("locked", False)),
                "key_item_id": d.get("key_item_id"),
                "lock_dc": d.get("lock_dc"),
            }

        ordered_doors = [placed_by_idx[idx] for idx in sorted(placed_by_idx.keys())]
        return nl, w_tot, h_tot, ordered_doors, by_room, all_dt

    packed_tuple = None
    last_err: ValueError | None = None
    for allow_swap in (False, True):
        cand = _solve_room_layout(
            rooms_meta,
            order,
            door_adj,
            allow_swap=allow_swap,
            door_pair_counts=door_pair_counts,
        )
        if cand is None:
            continue
        try:
            packed_tuple = _normalize_and_place_doors(cand)
            break
        except ValueError as exc:
            last_err = exc

    if packed_tuple is None:
        if last_err is not None:
            raise last_err
        raise ValueError("Unable to pack rooms into a non-overlapping <=24x24 connected floorplan")

    norm_layout, width, height, fp_doors, door_tiles_by_room, all_door_tiles = packed_tuple

    fp_rooms: dict[str, dict[str, Any]] = {}
    for rid in room_ids:
        rx, ry, rw, rh = norm_layout[rid]
        meta = rooms_meta[rid]
        fp_rooms[rid] = {
            "id": rid,
            "name": meta["name"],
            "kind": meta["kind"],
            "privacy": meta["privacy"],
            "owner_id": meta["owner_id"],
            "x": rx,
            "y": ry,
            "w": rw,
            "h": rh,
            "bounds": [rx, ry, rw, rh],
        }

    # 24x24 grid map (rows y=0..23, cols x=0..23)
    grid: list[list[str | None]] = [[None for _ in range(MAX_GRID)] for _ in range(MAX_GRID)]
    for rid, (rx, ry, rw, rh) in norm_layout.items():
        for gx in range(rx, rx + rw):
            for gy in range(ry, ry + rh):
                grid[gy][gx] = rid

    # Symmetric wall adjacency map
    adjacent_rooms: dict[str, list[str]] = {rid: [] for rid in room_ids}
    for i in range(len(room_ids)):
        ra = room_ids[i]
        for j in range(i + 1, len(room_ids)):
            rb = room_ids[j]
            if _shared_edge_tiles(norm_layout[ra], norm_layout[rb]):
                adjacent_rooms[ra].append(rb)
                adjacent_rooms[rb].append(ra)

    # Prop placement per room
    fp_props: list[dict[str, Any]] = []
    for rid in room_ids:
        rx, ry, rw, rh = norm_layout[rid]
        room_doors = door_tiles_by_room[rid]
        door_clear_tiles: set[tuple[int, int]] = set()
        for dx0, dy0 in room_doors:
            for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = dx0 + ddx, dy0 + ddy
                if rx <= nx < rx + rw and ry <= ny < ry + rh and (nx, ny) not in room_doors:
                    door_clear_tiles.add((nx, ny))
        raw_props = rooms_meta[rid]["props"]

        enriched: list[tuple[int, int, int, dict[str, Any], list[int], bool]] = []
        for p_idx, p in enumerate(raw_props):
            if not isinstance(p, dict):
                continue
            tag = str(p.get("asset_tag") or "mystery_placeholder")
            spec = cat.get(tag) or {}
            sz = p.get("size") or spec.get("size") or [1, 1]
            pw, pd = max(1, int(sz[0])), max(1, int(sz[1]))
            walkable = bool(p.get("walkable", spec.get("walkable", False)))
            enriched.append((1 if walkable else 0, -(pw * pd), p_idx, p, [pw, pd], walkable))

        # Place non-walkable largest props first, walkable props last
        enriched_sorted = sorted(
            enriched,
            key=lambda t: (t[0], t[1], t[2]),
        )

        occupied_nonwalkable: set[tuple[int, int]] = set()
        occupied_nonwalkable_props: list[set[tuple[int, int]]] = []
        occupied_all: set[tuple[int, int]] = set()
        for _, _, p_idx, p, sz, walkable in enriched_sorted:
            pw, pd = sz
            orientations = [(pw, pd)]
            if pw != pd:
                orientations.append((pd, pw))

            placed_prop: dict[str, Any] | None = None
            best_overall: tuple[tuple[int, ...], int, int, int, int] | None = None
            for cw, ch in orientations:
                if cw > rw or ch > rh:
                    continue
                for py in range(ry, ry + rh - ch + 1):
                    for px in range(rx, rx + rw - cw + 1):
                        tiles = {(px + dx, py + dy) for dx in range(cw) for dy in range(ch)}
                        if not tiles.isdisjoint(all_door_tiles):
                            continue
                        if not walkable:
                            if not tiles.isdisjoint(occupied_nonwalkable):
                                continue
                            if not _local_room_walkable_ok(
                                rx,
                                ry,
                                rw,
                                rh,
                                occupied_nonwalkable | tiles,
                                room_doors,
                                occupied_nonwalkable_props + [tiles],
                            ):
                                continue
                            disjoint_all = 0 if tiles.isdisjoint(occupied_all) else 1
                            in_clear = 0 if tiles.isdisjoint(door_clear_tiles) else 1
                            on_wall = 0 if (px == rx or py == ry or px + cw == rx + rw or py + ch == ry + rh) else 1
                            door_dist = min(
                                (abs(px - dx0) + abs(py - dy0) for (dx0, dy0) in room_doors),
                                default=5,
                            )
                            score = (disjoint_all, in_clear, on_wall, -door_dist, py * MAX_GRID + px)
                        else:
                            if not tiles.isdisjoint(occupied_nonwalkable):
                                continue
                            disjoint_all = 0 if tiles.isdisjoint(occupied_all) else 1
                            overlap_nw = 0
                            overlap_all = len(tiles & occupied_all)
                            in_clear = 0 if tiles.isdisjoint(door_clear_tiles) else 1
                            on_wall = 0 if (px == rx or py == ry or px + cw == rx + rw or py + ch == ry + rh) else 1
                            door_dist = min(
                                (abs(px - dx0) + abs(py - dy0) for (dx0, dy0) in room_doors),
                                default=5,
                            )
                            score = (disjoint_all, overlap_nw, overlap_all, in_clear, on_wall, -door_dist, py * MAX_GRID + px)
                        if best_overall is None or score < best_overall[0]:
                            best_overall = (score, px, py, cw, ch)

            if best_overall is not None:
                _, best_x, best_y, cw, ch = best_overall
                tiles = {(best_x + dx, best_y + dy) for dx in range(cw) for dy in range(ch)}
                occupied_all.update(tiles)
                if not walkable:
                    occupied_nonwalkable.update(tiles)
                    occupied_nonwalkable_props.append(tiles)
                placed_prop = copy.deepcopy(p)
                placed_prop.update({
                    "id": str(p.get("id") or f"prop_{rid}_{p_idx + 1}"),
                    "room_id": rid,
                    "x": best_x,
                    "y": best_y,
                    "size": [cw, ch],
                    "walkable": walkable,
                })

            if placed_prop is None:
                # Fallback 1x1 placement while strictly preserving door clearance & connectivity
                fb_cands: list[tuple[tuple[int, int, int], int, int]] = []
                for py in range(ry, ry + rh):
                    for px in range(rx, rx + rw):
                        tile = (px, py)
                        if tile in all_door_tiles:
                            continue
                        if not walkable:
                            if tile in occupied_nonwalkable:
                                continue
                            if not _local_room_walkable_ok(
                                rx,
                                ry,
                                rw,
                                rh,
                                occupied_nonwalkable | {tile},
                                room_doors,
                                occupied_nonwalkable_props + [{tile}],
                            ):
                                continue
                            in_clear = 0 if tile not in door_clear_tiles else 1
                            in_all = 0 if tile not in occupied_all else 1
                            fb_cands.append(((in_clear, in_all, py * MAX_GRID + px), px, py))
                        else:
                            if tile in occupied_nonwalkable:
                                continue
                            in_all = 0 if tile not in occupied_all else 1
                            in_clear = 0 if tile not in door_clear_tiles else 1
                            fb_cands.append(((in_all, in_clear, py * MAX_GRID + px), px, py))
                if fb_cands:
                    fb_cands.sort(key=lambda item: item[0])
                    _, px, py = fb_cands[0]
                    tile = (px, py)
                    occupied_all.add(tile)
                    if not walkable:
                        occupied_nonwalkable.add(tile)
                        occupied_nonwalkable_props.append({tile})
                    placed_prop = copy.deepcopy(p)
                    placed_prop.update({
                        "id": str(p.get("id") or f"prop_{rid}_{p_idx + 1}"),
                        "room_id": rid,
                        "x": px,
                        "y": py,
                        "size": [1, 1],
                        "walkable": walkable,
                    })
                else:
                    raise ValueError(
                        f"Cannot place prop '{p.get('id') or p_idx + 1}' in room '{rid}' without breaking door connectivity or overlapping blocked tiles"
                    )

            fp_props.append(placed_prop)

    fp = {
        "width": width,
        "height": height,
        "rooms": fp_rooms,
        "doors": fp_doors,
        "props": fp_props,
        "adjacent_rooms": adjacent_rooms,
        "grid": grid,
    }

    # Global A* reachability check with all doors unlocked
    all_open_state = {"doors": {d["id"]: False for d in fp_doors}}
    anchor_room = start_room if start_room in norm_layout else room_ids[0]
    start_xy = engine._first_walkable_tile(fp, anchor_room)
    for rid in room_ids:
        if rid == anchor_room:
            continue
        rx, ry, rw, rh = norm_layout[rid]
        reachable = False
        for gx in range(rx, rx + rw):
            for gy in range(ry, ry + rh):
                if engine.find_path(fp, start_xy, [gx, gy], all_open_state) is not None:
                    reachable = True
                    break
            if reachable:
                break
        if not reachable:
            raise ValueError(f"Room '{rid}' is not A*-reachable from '{anchor_room}'")

    for p in fp_props:
        if engine.find_approach_tile(fp, p, start_xy, all_open_state) is None:
            raise ValueError(f"Prop '{p.get('id')}' in room '{p.get('room_id')}' is not A*-approachable")

    return fp


# ---------------------------------------------------------------------------
# Campaign & Act Compilation
# ---------------------------------------------------------------------------

def _default_intrusion_scene(room_id: str, owner_id: str, owner_name: str) -> dict[str, Any]:
    return {
        "room_id": room_id,
        "owner_id": owner_id,
        "branches": {
            "hide_success": {
                "text": f"Bạn kịp nép mình vào góc khuất ngay khi {owner_name} đẩy cửa bước vào.",
                "pose": "hide_inside",
            },
            "hide_fail": {
                "text": f"Tiếng động nhỏ khiến {owner_name} quay lại và bắt gặp bạn ngay tại chỗ.",
                "pose": "stand",
            },
            "bluff": {
                "text": f"Bạn bình tĩnh giải thích lý do có mặt trong phòng với {owner_name}.",
                "pose": "stand",
            },
            "retreat": {
                "text": f"Bạn khéo léo rút ra hành lang trước khi {owner_name} kịp nhận ra.",
                "pose": "stand",
            },
        },
    }


def _normalize_storylets(raw_storylets: list[Any], act_index: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_storylets or []):
        if not isinstance(item, dict):
            continue
        st = copy.deepcopy(item)
        st.setdefault("id", f"st_act{act_index}_{idx + 1}")
        st["act_index"] = int(st.get("act_index") or act_index)
        trig = st.get("trigger") if isinstance(st.get("trigger"), dict) else {}
        if st.get("room_id") and not trig.get("room_id"):
            trig["room_id"] = st["room_id"]
        st["trigger"] = trig
        try:
            validated = schemas.validate_storylet(st)
            if st.get("room_id"):
                validated["room_id"] = st["room_id"]
            if st.get("completed"):
                validated["completed"] = True
            out.append(validated)
        except Exception:
            st.setdefault("title", str(st["id"]))
            st.setdefault("relevant_keys", {"flags": [], "items": [], "affinity_with": [], "extra": []})
            st.setdefault("priority", 50)
            st.setdefault("is_intrusion", False)
            st.setdefault("root_node_id", "root")
            out.append(st)
    return out


def _seed_storylet_cache(
    campaign_id: str,
    storylets: list[dict[str, Any]],
    initial_cache: list[Any] | dict[str, Any] | None,
    intrusion_scenes: dict[str, Any],
    state: dict[str, Any],
    db_path: str | None,
) -> None:
    if not db_path:
        return
    st_by_id = {str(s.get("id")): s for s in storylets if isinstance(s, dict) and s.get("id")}

    # Seed from initial_cache list or dict
    if isinstance(initial_cache, dict):
        for sid, tree in initial_cache.items():
            if isinstance(tree, dict):
                st = st_by_id.get(str(sid)) or {}
                mhash = prefetch.masked_state_hash(state, st.get("relevant_keys"))
                db.put_cached_scene(campaign_id, str(sid), mhash, tree, db_path=db_path)
    elif isinstance(initial_cache, list):
        for entry in initial_cache:
            if not isinstance(entry, dict):
                continue
            sid = str(entry.get("storylet_id") or entry.get("id") or "")
            tree = entry.get("tree") if isinstance(entry.get("tree"), dict) else entry
            if sid and isinstance(tree, dict):
                st = st_by_id.get(sid) or {}
                mhash = str(entry.get("masked_state_hash") or prefetch.masked_state_hash(state, st.get("relevant_keys")))
                db.put_cached_scene(campaign_id, sid, mhash, tree, db_path=db_path)

    # Seed any storylets with embedded nodes
    for st in storylets:
        if isinstance(st, dict) and isinstance(st.get("nodes"), dict) and st["nodes"]:
            sid = str(st["id"])
            mhash = prefetch.masked_state_hash(state, st.get("relevant_keys"))
            if db.get_cached_scene(campaign_id, sid, mhash, db_path=db_path) is None:
                tree = {
                    "storylet_id": sid,
                    "root_node_id": st.get("root_node_id") or next(iter(st["nodes"].keys())),
                    "nodes": st["nodes"],
                }
                db.put_cached_scene(campaign_id, sid, mhash, tree, db_path=db_path)

    # Seed intrusion_scenes in scene_cache under intrusion_<room_id>
    for room_id, scene in (intrusion_scenes or {}).items():
        if isinstance(scene, dict):
            db.put_cached_scene(campaign_id, f"intrusion_{room_id}", "default", scene, db_path=db_path)


def _init_canonical_state(
    world: dict[str, Any],
    fp: dict[str, Any],
    intrusion_scenes: dict[str, Any],
) -> dict[str, Any]:
    opening = world.get("opening") or {}
    rooms_dict = fp.get("rooms") if isinstance(fp.get("rooms"), dict) else {r["id"]: r for r in fp.get("rooms", [])}
    first_room_id = next(iter(rooms_dict.keys()), "foyer")
    start_room = str(opening.get("user_start_room") or first_room_id)
    start_pos = engine._first_walkable_tile(fp, start_room)
    p_def = world.get("player_defaults") or {}
    w_clk = (world.get("clock") or {}).get("start") or {}

    clock = {
        "mode": str((world.get("clock") or {}).get("mode") or "calendar"),
        "year": int(w_clk.get("year", 2027)),
        "month": int(w_clk.get("month", 9)),
        "day": int(w_clk.get("day", 15)),
        "hour": int(w_clk.get("hour", 8)),
        "minute": int(w_clk.get("minute", 30)),
    }

    npcs: dict[str, dict[str, Any]] = {}
    chars = world.get("characters") if isinstance(world.get("characters"), dict) else {}
    cast_by_id = {
        str(c.get("char_id") or c.get("id")): c
        for c in (world.get("cast") or [])
        if isinstance(c, dict)
    }
    cur_tod = clock["hour"] * 60 + clock["minute"]

    for cid, char_obj in chars.items():
        cast_entry = cast_by_id.get(cid, {})
        room_id = str(cast_entry.get("start_room") or start_room)
        pose = "stand"
        target_prop = None
        activity = ""
        for slot in (char_obj.get("schedule") or []):
            if isinstance(slot, dict) and engine._schedule_slot_active(slot, cur_tod):
                room_id = str(slot.get("room_id") or room_id)
                pose = str(slot.get("pose") or "stand")
                target_prop = slot.get("target_prop")
                activity = str(slot.get("activity") or "")
                break
        if room_id not in rooms_dict:
            room_id = start_room
        npcs[cid] = {
            "room_id": room_id,
            "pos": engine._first_walkable_tile(fp, room_id),
            "pose": pose,
            "target_prop": target_prop,
            "mood": "neutral",
            "activity": activity,
            "alertness": 1,
        }

    doors = {d["id"]: bool(d.get("locked", False)) for d in fp.get("doors", [])}
    doors_unlocked = [did for did, locked in doors.items() if not locked]
    affinity = {cid: 0 for cid in chars.keys()}
    lighting = {rid: "normal" for rid in rooms_dict.keys()}

    return {
        "act_index": 1,
        "clock": clock,
        "player": {
            "room_id": start_room,
            "pos": start_pos,
            "pose": "stand",
            "target_prop": None,
            "energy": int(p_def.get("energy", 100)),
            "stress": int(p_def.get("stress", 10)),
            "cash": int(p_def.get("cash", 0)),
            "stats": copy.deepcopy(p_def.get("stats") or {"charm": 12, "logic": 12, "stealth": 12, "will": 12}),
            "inventory": list(p_def.get("inventory") or []),
        },
        "npcs": npcs,
        "doors": doors,
        "doors_unlocked": doors_unlocked,
        "lighting": lighting,
        "flags": {},
        "discovered_items": [],
        "affinity": affinity,
        "relationships": copy.deepcopy(affinity),
        "traces": [],
        "completed_storylets": [],
        "turns": 0,
        "tension": int(opening.get("tension", 1)),
        "pending_intrusion": None,
        "ending": None,
        "free_sandbox_mode": False,
        "intrusion_scenes": copy.deepcopy(intrusion_scenes),
    }


def compile_new_campaign(
    world_or_v2: dict[str, Any],
    cast_override: list[dict[str, Any]] | dict[str, Any] | None = None,
    llm: Any = None,
    catalog: dict[str, dict[str, Any]] | None = None,
    campaign_id: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Upgrade world if needed, pack <=24x24 floorplan, compile Act 1 (1 LLM call, 0 if precompiled), and save."""
    if not isinstance(world_or_v2, dict):
        raise ValueError("world_or_v2 must be a dict")

    raw_container = copy.deepcopy(world_or_v2)
    if isinstance(raw_container.get("world"), dict):
        world_in = copy.deepcopy(raw_container["world"])
        pre_storylets = raw_container.get("storylets") or world_in.get("storylets")
        pre_intrusion = raw_container.get("intrusion_scenes") or world_in.get("intrusion_scenes")
        pre_cache = raw_container.get("initial_cache") or world_in.get("initial_cache")
    else:
        world_in = raw_container
        pre_storylets = world_in.get("storylets")
        pre_intrusion = world_in.get("intrusion_scenes")
        pre_cache = world_in.get("initial_cache")

    if cast_override:
        if isinstance(cast_override, list):
            world_in["cast"] = copy.deepcopy(cast_override)
        elif isinstance(cast_override, dict):
            world_in["characters"] = copy.deepcopy(cast_override)

    if world_in.get("format") != "iso-world-v1":
        world = schemas.upgrade_world_v2_to_iso(world_in)
    else:
        world = world_in
    world = schemas.validate_iso_world(world)

    cat = catalog if catalog is not None else catalog_mod.load_starter_catalog()
    fp = pack_floorplan(world, cat)

    cid = str(campaign_id or f"camp_{uuid.uuid4().hex[:10]}")
    intrusion_scenes: dict[str, Any] = copy.deepcopy(pre_intrusion) if isinstance(pre_intrusion, dict) else {}
    initial_cache: Any = pre_cache

    rooms_iter = list(fp["rooms"].values()) if isinstance(fp.get("rooms"), dict) else fp.get("rooms", [])
    if isinstance(pre_storylets, list) and len(pre_storylets) > 0:
        storylets = _normalize_storylets(pre_storylets, act_index=1)
    elif llm is not None:
        act1_spine = (world.get("acts_spine") or [{}])[0]
        compile_payload = {
            "world_name": (world.get("meta") or {}).get("name"),
            "act_index": 1,
            "act_spine": act1_spine,
            "rooms": [{"id": r["id"], "name": r["name"], "privacy": r["privacy"], "owner_id": r["owner_id"]} for r in rooms_iter],
            "cast": world.get("cast", []),
            "missing_props_count": len(world.get("missing_props", [])),
        }
        resp, _ = llm.complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Bạn là Campaign Act 1 Compiler cho game Isometric RP tiếng Việt. "
                        "Hãy trả về JSON gồm 'storylets' (danh sách Storylet Hồi 1), "
                        "'intrusion_scenes' (map room_id -> nhánh hide_success/hide_fail/bluff/retreat), "
                        "và 'initial_cache' (danh sách SceneNodeTree đã biên dịch sẵn nếu có)."
                    ),
                },
                {"role": "user", "content": json.dumps(compile_payload, ensure_ascii=False)},
            ],
            schema_hint="ActCompileResponse{storylets, intrusion_scenes, initial_cache}",
        )
        storylets = _normalize_storylets(resp.get("storylets") or [], act_index=1)
        if isinstance(resp.get("intrusion_scenes"), dict):
            for k, v in resp["intrusion_scenes"].items():
                intrusion_scenes.setdefault(k, v)
        if initial_cache is None:
            initial_cache = resp.get("initial_cache")
    else:
        storylets = []

    # Ensure every non-user private room has a deterministic intrusion_scene
    user_id = str((world.get("user") or {}).get("id") or "user")
    chars = world.get("characters") or {}
    for r in rooms_iter:
        if r.get("privacy") == "private" and r.get("owner_id") and r["owner_id"] != user_id:
            rid = r["id"]
            if rid not in intrusion_scenes:
                owner_id = str(r["owner_id"])
                owner_name = str(((chars.get(owner_id) or {}).get("meta") or {}).get("name") or owner_id)
                intrusion_scenes[rid] = _default_intrusion_scene(rid, owner_id, owner_name)

    world["intrusion_scenes"] = copy.deepcopy(intrusion_scenes)
    state = _init_canonical_state(world, fp, intrusion_scenes)

    campaign: dict[str, Any] = {
        "id": cid,
        "title": str((world.get("meta") or {}).get("name") or "Chiến dịch mới"),
        "act_index": 1,
        "world": world,
        "cast": copy.deepcopy(world.get("cast") or []),
        "floorplan": fp,
        "storylets": storylets,
        "intrusion_scenes": intrusion_scenes,
        "state": state,
        "history": [],
    }

    if db_path:
        db.init_db(db_path=db_path)
        db.save_campaign(campaign, db_path=db_path)
        _seed_storylet_cache(cid, storylets, initial_cache, intrusion_scenes, state, db_path)
        clk = state["clock"]
        db.create_checkpoint(
            cid,
            "Đầu Hồi 1",
            "act_start",
            1,
            f"{clk['hour']:02d}:{clk['minute']:02d}",
            state,
            [],
            storylets,
            db_path=db_path,
        )

    return campaign


def compile_next_act(
    campaign: dict[str, Any],
    llm: Any,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Compile Act 2 or Act 3 based on real campaign consequences (1 LLM call). Raises ValueError if act >= 3."""
    if not isinstance(campaign, dict):
        raise ValueError("campaign must be a dict")
    state = campaign.setdefault("state", {})
    cur_act = int(campaign.get("act_index") or state.get("act_index") or 1)
    if cur_act >= 3:
        raise ValueError("Campaign is already at final Act 3; cannot compile next act")

    next_act = cur_act + 1
    world = campaign.get("world") or {}
    acts_spine = world.get("acts_spine") or []
    next_spine = acts_spine[next_act - 1] if len(acts_spine) >= next_act else {"act_index": next_act}

    merged_aff: dict[str, Any] = {}
    if isinstance(state.get("affinity"), dict):
        merged_aff.update(state["affinity"])
    if isinstance(state.get("relationships"), dict):
        merged_aff.update(state["relationships"])

    summary = {
        "next_act": next_act,
        "act_spine": next_spine,
        "flags": state.get("flags") or {},
        "discovered_items": state.get("discovered_items") or [],
        "affinity": merged_aff,
        "traces": state.get("traces") or [],
        "npc_moods": {
            cid: (info.get("mood") if isinstance(info, dict) else "neutral")
            for cid, info in (state.get("npcs") or {}).items()
        },
        "recent_history": (campaign.get("history") or [])[-10:],
    }

    resp, _ = llm.complete_json(
        [
            {
                "role": "system",
                "content": (
                    f"Bạn là Campaign Act {next_act} Compiler cho game Isometric RP tiếng Việt. "
                    "Dựa trên tóm tắt hậu quả thực tế, hãy sinh JSON gồm 'storylets' cho Hồi mới và 'initial_cache' (nếu có)."
                ),
            },
            {"role": "user", "content": json.dumps(summary, ensure_ascii=False)},
        ],
        schema_hint=f"Act{next_act}CompileResponse{{storylets, initial_cache}}",
    )

    new_storylets = _normalize_storylets(resp.get("storylets") or [], act_index=next_act)
    existing = campaign.setdefault("storylets", [])
    existing_ids = {str(s.get("id")) for s in existing if isinstance(s, dict)}
    for st in new_storylets:
        if str(st.get("id")) not in existing_ids:
            existing.append(st)
            existing_ids.add(str(st.get("id")))

    campaign["act_index"] = next_act
    state["act_index"] = next_act

    if db_path and campaign.get("id"):
        cid = str(campaign["id"])
        intrusion_scenes = (
            campaign.get("intrusion_scenes")
            or world.get("intrusion_scenes")
            or state.get("intrusion_scenes")
            or {}
        )
        db.save_campaign(campaign, db_path=db_path)
        _seed_storylet_cache(
            cid,
            new_storylets,
            resp.get("initial_cache"),
            intrusion_scenes,
            state,
            db_path,
        )
        clk = state.get("clock") or {}
        if clk:
            clock_str = f"{int(clk.get('hour', 8)):02d}:{int(clk.get('minute', 0)):02d}"
        else:
            cm = int(state.get("clock_min", 480)) % 1440
            clock_str = f"{cm // 60:02d}:{cm % 60:02d}"
        db.create_checkpoint(
            cid,
            f"Đầu Hồi {next_act}",
            "act_start",
            next_act,
            clock_str,
            state,
            campaign.get("history") or [],
            existing,
            db_path=db_path,
        )

    return campaign


def generate_world_seed(
    prompt_text: str,
    tone_rating: str,
    llm: Any,
) -> dict[str, Any]:
    """Use LLM + compact catalog tag index to generate a validated iso-world-v1 seed."""
    tag_index = catalog_mod.export_llm_tag_index()
    req = {
        "prompt": str(prompt_text or "").strip(),
        "tone_rating": str(tone_rating or "mature_drama").strip(),
        "catalog_tags": tag_index[:30],
    }
    resp, _ = llm.complete_json(
        [
            {
                "role": "system",
                "content": (
                    "Bạn là AI World Seed Architect. Hãy sinh 1 thế giới 'iso-world-v1' JSON hợp lệ tiếng Việt "
                    "gồm 4..8 rooms (mỗi phòng size [4..8, 4..8]), doors kết nối liên thông, characters, cast, "
                    "3 acts_spine, endings, missing_props (tách visual_brief và story_meta), clock, và opening."
                ),
            },
            {"role": "user", "content": json.dumps(req, ensure_ascii=False)},
        ],
        schema_hint="iso-world-v1",
    )

    raw_world = resp.get("world") if isinstance(resp.get("world"), dict) else resp
    if not isinstance(raw_world, dict):
        raise ValueError("LLM did not return a world dict")

    world = copy.deepcopy(raw_world)
    if world.get("format") == "world-v2" or ("locations" in world and "rooms" not in world):
        world = schemas.upgrade_world_v2_to_iso(world)
    else:
        world.setdefault("format", "iso-world-v1")
        world.setdefault("id", f"seed_{uuid.uuid4().hex[:8]}")
        world.setdefault("meta", {"name": "Thế giới mới", "genre": "Trinh thám", "tone_rating": tone_rating or "mature_drama"})
        world.setdefault("rules", {"writing_style": "Tự nhiên.", "world_rules": ["Mọi nhân vật đều hư cấu."]})
        world.setdefault("user", {"id": "user", "name": "Người chơi", "aliases": ["Bạn"], "gender": "nam", "persona": "Khách mời"})
        world.setdefault("player_defaults", {"energy": 100, "stress": 10, "cash": 500, "stats": {"charm": 12, "logic": 12, "stealth": 12, "will": 12}})
        world.setdefault("clock", {"mode": "calendar", "start": {"year": 2027, "month": 9, "day": 15, "hour": 19, "minute": 0}})
        world.setdefault("missing_props", [])

    return schemas.validate_iso_world(world)
