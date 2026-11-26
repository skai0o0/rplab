"""Pipeline Bien dich & Bien tap Van phong (Two-Stage RP: Brain LLM -> Translator/Stylist LLM).

Ho tro ca Chat don (index.html) va Group chat (group.html) qua /api/group/turn, /api/group/replay, /api/group/preview.
- Giu nguyen Zone 1-2 de toi uu KV Cache khi bat che do dich (chi chen chi dan ngon ngu vao cuoi Zone 4).
- Chi gui goi ngu canh sieu gon (~500-1000 tokens) cho Translator LLM: ho so giong van, bang xung ho, 3 luot gan nhat, ban nhap.
- Ho tro Fallback tu dong khi Translator chinh bi chan (SAFETY / PROHIBITED_CONTENT / content_filter / refusal / loi mang).
"""
import re
import time

import group
import prompts
import rpformat

PIVOT_LABELS = {
    "zh": "Tiếng Trung (ZH)",
    "en": "Tiếng Anh (EN)",
    "vi": "Tiếng Việt (Biên tập)",
    "outline": "Dàn ý Beat Sheet (EN) → Viết văn (VI)",
}

_BLOCK_NOTICE_RE = re.compile(
    r"(?:bị chặn|PROHIBITED_CONTENT|SAFETY|RECITATION|BLOCKLIST|IMAGE_SAFETY|SPII|content_filter)",
    re.I,
)

_REFUSAL_START_RE = re.compile(
    r"^(?:"
    r"i cannot|i can't|i'?m sorry,\s*i|as an ai|"
    r"xin lỗi,\s*tôi không thể|rất tiếc,\s*tôi không thể|tôi không thể"
    r")\b",
    re.I,
)

_REFUSAL_ACTION_RE = re.compile(
    r"(?:dịch|chuyển ngữ|biên tập|thực hiện|hỗ trợ|đáp ứng|xử lý|tiếp tục|tạo|viết|hoàn thành|"
    r"translate|fulfill|assist|comply|process|generate|continue|provide)",
    re.I,
)

_TRANS_GLOSSARY_RE = re.compile(
    r"(?:glossary|từ vựng|thuật ngữ|từ điển|văn phong|xưng hô|khẩu ngữ|slang|danh xưng|tiếng lóng|nsfw|miêu tả|từ ngữ)",
    re.I,
)


def pivot_of(o):
    p = str((o or {}).get("translate_pivot") or "zh").strip().lower()
    return p if p in PIVOT_LABELS else "zh"


def is_enabled(o):
    if isinstance(o, dict) and not o.get("translate_pivot"):
        o["translate_pivot"] = "zh"
    return bool((o or {}).get("translate_enabled"))


def _collect_names_list(pack):
    names = []
    seen = set()
    user_name = group.user_name_of(pack or {})
    if user_name:
        seen.add(user_name)
        names.append(user_name)
    chars = (pack or {}).get("characters") or {}
    for c_entry in (pack or {}).get("cast") or []:
        cid = c_entry.get("char_id") or c_entry.get("id")
        if not cid:
            continue
        cname = ((chars.get(cid) or {}).get("meta") or {}).get("name") or cid
        if cname and cname not in seen:
            seen.add(cname)
            names.append(cname)
    for cid, c_v2 in chars.items():
        cname = (c_v2.get("meta") or {}).get("name") or cid
        if cname and cname not in seen:
            seen.add(cname)
            names.append(cname)
    return ", ".join(names) if names else user_name


def apply_pivot_instruction(msgs, pack, state, dec, o):
    """Chen chi dan ngon ngu ban nhap vao cuoi Zone 4 (giu nguyen Zone 1-2 cho KV Cache)."""
    out = [dict(m) for m in (msgs or [])]
    if not out:
        return out
    pivot = pivot_of(o)
    if pivot == "vi":
        return out
    sp = (dec or {}).get("speaker") or group.NARRATOR
    if sp == group.SCENE_SPEAKER:
        chars = (pack or {}).get("characters") or {}
        ch_parts = ((state or {}).get("active_channel") or {}).get("participants") or []
        sc_names = [group.name_of(pack or {}, c) for c in ch_parts if c in chars]
        sp_name = ", ".join(sc_names) if sc_names else group.name_of(pack or {}, sp)
    else:
        sp_name = (dec or {}).get("speaker_name") or group.name_of(pack or {}, sp)
    names_list = _collect_names_list(pack)
    key = (
        "translator.writer_pivot_zh" if pivot == "zh"
        else "translator.writer_pivot_outline" if pivot == "outline"
        else "translator.writer_pivot_en"
    )
    inst = prompts.render(key, speaker_name=sp_name, names_list=names_list)
    if inst.strip():
        tail = out[-1].get("content") or ""
        sep = "\n\n" if tail.strip() else ""
        out[-1]["content"] = tail + sep + inst.strip()
    return out


