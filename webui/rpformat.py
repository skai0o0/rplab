"""Kien truc du lieu 3 lop (char-v2, world-v2, Session State) va cac phan he Deterministic cho RP Lab v2.

Ho tro song song dinh dang moi (char-v2, world-v2) va tu dong nang cap tu dinh dang cu
(rplab-char-1, rplab-world-1, rplab-group-1) de tuong thich nguoc hoan toan.
"""
import copy
import datetime
from difflib import SequenceMatcher
import json
import os
import re
import unicodedata

REQUIRED_V1 = ("id", "ten", "tuoi", "hu_cau", "mot_dong", "tinh_cach")
SELECT_LABELS = {"ngoai_hinh": "ngoại hình", "qua_khu": "quá khứ"}

WEEKDAYS_VI = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")
TIME_DELTAS = {
    "instant": 0,
    "dialogue": 1,
    "brief": 5,
    "short": 15,
    "medium": 45,
    "long": 180,
}
TIME_DELTA_LABELS = {
    "instant": "Không trôi thêm thời gian (+0 phút)",
    "dialogue": "Trao đổi ngắn (+1 phút)",
    "brief": "Việc vặt thoáng qua (+5 phút)",
    "short": "Trò chuyện / làm việc ngắn (+15 phút)",
    "medium": "Một tiết / buổi nhỏ / ăn uống (+45 phút)",
    "long": "Nửa buổi / sự kiện dài (+180 phút)",
    "next_morning": "Chuyển sang sáng hôm sau (07:00)",
    "explicit_time": "Người chơi chỉ định một mốc giờ/ngày cụ thể",
}

DEFAULT_PRONOUN_DEFAULTS = {
    "close_peer": {"self": "tui", "call": "ông/bà"},
    "acquaintance": {"self": "mình", "call": "bạn"},
    "elder_formal": {"self": "em", "call": "anh/chị"},
    "angry": {"self": "tôi", "call": "ông/bà"},
}

_MALE_CALLS = ("ông", "anh", "thầy", "chú", "bác")
_FEMALE_CALLS = ("bà", "chị", "cô", "dì")
_TO_FEMALE = {"ông": "bà", "anh": "chị", "thầy": "cô"}
_TO_MALE = {"bà": "ông", "chị": "anh"}


def norm_gender(g):
    s = str(g or "").strip().lower()
    if s in ("nam", "male", "m", "trai", "con trai"):
        return "nam"
    if s in ("nữ", "nu", "female", "f", "gái", "con gái"):
        return "nu"
    return ""


def gender_of(pack, tid):
    if tid == "user":
        return norm_gender((pack.get("user") or {}).get("gender"))
    return norm_gender((((pack.get("characters") or {}).get(tid) or {}).get("meta") or {}).get("gender"))


def adapt_call(call, gender, fallback_name=""):
    """Tách 'ông/bà', 'thầy/cô/anh/chị'... theo giới tính target; đổi ông<->bà, anh<->chị nếu sai giới."""
    c = str(call or "").strip()
    if "/" in c:
        opts = [o.strip() for o in c.split("/") if o.strip()]
        pool = _MALE_CALLS if gender == "nam" else _FEMALE_CALLS if gender == "nu" else ()
        pick = next((o for o in opts if o.split()[0].lower() in pool), None) if pool else None
        if pick is None:
            pick = next((o for o in opts if o.split()[0].lower() not in _MALE_CALLS + _FEMALE_CALLS), None) or fallback_name or (opts[0] if opts else "")
        c = pick
    if not c:
        return c
    head, sep, rest = c.partition(" ")
    hl = head.lower()
    rep = _TO_FEMALE.get(hl) if gender == "nu" else _TO_MALE.get(hl) if gender == "nam" else None
    if rep:
        head = rep.capitalize() if head[:1].isupper() else rep
    return head + (sep + rest if rest else "")


USER_PRONOUN_PAIRS = {
    "tui_ong_ba": "Xưng tui và/hoặc gọi đối phương là ông, bà (kể cả câu ngắn chỉ gọi 'bà' hoặc 'ông', vd 'ăn chưa vậy bà?')",
    "tao_may": "Xưng tao và/hoặc gọi đối phương là mày",
    "minh_cau": "Xưng mình hoặc tớ, gọi đối phương là cậu",
    "anh_chi_em": "Xưng anh hoặc chị, gọi đối phương là em",
    "em_anh_chi": "Xưng em, gọi đối phương là anh hoặc chị",
    "toi_cau_ban": "Xưng tôi, gọi đối phương là cậu / bạn / anh / chị / cô (lịch sự, giữ khoảng cách)",
    "unclear": "Chưa rõ xưng hô (không có đại từ xưng hô trực tiếp nào trong câu, ví dụ 'Đi ăn không?')",
}


def infer_reciprocal(pair, ug, cg):
    """Suy diễn cặp xưng hô đối xứng (edge_user_to_char, edge_char_to_user) từ pair và giới tính."""
    u_is_f = (norm_gender(ug) == "nu")
    c_is_f = (norm_gender(cg) == "nu")
    table = {
        "tui_ong_ba": (
            {"self": "tui", "call": "bà" if c_is_f else "ông"},
            {"self": "tui", "call": "bà" if u_is_f else "ông"},
        ),
        "tao_may": (
            {"self": "tao", "call": "mày"},
            {"self": "tao", "call": "mày"},
        ),
        "minh_cau": (
            {"self": "mình", "call": "cậu"},
            {"self": "mình", "call": "cậu"},
        ),
        "anh_chi_em": (
            {"self": "chị" if u_is_f else "anh", "call": "em"},
            {"self": "em", "call": "chị" if u_is_f else "anh"},
        ),
        "em_anh_chi": (
            {"self": "em", "call": "chị" if c_is_f else "anh"},
            {"self": "chị" if c_is_f else "anh", "call": "em"},
        ),
        "toi_cau_ban": (
            {"self": "tôi", "call": "cậu"},
            {"self": "tôi", "call": "cậu"},
        ),
    }
    return copy.deepcopy(table.get(pair))


_COMPOUND_NON_SECOND = (
    "đàn ông", "ông trời", "ông bà", "ông già", "bà già", "ông chủ", "bà chủ",
    "ông nội", "bà nội", "ông ngoại", "bà ngoại", "ông thầy", "bà cô",
    "ông chú", "ông bác", "ông bảo vệ", "bà bán", "ông bán", "cậu ấm",
    "anh em", "chị em", "đàn anh", "đàn chị", "anh trai", "chị gái", "anh chàng",
)


