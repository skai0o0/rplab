"""Prompt Registry (Hot-Reload), Turn X-Ray Trace Storage (data/traces/) va State Diff."""
import copy
import hashlib
import json
import os
import re
import threading

import db
import rpformat

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "prompts_default.json")
DATA_DIR = os.path.join(HERE, "data")
CUSTOM_PATH = os.path.join(DATA_DIR, "prompts_custom.json")
TRACES_DIR = os.path.join(DATA_DIR, "traces")

_LOCK = threading.Lock()
_CACHE = {"def_mtime": None, "cust_mtime": None, "default": None, "custom": {}, "merged": None}
_TRACE_LOCK = threading.Lock()
_TRACE_MEM = {}     # (safe_sid, int(turn_id)) -> trace_obj
_LAST_PREFIX = {}   # safe_sid -> last prefix_hash

_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

CRITERION_ORDER = (
    "tra_loi_dung",
    "mo_dien_bien_moi",
    "viet_ho_user",
    "viet_thoai_nv_khac",
    "sai_xung_ho",
    "giong_tro_ly",
    "metagaming_leak",
)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(fallback)


def _ensure_loaded():
    with _LOCK:
        dm = _mtime(DEFAULT_PATH)
        cm = _mtime(CUSTOM_PATH)
        if _CACHE["merged"] is not None and dm == _CACHE["def_mtime"] and cm == _CACHE["cust_mtime"]:
            return _CACHE["default"], _CACHE["custom"], _CACHE["merged"]
        default_data = _load_json(DEFAULT_PATH, {"version": 1, "groups": [], "items": {}})
        custom_raw = _load_json(CUSTOM_PATH, {}) if cm is not None else {}
        custom_items = custom_raw.get("items") if isinstance(custom_raw.get("items"), dict) else (
            custom_raw if isinstance(custom_raw, dict) else {}
        )
        merged_items = {}
        for k, d_item in (default_data.get("items") or {}).items():
            m = copy.deepcopy(d_item)
            c_ov = custom_items.get(k)
            if isinstance(c_ov, dict):
                if m.get("kind") == "criterion":
                    for fk in ("q", "fix"):
                        if isinstance(c_ov.get(fk), str):
                            m[fk] = c_ov[fk]
                    if c_ov.get("threshold") is not None:
                        try:
                            m["threshold"] = max(0.05, min(0.99, float(c_ov["threshold"])))
                        except (TypeError, ValueError):
                            pass
                elif isinstance(c_ov.get("text"), str):
                    m["text"] = c_ov["text"]
            merged_items[k] = m
        _CACHE.update({
            "def_mtime": dm,
            "cust_mtime": cm,
            "default": default_data,
            "custom": custom_items,
            "merged": {"groups": copy.deepcopy(default_data.get("groups") or []), "items": merged_items},
        })
        return _CACHE["default"], _CACHE["custom"], _CACHE["merged"]


def safe_format(template, **kwargs):
    """Thay the {var} an toan: chi thay bien co trong kwargs, khong bao gio nem KeyError/ValueError."""
    if not template or not kwargs:
        return template or ""
    return _PH_RE.sub(lambda m: str(kwargs[m.group(1)]) if m.group(1) in kwargs else m.group(0), str(template))


def validate_placeholders(text, allowed):
    """Tra ve danh sach cac {bien} khong nam trong allowed."""
    allow_set = set(allowed or ())
    bad = []
    for m in _PH_RE.finditer(str(text or "")):
        name = m.group(1)
        if name not in allow_set and name not in bad:
            bad.append(name)
    return bad


def get_item(key):
    _, _, merged = _ensure_loaded()
    return (merged.get("items") or {}).get(key) or {}


def get_text(key, default=""):
    item = get_item(key)
    val = item.get("text")
    return val if isinstance(val, str) else default


def render(key, **kwargs):
    return safe_format(get_text(key), **kwargs)


def get_criteria_config(default_threshold=0.6):
    """Tra ve dict CRITERIA dong (q, fix, label, good, threshold) cho judge.py."""
    _, custom_items, merged = _ensure_loaded()
    items = merged.get("items") or {}
    out = {}
    for k in CRITERION_ORDER:
        full_key = f"judge.{k}"
        it = items.get(full_key) or {}
        c_ov = custom_items.get(full_key) if isinstance(custom_items, dict) else None
        has_custom_thresh = isinstance(c_ov, dict) and c_ov.get("threshold") is not None
        thresh = float(it["threshold"]) if has_custom_thresh else float(default_threshold)
        out[k] = {
            "label": it.get("label", k),
            "q": it.get("q", ""),
            "fix": it.get("fix", ""),
            "good": bool(it.get("good", False)),
            "threshold": thresh,
            "custom_threshold": has_custom_thresh,
        }
    return out


