"""He thong Lenh ngoai bang (Out-of-Band Slash Commands) cho RP Lab Group v2.

Tat ca tin nhan lenh co `in_context: False` -> cach ly tuyet doi khoi KV Cache,
Working Memory, Lorebook scan, Decision Sensors, Gate va Scene Summary.
"""
import copy
import re

import group
import prompts
import rpformat


def _norm_key(s):
    return re.sub(r"\s+", " ", str(s or "").replace("_", " ").strip()).lower()


def resolve_mention(pack, query):
    """Khop @mention khong phan biet hoa/thuong, dau tieng Viet va dau '_' -> char_id."""
    q = _norm_key(str(query or "").lstrip("@"))
    if not q:
        return None
    q_slug = rpformat.slug(q)
    chars = pack.get("characters") or {}
    order = [c["char_id"] for c in (pack.get("cast") or []) if c.get("char_id") in chars]
    for cid in chars:
        if cid not in order:
            order.append(cid)

    # 1. Khop chinh xac id / slug / ten / ten rieng / alias
    for cid in order:
        meta = (chars[cid].get("meta") or {})
        name = meta.get("name") or cid
        aliases = list(meta.get("aliases") or [])
        candidates = [cid, name, name.split()[-1]] + aliases
        for cand in candidates:
            cn = _norm_key(cand)
            if not cn:
                continue
            if q == cn or (q_slug and q_slug == rpformat.slug(cn)):
                return cid

    # 2. Khop tien to / chuoi con (uu tien chuoi dai nhat)
    best_cid, best_score = None, -1
    for cid in order:
        meta = (chars[cid].get("meta") or {})
        name = meta.get("name") or cid
        aliases = list(meta.get("aliases") or [])
        candidates = [name, name.split()[-1]] + aliases + [cid]
        for cand in candidates:
            cs = rpformat.slug(_norm_key(cand))
            if not cs or not q_slug:
                continue
            if cs.startswith(q_slug) or q_slug in cs:
                score = len(q_slug) * 10 - abs(len(cs) - len(q_slug))
                if score > best_score:
                    best_cid, best_score = cid, score
    return best_cid


def extract_mention(arg_str, pack):
    """Tach @mention khoi chuoi tham so -> (char_id | None, remaining_text)."""
    s = str(arg_str or "").strip()
    idx = s.find("@")
    if idx < 0:
        return None, s
    prefix = s[:idx].strip()
    after = s[idx + 1:]
    toks = after.split()
    for k in range(min(3, len(toks)), 0, -1):
        cand = " ".join(toks[:k]).strip(".,:;!?")
        cid = resolve_mention(pack, cand)
        if cid:
            rest = " ".join(([prefix] if prefix else []) + toks[k:]).strip()
            return cid, rest
    return None, s


def _lock_label(edge):
    lk = (edge or {}).get("lock")
    if lk == "observed":
        return "🔒 Đã khóa từ lời thoại"
    if lk == "manual":
        return "🔒 Khóa tay"
    return "🔓 Chưa khóa"


def _strip_tone_pronouns(tone_txt):
    cleaned = re.sub(
        r"(?i)\b(?:xưng\s+hô(?:\s+với\s+[^.:;]+)?|xưng)\s*:?\s*(?:tôi|tui|tao|em|anh|chị|mình|tớ)\s*[-–/]\s*(?:cậu|bạn|ông|bà|mày|em|anh|chị)[^.;]*[.;]?",
        "",
        tone_txt or "",
    )
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _infer_reverse_pair(u_self, u_call, ug, user_name):
    us, uc = u_self.strip().lower(), u_call.strip().lower()
    u_is_f = (rpformat.norm_gender(ug) == "nu")
    if us in ("tao", "mày") or uc == "mày":
        return "tao", "mày"
    if us in ("tui", "tôi") and uc in ("ông", "bà"):
        return us, ("bà" if u_is_f else "ông")
    if us == "tôi" and uc in ("cậu", "bạn"):
        return "tôi", uc
    if us in ("mình", "tớ") and uc == "cậu":
        return us, "cậu"
    if us in ("anh", "chị") and uc == "em":
        return "em", us
    if us == "em" and uc in ("anh", "chị"):
        return uc, "em"
    return u_call.strip(), (u_self.strip() if us not in ("tôi", "tui") else rpformat.adapt_call("ông/bà", ug, user_name))