def detect_user_pronoun_pair(text, pack=None, target_cid=None):
    """Nhan dien cap xưng hô user dung trong loi thoai truc tiep (0ms Python)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    quotes = re.findall(r'["“]([^"”]+)["”]', raw)
    cleaned = " ".join(quotes) if quotes else re.sub(r"\*[^*]+\*", " ", raw)

    for phrase in sorted(_COMPOUND_NON_SECOND, key=len, reverse=True):
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", " ", cleaned, flags=re.I)

    if pack:
        other_names = set()
        for cid, c in (pack.get("characters") or {}).items():
            if cid == target_cid:
                continue
            meta = c.get("meta") or {}
            if meta.get("name"):
                other_names.add(meta["name"])
                other_names.add(meta["name"].split()[-1])
            other_names.update(a for a in (meta.get("aliases") or []) if a)
        for nm in sorted((n for n in other_names if str(n).strip()), key=len, reverse=True):
            cleaned = re.sub(
                rf"\b(?:ông|bà|anh|chị|em|thầy|cô|chú|bác|cậu)\s+{re.escape(str(nm).strip())}\b",
                " ",
                cleaned,
                flags=re.I,
            )

    s = cleaned.lower()
    if re.search(r"\b(tao|mày)\b", s):
        return "tao_may"
    if re.search(r"\b(tui|ông|bà)\b", s):
        return "tui_ong_ba"
    if re.search(r"\btớ\b", s) or (re.search(r"\bmình\b", s) and re.search(r"\bcậu\b", s)):
        return "minh_cau"
    if re.search(r"\btôi\b", s) and re.search(r"\b(cậu|bạn)\b", s):
        return "toi_cau_ban"
    if (re.search(r"\banh\b", s) or re.search(r"\bchị\b", s)) and re.search(r"\bem\b", s):
        if re.search(r"\bem\s+(?:ơi|à|ạ|nha|nhé|hả|nè)\b", s) or re.search(r"\b(?:ơi|vậy|nha|nhé|hả|nè|đi)\s+em\b", s):
            return "anh_chi_em"
        if re.search(r"\b(?:anh|chị)\s+(?:ơi|à|ạ|nha|nhé|hả|nè)\b", s) or re.search(r"\b(?:ơi|vậy|nha|nhé|hả|nè|đi|ạ)\s+(?:anh|chị)\b", s):
            return "em_anh_chi"
    return None


def forbidden_pronouns(self_p, call_p, target_gender=""):
    """Sinh tối đa 6 đại từ cấm ngược chiều để chặn LLM trượt xưng hô."""
    s_low = str(self_p or "").strip().lower()
    c_low = str(call_p or "").strip().lower()
    c_head = c_low.split()[0] if c_low else ""
    allowed = {s_low, c_low, c_head} - {""}
    g = norm_gender(target_gender)
    if g == "nu":
        candidates = ["ông", "anh", "cậu", "tớ", "bạn", "tôi", "mình", "em", "tao", "mày"]
    elif g == "nam":
        candidates = ["cậu", "tớ", "bạn", "bà", "em", "tôi", "chị", "mình", "tao", "mày"]
    else:
        candidates = ["bà", "cậu", "tớ", "bạn", "tôi", "anh", "chị", "ông", "tao", "mày"]
    return [w for w in candidates if w not in allowed][:6]

_STOPWORDS = {
    "và", "hoặc", "nhưng", "mà", "nếu", "thì", "là", "của", "cho", "với", "trong", "ngoài",
    "trên", "dưới", "khi", "lúc", "từ", "đến", "tới", "về", "ở", "tại", "đang", "đã", "sẽ",
    "rất", "hơi", "quá", "lắm", "không", "chưa", "chẳng", "đừng", "hãy", "nên", "cần", "phải",
    "cái", "chiếc", "người", "bạn", "mình", "tôi", "tui", "nó", "họ", "anh", "chị", "em",
    "ông", "bà", "cô", "chú", "thầy", "một", "hai", "ba", "nhiều", "ít", "vài", "này", "kia",
    "đó", "đây", "nào", "gì", "sao", "như", "để", "vì", "bởi", "theo", "hay", "luôn", "thường",
}


class FormatError(ValueError):
    pass


def slug(text):
    plain = "".join(
        ch for ch in unicodedata.normalize("NFD", str(text or "").lower().replace("đ", "d"))
        if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_") or "nv"


def _auto_keywords(*texts, limit=6):
    """Rut tu khoa don gian cho cac muc upgrade tu v1 -> v2."""
    seen, out = set(), []
    for txt in texts:
        if not txt:
            continue
        for w in re.findall(r"[A-Za-zÀ-ỹ0-9_]{3,}", str(txt)):
            wl = w.lower()
            if wl in _STOPWORDS or wl in seen or wl.isdigit():
                continue
            seen.add(wl)
            out.append(w)
            if len(out) >= limit:
                return out
    return out


def _split_list(val):
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        parts = [p.strip(" -•\t") for p in re.split(r"[;\n]+", val) if p.strip(" -•\t")]
        return parts
    return []


# ----------------------------------------------------------------------------- Character Card (v1 & v2)
def validate_char_v2(c):
    if c.get("format") != "char-v2":
        raise FormatError("không phải char-v2")
    cid = c.get("id") or ""
    if not re.fullmatch(r"[a-z0-9_]+", cid):
        raise FormatError(f"{cid or '?'}: id chỉ gồm a-z, 0-9, _")
    meta = c.get("meta") or {}
    if not (meta.get("name") or "").strip():
        raise FormatError(f"{cid}: thiếu meta.name")
    if meta.get("fictional") is not True:
        raise FormatError(f"{meta['name']}: meta.fictional phải là true (chỉ nạp nhân vật hư cấu).")
    core = c.get("core") or {}
    if not (core.get("one_liner") or "").strip():
        raise FormatError(f"{meta['name']}: thiếu core.one_liner")
    pers = core.get("personality")
    if not pers or (isinstance(pers, list) and not any(str(x).strip() for x in pers)):
        raise FormatError(f"{meta['name']}: thiếu core.personality")


def validate_char(c):
    """Xac thuc ca rplab-char-1 va char-v2."""
    fmt = c.get("format")
    if fmt == "char-v2":
        return validate_char_v2(c)
    if fmt != "rplab-char-1":
        raise FormatError("không phải rplab-char-1 hoặc char-v2")
    miss = [k for k in REQUIRED_V1 if k not in c]
    if miss:
        raise FormatError(f"{c.get('ten', '?')}: thiếu trường {', '.join(miss)}")
    if c["hu_cau"] is not True:
        raise FormatError(f"{c['ten']}: hu_cau phải là true. RP Lab chỉ nạp nhân vật hư cấu, không dựng nhân vật dựa trên người thật.")
    if not re.fullmatch(r"[a-z0-9_]+", c["id"]):
        raise FormatError(f"{c['ten']}: id chỉ gồm a-z, 0-9, _")


def fill_placeholders(text, user_name="User", char_name=""):
    """Thay the {{user}}, <USER>, {{char}}, <BOT> bang ten that."""
    s = str(text or "")
    if user_name:
        s = re.sub(r"\{\{user\}\}|<USER>", user_name, s, flags=re.I)
    if char_name:
        s = re.sub(r"\{\{char\}\}|<BOT>", char_name, s, flags=re.I)
    return s


def _infer_pronouns_from_text(text):
    """Tu nhan dien cap xung ho tieng Viet dac trung tu mo ta / vi du hoi thoai cua card."""
    s = (text or "").lower()
    if not s.strip():
        return None
    # Uu tien mau khai bao tuong minh truoc (vd: "xưng tui - gọi ông", "xưng tôi, gọi cậu")
    explicit = re.search(
        r"xưng\s+[\"']?(tui|tôi|tao|em|anh|chị|tớ|mình)[\"']?\s*[-–,/và\s]+(?:gọi\s+)?[\"']?(ông/bà|ông|bà|mày|cậu|anh/chị|anh|chị|em|cưng|bạn)[\"']?",
        s,
    )
    if explicit:
        return {"self": explicit.group(1), "call": explicit.group(2)}
    # Nhan dien theo dau hieu khau ngu & hoi thoai thuc te
    if re.search(r"\btao\b", s) and re.search(r"\bmày\b", s):
        return {"self": "tao", "call": "mày"}
    if re.search(r"\btui\b", s) and re.search(r"\b(ông|bà)\b", s):
        has_ong = bool(re.search(r"\bông\b", s))
        has_ba = bool(re.search(r"\bbà\b", s))
        call = "ông" if (has_ong and not has_ba) else ("bà" if (has_ba and not has_ong) else "ông/bà")
        return {"self": "tui", "call": call}
    if re.search(r"\btôi\b", s) and re.search(r"\bcậu\b", s):
        return {"self": "tôi", "call": "cậu"}
    if re.search(r"\btớ\b", s) and re.search(r"\bcậu\b", s):
        return {"self": "tớ", "call": "cậu"}
    if re.search(r"\bchị\b", s) and re.search(r"\b(em|cưng)\b", s):
        return {"self": "chị", "call": "em"}
    if re.search(r"\banh\b", s) and re.search(r"\bem\b", s):
        return {"self": "anh", "call": "em"}
    if re.search(r"\bem\b", s) and re.search(r"\b(anh|chị)\b", s):
        has_anh = bool(re.search(r"\banh\b", s))
        has_chi = bool(re.search(r"\bchị\b", s))
        call = "anh" if (has_anh and not has_chi) else ("chị" if (has_chi and not has_anh) else "anh/chị")
        return {"self": "em", "call": call}
    if re.search(r"\btui\b", s):
        return {"self": "tui", "call": "ông/bà"}
    return None


def _parse_pronoun_pair(raw):
    if isinstance(raw, dict):
        return {"self": str(raw.get("self", "tui")).strip(), "call": str(raw.get("call", "ông/bà")).strip()}
    s = str(raw or "").strip()
    if "-" in s:
        a, b = s.split("-", 1)
        return {"self": a.strip(), "call": b.strip()}
    if "–" in s:
        a, b = s.split("–", 1)
        return {"self": a.strip(), "call": b.strip()}
    return {"self": s or "tui", "call": "ông/bà"}


def _infer_dialect(tone):
    t = (tone or "").lower()
    if "nam" in t:
        return "Nam Bộ"
    if "bắc" in t:
        return "Bắc Bộ"
    if "trung" in t:
        return "Trung Bộ"
    return "Trung tính"


def upgrade_char_v1_to_v2(c):
    """Chuyen rplab-char-1 (hoac char-v2 giu nguyen) sang char-v2 doc lap boi canh, giu nguyen ho so."""
    if c.get("format") == "char-v2":
        validate_char_v2(c)
        return copy.deepcopy(c)
    validate_char(c)
    noi = c.get("noi") or {}
    xh = noi.get("xung_ho") or {}
    them = c.get("them") or {}
    tone = (noi.get("giong") or "").strip()
    samples = list(noi.get("vi_du") or [])[:5]
    raw_sheet_parts = []
    if them.get("ho_so_day_du"):
        raw_sheet_parts.append(str(them["ho_so_day_du"]).strip())
    else:
        if them.get("mo_ta"):
            raw_sheet_parts.append(str(them["mo_ta"]).strip())
        if them.get("huong_dan_cu"):
            raw_sheet_parts.append(str(them["huong_dan_cu"]).strip())
    character_sheet = "\n\n".join(p for p in raw_sheet_parts if p)[:3000]

    inferred = _infer_pronouns_from_text(
        "\n".join([str(c.get("tinh_cach") or ""), tone, character_sheet, " ".join(samples)])
    )
    if xh.get("mac_dinh"):
        mac_dinh = _parse_pronoun_pair(xh["mac_dinh"])
    elif inferred:
        mac_dinh = inferred
    else:
        mac_dinh = {"self": "tui", "call": "ông/bà"}

    pers = _split_list(c.get("tinh_cach"))
    modules = []
    if (c.get("qua_khu") or "").strip():
        qk = c["qua_khu"].strip()
        modules.append({
            "id": "mod_qua_khu",
            "type": "backstory",
            "activation": "keyword",
            "when": "Khi nhắc tới quá khứ hoặc chuyện cũ của nhân vật.",
            "keywords": _auto_keywords(qk, "quá khứ", "ngày xưa", "năm nhất", "cấp ba"),
            "content": qk[:300],
        })
    for k, v in them.items():
        if k in ("mo_ta", "huong_dan_cu", "ho_so_day_du") or not str(v or "").strip():
            continue
        txt = str(v).strip()
        mtype = "habit" if k in ("so_thich", "thoi_quen", "do_vat") else "backstory"
        modules.append({
            "id": f"mod_{slug(k)}",
            "type": mtype,
            "activation": "keyword",
            "when": f"Khi nhắc tới {k.replace('_', ' ')}.",
            "keywords": _auto_keywords(k.replace("_", " "), txt),
            "content": txt[:300],
        })
    tics = list(noi.get("cau_cua_mieng") or [])
    texting_samples = []
    if samples:
        s0 = samples[0].strip().strip('"“”')
        texting_samples = [s0.lower()] if s0 else []
    core_obj = {
        "one_liner": (c.get("mot_dong") or "").strip()[:140],
        "personality": pers or [str(c.get("tinh_cach") or "").strip()],
        "values": (c.get("gia_tri") or c.get("muc_tieu") or "").strip(),
        "boundaries": list(c.get("gioi_han") or []),
    }
    if character_sheet:
        core_obj["character_sheet"] = character_sheet
    return {
        "format": "char-v2",
        "id": c["id"],
        "meta": {
            "name": c["ten"].strip(),
            "aliases": list(c.get("goi") or [c["ten"].split()[-1]]),
            "age": c.get("tuoi"),
            "gender": c.get("gioi_tinh") or "không rõ",
            "fictional": True,
        },
        "core": core_obj,
        "voice": {
            "dialect": _infer_dialect(tone),
            "tone": tone,
            "pronoun_defaults": {
                "close_peer": mac_dinh,
                "acquaintance": dict(mac_dinh),
                "elder_formal": dict(DEFAULT_PRONOUN_DEFAULTS["elder_formal"]),
                "angry": {"self": "tôi", "call": mac_dinh["call"]},
            },
            "modes": {
                "in_person": {
                    "style": "Văn nói trực tiếp, lồng cử chỉ vào văn xuôi.",
                    "tics": tics,
                    "samples": samples,
                },
                "texting": {
                    "style": "Tin nhắn ngắn tách dòng, giữ đúng chất giọng đời thường.",
                    "samples": texting_samples,
                },
            },
        },
        "appearance": {
            "summary": (c.get("ngoai_hinh") or "").strip(),
            "outfits": {"default": "Trang phục thường ngày"},
        },
        "modules": modules,
    }


def extract_v1_char_world_data(c):
    """Tach phan gan voi the gioi (goal, secrets, xung_ho, quan_he) tu card rplab-char-1."""
    if c.get("format") == "char-v2":
        return {"goal": "", "secrets": [], "xung_ho": {}, "quan_he": {}}
    cid = c["id"]
    noi = c.get("noi") or {}
    xh = noi.get("xung_ho") or {}
    qh = c.get("quan_he") or {}
    secrets = []
    for idx, b in enumerate(c.get("bi_mat") or []):
        txt = (b.get("noi_dung") or "").strip()
        if not txt:
            continue
        lo = (b.get("lo_khi") or "").strip()
        secrets.append({
            "id": f"sec_{cid}_{idx + 1:02d}",
            "text": txt,
            "known_by": [cid],
            "reveal_when": lo,
            "keywords": _auto_keywords(txt, lo),
        })
    return {
        "goal": (c.get("muc_tieu") or "").strip(),
        "secrets": secrets,
        "xung_ho": dict(xh),
        "quan_he": dict(qh),
    }


def char_to_internal(c):
    """Chuan hoa 1 nhan vat (v1 hoac v2) -> dict noi bo thong nhat."""
    v2 = upgrade_char_v1_to_v2(c)
    meta = v2["meta"]
    core = v2["core"]
    fields = {}
    if v2.get("appearance", {}).get("summary"):
        fields["ngoai_hinh"] = v2["appearance"]["summary"]
    for mod in v2.get("modules") or []:
        fields[mod["id"]] = mod["content"]
    return {
        "id": v2["id"],
        "name": meta["name"],
        "short": core["one_liner"],
        "aliases": meta.get("aliases") or [meta["name"].split()[-1]],
        "age": meta.get("age"),
        "v2": v2,
        "src": c if c.get("format") == "rplab-char-1" else None,
        "fields": fields,
    }


def field_label(key):
    if key.startswith("bi_mat__"):
        return "bí mật"
    if key.startswith("them__"):
        return key[6:].replace("_", " ")
    if key.startswith("mod_"):
        return key[4:].replace("_", " ")
    return SELECT_LABELS.get(key, key.replace("_", " "))


# ----------------------------------------------------------------------------- World Card (v1 & v2)
def _guess_location_tags(loc_id, text):
    s = f"{loc_id} {text}".lower()
    tags = []
    if any(k in s for k in ("phòng", "phong_", "ktx", "nhà", "nha_", "thư viện", "thu_vien", "giảng đường", "giang_duong", "căn tin", "can_tin", "quán", "quan_", "hội trường", "hoi_truong")):
        tags.append("indoor")
    if any(k in s for k in ("phòng 3", "phong_3", "phòng riêng", "phòng trọ", "xóm trọ", "riêng tư", "kín đáo", "ký túc", "ktx", "góc", "hồ sen", "ho_sen", "tầng 4", "tang_4", "boarding")):
        tags.append("private_possible")
    if "private_possible" not in tags and any(k in s for k in ("căn tin", "can_tin", "giảng đường", "giang_duong", "cổng", "cong_", "sân", "san_", "hành lang", "hanh_lang", "thư viện", "thu_vien", "hội trường", "hoi_truong", "trạm", "tram_")):
        tags.append("public")
    return list(dict.fromkeys(tags))


def _parse_v1_clock(time_str):
    """Co gang doc gio/phut/thu tu chuoi thoi gian v1 (vd: '12 giờ 30 trưa thứ Hai')."""
    s = (time_str or "").lower()
    hour, minute = 12, 0
    m = re.search(r"(\d{1,2})\s*(?:giờ|g|h|:)\s*(\d{1,2})?", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if ("chiều" in s or "tối" in s or "đêm" in s) and 1 <= hour < 12:
            hour += 12
        elif "trưa" in s and 1 <= hour <= 4:
            hour += 12
    # 2027-08-30 la Thu Hai
    day = 30
    for idx, wd in enumerate(WEEKDAYS_VI):
        if wd.lower() in s:
            # 2027-08-30 is Monday (idx 0)
            day = 30 + idx
            break
    dt = datetime.datetime(2027, 8, 30, hour % 24, minute % 60) + datetime.timedelta(days=day - 30)
    return {
        "mode": "calendar",
        "start": {"year": dt.year, "month": dt.month, "day": dt.day, "hour": dt.hour, "minute": dt.minute},
    }


def _resolve_char_from_disk(char_id, search_dirs=()):
    for d in search_dirs:
        if not d:
            continue
        p = os.path.join(d, f"{char_id}.json")
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8-sig"))
            except Exception:
                pass
    return None


def _norm_location_node(loc_id, val):
    if isinstance(val, dict):
        desc = (val.get("desc") or val.get("description") or "").strip()
        if not val.get("desc"):
            extra = []
            if val.get("zone"):
                extra.append(f"Vị trí: {val['zone']}")
            if val.get("vibe"):
                extra.append(f"Không khí: {val['vibe']}")
            if val.get("typical_activities"):
                acts = val["typical_activities"]
                extra.append("Hoạt động: " + ("; ".join(acts) if isinstance(acts, list) else str(acts)))
            if val.get("peak_hours"):
                extra.append(f"Giờ cao điểm: {val['peak_hours']}")
            if extra:
                desc = (desc + ("\n" if desc else "") + " · ".join(extra)).strip()
        tags = list(val.get("tags") or [])
        if not tags:
            tags = _guess_location_tags(loc_id, f"{val.get('name', '')} {desc}")
        return {
            "name": (val.get("name") or loc_id).strip(),
            "parent": val.get("parent"),
            "desc": desc,
            "tags": tags,
            "travel_min": int(val.get("travel_min") or 0),
            "lore_ids": list(val.get("lore_ids") or []),
        }
    s = str(val or "").strip()
    if ":" in s:
        name, desc = s.split(":", 1)
    else:
        name, desc = s or loc_id, ""
    return {
        "name": name.strip(),
        "parent": None,
        "desc": desc.strip(),
        "tags": _guess_location_tags(loc_id, s),
        "travel_min": 5,
        "lore_ids": [],
    }


def upgrade_world_v1_to_v2(obj, search_dirs=()):
    """Nang cap rplab-world-1 hoac rplab-group-1 -> world-v2."""
    fmt = obj.get("format")
    if fmt == "world-v2":
        return copy.deepcopy(obj)
    if fmt == "rplab-group-1":
        locations = {k: _norm_location_node(k, v) for k, v in (obj.get("locations") or {}).items()}
        st = obj.get("state") or {}
        characters, cast, edges = {}, [], {}
        for c in obj.get("cast") or []:
            cid = c["id"]
            f = c.get("fields") or {}
            tone = f.get("cach_noi", "")
            char_v2 = {
                "format": "char-v2",
                "id": cid,
                "meta": {"name": c["name"], "aliases": c.get("aliases") or [c["name"].split()[-1]], "age": c.get("age") or 20, "gender": "không rõ", "fictional": True},
                "core": {"one_liner": c.get("short", c["name"])[:140], "personality": _split_list(f.get("tinh_cach", c.get("short", ""))), "values": "", "boundaries": []},
                "voice": {
                    "dialect": _infer_dialect(tone),
                    "tone": tone,
                    "pronoun_defaults": copy.deepcopy(DEFAULT_PRONOUN_DEFAULTS),
                    "modes": {
                        "in_person": {"style": "Văn nói trực tiếp, lồng cử chỉ vào văn xuôi.", "tics": [], "samples": []},
                        "texting": {"style": "Tin nhắn ngắn tách dòng.", "samples": []},
                    },
                },
                "appearance": {"summary": f.get("ngoai_hinh", ""), "outfits": {"default": "Trang phục thường ngày"}},
                "modules": ([{"id": "mod_qua_khu", "type": "backstory", "activation": "keyword", "when": "Khi nhắc quá khứ.", "keywords": _auto_keywords(f["qua_khu"]), "content": f["qua_khu"]}] if f.get("qua_khu") else []),
            }
            characters[cid] = char_v2
            secrets = []
            if f.get("bi_mat"):
                secrets.append({"id": f"sec_{cid}_01", "text": f["bi_mat"], "known_by": [cid], "reveal_when": "khi bị hỏi thẳng hoặc tình huống ép buộc", "keywords": _auto_keywords(f["bi_mat"])})
            cast.append({
                "char_id": cid,
                "role_in_world": c.get("short", ""),
                "start_location": st.get("dia_diem") if cid in (st.get("co_mat") or []) else next(iter(locations), ""),
                "start_outfit": "default",
                "goal": "",
                "secrets": secrets,
            })
            if f.get("quan_he"):
                edges.setdefault(cid, {})
        return {
            "format": "world-v2",
            "id": slug(obj.get("name", "world")),
            "meta": {"name": obj.get("name", "Thế giới"), "genre": "Đời thường"},
            "rules": {"writing_style": obj.get("system", ""), "world_rules": []},
            "user": {"id": "user", "name": obj.get("user", "User"), "aliases": [], "persona": obj.get("user_persona", "")},
            "clock": _parse_v1_clock(st.get("thoi_gian", "")),
            "locations": locations,
            "lore": [{"id": e.get("id") or slug(e.get("title", "lore")), "title": e.get("title", ""), "activation": "director", "keys": [], "when": "", "content": e.get("content", "")} for e in obj.get("lorebook") or []],
            "characters": characters,
            "cast": cast,
            "relationships": {"auto_bootstrap": True, "edges": edges},
            "initial_facts": [],
            "opening": {"user_start_location": st.get("dia_diem") or next(iter(locations), ""), "tension": int(st.get("do_cang", 1)), "channel_type": "in_person", "narration": obj.get("opening", "")},
        }

    if fmt != "rplab-world-1":
        raise FormatError("File không phải world-v2, rplab-world-1 hoặc rplab-group-1")

    locations = {k: _norm_location_node(k, v) for k, v in (obj.get("dia_diem") or {}).items()}
    mo = obj.get("mo_dau") or {}
    st = mo.get("trang_thai") or {}
    start_loc = st.get("dia_diem") or next(iter(locations), "")
    co_mat = list(st.get("co_mat") or [])
    characters, cast, edges = {}, [], {}
    other_locs = [k for k in locations if k != start_loc]

    for idx, c in enumerate(obj.get("nhan_vat") or []):
        if isinstance(c, str):
            resolved = _resolve_char_from_disk(c, search_dirs)
            if not resolved:
                raise FormatError(f"Nhân vật '{c}' chỉ được tham chiếu theo id: hãy nhúng nội dung nhân vật vào world hoặc nạp file nhân vật riêng.")
            c = resolved
        v2_char = upgrade_char_v1_to_v2(c)
        cid = v2_char["id"]
        characters[cid] = v2_char
        wdata = extract_v1_char_world_data(c)
        c_loc = start_loc if cid in co_mat else (other_locs[idx % len(other_locs)] if other_locs else start_loc)
        cast.append({
            "char_id": cid,
            "role_in_world": v2_char["core"]["one_liner"],
            "start_location": c_loc,
            "start_outfit": "default",
            "goal": wdata["goal"],
            "secrets": wdata["secrets"],
        })
        # Chuyen xung_ho + quan_he tu v1 -> relationships.edges
        xh = wdata["xung_ho"]
        qh = wdata["quan_he"]
        all_targets = set(xh.keys()) | set(qh.keys())
        for raw_t in all_targets:
            if raw_t == "mac_dinh":
                continue
            tid = "user" if raw_t == "nguoi_choi" else raw_t
            pair = _parse_pronoun_pair(xh.get(raw_t) or xh.get("mac_dinh", "tui - ông/bà"))
            tag = (qh.get(raw_t) or "quen biết").strip()
            lvl = 75 if any(k in tag.lower() for k in ("thân", "người yêu", "chung phòng")) else 55
            edges.setdefault(cid, {})[tid] = {
                "tag": tag[:80],
                "level": lvl,
                "default": {**pair, "ref_3rd": tid},
                "overrides": {
                    "tense": {"self": "tôi", "call": pair["call"]},
                },
            }

    pc = obj.get("nguoi_choi") or {}
    return {
        "format": "world-v2",
        "id": slug(obj.get("ten", "world")),
        "meta": {"name": obj.get("ten", "Thế giới"), "genre": "Đời thường"},
        "rules": {"writing_style": obj.get("luat", ""), "world_rules": []},
        "user": {
            "id": "user",
            "name": pc.get("ten", "User"),
            "aliases": list(pc.get("goi") or []),
            **({"gender": norm_gender(pc.get("gioi_tinh"))} if pc.get("gioi_tinh") else {}),
            "persona": (pc.get("mo_ta") or "").strip(),
        },
        "clock": _parse_v1_clock(st.get("thoi_gian", "")),
        "locations": locations,
        "lore": [
            {
                "id": e.get("id") or slug(e.get("tieu_de", "lore")),
                "title": e.get("tieu_de", ""),
                "activation": "director",
                "keys": [],
                "when": "",
                "content": e.get("noi_dung", ""),
            }
            for e in (obj.get("lore") or [])
        ],
        "characters": characters,
        "cast": cast,
        "relationships": {"auto_bootstrap": True, "edges": edges},
        "initial_facts": [],
        "opening": {
            "user_start_location": start_loc,
            "tension": int(st.get("do_cang", 1)),
            "channel_type": "in_person",
            "narration": mo.get("loi_dan", ""),
        },
    }


def _normalize_world_v2(w, search_dirs=()):
    """Chuan hoa 1 world-v2 hoan chinh -> pack noi bo."""
    warn = []
    # Ho tro nhieu cach cung cap character card trong world-v2:
    # 1) w["characters"] (dict hoac list)  2) cast[].char  3) file tren dia
    chars_raw = {}
    raw_map = w.get("characters")
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            if isinstance(v, dict):
                chars_raw[v.get("id") or k] = v
    elif isinstance(raw_map, list):
        for v in raw_map:
            if isinstance(v, dict) and v.get("id"):
                chars_raw[v["id"]] = v

    characters = {}
    cast_out = []
    for entry in (w.get("cast") or []):
        cid = entry.get("char_id")
        if not cid:
            raise FormatError("Mục cast thiếu char_id")
        c_obj = entry.get("char") or entry.get("character") or chars_raw.get(cid) or _resolve_char_from_disk(cid, search_dirs)
        if not c_obj:
            raise FormatError(f"Không tìm thấy dữ liệu nhân vật cho char_id '{cid}'")
        v2_c = upgrade_char_v1_to_v2(c_obj)
        characters[cid] = v2_c
        if v2_c["meta"].get("age") is None:
            warn.append(f"{v2_c['meta']['name']}: chưa ghi tuổi.")
        cast_entry = {
            "char_id": cid,
            "role_in_world": (entry.get("role_in_world") or v2_c["core"]["one_liner"]).strip(),
            "start_location": entry.get("start_location") or "",
            "start_outfit": entry.get("start_outfit") or "default",
            "goal": (entry.get("goal") or "").strip(),
            "secrets": [
                {
                    "id": s.get("id") or f"sec_{cid}_{i + 1:02d}",
                    "text": (s.get("text") or "").strip(),
                    "known_by": list(s.get("known_by") or [cid]),
                    "reveal_when": (s.get("reveal_when") or "").strip(),
                    "keywords": list(s.get("keywords") or _auto_keywords(s.get("text"), s.get("reveal_when"))),
                }
                for i, s in enumerate(entry.get("secrets") or []) if (s.get("text") or "").strip()
            ],
        }
        if entry.get("reciprocate") is not None:
            cast_entry["reciprocate"] = entry.get("reciprocate")
        cast_out.append(cast_entry)

    # Chuan hoa locations
    locations = {lid: _norm_location_node(lid, lval) for lid, lval in (w.get("locations") or {}).items()}
    first_loc = next(iter(locations), "")
    for c in cast_out:
        if not c["start_location"] or c["start_location"] not in locations:
            c["start_location"] = first_loc

    # Chuan hoa clock
    clk = w.get("clock") or {}
    st_clk = clk.get("start") or {}
    clock = {
        "mode": clk.get("mode", "calendar"),
        "start": {
            "year": int(st_clk.get("year") or 2027),
            "month": int(st_clk.get("month") or 8),
            "day": int(st_clk.get("day") or 28),
            "hour": int(st_clk.get("hour") if st_clk.get("hour") is not None else 12),
            "minute": int(st_clk.get("minute") if st_clk.get("minute") is not None else 30),
        },
    }

    # Chuan hoa lore (cuu truong hop file dat nham 'modules' / 'keywords' o cap world)
    raw_lore = list(w.get("lore") or [])
    if not raw_lore and isinstance(w.get("modules"), list):
        raw_lore = list(w.get("modules") or [])
        if raw_lore:
            warn.append("File dùng 'modules' ở cấp world thay vì 'lore'; đã tự động chuyển sang 'lore'.")
    lore = []
    for e in raw_lore:
        keys = list(e.get("keys") or e.get("keywords") or [])
        title = (e.get("title") or e.get("when") or e.get("id") or "").strip()
        lore.append({
            "id": e.get("id") or slug(title or "lore"),
            "title": title,
            "activation": e.get("activation") or ("constant" if e.get("constant") else "keyword" if keys else "director"),
            "keys": keys,
            "when": (e.get("when") or "").strip(),
            "content": (e.get("content") or "").strip(),
        })
    core_w = w.get("core") if isinstance(w.get("core"), dict) else {}
    core_bits = []
    if core_w.get("one_liner"):
        core_bits.append(str(core_w["one_liner"]).strip())
    if core_w.get("atmosphere"):
        core_bits.append("Không khí: " + str(core_w["atmosphere"]).strip())
    if core_w.get("core_pillars"):
        pillars = core_w["core_pillars"]
        core_bits.append("Trụ cột: " + ("; ".join(pillars) if isinstance(pillars, list) else str(pillars)))
    if core_w.get("social_dynamics"):
        sd = core_w["social_dynamics"]
        core_bits.append("Xã hội: " + ("; ".join(sd) if isinstance(sd, list) else str(sd)))
    if core_bits and not any(e.get("id") == "lore_world_core" for e in lore):
        lore.insert(0, {
            "id": "lore_world_core",
            "title": "Bối cảnh tổng quan thế giới",
            "activation": "constant",
            "keys": [],
            "when": "",
            "content": "\n".join(core_bits),
        })

    user_raw = w.get("user") or {}
    user_obj = {
        "id": "user",
        "name": (user_raw.get("name") or "User").strip(),
        "aliases": list(user_raw.get("aliases") or []),
        "gender": norm_gender(user_raw.get("gender")),
        "persona": (user_raw.get("persona") or "").strip(),
    }

    op_raw = w.get("opening") or {}
    opening = {
        "user_start_location": op_raw.get("user_start_location") if op_raw.get("user_start_location") in locations else first_loc,
        "tension": int(op_raw.get("tension") if op_raw.get("tension") is not None else 1),
        "channel_type": op_raw.get("channel_type") or "in_person",
        "narration": (op_raw.get("narration") or "").strip(),
    }

    rel_raw = w.get("relationships") or {}
    edges = copy.deepcopy(rel_raw.get("edges") or {})
    # Dien lai ref_3rd neu bi trung id
    name_map = {"user": user_obj["name"], **{cid: characters[cid]["meta"]["name"].split()[-1] for cid in characters}}
    for src_id, targets in edges.items():
        for tgt_id, edge in (targets or {}).items():
            d = edge.setdefault("default", {"self": "tui", "call": name_map.get(tgt_id, tgt_id)})
            if not d.get("ref_3rd") or d.get("ref_3rd") == tgt_id:
                d["ref_3rd"] = name_map.get(tgt_id, tgt_id)

    rules = w.get("rules") or {}
    meta = w.get("meta") or {}
    pack = {
        "format": "world-v2",
        "id": w.get("id") or slug(meta.get("name", "world")),
        "meta": {"name": (meta.get("name") or "Thế giới").strip(), "genre": (meta.get("genre") or "").strip()},
        "rules": {
            "writing_style": (rules.get("writing_style") or "").strip(),
            "world_rules": list(rules.get("world_rules") or core_w.get("world_rules") or []),
        },
        "user": user_obj,
        "clock": clock,
        "locations": locations,
        "lore": lore,
        "characters": characters,
        "cast": cast_out,
        "relationships": {
            "auto_bootstrap": bool(rel_raw.get("auto_bootstrap", True)),
            "edges": edges,
        },
        "initial_facts": [
            {
                "id": f.get("id") or f"fact_init_{i + 1}",
                "time": f.get("time") or "trước mở đầu",
                "text": (f.get("text") or "").strip(),
                "entities": list(f.get("entities") or []),
                "known_by": list(f.get("known_by") or ["all"]),
                "keywords": list(f.get("keywords") or _auto_keywords(f.get("text"))),
                "status": f.get("status") or "active",
            }
            for i, f in enumerate(w.get("initial_facts") or []) if (f.get("text") or "").strip()
        ],
        "opening": opening,
    }
    # Tao state khoi tao kem vao pack["state"] de UI cu/moi dung ngay
    pack["state"] = bootstrap_session(pack)
    return pack, warn


def load_world(obj, search_dirs=None):
    """Nap world-v2, rplab-world-1 hoac rplab-group-1 -> (pack chuan v2, warnings[])."""
    if search_dirs is None:
        here = os.path.dirname(os.path.abspath(__file__))
        search_dirs = [os.path.join(here, "..", "samples", "chars")]
    fmt = obj.get("format")
    warn = []
    if fmt == "rplab-group-1":
        warn.append("Gói định dạng cũ rplab-group-1; đã tự động nâng cấp sang world-v2.")
        v2 = upgrade_world_v1_to_v2(obj, search_dirs)
    elif fmt == "rplab-world-1":
        v2 = upgrade_world_v1_to_v2(obj, search_dirs)
    elif fmt == "world-v2":
        v2 = obj
    else:
        raise FormatError("File không phải world-v2, rplab-world-1 hoặc rplab-group-1")
    pack, w2 = _normalize_world_v2(v2, search_dirs)
    return pack, warn + w2


# ----------------------------------------------------------------------------- Deterministic Clock
def time_period_vi(hour):
    if 0 <= hour < 5:
        return "rạng sáng"
    if 5 <= hour < 11:
        return "sáng"
    if 11 <= hour < 13:
        return "trưa"
    if 13 <= hour < 18:
        return "chiều"
    if 18 <= hour < 22:
        return "tối"
    return "đêm"


def format_clock(clock):
    if not isinstance(clock, dict):
        return str(clock or "?")
    mode = clock.get("mode", "calendar")
    hour = int(clock.get("hour", 12))
    minute = int(clock.get("minute", 0))
    period = time_period_vi(hour)
    if mode == "timeless":
        return f"{hour:02d}:{minute:02d} {period}"
    if mode == "day_counter":
        day = int(clock.get("day", 1))
        return f"Ngày {day} · {hour:02d}:{minute:02d} {period}"
    year = int(clock.get("year", 2027))
    month = int(clock.get("month", 8))
    day = int(clock.get("day", 28))
    try:
        dt = datetime.datetime(year, month, day, hour, minute)
        wd = WEEKDAYS_VI[dt.weekday()]
        return f"{wd}, {dt.day:02d}/{dt.month:02d}/{dt.year} · {dt.hour:02d}:{dt.minute:02d} {period}"
    except ValueError:
        return f"{day:02d}/{month:02d}/{year} · {hour:02d}:{minute:02d} {period}"


def apply_time_delta(clock, delta_key, explicit_obj=None, travel_min=0):
    """Cap nhat dong ho deterministic theo buoc nhay huu han hoac explicit_time."""
    c = dict(clock or {})
    mode = c.get("mode", "calendar")
    year = int(c.get("year", 2027))
    month = int(c.get("month", 8))
    day = int(c.get("day", 28))
    hour = int(c.get("hour", 12))
    minute = int(c.get("minute", 30))
    try:
        dt = datetime.datetime(year, month, day, hour, minute)
    except ValueError:
        dt = datetime.datetime(2027, 8, 28, 12, 30)

    if delta_key == "next_morning":
        dt = (dt + datetime.timedelta(days=1)).replace(hour=7, minute=0)
    elif delta_key == "explicit_time" and isinstance(explicit_obj, dict):
        nh = explicit_obj.get("hour")
        nm = explicit_obj.get("minute")
        day_offset = max(0, int(explicit_obj.get("day_offset") or 0))
        allow_rewind = bool(explicit_obj.get("allow_rewind"))
        new_dt = dt + datetime.timedelta(days=day_offset)
        if nh is not None:
            nh = max(0, min(23, int(nh)))
            nm = max(0, min(59, int(nm if nm is not None else 0)))
            cand = new_dt.replace(hour=nh, minute=nm)
            # Monotonic Guard (#17): khong bao gio lui dong ho va khong tu +1 ngay khi day_offset == 0 (tru luc dat moc gio dau canh)
            if not allow_rewind and day_offset == 0 and cand < dt:
                cand_pm = new_dt.replace(hour=nh + 12, minute=nm) if (nh < 12 and dt.hour >= 12) else None
                if cand_pm is not None and cand_pm >= dt:
                    cand = cand_pm
                else:
                    cand = dt
            new_dt = cand
        if explicit_obj.get("year") and explicit_obj.get("month") and explicit_obj.get("day"):
            try:
                cand_date = new_dt.replace(
                    year=int(explicit_obj["year"]),
                    month=int(explicit_obj["month"]),
                    day=int(explicit_obj["day"]),
                )
                if allow_rewind or cand_date >= dt:
                    new_dt = cand_date
            except ValueError:
                pass
        if not allow_rewind and new_dt < dt:
            new_dt = dt
        dt = new_dt
    else:
        mins = TIME_DELTAS.get(delta_key, 0)
        if delta_key == "instant" and travel_min:
            mins = int(travel_min)
        dt = dt + datetime.timedelta(minutes=mins)

    return {
        "mode": mode,
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
    }


def _clock_to_dt(clock):
    c = clock or {}
    try:
        return datetime.datetime(
            int(c.get("year", 2027)),
            int(c.get("month", 8)),
            int(c.get("day", 28)),
            int(c.get("hour", 12)),
            int(c.get("minute", 30)),
        )
    except ValueError:
        return datetime.datetime(2027, 8, 28, 12, 30)


def clock_diff_minutes(old_clock, new_clock):
    """Chenh lech phut giua hai moc clock."""
    return int((_clock_to_dt(new_clock) - _clock_to_dt(old_clock)).total_seconds() // 60)


def reset_stale_npc_locations(pack, state, old_clock, new_clock, excluded_ids=()):
    """Case #11: Reset vi tri NPC khong di cung user ve start_location/schedule_hint khi qua ngay hoac tua >= 3h."""
    if not isinstance(old_clock, dict) or not isinstance(new_clock, dict):
        return []
    day_changed = (
        old_clock.get("day") != new_clock.get("day")
        or old_clock.get("month") != new_clock.get("month")
        or old_clock.get("year") != new_clock.get("year")
    )
    if not day_changed and clock_diff_minutes(old_clock, new_clock) < 180:
        return []
    hour = int(new_clock.get("hour", 12))
    tod = "morning" if 5 <= hour < 12 else ("afternoon" if 12 <= hour < 18 else ("evening" if 18 <= hour < 22 else "night"))
    locs = state.get("locations") or pack.get("locations") or {}
    ents = state.get("entities") or {}
    excl = set(excluded_ids or ())
    moved = []
    for c in pack.get("cast") or []:
        cid = c.get("char_id")
        if not cid or cid in excl or cid not in ents:
            continue
        ent = ents[cid]
        ent.pop("at_door", None)
        ent.pop("transit", None)
        sched = c.get("schedule_hint")
        target = sched.get(tod) if isinstance(sched, dict) else None
        if not target or target not in locs:
            target = c.get("start_location")
        if target and target in locs and ent.get("location_id") != target:
            ent["location_id"] = target
            ent["silence_turns"] = 0
            moved.append(cid)
    return moved


