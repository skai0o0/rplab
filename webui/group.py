"""Group chat roleplay co "dao dien" (ban thu).

Moi luot:
  1. DAO DIEN — 1 lan goi Decisions (OpenRouter /api/alpha/decisions), nhieu cau hoi cung luc:
       - ai len tieng (choice: nhan vat dang co mat + nguoi dan truyen), loai luot (choice),
       - muc lore nao lien quan (noul tung muc), truong card nao can nap (noul tung truong cua tung nhan vat co mat).
     Luat du phong: nguoi choi goi ten 1 nhan vat dang co mat ma dao dien khong chac (< 0.6) -> uu tien nhan vat do.
  2. NGUOI VIET — model chinh viet luot cua nhan vat duoc chon, prompt chi chua nhung gi dao dien chon.
  3. (tuy chon) CONG CHAM — judge.py.
  4. CAP NHAT TRANG THAI — song song: Decisions (ai co mat, dia diem, do cang) + Extractions (thoi gian).
Dinh dang goi group: xem rp/samples/hoa_phuong_group.json ("rplab-group-1").
"""
import json, re, time, unicodedata, urllib.error, urllib.request

import rpformat
from concurrent.futures import ThreadPoolExecutor

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
EXTRACTIONS_URL = "https://openrouter.ai/api/alpha/extractions"
NARRATOR = "nguoi_dan_truyen"
FIELD_LABELS = {"ngoai_hinh": "ngoại hình", "tinh_cach": "tính cách", "cach_noi": "cách nói chuyện, xưng hô",
                "quan_he": "quan hệ với các nhân vật khác", "qua_khu": "quá khứ", "bi_mat": "bí mật", "mo_ta": "mô tả chung"}
CORE_FIELDS = ("tinh_cach", "cach_noi")  # luon nap cho nguoi len tieng
TURN_TYPES = {"thoai": "Đối thoại là chính", "hanh_dong": "Hành động là chính", "mo_ta": "Mô tả cảnh, không khí",
              "chuyen_canh": "Chuyển cảnh: người chơi vừa đề nghị hoặc bắt đầu đi chỗ khác, hoặc muốn nhảy thời gian"}
TENSION = ["Rất thoải mái", "Bình thường", "Hơi căng", "Căng thẳng", "Rất căng"]
RECENT_TURNS = 14          # so luot gan nhat dua nguyen van cho nguoi viet; cu hon thi vao tom tat
SUMMARY_EVERY = 8          # du bao nhieu luot cu chua tom tat thi tom tat mot lan
CHAIN_THRESHOLD = 0.6      # do chac toi thieu de mot nhan vat tu chen vao (phan ung day chuyen)
ARRIVE_THRESHOLD = 0.5     # do chac "nhan vat vang mat vua xuat hien" (goi bang chuc danh, vd "lop pho toi kia")
OWED_THRESHOLD = 0.2       # nguoi bi nguoi choi goi/noi toi ma chua dap: chi can model khong noi ro la "khong" la duoc chen
ADDRESS_CONF = 0.55        # do chac toi thieu cua cau hoi "nguoi choi dang noi voi ai" de ghi de lua chon cua dao dien
GROUP = "ca_nhom"
QUIET_TURNS = 3            # co mat ma im lang tu ngan nay luot tro len -> "im lang lau"
QUIET_THRESHOLD = 0.35     # nguong chen vao (phan ung day chuyen) danh cho nguoi im lang lau
_DAY = re.compile(r"(thứ\s+(hai|ba|tư|năm|sáu|bảy|\d)|chủ\s+nhật)", re.I)


def keep_day(old, new):
    """Extractions hay tra ve '12 giờ 30 trưa' lam mat 'thứ Hai' -> giu lai ngay cu neu gia tri moi khong co ngay."""
    if _DAY.search(new) or not _DAY.search(old):
        return new
    return f"{new.strip()} {_DAY.search(old).group(0)}"