def _help_text():
    return (
        "**Danh sách lệnh ngoài ngữ cảnh (`in_context: false` — không ảnh hưởng mạch truyện & KV Cache):**\n"
        "- `/state @Tên` — Xem trạng thái cứng + LLM phân tích tâm lý, động cơ và đầu mối với nhân vật.\n"
        "- `/state @Tên --full` — Như trên, kèm bí mật chưa lộ (`secrets`) và mục tiêu ngầm.\n"
        "- `/state` — Tổng quan cả phòng: vị trí, tâm trạng, mức thân thiết và xưng hô mọi nhân vật.\n"
        "- `/mind @Tên [--full]` — Đọc 1 đoạn độc thoại nội tâm ngắn đúng giọng nhân vật tại thời điểm hiện tại.\n"
        "- `/ooc <câu hỏi>` — Hỏi Game Master ngoài vai (thêm `--full` để xem cả bí mật chưa lộ).\n"
        "- `/mem [@Tên]` — Kiểm tra danh sách `Hard Memory` được phép biết và ranh giới nhận thức (`witnesses`).\n"
        "- `/pronoun @Tên <xưng> <gọi>` — Khóa tay nhanh xưng hô (`lock='manual'`, VD `/pronoun @Vy tui bà` hoặc `/pronoun @Vy tui bà / tui ông`).\n"
        "- `/help` — Hiện hướng dẫn này."
    )


OOC_INLINE_RE = re.compile(r"^\s*([\(\[])\s*ooc\b\s*[:\-]?\s*([\s\S]*)$", re.I)


def normalize_ooc_input(raw_text):
    """Case #25: Tu dong chuyen '(OOC: ...)' / '[OOC: ...]' thanh lenh '/ooc ...'."""
    s = str(raw_text or "").strip()
    m = OOC_INLINE_RE.match(s)
    if not m:
        return s
    opener, body = m.group(1), m.group(2).strip()
    closer = ")" if opener == "(" else "]"
    if body.endswith(closer):
        body = body[:-1].strip()
    return ("/ooc " + body).strip()