# ----------------------------------------------------------------------------- Spatial Tree
def location_breadcrumb(locations, loc_id):
    """Sinh breadcrumb tu node hien tai len root (co chong vong lap)."""
    if not loc_id or loc_id not in (locations or {}):
        return str(loc_id or "?")
    names, seen = [], set()
    cur = loc_id
    while cur and cur in locations and cur not in seen:
        seen.add(cur)
        node = locations[cur]
        names.append(node.get("name") or cur)
        cur = node.get("parent")
    return " > ".join(names)


def location_tags(locations, loc_id):
    """Tra ve tags cua node hien tai (khong de tag 'public' cua hanh lang lan sang phong rieng)."""
    if not loc_id or loc_id not in (locations or {}):
        return []
    node = locations[loc_id]
    own = list(node.get("tags") or [])
    if own:
        return own
    # Fallback len cha neu node con khong khai bao tag nao
    cur = node.get("parent")
    seen = {loc_id}
    while cur and cur in locations and cur not in seen:
        seen.add(cur)
        ptags = list(locations[cur].get("tags") or [])
        if ptags:
            return ptags
        cur = locations[cur].get("parent")
    return []


def inherited_lore_ids(locations, loc_id):
    """Ke thua lore_ids tu node hien tai len cac node cha."""
    lids, seen = [], set()
    cur = loc_id
    while cur and cur in (locations or {}) and cur not in seen:
        seen.add(cur)
        for lid in (locations[cur].get("lore_ids") or []):
            if lid not in lids:
                lids.append(lid)
        cur = locations[cur].get("parent")
    return lids