def summary_due(history, state):
    upto = int(state.get("tom_tat_upto", 0))
    return len(history) - RECENT_TURNS - upto >= SUMMARY_EVERY


def summary_messages(pack, state, history):
    """Tom tat cac luot [tom_tat_upto, len-RECENT_TURNS) noi vao tom tat cu (goi bang model viet, tat reasoning)."""
    upto, end = int(state.get("tom_tat_upto", 0)), len(history) - RECENT_TURNS
    chunk = transcript(history[upto:end], pack, n=10 ** 6)
    return end, [{"role": "system", "content": "Bạn tóm tắt truyện tương tác bằng tiếng Việt, ngắn gọn, đúng sự kiện, không bình luận."},
                 {"role": "user", "content": f"[TÓM TẮT CŨ]\n{state.get('tom_tat') or '(chưa có)'}\n\n[CÁC LƯỢT CẦN THÊM VÀO]\n{chunk}\n\n"
                                             f"Viết lại TÓM TẮT CHUNG (tối đa 12 gạch đầu dòng): ai đã làm gì, hứa gì, bí mật nào đã lộ, "
                                             f"quan hệ thay đổi ra sao, đồ vật và hẹn hò quan trọng. Chỉ in phần tóm tắt."}]


def _post(url, api_key, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                                          "X-Title": "RP Lab"})
    last = None
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:
            last = str(e)
            break
    raise RuntimeError(last)


def card_to_cast(card, existing_ids=()):
    """Character card CCv2/v3 -> (nhan vat dang group, cac muc lore). Card co extensions.rp_fields thi dung nguyen."""
    d = card.get("data") or card
    name = d["name"].strip()
    plain = "".join(ch for ch in unicodedata.normalize("NFD", name.lower().replace("đ", "d")) if unicodedata.category(ch) != "Mn")
    base = re.sub(r"[^a-z0-9]+", "_", plain).strip("_") or "nv"
    cid, i = base, 2
    while cid in existing_ids:
        cid, i = f"{base}_{i}", i + 1
    ext = d.get("extensions") or {}
    if isinstance(ext.get("rp_fields"), dict) and ext["rp_fields"]:
        fields = dict(ext["rp_fields"])
    else:
        fields = {}
        if d.get("description"):
            fields["mo_ta"] = d["description"].strip()
        if d.get("personality"):
            fields["tinh_cach"] = d["personality"].strip()
        style = (d.get("system_prompt") or "").strip()
        if d.get("mes_example"):
            style += ("\n" if style else "") + "Ví dụ cách nói:\n" + d["mes_example"].replace("<START>", "").strip()[:900]
        if style:
            fields["cach_noi"] = style.replace("{{char}}", name)
    first = re.split(r"(?<=[.!?])\s|\n", (d.get("description") or d.get("personality") or name).strip())[0]
    lore = []
    for j, e in enumerate((d.get("character_book") or {}).get("entries", [])):
        if e.get("enabled", True) and e.get("content"):
            lore.append({"id": f"{cid}_lore_{j}", "title": e.get("comment") or ", ".join(e.get("keys", [])[:3]) or f"{name} #{j}",
                         "content": e["content"].replace("{{char}}", name)})
    return {"id": cid, "name": name, "short": first[:160], "fields": fields}, lore


def mentioned(char, text):
    """Nhan vat co duoc goi ten (ten goi / ten cuoi) trong doan van khong — khop nguyen tu."""
    names = char.get("aliases") or [char["name"].split()[-1]]
    return any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.I) for n in names if n)


def cast_by_id(pack):
    return {c["id"]: c for c in pack["cast"]}


def name_of(pack, cid):
    return "Người dẫn truyện" if cid == NARRATOR else cast_by_id(pack).get(cid, {}).get("name", cid)


