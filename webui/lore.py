"""Engine lorebook (World Info) don gian, theo cach cua SillyTavern.

Nhan 2 dinh dang:
  - World Info cua SillyTavern: {"entries": {"0": {key, keysecondary, content, constant, selective, selectiveLogic,
    order, position, depth, role, disable, excludeRecursion, preventRecursion, probability, useProbability, ...}}}
  - character_book trong card (CCv2/v3): {"entries": [{keys, secondary_keys, content, enabled, insertion_order,
    constant, selective, position: before_char|after_char, extensions: {...}}]}

Kich hoat moi luot:
  1. Muc constant luon bat.
  2. Quet `scan_depth` tin gan nhat: muc bat khi co 1 tu khoa chinh khop (chuoi con, khong phan biet hoa thuong,
     hoac regex dang /mau/co) va thoa dieu kien tu khoa phu (selectiveLogic: 0 AND ANY, 1 NOT ALL, 2 NOT ANY, 3 AND ALL).
  3. De quy (toi da `max_recursion` vong): quet noi dung cac muc vua bat de bat tiep muc khac
     (bo qua muc preventRecursion lam nguon, va muc excludeRecursion khong bi bat qua de quy).
  4. Xac suat (useProbability), roi gioi han token (`budget`, uoc tinh ky tu/3.5): uu tien constant, roi order cao.
Vi tri chen: before (truoc mo ta nhan vat), after (sau, mac dinh), depth (chen vao lich su o do sau N, role tuy chon).
"""
import random, re

POS_INT = {0: "before", 1: "after", 4: "depth"}  # 2,3 (Author's Note), 5,6 (vi du), 7 (outlet) -> after
ROLE_INT = {0: "system", 1: "user", 2: "assistant"}


def _norm_entry(e, i, source):
    ext = e.get("extensions") or {}
    keys = e.get("keys", e.get("key")) or []
    sec = e.get("secondary_keys", e.get("keysecondary")) or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")]
    if isinstance(sec, str):
        sec = [k.strip() for k in sec.split(",")]
    pos = e.get("position")
    if isinstance(pos, str):
        pos = {"before_char": 0, "after_char": 1}.get(pos, 1)
    if "position" in ext and isinstance(ext["position"], int):
        pos = ext["position"]
    role = ext.get("role", e.get("role"))
    enabled = e.get("enabled", True) and not e.get("disable", False)
    prob = ext.get("probability", e.get("probability"))
    use_prob = ext.get("useProbability", e.get("useProbability", False))
    return {
        "uid": str(e.get("uid", e.get("id", i))),
        "source": source,
        "comment": e.get("comment") or e.get("name") or (keys[0] if keys else f"#{i}"),
        "keys": [k for k in keys if k],
        "secondary_keys": [k for k in sec if k],
        "logic": int(ext.get("selectiveLogic", e.get("selectiveLogic", 0)) or 0),
        "selective": bool(e.get("selective", True)),
        "content": e.get("content") or "",
        "constant": bool(e.get("constant", False)),
        "enabled": bool(enabled),
        "order": int(e.get("insertion_order", e.get("order", 100)) or 0),
        "position": POS_INT.get(pos if isinstance(pos, int) else 1, "after"),
        "depth": int(ext.get("depth", e.get("depth", 4)) or 4),
        "role": ROLE_INT.get(role, "system") if isinstance(role, int) else (role or "system"),
        "probability": int(prob) if (use_prob and prob is not None) else 100,
        "case_sensitive": bool(ext.get("case_sensitive", e.get("caseSensitive")) or False),
        # None = theo cai dat chung (mac dinh khop nguyen tu: "Hân" khong khop vao "nhân vật")
        "whole_words": ext.get("match_whole_words", e.get("matchWholeWords")),
        "exclude_recursion": bool(ext.get("exclude_recursion", e.get("excludeRecursion")) or False),
        "prevent_recursion": bool(ext.get("prevent_recursion", e.get("preventRecursion")) or False),
    }