def candidate_locations(locations, current_loc_id, limit=8):
    """Loc toi da 5-8 node ung vien cuc bo quanh current_loc_id cho Pha 1A."""
    if not locations:
        return {"stay": "Ở nguyên vị trí hiện tại", "new_place": "Địa điểm mới phát sinh ngoài bản đồ"}
    cur = locations.get(current_loc_id) or {}
    cur_name = cur.get("name") or current_loc_id
    out = {"stay": f"Ở nguyên tại {cur_name}"}
    added = []
    parent_id = cur.get("parent")
    # 1. Node cha
    if parent_id and parent_id in locations:
        added.append(parent_id)
    # 2. Node con truc tiep
    for lid, node in locations.items():
        if node.get("parent") == current_loc_id and lid != current_loc_id and lid not in added:
            added.append(lid)
    # 3. Node anh em cung cha
    for lid, node in locations.items():
        if node.get("parent") == parent_id and lid != current_loc_id and lid not in added:
            added.append(lid)
    # 4. Cac node con cua root / cac dia diem khac de du ngu canh chuyen canh
    for lid in locations:
        if lid != current_loc_id and lid not in added:
            added.append(lid)
    for lid in added[: max(1, limit - 2)]:
        node = locations[lid]
        bc = location_breadcrumb(locations, lid)
        desc = (node.get("desc") or "")[:70]
        out[lid] = f"{bc}" + (f": {desc}" if desc else "")
    out["new_place"] = "Một địa điểm mới chưa có trong bản đồ hiện tại"
    return out