def transcript(history, pack, n=12):
    """history: [{speaker: 'user'|cid|NARRATOR, content}] -> van ban 'Ten: noi dung'."""
    lines = []
    for m in history[-n:]:
        who = pack.get("user", "User") if m["speaker"] == "user" else name_of(pack, m["speaker"])
        lines.append(f"{who}: {m['content']}")
    return "\n\n".join(lines)


def state_text(pack, state):
    present = [name_of(pack, c) for c in state.get("co_mat", [])]
    loc = pack["locations"].get(state.get("dia_diem"), state.get("dia_diem", "?"))
    return (f"Địa điểm: {loc}\nThời gian: {state.get('thoi_gian', '?')}\n"
            f"Có mặt: {pack.get('user', 'User')}" + (", " + ", ".join(present) if present else "") +
            f"\nĐộ căng: {TENSION[int(state.get('do_cang', 1))]}")


# ----------------------------------------------------------------------------- 1. dao dien
def silence(history, ids):
    """So luot ke tu lan cuoi moi nhan vat len tieng (None = chua noi lan nao)."""
    out = {}
    for cid in ids:
        idx = next((i for i in range(len(history) - 1, -1, -1) if history[i]["speaker"] == cid), None)
        out[cid] = None if idx is None else len(history) - 1 - idx
    return out


