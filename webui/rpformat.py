"""Dinh dang nhan vat rplab-char-1 va the gioi rplab-world-1 (xem rp/formats/README.md).

- load_world(obj)      : rplab-world-1 (hoac goi cu rplab-group-1) -> pack noi bo cua engine group.py
- char_to_internal(c)  : rplab-char-1 -> nhan vat noi bo {id, name, short, aliases, age, src, fields}
- ccv3_to_char(card)   : character card SillyTavern CCv2/v3 -> (rplab-char-1, mo_dau|None)
- profile(...)         : ghep ho so nhan vat cho nguoi viet (chi phan can, xung ho/quan he theo nguoi co mat)
"""
import re, unicodedata

REQUIRED = ("id", "ten", "tuoi", "hu_cau", "mot_dong", "tinh_cach")
SELECT_LABELS = {"ngoai_hinh": "ngoại hình", "qua_khu": "quá khứ"}


class FormatError(ValueError):
    pass


def slug(text):
    plain = "".join(ch for ch in unicodedata.normalize("NFD", text.lower().replace("đ", "d")) if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_") or "nv"


def validate_char(c):
    if c.get("format") != "rplab-char-1":
        raise FormatError("không phải rplab-char-1")
    miss = [k for k in REQUIRED if k not in c]
    if miss:
        raise FormatError(f"{c.get('ten', '?')}: thiếu trường {', '.join(miss)}")
    if c["hu_cau"] is not True:
        raise FormatError(f"{c['ten']}: hu_cau phải là true. RP Lab chỉ nạp nhân vật hư cấu, không dựng nhân vật dựa trên người thật.")
    if not re.fullmatch(r"[a-z0-9_]+", c["id"]):
        raise FormatError(f"{c['ten']}: id chỉ gồm a-z, 0-9, _")


def char_to_internal(c):
    """Cac muc dao dien duoc chon (fields) + ban goc (src) de ghep ho so."""
    validate_char(c)
    fields = {}
    for k in ("ngoai_hinh", "qua_khu"):
        if c.get(k):
            fields[k] = c[k]
    for i, b in enumerate(c.get("bi_mat") or []):
        fields[f"bi_mat__{i}"] = b["noi_dung"] + (f" (chỉ lộ ra khi: {b['lo_khi']})" if b.get("lo_khi") else "")
    for k, v in (c.get("them") or {}).items():
        if v:
            fields[f"them__{k}"] = v
    return {"id": c["id"], "name": c["ten"], "short": c["mot_dong"], "aliases": c.get("goi") or [c["ten"].split()[-1]],
            "age": c.get("tuoi"), "src": c, "fields": fields}


def field_label(key):
    if key.startswith("bi_mat__"):
        return "bí mật"
    if key.startswith("them__"):
        return key[6:].replace("_", " ")
    return SELECT_LABELS.get(key, key.replace("_", " "))


def load_world(obj):
    """Tra ve (pack noi bo, canh bao[])."""
    warn = []
    if obj.get("format") == "rplab-group-1":  # goi cu: giu nguyen
        return obj, ["Gói định dạng cũ rplab-group-1; nên chuyển sang rplab-world-1."]
    if obj.get("format") != "rplab-world-1":
        raise FormatError("File không phải rplab-world-1 (hoặc rplab-group-1)")
    cast = []
    for c in obj.get("nhan_vat", []):
        if isinstance(c, str):
            raise FormatError(f"Nhân vật '{c}' chỉ được tham chiếu theo id: hãy nhúng nội dung nhân vật vào world hoặc nạp file nhân vật riêng.")
        cast.append(char_to_internal(c))
        if c.get("tuoi") is None:
            warn.append(f"{c['ten']}: chưa ghi tuổi.")
    mo = obj.get("mo_dau") or {}
    st = dict(mo.get("trang_thai") or {})
    st.setdefault("dia_diem", next(iter(obj.get("dia_diem") or {"?": ""})))
    st.setdefault("thoi_gian", "?")
    st.setdefault("co_mat", [])
    st.setdefault("do_cang", 1)
    pc = obj.get("nguoi_choi") or {}
    pack = {"format": "rplab-world-1", "name": obj.get("ten", "Thế giới"), "user": pc.get("ten", "User"),
            "user_persona": f"{pc.get('ten', 'User')}: {pc.get('mo_ta', '')}".strip(": "),
            "system": obj.get("luat", ""), "opening": mo.get("loi_dan", ""), "locations": obj.get("dia_diem") or {},
            "lorebook": [{"id": e.get("id") or slug(e.get("tieu_de", "lore")), "title": e.get("tieu_de", ""), "content": e.get("noi_dung", "")}
                         for e in obj.get("lore", [])],
            "cast": cast, "state": st}
    return pack, warn


def minors_present(pack, state):
    return [c["name"] for c in pack["cast"] if c["id"] in state.get("co_mat", []) and isinstance(c.get("age"), int) and c["age"] < 18]


def profile(char, selected, present_ids, user_name, names=None):
    """Ho so cho nguoi viet. selected: cac khoa trong char['fields'] dao dien da chon."""
    s = char["src"]
    lines = [f"- Tóm tắt: {s['mot_dong']}", f"- Tính cách: {s['tinh_cach']}"]
    if s.get("tuoi") is not None:
        lines.append(f"- Tuổi: {s['tuoi']}")
    if s.get("muc_tieu"):
        lines.append(f"- Đang muốn: {s['muc_tieu']}")
    noi = s.get("noi") or {}
    xh = noi.get("xung_ho") or {}
    targets = ["nguoi_choi"] + [p for p in present_ids if p != char["id"]]
    nm = lambda t: user_name if t == "nguoi_choi" else (names or {}).get(t, t)
    xh_lines = [f"với {nm(t)}: {xh[t]}" for t in targets if t in xh]
    if xh.get("mac_dinh"):
        xh_lines.append(f"mặc định: {xh['mac_dinh']}")
    if xh_lines:
        lines.append("- Xưng hô: " + "; ".join(xh_lines))
    if noi.get("giong"):
        lines.append(f"- Giọng nói: {noi['giong']}")
    if noi.get("cau_cua_mieng"):
        lines.append("- Câu cửa miệng (chỉ thỉnh thoảng dùng, không lặp mỗi lượt): " + " / ".join(noi["cau_cua_mieng"]))
    if noi.get("vi_du"):
        lines.append("- Câu nói mẫu: " + " ".join(noi["vi_du"][:3]))
    qh = s.get("quan_he") or {}
    rel = [f"với {nm(t)}: {qh[t]}" for t in targets if t in qh]
    if rel:
        lines.append("- Quan hệ: " + "; ".join(rel))
    for k in selected:
        if k in char["fields"]:
            lines.append(f"- {field_label(k).capitalize()}: {char['fields'][k]}")
    if s.get("gioi_han"):
        lines.append("- Giới hạn (bắt buộc): " + "; ".join(s["gioi_han"]))
    return "\n".join(lines)


def speech_line(char, present_ids, user_name, names=None):
    """1 dong cach noi cho luot nguoi dan truyen."""
    s = char.get("src")
    if not s:
        return f"- {char['name']}: {char['fields'].get('cach_noi', '')[:220]}"
    noi = s.get("noi") or {}
    xh = noi.get("xung_ho") or {}
    parts = [f"với {user_name if t == 'nguoi_choi' else (names or {}).get(t, t)}: {xh[t]}"
             for t in ["nguoi_choi"] + present_ids if t in xh and t != char["id"]]
    return f"- {char['name']}: xưng hô {'; '.join(parts) or xh.get('mac_dinh', '?')}. {noi.get('giong', '')}"


# ----------------------------------------------------------------------------- chuyen tu SillyTavern
def ccv3_to_char(card, existing_ids=()):
    d = card.get("data") or card
    name = (d.get("name") or "Nhân vật").strip()
    cid, i = slug(name), 2
    base = cid
    while cid in existing_ids:
        cid, i = f"{base}_{i}", i + 1
    desc = (d.get("description") or "").replace("{{char}}", name).strip()
    pers = (d.get("personality") or "").replace("{{char}}", name).strip()
    first = re.split(r"(?<=[.!?])\s|\n", desc or pers or name)[0][:140]
    m = re.search(r"(\d{1,2})\s*tuổi", desc + " " + pers)
    them = {}
    tinh_cach = pers or desc[:500]
    if desc and (pers or len(desc) > 500):
        them["mo_ta"] = desc[:1500]
    if d.get("system_prompt"):
        them["huong_dan_cu"] = d["system_prompt"].replace("{{char}}", name)[:800]
    # cau mau: gom tron luot cua {{char}} (toi dong {{user}}: hoac <START> ke tiep), uu tien cau thoai trong ngoac kep
    vi_du, block = [], None
    for line in (d.get("mes_example") or "").splitlines() + ["<START>"]:
        t = line.strip()
        if t.startswith("{{char}}:"):
            block = [t[len("{{char}}:"):].strip()]
        elif t.startswith("{{user}}:") or t.startswith("<START>"):
            if block:
                txt = " ".join(block)
                quote = re.search(r"[\"“]([^\"”]{6,})[\"”]", txt)
                vi_du.append(f"\"{quote.group(1).strip()[:200]}\"" if quote else txt[:220])
            block = None
        elif block is not None and t:
            block.append(t)
    for j, e in enumerate((d.get("character_book") or {}).get("entries", [])):
        if e.get("enabled", True) and e.get("content"):
            key = slug(e.get("comment") or "_".join(e.get("keys", [])[:2]) or f"lore_{j}")[:40]
            them[key] = e["content"].replace("{{char}}", name)[:1200]
    char = {"format": "rplab-char-1", "id": cid, "ten": name, "goi": [name.split()[-1]], "tuoi": int(m.group(1)) if m else None,
            "hu_cau": True, "mot_dong": first, "tinh_cach": tinh_cach,
            "noi": {"xung_ho": {}, "giong": "", "cau_cua_mieng": [], "vi_du": vi_du[:3]},
            "quan_he": {}, "bi_mat": [], "them": them}
    mo_dau = None
    if d.get("scenario") or d.get("first_mes"):
        mo_dau = {"boi_canh": (d.get("scenario") or "").replace("{{char}}", name),
                  "loi_dan": (d.get("first_mes") or "").replace("{{char}}", name)}
    return char, mo_dau