_LOC_FILLER = {
    "di", "xuong", "len", "ra", "vao", "toi", "den", "sang", "qua", "ve", "lai", "chay",
    "o", "tai", "trong", "ngoai", "phia", "ben", "gan", "khu", "co", "so", "truong", "cua",
    "cai", "cho", "moi", "nay", "kia", "do", "day",
}
_LOC_PHRASE_SYNONYMS = (
    (r"\b(?:can\s*tin|cang\s*tin|canteen|nha\s*an)\b", "cantin"),
    (r"\b(?:thu\s*vien)\b", "thuvien"),
    (r"\b(?:ky\s*tuc\s*xa|ktx)\b", "kytucxa"),
    (r"\b(?:phong\s*tro)\b", "phongtro"),
    (r"\b(?:cong\s*sau)\b", "congsau"),
    (r"\b(?:cong\s*truoc|cong\s*chinh)\b", "congchinh"),
    (r"\b(?:san\s*thuong)\b", "santhuong"),
    (r"\b(?:hanh\s*lang)\b", "hanhlang"),
)


def _norm_loc_str(s):
    plain = "".join(
        ch for ch in unicodedata.normalize("NFD", str(s or "").lower().replace("đ", "d"))
        if unicodedata.category(ch) != "Mn"
    )
    plain = re.sub(r"[^a-z0-9]+", " ", plain).strip()
    for pat, rep in _LOC_PHRASE_SYNONYMS:
        plain = re.sub(pat, rep, plain)
    toks = [t for t in plain.split() if t and t not in _LOC_FILLER]
    return toks


def find_matching_location(locations, query_name, query_id=""):
    """Case #10: Tim node dia diem co san trong toan bo locations de tranh tao node rac trung ten."""
    if not locations or (not query_name and not query_id):
        return None
    qid_norm = slug(query_id) if str(query_id or "").strip() else ""
    q_toks = _norm_loc_str(f"{query_name or ''} {query_id or ''}")
    if qid_norm and qid_norm in locations:
        return qid_norm
    q_clean = " ".join(q_toks)
    if not q_clean:
        return None

    best_lid, best_score = None, 0.0
    for lid, node in locations.items():
        if qid_norm and slug(lid) == qid_norm:
            return lid
        n_name = (node.get("name") if isinstance(node, dict) else str(node)) or lid
        n_toks = _norm_loc_str(n_name)
        id_toks = _norm_loc_str(lid)
        n_all = list(dict.fromkeys(n_toks + id_toks))
        n_clean = " ".join(n_toks) or lid.lower()
        if q_clean == n_clean:
            return lid
        q_set, n_set = set(q_toks), set(n_toks)
        inter_name = q_set & n_set
        inter_any = q_set & set(n_all)
        ratio = SequenceMatcher(None, q_clean, n_clean).ratio()
        containment = len(inter_name) / max(1, len(n_set)) if n_set else 0.0
        strong_hit = any(len(t) >= 5 for t in inter_any)
        # Neu khop 1 danh tu dia diem dac trung (>=5 ky tu, vd 'cantin') va khong xung dot tu rieng
        conflict_tokens = (n_set - q_set - {"b", "c"}) if len(n_set) > 1 else set()
        if q_set == n_set and q_set:
            score = 0.99
        elif strong_hit and len(inter_any) == len(n_set):
            score = 0.95
        elif strong_hit and not any(len(t) >= 2 for t in conflict_tokens):
            score = 0.85
        elif containment >= 0.75:
            score = 0.82
        else:
            score = ratio
        if score > best_score:
            best_lid, best_score = lid, score
    return best_lid if best_score >= 0.78 else None