def direct(api_key, model, pack, state, history, force_speaker=None, force_reason=None):
    present = [c for c in state.get("co_mat", []) if c in cast_by_id(pack)]
    cast = cast_by_id(pack)
    user = pack.get("user", "User")
    # nhan vat VANG MAT nhung duoc nguoi choi nhac ten trong 2 tin gan nhat (vd "Vy voi Han ghe choi do!")
    # -> cung la ung vien "dang buoc vao", de ho tu len tieng thay vi luon de nguoi dan truyen noi thay
    recent_user = " ".join(m["content"] for m in [m for m in history if m["speaker"] == "user"][-2:])
    arriving = [cid for cid in cast if cid not in present and mentioned(cast[cid], recent_user)]
    speakers = {cid: f"{cast[cid]['name']}: {cast[cid]['short']}" for cid in present}
    speakers.update({cid: f"{cast[cid]['name']} (đang tới hoặc vừa được {user} gọi vào): {cast[cid]['short']}" for cid in arriving})
    present = present + arriving  # de hoi truong card cho ca nguoi dang toi
    speakers[NARRATOR] = "Người dẫn truyện: mô tả cảnh, sự kiện, đưa nhân vật khác vào hoặc ra, nhân vật phụ nói"
    q = {"speaker": {"type": "choice", "criteria": speakers,
                     "instructions": f"Ai nên lên tiếng hoặc hành động tiếp theo để câu chuyện tự nhiên nhất? "
                                     f"Ưu tiên nhân vật mà {user} vừa gọi tên hoặc nói chuyện trực tiếp; nếu {user} không nói riêng với ai, "
                                     f"ưu tiên nhân vật có mặt đã im lặng lâu (xem da_im_lang). "
                                     f"Chọn người dẫn truyện khi cần mô tả, chuyển cảnh hoặc chưa có ai phù hợp."},
         "turn_type": {"type": "choice", "criteria": TURN_TYPES,
                       "instructions": "Lượt tiếp theo nên là loại nào?"}}
    for e in pack.get("lorebook", []):
        q[f"lore__{e['id']}"] = {"type": "noul", "instructions": f"Thông tin '{e['title']}' ({e['content'][:120]}) "
                                                                 f"có cần dùng cho lượt tiếp theo không?"}
    for cid in present:
        for f in cast[cid].get("fields", {}):
            if f in CORE_FIELDS:
                continue
            q[f"field__{cid}__{f}"] = {"type": "noul", "instructions": (
                f"Nếu {cast[cid]['name']} lên tiếng ở lượt tiếp theo, thông tin về "
                f"{rpformat.field_label(f) if cast[cid].get('src') else FIELD_LABELS.get(f, f)} sau đây có liên quan "
                f"và cần dùng không? «{cast[cid]['fields'][f][:160]}»")}
    last_is_user = bool(history) and history[-1]["speaker"] == "user"
    absent = [cid for cid in cast if cid not in present]
    for cid in absent:
        q[f"xuat_hien__{cid}"] = {"type": "noul", "instructions": (
            f"Theo diễn biến mới nhất, {cast[cid]['name']} ({cast[cid]['short']}) có vừa tới, đang đứng ở cửa, "
            f"hoặc vừa bước vào cảnh không? (Có thể được gọi bằng chức danh hay mô tả thay vì tên.)")}
    if last_is_user:
        # cau hoi nguyen tu, tach rieng khoi "ai noi tiep": model Decisions lam tot cau hoi 1 buoc hon cau hoi phai suy nhieu buoc
        targets = {cid: cast[cid]["name"] for cid in present}
        targets.update({cid: f"{cast[cid]['name']} ({cast[cid]['short']}; chưa có mặt)" for cid in absent})
        targets[GROUP] = "Cả nhóm, hoặc không nói với ai cụ thể"
        q["noi_voi"] = {"type": "choice", "criteria": targets, "instructions": (
            f"Trong tin cuối của {user}, {user} đang nói hoặc hỏi trực tiếp với ai? Để ý {user} đang đứng cạnh ai, "
            f"câu hỏi nhắm vào ai, và 'bà/ông/mày' trong câu chỉ người nào.")}
    quiet = {c: n for c in state.get("co_mat", []) if c in cast and (n := (state.get("im_lang") or {}).get(c, 0)) >= 2}
    st = {"boi_canh": state_text(pack, state), "dien_bien_gan_day": transcript(history, pack, 6) or pack.get("opening", ""),
          "da_im_lang": "; ".join(f"{cast[c]['name']}: có mặt {n} lượt mà chưa nói" for c, n in quiet.items()) or "không ai"}
    if last_is_user:
        st["tin_cuoi_cua_nguoi_choi"] = f"{user}: {history[-1]['content']}"
    t = time.time()
    d = _post(DECISIONS_URL, api_key, {"model": model, "state": st, "questions": q})
    a = d["answers"]
    seen = [cid for cid in absent if a.get(f"xuat_hien__{cid}", {}).get("noul", 0) >= ARRIVE_THRESHOLD]
    sp = a["speaker"]
    speaker, conf, why = sp["choice"], float(sp.get("confidence", 0)), "đạo diễn chọn"
    # luat du phong: nguoi choi goi ten nhan vat dang co mat
    last_user = next((m["content"] for m in reversed(history) if m["speaker"] == "user"), "")
    # nguoi choi vua noi voi nhung nguoi dang buoc vao -> de mot trong so ho tu len tieng, khong de nguoi dan truyen noi thay
    if speaker == NARRATOR and arriving and history and history[-1]["speaker"] == "user":
        probs = sp.get("probabilities") or {}
        speaker = max(arriving, key=lambda c: probs.get(c, 0))
        why = f"nhân vật đang bước vào, người chơi vừa nói với họ (đạo diễn nghiêng về người dẫn truyện {conf:.2f})"
    named = [cid for cid in present if mentioned(cast[cid], last_user)] if last_is_user else []
    addressee, addr_p = None, {}
    if "noi_voi" in a:
        addr_p = a["noi_voi"].get("probabilities") or {}
        ch = a["noi_voi"].get("choice")
        if ch in cast and float(a["noi_voi"].get("confidence", 0)) >= ADDRESS_CONF:
            addressee = ch
    if addressee in absent and addressee not in seen:
        seen.append(addressee)  # nguoi choi noi voi nguoi chua co mat -> ho dang toi
    new_arr = [c for c in seen if c not in arriving]
    arriving = arriving + new_arr
    if speaker == NARRATOR and new_arr and last_is_user and not addressee:
        speaker, why = new_arr[0], f"nhân vật vừa xuất hiện ({a[f'xuat_hien__{new_arr[0]}']['noul']:.2f}), không để người dẫn truyện nói thay"
    if addressee and speaker != addressee:
        speaker, why = addressee, (f"người chơi đang nói với {cast[addressee]['name']} ({float(a['noi_voi']['confidence']):.2f}; "
                                   f"đạo diễn chọn {name_of(pack, sp['choice'])})")
    # goi ten chi la du phong khi khong co luat nao o tren ap dung, va model khong cho la nguoi choi noi voi ca nhom
    elif named and speaker not in named and conf < 0.6 and why == "đạo diễn chọn" and addr_p.get(GROUP, 0) < 0.5:
        speaker, why = named[0], f"người chơi gọi tên (đạo diễn chỉ chắc {conf:.2f})"
    if force_speaker and (force_speaker in present or force_speaker == NARRATOR):
        speaker, why = force_speaker, force_reason or "phản ứng dây chuyền (chen vào sau lượt trước)"
    # nhung nguoi nguoi choi vua goi/noi toi ma chua dap -> uu tien chen vao sau (xem update_state)
    if last_is_user:
        # nhac ten ngoi thu ba ("Tuan no dang benh") khong tinh la dang hoi nguoi do
        owed = [addressee] if addressee else (named if addr_p.get(GROUP, 0) < 0.5 else [])
    else:
        owed = [c for c in state.get("cho_tra_loi", []) if c in cast]
    owed = [c for c in owed if c != speaker]
    lore = [e for e in pack.get("lorebook", []) if a.get(f"lore__{e['id']}", {}).get("noul", 0) >= 0.5]
    fields = list(CORE_FIELDS)
    if speaker in cast:
        fields += [f for f in cast[speaker].get("fields", {})
                   if f not in CORE_FIELDS and a.get(f"field__{speaker}__{f}", {}).get("noul", 0) >= 0.5]
    return {"speaker": speaker, "speaker_name": name_of(pack, speaker), "speaker_why": why, "speaker_conf": conf,
            "arriving": arriving, "entering": speaker in arriving, "owed": owed, "addressee": addressee,
            "address_probs": {(name_of(pack, k) if k != GROUP else "cả nhóm"): round(v, 2) for k, v in addr_p.items()},
            "speaker_probs": {name_of(pack, k): v for k, v in (sp.get("probabilities") or {}).items()},
            "turn_type": a["turn_type"]["choice"], "turn_type_label": TURN_TYPES.get(a["turn_type"]["choice"], ""),
            "lore": [{"id": e["id"], "title": e["title"], "p": round(a[f"lore__{e['id']}"]["noul"], 2)} for e in pack.get("lorebook", [])],
            "lore_used": [e["id"] for e in lore],
            "fields": [f for f in fields if speaker in cast and f in cast[speaker].get("fields", {})],
            "field_probs": {k.split("__", 2)[2]: round(v["noul"], 2) for k, v in a.items() if k.startswith(f"field__{speaker}__")},
            "cost": (d.get("usage") or {}).get("cost"), "secs": round(time.time() - t, 2), "model": d.get("model", model)}


