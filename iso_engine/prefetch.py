"""Selective Dependency Mask (masked_state_hash) and P0/P1 PrefetchManager."""

from __future__ import annotations

import copy
import hashlib
import heapq
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from . import db, schemas
except ImportError:
    import db
    import schemas


PRIO_P0 = 0
PRIO_P1 = 1


def _extract_inventory_set(state: dict[str, Any]) -> set[str]:
    items: set[str] = set()
    top_inv = state.get("inventory")
    if isinstance(top_inv, (list, tuple, set)):
        for x in top_inv:
            if x is not None:
                items.add(str(x))
    player = state.get("player")
    if isinstance(player, dict):
        p_inv = player.get("inventory")
        if isinstance(p_inv, (list, tuple, set)):
            for x in p_inv:
                if x is not None:
                    items.add(str(x))
    return items


def _get_nested(state: dict[str, Any], path: str) -> Any:
    if path in state:
        return state[path]
    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def masked_state_hash(state: dict | None, relevant_keys: dict | None) -> str:
    """Compute a deterministic hash over ONLY the variables listed in relevant_keys.

    Ignores clock, player position, unrelated flags, unrelated items, and unrelated NPC affinities.
    """
    st = state if isinstance(state, dict) else {}
    rk = relevant_keys if isinstance(relevant_keys, dict) else {}

    flags_map = st.get("flags") if isinstance(st.get("flags"), dict) else {}
    flag_keys = sorted({str(k) for k in (rk.get("flags") or []) if k is not None})
    flags_proj = {k: flags_map.get(k) for k in flag_keys}

    inv_set = _extract_inventory_set(st)
    item_keys = sorted({str(k) for k in (rk.get("items") or []) if k is not None})
    items_proj = {k: (k in inv_set) for k in item_keys}

    aff_map: dict[str, Any] = {}
    if isinstance(st.get("affinity"), dict):
        aff_map.update(st["affinity"])
    if isinstance(st.get("relationships"), dict):
        aff_map.update(st["relationships"])
    aff_keys = sorted({str(k) for k in (rk.get("affinity_with") or []) if k is not None})
    aff_proj = {k: aff_map.get(k) for k in aff_keys}

    extra_keys = sorted({str(k) for k in (rk.get("extra") or []) if k is not None})
    extra_proj = {k: _get_nested(st, k) for k in extra_keys}

    payload = {
        "flags": flags_proj,
        "items": items_proj,
        "affinity_with": aff_proj,
        "extra": extra_proj,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


@dataclass(order=False)
class _PrefetchTask:
    campaign_id: str
    storylet_id: str
    masked_hash: str
    storylet: dict[str, Any]
    state: dict[str, Any]
    llm: Any
    priority: int
    branch_group: str
    group_gen: int
    db_path: str | None
    seq: int
    enqueued_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    cancel_counted: bool = False
    world_context: dict[str, Any] | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.campaign_id, self.storylet_id, self.masked_hash)


def _build_scene_messages(task: _PrefetchTask) -> list[dict[str, str]]:
    st = task.storylet
    rk = st.get("relevant_keys") if isinstance(st.get("relevant_keys"), dict) else {}
    state = task.state
    flags_map = state.get("flags") if isinstance(state.get("flags"), dict) else {}
    aff_map: dict[str, Any] = {}
    if isinstance(state.get("affinity"), dict):
        aff_map.update(state["affinity"])
    if isinstance(state.get("relationships"), dict):
        aff_map.update(state["relationships"])
    inv_set = _extract_inventory_set(state)

    masked_ctx = {
        "flags": {k: flags_map.get(k) for k in (rk.get("flags") or [])},
        "items": {k: (k in inv_set) for k in (rk.get("items") or [])},
        "affinity": {k: aff_map.get(k) for k in (rk.get("affinity_with") or [])},
    }
    prompt_payload = {
        "storylet_id": task.storylet_id,
        "title": st.get("title") or task.storylet_id,
        "act_index": st.get("act_index", 1),
        "trigger": st.get("trigger") or {},
        "masked_state": masked_ctx,
        "world_context": task.world_context or {},
    }
    return [
        {
            "role": "system",
            "content": (
                "Bạn là Narrative Scene Compiler cho game Isometric Point-and-Click tiếng Việt. "
                "Hãy biên dịch 1 SceneNodeTree JSON hợp lệ gồm root_node_id và nodes "
                "(mỗi node có node_id, text, speaker_id, presentation_mode, pose_updates, lighting, auto_effects, choices)."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt_payload, ensure_ascii=False),
        },
    ]


def _normalize_scene_tree(raw_tree: Any, storylet_id: str) -> dict[str, Any]:
    if not isinstance(raw_tree, dict):
        raise ValueError("Compiled scene tree must be a dict")
    tree = copy.deepcopy(raw_tree)
    nodes = tree.get("nodes")
    if isinstance(nodes, dict) and nodes:
        if not tree.get("root_node_id"):
            tree["root_node_id"] = next(iter(nodes.keys()))
        try:
            validated = schemas.validate_scene_node_tree(tree)
            validated["storylet_id"] = storylet_id
            return validated
        except Exception:
            pass
    tree.setdefault("storylet_id", storylet_id)
    return tree