def get_studio_payload():
    """Du lieu day du cho trang /prompts (groups + items kem default & trang thai da chinh)."""
    default_data, custom_items, merged = _ensure_loaded()
    def_items = default_data.get("items") or {}
    mer_items = merged.get("items") or {}
    items_out = {}
    custom_count = 0
    for k, d_it in def_items.items():
        m_it = mer_items.get(k) or d_it
        is_custom = False
        if d_it.get("kind") == "criterion":
            is_custom = (
                m_it.get("q") != d_it.get("q")
                or m_it.get("fix") != d_it.get("fix")
                or float(m_it.get("threshold", 0.6)) != float(d_it.get("threshold", 0.6))
            )
            entry = {
                **d_it,
                "q": m_it.get("q", ""),
                "fix": m_it.get("fix", ""),
                "threshold": float(m_it.get("threshold", 0.6)),
                "default_q": d_it.get("q", ""),
                "default_fix": d_it.get("fix", ""),
                "default_threshold": float(d_it.get("threshold", 0.6)),
                "is_custom": is_custom,
            }
        else:
            is_custom = (m_it.get("text") != d_it.get("text"))
            entry = {
                **d_it,
                "text": m_it.get("text", ""),
                "default_text": d_it.get("text", ""),
                "is_custom": is_custom,
            }
        if is_custom:
            custom_count += 1
        items_out[k] = entry
    return {
        "groups": copy.deepcopy(default_data.get("groups") or []),
        "items": items_out,
        "custom_count": custom_count,
        "custom_path": CUSTOM_PATH,
    }


def _write_custom(custom_items):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CUSTOM_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"version": 1, "items": custom_items}, f, ensure_ascii=False, indent=2)
    with _LOCK:
        _CACHE["merged"] = None


def save_custom(updates):
    """Luu cac muc da chinh vao data/prompts_custom.json (chi luu phan khac mac dinh) -> hot-reload ngay."""
    default_data, custom_items, _ = _ensure_loaded()
    def_items = default_data.get("items") or {}
    next_custom = copy.deepcopy(custom_items) if isinstance(custom_items, dict) else {}
    for k, val in (updates or {}).items():
        d_it = def_items.get(k)
        if not d_it or not isinstance(val, dict):
            continue
        if d_it.get("kind") == "criterion":
            ov = {}
            q_val = val.get("q")
            fix_val = val.get("fix")
            th_val = val.get("threshold")
            if isinstance(q_val, str) and q_val != d_it.get("q"):
                ov["q"] = q_val
            if isinstance(fix_val, str) and fix_val != d_it.get("fix"):
                ov["fix"] = fix_val
            if th_val is not None:
                try:
                    tf = round(max(0.05, min(0.99, float(th_val))), 3)
                    if abs(tf - float(d_it.get("threshold", 0.6))) > 1e-6:
                        ov["threshold"] = tf
                except (TypeError, ValueError):
                    pass
            if ov:
                next_custom[k] = ov
            else:
                next_custom.pop(k, None)
        else:
            txt = val.get("text")
            if isinstance(txt, str) and txt != d_it.get("text"):
                next_custom[k] = {"text": txt}
            else:
                next_custom.pop(k, None)
    _write_custom(next_custom)
    return get_studio_payload()


def reset_custom(keys=None):
    """Khoi phuc mac dinh cho 1 hoac tat ca cac muc."""
    _, custom_items, _ = _ensure_loaded()
    if not keys:
        _write_custom({})
    else:
        next_custom = copy.deepcopy(custom_items) if isinstance(custom_items, dict) else {}
        for k in keys:
            next_custom.pop(k, None)
        _write_custom(next_custom)
    return get_studio_payload()


# ----------------------------------------------------------------------------- Turn X-Ray & State Diff
def est_tokens(text):
    s = str(text or "")
    return 0 if not s else max(1, int(len(s) / 3.5))