# ----------------------------------------------------------------------------- 2. nguoi viet
def writer_messages(pack, state, history, dec, active_lore=None):
    cast = cast_by_id(pack)
    user = pack.get("user", "User")
    sp = dec["speaker"]
    present = [cast[c] for c in state.get("co_mat", []) if c in cast]
    parts = [pack.get("system", ""), f"[NGƯỜI CHƠI] {pack.get('user_persona') or user}",
             f"[TRẠNG THÁI HIỆN TẠI]\n{state_text(pack, state)}",
             "[NHÂN VẬT ĐANG CÓ MẶT]\n" + ("\n".join(f"- {c['name']}: {c['short']}" for c in present) or "(không ai ngoài người chơi)")]
    present_ids = [c for c in state.get("co_mat", []) if c in cast]
    arriving = [c for c in dec.get("arriving", []) if c in cast and c not in present_ids]
    if arriving:
        parts.append("[ĐANG BƯỚC VÀO CẢNH] " + ", ".join(f"{cast[c]['name']}: {cast[c]['short']}" for c in arriving))
        present_ids = present_ids + arriving  # de nap xung ho / quan he voi ho
    names = {c["id"]: c["name"] for c in pack["cast"]}
    if sp in cast and cast[sp].get("src"):
        prof = rpformat.profile(cast[sp], dec["fields"], present_ids, user, names)
        parts.append(f"[HỒ SƠ {cast[sp]['name'].upper()} — chỉ những phần cần cho lượt này]\n{prof}")
    elif sp in cast:
        prof = "\n".join(f"- {FIELD_LABELS.get(f, f).capitalize()}: {cast[sp]['fields'][f]}" for f in dec["fields"])
        parts.append(f"[HỒ SƠ {cast[sp]['name'].upper()} — chỉ những phần cần cho lượt này]\n{prof}")
    elif sp == NARRATOR:
        # nguoi dan truyen hay cho nhan vat chinh noi vai cau -> can cach xung ho cua tung nguoi
        speech = [rpformat.speech_line(c, present_ids, user, names) for c in pack["cast"]
                  if c.get("src") or c["fields"].get("cach_noi")]
        if speech:
            parts.append("[CÁCH NÓI CỦA CÁC NHÂN VẬT — dùng khi cho họ nói]\n" + "\n".join(speech))
    lore = [e for e in pack.get("lorebook", []) if e["id"] in dec.get("lore_used", [])]
    lore_lines = [f"- {e['title']}: {e['content']}" for e in lore]
    seen_contents = {e["content"].strip() for e in lore}
    if active_lore:
        char_name = cast[sp]["name"] if sp in cast else "Người dẫn truyện"
        for e in active_lore:
            content = e["content"].replace("{{user}}", user).replace("{{char}}", char_name)
            if content.strip() in seen_contents:
                continue
            seen_contents.add(content.strip())
            title = e.get("comment") or e.get("title") or "Lore"
            lore_lines.append(f"- {title}: {content}")
    if lore_lines:
        parts.append("[THÔNG TIN LIÊN QUAN]\n" + "\n".join(lore_lines))
    if sp == NARRATOR:
        others = [c["name"] for c in pack["cast"] if c["id"] not in state.get("co_mat", [])]
        role = (f"Viết lượt của NGƯỜI DẪN TRUYỆN: mô tả cảnh, sự kiện, có thể cho nhân vật phụ (cô Sáu, chú Tư, thầy Phúc...) nói. "
                f"Có thể đưa nhân vật chính khác vào cảnh nếu hợp lý ({', '.join(others) or 'không còn ai'}); nhân vật vừa xuất hiện "
                f"được nói 1–2 câu ngắn đúng tính cách để chào hoặc phản ứng, phần trò chuyện dài để dành cho lượt của họ.")
    else:
        role = (f"Viết lượt của {cast[sp]['name']}: lời nói và hành động của {cast[sp]['name']}. Các nhân vật khác chỉ được có phản ứng "
                f"nhỏ không lời nếu cần; KHÔNG viết thoại cho nhân vật chính khác, để họ tự nói ở lượt của họ.")
        if dec.get("entering"):
            others = [cast[c]["name"] for c in arriving if c != sp]
            role += (f" {cast[sp]['name']} đang bước vào cảnh: mở đầu bằng việc {cast[sp]['name']} xuất hiện" +
                     (f" (cùng {', '.join(others)}, chỉ tả họ vào chứ không cho họ nói)" if others else "") + ".")
    is_continue = not history or history[-1]["speaker"] != "user"
    if is_continue:
        flow = (f"{user} chưa nói gì thêm và muốn câu chuyện tự tiếp diễn: viết diễn biến TIẾP THEO một cách tự nhiên "
                f"(có thể có hành động, sự việc nhỏ hay người mới xuất hiện), tuyệt đối không lặp lại hay viết lại tin trước.")
    else:
        flow = (f"Phản hồi đúng vào điều {user} vừa nói hoặc làm. Không TỰ Ý nhảy thời gian hay đổi địa điểm; nhưng nếu {user} "
                f"vừa đề nghị hoặc bắt đầu đi chỗ khác thì đi theo {user} (được chuyển địa điểm, kể đoạn chuyển thật gọn).")
    instr = (f"{role}\nLoại lượt: {dec['turn_type_label']}.\n{flow}\n"
             f"Không viết lời, suy nghĩ hay hành động của {user}. Viết 1–2 đoạn. Không ghi tên người nói ở đầu. "
             f"Lời thoại đặt trong ngoặc kép \"...\"; hành động và miêu tả viết thành văn xuôi thường.")
    if state.get("tom_tat"):
        parts.append(f"[TÓM TẮT NHỮNG GÌ ĐÃ XẢY RA TRƯỚC ĐÓ]\n{state['tom_tat']}")
    minors = rpformat.minors_present(pack, state)
    if minors:
        parts.append(f"[GIỚI HẠN BẮT BUỘC] {', '.join(minors)} dưới 18 tuổi: tuyệt đối không có nội dung tình dục, gợi cảm "
                     f"hay lãng mạn liên quan tới các nhân vật này.")
    convo = transcript(history, pack, RECENT_TURNS)
    return [{"role": "system", "content": "\n\n".join(p for p in parts if p)},
            {"role": "user", "content": f"[DIỄN BIẾN]\n{pack.get('opening', '')}\n\n{convo}\n\n[YÊU CẦU]\n{instr}"}]