# ----------------------------------------------------------------------------- Turn 0 Bootstrap & Session State
def _resolve_sparse_edge(pack, edges, src_id, tgt_id):
    """Tra ve canh da khai bao, hoac fallback khong ep cung 'minh - ban' (is_fallback=True)."""
    if src_id in edges and tgt_id in edges[src_id]:
        edge = copy.deepcopy(edges[src_id][tgt_id])
        edge.setdefault("is_fallback", False)
        edge.setdefault("lock", edge.get("lock"))
        return edge
    target_name = pack["user"]["name"] if tgt_id == "user" else pack["characters"][tgt_id]["meta"]["name"].split()[-1]
    if src_id == "user":
        return {
            "tag": "quen biết",
            "level": 50,
            "is_fallback": True,
            "lock": None,
            "default": {"self": "tôi", "call": target_name, "ref_3rd": target_name},
            "overrides": {},
        }
    char_v2 = pack["characters"].get(src_id) or {}
    core = char_v2.get("core") or {}
    voice = char_v2.get("voice") or {}
    ip_samples = " ".join(((voice.get("modes") or {}).get("in_person") or {}).get("samples") or [])
    inferred = _infer_pronouns_from_text(
        "\n".join([core.get("character_sheet", ""), core.get("one_liner", ""), voice.get("tone", ""), ip_samples])
    )
    pd = voice.get("pronoun_defaults") or DEFAULT_PRONOUN_DEFAULTS
    base_pair = dict(inferred or pd.get("close_peer") or pd.get("acquaintance") or {"self": "tui", "call": "ông/bà"})
    src_age = (char_v2.get("meta") or {}).get("age")
    tgt_age = None if tgt_id == "user" else (((pack.get("characters") or {}).get(tgt_id) or {}).get("meta") or {}).get("age")
    target_is_elder = isinstance(src_age, int) and isinstance(tgt_age, int) and tgt_age - src_age >= 3
    formal_src = pd.get("elder_formal") if target_is_elder else (pd.get("acquaintance") or base_pair)
    formal = dict(formal_src or base_pair)
    angry = dict(pd.get("angry") or {"self": "tôi", "call": base_pair.get("call", "ông/bà")})
    tgt_gender = gender_of(pack, tgt_id)
    for pair in (base_pair, formal, angry):
        pair["call"] = adapt_call(pair.get("call", "ông/bà"), tgt_gender, target_name)
    return {
        "tag": "quen biết",
        "level": 50,
        "is_fallback": True,
        "lock": None,
        "default": {"self": base_pair["self"], "call": base_pair["call"], "ref_3rd": target_name},
        "overrides": {
            "private": dict(base_pair),
            "public_formal": dict(formal),
            "tense": dict(angry),
        },
    }


def bootstrap_session(pack):
    """Turn 0 Bootstrap: nhan ban World Card -> Session State dong."""
    clk_start = (pack.get("clock") or {}).get("start") or {}
    clock = {
        "mode": (pack.get("clock") or {}).get("mode", "calendar"),
        "year": int(clk_start.get("year", 2027)),
        "month": int(clk_start.get("month", 8)),
        "day": int(st_val) if (st_val := clk_start.get("day", 28)) is not None else 28,
        "hour": int(clk_start.get("hour", 12)),
        "minute": int(clk_start.get("minute", 30)),
    }
    locations = copy.deepcopy(pack.get("locations") or {})
    op = pack.get("opening") or {}
    user_loc = op.get("user_start_location") or next(iter(locations), "")
    entities = {
        "user": {
            "location_id": user_loc,
            "mood": "normal",
            "outfit": "default",
            "activity": "",
            "silence_turns": 0,
            "entered_turn": 0,
        }
    }
    for c in pack.get("cast") or []:
        cid = c["char_id"]
        entities[cid] = {
            "location_id": c.get("start_location") or user_loc,
            "mood": "normal",
            "outfit": c.get("start_outfit") or "default",
            "activity": "",
            "silence_turns": 0,
            "entered_turn": 0,
        }

    co_located = [c["char_id"] for c in (pack.get("cast") or []) if entities[c["char_id"]]["location_id"] == user_loc]
    ch_type = op.get("channel_type") or "in_person"
    active_channel = {
        "channel_type": ch_type,
        "participants": ["user"] + co_located,
        "group_id": None,
    }

    all_ids = ["user"] + [c["char_id"] for c in (pack.get("cast") or [])]
    raw_edges = (pack.get("relationships") or {}).get("edges") or {}
    full_edges = {}
    for s_id in all_ids:
        full_edges[s_id] = {}
        for t_id in all_ids:
            if s_id == t_id:
                continue
            full_edges[s_id][t_id] = _resolve_sparse_edge(pack, raw_edges, s_id, t_id)

    hard_memory = []
    for f in pack.get("initial_facts") or []:
        hard_memory.append(copy.deepcopy(f))
    for c in pack.get("cast") or []:
        cid = c["char_id"]
        for s in c.get("secrets") or []:
            hard_memory.append({
                "id": s["id"],
                "time": "trước mở đầu",
                "text": s["text"],
                "entities": [cid],
                "known_by": list(s.get("known_by") or [cid]),
                "reveal_when": s.get("reveal_when", ""),
                "keywords": list(s.get("keywords") or []),
                "status": "secret",
            })

    state = {
        "clock": clock,
        "locations": locations,
        "entities": entities,
        "active_channel": active_channel,
        "tension": int(op.get("tension", 1)),
        "scene_vibe": op.get("scene_vibe") or {0: "relaxed", 1: "lively", 2: "awkward", 3: "dramatic", 4: "conflict"}.get(int(op.get("tension", 1)), "lively"),
        "relationships": {"edges": full_edges},
        "hard_memory": hard_memory,
        "recent_scenes": [],
        "story_arc": "",
        "summarized_upto": 0,
        "owed": [],
        "summary_sync": {"pending": False, "last_job_id": None},
    }
    return sync_legacy_state(pack, state)


def sync_legacy_state(pack, state):
    """Dong bo cac truong tien ich / tuong thich nguoc tu Session State v2."""
    s = dict(state)
    entities = dict(s.get("entities") or {})
    locs = s.get("locations") or pack.get("locations") or {}
    user_loc = (entities.get("user") or {}).get("location_id") or s.get("dia_diem") or next(iter(locs or {"?": {}}))
    cast_ids = []
    fallback_other = next((k for k in locs if k != user_loc), user_loc)
    for c in pack.get("cast") or []:
        cid = c.get("char_id") or c.get("id")
        if not cid:
            continue
        cast_ids.append(cid)
        if cid not in entities:
            sloc = c.get("start_location")
            if not sloc or (locs and sloc not in locs):
                sloc = fallback_other or user_loc
            entities[cid] = {
                "location_id": sloc,
                "mood": "normal",
                "outfit": c.get("start_outfit") or "default",
                "activity": "",
                "silence_turns": 0,
                "entered_turn": 0,
            }
    s["entities"] = entities
    rel = s.get("relationships")
    if isinstance(rel, dict) and isinstance(rel.get("edges"), dict):
        chars_map = pack.get("characters") or {}
        all_ids = ["user"] + [cid for cid in cast_ids if cid in chars_map]
        raw_edges = (pack.get("relationships") or {}).get("edges") or {}
        edges = rel["edges"]
        for s_id in all_ids:
            row = edges.setdefault(s_id, {})
            for t_id in all_ids:
                if s_id != t_id and t_id not in row:
                    row[t_id] = _resolve_sparse_edge(pack, raw_edges, s_id, t_id)
    co_mat = [cid for cid in cast_ids if (entities.get(cid) or {}).get("location_id") == user_loc]
    s["dia_diem"] = user_loc
    s["thoi_gian"] = format_clock(s.get("clock")) if isinstance(s.get("clock"), dict) else str(s.get("thoi_gian", "?"))
    s["co_mat"] = co_mat
    s["do_cang"] = int(s.get("tension", s.get("do_cang", 1)))
    s["im_lang"] = {cid: int((entities.get(cid) or {}).get("silence_turns", 0)) for cid in co_mat}
    s["cho_tra_loi"] = list(s.get("owed") or s.get("cho_tra_loi") or [])
    s["tom_tat_upto"] = int(s.get("summarized_upto", s.get("tom_tat_upto", 0)))
    arc = (s.get("story_arc") or "").strip()
    scenes = s.get("recent_scenes") or []
    parts = ([f"[ĐẠI CƯƠNG CỐT TRUYỆN]\n{arc}"] if arc else []) + [
        (sc if isinstance(sc, str) else sc.get("text", "")) for sc in scenes
    ]
    s["tom_tat"] = "\n\n".join(p for p in parts if p)
    return s


def co_located_npcs(pack, state):
    entities = state.get("entities") or {}
    user_loc = (entities.get("user") or {}).get("location_id") or state.get("dia_diem")
    return [c["char_id"] for c in (pack.get("cast") or []) if (entities.get(c["char_id"]) or {}).get("location_id") == user_loc]