def execute_command(raw_cmd, pack, state, history):
    """Thuc thi lenh Game Master -> dict mo ta snapshot (0ms) + prompt LLM side-channel (neu can)."""
    raw = normalize_ooc_input(raw_cmd)
    if not raw.startswith("/"):
        return {
            "cmd": "/help",
            "title": "Hướng dẫn lệnh",
            "summary": "⌘ /help — Danh sách lệnh",
            "snapshot": _help_text(),
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": None,
        }

    parts = raw.split(None, 1)
    cmd = parts[0].lower()
    arg_str = parts[1].strip() if len(parts) > 1 else ""
    is_full = bool(re.search(r"(?:^|\s)--full(?:\s|$)", arg_str))
    arg_clean = re.sub(r"(?:^|\s)--full(?=\s|$)", " ", arg_str).strip()

    rp_hist = group.rp_history(history)
    state = group._ensure_v2_state(pack, state)
    chars = pack.get("characters") or {}
    user_name = group.user_name_of(pack)
    locs = state.get("locations") or pack.get("locations") or {}
    user_loc = ((state.get("entities") or {}).get("user") or {}).get("location_id") or state.get("dia_diem", "")
    user_bc = rpformat.location_breadcrumb(locs, user_loc)
    ten_idx = min(4, max(0, int(state.get("tension", 1))))
    ten_lbl = group.TENSION[ten_idx]
    clk = rpformat.format_clock(state["clock"]) if isinstance(state.get("clock"), dict) else state.get("thoi_gian", "?")

    if cmd == "/help":
        return {
            "cmd": "/help",
            "title": "Hướng dẫn lệnh",
            "summary": "⌘ /help — Danh sách lệnh ngoài ngữ cảnh",
            "snapshot": _help_text(),
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": None,
        }

    if cmd not in ("/state", "/mind", "/ooc", "/mem", "/pronoun"):
        return {
            "cmd": cmd,
            "title": "Lệnh không hợp lệ",
            "summary": f"⌘ {cmd} — Lệnh không hỗ trợ (xem /help)",
            "snapshot": f"Không có lệnh `{cmd}`.\n\n" + _help_text(),
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": None,
        }

    # --- /state (khong tham so -> tong quan ca phong khi nhieu nhan vat) ---
    if cmd == "/state" and not arg_clean and len(chars) != 1:
        ch = state.get("active_channel") or {}
        ch_type = group.CHANNEL_TYPES.get(ch.get("channel_type", "in_person"), ch.get("channel_type", "in_person"))
        co_npcs = set(rpformat.co_located_npcs(pack, state))
        edges = (state.get("relationships") or {}).get("edges") or {}
        lines = [
            f"- **Thời gian:** {clk}",
            f"- **Vị trí của {user_name}:** {user_bc}",
            f"- **Kênh hiện tại:** {ch_type}",
            f"- **Độ căng cảnh:** {ten_lbl}",
            "",
            "**Dàn nhân vật:**",
        ]
        for c_entry in pack.get("cast") or []:
            cid = c_entry["char_id"]
            if cid not in chars:
                continue
            cname = chars[cid]["meta"]["name"]
            ent = (state.get("entities") or {}).get(cid) or {}
            cloc = ent.get("location_id") or user_loc
            loc_short = (locs.get(cloc) or {}).get("name") or cloc
            co_mark = "🟢 Cùng phòng" if cid in co_npcs else f"⚪ {loc_short}"
            mood_lbl = group.MOODS.get(ent.get("mood", "normal"), "Bình thường")
            c2u = (edges.get(cid) or {}).get("user") or {}
            lvl = int(c2u.get("level", 50))
            pr = rpformat.resolve_pronouns(pack, state, cid, ["user", cid], focus_id="user")
            fp = pr.get("focus_pair") or {}
            c_self = fp.get("self") or (c2u.get("default") or {}).get("self", "tui")
            c_call = fp.get("call") or (c2u.get("default") or {}).get("call", user_name)
            lines.append(f"- **{cname}** ({co_mark}) · {mood_lbl} · Level {lvl}/100 · `{c_self} – {c_call}` ({_lock_label(c2u)})")
        loc_short = (locs.get(user_loc) or {}).get("name") or user_loc
        return {
            "cmd": "/state",
            "title": "Tổng quan trạng thái",
            "summary": f"⌘ /state — {loc_short} · {len(co_npcs)}/{len(chars)} có mặt · {ten_lbl}",
            "snapshot": "\n".join(lines),
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": None,
        }

    # --- /ooc <cau hoi> ---
    if cmd == "/ooc":
        if not arg_clean:
            return {
                "cmd": "/ooc",
                "title": "Hỏi Game Master",
                "summary": "⌘ /ooc — Thiếu câu hỏi",
                "snapshot": "Hãy nhập câu hỏi sau `/ooc`, ví dụ: `/ooc nãy giờ có ai nhắc tới vụ quỹ không?`",
                "needs_llm": False,
                "msgs": [],
                "llm_options": {},
                "mutated_state": None,
            }
        lore_txt = "\n".join(f"- {e['title']}: {e['content']}" for e in (pack.get("lore") or [])) or "(không có)"
        hm_lines = []
        for f in state.get("hard_memory") or []:
            if f.get("status") == "secret" and not is_full:
                continue
            kb = ", ".join(group.name_of(pack, x) if x != "all" else "tất cả" for x in (f.get("known_by") or []))
            hm_lines.append(f"- [{f.get('status', 'active')}] {f['text']} (Biết: {kb})")
        arc_txt = (state.get("story_arc") or "").strip()
        sc_txt = "\n".join(sc if isinstance(sc, str) else sc.get("text", "") for sc in (state.get("recent_scenes") or []))
        convo = group.transcript(rp_hist, pack, 10) or "(Chưa có hội thoại)"
        opening_nar = (pack.get("opening") or {}).get("narration", "")
        secret_rule = "" if is_full else " Không tiết lộ các bí mật chưa lộ (`secrets`) của nhân vật."
        sys_p = prompts.render(
            "commands.ooc_sys",
            user_name=user_name,
            world_name=(pack.get("meta") or {}).get("name", "Thế giới"),
            secret_rule=secret_rule,
        )
        usr_p = (
            f"[TRẠNG THÁI HIỆN TẠI]\n{group.state_text(pack, state)}\n\n"
            f"[LORE THẾ GIỚI]\n{lore_txt}\n\n"
            f"[TRÍ NHỚ CỨNG]\n{chr(10).join(hm_lines) or '(chưa có)'}\n\n"
            + (f"[ĐẠI CƯƠNG]\n{arc_txt}\n\n" if arc_txt else "")
            + (f"[CÁC CẢNH GẦN ĐÂY]\n{sc_txt}\n\n" if sc_txt else "")
            + (f"[MỞ ĐẦU]\n{opening_nar}\n\n" if opening_nar else "")
            + f"[HỘI THOẠI GẦN NHẤT]\n{convo}\n\n"
            f"[CÂU HỎI OOC CỦA {user_name}]\n{arg_clean}"
        )
        return {
            "cmd": "/ooc",
            "title": "Trả lời OOC từ Game Master",
            "summary": f"⌘ /ooc — {arg_clean[:48]}{'…' if len(arg_clean) > 48 else ''}",
            "snapshot": "",
            "needs_llm": True,
            "msgs": [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}],
            "llm_options": {"reasoning": "off", "temperature": 0.35, "max_tokens": 600},
            "mutated_state": None,
        }

    # --- /mem [@Char] ---
    if cmd == "/mem":
        cid, _ = extract_mention(arg_clean, pack)
        if arg_clean and not cid:
            return {
                "cmd": "/mem",
                "title": "Kiểm tra bộ nhớ",
                "summary": f"⌘ {raw} — Không rõ nhân vật",
                "snapshot": f"Không tìm thấy nhân vật khớp với `{arg_clean}`.",
                "needs_llm": False,
                "msgs": [],
                "llm_options": {},
                "mutated_state": None,
            }
        hm = state.get("hard_memory") or []
        if not cid:
            lines = [f"**Toàn bộ Trí nhớ cứng ({len(hm)} mục):**"]
            for f in hm:
                kb = ", ".join("tất cả" if x == "all" else group.name_of(pack, x) for x in (f.get("known_by") or []))
                lines.append(f"- [{f.get('status', 'active')}] {f['text']} *(Biết: {kb})*")
            if len(lines) == 1:
                lines.append("- (Chưa có mục nào)")
            return {
                "cmd": "/mem",
                "title": "Toàn bộ Trí nhớ cứng",
                "summary": f"⌘ /mem — {len(hm)} mục Hard Memory",
                "snapshot": "\n".join(lines),
                "needs_llm": False,
                "msgs": [],
                "llm_options": {},
                "mutated_state": None,
            }
        cname = chars[cid]["meta"]["name"]
        short_name = cname.split()[-1]
        known_lines = []
        for f in hm:
            kb = f.get("known_by") or []
            if "all" in kb or cid in kb:
                st_tag = "BÍ MẬT" if f.get("status") == "secret" else ("ĐÃ LỘ" if f.get("status") == "revealed" else "SỰ THẬT")
                known_lines.append(f"- [{st_tag}] {rpformat.fill_placeholders(f['text'], user_name, cname)}")
        upto = int(state.get("summarized_upto", 0))
        working = rp_hist[upto:]
        seen_turns, blind_turns = [], []
        for idx, m in enumerate(working):
            tid = f"#{m.get('turn_id', upto + idx + 1)}"
            w = m.get("witnesses")
            if isinstance(w, list):
                if cid in w or m.get("speaker") == cid:
                    seen_turns.append(tid)
                else:
                    blind_turns.append(tid)
        snap = (
            f"**Trí nhớ cứng mà {cname} được phép biết ({len(known_lines)} mục):**\n"
            + ("\n".join(known_lines) if known_lines else "- (Không có mục riêng nào)")
            + "\n\n**Ranh giới chứng kiến (Working Memory):**\n"
            + f"- Đã chứng kiến: {', '.join(seen_turns) if seen_turns else '(chưa ghi nhận)'}\n"
            + f"- Không có mặt / Không được biết: {', '.join(blind_turns) if blind_turns else 'không có'}"
        )
        return {
            "cmd": "/mem",
            "title": f"Bộ nhớ của {cname}",
            "summary": f"⌘ /mem @{short_name} — Bộ nhớ & Ranh giới nhận thức",
            "snapshot": snap,
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": None,
        }

    # --- Cac lenh bat buoc @Char: /state @Char, /mind @Char, /pronoun @Char ---
    cid, rest = extract_mention(arg_clean, pack)
    if not cid and len(chars) == 1:
        cid = next(iter(chars))
        rest = arg_clean
    if not cid:
        return {
            "cmd": cmd,
            "title": "Không tìm thấy nhân vật",
            "summary": f"⌘ {raw} — Không rõ nhân vật",
            "snapshot": f"Không tìm thấy nhân vật khớp với `{arg_clean or raw}`. Dùng `/state` để xem danh sách nhân vật hoặc `/help`.",
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": None,
        }

    c_v2 = chars[cid]
    cname = c_v2["meta"]["name"]
    short_name = cname.split()[-1]

    # --- /pronoun @Char <self> <call> [/ <c_self> <c_call>] ---
    if cmd == "/pronoun":
        if not rest:
            return {
                "cmd": "/pronoun",
                "title": "Thiếu cặp xưng hô",
                "summary": f"⌘ /pronoun @{short_name} — Thiếu tham số",
                "snapshot": f"Cách dùng: `/pronoun @{short_name} tui bà` hoặc `/pronoun @{short_name} tui bà / tui ông`",
                "needs_llm": False,
                "msgs": [],
                "llm_options": {},
                "mutated_state": None,
            }
        sides = [s.strip() for s in rest.split("/", 1)]
        u_toks = sides[0].split()
        if len(u_toks) < 2:
            return {
                "cmd": "/pronoun",
                "title": "Thiếu từ gọi",
                "summary": f"⌘ /pronoun @{short_name} — Cần cả <xưng> và <gọi>",
                "snapshot": f"Hãy nhập đủ 2 từ (xưng và gọi), ví dụ: `/pronoun @{short_name} tui bà`.",
                "needs_llm": False,
                "msgs": [],
                "llm_options": {},
                "mutated_state": None,
            }
        u_self, u_call = u_toks[0], " ".join(u_toks[1:])
        ug = rpformat.gender_of(pack, "user")
        if len(sides) > 1 and len(sides[1].split()) >= 2:
            c_toks = sides[1].split()
            c_self, c_call = c_toks[0], " ".join(c_toks[1:])
        else:
            c_self, c_call = _infer_reverse_pair(u_self, u_call, ug, user_name)

        new_state = copy.deepcopy(state)
        edges = new_state.setdefault("relationships", {}).setdefault("edges", {})
        raw_world_edges = (pack.get("relationships") or {}).get("edges") or {}

        u2c = edges.setdefault("user", {}).get(cid) or rpformat._resolve_sparse_edge(pack, raw_world_edges, "user", cid)
        u2c["default"] = {**(u2c.get("default") or {}), "self": u_self, "call": u_call}
        u2c["is_fallback"] = False
        u2c["lock"] = "manual"
        edges["user"][cid] = u2c

        c2u = edges.setdefault(cid, {}).get("user") or rpformat._resolve_sparse_edge(pack, raw_world_edges, cid, "user")
        c2u["default"] = {**(c2u.get("default") or {}), "self": c_self, "call": c_call}
        ov = dict(c2u.get("overrides") or {})
        ov["private"] = {"self": c_self, "call": c_call}
        ov["public_formal"] = {"self": c_self, "call": c_call}
        ov["tense"] = {"self": c_self, "call": c_call}
        c2u["overrides"] = ov
        c2u["is_fallback"] = False
        c2u["lock"] = "manual"
        edges[cid]["user"] = c2u
        new_state = rpformat.sync_legacy_state(pack, new_state)

        snap = (
            f"Đã khóa cứng xưng hô 2 chiều (`lock = \"manual\"`) cho **{cname}**:\n"
            f"- **{user_name} → {cname}:** xưng `{u_self}`, gọi `{u_call}` 🔒\n"
            f"- **{cname} → {user_name}:** xưng `{c_self}`, gọi `{c_call}` 🔒"
        )
        return {
            "cmd": "/pronoun",
            "title": f"Khóa xưng hô với {cname}",
            "summary": f"⌘ /pronoun @{short_name} — Đã khóa tay: Bạn ({u_self}–{u_call}) ↔ {short_name} ({c_self}–{c_call}) 🔒",
            "snapshot": snap,
            "needs_llm": False,
            "msgs": [],
            "llm_options": {},
            "mutated_state": new_state,
        }

    # --- Thong tin chung cho /state @Char va /mind @Char ---
    ent = (state.get("entities") or {}).get(cid) or {}
    char_loc = ent.get("location_id") or user_loc
    char_bc = rpformat.location_breadcrumb(locs, char_loc)
    loc_short = (locs.get(char_loc) or {}).get("name") or char_loc
    co_located = (char_loc == user_loc)
    co_str = "🟢 Đang ở cùng bạn" if co_located else f"⚪ Khác vị trí (bạn đang ở: {user_bc})"
    mood_key = ent.get("mood", "normal")
    mood_lbl = group.MOODS.get(mood_key, mood_key)
    mood_short = mood_lbl.split(",")[0].strip()
    outfit_key = ent.get("outfit", "default")
    outfit_desc = ((c_v2.get("appearance") or {}).get("outfits") or {}).get(outfit_key) or outfit_key

    edges = (state.get("relationships") or {}).get("edges") or {}
    u2c = (edges.get("user") or {}).get(cid) or {}
    c2u = (edges.get(cid) or {}).get("user") or {}
    u_dflt = u2c.get("default") or {}
    u_self = u_dflt.get("self", "tôi")
    u_call = u_dflt.get("call", short_name)
    pr = rpformat.resolve_pronouns(pack, state, cid, ["user", cid], focus_id="user")
    fp = pr.get("focus_pair") or {}
    c_self = fp.get("self") or (c2u.get("default") or {}).get("self", "tui")
    c_call = fp.get("call") or (c2u.get("default") or {}).get("call", user_name)
    c_tier = fp.get("tier", "default")
    lvl = int(c2u.get("level", 50))
    tag = c2u.get("tag", "quen biết")
    cast_entry = next((x for x in (pack.get("cast") or []) if x.get("char_id") == cid), {})

    # Loc Hard Memory
    hm_public_lines, hm_secret_lines = [], []
    for f in state.get("hard_memory") or []:
        kb = f.get("known_by") or []
        ents = f.get("entities") or []
        relevant = (cid in kb) or (cid in ents) or ("all" in kb and ("user" in ents or cid in ents))
        if not relevant:
            continue
        txt = rpformat.fill_placeholders(f["text"], user_name, cname)
        if f.get("status") == "secret":
            if is_full:
                hm_secret_lines.append(f"- [BÍ MẬT CHƯA LỘ] {txt}")
        else:
            hm_public_lines.append(f"- [SỰ THẬT] {txt}")

    arc_txt = (state.get("story_arc") or "").strip()
    sc_txt = "\n".join(sc if isinstance(sc, str) else sc.get("text", "") for sc in (state.get("recent_scenes") or []))
    convo8 = group.transcript(rp_hist, pack, 8) or "(Chưa có hội thoại)"
    opening_nar = (pack.get("opening") or {}).get("narration", "")
    pers = ", ".join(c_v2["core"]["personality"]) if isinstance(c_v2["core"]["personality"], list) else str(c_v2["core"]["personality"])
    pers = rpformat.fill_placeholders(pers, user_name, cname)

    if cmd == "/mind":
        snap = f"📍 **{char_bc}** ({co_str}) · Tâm trạng: **{mood_lbl}** · Xưng hô với {user_name}: `{c_self} – {c_call}`"
        voice = c_v2.get("voice") or {}
        tone_clean = _strip_tone_pronouns(rpformat.fill_placeholders(voice.get("tone", ""), user_name, cname))
        ip = (voice.get("modes") or {}).get("in_person") or {}
        secret_rule = "" if is_full else " Không nhắc tới bí mật chưa được tiết lộ nếu chưa xảy ra trong hội thoại."
        sys_p = prompts.render(
            "commands.mind_sys",
            char_name=cname,
            dialect=voice.get("dialect", ""),
            tone=tone_clean,
            user_name=user_name,
            self=c_self,
            call=c_call,
            secret_rule=secret_rule,
        )
        usr_p = (
            f"[HỒ SƠ {cname.upper()}]\n"
            f"- Tóm tắt: {rpformat.fill_placeholders(c_v2['core']['one_liner'], user_name, cname)}\n"
            f"- Tính cách: {pers}\n"
            + (f"- Mục tiêu ngầm: {rpformat.fill_placeholders(cast_entry.get('goal', ''), user_name, cname)}\n" if is_full and cast_entry.get("goal") else "")
            + (f"- Câu mẫu nhịp giọng: {' / '.join(ip.get('samples', [])[:2])}\n" if ip.get("samples") else "")
            + f"- Tâm trạng hiện tại: {mood_lbl} · Độ căng cảnh: {ten_lbl}\n"
            + f"- Quan hệ với {user_name}: {tag} (thân thiết {lvl}/100)\n\n"
            + f"[TRÍ NHỚ LIÊN QUAN]\n{chr(10).join(hm_public_lines + hm_secret_lines) or '(chưa có)'}\n\n"
            + (f"[CẢNH GẦN ĐÂY]\n{sc_txt}\n\n" if sc_txt else "")
            + (f"[MỞ ĐẦU]\n{opening_nar}\n\n" if not sc_txt and opening_nar else "")
            + f"[HỘI THOẠI GẦN NHẤT]\n{convo8}\n\n"
            + prompts.render("commands.mind_user_tail", char_name=cname)
        )
        return {
            "cmd": "/mind",
            "title": f"Nội tâm {cname}",
            "summary": f"⌘ /mind @{short_name}{' --full' if is_full else ''} — Độc thoại nội tâm ({loc_short} · {mood_short})",
            "snapshot": snap,
            "needs_llm": True,
            "msgs": [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}],
            "llm_options": {"reasoning": "off", "temperature": 0.65, "max_tokens": 450},
            "mutated_state": None,
        }

    # --- Mac dinh: /state @Char [--full] ---
    snap_lines = [
        f"**{cname}** ({c_v2['meta'].get('age', '?')} tuổi, {c_v2['meta'].get('gender', '')})",
        f"- **Vị trí:** {char_bc} ({co_str})",
        f"- **Tâm trạng:** {mood_lbl} · **Độ căng cảnh:** {ten_lbl}",
        f"- **Trang phục:** {outfit_desc}",
        f"- **Xưng hô {user_name} → {short_name}:** xưng \"{u_self}\", gọi \"{u_call}\" ({_lock_label(u2c)})",
        f"- **Quan hệ & Xưng hô {short_name} → {user_name}:** {tag} · Thân thiết **{lvl}/100** · chế độ `[{c_tier}]`: xưng \"{c_self}\", gọi \"{c_call}\" ({_lock_label(c2u)})",
    ]
    if is_full and cast_entry.get("goal"):
        snap_lines.append(f"- **Mục tiêu ngầm (`--full`):** {rpformat.fill_placeholders(cast_entry['goal'], user_name, cname)}")
    all_hm = hm_public_lines + hm_secret_lines
    snap_lines.append("**Trí nhớ cứng liên quan:**")
    snap_lines.extend(all_hm if all_hm else ["- (Chưa có mục nào)"])
    snapshot = "\n".join(snap_lines)

    secret_rule = "" if is_full else " Tuyệt đối không suy diễn hay tiết lộ các bí mật chưa lộ trong hồ sơ."
    sys_p = prompts.render("commands.state_sys", user_name=user_name, char_name=cname, secret_rule=secret_rule)
    usr_p = (
        f"[HỒ SƠ CỐT LÕI — {cname.upper()}]\n"
        f"- Một dòng: {rpformat.fill_placeholders(c_v2['core']['one_liner'], user_name, cname)}\n"
        f"- Tính cách: {pers}\n"
        f"- Giá trị sống: {rpformat.fill_placeholders(c_v2['core'].get('values', ''), user_name, cname)}\n"
        + (f"- Mục tiêu ngầm: {rpformat.fill_placeholders(cast_entry.get('goal', ''), user_name, cname)}\n" if is_full and cast_entry.get("goal") else "")
        + f"\n[TRẠNG THÁI & QUAN HỆ HIỆN TẠI]\n{snapshot}\n\n"
        + (f"[ĐẠI CƯƠNG]\n{arc_txt}\n\n" if arc_txt else "")
        + (f"[CÁC CẢNH GẦN ĐÂY]\n{sc_txt}\n\n" if sc_txt else "")
        + (f"[MỞ ĐẦU]\n{opening_nar}\n\n" if not sc_txt and opening_nar else "")
        + f"[HỘI THOẠI GẦN NHẤT]\n{convo8}\n\n"
        + prompts.render("commands.state_user_tail", user_name=user_name)
    )
    return {
        "cmd": "/state",
        "title": f"Trạng thái {cname}",
        "summary": f"⌘ /state @{short_name}{' --full' if is_full else ''} — {loc_short} · Level {lvl}/100 ({c_self}–{c_call}) · {mood_short}",
        "snapshot": snapshot,
        "needs_llm": True,
        "msgs": [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}],
        "llm_options": {"reasoning": "off", "temperature": 0.4, "max_tokens": 600},
        "mutated_state": None,
    }