# ----------------------------------------------------------------------------- 4. cap nhat trang thai
def update_state(api_key, decide_model, extract_model, pack, state, history, owed=(), arriving=()):
    cast = cast_by_id(pack)
    user = pack.get("user", "User")
    recent = transcript(history, pack, 3)
    st_text = f"[TRẠNG THÁI TRƯỚC]\n{state_text(pack, state)}\n\n[LƯỢT MỚI]\n{recent}"
    q = {f"co_mat__{cid}": {"type": "noul", "instructions": f"Sau các lượt mới, {c['name']} ({c['short']}) có đang có mặt trong cảnh "
                                                             f"không, kể cả đang đứng ở cửa hay ngay gần đó và thấy mọi người? "
                                                             f"(Có thể được gọi bằng chức danh hay mô tả thay vì tên.)"}
         for cid, c in cast.items()}
    # phan ung day chuyen: ai (dang co mat, khong phai nguoi vua noi) muon chen vao ngay truoc khi nguoi choi noi tiep?
    last_speaker = history[-1]["speaker"] if history else None
    for cid, c in cast.items():  # hoi ca nguoi vua buoc vao trong luot nay; loc theo ai co mat SAU luot o duoi
        if cid != last_speaker:
            q[f"chen__{cid}"] = {"type": "noul", "instructions":
                                 f"Ngay sau lượt cuối, {c['name']} ({c['short']}) có lý do rõ ràng để lên tiếng hoặc phản ứng tiếp "
                                 f"(bị nhắc tới, bị hỏi, bị chọc) trước khi {user} nói không?"}
    q["dia_diem"] = {"type": "choice", "criteria": pack["locations"], "instructions": "Cảnh hiện đang diễn ra ở đâu sau các lượt mới?"}
    q["do_cang"] = {"type": "score", "criteria": TENSION, "instructions": "Độ căng thẳng của cảnh hiện tại?"}
    ex_fields = {"thoi_gian": {"type": "text", "instructions": "Thời gian hiện tại trong truyện sau các lượt mới (nếu không đổi thì lấy thời gian trước)"}}
    t = time.time()
    with ThreadPoolExecutor(2) as ex:
        fd = ex.submit(_post, DECISIONS_URL, api_key, {"model": decide_model, "state": {"noi_dung": st_text}, "questions": q})
        fe = ex.submit(_post, EXTRACTIONS_URL, api_key, {"model": extract_model, "state": st_text, "fields": ex_fields})
        d = fd.result()
        try:
            e = fe.result()
        except Exception as err:
            e = {"answers": {}, "error": str(err)}
    a = d["answers"]
    new = dict(state)
    # co quan tinh: vao canh can >= 0.5, nhung da co mat thi chi roi canh khi bang chung manh (< 0.2)
    # (tranh mat nhan vat chi vi ai do hoi "X dau roi?")
    before = set(state.get("co_mat", []))
    # nguoi dao dien thay dang toi / dung o cua: da thay nhau la tinh co mat, chi can bang chung yeu
    new["co_mat"] = [cid for cid in cast if a[f"co_mat__{cid}"]["noul"] >= (0.2 if cid in before or cid in arriving else 0.5)]
    if a["dia_diem"].get("confidence", 0) >= 0.3:
        new["dia_diem"] = a["dia_diem"]["choice"]
    new["do_cang"] = int(round(a["do_cang"]["score"]))
    tg = (e.get("answers") or {}).get("thoi_gian") or {}
    if tg.get("value") and tg.get("confidence", 0) >= 0.4:
        new["thoi_gian"] = keep_day(state.get("thoi_gian", ""), tg["value"])
    chen = {cid: a[k]["noul"] for cid in cast if (k := f"chen__{cid}") in a and cid in new["co_mat"]}
    fresh = [c for c in new["co_mat"] if c not in before and c != last_speaker]  # vua buoc vao, chua noi cau nao
    new["cho_tra_loi"] = list(dict.fromkeys([c for c in owed if c in new["co_mat"] and c != last_speaker] + fresh))
    # dem so luot moi nguoi co mat ma khong noi (nguoi vua noi ve 0) -> dao dien uu tien nguoi im lang lau
    old_q = state.get("im_lang") or {}
    new["im_lang"] = {c: (0 if c == last_speaker else old_q.get(c, 0) + 1) for c in new["co_mat"]}
    # nguoi bi goi/noi toi ma chua dap duoc uu tien: chi can model khong noi ro la "khong"
    # nguoi vua buoc vao ma chua noi luon duoc len tieng (gap nhau ma im re la vo ly); nguoi bi hoi can >= OWED_THRESHOLD
    owed_ok = [c for c in new["cho_tra_loi"] if c in fresh or chen.get(c, 0) >= OWED_THRESHOLD]
    if owed_ok:
        nxt = max(owed_ok, key=lambda c: chen.get(c, 0))
    else:
        need = lambda c: QUIET_THRESHOLD if new["im_lang"].get(c, 0) >= QUIET_TURNS else CHAIN_THRESHOLD
        ok = [c for c in chen if chen[c] >= need(c)]
        nxt = max(ok, key=chen.get) if ok else None
    cost = ((d.get("usage") or {}).get("cost") or 0) + ((e.get("usage") or {}).get("cost") or 0)
    detail = {"co_mat_p": {cast[c]["name"]: round(a[f"co_mat__{c}"]["noul"], 2) for c in cast},
              "chen_p": {cast[c]["name"]: round(p, 2) for c, p in chen.items()},
              "next_speaker": nxt, "next_why": (("vừa bước vào, chưa nói câu nào" if nxt in fresh else "chưa đáp lời người chơi") if owed_ok else "im lặng lâu, muốn chen vào"
                                                           if new["im_lang"].get(nxt, 0) >= QUIET_TURNS else "muốn chen vào") if nxt else None,
              "owed": [cast[c]["name"] for c in new["cho_tra_loi"]],
              "dia_diem_conf": round(a["dia_diem"].get("confidence", 0), 2), "thoi_gian_conf": round(tg.get("confidence", 0), 2),
              "extract_error": e.get("error")}
    return new, {"cost": cost, "secs": round(time.time() - t, 2), **detail}