class PrefetchManager:
    """Priority background SceneNodeTree compiler (P0/P1, max 2 workers, depth=1)."""

    PRIO_P0 = PRIO_P0
    PRIO_P1 = PRIO_P1

    def __init__(
        self,
        max_workers: int = 2,
        max_prefetch_depth: int = 1,
        *,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        coalesce_ms: float = 15.0,
    ) -> None:
        self.max_workers: int = max(1, min(2, int(max_workers)))
        self.max_prefetch_depth: int = max(0, int(max_prefetch_depth))
        self._on_status = on_status
        self._coalesce_sec: float = max(0.0, float(coalesce_ms) / 1000.0)

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._heap: list[tuple[int, int, _PrefetchTask]] = []
        self._seq: int = 0
        self._pending_by_key: dict[tuple[str, str, str], _PrefetchTask] = {}
        self._in_flight: dict[tuple[str, str, str], _PrefetchTask] = {}
        self._group_gen: dict[str, int] = {}

        self._completed: int = 0
        self._cache_hits: int = 0
        self._cancelled: int = 0
        self._errors: int = 0
        self._active_branch: str = ""
        self._last_error: str = ""

        self._stopping: bool = False
        self._drop_pending_on_stop: bool = False
        self._workers: list[threading.Thread] = []
        for i in range(self.max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"iso-prefetch-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    def _emit_status(self) -> None:
        cb = self._on_status
        if cb is None:
            return
        try:
            cb(self.get_status())
        except Exception:
            pass

    def _is_group_stale(self, task: _PrefetchTask) -> bool:
        if not task.branch_group:
            return False
        return self._group_gen.get(task.branch_group, 0) != task.group_gen

    def _is_task_cancelled(self, task: _PrefetchTask) -> bool:
        with self._lock:
            return task.cancelled or self._is_group_stale(task)

    def enqueue(
        self,
        campaign_id: str,
        storylet: dict[str, Any],
        state: dict[str, Any],
        llm: Any,
        priority: int = PRIO_P1,
        branch_group: str = "",
        db_path: str | None = None,
        *,
        depth: int = 1,
        world_context: dict[str, Any] | None = None,
    ) -> bool:
        if self.max_prefetch_depth <= 0 or int(depth) > self.max_prefetch_depth:
            return False
        if not campaign_id or not isinstance(storylet, dict) or llm is None:
            return False
        storylet_id = str(storylet.get("id") or "").strip()
        if not storylet_id:
            return False

        rk = storylet.get("relevant_keys") if isinstance(storylet.get("relevant_keys"), dict) else {}
        mhash = masked_state_hash(state, rk)

        if db.get_cached_scene(campaign_id, storylet_id, mhash, db_path=db_path) is not None:
            with self._lock:
                self._cache_hits += 1
            self._emit_status()
            return False

        prio = int(priority)
        grp = str(branch_group or "")
        key = (str(campaign_id), storylet_id, mhash)

        with self._cond:
            if self._stopping:
                return False
            if key in self._in_flight:
                existing_fl = self._in_flight[key]
                if not existing_fl.cancelled and not self._is_group_stale(existing_fl):
                    return False

            existing = self._pending_by_key.get(key)
            if existing is not None and not existing.cancelled and not self._is_group_stale(existing):
                changed = False
                if prio < existing.priority:
                    existing.priority = prio
                    changed = True
                if grp and not existing.branch_group:
                    existing.branch_group = grp
                    existing.group_gen = self._group_gen.get(grp, 0)
                    changed = True
                if changed:
                    self._heap = [
                        (t.priority, t.seq, t)
                        for _, _, t in self._heap
                        if not t.cancelled and not self._is_group_stale(t)
                    ]
                    heapq.heapify(self._heap)
                    self._cond.notify_all()
                return True

            self._seq += 1
            gen = self._group_gen.get(grp, 0) if grp else 0
            task = _PrefetchTask(
                campaign_id=str(campaign_id),
                storylet_id=storylet_id,
                masked_hash=mhash,
                storylet=copy.deepcopy(storylet),
                state=copy.deepcopy(state if isinstance(state, dict) else {}),
                llm=llm,
                priority=prio,
                branch_group=grp,
                group_gen=gen,
                db_path=db_path,
                seq=self._seq,
                world_context=copy.deepcopy(world_context) if isinstance(world_context, dict) else None,
            )
            self._pending_by_key[key] = task
            heapq.heappush(self._heap, (prio, task.seq, task))
            self._cond.notify_all()

        self._emit_status()
        return True

    def promote_branch(
        self,
        active_branch_group: str,
        cancel_other_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        active = str(active_branch_group or "")
        cancel_set = {str(g) for g in (cancel_other_groups or []) if g}
        cancelled_here = 0

        with self._cond:
            self._active_branch = active
            for grp in cancel_set:
                self._group_gen[grp] = self._group_gen.get(grp, 0) + 1

            new_heap: list[tuple[int, int, _PrefetchTask]] = []
            for _, _, task in self._heap:
                if task.cancelled or self._is_group_stale(task) or (task.branch_group in cancel_set):
                    if not task.cancel_counted:
                        task.cancel_counted = True
                        self._cancelled += 1
                        cancelled_here += 1
                    task.cancelled = True
                    self._pending_by_key.pop(task.key, None)
                    continue
                if active and task.branch_group == active:
                    task.priority = PRIO_P0
                new_heap.append((task.priority, task.seq, task))

            heapq.heapify(new_heap)
            self._heap = new_heap

            for task in self._in_flight.values():
                if task.branch_group in cancel_set or self._is_group_stale(task):
                    if not task.cancel_counted:
                        task.cancel_counted = True
                        self._cancelled += 1
                        cancelled_here += 1
                    task.cancelled = True

            self._cond.notify_all()

        self._emit_status()
        return {"promoted": active, "cancelled_count": cancelled_here}

    def _worker_loop(self) -> None:
        while True:
            task: _PrefetchTask | None = None
            with self._cond:
                while True:
                    while not self._heap and not self._stopping:
                        self._cond.wait()

                    if self._stopping and (self._drop_pending_on_stop or not self._heap):
                        return

                    if not self._heap:
                        continue

                    prio, _, peek_task = self._heap[0]
                    if peek_task.cancelled or self._is_group_stale(peek_task):
                        heapq.heappop(self._heap)
                        self._pending_by_key.pop(peek_task.key, None)
                        if not peek_task.cancel_counted:
                            peek_task.cancel_counted = True
                            self._cancelled += 1
                        continue

                    # Short coalescing window for P1 tasks so immediate promote_branch() can cancel siblings
                    if prio >= PRIO_P1 and self._coalesce_sec > 0 and not self._stopping:
                        rem = (peek_task.enqueued_at + self._coalesce_sec) - time.monotonic()
                        if rem > 0:
                            self._cond.wait(timeout=rem)
                            continue

                    _, _, task = heapq.heappop(self._heap)
                    self._pending_by_key.pop(task.key, None)
                    if task.cancelled or self._is_group_stale(task):
                        if not task.cancel_counted:
                            task.cancel_counted = True
                            self._cancelled += 1
                        task = None
                        continue

                    self._in_flight[task.key] = task
                    break

            if task is None:
                continue

            try:
                if self._is_task_cancelled(task):
                    continue

                if db.get_cached_scene(task.campaign_id, task.storylet_id, task.masked_hash, db_path=task.db_path) is not None:
                    with self._lock:
                        self._cache_hits += 1
                    continue

                messages = _build_scene_messages(task)
                if self._is_task_cancelled(task):
                    continue

                raw_tree, meta = task.llm.complete_json(messages, schema_hint="SceneNodeTree")
                if self._is_task_cancelled(task):
                    continue

                normalized = _normalize_scene_tree(raw_tree, task.storylet_id)
                if self._is_task_cancelled(task):
                    continue

                db.put_cached_scene(
                    task.campaign_id,
                    task.storylet_id,
                    task.masked_hash,
                    normalized,
                    model=str((meta or {}).get("model") or ""),
                    cost_usd=float((meta or {}).get("cost_usd") or 0.0),
                    db_path=task.db_path,
                )
                with self._lock:
                    self._completed += 1
            except Exception as exc:
                with self._lock:
                    self._errors += 1
                    self._last_error = str(exc)
            finally:
                with self._cond:
                    if self._in_flight.get(task.key) is task:
                        self._in_flight.pop(task.key, None)
                    self._cond.notify_all()
                self._emit_status()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            queued = sum(
                1
                for _, _, t in self._heap
                if not t.cancelled and not self._is_group_stale(t)
            )
            return {
                "max_workers": self.max_workers,
                "max_prefetch_depth": self.max_prefetch_depth,
                "queued": queued,
                "active": len(self._in_flight),
                "completed": self._completed,
                "cache_hits": self._cache_hits,
                "cancelled": self._cancelled,
                "errors": self._errors,
                "active_branch": self._active_branch,
                "last_error": self._last_error,
                "stopping": self._stopping,
            }

    def shutdown(self, wait: bool = False) -> None:
        with self._cond:
            self._stopping = True
            if not wait:
                self._drop_pending_on_stop = True
                for _, _, t in self._heap:
                    t.cancelled = True
                self._heap.clear()
                self._pending_by_key.clear()
            else:
                # Drain queued + in-flight tasks before exiting
                self._drop_pending_on_stop = False
            self._cond.notify_all()

        timeout = 30.0 if wait else 0.5
        for worker in self._workers:
            if worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=timeout)