def select_translator_lore(pack, state, history, dec, active_lore=None, char_budget=1500):
    """Chon loc cac muc Lorebook / Glossary dang kich hoat trong ngan sach ky tu gon nhe cho Translator."""
    pack = pack or {}
    state = state or {}
    dec = dec or {}
    user_name = group.user_name_of(pack)
    sp = dec.get("speaker") or group.NARRATOR
    sp_name = dec.get("speaker_name") or group.name_of(pack, sp)
    chars = pack.get("characters") or {}
    cast = group.cast_by_id(pack)
    locs = state.get("locations") or pack.get("locations") or {}
    user_loc = ((state.get("entities") or {}).get("user") or {}).get("location_id") or state.get("dia_diem")

    ch = state.get("active_channel") or {}
    ch_type = ch.get("channel_type", "in_person")
    comm_medium = ch.get("medium") or ("spoken" if ch_type == "in_person" else "texting")
    mode_key = "in_person" if (ch_type == "in_person" or comm_medium == "spoken") else "texting"

    rp_hist = group.rp_history(history or [])
    scan_text = "\n".join(m.get("content", "") for m in rp_hist[-4:])
    hot_text = "\n".join(m.get("content", "") for m in rp_hist[-2:])

    loc_lore_ids = set(rpformat.inherited_lore_ids(locs, user_loc))
    dir_lore_ids = set(dec.get("lore_used") or [])

    raw_cands = []
    for e in pack.get("lore") or []:
        eid = e.get("id", "")
        title = e.get("title") or "Lore"
        keys = e.get("keys") or []
        content = e.get("content") or ""
        act = e.get("activation")
        is_gloss = bool(_TRANS_GLOSSARY_RE.search(f"{title}\n{content[:120]}"))
        hit = (
            (eid in loc_lore_ids)
            or (eid in dir_lore_ids)
            or (act == "keyword" and group._match_keywords(keys, scan_text))
            or (act == "constant" and is_gloss)
        )
        if hit:
            raw_cands.append((eid, title, keys, content))

    for e in active_lore or []:
        eid = e.get("uid", e.get("id", ""))
        title = e.get("comment") or e.get("title") or "Lore"
        keys = e.get("keys") or []
        content = e.get("content") or ""
        raw_cands.append((eid, title, keys, content))

    seen_contents = set()
    scored = []
    is_scene_all = (sp == group.SCENE_SPEAKER)
    active_cids = [c for c in (ch.get("participants") or []) if c in chars] if is_scene_all else None
    for idx, (eid, title, keys, content) in enumerate(raw_cands):
        c_txt = rpformat.fill_placeholders(content, user_name, sp_name).strip()
        if not c_txt or c_txt in seen_contents:
            continue
        seen_contents.add(c_txt)
        if group._is_mode_mismatched_lore(title, c_txt, mode_key):
            continue
        if (sp in chars or is_scene_all) and group._classify_char_lore_owner(title, c_txt, sp, cast, active_cids=active_cids) == "foreign":
            continue

        score = 0
        if _TRANS_GLOSSARY_RE.search(f"{title}\n{c_txt[:120]}"):
            score += 120
        if group._match_keywords(keys, hot_text):
            score += 60
        elif group._match_keywords(keys, scan_text) or (eid in dir_lore_ids) or (eid in loc_lore_ids):
            score += 35
        else:
            score += 10
        scored.append((score, idx, title, group._sanitize_z4_text(c_txt)))

    scored.sort(key=lambda item: (-item[0], item[1]))

    lore_lines = []
    used = 0
    for _, _, title, c_txt in scored:
        line = f"- {title}: {c_txt}"
        rem = char_budget - used
        if rem <= 0:
            break
        if len(line) <= rem:
            lore_lines.append(line)
            used += len(line) + 1
        elif rem >= 180:
            clipped = line[:rem - 3].rstrip() + "..."
            lore_lines.append(clipped)
            used += len(clipped) + 1
            break
    return lore_lines