def resolve_pronouns(pack, state, speaker_id, channel_participants, focus_id=None):
    """Ap dung precedence: tense (khi chinh speaker tuc gian hoac tension>=4 & it than) > public_formal > private > default."""
    if speaker_id not in (pack.get("characters") or {}):
        return {"tier": "default", "pairs": [], "ref_3rd": [], "lines": [], "summary_text": "", "strict_rule_text": "", "focus_rule_text": "", "focus_pair": None}
    entities = state.get("entities") or {}
    sp_ent = entities.get(speaker_id) or {}
    loc_id = sp_ent.get("location_id") or (entities.get("user") or {}).get("location_id")
    tags = set(location_tags(state.get("locations") or pack.get("locations") or {}, loc_id))
    ch_type = (state.get("active_channel") or {}).get("channel_type", "in_person")
    present_npcs = co_located_npcs(pack, state)
    tension = int(state.get("tension", 1))
    sp_angry = (sp_ent.get("mood") == "angry")
    is_public = "public" in tags
    is_private = (ch_type == "in_person" and len(present_npcs) == 1 and "private_possible" in tags)

    edges = ((state.get("relationships") or {}).get("edges") or {}).get(speaker_id) or {}
    user_name = pack["user"]["name"]
    chars = pack.get("characters") or {}
    target_name = lambda tid: user_name if tid == "user" else (chars.get(tid, {}).get("meta", {}).get("name") or tid)

    raw_world_edges = (pack.get("relationships") or {}).get("edges") or {}
    pairs = []
    tiers_used = []
    for tid in channel_participants:
        if tid == speaker_id:
            continue
        edge = edges.get(tid) or _resolve_sparse_edge(pack, raw_world_edges, speaker_id, tid)
        is_stale_fb = (
            edge.get("tag") == "quen biết"
            and int(edge.get("level", 50)) == 50
            and (edge.get("default") or {}).get("self") == "mình"
            and (edge.get("default") or {}).get("call") == "bạn"
            and tid not in (raw_world_edges.get(speaker_id) or {})
        )
        if is_stale_fb:
            edge = _resolve_sparse_edge(pack, raw_world_edges, speaker_id, tid)
        is_fb = bool(edge.get("is_fallback", False)) or is_stale_fb
        lvl = int(edge.get("level", 50))
        ov = edge.get("overrides") or {}
        dflt = edge.get("default") or {"self": "tui", "call": "ông/bà"}
        tier = "default"
        pair = dflt
        # Chi kich hoat tense khi CHINH speaker dang buc boi (angry) hoac dung nhip xung dot (conflict)
        use_tense = sp_angry or (state.get("scene_vibe") in (None, "", "conflict") and tension >= 4 and lvl < 70)
        if use_tense:
            tier = "tense"
            pair = ov.get("tense") or dflt
        elif is_public and ov.get("public_formal"):
            tier = "public_formal"
            pair = ov["public_formal"]
        elif is_private and ov.get("private"):
            tier = "private"
            pair = ov["private"]
        tiers_used.append(tier)
        tname = target_name(tid)
        short_name = user_name if tid == "user" else (chars.get(tid, {}).get("meta", {}).get("name") or tid).split()[-1]
        g = gender_of(pack, tid)
        self_val = pair.get("self", dflt.get("self", "tui"))
        call = adapt_call(pair.get("call", dflt.get("call", "ông/bà")), g, short_name)
        forb = forbidden_pronouns(self_val, call, g)
        pairs.append({
            "target_id": tid,
            "target_name": tname,
            "gender": g,
            "tier": tier,
            "is_fallback": is_fb,
            "lock": edge.get("lock"),
            "self": self_val,
            "call": call,
            "forbidden": forb,
            "tag": edge.get("tag", ""),
            "level": lvl,
        })

    if focus_id and any(p["target_id"] == focus_id for p in pairs):
        pairs.sort(key=lambda p: 0 if p["target_id"] == focus_id else 1)

    ref_3rd = []
    for c in pack.get("cast") or []:
        cid = c["char_id"]
        if cid == speaker_id or cid in channel_participants:
            continue
        edge = edges.get(cid) or {}
        r3 = (edge.get("default") or {}).get("ref_3rd") or chars[cid]["meta"]["name"].split()[-1]
        ref_3rd.append({"id": cid, "name": chars[cid]["meta"]["name"], "ref_3rd": r3})

    lines = []
    for p in pairs:
        g_label = "nam" if p["gender"] == "nam" else ("nữ" if p["gender"] == "nu" else "")
        g_prefix = f"{g_label}; " if g_label else ""
        focus_prefix = "[ĐANG NÓI CHUYỆN TRỰC TIẾP] " if (focus_id and p["target_id"] == focus_id) else ""
        forb_txt = f" (không dùng: {', '.join(p['forbidden'])})" if p.get("forbidden") else ""
        if p["is_fallback"]:
            lines.append(
                f"- {focus_prefix}Với {p['target_name']} ({g_prefix}{p['tag']}, chưa khóa cứng quan hệ): dùng đúng giọng và cách xưng hô tự nhiên trong hồ sơ nhân vật "
                f"(tham khảo gợi ý: \"{p['self']} - {p['call']}\"){forb_txt}"
            )
        else:
            lines.append(
                f"- {focus_prefix}Với {p['target_name']} ({g_prefix}{p['tag']}, thân thiết {p['level']}/100, chế độ [{p['tier']}]): xưng \"{p['self']}\", gọi \"{p['call']}\"{forb_txt}"
            )
    if ref_3rd:
        lines.append("- Khi nhắc ngôi thứ 3 về người vắng mặt: " + "; ".join(f"{r['name']} → gọi là \"{r['ref_3rd']}\"" for r in ref_3rd))

    strict_pairs = [p for p in pairs if (not p["is_fallback"]) or (p.get("lock") in ("manual", "observed"))]
    summary_text = "; ".join(f"với {p['target_name']}: xưng '{p['self']}' - gọi '{p['call']}'" for p in pairs)
    strict_rule_text = "; ".join(f"với {p['target_name']}: xưng '{p['self']}' - gọi '{p['call']}'" for p in strict_pairs)
    focus_strict = next((p for p in strict_pairs if p["target_id"] == focus_id), None) if focus_id else None
    focus_pair = next((p for p in pairs if p["target_id"] == focus_id), None) if focus_id else None
    focus_rule_text = (
        f"với {focus_strict['target_name']}: xưng '{focus_strict['self']}' - gọi '{focus_strict['call']}'"
        if focus_strict else ""
    )
    dominant_tier = tiers_used[0] if tiers_used else ("tense" if sp_angry else "public_formal" if is_public else "private" if is_private else "default")
    return {
        "tier": dominant_tier,
        "pairs": pairs,
        "ref_3rd": ref_3rd,
        "lines": lines,
        "summary_text": summary_text,
        "strict_rule_text": strict_rule_text,
        "focus_rule_text": focus_rule_text,
        "focus_pair": focus_pair,
    }


def minors_present(pack, state):
    chars = pack.get("characters") or {}
    present = state.get("co_mat") or co_located_npcs(pack, state)
    out = []
    for cid in present:
        c = chars.get(cid)
        if not c:
            continue
        age = (c.get("meta") or {}).get("age")
        if isinstance(age, int) and age < 18:
            out.append(c["meta"]["name"])
    return out


# ----------------------------------------------------------------------------- Legacy / Helper formatting
def profile(char, selected, present_ids, user_name, names=None):
    """Ho so rut gon cho truong hop goi truc tiep tu ma cu."""
    v2 = char.get("v2") or upgrade_char_v1_to_v2(char["src"] if char.get("src") else char)
    meta, core, voice = v2["meta"], v2["core"], v2["voice"]
    pers = ", ".join(core["personality"]) if isinstance(core["personality"], list) else str(core["personality"])
    lines = [f"- Tóm tắt: {core['one_liner']}", f"- Tính cách: {pers}"]
    if meta.get("age") is not None:
        lines.append(f"- Tuổi: {meta['age']}")
    if core.get("values"):
        lines.append(f"- Giá trị / mục tiêu: {core['values']}")
    if voice.get("tone"):
        lines.append(f"- Giọng nói: {voice['tone']}")
    ip = (voice.get("modes") or {}).get("in_person") or {}
    if ip.get("tics"):
        lines.append("- Câu cửa miệng: " + " / ".join(ip["tics"]))
    if ip.get("samples"):
        lines.append("- Câu mẫu: " + " ".join(ip["samples"][:2]))
    for k in selected:
        if k in (char.get("fields") or {}):
            lines.append(f"- {field_label(k).capitalize()}: {char['fields'][k]}")
    if core.get("boundaries"):
        lines.append("- Giới hạn (bắt buộc): " + "; ".join(core["boundaries"]))
    return "\n".join(lines)


def speech_line(char, present_ids, user_name, names=None, state=None, pack=None):
    """1 dong cach noi + xưng hô cho luot nguoi dan truyen."""
    v2 = char.get("v2") or (pack and (pack.get("characters") or {}).get(char["id"]))
    if not v2:
        return f"- {char['name']}: {char.get('fields', {}).get('cach_noi', '')[:200]}"
    tone = (v2.get("voice") or {}).get("tone", "")
    if pack and state:
        pr = resolve_pronouns(pack, state, v2["id"], ["user"] + list(present_ids))
        xh_str = pr["summary_text"] or ""
        return f"- {v2['meta']['name']}: {xh_str}. {tone}"
    return f"- {v2['meta']['name']}: {tone}"


# ----------------------------------------------------------------------------- Chuyen tu SillyTavern (CCv2/v3)
def ccv3_to_char(card, existing_ids=(), user_name=""):
    d = card.get("data") or card
    name = (d.get("name") or "Nhân vật").strip()
    cid, i = slug(name), 2
    base = cid
    while cid in existing_ids:
        cid, i = f"{base}_{i}", i + 1
    desc = fill_placeholders((d.get("description") or "").strip(), user_name=user_name, char_name=name)
    pers = fill_placeholders((d.get("personality") or "").strip(), user_name=user_name, char_name=name)
    sys_p = fill_placeholders((d.get("system_prompt") or "").strip(), user_name=user_name, char_name=name)
    mes_ex = fill_placeholders((d.get("mes_example") or "").strip(), user_name=user_name, char_name=name)

    first = re.split(r"(?<=[.!?])\s|\n", desc or pers or name)[0][:140]
    m = re.search(r"(\d{1,2})\s*tuổi", desc + " " + pers)
    them = {}
    tinh_cach = pers or desc[:500]
    sheet_parts = []
    if desc:
        them["mo_ta"] = desc[:3000]
        sheet_parts.append(desc)
    if sys_p:
        them["huong_dan_cu"] = sys_p[:1500]
        sheet_parts.append(sys_p)
    if sheet_parts:
        them["ho_so_day_du"] = "\n\n".join(sheet_parts)[:3000]

    vi_du, block = [], None
    for line in mes_ex.splitlines() + ["<START>"]:
        t = line.strip()
        if t.startswith(f"{name}:") or t.startswith("{{char}}:"):
            prefix = f"{name}:" if t.startswith(f"{name}:") else "{{char}}:"
            block = [t[len(prefix):].strip()]
        elif t.startswith("{{user}}:") or (user_name and t.startswith(f"{user_name}:")) or t.startswith("<START>"):
            if block:
                txt = " ".join(block)
                quote = re.search(r"[\"“]([^\"”]{6,})[\"”]", txt)
                vi_du.append(f"\"{quote.group(1).strip()[:240]}\"" if quote else txt[:260])
            block = None
        elif block is not None and t:
            block.append(t)

    for j, e in enumerate((d.get("character_book") or {}).get("entries", [])):
        if e.get("enabled", True) and e.get("content"):
            key = slug(e.get("comment") or "_".join(e.get("keys", [])[:2]) or f"lore_{j}")[:40]
            them[key] = fill_placeholders(e["content"], user_name=user_name, char_name=name)[:1200]

    inferred = _infer_pronouns_from_text("\n".join([desc, pers, sys_p, mes_ex, " ".join(vi_du)]))
    xh = {"mac_dinh": f"{inferred['self']} - {inferred['call']}"} if inferred else {}
    char = {
        "format": "rplab-char-1", "id": cid, "ten": name, "goi": [name.split()[-1]],
        "tuoi": int(m.group(1)) if m else None, "hu_cau": True, "mot_dong": first,
        "tinh_cach": tinh_cach,
        "noi": {"xung_ho": xh, "giong": sys_p[:240] if sys_p else "", "cau_cua_mieng": [], "vi_du": vi_du[:5]},
        "quan_he": {}, "bi_mat": [], "them": them,
    }
    mo_dau = None
    if d.get("scenario") or d.get("first_mes"):
        mo_dau = {
            "boi_canh": fill_placeholders(d.get("scenario") or "", user_name=user_name, char_name=name),
            "loi_dan": fill_placeholders(d.get("first_mes") or "", user_name=user_name, char_name=name),
        }
    return char, mo_dau


