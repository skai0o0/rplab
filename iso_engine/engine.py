"""0ms deterministic runtime engine: A* pathfinding, d20, intrusion, room acoustics, off-screen sim, StateDelta, and 3-act/ending evaluator."""

from __future__ import annotations

import copy
import datetime
import heapq
import random
from typing import Any

try:
    from . import catalog, db, schemas
except ImportError:
    import catalog
    import db
    import schemas


_STARTER_CACHE: dict[str, dict[str, Any]] | None = None


def _get_starter() -> dict[str, dict[str, Any]]:
    global _STARTER_CACHE
    if _STARTER_CACHE is None:
        try:
            _STARTER_CACHE = catalog.load_starter_catalog()
        except Exception:
            _STARTER_CACHE = {}
    return _STARTER_CACHE


# ---------------------------------------------------------------------------
# Dual-shape state adapters (canonical nested vs legacy flat keys)
# ---------------------------------------------------------------------------

def _rooms_list(container: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = (container or {}).get("rooms")
    if isinstance(rooms, dict):
        return list(rooms.values())
    if isinstance(rooms, list):
        return list(rooms)
    return []


def _find_room(container: dict[str, Any], room_id: str) -> dict[str, Any] | None:
    rooms = (container or {}).get("rooms")
    if isinstance(rooms, dict):
        r = rooms.get(room_id)
        return r if isinstance(r, dict) else None
    if isinstance(rooms, list):
        for r in rooms:
            if isinstance(r, dict) and r.get("id") == room_id:
                return r
    return None


def _doors_list(container: dict[str, Any]) -> list[dict[str, Any]]:
    doors = (container or {}).get("doors")
    if isinstance(doors, dict):
        return list(doors.values())
    if isinstance(doors, list):
        return list(doors)
    return []


def _props_list(floorplan: dict[str, Any], world: dict[str, Any] | None = None, room_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    fp_props = (floorplan or {}).get("props")
    if isinstance(fp_props, dict):
        out.extend(fp_props.values())
    elif isinstance(fp_props, list):
        out.extend(fp_props)
    if not out and world:
        for r in _rooms_list(world):
            for p in r.get("props") or []:
                if isinstance(p, dict):
                    cp = dict(p)
                    cp.setdefault("room_id", r.get("id"))
                    out.append(cp)
    if room_id is not None:
        return [p for p in out if isinstance(p, dict) and p.get("room_id") == room_id]
    return [p for p in out if isinstance(p, dict)]


def _is_prop_walkable(prop: dict[str, Any]) -> bool:
    if "walkable" in prop:
        return bool(prop["walkable"])
    tag = str(prop.get("asset_tag") or "")
    spec = _get_starter().get(tag)
    if spec is not None:
        return bool(spec.get("walkable", False))
    return False


def _is_door_locked(door: dict[str, Any], state: dict[str, Any] | None) -> bool:
    st = state or {}
    did = str(door.get("id") or "")
    for key in ("unlocked_doors", "doors_unlocked"):
        unlocked_list = st.get(key)
        if isinstance(unlocked_list, (list, tuple, set)) and did in unlocked_list:
            return False
    door_states = st.get("door_states")
    if isinstance(door_states, dict) and did in door_states:
        entry = door_states[did]
        ds_locked = bool(entry.get("locked", False)) if isinstance(entry, dict) else bool(entry)
        if not ds_locked:
            return False
        doors_map = st.get("doors")
        if isinstance(doors_map, dict) and did in doors_map and not bool(doors_map[did]):
            return False
        return True
    doors_map = st.get("doors")
    if isinstance(doors_map, dict) and did in doors_map:
        return bool(doors_map[did])
    return bool(door.get("locked", False))


def _unlock_door(door_id: str, state: dict[str, Any]) -> None:
    if isinstance(state.get("doors"), dict):
        state["doors"][door_id] = False
    if isinstance(state.get("door_states"), dict):
        entry = state["door_states"].get(door_id)
        if isinstance(entry, dict):
            entry["locked"] = False
        else:
            state["door_states"][door_id] = {"locked": False}
    for key in ("unlocked_doors", "doors_unlocked"):
        if isinstance(state.get(key), list) and door_id not in state[key]:
            state[key].append(door_id)
    if "doors" not in state and "door_states" not in state and "unlocked_doors" not in state and "doors_unlocked" not in state:
        state["doors"] = {door_id: False}


def _get_clock_hm(state: dict[str, Any], world: dict[str, Any] | None = None) -> tuple[int, int]:
    clk = state.get("clock")
    if isinstance(clk, dict) and ("hour" in clk or "minute" in clk):
        return int(clk.get("hour", 0)) % 24, int(clk.get("minute", 0)) % 60
    if "clock_min" in state and state["clock_min"] is not None:
        cm = int(state["clock_min"]) % 1440
        return cm // 60, cm % 60
    w_clk = ((world or {}).get("clock") or {}).get("start") or {}
    return int(w_clk.get("hour", 8)) % 24, int(w_clk.get("minute", 0)) % 60


def _advance_clock(state: dict[str, Any], minutes: int) -> None:
    if minutes == 0:
        return
    clk = state.get("clock")
    if isinstance(clk, dict):
        year = int(clk.get("year", 2027))
        month = int(clk.get("month", 1))
        day = int(clk.get("day", 1))
        hour = int(clk.get("hour", 0))
        minute = int(clk.get("minute", 0))
        dt = datetime.datetime(year, month, day, hour, minute) + datetime.timedelta(minutes=minutes)
        clk["year"] = dt.year
        clk["month"] = dt.month
        clk["day"] = dt.day
        clk["hour"] = dt.hour
        clk["minute"] = dt.minute
    if "clock_min" in state and state["clock_min"] is not None:
        state["clock_min"] = int(state["clock_min"]) + minutes


def _get_player_room(state: dict[str, Any]) -> str:
    player = state.get("player")
    if isinstance(player, dict) and player.get("room_id"):
        return str(player["room_id"])
    return str(state.get("player_room") or "")


def _set_player_room(state: dict[str, Any], room_id: str) -> None:
    if isinstance(state.get("player"), dict):
        state["player"]["room_id"] = room_id
    if "player_room" in state or not isinstance(state.get("player"), dict):
        state["player_room"] = room_id


def _set_player_pos(state: dict[str, Any], pos: list[int]) -> None:
    xy = [int(pos[0]), int(pos[1])]
    if isinstance(state.get("player"), dict):
        state["player"]["pos"] = xy
    if "player_pos" in state or not isinstance(state.get("player"), dict):
        state["player_pos"] = xy


def _get_player_pose(state: dict[str, Any]) -> str:
    player = state.get("player")
    if isinstance(player, dict) and player.get("pose"):
        return str(player["pose"])
    return str(state.get("player_pose") or "stand")


def _set_player_pose(state: dict[str, Any], pose: str, target_prop: str | None = None) -> None:
    if isinstance(state.get("player"), dict):
        state["player"]["pose"] = pose
        if target_prop is not None:
            state["player"]["target_prop"] = target_prop
        elif "target_prop" in state["player"]:
            state["player"]["target_prop"] = None
    if "player_pose" in state or not isinstance(state.get("player"), dict):
        state["player_pose"] = pose


def _get_player_stats(state: dict[str, Any], world: dict[str, Any] | None = None) -> dict[str, int]:
    player = state.get("player")
    if isinstance(player, dict) and isinstance(player.get("stats"), dict):
        return {k: int(v) for k, v in player["stats"].items()}
    if isinstance(state.get("player_stats"), dict):
        return {k: int(v) for k, v in state["player_stats"].items()}
    w_stats = ((world or {}).get("player_defaults") or {}).get("stats")
    if isinstance(w_stats, dict):
        return {k: int(v) for k, v in w_stats.items()}
    return {"charm": 12, "logic": 12, "stealth": 12, "will": 12}


def _get_inventory_list(state: dict[str, Any]) -> list[str]:
    player = state.get("player")
    if isinstance(player, dict) and isinstance(player.get("inventory"), list):
        return list(player["inventory"])
    if isinstance(state.get("inventory"), list):
        return list(state["inventory"])
    return []


def _mutate_inventory(state: dict[str, Any], add_items: list[str], remove_items: list[str]) -> None:
    targets: list[list[str]] = []
    player = state.get("player")
    if isinstance(player, dict):
        inv = player.setdefault("inventory", [])
        if isinstance(inv, list):
            targets.append(inv)
    if isinstance(state.get("inventory"), list):
        targets.append(state["inventory"])
    if not targets:
        state["inventory"] = []
        targets.append(state["inventory"])
    for inv in targets:
        for item in add_items:
            if item not in inv:
                inv.append(item)
        for item in remove_items:
            while item in inv:
                inv.remove(item)


def _get_npc_room(state: dict[str, Any], cid: str) -> str:
    npcs = state.get("npcs")
    if isinstance(npcs, dict) and isinstance(npcs.get(cid), dict):
        return str(npcs[cid].get("room_id") or "")
    pos_map = state.get("npc_positions")
    if isinstance(pos_map, dict) and isinstance(pos_map.get(cid), dict):
        return str(pos_map[cid].get("room_id") or "")
    return ""


def _get_all_npc_ids(state: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    if isinstance(state.get("npcs"), dict):
        ids.extend(str(k) for k in state["npcs"].keys())
    if isinstance(state.get("npc_positions"), dict):
        for k in state["npc_positions"].keys():
            if str(k) not in ids:
                ids.append(str(k))
    return ids


def _update_npc(
    state: dict[str, Any],
    cid: str,
    *,
    room_id: str | None = None,
    pose: str | None = None,
    mood: str | None = None,
    target_prop: str | None = None,
    pos: list[int] | None = None,
) -> None:
    if isinstance(state.get("npcs"), dict):
        entry = state["npcs"].setdefault(cid, {})
        if room_id is not None:
            entry["room_id"] = room_id
        if pose is not None:
            entry["pose"] = pose
        if mood is not None:
            entry["mood"] = mood
        if target_prop is not None:
            entry["target_prop"] = target_prop
        if pos is not None:
            entry["pos"] = list(pos)
    if isinstance(state.get("npc_positions"), dict):
        p_entry = state["npc_positions"].setdefault(cid, {})
        if room_id is not None:
            p_entry["room_id"] = room_id
        if pose is not None:
            p_entry["pose"] = pose
        if pos is not None:
            p_entry["pos"] = list(pos)
    if mood is not None and isinstance(state.get("npc_moods"), dict):
        state["npc_moods"][cid] = mood


def _append_trace(state: dict[str, Any], trace: dict[str, Any]) -> None:
    wrote = False
    if isinstance(state.get("traces"), list):
        state["traces"].append(copy.deepcopy(trace))
        wrote = True
    if isinstance(state.get("trace_hotspots"), list):
        state["trace_hotspots"].append(copy.deepcopy(trace))
        wrote = True
    if not wrote:
        state["traces"] = [copy.deepcopy(trace)]


def _parse_slot_minutes(slot: dict[str, Any], key: str = "start") -> int | None:
    val = slot.get(key)
    if isinstance(val, str) and ":" in val:
        try:
            hh, mm = val.strip().split(":", 1)
            return (int(hh) % 24) * 60 + (int(mm) % 60)
        except ValueError:
            return None
    if key == "start" and "hour" in slot:
        return (int(slot.get("hour", 0)) % 24) * 60 + (int(slot.get("minute", 0)) % 60)
    return None


def _first_walkable_tile(floorplan: dict[str, Any], room_id: str) -> list[int]:
    room = _find_room(floorplan, room_id)
    if not room:
        return [0, 0]
    rx = int(room.get("x", 0))
    ry = int(room.get("y", 0))
    rw = int(room.get("w", room.get("width", 4)))
    rh = int(room.get("h", room.get("height", 4)))
    blocked: set[tuple[int, int]] = set()
    for p in _props_list(floorplan):
        if p.get("room_id") == room_id and not _is_prop_walkable(p):
            px, py = int(p.get("x", 0)), int(p.get("y", 0))
            pw, pd = p.get("size", [1, 1])
            for dx in range(int(pw)):
                for dy in range(int(pd)):
                    blocked.add((px + dx, py + dy))
    cx, cy = rx + rw // 2, ry + rh // 2
    if (cx, cy) not in blocked:
        return [cx, cy]
    for y in range(ry, ry + rh):
        for x in range(rx, rx + rw):
            if (x, y) not in blocked:
                return [x, y]
    return [rx, ry]


# ---------------------------------------------------------------------------
# Public Runtime Functions
# ---------------------------------------------------------------------------

def _build_grid_maps(
    floorplan: dict[str, Any],
    state: dict[str, Any] | None,
) -> tuple[dict[tuple[int, int], str], set[tuple[int, int]], set[tuple[tuple[int, int], tuple[int, int]]]]:
    tile_room: dict[tuple[int, int], str] = {}
    for room in _rooms_list(floorplan):
        rid = str(room.get("id") or "")
        rx = int(room.get("x", 0))
        ry = int(room.get("y", 0))
        rw = int(room.get("w", room.get("width", 0)))
        rh = int(room.get("h", room.get("height", 0)))
        for x in range(rx, rx + rw):
            for y in range(ry, ry + rh):
                tile_room[(x, y)] = rid

    blocked_tiles: set[tuple[int, int]] = set()
    for p in _props_list(floorplan):
        if not _is_prop_walkable(p):
            px = int(p.get("x", 0))
            py = int(p.get("y", 0))
            sz = p.get("size") or [1, 1]
            pw, pd = int(sz[0]), int(sz[1])
            for dx in range(pw):
                for dy in range(pd):
                    blocked_tiles.add((px + dx, py + dy))

    open_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for d in _doors_list(floorplan):
        if _is_door_locked(d, state):
            continue
        ta = d.get("tile_a")
        tb = d.get("tile_b")
        if isinstance(ta, (list, tuple)) and len(ta) == 2 and isinstance(tb, (list, tuple)) and len(tb) == 2:
            ua = (int(ta[0]), int(ta[1]))
            ub = (int(tb[0]), int(tb[1]))
            open_edges.add((ua, ub))
            open_edges.add((ub, ua))
        elif "xy" in d or ("x" in d and "y" in d):
            xy = d.get("xy") or [d.get("x"), d.get("y")]
            u = (int(xy[0]), int(xy[1]))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                v = (u[0] + dx, u[1] + dy)
                if v in tile_room and tile_room.get(v) != tile_room.get(u):
                    open_edges.add((u, v))
                    open_edges.add((v, u))

    return tile_room, blocked_tiles, open_edges


def find_path(
    floorplan: dict[str, Any],
    start_xy: list[int] | tuple[int, int],
    goal_xy: list[int] | tuple[int, int],
    state: dict[str, Any] | None = None,
) -> list[list[int]] | None:
    """4-directional A* pathfinding on the <=24x24 grid respecting walls, props, and locked doors."""
    if not isinstance(floorplan, dict) or not start_xy or not goal_xy:
        return None
    start = (int(start_xy[0]), int(start_xy[1]))
    goal = (int(goal_xy[0]), int(goal_xy[1]))

    width = int(floorplan.get("width", 24))
    height = int(floorplan.get("height", 24))
    if not (0 <= start[0] < width and 0 <= start[1] < height):
        return None
    if not (0 <= goal[0] < width and 0 <= goal[1] < height):
        return None

    tile_room, blocked_tiles, open_edges = _build_grid_maps(floorplan, state)
    if start not in tile_room or goal not in tile_room:
        return None
    if start in blocked_tiles or goal in blocked_tiles:
        return None
    if start == goal:
        return [[start[0], start[1]]]

    heap: list[tuple[int, int, tuple[int, int]]] = [(abs(goal[0] - start[0]) + abs(goal[1] - start[1]), 0, start)]
    g_score: dict[tuple[int, int], int] = {start: 0}
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    closed: set[tuple[int, int]] = set()

    while heap:
        _, g, cur = heapq.heappop(heap)
        if cur in closed:
            continue
        if cur == goal:
            path: list[list[int]] = []
            node: tuple[int, int] | None = cur
            while node is not None:
                path.append([node[0], node[1]])
                node = came_from.get(node)
            path.reverse()
            return path
        closed.add(cur)
        cur_room = tile_room[cur]

        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt in closed or nxt not in tile_room or nxt in blocked_tiles:
                continue
            if tile_room[nxt] != cur_room and (cur, nxt) not in open_edges:
                continue
            ng = g + 1
            if ng < g_score.get(nxt, 1_000_000):
                g_score[nxt] = ng
                came_from[nxt] = cur
                h = abs(goal[0] - nxt[0]) + abs(goal[1] - nxt[1])
                heapq.heappush(heap, (ng + h, ng, nxt))

    return None


def find_approach_tile(
    floorplan: dict[str, Any],
    prop: dict[str, Any],
    start_xy: list[int] | tuple[int, int],
    state: dict[str, Any] | None = None,
) -> list[int] | None:
    """Find the nearest reachable walkable tile adjacent to (or on, if walkable) a prop."""
    if not isinstance(floorplan, dict) or not isinstance(prop, dict):
        return None
    tile_room, blocked_tiles, _ = _build_grid_maps(floorplan, state)

    px = int(prop.get("x", 0))
    py = int(prop.get("y", 0))
    sz = prop.get("size") or [1, 1]
    pw, pd = int(sz[0]), int(sz[1])
    prop_room = prop.get("room_id")

    footprint = {(px + dx, py + dy) for dx in range(pw) for dy in range(pd)}
    candidates: list[tuple[int, int]] = []
    if _is_prop_walkable(prop):
        for t in sorted(footprint):
            if t in tile_room and t not in blocked_tiles:
                candidates.append(t)
    else:
        seen: set[tuple[int, int]] = set()
        same_room: list[tuple[int, int]] = []
        any_room: list[tuple[int, int]] = []
        for fx, fy in sorted(footprint):
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (fx + dx, fy + dy)
                if nb in seen or nb in footprint or nb not in tile_room or nb in blocked_tiles:
                    continue
                seen.add(nb)
                if prop_room and tile_room[nb] == prop_room:
                    same_room.append(nb)
                else:
                    any_room.append(nb)
        candidates = same_room if same_room else any_room

    best_tile: list[int] | None = None
    best_len = 1_000_000
    for cand in candidates:
        path = find_path(floorplan, start_xy, [cand[0], cand[1]], state)
        if path is not None and len(path) < best_len:
            best_len = len(path)
            best_tile = [cand[0], cand[1]]
    return best_tile


def roll_d20(
    stat_value: int,
    dc: int,
    rng: random.Random | None = None,
    forced_roll: int | None = None,
) -> dict[str, Any]:
    """Roll d20 check: D&D modifier (stat - 10)//2 across 0..20; nat 1 fails, nat 20 succeeds."""
    sv = int(stat_value)
    modifier = (sv - 10) // 2
    if forced_roll is not None:
        roll = max(1, min(20, int(forced_roll)))
    else:
        r_obj = rng if rng is not None else random
        roll = r_obj.randint(1, 20)
    total = roll + modifier
    critical = roll in (1, 20)
    if roll == 1:
        success = False
    elif roll == 20:
        success = True
    else:
        success = total >= int(dc)
    return {
        "roll": roll,
        "modifier": modifier,
        "total": total,
        "dc": int(dc),
        "success": success,
        "critical": critical,
    }


def _find_hide_prop_in_room(
    floorplan: dict[str, Any],
    world: dict[str, Any],
    room_id: str,
) -> dict[str, Any] | None:
    for p in _props_list(floorplan, world, room_id=room_id):
        tag = str(p.get("asset_tag") or "")
        if not tag:
            continue
        try:
            slot = catalog.get_pose_slot(tag, "hide_inside")
        except Exception:
            slot = None
        if slot is not None:
            return p
    return None


def check_intrusion_imminent(
    world: dict[str, Any],
    floorplan: dict[str, Any],
    state: dict[str, Any],
    room_id: str,
    action_minutes: int = 5,
) -> dict[str, Any] | None:
    """Warn if player is in a private room whose owner arrives within <= action_minutes (with midnight wrap)."""
    if not isinstance(world, dict) or not isinstance(state, dict) or not room_id:
        return None
    room = _find_room(world, room_id) or _find_room(floorplan, room_id)
    if not room:
        return None
    privacy = room.get("privacy") or room.get("type") or "public"
    if privacy != "private":
        return None

    user_id = str((world.get("user") or {}).get("id") or "user")
    owner_id = room.get("owner_id")
    if not owner_id or owner_id == user_id:
        return None

    owner = (world.get("characters") or {}).get(owner_id)
    if not isinstance(owner, dict):
        return None

    cur_room_of_owner = _get_npc_room(state, owner_id)
    if cur_room_of_owner == room_id:
        return None

    hour, minute = _get_clock_hm(state, world)
    cur_min = hour * 60 + minute
    threshold = max(1, int(action_minutes))

    soonest: int | None = None
    for slot in owner.get("schedule") or []:
        if not isinstance(slot, dict) or slot.get("room_id") != room_id:
            continue
        start_min = _parse_slot_minutes(slot, "start")
        if start_min is None:
            continue
        diff = (start_min - cur_min) % 1440
        if 0 < diff <= threshold:
            if soonest is None or diff < soonest:
                soonest = diff

    if soonest is None:
        return None

    owner_name = str(((owner.get("meta") or {}).get("name")) or owner_id)
    hide_prop = _find_hide_prop_in_room(floorplan, world, room_id)
    hide_prop_id = hide_prop.get("id") if hide_prop else None

    msg = f"Nghe tiếng bước chân của {owner_name} tới gần ({soonest} phút nữa)!"
    return {
        "room_id": room_id,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "minutes_until": soonest,
        "hide_prop_id": hide_prop_id,
        "message": msg,
        "warning": msg,
        "choices": [
            {
                "kind": "hide",
                "label": "Trốn vào tủ (Stealth d20)" if hide_prop_id else "Tìm góc khuất né tránh (Stealth d20)",
                "stat": "stealth",
                "dc": 12 if hide_prop_id else 99,
            },
            {
                "kind": "bluff",
                "label": "Giả vờ bình thường (Charm d20)",
                "stat": "charm",
                "dc": 13,
            },
            {
                "kind": "retreat",
                "label": "Rút lui ra khỏi phòng",
                "stat": None,
                "dc": None,
            },
        ],
    }


def _extract_intrusion_branch_text(
    intrusion_scene: dict[str, Any] | None,
    branch_keys: tuple[str, ...],
    fallback_text: str,
    *,
    allow_root_fallback: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(intrusion_scene, dict):
        return fallback_text, None

    branches = intrusion_scene.get("branches")
    if isinstance(branches, dict):
        for bk in branch_keys:
            b_obj = branches.get(bk)
            if isinstance(b_obj, dict) and b_obj.get("text"):
                return str(b_obj["text"]), b_obj
            if isinstance(b_obj, str) and b_obj.strip():
                return b_obj.strip(), None

    nodes = intrusion_scene.get("nodes")
    if isinstance(nodes, dict) and nodes:
        for bk in branch_keys:
            if bk in nodes and isinstance(nodes[bk], dict) and nodes[bk].get("text"):
                return str(nodes[bk]["text"]), nodes[bk]
        if allow_root_fallback:
            root_id = intrusion_scene.get("root_node_id") or next(iter(nodes.keys()))
            root_node = nodes.get(root_id)
            if isinstance(root_node, dict) and root_node.get("text"):
                return str(root_node["text"]), root_node

    return fallback_text, None


def resolve_intrusion_choice(
    campaign: dict[str, Any],
    choice_kind: str,
    forced_roll: int | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Resolve 0ms intrusion choice ('hide'/'bluff'/'retreat'), saving auto_risk checkpoint first."""
    world = campaign.get("world") if isinstance(campaign.get("world"), dict) else {}
    fp = campaign.get("floorplan") if isinstance(campaign.get("floorplan"), dict) else {}
    state = campaign.setdefault("state", {})
    history = campaign.setdefault("history", [])
    storylets = campaign.get("storylets") if isinstance(campaign.get("storylets"), list) else []

    pending = state.get("pending_intrusion") if isinstance(state.get("pending_intrusion"), dict) else {}
    room_id = str(pending.get("room_id") or _get_player_room(state) or "")

    # Auto-save auto_risk checkpoint before mutating state
    if db_path and campaign.get("id"):
        hour, minute = _get_clock_hm(state, world)
        act_idx = int(campaign.get("act_index") or state.get("act_index") or 1)
        try:
            db.create_checkpoint(
                campaign["id"],
                f"Trước pha mạo hiểm ({room_id})",
                "auto_risk",
                act_idx,
                f"{hour:02d}:{minute:02d}",
                state,
                history,
                storylets,
                db_path=db_path,
            )
        except Exception:
            pass

    intrusion_scene = (
        (campaign.get("intrusion_scenes") or {}).get(room_id)
        or (world.get("intrusion_scenes") or {}).get(room_id)
        or (state.get("intrusion_scenes") or {}).get(room_id)
    )
    if not intrusion_scene and db_path and campaign.get("id"):
        intrusion_scene = db.get_cached_scene(campaign["id"], f"intrusion_{room_id}", "default", db_path=db_path)

    kind = str(choice_kind or "retreat").strip().lower()
    stats = _get_player_stats(state, world)
    d20_res: dict[str, Any] | None = None
    success = True

    if kind == "hide":
        hide_prop = _find_hide_prop_in_room(fp, world, room_id)
        if hide_prop is not None:
            d20_res = roll_d20(stats.get("stealth", 12), 12, forced_roll=forced_roll)
            success = bool(d20_res["success"])
            if success:
                _set_player_pose(state, "hide_inside", target_prop=hide_prop.get("id"))
                branch_keys = ("hide_success", "hide_ok", "hide")
                fallback = "Bạn nép gọn vào trong chỗ ẩn nấp vừa kịp lúc cánh cửa mở ra."
            else:
                _set_player_pose(state, "stand")
                branch_keys = ("hide_fail", "caught", "fail")
                fallback = "Bạn không kịp giấu mình và bị bắt gặp ngay tại chỗ."
        else:
            d20_res = roll_d20(stats.get("stealth", 12), 99, forced_roll=forced_roll)
            success = bool(d20_res["success"])
            _set_player_pose(state, "stand")
            if success:
                branch_keys = ("hide_success", "evade", "success")
                fallback = "Bạn nép vào góc khuất trong phòng vừa kịp lúc."
            else:
                branch_keys = ("hide_fail", "caught", "fail")
                fallback = "Phòng này không có tủ hay chỗ ẩn nấp kín đáo nào!"
    elif kind == "bluff":
        d20_res = roll_d20(stats.get("charm", 12), 13, forced_roll=forced_roll)
        success = bool(d20_res["success"])
        _set_player_pose(state, "stand")
        branch_keys = ("bluff", "bluff_success") if success else ("bluff_fail", "bluff", "fail")
        fallback = (
            "Bạn giữ vẻ mặt tự nhiên và viện cớ hợp lý."
            if success
            else "Lời giải thích gượng gạo khiến đối phương nghi ngờ."
        )
    else:
        kind = "retreat"
        _set_player_pose(state, "stand")
        dest_room: str | None = None
        for d in _doors_list(fp) or _doors_list(world):
            if _is_door_locked(d, state):
                continue
            if d.get("room_a") == room_id:
                dest_room = str(d.get("room_b"))
                break
            if d.get("room_b") == room_id:
                dest_room = str(d.get("room_a"))
                break
        if dest_room:
            _set_player_room(state, dest_room)
            _set_player_pos(state, _first_walkable_tile(fp, dest_room))
            success = True
            branch_keys = ("retreat", "escape")
            fallback = "Bạn nhanh chân rút lui ra khỏi phòng trước khi bị phát hiện."
        else:
            success = False
            branch_keys = ("retreat_fail", "caught", "fail")
            fallback = "Cửa ra khỏi phòng đều đang bị khóa, bạn không thể rút lui!"

    text, scene_obj = _extract_intrusion_branch_text(
        intrusion_scene,
        branch_keys,
        fallback,
        allow_root_fallback=not (kind == "retreat" and not success),
    )
    state["pending_intrusion"] = None
    node_obj: dict[str, Any] | None = None
    if isinstance(scene_obj, dict) and isinstance(scene_obj.get("nodes"), dict):
        rid_node = scene_obj.get("root_node_id") or next(iter(scene_obj["nodes"].keys()), None)
        if rid_node and isinstance(scene_obj["nodes"].get(rid_node), dict):
            node_obj = scene_obj["nodes"][rid_node]
    elif isinstance(scene_obj, dict):
        node_obj = scene_obj
    if node_obj is None:
        node_obj = {"node_id": f"intr_{kind}", "text": text, "presentation_mode": "standard", "choices": []}

    return {
        "choice_kind": kind,
        "success": success,
        "d20": d20_res,
        "pose": _get_player_pose(state),
        "room_id": _get_player_room(state),
        "text": text,
        "scene": scene_obj,
        "node": node_obj,
    }


def _schedule_slot_active(slot: dict[str, Any], cur_min: int) -> bool:
    s_min = _parse_slot_minutes(slot, "start")
    e_min = _parse_slot_minutes(slot, "end")
    if s_min is None:
        return False
    if e_min is None:
        return cur_min >= s_min
    if s_min <= e_min:
        return s_min <= cur_min < e_min
    # Midnight wrap (e.g., 22:00 -> 06:00)
    return cur_min >= s_min or cur_min < e_min


def advance_clock_and_sim(
    world: dict[str, Any],
    floorplan: dict[str, Any],
    state: dict[str, Any],
    minutes: int,
) -> list[dict[str, Any]]:
    """Advance clock and simulate NPC schedules & off-screen events (adjacent audio + traces, zero dialogue leak)."""
    mins = int(minutes)
    old_h, old_m = _get_clock_hm(state, world)
    old_tod = old_h * 60 + old_m
    old_abs = int(state["clock_min"]) if state.get("clock_min") is not None else old_tod

    _advance_clock(state, mins)
    if mins <= 0:
        return []

    new_h, new_m = _get_clock_hm(state, world)
    new_tod = new_h * 60 + new_m
    new_abs = old_abs + mins

    events: list[dict[str, Any]] = []
    player_room = _get_player_room(state)
    adj_rooms = set((floorplan.get("adjacent_rooms") or {}).get(player_room, []))

    moved_into: dict[str, list[str]] = {}
    chars = world.get("characters") if isinstance(world.get("characters"), dict) else {}
    for cid, char_obj in chars.items():
        if not isinstance(char_obj, dict):
            continue
        prev_room = _get_npc_room(state, cid)
        active_slot: dict[str, Any] | None = None
        for slot in char_obj.get("schedule") or []:
            if isinstance(slot, dict) and _schedule_slot_active(slot, new_tod):
                active_slot = slot
        if active_slot:
            next_room = str(active_slot.get("room_id") or prev_room)
            next_pose = str(active_slot.get("pose") or "stand")
            target_prop = active_slot.get("target_prop")
            _update_npc(
                state,
                cid,
                room_id=next_room,
                pose=next_pose,
                target_prop=target_prop,
                pos=_first_walkable_tile(floorplan, next_room) if next_room else None,
            )
            if next_room and next_room != prev_room:
                moved_into.setdefault(next_room, []).append(cid)

    # 1) Explicit offscreen_events from world
    explicit_rooms_this_tick: set[str] = set()
    triggered_ids = state.setdefault("_triggered_offscreen", [])
    for idx, ev in enumerate(world.get("offscreen_events") or []):
        if not isinstance(ev, dict):
            continue
        ev_id = str(ev.get("id") or f"off_{idx}")
        if ev_id in triggered_ids:
            continue
        trig = ev.get("trigger_min")
        if trig is None:
            continue
        trig_i = int(trig)
        hit = old_abs < trig_i <= new_abs
        if not hit and 0 <= trig_i < 1440:
            tod_delta = (trig_i - old_tod) % 1440
            if (0 < tod_delta <= mins) or (tod_delta == 0 and mins >= 1440):
                hit = True
        if not hit:
            continue
        ev_room = str(ev.get("room_id") or "")
        if not ev_room or ev_room == player_room:
            continue

        triggered_ids.append(ev_id)
        explicit_rooms_this_tick.add(ev_room)

        for cid, mood in (ev.get("mood_effects") or {}).items():
            _update_npc(state, str(cid), mood=str(mood))

        trace = {
            "id": f"trace_{ev_id}",
            "room_id": ev_room,
            "pos": _first_walkable_tile(floorplan, ev_room),
            "label": str(ev.get("trace_label") or "Dấu vết bất thường"),
            "inspected": False,
        }
        _append_trace(state, trace)
        events.append({
            "type": "trace_spawned",
            "kind": "trace_spawned",
            "room_id": ev_room,
            "trace_id": trace["id"],
            "label": trace["label"],
        })

        if ev_room in adj_rooms:
            sound_text = str(ev.get("sound_hint") or "Có tiếng động vọng qua vách tường.")
            events.append({
                "type": "adjacent_sound",
                "kind": "adjacent_sound",
                "from_room": ev_room,
                "room_id": ev_room,
                "text": sound_text,
            })

    # 2) Implicit off-screen co-located NPC encounters when NPCs move into a non-player room
    room_occupants: dict[str, list[str]] = {}
    for cid in _get_all_npc_ids(state):
        r = _get_npc_room(state, cid)
        if r:
            room_occupants.setdefault(r, []).append(cid)

    for room_id, entrants in moved_into.items():
        if room_id == player_room or room_id in explicit_rooms_this_tick:
            continue
        occ = room_occupants.get(room_id, [])
        if len(occ) >= 2:
            for cid in occ:
                _update_npc(state, cid, mood="tense")
            trace = {
                "id": f"trace_{room_id}_{new_abs}",
                "room_id": room_id,
                "pos": _first_walkable_tile(floorplan, room_id),
                "label": f"Dấu vết cuộc trao đổi vội tại {room_id}",
                "inspected": False,
            }
            _append_trace(state, trace)
            events.append({
                "type": "trace_spawned",
                "kind": "trace_spawned",
                "room_id": room_id,
                "trace_id": trace["id"],
                "label": trace["label"],
            })
            if room_id in adj_rooms:
                events.append({
                    "type": "adjacent_sound",
                    "kind": "adjacent_sound",
                    "from_room": room_id,
                    "room_id": room_id,
                    "text": "Tiếng trao đổi khẽ vọng qua vách phòng bên cạnh.",
                })

    return events


def perform_speech_or_dialogue(
    world: dict[str, Any],
    state: dict[str, Any],
    text: str,
    whisper: bool = False,
    target_npc_id: str | None = None,
    forced_roll: int | None = None,
) -> dict[str, Any]:
    """Evaluate room acoustics: public speech reaches all co-located NPCs; whisper rolls Stealth DC 12."""
    player_room = _get_player_room(state)
    co_located = [cid for cid in _get_all_npc_ids(state) if _get_npc_room(state, cid) == player_room]
    stats = _get_player_stats(state, world)

    d20_res: dict[str, Any] | None = None
    if not whisper or len(co_located) <= 1:
        if whisper and target_npc_id and target_npc_id in co_located and len(co_located) == 1:
            witnesses = [target_npc_id]
        else:
            witnesses = list(co_located)
    else:
        d20_res = roll_d20(stats.get("stealth", 12), 12, forced_roll=forced_roll)
        if d20_res["success"] and target_npc_id and target_npc_id in co_located:
            witnesses = [target_npc_id]
        else:
            witnesses = list(co_located)

    if isinstance(state.get("history"), list):
        state["history"].append({
            "type": "speech",
            "room_id": player_room,
            "text": text,
            "whisper": bool(whisper),
            "witnesses": list(witnesses),
        })

    return {
        "witnesses": witnesses,
        "co_located_npcs": co_located,
        "whisper": bool(whisper),
        "whisper_success": bool(whisper and target_npc_id and witnesses == [target_npc_id]),
        "d20": d20_res,
        "room_id": player_room,
        "text": text,
    }


def use_item(
    world: dict[str, Any],
    floorplan: dict[str, Any],
    state: dict[str, Any],
    item_id: str,
    target_id: str,
) -> dict[str, Any]:
    """Use an inventory item on a door or prop target."""
    inv = _get_inventory_list(state)
    if item_id not in inv:
        return {"success": False, "message": f"Không có vật phẩm '{item_id}' trong túi đồ."}

    target_door = None
    for d in _doors_list(floorplan) or _doors_list(world):
        if d.get("id") == target_id:
            target_door = d
            break

    if target_door is not None:
        if not _is_door_locked(target_door, state):
            _unlock_door(target_id, state)
            return {"success": True, "unlocked_door": target_id, "message": "Cửa đã mở sẵn."}
        expected_key = target_door.get("key_item_id")
        if expected_key and expected_key == item_id:
            _unlock_door(target_id, state)
            return {"success": True, "unlocked_door": target_id, "message": "Đã mở khóa cửa thành công."}
        return {"success": False, "message": "Chìa khóa này không mở được cánh cửa đó."}

    for p in _props_list(floorplan, world):
        if p.get("id") == target_id:
            if p.get("locked_by") == item_id:
                unlocked = state.setdefault("unlocked_props", [])
                if isinstance(unlocked, list) and target_id not in unlocked:
                    unlocked.append(target_id)
                return {"success": True, "unlocked_prop": target_id, "message": "Đã mở khóa vật phẩm."}
            return {"success": False, "message": "Vật phẩm không tác động được lên đồ vật này."}

    return {"success": False, "message": f"Không tìm thấy mục tiêu '{target_id}'."}


def apply_state_delta(
    world: dict[str, Any],
    floorplan: dict[str, Any],
    state: dict[str, Any],
    delta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and apply a StateDelta atomically with clamping, MissingProp reveal, and pose validation."""
    d = schemas.normalize_state_delta(delta)

    if d["add_items"] or d["remove_items"]:
        _mutate_inventory(state, d["add_items"], d["remove_items"])

    flags = state.setdefault("flags", {})
    if d["set_flags"]:
        flags.update(d["set_flags"])

    if d["discover_items"]:
        disc = state.setdefault("discovered_items", [])
        missing_by_tag = {
            str(spec.get("asset_tag")): spec
            for spec in (world.get("missing_props") or [])
            if isinstance(spec, dict) and spec.get("asset_tag")
        }
        for item_tag in d["discover_items"]:
            if item_tag not in disc:
                disc.append(item_tag)
            spec = missing_by_tag.get(item_tag)
            if spec:
                rev_flag = str((spec.get("story_meta") or {}).get("revealed_flag") or "")
                if rev_flag:
                    flags[rev_flag] = True

    for door_id in d["unlock_doors"]:
        _unlock_door(door_id, state)

    if d["energy_delta"] != 0:
        if isinstance(state.get("player"), dict):
            cur_e = int(state["player"].get("energy", state.get("energy", 100)))
            new_e = max(0, min(100, cur_e + d["energy_delta"]))
            state["player"]["energy"] = new_e
        if "energy" in state or not isinstance(state.get("player"), dict):
            cur_e = int(state.get("energy", 100))
            state["energy"] = max(0, min(100, cur_e + d["energy_delta"]))

    if d["stress_delta"] != 0:
        if isinstance(state.get("player"), dict):
            cur_s = int(state["player"].get("stress", state.get("stress", 0)))
            new_s = max(0, min(100, cur_s + d["stress_delta"]))
            state["player"]["stress"] = new_s
        if "stress" in state or not isinstance(state.get("player"), dict):
            cur_s = int(state.get("stress", 0))
            state["stress"] = max(0, min(100, cur_s + d["stress_delta"]))

    if d["cash_delta"] != 0:
        if isinstance(state.get("player"), dict):
            state["player"]["cash"] = max(0, int(state["player"].get("cash", 0)) + d["cash_delta"])
        elif "cash" in state:
            state["cash"] = max(0, int(state.get("cash", 0)) + d["cash_delta"])

    if d["tension_delta"] != 0:
        state["tension"] = max(0, int(state.get("tension", 1)) + d["tension_delta"])

    if d["affinity_deltas"]:
        has_rel = isinstance(state.get("relationships"), dict)
        has_aff = isinstance(state.get("affinity"), dict)
        if has_rel or not has_aff:
            rel = state.setdefault("relationships", {})
            for cid, delta_val in d["affinity_deltas"].items():
                rel[cid] = int(rel.get(cid, 0)) + int(delta_val)
        if has_aff:
            aff = state.setdefault("affinity", {})
            for cid, delta_val in d["affinity_deltas"].items():
                aff[cid] = int(aff.get(cid, 0)) + int(delta_val)

    if d["npc_moods"]:
        for cid, mood in d["npc_moods"].items():
            _update_npc(state, cid, mood=mood)

    if d["reveal_secrets"]:
        rev = state.setdefault("revealed_secrets", [])
        for sec in d["reveal_secrets"]:
            if sec not in rev:
                rev.append(sec)

    if d["pose_updates"]:
        props_by_id = {str(p.get("id")): p for p in _props_list(floorplan, world) if p.get("id")}
        user_id = str((world.get("user") or {}).get("id") or "user")
        for actor_id, pdata in d["pose_updates"].items():
            pose = str(pdata["pose"])
            tp = pdata.get("target_prop")
            if pose != "stand":
                prop_obj = props_by_id.get(str(tp or ""))
                if prop_obj is None:
                    continue
                tag = str(prop_obj.get("asset_tag") or "")
                try:
                    slot = catalog.get_pose_slot(tag, pose)
                except Exception:
                    slot = None
                if slot is None:
                    continue
            if actor_id in ("user", "player", user_id):
                _set_player_pose(state, pose, target_prop=tp)
            else:
                _update_npc(state, actor_id, pose=pose, target_prop=tp)

    if d["lighting_override"] is not None:
        if isinstance(state.get("lighting"), dict):
            room_id = _get_player_room(state) or "global"
            state["lighting"][room_id] = d["lighting_override"]
        state["lighting_override"] = d["lighting_override"]

    for tr in d["spawn_traces"]:
        _append_trace(state, tr)

    if d["time_minutes"] != 0:
        advance_clock_and_sim(world, floorplan, state, d["time_minutes"])

    return state


_KNOWN_CONDITION_KEYS = frozenset(
    {"flag", "flags", "min_turns", "min_affinity", "discovered_items", "min_act"}
)


def _condition_met(cond: Any, state: dict[str, Any]) -> bool:
    if not isinstance(cond, dict) or not cond:
        return False
    flags = state.get("flags") or {}
    affinity: dict[str, Any] = {}
    if isinstance(state.get("affinity"), dict):
        affinity.update(state["affinity"])
    if isinstance(state.get("relationships"), dict):
        affinity.update(state["relationships"])
    turns = max(int(state.get("turns", 0)), len(state.get("history") or []))
    act_index = int(state.get("act_index", 1))

    unknown_keys = set(cond.keys()) - _KNOWN_CONDITION_KEYS
    for uk in unknown_keys:
        if uk == "ending_ready" and cond[uk] is True and bool(flags.get("ending_ready") or state.get("ending_ready")):
            continue
        return False

    if "flag" in cond:
        if not bool(flags.get(cond["flag"])):
            return False

    if "flags" in cond:
        f_req = cond["flags"]
        if isinstance(f_req, dict):
            for k, v in f_req.items():
                if bool(flags.get(k)) != bool(v):
                    return False
        elif isinstance(f_req, list):
            for k in f_req:
                if not bool(flags.get(k)):
                    return False
        else:
            return False

    if "min_turns" in cond:
        if turns < int(cond["min_turns"]):
            return False

    if "min_act" in cond:
        if act_index < int(cond["min_act"]):
            return False

    if "min_affinity" in cond:
        min_aff = cond["min_affinity"]
        if not isinstance(min_aff, dict):
            return False
        for cid, min_v in min_aff.items():
            if int(affinity.get(cid, 0)) < int(min_v):
                return False

    if "discovered_items" in cond:
        req_items = cond["discovered_items"]
        if not isinstance(req_items, list):
            return False
        disc = set(state.get("discovered_items") or [])
        for it in req_items:
            if it not in disc:
                return False

    return True


def check_act_or_ending(
    world: dict[str, Any],
    state: dict[str, Any],
    storylets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check whether current act can advance (Act 1->2->3) or an Ending is triggered (Act 3 only)."""
    act_index = int(state.get("act_index", 1))
    ending: dict[str, Any] | None = None

    if act_index >= 3:
        for end in (world.get("endings") or []):
            if isinstance(end, dict) and _condition_met(end.get("condition"), state):
                ending = copy.deepcopy(end)
                break

    can_advance = False
    next_act: int | None = None
    if act_index < 3:
        spine = None
        for sp in (world.get("acts_spine") or []):
            if isinstance(sp, dict) and int(sp.get("act_index", 0)) == act_index:
                spine = sp
                break
        if spine and _condition_met(spine.get("advance_condition"), state):
            can_advance = True
        else:
            act_storylets = [
                s for s in (storylets or [])
                if isinstance(s, dict) and int(s.get("act_index", 1)) == act_index
            ]
            completed_set = set(state.get("completed_storylets") or [])
            if act_storylets and all(
                bool(s.get("completed")) or (s.get("id") in completed_set)
                for s in act_storylets
            ):
                can_advance = True
        if can_advance:
            next_act = act_index + 1

    return {
        "act_index": act_index,
        "can_advance_act": can_advance,
        "act_transition": can_advance,
        "next_act": next_act,
        "ending": ending,
    }


def enable_free_sandbox(state: dict[str, Any]) -> dict[str, Any]:
    """Enable Free Sandbox mode after an ending while preserving all campaign flags/state."""
    state["free_sandbox_mode"] = True
    return state