def split_4zones(msgs):
    """Tach msgs (3 phan tu tu compile_context_4zones) thanh 4 Zone + tinh token & prefix_hash."""
    z1_text = (msgs[0].get("content") or "") if len(msgs) > 0 else ""
    z23_text = (msgs[1].get("content") or "") if len(msgs) > 1 else ""
    z4_text = (msgs[2].get("content") or "") if len(msgs) > 2 else ""

    marker = "[DIỄN BIẾN HỘI THOẠI NGUYÊN VĂN"
    idx = z23_text.find(marker)
    if idx >= 0:
        z2_text = z23_text[:idx].strip()
        z3_text = z23_text[idx:].strip()
    else:
        z2_text = ""
        z3_text = z23_text

    t1, t2, t3, t4 = est_tokens(z1_text), est_tokens(z2_text), est_tokens(z3_text), est_tokens(z4_text)
    prefix_hash = hashlib.sha1((z1_text + "\n===Z2===\n" + z2_text).encode("utf-8")).hexdigest()[:12]
    return {
        "z1": {"label": "Zone 1 — Tĩnh (System Core & Roster)", "text": z1_text, "tokens": t1},
        "z2": {"label": "Zone 2 — Bán tĩnh (Cốt truyện & Tóm tắt cảnh)", "text": z2_text, "tokens": t2},
        "z3": {"label": "Zone 3 — Working Memory (Hội thoại nguyên văn)", "text": z3_text, "tokens": t3},
        "z4": {"label": "Zone 4 — Động mỗi lượt (Recency Bias)", "text": z4_text, "tokens": t4},
        "prefix_tokens": t1 + t2,
        "prefix_hash": prefix_hash,
        "total_tokens": t1 + t2 + t3 + t4,
    }


def compute_state_diff(pack, before, after):
    """So sanh Session State truoc -> sau va tra ve cac bien thuc su thay doi."""
    b = before or {}
    a = after or {}
    diffs = []
    chars = (pack or {}).get("characters") or {}
    user_name = (pack.get("user") or {}).get("name", "User") if isinstance((pack or {}).get("user"), dict) else "User"

    def ename(eid):
        return user_name if eid == "user" else ((chars.get(eid) or {}).get("meta") or {}).get("name", eid)

    # 1. Clock
    bc = rpformat.format_clock(b["clock"]) if isinstance(b.get("clock"), dict) else str(b.get("thoi_gian", ""))
    ac = rpformat.format_clock(a["clock"]) if isinstance(a.get("clock"), dict) else str(a.get("thoi_gian", ""))
    if bc != ac:
        diffs.append({"key": "clock", "label": "Đồng hồ", "before": bc, "after": ac})

    # 2. Tension & Scene Vibe
    bt, at = b.get("tension"), a.get("tension")
    if bt != at:
        diffs.append({"key": "tension", "label": "Độ căng", "before": str(bt), "after": str(at)})
    bv, av = b.get("scene_vibe"), a.get("scene_vibe")
    if bv != av:
        diffs.append({"key": "scene_vibe", "label": "Nhịp kịch tính", "before": str(bv), "after": str(av)})

    # 3. Active channel
    bch = b.get("active_channel") or {}
    ach = a.get("active_channel") or {}
    b_ch_s = f"{bch.get('channel_type', '')} [{', '.join(ename(p) for p in (bch.get('participants') or []))}]"
    a_ch_s = f"{ach.get('channel_type', '')} [{', '.join(ename(p) for p in (ach.get('participants') or []))}]"
    if b_ch_s != a_ch_s:
        diffs.append({"key": "active_channel", "label": "Kênh giao tiếp", "before": b_ch_s, "after": a_ch_s})

    # 4. Entities (location, mood, outfit)
    b_ents = b.get("entities") or {}
    a_ents = a.get("entities") or {}
    for eid in dict.fromkeys(list(b_ents.keys()) + list(a_ents.keys())):
        be, ae = b_ents.get(eid) or {}, a_ents.get(eid) or {}
        for fld, flbl in (("location_id", "vị trí"), ("mood", "tâm trạng"), ("outfit", "trang phục")):
            if be.get(fld) != ae.get(fld):
                diffs.append({
                    "key": f"entities.{eid}.{fld}",
                    "label": f"{ename(eid)} · {flbl}",
                    "before": str(be.get(fld, "")),
                    "after": str(ae.get(fld, "")),
                })

    # 5. Relationship edges (level, default self/call, lock)
    b_edges = (b.get("relationships") or {}).get("edges") or {}
    a_edges = (a.get("relationships") or {}).get("edges") or {}
    for src in dict.fromkeys(list(b_edges.keys()) + list(a_edges.keys())):
        bs, as_ = b_edges.get(src) or {}, a_edges.get(src) or {}
        for tgt in dict.fromkeys(list(bs.keys()) + list(as_.keys())):
            be, ae = bs.get(tgt) or {}, as_.get(tgt) or {}
            if be.get("level") != ae.get("level"):
                diffs.append({
                    "key": f"rel.{src}->{tgt}.level",
                    "label": f"Quan hệ {ename(src)} → {ename(tgt)} (level)",
                    "before": str(be.get("level")),
                    "after": str(ae.get("level")),
                })
            bd, ad = be.get("default") or {}, ae.get("default") or {}
            b_pair = f"{bd.get('self', '')} – {bd.get('call', '')}"
            a_pair = f"{ad.get('self', '')} – {ad.get('call', '')}"
            if b_pair != a_pair:
                diffs.append({
                    "key": f"rel.{src}->{tgt}.pronoun",
                    "label": f"Xưng hô {ename(src)} → {ename(tgt)}",
                    "before": b_pair,
                    "after": a_pair,
                })
            if be.get("lock") != ae.get("lock"):
                diffs.append({
                    "key": f"rel.{src}->{tgt}.lock",
                    "label": f"Khóa {ename(src)} → {ename(tgt)}",
                    "before": str(be.get("lock")),
                    "after": str(ae.get("lock")),
                })

    # 6. Hard Memory
    b_hm = {f.get("id"): f for f in (b.get("hard_memory") or []) if f.get("id")}
    a_hm = {f.get("id"): f for f in (a.get("hard_memory") or []) if f.get("id")}
    for fid, af in a_hm.items():
        if fid not in b_hm:
            diffs.append({"key": f"hard_memory.{fid}", "label": "Hard Memory (+mới)", "before": "(chưa có)", "after": af.get("text", "")})
        else:
            bf = b_hm[fid]
            if bf.get("status") != af.get("status") or bf.get("known_by") != af.get("known_by"):
                diffs.append({
                    "key": f"hard_memory.{fid}",
                    "label": f"Hard Memory ({fid})",
                    "before": f"{bf.get('status')} · biết: {', '.join(bf.get('known_by') or [])}",
                    "after": f"{af.get('status')} · biết: {', '.join(af.get('known_by') or [])}",
                })

    # 7. Summaries
    b_sc, a_sc = len(b.get("recent_scenes") or []), len(a.get("recent_scenes") or [])
    if b_sc != a_sc or b.get("summarized_upto") != a.get("summarized_upto"):
        diffs.append({
            "key": "recent_scenes",
            "label": "Cảnh đã tóm tắt",
            "before": f"{b_sc} cảnh (upto #{b.get('summarized_upto', 0)})",
            "after": f"{a_sc} cảnh (upto #{a.get('summarized_upto', 0)})",
        })
    if (b.get("story_arc") or "") != (a.get("story_arc") or ""):
        diffs.append({
            "key": "story_arc",
            "label": "Đại cương cốt truyện",
            "before": (b.get("story_arc") or "")[:120],
            "after": (a.get("story_arc") or "")[:120],
        })
    return diffs