def normalize_book(obj, name="Lorebook", source=None):
    """Tra ve {name, entries:[...]} tu World Info ST, character_book, hoac card chua character_book."""
    if "data" in obj and isinstance(obj["data"], dict) and obj["data"].get("character_book"):
        name = obj["data"].get("name", name) + " (lorebook trong card)"
        obj = obj["data"]["character_book"]
    elif obj.get("character_book"):
        obj = obj["character_book"]
    raw = obj.get("entries")
    if raw is None:
        raise ValueError("Không thấy 'entries' trong file lorebook")
    items = list(raw.values()) if isinstance(raw, dict) else list(raw)
    name = obj.get("name") or name
    src = source or name
    return {"name": name, "entries": [_norm_entry(e, i, src) for i, e in enumerate(items)]}


def _match(key, text, e, whole_default=True):
    if len(key) > 2 and key.startswith("/") and key.rfind("/") > 0:
        body, flags = key[1:key.rfind("/")], key[key.rfind("/") + 1:]
        try:
            return re.search(body, text, re.I if "i" in flags else 0) is not None
        except re.error:
            return False
    k, t = (key, text) if e["case_sensitive"] else (key.lower(), text.lower())
    whole = whole_default if e.get("whole_words") is None else e["whole_words"]
    if whole:
        return re.search(r"(?<!\w)" + re.escape(k.strip()) + r"(?!\w)", t) is not None
    return k in t


def _triggered(e, text, whole=True):
    hit = next((k for k in e["keys"] if _match(k, text, e, whole)), None)
    if not hit:
        return None
    sec = e["secondary_keys"]
    if e["selective"] and sec:
        m = [_match(k, text, e, whole) for k in sec]
        ok = {0: any(m), 1: not all(m), 2: not any(m), 3: all(m)}.get(e["logic"], any(m))
        if not ok:
            return None
    return hit


def activate(books, history, scan_depth=4, recursive=True, max_recursion=3, budget=6000, whole_words=True, rng=random):
    """books: [{name, entries}], history: [{role, content}] -> (danh sach muc da bat, log ly do)."""
    entries = [e for b in books for e in b["entries"] if e["enabled"] and e["content"].strip()]
    scan = "\n".join(m["content"] for m in history[-scan_depth:]) if scan_depth > 0 else ""
    active, why = {}, {}
    for e in entries:
        if e["constant"]:
            active[id(e)], why[id(e)] = e, "luôn bật"
        else:
            hit = _triggered(e, scan, whole_words)
            if hit:
                active[id(e)], why[id(e)] = e, f"từ khóa “{hit}”"
    rounds = 0
    while recursive and rounds < max_recursion:
        rounds += 1
        rec_text = "\n".join(e["content"] for e in active.values() if not e["prevent_recursion"])
        new = 0
        for e in entries:
            if id(e) in active or e["exclude_recursion"] or e["constant"]:
                continue
            hit = _triggered(e, rec_text, whole_words)
            if hit:
                src = next((a["comment"] for a in active.values()
                            if not a["prevent_recursion"] and _match(hit, a["content"], e, whole_words)), "?")
                active[id(e)], why[id(e)] = e, f"đệ quy từ “{src}” (từ khóa “{hit}”)"
                new += 1
        if not new:
            break
    picked, used, log = [], 0, []
    for e in sorted(active.values(), key=lambda e: (not e["constant"], -e["order"])):
        if e["probability"] < 100 and rng.randint(1, 100) > e["probability"]:
            log.append({"entry": e["comment"], "book": e["source"], "status": f"bỏ qua (xác suất {e['probability']}%)"})
            continue
        cost = int(len(e["content"]) / 3.5) + 1
        if budget and used + cost > budget and not e["constant"]:
            log.append({"entry": e["comment"], "book": e["source"], "status": f"bỏ qua (vượt ngân sách {budget} token)"})
            continue
        used += cost
        picked.append(e)
        log.append({"entry": e["comment"], "book": e["source"], "status": why[id(e)], "position": e["position"]
                    + (f" {e['depth']}" if e["position"] == "depth" else ""), "tokens": cost})
    picked.sort(key=lambda e: e["order"])
    return picked, log, used