def build_translate_messages(raw_text, pack, state, history, dec, pr_info, o, extra_fix_note="", active_lore=None):
    """Dong goi prompt sieu gon (~500-1000 tokens) cho Translator/Stylist LLM."""
    pivot = pivot_of(o)
    pack = pack or {}
    state = state or {}
    dec = dec or {}
    sp = dec.get("speaker") or group.NARRATOR
    sp_name = dec.get("speaker_name") or group.name_of(pack, sp)
    user_name = group.user_name_of(pack)

    if pivot == "vi":
        sys_text = prompts.render("translator.system_polish_vi", speaker_name=sp_name)
    elif pivot == "outline":
        sys_text = prompts.render("translator.system_render_outline", speaker_name=sp_name)
    else:
        sys_text = prompts.render(
            "translator.system_translate",
            source_lang_label=PIVOT_LABELS.get(pivot, pivot),
            speaker_name=sp_name,
        )

    # 1. Ho so giong van nhan vat & kenh giao tiep (khong gui full character_sheet/lore)
    ch = state.get("active_channel") or {}
    ch_type = ch.get("channel_type", "in_person")
    comm_medium = ch.get("medium") or ("spoken" if ch_type == "in_person" else "texting")
    mode_key = "in_person" if (ch_type == "in_person" or comm_medium == "spoken") else "texting"
    ch_label = group.CHANNEL_TYPES.get(ch_type, ch_type)
    if ch_type != "in_person" and comm_medium == "spoken":
        ch_label += " (đang gọi điện thoại trực tiếp)"

    chars = pack.get("characters") or {}
    ch_parts = ch.get("participants") or []
    is_scene_all = (sp == group.SCENE_SPEAKER)
    scene_cids = [c for c in ch_parts if c in chars][:6] if is_scene_all else []
    if is_scene_all and scene_cids:
        sp_name = ", ".join(((chars[c].get("meta") or {}).get("name") or c) for c in scene_cids)
        if pivot == "vi":
            sys_text = prompts.render("translator.system_polish_vi", speaker_name=sp_name)
        elif pivot == "outline":
            sys_text = prompts.render("translator.system_render_outline", speaker_name=sp_name)
        else:
            sys_text = prompts.render(
                "translator.system_translate",
                source_lang_label=PIVOT_LABELS.get(pivot, pivot),
                speaker_name=sp_name,
            )

    voice_lines = [f"- Kênh giao tiếp: {ch_label}"]
    if is_scene_all and scene_cids:
        voice_lines.insert(0, f"- Chế độ Toàn cảnh (nhiều nhân vật trong 1 lượt): {sp_name}")
        for cid in scene_cids:
            c_v2 = chars[cid]
            meta = c_v2.get("meta") or {}
            cname = meta.get("name") or cid
            voice = c_v2.get("voice") or {}
            modes = voice.get("modes") or {}
            mode_obj = modes.get(mode_key) or modes.get("in_person") or {}
            core = c_v2.get("core") or {}
            one_l = rpformat.fill_placeholders(core.get("one_liner", ""), user_name, cname).strip()[:140]
            dial = voice.get("dialect") or ""
            tone = rpformat.fill_placeholders(voice.get("tone", ""), user_name, cname).strip()[:180]
            voice_lines.append(f"* {cname} ({meta.get('age', '?')}t, {dial}): {one_l}" + (f" | Giọng: {tone}" if tone else ""))
            samples = [rpformat.fill_placeholders(s, user_name, cname) for s in (mode_obj.get("samples") or [])[:2] if str(s).strip()]
            if samples:
                voice_lines.append("  Mẫu: " + " // ".join(samples))
    elif sp in chars:
        c_v2 = chars[sp]
        meta = c_v2.get("meta") or {}
        cname = meta.get("name") or sp_name
        voice = c_v2.get("voice") or {}
        modes = voice.get("modes") or {}
        mode_obj = modes.get(mode_key) or modes.get("in_person") or {}
        voice_lines.insert(0, f"- Nhân vật: {cname} ({meta.get('age', '?')} tuổi, {meta.get('gender', '')})")
        core = c_v2.get("core") or {}
        if core.get("one_liner"):
            one_l = rpformat.fill_placeholders(core["one_liner"], user_name, cname).strip()
            if len(one_l) > 180:
                one_l = one_l[:177] + "..."
            if one_l:
                voice_lines.append(f"- Bản chất / Vai: {one_l}")
        if core.get("personality"):
            pers_raw = ", ".join(core["personality"]) if isinstance(core["personality"], list) else str(core["personality"])
            pers = rpformat.fill_placeholders(pers_raw, user_name, cname).strip()
            if len(pers) > 280:
                pers = pers[:277] + "..."
            if pers:
                voice_lines.append(f"- Tính cách & Phản xạ ngầm: {pers}")
        sec_mod = next(
            (m for m in (c_v2.get("modules") or []) if m.get("type") == "secret" and str(m.get("content") or "").strip()),
            None,
        )
        if sec_mod:
            sec_txt = rpformat.fill_placeholders(sec_mod["content"], user_name, cname).strip()
            if len(sec_txt) > 220:
                sec_txt = sec_txt[:217] + "..."
            if sec_txt:
                voice_lines.append(f"- Tâm lý ngầm: {sec_txt}")
        if voice.get("dialect"):
            voice_lines.append(f"- Phương ngữ: {voice['dialect']}")
        if voice.get("tone"):
            voice_lines.append(f"- Giọng điệu: {rpformat.fill_placeholders(voice['tone'], user_name, cname)}")
        if mode_obj.get("style"):
            voice_lines.append(f"- Phong cách [{mode_key}]: {rpformat.fill_placeholders(mode_obj['style'], user_name, cname)}")
        tics = [str(t).strip() for t in (mode_obj.get("tics") or [])[:4] if str(t).strip()]
        if tics:
            voice_lines.append(f"- Câu cửa miệng: {' / '.join(tics)}")
        samples = [rpformat.fill_placeholders(s, user_name, cname) for s in (mode_obj.get("samples") or [])[:3] if str(s).strip()]
        if samples:
            voice_lines.append("- Câu thoại mẫu để bắt nhịp khẩu ngữ:\n" + "\n".join(f"  {s}" for s in samples))
    else:
        voice_lines.insert(0, f"- Vai trò: {prompts.get_text('writer.narrator_role')}")

    world_style = ((pack.get("rules") or {}).get("writing_style") or "").strip()
    if world_style:
        if len(world_style) > 500:
            world_style = world_style[:497] + "..."
        voice_lines.append(f"- Văn phong thế giới: {world_style}")

    sections = ["[NHÂN VẬT & GIỌNG VĂN]\n" + "\n".join(voice_lines)]

    # 2. Bang xung ho bat doi xung
    pr_lines = list((pr_info or {}).get("lines") or [])
    if not pr_lines:
        pr_lines = (
            ["- Ngôi thứ ba khách quan của người dẫn truyện."]
            if sp == group.NARRATOR
            else ["- Dùng xưng hô tự nhiên theo vai."]
        )
    sections.append("[XƯNG HÔ LƯỢT NÀY]\n" + "\n".join(pr_lines))

    # 2b. Thuat ngu & Lorebook dang kich hoat (cat gon theo ngan sach ky tu)
    lore_lines = select_translator_lore(pack, state, history, dec, active_lore=active_lore)
    if lore_lines:
        sections.append(
            "[THUẬT NGỮ & LORE THAM KHẢO (CHỈ ĐỂ DÙNG ĐÚNG TỪ VỰNG/TÊN RIÊNG — KHÔNG IN LẠI KHỐI NÀY)]\n"
            + "\n".join(lore_lines)
        )

    # 3. 3 luot gan nhat de giu mach van & xung ho
    rp_hist = group.rp_history(history or [])
    ctx_lines = []
    for m in rp_hist[-3:]:
        who = user_name if m.get("speaker") == "user" else group.name_of(pack, m.get("speaker"))
        c = str(m.get("content") or "").strip()
        if len(c) > 400:
            c = c[:397] + "..."
        if c:
            ctx_lines.append(f"{who}: {c}")
    if ctx_lines:
        sections.append("[NGỮ CẢNH GẦN NHẤT (CHỈ ĐỂ BẮT MẠCH)]\n" + "\n\n".join(ctx_lines))

    if (extra_fix_note or "").strip():
        sections.append(f"[LƯU Ý SỬA LỖI BẮT BUỘC]\n{extra_fix_note.strip()}")

    draft_hdr = (
        "[BẢN NHÁP CẦN BIÊN TẬP]" if pivot == "vi"
        else "[DÀN Ý DIỄN BIẾN (BEAT SHEET) CẦN VIẾT THÀNH VĂN]" if pivot == "outline"
        else "[BẢN GỐC CẦN BIÊN DỊCH]"
    )
    sections.append(f"{draft_hdr}\n{(raw_text or '').strip()}")

    return [
        {"role": "system", "content": sys_text},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def is_refusal_or_blocked(text, notices=None):
    """Phat hien bi chan noi dung (SAFETY / PROHIBITED_CONTENT / content_filter) hoac model tu choi dich."""
    for n in notices or []:
        ns = str(n or "")
        if _BLOCK_NOTICE_RE.search(ns):
            return True, ns
    s = (text or "").strip()
    if not s:
        return True, "empty_response"
    if len(s) < 350 and _REFUSAL_START_RE.match(s):
        has_rp_markup = bool(re.search(r'["“”*]', s))
        if not has_rp_markup or _REFUSAL_ACTION_RE.search(s):
            return True, f"refusal: {s[:120]}"
    return False, ""


def build_translator_options(o, use_fallback=False):
    """Tao options dict tuong thich stream_openai / stream_gemini cua server.py."""
    o = o or {}
    try:
        temp = float(o.get("translate_temperature")) if o.get("translate_temperature") not in (None, "") else 0.7
    except (TypeError, ValueError):
        temp = 0.7
    try:
        base_max_tok = int(o.get("max_tokens") or 2048)
    except (TypeError, ValueError):
        base_max_tok = 2048

    use_outline_reasoning = (pivot_of(o) == "outline" and not use_fallback)
    common = {
        "reasoning": "low" if use_outline_reasoning else "off",
        "temperature": temp,
        "top_p": 0.95,
        "max_tokens": max(3000 if use_outline_reasoning else 1500, base_max_tok),
        "safety": "BLOCK_NONE",
    }

    if not use_fallback:
        return {
            **common,
            "provider": o.get("translate_provider") or "openrouter",
            "model": (o.get("translate_model") or "").strip() or "google/gemini-2.5-flash-lite",
            "base_url": o.get("translate_base_url") or "",
            "api_key": o.get("translate_api_key") or "",
            "or_providers": o.get("translate_or_providers") or "",
            "or_quant": "",
            "or_fallbacks": True,
        }

    fb_model = (o.get("translate_fallback_model") or "").strip()
    if fb_model:
        tprov = o.get("translate_provider") or "openrouter"
        if tprov == "custom":
            return {
                **common,
                "provider": "custom",
                "model": fb_model,
                "base_url": o.get("translate_base_url") or "",
                "api_key": o.get("translate_api_key") or "",
                "or_providers": "",
                "or_quant": "",
                "or_fallbacks": True,
            }
        return {
            **common,
            "provider": "openrouter",
            "model": fb_model,
            "base_url": "",
            "api_key": "",
            "or_providers": "",
            "or_quant": "",
            "or_fallbacks": True,
        }

    # Mac dinh fallback = dung chinh Writer model (thuong la model Trung khong kiem duyet)
    return {
        **common,
        "provider": o.get("provider") or "openrouter",
        "model": o.get("model") or "deepseek/deepseek-v4.1-flash-1017",
        "base_url": o.get("base_url") or "",
        "api_key": o.get("api_key") or "",
        "or_providers": o.get("or_providers") or "",
        "or_quant": o.get("or_quant") or "",
        "or_fallbacks": bool(o.get("or_fallbacks", True)),
    }


def _fallback_common(o, *, reasoning="off", max_tokens=None):
    try:
        temp = float(o.get("translate_temperature")) if o.get("translate_temperature") not in (None, "") else 0.7
    except (TypeError, ValueError):
        temp = 0.7
    try:
        base_max_tok = int(o.get("max_tokens") or 2048)
    except (TypeError, ValueError):
        base_max_tok = 2048
    return {
        "reasoning": reasoning,
        "temperature": temp,
        "top_p": 0.95,
        "max_tokens": max(1500, base_max_tok) if max_tokens is None else max_tokens,
        "safety": "BLOCK_NONE",
    }


def build_translator_fallback_chain(o, primary_opts, has_gemini_key=False):
    """Tra ve chuoi (label, opts) du phong theo thu tu: Google BYOK -> model du phong -> Writer model."""
    o = o or {}
    chain = []
    seen = {
        (
            primary_opts.get("provider"),
            primary_opts.get("model"),
            primary_opts.get("base_url") or "",
        )
    }

    def _push(label, opts):
        key = (opts.get("provider"), opts.get("model"), opts.get("base_url") or "")
        if not opts.get("model") or key in seen:
            return
        seen.add(key)
        chain.append((label, opts))

    # 1. Google AI Studio BYOK (GEMINI_API_KEY trong .env) khi model chinh la Gemini tren OpenRouter
    pmodel = (primary_opts.get("model") or "").strip()
    if has_gemini_key and primary_opts.get("provider") != "google" and "gemini" in pmodel.lower():
        bare = pmodel.split("/", 1)[1] if "/" in pmodel else pmodel
        bare = bare.split(":", 1)[0]
        _push(
            f"google/{bare} (BYOK)",
            {
                **primary_opts,
                "provider": "google",
                "model": bare,
                "base_url": "",
                "api_key": "",
                "or_providers": "",
                "or_quant": "",
                "or_fallbacks": True,
                "safety": "BLOCK_NONE",
            },
        )

    # 2. Model du phong do nguoi dung chon (vd: z-ai/glm-5.3-flash)
    common_fb = _fallback_common(o, reasoning="off")
    fb_model = (o.get("translate_fallback_model") or "").strip()
    if fb_model:
        tprov = o.get("translate_provider") or "openrouter"
        if tprov == "custom":
            _push(
                fb_model,
                {
                    **common_fb,
                    "provider": "custom",
                    "model": fb_model,
                    "base_url": o.get("translate_base_url") or "",
                    "api_key": o.get("translate_api_key") or "",
                    "or_providers": "",
                    "or_quant": "",
                    "or_fallbacks": True,
                },
            )
        else:
            _push(
                fb_model,
                {
                    **common_fb,
                    "provider": "openrouter",
                    "model": fb_model,
                    "base_url": "",
                    "api_key": "",
                    "or_providers": "",
                    "or_quant": "",
                    "or_fallbacks": True,
                },
            )

    # 3. Writer model (lop du phong cuoi cung, thuong la model Trung khong kiem duyet)
    writer_model = (o.get("model") or "deepseek/deepseek-v4.1-flash-1017").strip()
    _push(
        writer_model,
        {
            **common_fb,
            "provider": o.get("provider") or "openrouter",
            "model": writer_model,
            "base_url": o.get("base_url") or "",
            "api_key": o.get("api_key") or "",
            "or_providers": o.get("or_providers") or "",
            "or_quant": o.get("or_quant") or "",
            "or_fallbacks": bool(o.get("or_fallbacks", True)),
        },
    )
    return chain


def _sum_opt(a, b):
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def run_translate(
    raw_text,
    pack,
    state,
    history,
    dec,
    pr_info,
    o,
    stream_fn_map,
    on_chunk=None,
    extra_fix_note="",
    on_reset=None,
    active_lore=None,
):
    """Chay Translator LLM + tu dong Fallback khi bi chan hoac loi."""
    pivot = pivot_of(o)
    raw_clean = (raw_text or "").strip()
    topts = build_translator_options(o, use_fallback=False)
    if not raw_clean:
        return "", {
            "pivot": pivot,
            "provider": topts["provider"],
            "model": topts["model"],
            "fallback_used": False,
            "fallback_model": "",
            "fallback_reason": "",
            "raw_draft": "",
            "lore_count": 0,
            "secs": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost": 0,
            "request_body": None,
        }

    tmsgs = build_translate_messages(
        raw_clean, pack, state, history, dec, pr_info, o, extra_fix_note=extra_fix_note, active_lore=active_lore
    )
    lore_match = re.search(
        r"\[THUẬT NGỮ & LORE THAM KHẢO[^\n]*\]\n([\s\S]*?)(?:\n\n\[(?:NGỮ CẢNH|LƯU Ý|BẢN |DÀN )|$)",
        tmsgs[1]["content"],
    )
    lore_count = sum(1 for ln in lore_match.group(1).splitlines() if ln.startswith("- ")) if lore_match else 0
    t0 = time.time()

    def _run_once(opts):
        gen_fn = stream_fn_map["gemini"] if opts.get("provider") in ("google", "vertex") else stream_fn_map["openai"]
        txt, notices, usage, req_body, err = "", [], {}, None, ""
        try:
            for ev in gen_fn(tmsgs, opts):
                etype = ev.get("type")
                if etype == "content":
                    chunk = ev.get("text") or ""
                    if chunk:
                        txt += chunk
                        if on_chunk:
                            on_chunk(chunk)
                elif etype == "notice":
                    notices.append(ev.get("text") or "")
                elif etype == "usage":
                    usage = ev
                elif etype == "request":
                    req_body = ev.get("body")
        except Exception as exc:
            err = str(exc)
        return txt, notices, usage, req_body, err

    def _needs_reasoning_retry(err_msg):
        low = (err_msg or "").lower()
        return "reasoning is mandatory" in low or "cannot be disabled" in low

    def _is_effort_400(err_msg):
        low = (err_msg or "").lower()
        return "400" in low and any(w in low for w in ("effort", "reasoning", "minimal", "invalid", "unsupported"))

    def _reasoning_unsupported_400(err_msg):
        low = (err_msg or "").lower()
        return (
            "400" in low
            and not _needs_reasoning_retry(err_msg)
            and any(w in low for w in ("reasoning", "thinking", "effort", "unsupported", "invalid"))
        )

    def _run_with_retry(opts):
        t_txt, t_notices, t_usage, t_req, t_err = _run_once(opts)
        if t_err and opts.get("reasoning") != "off" and _reasoning_unsupported_400(t_err):
            if on_chunk and on_reset:
                on_reset()
            t_txt, t_notices, t_usage, t_req, t_err = _run_once({**opts, "reasoning": "off"})
        elif t_err and opts.get("reasoning") == "off" and _needs_reasoning_retry(t_err):
            for lvl in ("minimal", "low"):
                if on_chunk and on_reset:
                    on_reset()
                retry_opts = {**opts, "reasoning": lvl, "max_tokens": max(int(opts.get("max_tokens") or 1500), 3000)}
                t_txt, t_notices, t_usage, t_req, t_err = _run_once(retry_opts)
                if not t_err or not _is_effort_400(t_err):
                    break
        return t_txt, t_notices, t_usage, t_req, t_err

    txt, notices, usage, req_body, err = _run_with_retry(topts)
    blocked, reason = (True, err) if err else is_refusal_or_blocked(txt, notices)

    fallback_used = False
    fallback_model = ""
    fallback_reason = ""
    final_txt = txt.strip()
    final_req = req_body

    if blocked:
        chain = build_translator_fallback_chain(
            o, topts, has_gemini_key=bool(stream_fn_map.get("has_gemini_key"))
        )
        errors = [f"chính ({topts['model']}): {reason}"]
        total_pt = usage.get("prompt_tokens")
        total_ct = usage.get("completion_tokens")
        total_cost = usage.get("cost")
        succeeded = False

        for cand_label, cand_opts in chain:
            if on_chunk and on_reset:
                on_reset()
            fb_txt, fb_notices, fb_usage, fb_req, fb_err = _run_with_retry(cand_opts)
            total_pt = _sum_opt(total_pt, fb_usage.get("prompt_tokens"))
            total_ct = _sum_opt(total_ct, fb_usage.get("completion_tokens"))
            total_cost = _sum_opt(total_cost, fb_usage.get("cost"))
            fb_blocked, fb_block_reason = (True, fb_err) if fb_err else is_refusal_or_blocked(fb_txt, fb_notices)
            if not fb_blocked and fb_txt.strip():
                fallback_used = True
                fallback_model = cand_label
                fallback_reason = " | ".join(errors)
                final_txt = fb_txt.strip()
                final_req = fb_req or req_body
                usage = {
                    **fb_usage,
                    "prompt_tokens": total_pt,
                    "completion_tokens": total_ct,
                    "cost": total_cost,
                }
                succeeded = True
                break
            errors.append(f"dự phòng ({cand_label}): {fb_block_reason}")

        if not succeeded:
            raise RuntimeError("Biên dịch thất bại — " + "; ".join(errors))

    meta = {
        "pivot": pivot,
        "provider": topts["provider"],
        "model": topts["model"],
        "fallback_used": fallback_used,
        "fallback_model": fallback_model,
        "fallback_reason": fallback_reason,
        "raw_draft": raw_clean,
        "lore_count": lore_count,
        "secs": round(time.time() - t0, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost": usage.get("cost"),
        "request_body": final_req,
    }
    return final_txt, meta