def _safe_sid(session_id):
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(session_id or "default")).strip("._")
    return s or "default"


def _last_prefix_from_disk(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            last = None
            for line in f:
                if line.strip():
                    last = line
        if last:
            obj = json.loads(last)
            return ((obj.get("zones_info") or {}).get("prefix_hash")) or obj.get("prefix_hash")
    except Exception:
        return None
    return None


def save_trace(session_id, trace_obj):
    """Luu ban ghi Turn X-Ray vao SQLite va tra ve metadata ngan gon."""
    sid = _safe_sid(session_id)
    turn_id = int(trace_obj.get("turn_id") or 0)
    path = os.path.join(TRACES_DIR, f"{sid}.jsonl")

    zi = trace_obj.get("zones_info") or {}
    cur_hash = zi.get("prefix_hash") or ""
    with _TRACE_LOCK:
        prev_hash = _LAST_PREFIX.get(sid)
        if prev_hash is None:
            prev_hash = db.get_last_prefix_hash(sid) or _last_prefix_from_disk(path)
        if not prev_hash:
            cache_status = "first"
        elif prev_hash == cur_hash:
            cache_status = "hit"
        else:
            cache_status = "miss"
        zi["cache_status"] = cache_status
        zi["prev_prefix_hash"] = prev_hash
        trace_obj["zones_info"] = zi

        db.save_trace(sid, turn_id, trace_obj, prefix_hash=cur_hash)
        _LAST_PREFIX[sid] = cur_hash
        _TRACE_MEM[(sid, turn_id)] = trace_obj
    return {
        "cache_status": cache_status,
        "prefix_hash": cur_hash,
        "prefix_tokens": zi.get("prefix_tokens", 0),
        "total_tokens": zi.get("total_tokens", 0),
    }


def load_trace(session_id, turn_id):
    """Doc ban ghi Turn X-Ray moi nhat cua turn_id tu RAM, SQLite, hoac file JSONL cu."""
    sid = _safe_sid(session_id)
    tid = int(turn_id)
    with _TRACE_LOCK:
        if (sid, tid) in _TRACE_MEM:
            return _TRACE_MEM[(sid, tid)]
        obj = db.load_trace(sid, tid)
        if obj is not None:
            _TRACE_MEM[(sid, tid)] = obj
            return obj
        path = os.path.join(TRACES_DIR, f"{sid}.jsonl")
        if not os.path.exists(path):
            return None
        found = None
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(item.get("turn_id") or -1) == tid:
                        found = item
            if found is not None:
                _TRACE_MEM[(sid, tid)] = found
        except OSError:
            return None
        return found