def build_single_world_pack(
    card_obj,
    user_name="Nem",
    user_gender="nam",
    user_persona="",
    custom_style="",
    greeting_idx=0,
    include_card_lore=True,
):
    """Dong goi 1 nhan vat (char-v2, rplab-char-1, SillyTavern card hoac None) thanh world-v2 1-1."""
    user_name = (user_name or "Nem").strip() or "Nem"
    ug = norm_gender(user_gender or "nam")
    if ug == "khong_ro":
        ug = "nam"

    greetings = []
    scenario_txt = ""
    world_lore = []

    if not card_obj:
        char_v2 = {
            "format": "char-v2",
            "id": "assistant",
            "meta": {"name": "Nhân vật", "aliases": ["Nhân vật"], "age": 22, "gender": "nữ", "fictional": True},
            "core": {
                "one_liner": "Nhân vật đồng hành trò chuyện 1-1 tự nhiên cùng {{user}}.",
                "personality": ["Tự nhiên, linh hoạt, phản ứng bám sát ngữ cảnh"],
                "values": "",
                "boundaries": [],
            },
            "voice": {
                "dialect": "Trung tính",
                "tone": "Tự nhiên, gần gũi.",
                "pronoun_defaults": copy.deepcopy(DEFAULT_PRONOUN_DEFAULTS),
                "modes": {
                    "in_person": {"style": "Đối thoại trực tiếp tự nhiên.", "tics": [], "samples": []},
                    "texting": {"style": "Nhắn tin ngắn gọn tự nhiên.", "tics": [], "samples": []},
                },
            },
            "appearance": {"summary": "", "outfits": {"default": "Trang phục thường ngày"}},
            "modules": [],
        }
    elif card_obj.get("format") in ("char-v2", "rplab-char-1"):
        char_v2 = char_to_internal(card_obj)["v2"]
        raw_gr = card_obj.get("greetings")
        if isinstance(raw_gr, list):
            greetings = [str(g) for g in raw_gr if str(g).strip()]
        fm = card_obj.get("first_mes") or (card_obj.get("mo_dau") or {}).get("loi_dan") or ""
        if str(fm).strip() and str(fm) not in greetings:
            greetings.insert(0, str(fm))
        if not greetings:
            ip_samples = ((char_v2.get("voice") or {}).get("modes") or {}).get("in_person", {}).get("samples") or []
            if ip_samples and str(ip_samples[0]).strip():
                greetings = [str(ip_samples[0]).strip()]
        scenario_txt = (card_obj.get("mo_dau") or {}).get("boi_canh") or card_obj.get("scenario") or ""
    else:
        char_v1, mo_dau = ccv3_to_char(card_obj, user_name=user_name)
        char_v2 = char_to_internal(char_v1)["v2"]
        d = card_obj.get("data") or card_obj
        cname_raw = char_v2["meta"]["name"]
        desc = fill_placeholders((d.get("description") or "").strip(), user_name=user_name, char_name=cname_raw)
        pers = fill_placeholders((d.get("personality") or "").strip(), user_name=user_name, char_name=cname_raw)
        sheet_parts = [p for p in (desc, pers) if p]
        if sheet_parts:
            char_v2["core"]["character_sheet"] = "\n\n".join(sheet_parts)[:4000]
        raw_alts = list(d.get("alternate_greetings") or [])
        greetings = [str(g) for g in ([d.get("first_mes") or ""] + raw_alts) if str(g).strip()]
        scenario_txt = fill_placeholders((d.get("scenario") or "").strip(), user_name=user_name, char_name=cname_raw)

        cb = d.get("character_book") or {}
        raw_entries = cb.get("entries")
        book_list = list(raw_entries.values()) if isinstance(raw_entries, dict) else list(raw_entries or [])
        book_prefixes = set()
        for j, e in enumerate(book_list):
            content_raw = (e.get("content") or "").strip()
            if content_raw:
                book_prefixes.add(fill_placeholders(content_raw, user_name=user_name, char_name=cname_raw)[:120])
            if not (e.get("enabled", True) and not e.get("disable", False)) or not content_raw:
                continue
            if include_card_lore:
                raw_k = e.get("keys") if e.get("keys") is not None else e.get("key")
                if isinstance(raw_k, list):
                    keys = [str(k).strip() for k in raw_k if str(k).strip()]
                else:
                    keys = [k.strip() for k in str(raw_k or "").split(",") if k.strip()]
                is_const = bool(e.get("constant") and not keys)
                world_lore.append({
                    "id": f"card_lore_{j}",
                    "title": (e.get("comment") or e.get("name") or (keys[0] if keys else f"Lore #{j + 1}")).strip(),
                    "activation": "constant" if is_const else ("keyword" if keys else "director"),
                    "keys": keys,
                    "when": "",
                    "content": content_raw,
                })
        if book_prefixes:
            char_v2["modules"] = [
                m for m in (char_v2.get("modules") or [])
                if (m.get("content") or "").strip()[:120] not in book_prefixes
            ]

    cid = char_v2["id"]
    cname = char_v2["meta"]["name"]
    c_gender = norm_gender(char_v2["meta"].get("gender"))

    g_idx = int(greeting_idx) if greeting_idx is not None else 0
    if 0 <= g_idx < len(greetings):
        opening_nar = fill_placeholders(greetings[g_idx], user_name=user_name, char_name=cname)
    else:
        opening_nar = ""

    style_txt = fill_placeholders((custom_style or "").strip(), user_name=user_name, char_name=cname)
    if not style_txt:
        style_txt = (
            "Truyện tương tác 1-1 tự nhiên bằng tiếng Việt; lời thoại trực tiếp đặt trong ngoặc kép \"...\", "
            "hành động và cảm xúc lồng vào văn xuôi tự nhiên."
        )

    pd = (char_v2.get("voice") or {}).get("pronoun_defaults") or {}
    c_pair = dict(pd.get("close_peer") or {"self": "tui", "call": "ông/bà"})
    c_self = c_pair.get("self", "tui")
    c_call_raw = c_pair.get("call", "ông/bà")
    c_call = adapt_call(c_call_raw, ug, user_name)

    cs_l, cc_l = c_self.lower(), c_call.lower()
    pair_key = None
    if cs_l == "tao" or cc_l == "mày":
        pair_key = "tao_may"
    elif cs_l == "em" and cc_l in ("anh", "chị"):
        pair_key = "anh_chi_em"
    elif cs_l in ("anh", "chị") and cc_l == "em":
        pair_key = "em_anh_chi"
    elif cs_l in ("mình", "tớ") and cc_l == "cậu":
        pair_key = "minh_cau"
    elif cs_l == "tôi" and cc_l in ("cậu", "bạn"):
        pair_key = "toi_cau_ban"
    elif cs_l == "tui" and cc_l in ("ông", "bà", "ông/bà"):
        pair_key = "tui_ong_ba"

    rec = infer_reciprocal(pair_key, ug, c_gender) if pair_key else None
    u_pair = dict(rec[0]) if rec else {"self": "tui", "call": "bà" if c_gender == "nu" else "ông"}
    u_pair["ref_3rd"] = cname.split()[-1]

    c_ov = {}
    if pd.get("acquaintance"):
        acq = pd["acquaintance"]
        c_ov["public_formal"] = {"self": acq.get("self", c_self), "call": adapt_call(acq.get("call", c_call_raw), ug, user_name)}
    if pd.get("angry"):
        ang = pd["angry"]
        c_ov["tense"] = {"self": ang.get("self", "tôi"), "call": adapt_call(ang.get("call", c_call_raw), ug, user_name)}

    scen_filled = fill_placeholders(scenario_txt, user_name=user_name, char_name=cname).strip()
    world_dict = {
        "format": "world-v2",
        "id": f"single_{cid}",
        "meta": {"name": cname, "genre": "Chat đơn 1-1"},
        "rules": {
            "writing_style": style_txt,
            "world_rules": [
                "Trò chuyện 1-1 nhập vai chân thực, tôn trọng cá tính, giọng nói và xưng hô của nhân vật.",
                "Không tự ý nhảy thời gian, chuyển địa điểm hoặc viết thay hành động/lời nói của người chơi.",
            ],
        },
        "user": {
            "id": "user",
            "name": user_name,
            "aliases": [user_name],
            "gender": ug,
            "persona": fill_placeholders((user_persona or "").strip(), user_name=user_name, char_name=cname),
        },
        "clock": {"mode": "calendar", "start": {"year": 2027, "month": 9, "day": 15, "hour": 14, "minute": 0}},
        "locations": {
            "boi_canh_chinh": {
                "name": "Không gian hiện tại",
                "parent": None,
                "desc": scen_filled or f"Không gian trò chuyện trực tiếp giữa {user_name} và {cname}.",
                "tags": ["indoor", "private_possible"],
                "travel_min": 0,
                "lore_ids": [],
            },
            "phong_rieng": {
                "name": "Phòng riêng",
                "parent": "boi_canh_chinh",
                "desc": "Không gian riêng tư, kín đáo của hai người.",
                "tags": ["indoor", "private_possible"],
                "travel_min": 2,
                "lore_ids": [],
            },
            "ben_ngoai": {
                "name": "Bên ngoài / Đường phố",
                "parent": "boi_canh_chinh",
                "desc": "Không gian mở bên ngoài.",
                "tags": ["public"],
                "travel_min": 5,
                "lore_ids": [],
            },
            "quan_nuoc": {
                "name": "Quán cà phê / Quán ăn",
                "parent": "boi_canh_chinh",
                "desc": "Quán nước công cộng.",
                "tags": ["indoor", "public"],
                "travel_min": 5,
                "lore_ids": [],
            },
        },
        "lore": world_lore,
        "characters": {cid: char_v2},
        "cast": [
            {
                "char_id": cid,
                "role_in_world": char_v2["core"].get("one_liner") or cname,
                "start_location": "boi_canh_chinh",
                "start_outfit": "default",
                "goal": "",
            }
        ],
        "relationships": {
            "auto_bootstrap": False,
            "edges": {
                cid: {
                    "user": {
                        "tag": "bạn trò chuyện 1-1",
                        "level": 65,
                        "default": {"self": c_self, "call": c_call, "ref_3rd": user_name},
                        "overrides": c_ov,
                    }
                },
                "user": {
                    cid: {
                        "tag": "đối phương",
                        "level": 65,
                        "default": u_pair,
                        "overrides": {},
                    }
                },
            },
        },
        "initial_facts": [],
        "opening": {
            "user_start_location": "boi_canh_chinh",
            "tension": 1,
            "channel_type": "in_person",
            "narration": opening_nar,
        },
    }
    pack, _ = load_world(world_dict)
    pack["single_meta"] = {
        "char_id": cid,
        "greetings": greetings,
        "greeting_idx": g_idx,
        "card_lore_count": len(world_lore),
    }
    return pack
