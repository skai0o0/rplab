"""Vietnamese Decision-Driven Group RP Engine (v2 — 5 Pha Neuro-Symbolic).

Tang 1 — State Machine (Python Deterministic): Clock, Spatial Tree, Graph, known_by, Context 4 Zone.
Tang 2 — Sensors & Gate (DecisionProvider): OpenRouter /api/alpha/decisions (noul / choice / score).
Tang 3 — Unbounded Extractors (extract_json): Chi goi khi explicit_time / new_place / mem_worthy / bootstrap.
Tang 4 — Renderer (Writer LLM): Nhan prompt 4 Zone toi uu KV Cache & Recency Bias.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import re
import time
import urllib.error
import urllib.request

import lore as lore_engine
import prompts
import rpformat

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

NARRATOR = "nguoi_dan_truyen"
SCENE_SPEAKER = "toan_canh"
GROUP = "ca_nhom"
MINOR_NPC = "minor_npc_or_env"

# 3 vung xac suat & hysteresis (Guardrail #2)
CONF_HIGH = 0.80
CONF_LOW = 0.25

CHAIN_THRESHOLD = 0.60
ARRIVE_THRESHOLD = 0.50
OWED_THRESHOLD = 0.20
ADDRESS_CONF = 0.55
QUIET_TURNS = 3
QUIET_THRESHOLD = 0.35
MODULE_THRESHOLD = 0.38
MAX_Z4_LORE_ENTRIES = 8
MAX_Z4_LORE_CHARS = 4200

# Sawtooth Working Memory & 2-tier Scene Summaries
SAWTOOTH_MIN = 8
SAWTOOTH_MAX = 16
SCENE_MIN_SWITCH = 4
MAX_RECENT_SCENES = 5
RECENT_TURNS = SAWTOOTH_MAX  # tuong thich nguoc

TENSION = ["Rất thoải mái", "Bình thường", "Hơi căng", "Căng thẳng", "Rất căng"]
SCENE_VIBES = {
    "relaxed": "Thư giãn / Đời thường",
    "lively": "Sôi nổi / Tung hứng",
    "awkward": "Ngượng ngùng / Bối rối",
    "sensual": "Ám muội / Căng thẳng ngầm",
    "dramatic": "Kịch tính / Thế khó (Bắt quả tang)",
    "conflict": "Xung đột / Đối đầu gay gắt",
    "intimate": "Cao trào Thể xác / Nhục cảm (NSFW)",
    "shock": "Chấn động / Biến cố đột ngột",
}
SCENE_VIBE_CRITERIA = {
    "relaxed": "Thư giãn / Đời thường: trò chuyện sinh hoạt bình thường, chưa có va chạm lớn",
    "lively": "Sôi nổi / Tung hứng: cà khịa, trêu chọc, tranh luận sôi nổi, giàu năng lượng",
    "awkward": "Ngượng ngùng / Bối rối: lỡ lời, lộ thính, xấu hổ hoặc tình huống khó xử nhẹ",
    "sensual": "Ám muội / Căng thẳng ngầm: đụng chạm gần, ôm ấp, ánh mắt khiêu khích, lực hút giới tính",
    "dramatic": "Kịch tính / Thế khó: bị bắt quả tang, người thứ ba bước vào lúc nhạy cảm, suýt lộ bí mật",
    "conflict": "Xung đột / Đối đầu gay gắt: cãi vã, giận dữ thật sự, tổn thương lòng tự trọng, ghen tuông căng",
    "intimate": "Cao trào Thể xác / Nhục cảm (NSFW): hôn sâu, đè lên người, sờ chạm cơ thể trực tiếp hoặc làm tình",
    "shock": "Chấn động / Biến cố đột ngột: tin sét đánh hoặc hành động gây sốc làm đảo lộn hoàn toàn không khí",
}
SCENE_VIBE_DIRECTIVES = {
    "relaxed": "Nhịp sinh hoạt tự nhiên nhưng không đều đều nhạt nhẽo; luôn cài cắm một cử chỉ, ánh nhìn hoặc câu nói gợi mở tạo đà đẩy tương tác lên nấc kịch tính hơn.",
    "lively": "Đẩy nhanh nhịp đối thoại tung hứng, tăng độ sắc sảo, cà khịa, trêu chọc hoặc phản ứng lanh lẹ; tạo ma sát thú vị giữa các nhân vật.",
    "awkward": "Không khí ngượng ngùng, lúng túng; nhấn mạnh ánh mắt né tránh, nhịp thở khựng lại, khoảng cách cơ thể quá gần hoặc lời nói lắp bắp/nói cứng để che giấu sự bối rối.",
    "sensual": "Không khí ám muội, đặc quánh lực hút giới tính hoặc tình cảm ngầm; tập trung miêu tả khoảng cách cơ thể sát rạt, hơi ấm, ánh mắt dừng lâu trên môi/da thịt và những câu thoại mang ẩn ý khiêu khích.",
    "dramatic": "Tình thế kịch tính cao độ (bị đặt vào thế khó, bắt quả tang, suýt lộ bí mật hoặc có người thứ ba chứng kiến); đẩy mạnh áp lực tâm lý, sự giằng xé, phản ứng gấp gáp để chữa cháy hoặc đối phó ngay tại hiện trường.",
    "conflict": "Xung đột và đối đầu trực diện; tự ái, ghen tuông hoặc giận dữ bùng lên thành lời lẽ sắc lạnh, thái độ gay gắt, không nhượng bộ dễ dàng.",
    "intimate": "Cao trào va chạm thể xác và nhục cảm mãnh liệt; cuốn nhân vật vào phản ứng sinh lý chân thực (nhịp thở đứt quãng, da thịt nóng rực, run rẩy, tiếng rên nghẹn và sự giằng xé giữa vỏ bọc lý trí với khoái cảm/xấu hổ), bỏ qua mọi việc vặt ngoài lề.",
    "shock": "Biến cố bất ngờ gây chấn động mạnh; phá vỡ hoàn toàn vẻ điềm tĩnh thường ngày, buộc nhân vật sững sờ, hoảng hốt hoặc bật ra phản ứng bản năng tức thì.",
}
VIBE_TO_TENSION = {
    "relaxed": 0,
    "lively": 1,
    "awkward": 2,
    "sensual": 2,
    "dramatic": 3,
    "conflict": 4,
    "intimate": 3,
    "shock": 3,
}
TENSION_TO_VIBE = {0: "relaxed", 1: "lively", 2: "awkward", 3: "dramatic", 4: "conflict"}
MOODS = {
    "normal": "Bình thường, ổn định",
    "happy": "Vui vẻ, hào hứng, thoải mái",
    "anxious": "Lo lắng, bồn chồn, chột dạ",
    "angry": "Bực bội, cáu gắt, căng thẳng",
    "sad": "Buồn bã, trầm lắng, thất vọng",
}
TURN_TYPES = {
    "thoai": "Đối thoại là chính",
    "hanh_dong": "Hành động là chính",
    "mo_ta": "Mô tả cảnh, không khí",
    "chuyen_canh": "Chuyển cảnh: người chơi vừa đề nghị hoặc bắt đầu đi chỗ khác, hoặc muốn nhảy thời gian",
}
MOVE_STATUSES = {
    "stay": "Đứng yên tại chỗ hiện tại",
    "plan_only": "Chỉ mới bàn bạc, rủ rê hoặc hẹn đi vào lúc khác (chưa đi ngay)",
    "in_transit": "Đang trên đường di chuyển (vừa đi vừa nói chuyện, chưa tới đích)",
    "arrived": "Thực sự bước tới địa điểm mới ngay trong lượt này",
}
CHANNEL_TYPES = {
    "in_person": "Trò chuyện trực tiếp mặt đối mặt tại cùng địa điểm",
    "dm_text": "Nhắn tin riêng 1-1 qua điện thoại (Zalo/Messenger) — người khác ở cạnh không đọc được",
    "group_text": "Nhắn tin trong nhóm chat chung qua điện thoại (Zalo/Messenger)",
}
REL_SHIFTS = {
    "unchanged": "Không thay đổi rõ rệt",
    "warmer": "Thân thiết hơn, cởi mở hoặc cảm kích rõ rệt",
    "colder": "Xa cách hơn, hụt hẫng hoặc mất thiện cảm rõ rệt",
    "tense": "Căng thẳng, khó chịu tức thời",
}
DIALOGUE_TIME_DELTAS = {
    "instant": rpformat.TIME_DELTA_LABELS["instant"],
    "dialogue": rpformat.TIME_DELTA_LABELS["dialogue"],
    "brief": rpformat.TIME_DELTA_LABELS["brief"],
    "short": rpformat.TIME_DELTA_LABELS["short"],
}


def _post(url, api_key, body, timeout=60):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "RP Lab"},
    )
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


# ----------------------------------------------------------------------------- Tang 2 & 3 Abstraction
class DecisionProvider:
    """Lop truu tuong cho Decision API (noul / choice / score)."""

    def __init__(self, api_key, model="microsoft/microsoft-decision-1", url=DECISIONS_URL):
        self.api_key = api_key
        self.model = model
        self.url = url

    def ask(self, state_dict, questions_dict):
        t = time.time()
        d = _post(self.url, self.api_key, {"model": self.model, "state": state_dict, "questions": questions_dict})
        return {
            "answers": d.get("answers") or {},
            "cost": (d.get("usage") or {}).get("cost") or 0,
            "model": d.get("model", self.model),
            "secs": round(time.time() - t, 2),
        }


def _strip_json_fences(txt):
    s = (txt or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.I)
    if m:
        s = m.group(1).strip()
    elif "```" in s:
        # Truong hop block ```json bi cat cut nen khong co dau ``` dong
        s = re.sub(r"^[\s\S]*?```(?:json)?\s*", "", s, count=1, flags=re.I).strip()
    idx = s.find("{")
    if idx > 0:
        s = s[idx:]
    return s


def _balance_brackets(snippet):
    """Tinh chuoi ngoac dong con thieu ben ngoai chuoi string."""
    stack, in_str, esc = [], False, False
    for ch in snippet:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return stack, in_str


def _parse_or_repair_json(raw_text):
    """Parse JSON voi kha nang tu sua loi trailing comma, thieu dau phay va cat cut do max_tokens."""
    cleaned = _strip_json_fences(raw_text)
    if not cleaned:
        return {}
    # 1. Thu parse truc tiep va xoa trailing comma
    candidates = [
        cleaned,
        re.sub(r",\s*([}\]])", r"\1", cleaned),
    ]
    # Thu sua loi thieu dau phay giua hai dong JSON ("val"\n"next_key": hoac }\n"next_key":)
    comma_fixed = re.sub(r'([}\]"0-9]|true|false|null)[ \t]*\r?\n[ \t]*(")', r"\1,\n\2", candidates[1])
    candidates.append(comma_fixed)

    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass

    # 2. Neu bi cat cut giua chung: thu cat lui ve cac moc '}', ']', ',' hoac dong ngoac con thieu
    base = candidates[-1]
    cut_points = [len(base)]
    in_str, esc = False, False
    for i, ch in enumerate(base):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "}]":
            cut_points.append(i + 1)
        elif ch == ",":
            cut_points.append(i)

    # Thu tu vi tri xa nhat lui dan ve phia truoc
    for pos in reversed(cut_points):
        sub = base[:pos].rstrip(" \t\r\n,:")
        if not sub:
            continue
        stack, still_str = _balance_brackets(sub)
        if still_str:
            sub += '"'
        sub = re.sub(r",\s*([}\]])", r"\1", sub)
        trial = sub + "".join(reversed(stack))
        try:
            obj = json.loads(trial)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("Không thể phục hồi JSON bị lỗi cú pháp hoặc cắt cụt", cleaned, len(cleaned))


def extract_json(api_key, model, system_prompt, user_text, max_tokens=500):
    """Tang 3 — Small JSON LLM goi co dieu kien (explicit_time, new_place, mem_worthy, bootstrap)."""
    if not api_key:
        raise RuntimeError("Thiếu OPENROUTER_API_KEY để gọi Extractor")
    use_model = model if (model and model != "cloudflare/clairic") else "deepseek/deepseek-v4.1-flash-1017"
    body = {
        "model": use_model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt + "\nChỉ trả về JSON minified hợp lệ trên 1 dòng (không markdown, không thụt dòng, không giải thích).",
            },
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False},
        "usage": {"include": True},
    }
    res = _post(OPENROUTER_CHAT_URL, api_key, body, timeout=60)
    raw = (((res.get("choices") or [{}])[0].get("message") or {}).get("content")) or "{}"
    parsed = _parse_or_repair_json(raw)
    return parsed, float((res.get("usage") or {}).get("cost") or 0)


def bootstrap_relationships_llm(api_key, model, pack, state, force_refresh=False):
    """Turn 0 Bootstrap (tuy chon LLM): sinh ma tran xung ho bat doi xung theo lo an toan (khong cat cut)."""
    chars = pack.get("characters") or {}
    if not chars:
        return state, 0.0
    new_state = copy.deepcopy(state)
    cur_edges = new_state.setdefault("relationships", {}).setdefault("edges", {})
    raw_world_edges = (pack.get("relationships") or {}).get("edges") or {}

    all_pairs = [(s, t) for s in cur_edges for t in (cur_edges[s] or {}) if s != "user"]
    missing_or_generic = [
        (s, t) for (s, t) in all_pairs
        if (cur_edges[s][t].get("tag") in ("", "quen biết") or not (cur_edges[s][t].get("default") or {}).get("self"))
        and t not in (raw_world_edges.get(s) or {})
        and (cur_edges[s][t] or {}).get("lock") not in ("manual", "observed")
    ]

    if force_refresh:
        llm_pairs = [
            (s, t) for (s, t) in all_pairs
            if (cur_edges[s][t] or {}).get("source") == "llm"
            and (cur_edges[s][t] or {}).get("lock") not in ("manual", "observed")
            and (s, t) not in missing_or_generic
            and (
                t not in (raw_world_edges.get(s) or {})
                or ((raw_world_edges.get(s) or {}).get(t) or {}).get("source") == "llm"
            )
        ]
        pairs_to_gen = missing_or_generic + llm_pairs
    else:
        pairs_to_gen = missing_or_generic

    if not pairs_to_gen:
        return new_state, 0.0

    cast_brief = "\n".join(
        f"- {cid} ({c['meta']['name']}, {c['meta'].get('age', '?')}t, giới tính: {c['meta'].get('gender') or 'không rõ'}): {c['core']['one_liner']} | vai trò: {e.get('role_in_world', '')} | "
        f"giọng: {c['voice'].get('dialect', '')}"
        for e in pack.get("cast") or []
        if (cid := e["char_id"]) in chars and (c := chars[cid])
    )
    sys_p = prompts.get_text("commands.bootstrap_rel_sys")

    user_g = rpformat.gender_of(pack, "user")
    user_g_lbl = "nam" if user_g == "nam" else ("nữ" if user_g == "nu" else "không rõ")
    total_cost = 0.0
    batch_size = 8
    for i in range(0, len(pairs_to_gen), batch_size):
        batch = pairs_to_gen[i:i + batch_size]
        pair_desc = ", ".join(f"{s}->{t}" for s, t in batch)
        sample_cur = {}
        for s, t in batch:
            sample_cur.setdefault(s, {})[t] = cur_edges[s][t]
        user_p = (
            f"Người chơi: id='user', tên='{pack['user']['name']}', giới tính='{user_g_lbl}', persona='{pack['user'].get('persona', '')}'.\n"
            f"Nhân vật:\n{cast_brief}\n\n"
            f"Các cặp cần tạo/làm giàu: {pair_desc}\n"
            f"Tham khảo hiện tại: {json.dumps(sample_cur, ensure_ascii=False, separators=(',', ':'))}"
        )
        try:
            data, cost = extract_json(api_key, model, sys_p, user_p, max_tokens=2500)
            total_cost += cost
        except Exception:
            # Giu nguyen canh deterministic neu 1 lo gap su co
            continue

        gen_edges = data.get("edges") if isinstance(data, dict) else {}
        if not isinstance(gen_edges, dict):
            continue
        for s, t in batch:
            edge = (gen_edges.get(s) or {}).get(t) if isinstance(gen_edges.get(s), dict) else None
            raw_edge = (raw_world_edges.get(s) or {}).get(t)
            if (cur_edges[s][t] or {}).get("lock") in ("manual", "observed"):
                continue
            if t in (raw_world_edges.get(s) or {}) and (raw_edge or {}).get("source") != "llm":
                continue
            if isinstance(edge, dict) and isinstance(edge.get("default"), dict):
                old = cur_edges[s][t]
                tgt_g = rpformat.gender_of(pack, t)
                tgt_short = pack["user"]["name"] if t == "user" else (chars.get(t, {}).get("meta", {}).get("name") or t).split()[-1]
                raw_ov = edge.get("overrides") if isinstance(edge.get("overrides"), dict) else old.get("overrides", {})
                norm_ov = {}
                for ok, ov_pair in (raw_ov or {}).items():
                    if isinstance(ov_pair, dict):
                        norm_ov[ok] = {
                            **ov_pair,
                            "call": rpformat.adapt_call(ov_pair.get("call") or old["default"].get("call", "bạn"), tgt_g, tgt_short),
                        }
                cur_edges[s][t] = {
                    "tag": edge.get("tag") or old.get("tag", "quen biết"),
                    "level": max(0, min(100, int(edge.get("level") if edge.get("level") is not None else old.get("level", 50)))),
                    "source": "llm",
                    "lock": None,
                    "default": {
                        "self": edge["default"].get("self") or old["default"].get("self", "tui"),
                        "call": rpformat.adapt_call(edge["default"].get("call") or old["default"].get("call", "bạn"), tgt_g, tgt_short),
                        "ref_3rd": edge["default"].get("ref_3rd") or old["default"].get("ref_3rd", tgt_short),
                    },
                    "overrides": norm_ov,
                }
    return new_state, total_cost


# ----------------------------------------------------------------------------- Helpers
def cast_by_id(pack):
    """Map char_id -> dict tom tat thong nhat (tuong thich ca code cu)."""
    chars = pack.get("characters") or {}
    cast_map = {c["char_id"]: c for c in (pack.get("cast") or [])}
    out = {}
    for cid, v2 in chars.items():
        c_entry = cast_map.get(cid) or {}
        out[cid] = {
            "id": cid,
            "name": v2["meta"]["name"],
            "short": v2["core"]["one_liner"],
            "aliases": v2["meta"].get("aliases") or [v2["meta"]["name"].split()[-1]],
            "age": v2["meta"].get("age"),
            "role_in_world": c_entry.get("role_in_world", ""),
            "goal": c_entry.get("goal", ""),
            "v2": v2,
        }
    return out


def user_name_of(pack):
    u = pack.get("user")
    return u["name"] if isinstance(u, dict) else (u or "User")


def name_of(pack, cid):
    if cid == NARRATOR:
        return "Người dẫn truyện"
    if cid == SCENE_SPEAKER:
        return "Cả cảnh"
    if cid == "user":
        return user_name_of(pack)
    c = (pack.get("characters") or {}).get(cid)
    return c["meta"]["name"] if c else cid


def rp_history(history):
    """Chỉ giữ các lượt hội thoại RP thật, loại bỏ hoàn toàn tin nhắn lệnh ngoài băng (in_context=False / speaker='command')."""
    return [m for m in (history or []) if m.get("in_context", True) and m.get("speaker") != "command"]


def focus_target(history, speaker):
    """Người mà speaker đang đáp lời lượt này (tin gần nhất không phải của chính speaker/người dẫn truyện)."""
    for m in reversed(rp_history(history)):
        s = m.get("speaker")
        if s and s not in (speaker, NARRATOR, SCENE_SPEAKER):
            return s
    return "user"


def is_pronoun_locked(state, char_id):
    edges = ((state.get("relationships") or {}).get("edges") or {})
    c2u = (edges.get(char_id) or {}).get("user") or {}
    u2c = (edges.get("user") or {}).get(char_id) or {}
    return bool(c2u.get("lock") in ("manual", "observed") or u2c.get("lock") in ("manual", "observed"))


def should_reciprocate(pack, state, char_id):
    for edges_src in (((state.get("relationships") or {}).get("edges") or {}), ((pack.get("relationships") or {}).get("edges") or {})):
        e = (edges_src.get(char_id) or {}).get("user") or {}
        if e.get("reciprocate") is False:
            return False
    cast_entry = next((x for x in (pack.get("cast") or []) if x.get("char_id") == char_id), {})
    if cast_entry.get("reciprocate") is False:
        return False
    c_v2 = (pack.get("characters") or {}).get(char_id) or {}
    if c_v2.get("reciprocate") is False or (c_v2.get("voice") or {}).get("reciprocate") is False:
        return False
    return True


_ROLE_TITLES_RE = re.compile(
    r"(?i)\b(?:lớp trưởng|lớp phó|bí thư|chủ trọ|hội trưởng|trưởng nhóm|đàn chị|đàn anh|thủ quỹ|bác bảo vệ|cô giáo|thầy giáo)\b"
)
_REMOTE_SIGNAL_RE = re.compile(
    r"(?i)(?:\*[^*]*(?:nhắn|gọi|điện thoại|zalo|mess|inbox|bấm máy)[^*]*\*|"
    r"\b(?:nhắn tin|nhắn riêng|nhắn cho|nhắn vô|nhắn vào|nhắn trong|gửi tin|inbox|ib|zalo|messenger|call|facetime|"
    r"điện thoại|gọi điện|gọi cho|alo|bắt máy|nhấc máy|nghe máy|đầu dây|cuộc gọi)\b)"
)
_VOICE_CALL_RE = re.compile(
    r"(?i)\b(?:điện thoại|gọi điện|gọi cho|alo|bắt máy|nhấc máy|nghe máy|đầu dây|cuộc gọi|loa ngoài|bật loa|facetime)\b"
)
_PLAN_ONLY_TIME_RE = re.compile(
    r"(?i)\b(?:lát|lát nữa|chiều nay|tối nay|mai|ngày mai|tuần sau|bữa nào|hôm nào|mốt|xíu nữa|tí nữa)\b"
)
_IMMEDIATE_MOVE_RE = re.compile(
    r"(?i)\b(?:liền|ngay|bây giờ|lập tức|đứng dậy|xách|lấy xe|lên đường|xuống thôi|đi thôi|đi luôn)\b"
)
_NAR_MOVE_RE = re.compile(r"(?i)\*[^*]*(?:bước|đi|xuống|lên|ra|chạy|vào|ghé|chở)[^*]*\*")
_SCENE_SKIP_OUTSIDE_RE = re.compile(
    r"(?i)\b(?:tua|sau đó|hôm sau|sáng mai|chiều hôm đó|tiếng sau|lát sau|đến giờ|trôi qua|sang ngày|tối hôm đó|nửa tiếng|một tiếng|hai tiếng)\b"
)
_MOVE_ALONE_RE = re.compile(
    r"(?i)\b(?:ngồi đây|ở đây đợi|đợi ở đây|đợi tui|đợi mình|đợi tớ|đợi anh|đợi em|chờ tui|chờ ở đây|đi một mình|xíu quay lại|tí quay lại|lát quay lại|ở lại nhé|ở lại nha|ở nhà nhé|ở nhà nha)\b"
)
_GROUP_ADDR_RE = re.compile(
    r"(?i)\b(?:mấy bà|mấy ông|mấy đứa|mấy người|hai bà|hai ông|ba bà|cả nhóm|mọi người|các bạn|các cậu|tụi bay|chúng mày|mấy chị|mấy anh|cả nhà)\b"
)
_IN_TRANSIT_RE = re.compile(
    r"(?i)(?:vừa đi vừa|trên đường|đang chở|lấy xe chở|chở .{1,25} chạy|bám chắc|ngồi sau xe)"
)
_AT_DOOR_RE = re.compile(
    r"(?i)(?:gõ cửa|tiếng gõ cửa|đứng ngoài cửa|ngoài cửa vọng vào|vọng vào từ ngoài|chưa vào phòng|ngoài hành lang gọi)"
)
_DOOR_OPEN_RE = re.compile(r"(?i)(?:mở cửa|mời vào|bước vào phòng|kéo cửa ra|đẩy cửa vào)")
_DEICTIC_PREV_RE = re.compile(r"(?i)\b(?:bà kia|ông kia|cô kia|anh kia|chị kia|người vừa nói|bà đó|ông đó)\b")
_TIME_MAI_RE = re.compile(r"(?i)\b(?:ngày|sáng|trưa|chiều|tối|đêm|hôm|nay|mốt)\s+mai\b|\bmai\s+mốt\b")
_ARRIVAL_NAR_RE = re.compile(
    r"(?i)(?:\b(?:bước vào|đi vào|chạy vào|lách vào|xông vào|kéo vào|dẫn vào|"
    r"đẩy cửa|mở cửa|gõ cửa|xuất hiện|bắt gặp|chạm mặt|bước tới|đi tới|chạy tới|chạy lại|lại gần|"
    r"ghé qua|ghé vào|ghé tới|vừa tới|vừa đến|đến nơi|tới nơi|có mặt|đứng ngay|ngồi xuống|"
    r"đến nhìn|tới nhìn|đến gặp|tới gặp|đi ngang|quay lại thấy|nhìn thấy|bắt đầu bước)\b|"
    r"(?:lát sau|chút sau|lúc sau|ngay lúc đó|vừa lúc đó)[^.!?]{0,40}\b(?:đến|tới|vào|lại)\b)"
)
_AT_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-zÀ-ỹ0-9_]+)")
_INTIMATE_VIBE_RE = re.compile(
    r"(?i)(?:cặc|lồn|đụ|địt|nắc|bú|liếm|đầu ti|núm vú|lột đồ|cởi đồ|cởi quần|kéo cạp quần|đè ngửa|hôn môi|nút lưỡi|ôm eo|ngồi lên đùi|thúc mạnh|sờ ngực|bóp vú|bóp mông|vén áo)"
)
_SENSUAL_VIBE_RE = re.compile(
    r"(?i)\b(?:ôm lấy|kéo vào lòng|áp sát|hôn nhẹ|vuốt má|ghé sát tai|thì thầm|vuốt ve|ôm chặt|chạm môi)\b"
)


def _char_mention_names(char):
    """Tong hop cac bien the ten goi, biet danh, 2 tu cuoi, ten rieng va chuc danh cua nhan vat."""
    if not isinstance(char, dict):
        return []
    raw_names = []
    for a in (char.get("aliases") or []):
        if a:
            raw_names.append(str(a).strip())
    full_name = str(char.get("name") or "").strip()
    if full_name:
        raw_names.append(full_name)
        parts = full_name.split()
        if len(parts) >= 2:
            raw_names.append(" ".join(parts[-2:]))
        if len(parts[-1]) >= 2:
            raw_names.append(parts[-1])
    elif raw_names:
        last_tok = raw_names[0].split()[-1]
        if len(last_tok) >= 2:
            raw_names.append(last_tok)
    role_txt = char.get("role_in_world") or ""
    for m in _ROLE_TITLES_RE.findall(role_txt):
        if m:
            raw_names.append(m.strip())
    cid = str(char.get("id") or "").strip()
    if cid:
        raw_names.append(cid)
    out = []
    seen_low = set()
    for n in raw_names:
        low = n.lower()
        if n and low not in seen_low:
            seen_low.add(low)
            out.append(n)
    return out


def _normalize_mention_text(text):
    """Chuyen @Ten_Dem thanh khoang trang giu nguyen do dai de khop duoc ca @Gia_Linh lan Gia Linh."""
    s = str(text or "")
    return _AT_MENTION_RE.sub(lambda m: " " + m.group(1).replace("_", " "), s)


def _explicit_at_mentions(cast, text):
    """Bat cac the @Ten / @Ten_Dem / @char_id do nguoi choi go truc tiep."""
    out = []
    seen_ids = set()
    for raw_tok in _AT_MENTION_RE.findall(text or ""):
        tok_raw = raw_tok.strip()
        tok_space = tok_raw.replace("_", " ").strip()
        low_raw = tok_raw.lower()
        low_space = tok_space.lower()
        for cid, ch in (cast or {}).items():
            if cid in seen_ids:
                continue
            if cid.lower() == low_raw:
                seen_ids.add(cid)
                out.append(cid)
                break
            names_low = [n.lower() for n in _char_mention_names(ch)]
            if low_space in names_low or low_raw in names_low:
                seen_ids.add(cid)
                out.append(cid)
                break
    return out


def _search_mention_text(norm_text, token, mask_mai=None):
    target = mask_mai if (token.lower() == "mai" and mask_mai is not None) else norm_text
    return re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", target, re.I)


def mentioned(char, text):
    """Khop nguyen tu theo ten / aliases / chuc danh / @tag trong doan van (Case #4)."""
    if not text:
        return False
    norm = _normalize_mention_text(text)
    mask_mai = _TIME_MAI_RE.sub(lambda m: " " * len(m.group(0)), norm)
    return any(_search_mention_text(norm, n, mask_mai) is not None for n in _char_mention_names(char))


def _first_mention_pos(char, text):
    if not text:
        return None
    norm = _normalize_mention_text(text)
    mask_mai = _TIME_MAI_RE.sub(lambda m: " " * len(m.group(0)), norm)
    positions = []
    for n in _char_mention_names(char):
        m = _search_mention_text(norm, n, mask_mai)
        if m:
            positions.append(m.start())
    return min(positions) if positions else None


def _has_arrival_narration(char, text):
    """True neu phan dan truyen/hanh dong (ngoai ngoac kep) mo ta nhan vat vua toi hoac duoc bat gap."""
    outside = _strip_dialogue_quotes(text)
    if not outside:
        return False
    return mentioned(char, outside) and bool(_ARRIVAL_NAR_RE.search(outside))


def recent_chain_speakers(history):
    """Case #16/#26: Danh sach speaker != 'user' lien tiep o cuoi history sau tin 'user' gan nhat."""
    out = []
    for m in reversed(rp_history(history)):
        sp = m.get("speaker")
        if sp == "user":
            break
        if sp and sp != NARRATOR:
            out.append(sp)
    out.reverse()
    return out


def _has_remote_signal(text):
    return bool(_REMOTE_SIGNAL_RE.search(text or ""))


def detect_comm_medium(text, ch_type="in_person"):
    """Case #13: Phan biet spoken (truc tiep / goi dien) vs texting (nhan tin)."""
    s = text or ""
    is_call = bool(_VOICE_CALL_RE.search(s))
    speakerphone = bool(is_call and re.search(r"(?i)\b(?:loa ngoài|bật loa)\b", s))
    medium = "spoken" if (is_call or ch_type == "in_person") else "texting"
    return {"medium": medium, "is_voice_call": is_call, "speakerphone": speakerphone}


def _is_pure_dialogue_time_mention(text):
    """Case #15: True neu moc gio chi nam trong loi thoai ngoac kep, ben ngoai khong co lenh tua canh."""
    s = str(text or "")
    quotes = re.findall(r'["“]([^"”]+)["”]', s)
    if not quotes:
        return False
    outside = re.sub(r'["“][^"”]*["”]', " ", s)
    if _SCENE_SKIP_OUTSIDE_RE.search(outside):
        return False
    q_txt = " ".join(quotes)
    return bool(re.search(r"(?i)\b(?:\d{1,2}\s*(?:h|giờ|:)\s*\d{0,2}|sáng mai|tối qua|hôm qua|hôm nay|chiều nay|ngày mai)\b", q_txt))


def _is_plan_only_move(text):
    s = str(text or "")
    if _NAR_MOVE_RE.search(s) or _IMMEDIATE_MOVE_RE.search(s):
        return False
    return bool(_PLAN_ONLY_TIME_RE.search(s))


def _user_moves_alone(text):
    return bool(_MOVE_ALONE_RE.search(text or ""))


def _is_group_address_text(text):
    return bool(_GROUP_ADDR_RE.search(text or ""))


def _is_short_passive_turn(text):
    """Case #28: Tin ngan/thu dong/bo ngo de nhan vat tu dan dat tiep."""
    s = str(text or "").strip()
    if not s or "?" in s:
        return False
    words = s.split()
    if len(words) > 8:
        return False
    no_nar = re.sub(r"\*[^*]*\*", " ", s).strip()
    cleaned = re.sub(r"[^\w\sÀ-ỹ]", "", no_nar.lower()).strip()
    return (not cleaned and len(words) <= 8) or cleaned in {
        "ờ", "ừ", "ừm", "à", "vâng", "dạ", "ok", "okay", "uk", "uh", "im lặng", "gật đầu", "cười trừ"
    }


def _is_in_transit_text(text):
    return bool(_IN_TRANSIT_RE.search(text or ""))


def _is_at_door_text(text):
    return bool(_AT_DOOR_RE.search(text or ""))


def _is_door_opened_text(text):
    s = str(text or "")
    outside = re.sub(r'["“][^"”]*["”]', " ", s)
    if re.search(r"(?i)(?:mời vào|bước vào phòng|kéo cửa|đẩy cửa)", outside):
        return True
    if re.search(r"(?i)\bmở cửa\b", outside) and not re.search(r"(?i)(?:kêu|bảo|nhờ|đòi|xin)\s+mở cửa", outside):
        return True
    return bool(re.search(r"(?i)\b(?:mời vào|vào đi|vào phòng đi)\b", s))


def _has_deictic_prev_ref(text):
    return bool(_DEICTIC_PREV_RE.search(text or ""))


_NARRATION_CLOCK_RE = re.compile(
    r"(?i)\b(\d{1,2})(?:\s*(?:h|giờ)\s*(\d{1,2})?(?:\s*(?:phút|p)\b)?|:\s*(\d{2}))"
    r"(?:\s*(rạng sáng|sáng|trưa|chiều|tối|đêm|khuya|am|pm)\b)?"
)
_CLOCK_PERIOD_RULES = (
    (re.compile(r"(?i)\bnửa đêm\b"), 0, 0, (23, 0, 1)),
    (re.compile(r"(?i)\b(?:đêm khuya|khuya muộn|giữa đêm)\b"), 23, 0, (22, 23, 0, 1, 2, 3)),
    (re.compile(r"(?i)\brạng sáng\b"), 4, 30, (3, 4, 5)),
    (re.compile(r"(?i)\bsáng sớm\b"), 6, 30, (5, 6, 7)),
    (re.compile(r"(?i)\bbuổi sáng\b"), 8, 30, (6, 7, 8, 9, 10)),
    (re.compile(r"(?i)\b(?:giữa trưa|buổi trưa)\b"), 12, 0, (11, 12, 13)),
    (re.compile(r"(?i)\b(?:buổi chiều|xế chiều)\b"), 15, 30, (14, 15, 16, 17)),
    (re.compile(r"(?i)\b(?:hoàng hôn|chập tối)\b"), 18, 0, (17, 18, 19)),
    (re.compile(r"(?i)\b(?:buổi tối|tối muộn)\b"), 20, 30, (19, 20, 21, 22)),
)
_SCENE_PLACE_RE = re.compile(
    r"(?i)\b("
    r"phòng trọ|phòng ngủ|phòng khách|phòng tắm|phòng bếp|ký túc xá|ktx|căn hộ|chung cư|"
    r"quán cà phê|quán cafe|quán bar|quán nhậu|quán ăn|nhà hàng|"
    r"lớp học|giảng đường|thư viện|căn tin|sân trường|"
    r"văn phòng|công ty|bệnh viện|khách sạn|nhà nghỉ|"
    r"sân thượng|ban công|hành lang|bãi biển|công viên|rạp phim|siêu thị|"
    r"nhà riêng|nhà bếp"
    r")\b"
)
_PRIVATE_PLACE_RE = re.compile(r"(?i)(?:phòng|nhà riêng|nhà bếp|căn hộ|chung cư|khách sạn|nhà nghỉ|ktx|ký túc)")


def _strip_dialogue_quotes(text):
    return re.sub(r'["“][^"”]*["”]', " ", str(text or "")).strip()


def _detect_narration_clock(text, cur_clock, is_opening=False):
    outside = _strip_dialogue_quotes(text)
    if not outside:
        return None
    cur_h = int((cur_clock or {}).get("hour", 14))
    cur_m = int((cur_clock or {}).get("minute", 0))

    for m in _NARRATION_CLOCK_RE.finditer(outside):
        after = outside[m.end():m.end() + 14].lstrip().lower()
        if re.match(r"^(?:sau|trước|nữa|đồng hồ|liền|qua)\b", after):
            continue
        h = int(m.group(1))
        min_str = m.group(2) if m.group(2) is not None else m.group(3)
        minute = int(min_str) if min_str is not None else 0
        if not (0 <= h <= 23 and 0 <= minute <= 59):
            continue
        period = (m.group(4) or "").lower()
        if period in ("chiều", "tối", "đêm", "khuya", "pm"):
            if 1 <= h <= 11:
                h += 12
            elif h == 12 and period in ("đêm", "khuya"):
                h = 0
        elif period in ("sáng", "rạng sáng", "am"):
            if h == 12:
                h = 0
        if (h, minute) != (cur_h, cur_m):
            return {"hour": h, "minute": minute, "day_offset": 0, "allow_rewind": bool(is_opening)}
        return None

    for rx, h, minute, ok_hours in _CLOCK_PERIOD_RULES:
        if rx.search(outside):
            if is_opening or cur_h not in ok_hours:
                return {"hour": h, "minute": minute, "day_offset": 0, "allow_rewind": bool(is_opening)}
            return None
    return None


def _detect_scene_place(text):
    outside = _strip_dialogue_quotes(text) or str(text or "")
    m = _SCENE_PLACE_RE.search(outside)
    if not m:
        return None
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    if raw.lower() == "ktx":
        return "KTX"
    return raw[0].upper() + raw[1:]


def resolve_scene_vibe(state):
    """Tra ve (vibe_key, vibe_label, vibe_directive) tu state['scene_vibe'] hoac suy ra tu tension."""
    st = state or {}
    v = st.get("scene_vibe")
    if v not in SCENE_VIBES:
        t_idx = min(4, max(0, int(st.get("tension", st.get("do_cang", 1)))))
        v = TENSION_TO_VIBE.get(t_idx, "lively")
    return v, SCENE_VIBES[v], SCENE_VIBE_DIRECTIVES[v]


def _detect_vibe_override(text, cur_vibe):
    s = str(text or "")
    if _INTIMATE_VIBE_RE.search(s):
        return "intimate"
    if _SENSUAL_VIBE_RE.search(s) and cur_vibe in (None, "", "relaxed", "lively"):
        return "sensual"
    return cur_vibe if cur_vibe in SCENE_VIBES else "lively"


def _match_keywords(keys, text):
    if not keys or not text:
        return None
    fake_entry = {"case_sensitive": False, "whole_words": True}
    for k in keys:
        if k and lore_engine._match(k, text, fake_entry, whole_default=True):
            return k
    return None


def transcript(history, pack, n=12):
    lines = []
    for m in rp_history(history)[-n:]:
        who = user_name_of(pack) if m["speaker"] == "user" else name_of(pack, m["speaker"])
        ch = m.get("channel_type")
        tag = " [Nhắn riêng]" if ch == "dm_text" else (" [Nhóm chat]" if ch == "group_text" else "")
        tid = f"#{m['turn_id']} " if m.get("turn_id") is not None else ""
        lines.append(f"{tid}{who}{tag}: {m.get('content', '')}")
    return "\n\n".join(lines)


def state_text(pack, state):
    user = user_name_of(pack)
    locs = state.get("locations") or pack.get("locations") or {}
    ents = state.get("entities") or {}
    user_ent = ents.get("user") or {}
    user_loc = user_ent.get("location_id") or state.get("dia_diem", "")
    bc = rpformat.location_breadcrumb(locs, user_loc)
    transit = user_ent.get("transit")
    if isinstance(transit, dict) and transit.get("from") and transit.get("to"):
        loc_line = f"Đang trên đường di chuyển từ {rpformat.location_breadcrumb(locs, transit['from'])} tới {bc}"
    else:
        loc_desc = (locs.get(user_loc) or {}).get("desc", "")
        loc_line = bc + (f" ({loc_desc})" if loc_desc else "")
    clk = rpformat.format_clock(state["clock"]) if isinstance(state.get("clock"), dict) else state.get("thoi_gian", "?")
    present = [name_of(pack, c) for c in rpformat.co_located_npcs(pack, state)]
    at_door_names = [
        name_of(pack, c["char_id"]) for c in (pack.get("cast") or [])
        if (ents.get(c["char_id"]) or {}).get("at_door") == user_loc and c["char_id"] not in rpformat.co_located_npcs(pack, state)
    ]
    ch = state.get("active_channel") or {}
    ch_type = ch.get("channel_type", "in_person")
    ch_names = [name_of(pack, p) for p in ch.get("participants", [])]
    ten = TENSION[min(4, max(0, int(state.get("tension", state.get("do_cang", 1)))))]
    _, vibe_label, _ = resolve_scene_vibe(state)
    co_str = (", " + ", ".join(present) if present else " (chỉ có một mình)") + (
        f" · ngoài cửa: {', '.join(at_door_names)}" if at_door_names else ""
    )
    return (
        f"Thời gian: {clk}\n"
        f"Địa điểm: {loc_line}\n"
        f"Có mặt vật lý: {user}{co_str}\n"
        f"Kênh giao tiếp: {CHANNEL_TYPES.get(ch_type, ch_type)}" + (f" [{', '.join(ch_names)}]" if ch_names else "") + "\n"
        f"Độ căng & Nhịp cảnh: {vibe_label} (mức {ten})"
    )


def _ensure_v2_state(pack, state):
    """Dam bao state tu client cu (rplabg) co du cau truc v2."""
    if isinstance(state.get("clock"), dict) and isinstance(state.get("entities"), dict):
        return rpformat.sync_legacy_state(pack, state)
    boot = rpformat.bootstrap_session(pack)
    if state.get("dia_diem") and state["dia_diem"] in boot["locations"]:
        boot["entities"]["user"]["location_id"] = state["dia_diem"]
    if state.get("co_mat"):
        for cid in boot["entities"]:
            if cid != "user":
                if cid in state["co_mat"]:
                    boot["entities"][cid]["location_id"] = boot["entities"]["user"]["location_id"]
                elif boot["entities"][cid]["location_id"] == boot["entities"]["user"]["location_id"]:
                    other = next((k for k in boot["locations"] if k != boot["entities"]["user"]["location_id"]), "")
                    if other:
                        boot["entities"][cid]["location_id"] = other
    if "do_cang" in state:
        boot["tension"] = int(state["do_cang"])
    if state.get("tom_tat"):
        boot["story_arc"] = state["tom_tat"]
        boot["summarized_upto"] = int(state.get("tom_tat_upto", 0))
    return rpformat.sync_legacy_state(pack, boot)


def _build_decision_trace(phase, st, q, res, thresholds=None, effects=None):
    """Dong goi bang trace Decision cho Turn X-Ray (cau hoi | xac suat | nguong | quyet dinh Python)."""
    a = (res or {}).get("answers") or {}
    thresholds = thresholds or {}
    effects = effects or {}
    rows = []
    for k, meta in (q or {}).items():
        qtype = meta.get("type", "")
        ans = a.get(k) or {}
        if qtype == "choice":
            probs = ans.get("probabilities") or {}
            top = ", ".join(f"{pk}:{float(pv):.2f}" for pk, pv in sorted(probs.items(), key=lambda x: -x[1])[:3])
            ans_str = f"{ans.get('choice')} (conf {float(ans.get('confidence', 0)):.2f})" + (f" [{top}]" if top else "")
        elif qtype == "score":
            ans_str = f"score {float(ans.get('score', 0)):.2f}"
        else:
            ans_str = f"p={float(ans.get('noul', 0)):.2f}"
        rows.append({
            "key": k,
            "type": qtype,
            "question": meta.get("instructions", ""),
            "answer": ans_str,
            "threshold": thresholds.get(k, "—"),
            "effect": effects.get(k, ""),
        })
    return {
        "phase": phase,
        "state": copy.deepcopy(st),
        "rows": rows,
        "model": (res or {}).get("model", ""),
        "secs": (res or {}).get("secs", 0),
        "cost": (res or {}).get("cost", 0),
    }


# ----------------------------------------------------------------------------- Pha 1A & 1B & EX1
def direct(
    api_key,
    model,
    pack,
    state,
    history,
    force_speaker=None,
    force_reason=None,
    turn_source="user",
    channel_override="auto",
    decision_provider=None,
    group_mode="single",
):
    """Pha 1A (user turn) hoac Pha 1B (npc_chain turn)."""
    history = rp_history(history)
    state = _ensure_v2_state(pack, state)
    dp = decision_provider or DecisionProvider(api_key, model)
    cast = cast_by_id(pack)
    user = user_name_of(pack)
    present = rpformat.co_located_npcs(pack, state)
    ch = state.get("active_channel") or {"channel_type": "in_person", "participants": ["user"] + present}
    channel_npcs = [c for c in ch.get("participants", []) if c in cast]

    # --- PHA 1B: NPC CHAIN ---
    if turn_source == "npc_chain":
        last_sp = history[-1]["speaker"] if history else None
        chain_said = set(recent_chain_speakers(history))
        eligible = [c for c in channel_npcs if c != last_sp and c not in chain_said]
        if not eligible:
            eligible = [c for c in present if c != last_sp and c not in chain_said]
        speakers = {cid: f"{cast[cid]['name']}: {cast[cid]['short']}" for cid in eligible}
        speakers[NARRATOR] = "Người dẫn truyện: mô tả phản ứng cảnh, chuyển nhịp tự nhiên"
        q = {
            "turn_type": {"type": "choice", "criteria": TURN_TYPES, "instructions": prompts.get_text("director.turn_type_1b")},
        }
        allow_force = bool(force_speaker and (force_speaker in eligible or force_speaker == NARRATOR or (force_speaker in cast and force_speaker not in chain_said)))
        if not allow_force and len(speakers) > 1:
            q["speaker"] = {
                "type": "choice",
                "criteria": speakers,
                "instructions": prompts.get_text("director.speaker_1b"),
            }
        # Lore (director) & toi da 1-2 director_critical modules cho nguoi noi
        for e in pack.get("lore") or []:
            if e.get("activation") == "director":
                q[f"lore__{e['id']}"] = {
                    "type": "noul",
                    "instructions": prompts.render("director.lore", title=e["title"], preview=e["content"][:120]),
                }
        mod_targets = [force_speaker] if (allow_force and force_speaker in cast) else eligible
        for cid in mod_targets:
            crit = [m for m in cast[cid]["v2"].get("modules", []) if m.get("activation") == "director_critical"][:2]
            for mod in crit:
                q[f"mod__{cid}__{mod['id']}"] = {
                    "type": "noul",
                    "instructions": prompts.render("director.module", char_name=cast[cid]["name"], when_text=mod.get("when") or mod["content"][:80]),
                }
        st = {
            "boi_canh": state_text(pack, state),
            "dien_bien_gan_day": transcript(history, pack, 6) or (pack.get("opening") or {}).get("narration", ""),
        }
        res = dp.ask(st, q)
        a = res["answers"]
        if allow_force:
            speaker, conf, why = force_speaker, 1.0, force_reason or "phản ứng dây chuyền (Pha 1B)"
            probs = {name_of(pack, force_speaker): 1.0}
        elif "speaker" in a:
            sp = a["speaker"]
            speaker = sp["choice"]
            conf = float(sp.get("confidence", 0))
            why = "đạo diễn Pha 1B chọn"
            probs = {name_of(pack, k): v for k, v in (sp.get("probabilities") or {}).items()}
        else:
            speaker, conf, why = NARRATOR, 1.0, "hết nhân vật chưa phát biểu trong nhịp chain"
            probs = {"Người dẫn truyện": 1.0}
        lore_used = [e["id"] for e in (pack.get("lore") or []) if e.get("activation") == "director" and a.get(f"lore__{e['id']}", {}).get("noul", 0) >= 0.5]
        active_mods = [
            k.split("__", 2)[2]
            for k, v in a.items()
            if k.startswith(f"mod__{speaker}__") and v.get("noul", 0) >= MODULE_THRESHOLD
        ]
        th_1b = {k: (f">= {MODULE_THRESHOLD}" if k.startswith("mod__") else ">= 0.50") for k in q if k.startswith(("lore__", "mod__"))}
        eff_1b = {
            "turn_type": TURN_TYPES.get(a["turn_type"]["choice"], ""),
            **({"speaker": f"{name_of(pack, speaker)} ({why})"} if "speaker" in q else {}),
            **{f"lore__{e['id']}": "Nạp vào Zone 4" if e["id"] in lore_used else "Bỏ qua" for e in (pack.get("lore") or []) if f"lore__{e['id']}" in q},
        }
        for k in q:
            if k.startswith("mod__"):
                mid = k.split("__", 2)[2]
                eff_1b[k] = "Kích hoạt" if (k.startswith(f"mod__{speaker}__") and mid in active_mods) else "Bỏ qua"
        dec_trace_1b = _build_decision_trace("Pha 1B (Phản ứng dây chuyền)", st, q, res, th_1b, eff_1b)
        vibe_key_1b, vibe_lbl_1b, _ = resolve_scene_vibe(state)
        return {
            "turn_source": "npc_chain",
            "channel_type": ch.get("channel_type", "in_person"),
            "speaker": speaker,
            "speaker_name": name_of(pack, speaker),
            "speaker_why": why,
            "speaker_conf": conf,
            "speaker_probs": probs,
            "arriving": [],
            "entering": False,
            "moving_companions": [],
            "user_time_delta": "instant",
            "user_move_to": "stay",
            "owed": list(state.get("owed") or []),
            "addressee": None,
            "address_probs": {},
            "turn_type": a["turn_type"]["choice"],
            "turn_type_label": TURN_TYPES.get(a["turn_type"]["choice"], ""),
            "scene_vibe": vibe_key_1b,
            "scene_vibe_label": vibe_lbl_1b,
            "lore": [{"id": e["id"], "title": e["title"], "p": round(a[f"lore__{e['id']}"]["noul"], 2)} for e in (pack.get("lore") or []) if f"lore__{e['id']}" in a],
            "lore_used": lore_used,
            "active_modules": active_mods,
            "decision_trace": dec_trace_1b,
            "cost": res["cost"],
            "secs": res["secs"],
            "model": res["model"],
        }

    # --- PHA 1A: USER INPUT ANALYZER & PRE-RENDER DELTA ---
    recent_user = " ".join(m["content"] for m in [m for m in history if m["speaker"] == "user"][-2:])
    last_is_user = bool(history) and history[-1]["speaker"] == "user"
    last_user = history[-1]["content"] if last_is_user else ""
    user_loc = ((state.get("entities") or {}).get("user") or {}).get("location_id") or state.get("dia_diem")
    arriving = [cid for cid in cast if cid not in present and mentioned(cast[cid], recent_user)]
    ch_prev_type = ch.get("channel_type", "in_person")
    ents_now = state.get("entities") or {}
    active_remote = (
        [cid for cid in channel_npcs if cid not in present and cid not in arriving]
        if ch_prev_type in ("dm_text", "group_text")
        else [cid for cid in channel_npcs if cid not in present and (ents_now.get(cid) or {}).get("at_door") == user_loc]
    )
    candidates = list(dict.fromkeys(present + active_remote + arriving))
    at_mentions = _explicit_at_mentions(cast, last_user) if last_is_user else []
    for cid in at_mentions:
        if cid not in present and cid not in arriving:
            arriving.append(cid)
        if cid not in candidates:
            candidates.append(cid)
    if force_speaker in cast and force_speaker not in candidates:
        candidates.append(force_speaker)
    is_single_chat = (len(cast) == 1)
    sole_cid = next(iter(cast)) if is_single_chat else None
    if is_single_chat and sole_cid not in candidates:
        candidates.append(sole_cid)
    absent = [cid for cid in cast if cid not in present]
    prev_npc = next((m.get("speaker") for m in reversed(history[:-1] if last_is_user else history) if m.get("speaker") in cast), None)

    def _char_hint(cid):
        c = cast[cid]
        al = ", ".join((c.get("aliases") or [])[:3])
        rl = c.get("role_in_world") or ""
        extra = "; ".join(x for x in (f"tên gọi: {al}" if al else "", f"vai trò: {rl}" if rl else "") if x)
        return f" ({extra})" if extra else ""

    solo_env_mode = (
        not present
        and not active_remote
        and not arriving
        and ch.get("channel_type", "in_person") == "in_person"
        and not _has_remote_signal(last_user)
        and force_speaker not in cast
    )

    speakers = {cid: f"{cast[cid]['name']}{_char_hint(cid)}: {cast[cid]['short']}" for cid in present}
    speakers.update({cid: f"{cast[cid]['name']} (đang chat/nhắn tin từ xa{_char_hint(cid)}): {cast[cid]['short']}" for cid in active_remote})
    speakers.update({cid: f"{cast[cid]['name']} (được nhắc tên hoặc đang tới{_char_hint(cid)}): {cast[cid]['short']}" for cid in arriving})
    speakers[NARRATOR] = "Người dẫn truyện: mô tả cảnh, sự kiện, nhân vật phụ nói, hoặc chưa có nhân vật chính phù hợp"

    locs = state.get("locations") or pack.get("locations") or {}
    user_loc = ((state.get("entities") or {}).get("user") or {}).get("location_id") or state.get("dia_diem")
    cand_locs = rpformat.candidate_locations(locs, user_loc, limit=8)

    q = {}
    is_scene_mode = (group_mode == "scene") and not is_single_chat
    if not solo_env_mode and not is_single_chat and not (is_scene_mode and force_speaker not in cast):
        q["speaker"] = {
            "type": "choice",
            "criteria": speakers,
            "instructions": prompts.render("director.speaker_1a", user=user),
        }
    q["turn_type"] = {"type": "choice", "criteria": TURN_TYPES, "instructions": prompts.get_text("director.turn_type_1a")}
    q["user_time_delta"] = {
        "type": "choice",
        "criteria": rpformat.TIME_DELTA_LABELS,
        "instructions": prompts.render("director.user_time_delta", user=user),
    }
    q["user_move_to"] = {
        "type": "choice",
        "criteria": cand_locs,
        "instructions": prompts.render("director.user_move_to", user=user),
    }
    if last_is_user:
        q["move_status"] = {
            "type": "choice",
            "criteria": MOVE_STATUSES,
            "instructions": prompts.render("director.move_status", user=user),
        }
        q["user_pass_ball"] = {
            "type": "noul",
            "instructions": prompts.render("director.user_pass_ball", user=user),
        }
        q["scene_vibe"] = {
            "type": "choice",
            "criteria": SCENE_VIBE_CRITERIA,
            "instructions": prompts.render("director.scene_vibe", user=user),
        }

    if channel_override in ("in_person", "dm_text", "group_text"):
        pass
    elif not solo_env_mode or _has_remote_signal(last_user):
        q["channel_type"] = {
            "type": "choice",
            "criteria": CHANNEL_TYPES,
            "instructions": prompts.render("director.channel_type", user=user),
        }

    named_candidates = []
    if last_is_user:
        if not solo_env_mode and not is_single_chat:
            targets = {cid: f"{cast[cid]['name']}{_char_hint(cid)}" for cid in candidates}
            targets.update({cid: f"{cast[cid]['name']} ({cast[cid]['short']}; chưa có mặt)" for cid in cast if cid not in candidates})
            targets[GROUP] = "Cả nhóm, hoặc không nói riêng với ai cụ thể"
            targets[MINOR_NPC] = "Nhân vật phụ ngoài dàn chính (cô bán hàng, giảng viên, bảo vệ...) hoặc môi trường"
            q["noi_voi"] = {
                "type": "choice",
                "criteria": targets,
                "instructions": prompts.render("director.noi_voi", user=user),
            }

        named_candidates = [cid for cid in candidates if mentioned(cast[cid], last_user)] or [cid for cid in cast if mentioned(cast[cid], last_user)]
        if not solo_env_mode and not is_single_chat and (len(present) >= 2 or len(named_candidates) >= 2):
            multi_pool = list(dict.fromkeys([c for c in named_candidates if c in candidates] + present))[:4]
            for cid in multi_pool:
                q[f"addressed__{cid}"] = {
                    "type": "noul",
                    "instructions": prompts.render("director.addressed_multi", user=user, char_name=cast[cid]["name"]),
                }

        if force_speaker in cast:
            hint_target = force_speaker
        elif is_single_chat:
            hint_target = sole_cid
        elif len(named_candidates) == 1:
            hint_target = named_candidates[0]
        elif len(present) == 1:
            hint_target = present[0]
        elif prev_npc in candidates:
            hint_target = prev_npc
        elif len(candidates) == 1:
            hint_target = candidates[0]
        else:
            hint_target = None

        ask_pronoun = (
            not _is_group_address_text(last_user)
            and (
                (not is_pronoun_locked(state, hint_target) or any(not is_pronoun_locked(state, c) for c in named_candidates))
                if hint_target
                else any(not is_pronoun_locked(state, c) for c in candidates)
            )
        )
        if ask_pronoun:
            t_label = cast[hint_target]["name"] if hint_target else "nhân vật đối diện"
            q["user_pronoun_pair"] = {
                "type": "choice",
                "criteria": rpformat.USER_PRONOUN_PAIRS,
                "instructions": prompts.render("director.user_pronoun_pair", target_name=t_label, user=user),
            }

    if not is_single_chat:
        for cid in present:
            q[f"companion__{cid}"] = {
                "type": "noul",
                "instructions": prompts.render("director.companion", user=user, char_name=cast[cid]["name"]),
            }

        for cid in absent:
            q[f"xuat_hien__{cid}"] = {
                "type": "noul",
                "instructions": prompts.render("director.xuat_hien", char_name=cast[cid]["name"], char_short=cast[cid]["short"]),
            }

    for e in pack.get("lore") or []:
        if e.get("activation") == "director":
            q[f"lore__{e['id']}"] = {
                "type": "noul",
                "instructions": prompts.render("director.lore", title=e["title"], preview=e["content"][:120]),
            }

    # Hoi toi da 2 director_critical modules / ung vien (tong tran 6), du bao phu cac NPC trong canh
    crit_budget = 6
    for cid in candidates:
        if crit_budget <= 0:
            break
        per_char = 0
        for mod in cast[cid]["v2"].get("modules", []):
            if mod.get("activation") == "director_critical" and crit_budget > 0 and per_char < 2:
                q[f"mod__{cid}__{mod['id']}"] = {
                    "type": "noul",
                    "instructions": prompts.render("director.module", char_name=cast[cid]["name"], when_text=mod.get("when") or mod["content"][:90]),
                }
                crit_budget -= 1
                per_char += 1

    quiet = {c: n for c in present if (n := (state.get("im_lang") or {}).get(c, 0)) >= 2}
    st = {
        "boi_canh": state_text(pack, state),
        "dien_bien_gan_day": transcript(history, pack, 6) or (pack.get("opening") or {}).get("narration", ""),
        "da_im_lang": "; ".join(f"{cast[c]['name']}: có mặt {n} lượt chưa nói" for c, n in quiet.items()) or "không ai",
    }
    if prev_npc and prev_npc in cast:
        st["nguoi_vua_noi_luot_truoc"] = f"{cast[prev_npc]['name']} (id: {prev_npc})"
    if last_is_user:
        st["tin_cuoi_cua_nguoi_choi"] = f"{user}: {last_user}"

    res = dp.ask(st, q)
    a = res["answers"]

    seen = [cid for cid in absent if a.get(f"xuat_hien__{cid}", {}).get("noul", 0) >= ARRIVE_THRESHOLD]
    sp = a.get("speaker") or {}
    if is_single_chat:
        speaker, conf, why = (NARRATOR if force_speaker == NARRATOR else sole_cid), 1.0, "đối thoại 1-1"
    elif sp:
        speaker, conf, why = sp.get("choice", NARRATOR), float(sp.get("confidence", 0)), "đạo diễn Pha 1A chọn"
    else:
        speaker, conf, why = NARRATOR, 1.0, "người chơi đang ở một mình tương tác môi trường"

    addressee, addr_p, ach, ach_conf = None, {}, None, 0.0
    if is_single_chat:
        addressee = sole_cid if last_is_user else None
    elif "noi_voi" in a:
        addr_p = a["noi_voi"].get("probabilities") or {}
        ach = a["noi_voi"].get("choice")
        ach_conf = float(a["noi_voi"].get("confidence", 0))
        if ach == MINOR_NPC and ach_conf >= ADDRESS_CONF:
            addressee = MINOR_NPC
        elif ach in cast and ach_conf >= ADDRESS_CONF:
            addressee = ach

    # Giai cuu @mention truc tiep truoc khi chot kenh va Case #3
    if last_is_user and len(at_mentions) == 1 and addressee in (None, MINOR_NPC) and not force_speaker:
        addressee = at_mentions[0]

    # Chot channel_type (+ Case #12 khong dinh dm_text/group_text + Case #13 goi dien thoai)
    prev_ch_type = ch.get("channel_type", "in_person")
    remote_sig = _has_remote_signal(last_user)
    if channel_override in ("in_person", "dm_text", "group_text"):
        ch_type = channel_override
    else:
        ch_ans = a.get("channel_type") or {}
        ch_choice = ch_ans.get("choice") or prev_ch_type
        ch_conf = float(ch_ans.get("confidence", 0))
        ch_probs = ch_ans.get("probabilities") or {}
        ch_type = ch_choice
        if remote_sig and ch_choice in ("dm_text", "group_text") and (addressee in absent or (addressee is None and speaker in absent)):
            if ch_conf < 0.45 and float(ch_probs.get(ch_choice, 0)) < 0.60:
                ch_type = prev_ch_type
        elif ch_conf < 0.65:
            ch_type = prev_ch_type
        if prev_ch_type in ("dm_text", "group_text") and last_is_user and not remote_sig:
            if speaker in present or addressee in present or ach in present or ach in (GROUP, MINOR_NPC):
                ch_type = "in_person"

    comm = detect_comm_medium(last_user, ch_type)
    if comm["is_voice_call"] and channel_override == "auto" and ((addressee in absent) or (speaker in absent and not present) or remote_sig):
        ch_type = "dm_text"
    elif (
        channel_override == "auto"
        and remote_sig
        and ch_type == "in_person"
        and addressee in absent
        and addressee not in seen
        and not _has_arrival_narration(cast[addressee], last_user)
    ):
        ch_type = "dm_text"
    comm_medium = "spoken" if (ch_type == "in_person" or comm["is_voice_call"]) else "texting"
    speakerphone = bool(comm["speakerphone"] and ch_type != "in_person")

    # Promote nhan vat vang mat that su duoc dan vao canh / goi @Ten truoc khi chay Case #1
    if ch_type == "in_person" and last_is_user and not remote_sig:
        for cid in absent:
            if cid in seen:
                continue
            x_p = float(a.get(f"xuat_hien__{cid}", {}).get("noul", 0))
            if (
                cid in at_mentions
                or (_has_arrival_narration(cast[cid], last_user) and (x_p >= 0.15 or addressee == cid or speaker == cid))
                or (addressee == cid and ach_conf >= 0.70 and speaker == cid and x_p >= 0.30)
            ):
                seen.append(cid)

    # Case #3: Tuong tac nhan vat phu / moi truong -> ep narrator
    if addressee == MINOR_NPC and not force_speaker:
        speaker, why = NARRATOR, f"{user} đang nói chuyện với nhân vật phụ / môi trường"

    # Case #1: Nhac ten nguoi vang mat o ngoi thu 3 trong phong -> khong keo nham vao addressees
    if ch_type == "in_person" and not remote_sig:
        if addressee in absent and addressee not in seen and present:
            addressee = None
        if speaker in absent and speaker not in seen and speaker != addressee:
            probs = sp.get("probabilities") or {}
            speaker = max(present, key=lambda c: probs.get(c, 0)) if present else NARRATOR
            why = "người nhắc tên ở ngôi thứ 3 không có mặt → giữ người đang có mặt"

    if ch_type == "in_person":
        confirmed_arriving = [c for c in arriving if c in seen]
        if speaker == NARRATOR and confirmed_arriving and last_is_user and addressee != MINOR_NPC:
            probs = sp.get("probabilities") or {}
            speaker = max(confirmed_arriving, key=lambda c: probs.get(c, 0))
            why = f"nhân vật đang bước vào được {user} nói tới (đạo diễn {conf:.2f})"
        if addressee in absent and addressee not in seen and not present and not remote_sig:
            seen.append(addressee)
    new_arr = [c for c in seen if c not in arriving]
    arriving = arriving + new_arr

    # Case #4: Giai ma dai tu quy chieu ("ba kia", "ong do") theo nguoi vua noi
    if addressee is None and last_is_user and _has_deictic_prev_ref(last_user) and prev_npc in present and ach != MINOR_NPC and not force_speaker:
        addressee = prev_npc
        if speaker != prev_npc:
            speaker, why = prev_npc, f"{user} quy chiếu tới người vừa nói ({cast[prev_npc]['name']})"

    if ch_type == "in_person" and speaker == NARRATOR and new_arr and last_is_user and not addressee:
        speaker, why = new_arr[0], f"nhân vật vừa xuất hiện ({a[f'xuat_hien__{new_arr[0]}']['noul']:.2f})"
    if addressee in cast and speaker != addressee:
        speaker, why = addressee, f"{user} đang nói/nhắn trực tiếp với {cast[addressee]['name']} ({ach_conf:.2f})"
    elif addressee is None and not solo_env_mode:
        named_pool = [c for c in candidates if (ch_type != "in_person" or remote_sig or c in present or c in seen)]
        named = [cid for cid in named_pool if mentioned(cast[cid], last_user)] if last_is_user else []
        if named and speaker not in named and conf < 0.6 and why == "đạo diễn Pha 1A chọn" and addr_p.get(GROUP, 0) < 0.5:
            speaker, why = named[0], f"{user} gọi tên (đạo diễn chỉ chắc {conf:.2f})"

    if force_speaker and (force_speaker in cast or force_speaker == NARRATOR):
        speaker, why = force_speaker, force_reason or "chọn lại người nói"

    # Pre-render delta: time & move (#6 plan_only vs moving_now, #8 in_transit, #15 dialogue time)
    td = a.get("user_time_delta") or {}
    user_time_delta = td.get("choice", "instant") if float(td.get("confidence", 0)) >= 0.55 else "instant"
    turn_type_choice = (a.get("turn_type") or {}).get("choice", "thoai")
    if user_time_delta != "instant" and last_is_user and _is_pure_dialogue_time_mention(last_user) and turn_type_choice != "chuyen_canh":
        user_time_delta = "instant"

    explicit_time_override = None
    if last_is_user and user_time_delta == "instant":
        det_clk = _detect_narration_clock(last_user, state.get("clock"), is_opening=(len(history) <= 1))
        if det_clk:
            user_time_delta = "explicit_time"
            explicit_time_override = det_clk

    mv = a.get("user_move_to") or {}
    mv_choice = mv.get("choice", "stay")
    mv_conf = float(mv.get("confidence", 0))
    move_threshold = 0.65 if turn_type_choice == "chuyen_canh" else CONF_HIGH
    user_move_to = mv_choice if (mv_choice != "stay" and mv_conf >= move_threshold) else "stay"

    mstat_ans = a.get("move_status") or {}
    mstat_choice = mstat_ans.get("choice") if float(mstat_ans.get("confidence", 0)) >= 0.55 else (
        "in_transit" if (user_move_to != "stay" and _is_in_transit_text(last_user)) else ("arrived" if user_move_to != "stay" else "stay")
    )
    if user_move_to != "stay":
        if mstat_choice in ("stay", "plan_only") or (last_is_user and _is_plan_only_move(last_user) and mstat_choice != "in_transit"):
            user_move_to = "stay"
            mstat_choice = "plan_only"
        elif mstat_choice == "in_transit" or (last_is_user and _is_in_transit_text(last_user)):
            mstat_choice = "in_transit"
    else:
        mstat_choice = "plan_only" if mstat_choice == "plan_only" else "stay"

    scene_place_hint = None
    is_placeholder_loc = (
        user_loc == "boi_canh_chinh"
        or (locs.get(user_loc) or {}).get("name") == "Không gian hiện tại"
    )
    if last_is_user and user_move_to == "stay" and is_placeholder_loc:
        outside_txt = _strip_dialogue_quotes(last_user)
        matched_loc = rpformat.find_matching_location(
            {k: v for k, v in locs.items() if k != "boi_canh_chinh"},
            outside_txt,
        ) if outside_txt else None
        if matched_loc:
            user_move_to = matched_loc
            mstat_choice = "arrived"
        else:
            place_hint = _detect_scene_place(last_user)
            if place_hint:
                user_move_to = "new_place"
                mstat_choice = "arrived"
                scene_place_hint = place_hint

    moving_companions = []
    if user_move_to != "stay" and ch_type == "in_person" and not (last_is_user and _user_moves_alone(last_user)):
        if is_single_chat:
            moving_companions = [sole_cid]
        else:
            moving_companions = [cid for cid in present if a.get(f"companion__{cid}", {}).get("noul", 0) >= 0.55]
            # Neu speaker la mot NPC hien dien va khong bi danh dau o lai, ma ca nhom dang di chuyen cung speaker -> giu dong bo
            if speaker in present and speaker not in moving_companions and len(present) == 1:
                if a.get(f"companion__{speaker}", {}).get("noul", 0) > CONF_LOW:
                    moving_companions.append(speaker)

    # Case #2: Multi-addressee owed queue
    if is_single_chat:
        owed = []
    elif last_is_user and addressee != MINOR_NPC and not _is_group_address_text(last_user):
        valid_pool = [c for c in candidates if (ch_type != "in_person" or remote_sig or c in present or c in seen)]
        dec_multi = [c for c in valid_pool if a.get(f"addressed__{c}", {}).get("noul", 0) >= 0.55]
        named_p = [c for c in valid_pool if mentioned(cast[c], last_user)]
        merged_multi = list(dict.fromkeys(dec_multi + (named_p if (addr_p.get(GROUP, 0) < 0.5 or len(dec_multi) >= 2) else [])))
        merged_multi.sort(key=lambda c: (pos if (pos := _first_mention_pos(cast[c], last_user)) is not None else 9999))
        if len(merged_multi) >= 2:
            if addressee not in merged_multi and not force_speaker and speaker not in merged_multi:
                speaker, why = merged_multi[0], f"{user} giao lời cho nhiều người (đầu tiên: {cast[merged_multi[0]]['name']})"
            owed = [c for c in merged_multi if c != speaker]
        else:
            owed = ([addressee] if addressee in cast else (named_p if addr_p.get(GROUP, 0) < 0.5 else []))
            owed = [c for c in owed if c != speaker]
    elif last_is_user:
        owed = []
    else:
        owed = [c for c in state.get("owed", []) if c in cast and c != speaker]

    if is_scene_mode and not (force_speaker and (force_speaker in cast or force_speaker == NARRATOR)):
        if (not solo_env_mode) or seen or arriving or (addressee in cast) or active_remote:
            speaker = SCENE_SPEAKER
            conf = 1.0
            why = force_reason or "chế độ Toàn cảnh (1 lần gọi tất cả nhân vật)"
            owed = []

    lore_used = [
        e["id"] for e in (pack.get("lore") or [])
        if e.get("activation") == "director" and a.get(f"lore__{e['id']}", {}).get("noul", 0) >= 0.5
    ]
    scene_modules = {}
    if speaker == SCENE_SPEAKER:
        for cid in candidates:
            mids = [
                k.split("__", 2)[2]
                for k, v in a.items()
                if k.startswith(f"mod__{cid}__") and v.get("noul", 0) >= MODULE_THRESHOLD
            ]
            if mids:
                scene_modules[cid] = mids
        active_mods = list(dict.fromkeys(mid for mids in scene_modules.values() for mid in mids))
    else:
        active_mods = [
            k.split("__", 2)[2]
            for k, v in a.items()
            if k.startswith(f"mod__{speaker}__") and v.get("noul", 0) >= MODULE_THRESHOLD
        ]

    # Case #20 & #21: Khong hoc xưng hô khi nói cả nhóm + Phat hien pha xưng hô đã khóa
    is_group_addr = bool(
        addressee in (GROUP, MINOR_NPC)
        or ach in (GROUP, MINOR_NPC)
        or addr_p.get(GROUP, 0) >= 0.45
        or (last_is_user and _is_group_address_text(last_user))
    )
    up_ans = a.get("user_pronoun_pair") or {}
    up_choice = up_ans.get("choice", "unclear")
    up_conf = float(up_ans.get("confidence", 0))
    if addressee in cast:
        lock_target = addressee
    elif not is_group_addr and speaker in cast and (len(present) == 1 or speaker in named_candidates or addr_p.get(GROUP, 0) < 0.5):
        lock_target = speaker
    else:
        lock_target = None
    det_pair = rpformat.detect_user_pronoun_pair(last_user, pack, lock_target) if (last_is_user and lock_target and not is_group_addr) else None
    eff_pair = det_pair or (up_choice if (up_choice != "unclear" and up_conf >= 0.55) else None)
    pronoun_break = None
    if not is_group_addr and lock_target and not is_pronoun_locked(state, lock_target) and eff_pair:
        locked_pair, locked_target = eff_pair, lock_target
        eff_conf = 1.0 if det_pair else round(up_conf, 2)
    else:
        locked_pair, locked_target = None, None
        eff_conf = round(up_conf, 2) if up_ans else 0
        if not is_group_addr and lock_target and is_pronoun_locked(state, lock_target) and det_pair:
            u2c_edge = (((state.get("relationships") or {}).get("edges") or {}).get("user") or {}).get(lock_target) or {}
            u2c_dflt = u2c_edge.get("default") or {}
            recip_chk = rpformat.infer_reciprocal(det_pair, rpformat.gender_of(pack, "user"), rpformat.gender_of(pack, lock_target))
            if recip_chk:
                cand_u = recip_chk[0]
                if (u2c_dflt.get("self"), u2c_dflt.get("call")) != (cand_u.get("self"), cand_u.get("call")):
                    pronoun_break = det_pair

    # Case #28: user_pass_ball
    upb_p = float((a.get("user_pass_ball") or {}).get("noul", 0))
    user_pass_ball = bool(last_is_user and (upb_p >= 0.65 or _is_short_passive_turn(last_user)))

    # Scene Vibe (Pre-Render 8 trang thai kich tinh)
    cur_vibe, _, _ = resolve_scene_vibe(state)
    sv_ans = a.get("scene_vibe") or {}
    sv_choice = sv_ans.get("choice")
    base_vibe = sv_choice if (sv_choice in SCENE_VIBES and float(sv_ans.get("confidence", 0)) >= 0.50) else cur_vibe
    eff_vibe = _detect_vibe_override(last_user, base_vibe) if last_is_user else base_vibe
    eff_vibe_label = SCENE_VIBES[eff_vibe]

    th_1a = {
        "channel_type": "conf >= 0.65",
        "noi_voi": f"conf >= {ADDRESS_CONF}",
        "user_time_delta": "conf >= 0.55",
        "user_move_to": f"conf >= {move_threshold}",
        "move_status": "conf >= 0.55",
        "scene_vibe": "conf >= 0.50 / regex cường độ",
        "user_pass_ball": ">= 0.65 / ngắn thụ động",
        "user_pronoun_pair": "Regex / conf >= 0.55",
    }
    eff_1a = {
        "speaker": f"{name_of(pack, speaker)} ({why})",
        "turn_type": TURN_TYPES.get(turn_type_choice, ""),
        "user_time_delta": user_time_delta,
        "explicit_time_override": explicit_time_override,
        "user_move_to": user_move_to,
        "scene_place_hint": scene_place_hint,
        "move_status": mstat_choice,
        "scene_vibe": f"{eff_vibe_label} ({eff_vibe})",
        "user_pass_ball": "Bỏ qua mo_dien_bien_moi ở Gate" if user_pass_ball else "Bình thường",
        "channel_type": f"{ch_type} ({comm_medium})",
        "noi_voi": "Nhân vật phụ / môi trường" if addressee == MINOR_NPC else (name_of(pack, addressee) if addressee else "Chưa chốt riêng"),
        "user_pronoun_pair": (
            f"Khóa {locked_pair} cho {name_of(pack, locked_target)}" if locked_pair
            else (f"Phát hiện bẻ xưng hô ({pronoun_break}) — giữ lock gốc" if pronoun_break
                  else ("Bỏ qua (nói cả nhóm)" if is_group_addr else ("Đã khóa sẵn / unclear" if "user_pronoun_pair" in q else "")))
        ),
    }
    for k in q:
        if k.startswith("companion__"):
            cid = k.split("__", 1)[1]
            th_1a[k] = ">= 0.55"
            eff_1a[k] = "Đi cùng" if cid in moving_companions else "Ở lại"
        elif k.startswith("addressed__"):
            cid = k.split("__", 1)[1]
            th_1a[k] = ">= 0.55"
            eff_1a[k] = "Có giao lời" if (cid == speaker or cid in owed) else "Không"
        elif k.startswith("xuat_hien__"):
            cid = k.split("__", 1)[1]
            th_1a[k] = f">= {ARRIVE_THRESHOLD}"
            eff_1a[k] = "Bước vào cảnh" if cid in seen else "Chưa tới"
        elif k.startswith("lore__"):
            eid = k.split("__", 1)[1]
            th_1a[k] = ">= 0.50"
            eff_1a[k] = "Nạp vào Zone 4" if eid in lore_used else "Bỏ qua"
        elif k.startswith("mod__"):
            _, m_cid, mid = k.split("__", 2)
            th_1a[k] = f">= {MODULE_THRESHOLD}"
            mod_on = (mid in scene_modules.get(m_cid, ())) if speaker == SCENE_SPEAKER else (m_cid == speaker and mid in active_mods)
            eff_1a[k] = "Kích hoạt" if mod_on else "Bỏ qua"
    dec_trace_1a = _build_decision_trace("Pha 1A (Đạo diễn & Phân tích Input)", st, q, res, th_1a, eff_1a)

    return {
        "turn_source": "user",
        "group_mode": "scene" if speaker == SCENE_SPEAKER else "single",
        "channel_type": ch_type,
        "comm_medium": comm_medium,
        "speakerphone": speakerphone,
        "speaker": speaker,
        "speaker_name": name_of(pack, speaker),
        "speaker_why": why,
        "speaker_conf": conf,
        "speaker_probs": {name_of(pack, k): v for k, v in (sp.get("probabilities") or {}).items()} if sp else {name_of(pack, speaker): 1.0},
        "arriving": arriving,
        "seen_physical": seen,
        "entering": ((speaker in arriving and speaker in seen) or (speaker == SCENE_SPEAKER and bool(seen))) and ch_type == "in_person" and (speaker not in present),
        "scene_modules": scene_modules,
        "moving_companions": moving_companions,
        "user_time_delta": user_time_delta,
        "explicit_time_override": explicit_time_override,
        "user_move_to": user_move_to,
        "scene_place_hint": scene_place_hint,
        "move_status": mstat_choice,
        "user_move_conf": round(mv_conf, 2),
        "owed": owed,
        "addressee": addressee,
        "is_group_address": is_group_addr,
        "pronoun_break": pronoun_break,
        "user_pass_ball": user_pass_ball,
        "address_probs": {
            ("nhân vật phụ" if k == MINOR_NPC else ("cả nhóm" if k == GROUP else name_of(pack, k))): round(v, 2)
            for k, v in addr_p.items()
        },
        "turn_type": turn_type_choice,
        "turn_type_label": TURN_TYPES.get(turn_type_choice, ""),
        "scene_vibe": eff_vibe,
        "scene_vibe_label": eff_vibe_label,
        "lore": [
            {"id": e["id"], "title": e["title"], "p": round(a[f"lore__{e['id']}"]["noul"], 2)}
            for e in (pack.get("lore") or []) if f"lore__{e['id']}" in a
        ],
        "lore_used": lore_used,
        "active_modules": active_mods,
        "user_pronoun_pair": locked_pair,
        "user_pronoun_conf": eff_conf,
        "pronoun_lock_target": locked_target,
        "decision_trace": dec_trace_1a,
        "cost": res["cost"],
        "secs": res["secs"],
        "model": res["model"],
    }


def apply_pre_render_updates(api_key, extract_model, pack, state, history, dec):
    """EX1: Cap nhat Clock, Spatial Tree, user + moving_companions / arriving_speaker TRUOC Pha 2."""
    history = rp_history(history)
    new_state = copy.deepcopy(_ensure_v2_state(pack, state))
    extra_cost = 0.0
    ch_type = dec.get("channel_type") or "in_person"
    last_is_user = bool(history) and history[-1]["speaker"] == "user"
    last_user = next((m["content"] for m in reversed(history) if m["speaker"] == "user"), "")
    locs = new_state["locations"]
    user_loc = new_state["entities"]["user"]["location_id"]

    old_user_loc = user_loc
    # 1. New place extraction neu user_move_to == "new_place" (+ Case #10 deduplication)
    target_loc = dec.get("user_move_to", "stay")
    if target_loc == "new_place":
        matched = rpformat.find_matching_location(locs, last_user)
        if matched:
            target_loc = matched
            dec["user_move_to"] = matched
        else:
            created = False
            if api_key:
                try:
                    sys_p = prompts.get_text("post_render.new_place_extractor_sys")
                    loc_list = ", ".join(f"{k} ({v['name']})" for k, v in locs.items())
                    obj, c = extract_json(api_key, extract_model, sys_p, f"Bản đồ hiện có: {loc_list}\nVị trí trước đó: {user_loc}\nTin người chơi: {last_user}")
                    extra_cost += c
                    matched2 = rpformat.find_matching_location(locs, obj.get("name", ""), obj.get("id", ""))
                    if matched2:
                        target_loc = matched2
                        dec["user_move_to"] = matched2
                    else:
                        nid = rpformat.slug(obj.get("id") or obj.get("name") or "dia_diem_moi")
                        base, k = nid, 2
                        while nid in locs:
                            nid, k = f"{base}_{k}", k + 1
                        parent = obj.get("parent") if obj.get("parent") in locs else (locs.get(user_loc, {}).get("parent"))
                        locs[nid] = {
                            "name": (obj.get("name") or "Địa điểm mới").strip(),
                            "parent": parent,
                            "desc": (obj.get("desc") or "").strip(),
                            "tags": list(obj.get("tags") or ["public"]),
                            "travel_min": int(obj.get("travel_min") or 5),
                            "lore_ids": [],
                        }
                        target_loc = nid
                        dec["user_move_to"] = nid
                    created = True
                except Exception:
                    created = False
            if not created:
                hint = (dec.get("scene_place_hint") or "").strip()
                if hint:
                    nid = rpformat.slug(hint) or "dia_diem_moi"
                    base, k = nid, 2
                    while nid in locs:
                        nid, k = f"{base}_{k}", k + 1
                    is_priv = bool(_PRIVATE_PLACE_RE.search(hint))
                    locs[nid] = {
                        "name": hint,
                        "parent": locs.get(user_loc, {}).get("parent"),
                        "desc": f"{hint} trong cảnh hiện tại.",
                        "tags": ["indoor", "private_possible"] if is_priv else ["public"],
                        "travel_min": 0,
                        "lore_ids": [],
                    }
                    target_loc = nid
                    dec["user_move_to"] = nid
                else:
                    target_loc = "stay"
                    dec["user_move_to"] = "stay"

    # 2. Di chuyen user + moving_companions (+ Case #8 In-Transit)
    travel_min = 0
    if target_loc not in ("stay", None) and target_loc in locs and target_loc != user_loc:
        if dec.get("move_status") == "in_transit":
            new_state["entities"]["user"]["transit"] = {"from": user_loc, "to": target_loc}
        else:
            new_state["entities"]["user"].pop("transit", None)
        new_state["entities"]["user"]["location_id"] = target_loc
        for cid in dec.get("moving_companions") or []:
            if cid in new_state["entities"]:
                new_state["entities"][cid]["location_id"] = target_loc
                new_state["entities"][cid].pop("at_door", None)
        travel_min = 0 if old_user_loc == "boi_canh_chinh" else int(locs[target_loc].get("travel_min") or 0)
        user_loc = target_loc
    elif dec.get("move_status") != "in_transit":
        new_state["entities"]["user"].pop("transit", None)

    # 3. Dong ho Pre-Render + Case #11 Reset stale NPC khi qua ngay / tua >= 3h
    old_clock = copy.deepcopy(new_state["clock"])
    td = dec.get("user_time_delta", "instant")
    if td == "explicit_time" and isinstance(dec.get("explicit_time_override"), dict):
        new_state["clock"] = rpformat.apply_time_delta(new_state["clock"], "explicit_time", explicit_obj=dec["explicit_time_override"])
    elif td == "explicit_time" and api_key and last_user:
        try:
            sys_p = prompts.get_text("post_render.explicit_time_extractor_sys")
            cur_clk = rpformat.format_clock(new_state["clock"])
            obj, c = extract_json(api_key, extract_model, sys_p, f"Hiện tại: {cur_clk}\nTin người chơi: {last_user}", max_tokens=200)
            extra_cost += c
            new_state["clock"] = rpformat.apply_time_delta(new_state["clock"], "explicit_time", explicit_obj=obj)
        except Exception:
            new_state["clock"] = rpformat.apply_time_delta(new_state["clock"], "brief", travel_min=travel_min)
    else:
        new_state["clock"] = rpformat.apply_time_delta(new_state["clock"], td, travel_min=travel_min)

    rpformat.reset_stale_npc_locations(
        pack, new_state, old_clock, new_state["clock"],
        excluded_ids=list(dec.get("moving_companions") or []),
    )

    # 4. Dong bo arriving_speaker & Case #9 Doorway Threshold (at_door)
    sp = dec.get("speaker")
    at_door_now = []
    if ch_type == "in_person":
        door_opened = last_is_user and _is_door_opened_text(last_user)
        door_knock = last_is_user and _is_at_door_text(last_user) and not door_opened
        if door_opened:
            for cid, ent in new_state["entities"].items():
                if cid != "user" and ent.get("at_door") == user_loc:
                    ent.pop("at_door", None)
                    ent["location_id"] = user_loc
                    ent["entered_turn"] = len(history)
        arriving_cids = list(dict.fromkeys(list(dec.get("seen_physical") or []) + ([sp] if sp in new_state["entities"] and sp != "user" else [])))
        for cid in arriving_cids:
            if cid not in new_state["entities"]:
                continue
            ent = new_state["entities"][cid]
            if ent.get("location_id") != user_loc:
                if door_knock:
                    ent["at_door"] = user_loc
                    at_door_now.append(cid)
                else:
                    ent.pop("at_door", None)
                    ent["location_id"] = user_loc
                    ent["entered_turn"] = len(history)
                    if cid == sp:
                        dec["entering"] = True
        for cid, ent in new_state["entities"].items():
            if cid != "user" and ent.get("at_door") == user_loc and cid not in at_door_now:
                at_door_now.append(cid)

    # 5. Chot active_channel (+ Case #13 medium & speakerphone)
    co_npcs = rpformat.co_located_npcs(pack, new_state)
    comm_medium = dec.get("comm_medium") or ("spoken" if ch_type == "in_person" else "texting")
    if ch_type == "in_person":
        participants = ["user"] + list(dict.fromkeys(co_npcs + at_door_now))
    elif ch_type == "dm_text":
        target = sp if sp in pack.get("characters", {}) else dec.get("addressee")
        participants = ["user"] + ([target] if target in pack.get("characters", {}) else [])
        if dec.get("speakerphone"):
            for cid in co_npcs:
                if cid not in participants:
                    participants.append(cid)
    else:  # group_text
        prev_p = [c for c in (new_state.get("active_channel") or {}).get("participants", []) if c in pack.get("characters", {})]
        mentioned_p = [cid for cid, c in cast_by_id(pack).items() if mentioned(c, last_user)]
        if sp in pack.get("characters", {}) and sp not in prev_p:
            prev_p.append(sp)
        if dec.get("addressee") in pack.get("characters", {}) and dec["addressee"] not in prev_p:
            prev_p.append(dec["addressee"])
        for mp in mentioned_p:
            if mp not in prev_p:
                prev_p.append(mp)
        if not prev_p:
            prev_p = list((pack.get("characters") or {}).keys())
        participants = ["user"] + prev_p
        if dec.get("speakerphone"):
            for cid in co_npcs:
                if cid not in participants:
                    participants.append(cid)

    new_state["active_channel"] = {
        "channel_type": ch_type,
        "medium": comm_medium,
        "participants": participants,
        "group_id": "group_default" if ch_type == "group_text" else None,
    }
    new_state["owed"] = list(dec.get("owed") or [])

    if dec.get("scene_vibe") in SCENE_VIBES:
        new_state["scene_vibe"] = dec["scene_vibe"]
        new_state["tension"] = VIBE_TO_TENSION.get(dec["scene_vibe"], int(new_state.get("tension", 1)))

    # 6. Khoa xung ho doi xung (Reciprocal Pronoun Locking) — Case #20 bo qua khi noi voi ca nhom
    if not dec.get("is_group_address"):
        pair_key = dec.get("user_pronoun_pair")
        lock_cid = dec.get("pronoun_lock_target")
        if not pair_key and sp in (pack.get("characters") or {}) and not is_pronoun_locked(new_state, sp) and last_user:
            det_pair = rpformat.detect_user_pronoun_pair(last_user, pack, sp)
            if det_pair:
                pair_key, lock_cid = det_pair, sp
        if pair_key and lock_cid in (pack.get("characters") or {}) and not is_pronoun_locked(new_state, lock_cid):
            ug = rpformat.gender_of(pack, "user")
            cg = rpformat.gender_of(pack, lock_cid)
            recip = rpformat.infer_reciprocal(pair_key, ug, cg)
            if recip:
                u_pair, c_pair = recip
                edges = new_state.setdefault("relationships", {}).setdefault("edges", {})
                raw_world_edges = (pack.get("relationships") or {}).get("edges") or {}
                u2c = edges.setdefault("user", {}).get(lock_cid) or rpformat._resolve_sparse_edge(pack, raw_world_edges, "user", lock_cid)
                if u2c.get("lock") != "manual":
                    u2c["default"] = {**(u2c.get("default") or {}), "self": u_pair["self"], "call": u_pair["call"]}
                    u2c["is_fallback"] = False
                    u2c["lock"] = "observed"
                    edges["user"][lock_cid] = u2c
                c2u = edges.setdefault(lock_cid, {}).get("user") or rpformat._resolve_sparse_edge(pack, raw_world_edges, lock_cid, "user")
                if should_reciprocate(pack, new_state, lock_cid) and c2u.get("lock") != "manual":
                    c2u["default"] = {**(c2u.get("default") or {}), "self": c_pair["self"], "call": c_pair["call"]}
                    ov = dict(c2u.get("overrides") or {})
                    ov["private"] = {"self": c_pair["self"], "call": c_pair["call"]}
                    ov["public_formal"] = {"self": c_pair["self"], "call": c_pair["call"]}
                    ov["tense"] = {"self": c_pair["self"], "call": c_pair["call"]}
                    c2u["overrides"] = ov
                    c2u["is_fallback"] = False
                    c2u["lock"] = "observed"
                else:
                    c2u["lock"] = c2u.get("lock") or "manual"
                edges[lock_cid]["user"] = c2u

    return rpformat.sync_legacy_state(pack, new_state), extra_cost


# ----------------------------------------------------------------------------- Pha 2: Context Compiler 4 Zone
def _is_init_bg_fact(pack, fact):
    if fact.get("status") == "secret":
        return False
    init_ids = {f.get("id") for f in ((pack or {}).get("initial_facts") or []) if isinstance(f, dict) and f.get("id")}
    fid = str(fact.get("id") or "")
    ftime = str(fact.get("time") or "").strip().lower()
    return bool(
        (fid and fid in init_ids)
        or fid.startswith(("init_", "fact_init_"))
        or ftime in ("trước mở đầu", "lúc mở đầu", "mở đầu")
    )


def _filter_hard_memory(pack, state, speaker, scan_text, ch_participants, turn_count=0):
    co_ids = ["user"] + rpformat.co_located_npcs(pack, state)
    vis_ids = list(dict.fromkeys(co_ids + [p for p in (ch_participants or []) if p]))
    out = []
    for fact in state.get("hard_memory") or []:
        if fact.get("status") == "superseded":
            continue
        kb = fact.get("known_by") or []
        if "all" not in kb:
            if speaker in (NARRATOR, SCENE_SPEAKER):
                if not any(e in kb for e in vis_ids):
                    continue
            elif speaker not in kb:
                continue
        status = fact.get("status", "active")
        kw_hit = _match_keywords(fact.get("keywords") or [], scan_text)
        if _is_init_bg_fact(pack, fact) and turn_count > 2 and not kw_hit:
            continue
        if status == "resolved" and not kw_hit:
            continue
        ent_hit = any(e in ch_participants for e in (fact.get("entities") or []))
        score = (3 if kw_hit else 0) + (2 if ent_hit else 0) + (1 if status == "secret" else 0)
        out.append((score, fact))
    out.sort(key=lambda x: -x[0])
    return [f for _, f in out[:8]]


_TEXTING_LORE_RE = re.compile(r"(?i)\b(?:texting\s+(?:style|mode)|chế\s+độ\s+nhắn\s+tin|phong\s+cách\s+nhắn\s+tin|burst-messaging)\b")
_SPOKEN_LORE_RE = re.compile(r"(?i)\b(?:spoken\s+(?:style|mode)|chế\s+độ\s+nói\s+chuyện\s+trực\s+tiếp|phong\s+cách\s+nói)\b")
_CHAR_SHEET_LORE_RE = re.compile(r"(?i)(?:nhân\s+vật\s+chính\s*:|core\s+persona|mode\s+[12]\s*:|phong\s+cách\s+(?:nói|nhắn\s+tin)|chế\s+độ\s+(?:nói|nhắn\s+tin)|hồ\s+sơ\s+nhân\s+vật)")
_SITUATIONAL_FETISH_RE = re.compile(
    r"(?i)\b(?:bdsm|bondage|impact\s+play|pain\s+play|humiliation|làm\s+nhục|verbal\s+degradation|orgasm\s+control|"
    r"safeword|aftercare|ntr|netorare|cuckold|cheating|ngoại\s+tình|anal\s+sex|đụ\s+lỗ\s+đít|missionary|"
    r"tư\s+thế\s+truyền\s+thống|doggy|cowgirl|cưỡi\s+ngựa|tư\s+thế\s+đứng|handjob|sục\s+cặc|blowjob|oral\s+sex|"
    r"bú\s+cặc|group\s*/\s*shared|gangbang|double\s+penetration|public\s+sex|exhibitionism|"
    r"dưới\s+gầm\s+bàn|nhà\s+vệ\s+sinh\s+trường|phòng\s+giáo\s+viên)\b"
)
_GENERAL_NSFW_GLOSSARY_RE = re.compile(r"(?i)\b(?:thuật\s+ngữ\s+làm\s+tình|quy\s+chuẩn\s+bộ\s+phận\s+sinh\s+dục)\b")
_CLOTHING_STATE_LORE_RE = re.compile(
    r"(?i)\b(?:quy\s+cách\s+mặc\s+đồ|hở\s+eo|midriff|hở\s+nửa\s+dưới\s+ngực|underboob|"
    r"hở\s+nửa\s+trên\s+mông|trạng\s+thái\s+lộ\s+nội\s+y|váy\s+liền\s+thân)\b"
)
_CLOTHING_TRIGGER_WORDS = [
    "váy", "đầm", "áo", "quần", "nội y", "áo ngực", "quần lót", "bra", "panty",
    "cởi", "kéo", "lộ", "hở", "tụt", "bung cúc", "xộc xệch", "ngực", "vú", "eo", "mông", "tắm", "khăn",
]
_VIBE_MODULE_RE = re.compile(r"(?i)(?:sexual|intimate|nsfw|secret|bí\s*mật|thỏa\s*thuận|nhục\s*cảm|thân\s*mật)")


def _sanitize_z4_text(txt):
    return str(txt or "").replace("TUYỆT ĐỐI", "Tuyệt đối")


def _is_mode_mismatched_lore(title, content, mode_key):
    head = f"{title or ''}\n{(content or '')[:220]}"
    if mode_key == "in_person" and _TEXTING_LORE_RE.search(head) and not _SPOKEN_LORE_RE.search(title or ""):
        return True
    if mode_key == "texting" and _SPOKEN_LORE_RE.search(head) and not _TEXTING_LORE_RE.search(title or ""):
        return True
    return False


def _classify_char_lore_owner(title, content, sp, cast, active_cids=None):
    first_line = next((l.strip() for l in (content or "").splitlines() if l.strip()), "")
    header_txt = f"{title or ''}\n{first_line}"
    head = f"{title or ''}\n{(content or '')[:260]}"
    if not (_CHAR_SHEET_LORE_RE.search(head) or re.search(r"\[[^\]]{3,80}\]", head)):
        return "world"

    def _matched_in(txt):
        hits = []
        allow_short = bool(re.search(r"(?i)(?:nhân\s+vật\s+chính|mode\s+[12]|phong\s+cách|chế\s+độ|hồ\s+sơ|\[)", txt))
        for cid, c in cast.items():
            terms = [c["name"]] + [a for a in (c.get("aliases") or []) if len(str(a).strip()) >= 3]
            if allow_short:
                last_tok = (c["name"] or "").split()[-1]
                if len(last_tok) >= 2:
                    terms.append(last_tok)
            if _match_keywords(terms, txt):
                hits.append(cid)
        return hits

    matched = _matched_in(header_txt) or _matched_in(head)
    if sp in cast and sp in matched and len(matched) == 1:
        return "speaker"
    if active_cids:
        act_set = set(active_cids)
        if matched:
            return "speaker" if any(c in act_set for c in matched) else "foreign"
        return "world"
    if matched and sp not in matched:
        return "foreign"
    return "world"


def _is_redundant_with_profile(lore_txt, profile_txt):
    if not lore_txt or not profile_txt:
        return False
    prof_low = re.sub(r"\s+", " ", profile_txt).strip().lower()
    norm_lore = re.sub(r"\s+", " ", lore_txt).strip().lower()
    if len(norm_lore) >= 40 and norm_lore[:120] in prof_low:
        return True
    long_lines = [
        re.sub(r"\s+", " ", l).strip(" -*\t").lower()
        for l in lore_txt.splitlines()
        if len(l.strip(" -*\t")) >= 25
    ]
    if not long_lines:
        return False
    matched = sum(1 for l in long_lines if l in prof_low)
    return (matched / len(long_lines)) >= 0.65


def _build_char_profile(cid, pack, state, history, dec, chars, user, mode_key, scan_text, vibe_key, active_mod_ids=None, compact=False):
    """Xay dung khoi ho so Zone 4 cho 1 nhan vat (tai su dung cho ca che do 1-nguoi va Toan canh)."""
    c_v2 = chars[cid]
    cname = c_v2["meta"]["name"]
    c_cast = next((x for x in (pack.get("cast") or []) if x["char_id"] == cid), {})
    sp_ent = (state.get("entities") or {}).get(cid) or {}
    pers = ", ".join(c_v2["core"]["personality"]) if isinstance(c_v2["core"]["personality"], list) else str(c_v2["core"]["personality"])
    pers = rpformat.fill_placeholders(pers, user, cname)
    voice = c_v2.get("voice") or {}
    mode_obj = (voice.get("modes") or {}).get(mode_key) or (voice.get("modes") or {}).get("in_person") or {}
    sample_lim = 3 if compact else 5
    sp_samples = [rpformat.fill_placeholders(s, user, cname) for s in (mode_obj.get("samples") or [])[:sample_lim] if str(s).strip()]

    prof_lines = [
        f"- Nhân vật: {cname} ({c_v2['meta'].get('age', '?')} tuổi, {c_v2['meta'].get('gender', '')})",
        f"- Vai trò trong thế giới: {rpformat.fill_placeholders(c_cast.get('role_in_world', ''), user, cname)}",
    ]
    if (c_cast.get("goal") or "").strip():
        prof_lines.append(
            "- Việc nền / mối bận tâm lúc rảnh (chỉ nhắc tới khi câu chuyện đang xoay quanh chủ đề này; "
            "phải lập tức gác lại khi có biến cố hoặc tương tác trực tiếp ngay trước mắt): "
            + rpformat.fill_placeholders(c_cast.get("goal", ""), user, cname)
        )
    prof_lines.append(f"- Tính cách: {pers}")
    if c_v2["core"].get("character_sheet"):
        sheet_lim = 1500 if compact else 3000
        sheet = rpformat.fill_placeholders(c_v2["core"]["character_sheet"], user, cname)[:sheet_lim]
        prof_lines.append(f"- Hồ sơ chi tiết nhân vật:\n{_sanitize_z4_text(sheet)}")
    if c_v2["core"].get("values"):
        prof_lines.append(f"- Giá trị sống: {rpformat.fill_placeholders(c_v2['core']['values'], user, cname)}")
    prof_lines.append(f"- Tâm trạng hiện tại: {MOODS.get(sp_ent.get('mood', 'normal'), 'Bình thường')}")
    tone_txt = rpformat.fill_placeholders(voice.get("tone", ""), user, cname)
    tone_txt = re.sub(
        r"(?i)\b(?:xưng\s+hô(?:\s+với\s+[^.:;]+)?|xưng)\s*:?\s*(?:tôi|tui|tao|em|anh|chị|mình|tớ)\s*[-–/]\s*(?:cậu|bạn|ông|bà|mày|em|anh|chị)[^.;]*[.;]?",
        "",
        tone_txt,
    )
    tone_txt = re.sub(r"\s{2,}", " ", tone_txt).strip()
    prof_lines.append(f"- Giọng & Phương ngữ ({voice.get('dialect', '')}): {tone_txt}")
    prof_lines.append(f"- Chế độ [{mode_key}]: {mode_obj.get('style', '')}")
    if mode_obj.get("tics"):
        prof_lines.append("- Câu cửa miệng (dùng thỉnh thoảng): " + " / ".join(mode_obj["tics"]))
    if sp_samples:
        prof_lines.append(
            prompts.get_text("writer.samples_header") + "\n"
            + "\n".join(f"  {s}" for s in sp_samples)
        )
    if c_v2["core"].get("boundaries"):
        prof_lines.append("- Giới hạn cứng: " + "; ".join(_sanitize_z4_text(rpformat.fill_placeholders(b, user, cname)) for b in c_v2["core"]["boundaries"]))

    app = c_v2.get("appearance") or {}
    seen_phys = set(dec.get("seen_physical") or [])
    newly_entered = (len(history) - int(sp_ent.get("entered_turn", 0))) <= 2 or dec.get("entering") or (cid in seen_phys)
    app_hit = _match_keywords(["mặc", "áo", "tóc", "mặt", "ngoại hình", "đồ"], scan_text)
    if app.get("summary") and (newly_entered or app_hit or len(history) < 2):
        outfit_key = sp_ent.get("outfit", "default")
        outfit_desc = (app.get("outfits") or {}).get(outfit_key, "")
        app_txt = rpformat.fill_placeholders(app["summary"], user, cname)
        prof_lines.append(f"- Ngoại hình & Trang phục: {app_txt}" + (f" Đang mặc: {outfit_desc}" if outfit_desc else ""))

    mod_ids = set(active_mod_ids if active_mod_ids is not None else (dec.get("active_modules") or []))
    for mod in c_v2.get("modules") or []:
        vibe_mod_hit = (
            vibe_key in ("sensual", "intimate", "dramatic")
            and bool(_VIBE_MODULE_RE.search(f"{mod.get('id', '')} {mod.get('type', '')} {mod.get('when', '')} {' '.join(mod.get('keywords') or [])}"))
        )
        if mod["id"] in mod_ids or _match_keywords(mod.get("keywords") or [], scan_text) or vibe_mod_hit:
            m_txt = rpformat.fill_placeholders(mod["content"], user, cname)
            prof_lines.append(f"- [{mod.get('type', 'module').upper()}]: {_sanitize_z4_text(m_txt)}")
    return cname, prof_lines


def compile_context_4zones(pack, state, history, dec, active_lore=None):
    """Pha 2 (0ms, $0): Bien dich Context Window 4 Zone toi uu KV Cache & Recency Bias."""
    history = rp_history(history)
    state = _ensure_v2_state(pack, state)
    chars = pack.get("characters") or {}
    cast = cast_by_id(pack)
    user = user_name_of(pack)
    sp = dec["speaker"]
    ch = state.get("active_channel") or {}
    ch_type = ch.get("channel_type", "in_person")
    ch_participants = ch.get("participants") or (["user"] + rpformat.co_located_npcs(pack, state))
    locs = state.get("locations") or pack.get("locations") or {}
    user_loc = ((state.get("entities") or {}).get("user") or {}).get("location_id") or state.get("dia_diem")

    # ---------------- ZONE 1 — TINH 100% (System Core + World Rules + User + Cast Roster 1 dong + Constant Lore)
    rules = pack.get("rules") or {}
    world_rules = "\n".join(f"- {r}" for r in (rules.get("world_rules") or []))
    roster_lines = []
    for c_entry in pack.get("cast") or []:
        cid = c_entry["char_id"]
        c_v2 = chars.get(cid)
        if not c_v2:
            continue
        cname = c_v2["meta"]["name"]
        aliases = ", ".join(c_v2["meta"].get("aliases") or [])
        one_l = rpformat.fill_placeholders(c_v2["core"]["one_liner"], user, cname)
        role_w = rpformat.fill_placeholders(c_entry.get("role_in_world", ""), user, cname)
        roster_lines.append(
            f"- {cid} | {cname} (gọi: {aliases}): {one_l} [Vai trò: {role_w}]"
        )
    const_lore = [
        f"- {e['title']}: {e['content']}"
        for e in (pack.get("lore") or []) if e.get("activation") == "constant"
    ]
    z1_parts = [
        "[QUY TẮC CỐT LÕI]\n" + prompts.render("writer.core_rules", world_name=pack["meta"].get("name", "Thế giới")),
        f"[VĂN PHONG & LUẬT THẾ GIỚI]\n{rules.get('writing_style', '')}" + (f"\n{world_rules}" if world_rules else ""),
        f"[NGƯỜI CHƠI]\n{user}: {(pack.get('user') or {}).get('persona', '')}",
        "[DÀN NHÂN VẬT (ROSTER 1 DÒNG — không lẫn tính cách)]\n" + ("\n".join(roster_lines) or "(chưa có)"),
    ]
    if const_lore:
        z1_parts.append("[LORE THẾ GIỚI CỐ ĐỊNH]\n" + "\n".join(const_lore))
    zone1 = "\n\n".join(p for p in z1_parts if p.strip())

    # ---------------- ZONE 2 — BAN TINH + ZONE 3 — SAWTOOTH WORKING MEMORY (8 -> 16 luot)
    upto = int(state.get("summarized_upto", 0))
    working = history[upto:]
    z2_parts = []
    if (state.get("story_arc") or "").strip():
        z2_parts.append(f"[ĐẠI CƯƠNG CỐT TRUYỆN XA]\n{state['story_arc'].strip()}")
    if state.get("recent_scenes"):
        sc_txt = "\n\n".join(sc if isinstance(sc, str) else sc.get("text", "") for sc in state["recent_scenes"])
        z2_parts.append(f"[TÓM TẮT CÁC CẢNH GẦN ĐÂY]\n{sc_txt}")

    z3_lines = []
    opening_nar = (pack.get("opening") or {}).get("narration", "")
    if upto == 0 and opening_nar:
        z3_lines.append(f"[MỞ ĐẦU]\n{opening_nar}")
    for idx, m in enumerate(working):
        tid = m.get("turn_id", upto + idx + 1)
        who = user if m["speaker"] == "user" else name_of(pack, m["speaker"])
        mch = m.get("channel_type")
        ch_badge = " [Nhắn riêng]" if mch == "dm_text" else (" [Nhóm chat]" if mch == "group_text" else "")
        z3_lines.append(f"[Lượt #{tid}{ch_badge}] {who}: {m.get('content', '')}")

    zone2_3 = "\n\n".join(
        ([p for p in z2_parts if p])
        + ["[DIỄN BIẾN HỘI THOẠI NGUYÊN VĂN — WORKING MEMORY]\n" + ("\n\n".join(z3_lines) or "(Chưa có hội thoại)")]
    )

    # ---------------- ZONE 4 — DONG MOI LUOT (Recency Bias toi da)
    # 1. World State Snapshot
    z4_state = f"[TRẠNG THÁI THẾ GIỚI HIỆN TẠI]\n{state_text(pack, state)}"

    comm_medium = ch.get("medium") or ("spoken" if ch_type == "in_person" else "texting")
    mode_key = "in_person" if (ch_type == "in_person" or comm_medium == "spoken") else "texting"
    vibe_key, vibe_label, vibe_directive = resolve_scene_vibe(state)
    scan_text = "\n".join(m.get("content", "") for m in working[-4:])
    hot_text = "\n".join(m.get("content", "") for m in working[-2:])
    sp_name = name_of(pack, sp)
    is_scene_all = (sp == SCENE_SPEAKER)
    scene_cids = []
    if is_scene_all:
        scene_cids = [c for c in ch_participants if c in chars]
        if not scene_cids and ch_type == "in_person":
            scene_cids = [c for c in rpformat.co_located_npcs(pack, state) if c in chars]
    scene_cids = scene_cids[:6]
    scene_names = ", ".join(name_of(pack, c) for c in scene_cids)
    if is_scene_all and scene_names:
        sp_name = scene_names

    # 2. Active Speaker Profile (xay truoc de loai lore trung lap va gan bo sung lore rieng cua speaker)
    if sp in chars:
        cname, prof_lines = _build_char_profile(
            sp, pack, state, history, dec, chars, user, mode_key, scan_text, vibe_key,
            active_mod_ids=set(dec.get("active_modules") or []), compact=False,
        )
        z4_speaker = f"[HỒ SƠ NHÂN VẬT LÊN TIẾNG — {cname.upper()}]\n" + "\n".join(prof_lines)
    elif is_scene_all and scene_cids:
        sc_mods_map = dec.get("scene_modules") or {}
        flat_mods = set(dec.get("active_modules") or [])
        blocks = []
        compact_flag = len(scene_cids) >= 3
        for cid in scene_cids:
            m_ids = set(sc_mods_map.get(cid) or []) | flat_mods
            cname, prof_lines = _build_char_profile(
                cid, pack, state, history, dec, chars, user, mode_key, scan_text, vibe_key,
                active_mod_ids=m_ids, compact=compact_flag,
            )
            blocks.append(f"### {cname}\n" + "\n".join(prof_lines))
        z4_speaker = f"[HỒ SƠ CÁC NHÂN VẬT THAM GIA CẢNH ({len(scene_cids)})]\n" + "\n\n".join(blocks)
    else:
        present_ids = rpformat.co_located_npcs(pack, state)
        speech_notes = [
            rpformat.speech_line({"id": cid, "v2": chars[cid]}, present_ids, user, state=state, pack=pack)
            for cid in present_ids if cid in chars
        ]
        n_lines = [prompts.get_text("writer.narrator_role")]
        if speech_notes:
            n_lines.append("Tóm tắt giọng/xưng hô các NPC đang có mặt (nếu xuất hiện thoáng qua):\n" + "\n".join(speech_notes))
        z4_speaker = "[HỒ SƠ NGƯỜI DẪN TRUYỆN]\n" + "\n".join(n_lines)

    # Smart Lore Filtering & Ranking (chong phinh Zone 4 & loai nhieu nhan vat/che do khac)
    loc_lore_ids = set(rpformat.inherited_lore_ids(locs, user_loc))
    dir_lore_ids = set(dec.get("lore_used") or [])
    cand_entries = []
    for e in pack.get("lore") or []:
        if e.get("activation") == "constant":
            continue
        hit = (
            (e["id"] in loc_lore_ids)
            or (e["id"] in dir_lore_ids)
            or (e.get("activation") == "keyword" and _match_keywords(e.get("keys") or [], scan_text))
        )
        if hit:
            cand_entries.append({
                "id": e.get("id", ""),
                "title": e.get("title") or "Lore",
                "keys": e.get("keys") or [],
                "content": e.get("content") or "",
            })
    if active_lore:
        for e in active_lore:
            cand_entries.append({
                "id": e.get("uid", e.get("id", "")),
                "title": e.get("comment") or e.get("title") or "Lore",
                "keys": e.get("keys") or [],
                "content": e.get("content") or "",
            })

    seen_lore_contents = set()
    speaker_extra_lines = []
    speaker_extra_chars = 0
    world_candidates = []
    for entry in cand_entries:
        c_txt = rpformat.fill_placeholders(entry["content"], user, sp_name).strip()
        if not c_txt or c_txt in seen_lore_contents:
            continue
        seen_lore_contents.add(c_txt)
        title = entry["title"]
        keys = entry["keys"]
        eid = entry["id"]

        if _is_mode_mismatched_lore(title, c_txt, mode_key):
            continue
        owner = _classify_char_lore_owner(title, c_txt, sp, cast, active_cids=(scene_cids if is_scene_all else None))
        if owner == "foreign" and (sp in chars or (is_scene_all and scene_cids)):
            continue
        if _is_redundant_with_profile(c_txt, z4_speaker):
            continue
        if owner == "speaker" and (sp in chars or (is_scene_all and scene_cids)):
            extra_line = _sanitize_z4_text(f"- [Bổ sung hồ sơ] {title}: {c_txt}" if is_scene_all else f"- [Bổ sung hồ sơ {sp_name}] {title}: {c_txt}")
            if speaker_extra_chars == 0 or speaker_extra_chars + len(extra_line) <= 2500:
                clipped = extra_line if len(extra_line) <= 2500 else extra_line[:2497] + "..."
                speaker_extra_lines.append(clipped)
                speaker_extra_chars += len(clipped)
            continue

        head = f"{title}\n{c_txt[:180]}"
        is_clothing = False
        if _SITUATIONAL_FETISH_RE.search(head):
            fetish_terms = _SITUATIONAL_FETISH_RE.findall(head) + [
                s.strip() for s in re.split(r"[:,/\-()&]+", title) if len(s.strip()) >= 4
            ]
            if not (_match_keywords(keys, scan_text) or _match_keywords(fetish_terms, scan_text)):
                continue
        elif _GENERAL_NSFW_GLOSSARY_RE.search(head):
            if not (vibe_key in ("sensual", "intimate") or _INTIMATE_VIBE_RE.search(scan_text) or _SENSUAL_VIBE_RE.search(scan_text) or _match_keywords(keys, scan_text)):
                continue
        elif _CLOTHING_STATE_LORE_RE.search(head):
            if not (vibe_key in ("sensual", "intimate") or _match_keywords(keys or _CLOTHING_TRIGGER_WORDS, scan_text)):
                continue
            is_clothing = True

        score = 100 if (eid in loc_lore_ids or eid in dir_lore_ids) else 0
        t_segs = [s.strip() for s in re.split(r"[:,/\-()&]+", title) if len(s.strip()) >= 4]
        if _match_keywords(keys, hot_text) or _match_keywords(t_segs, hot_text):
            score += 60
        elif _match_keywords(keys, scan_text):
            score += 35
        else:
            score += 10
        world_candidates.append((score, is_clothing, _sanitize_z4_text(f"- {title}: {c_txt}")))

    if speaker_extra_lines:
        z4_speaker += "\n" + "\n".join(speaker_extra_lines)

    world_candidates.sort(key=lambda item: -item[0])
    dyn_lore_lines = []
    lore_chars = 0
    clothing_count = 0
    for _, is_clothing, line in world_candidates:
        if is_clothing and clothing_count >= 2:
            continue
        if len(dyn_lore_lines) >= MAX_Z4_LORE_ENTRIES:
            break
        if lore_chars + len(line) > MAX_Z4_LORE_CHARS and dyn_lore_lines:
            continue
        dyn_lore_lines.append(line)
        lore_chars += len(line)
        if is_clothing:
            clothing_count += 1

    z4_lore = ""
    if dyn_lore_lines:
        z4_lore = (
            "[BỐI CẢNH & LOREBOOK THẾ GIỚI]\n"
            + prompts.render("writer.lore_note", speaker_name=sp_name) + "\n"
            + "\n".join(dyn_lore_lines)
        )

    # 3. Filtered Hard Memory (known_by)
    hm = _filter_hard_memory(pack, state, sp, scan_text, ch_participants, turn_count=len(history))
    if hm:
        hm_lines = []
        for f in hm:
            if f.get("status") == "secret":
                kb_hint = ""
                if is_scene_all:
                    holders = [name_of(pack, k) for k in (f.get("known_by") or []) if k in chars]
                    if holders:
                        kb_hint = f" · chỉ {', '.join(holders)} biết"
                tag = f"[BÍ MẬT CHƯA LỘ{kb_hint}" + (f" — chỉ lộ khi: {f.get('reveal_when')}" if f.get("reveal_when") else "") + "]"
            elif _is_init_bg_fact(pack, f):
                tag = "[VIỆC NỀN LÚC MỞ ĐẦU — tạm gác lại nếu đang có tương tác khác]"
            else:
                tag = "[SỰ THẬT]"
            hm_lines.append(f"- {tag} {rpformat.fill_placeholders(f['text'], user, sp_name)}")
        z4_mem = "[TRÍ NHỚ CỨNG & BÍ MẬT ĐƯỢC PHÉP BIẾT]\n" + "\n".join(hm_lines)
    else:
        z4_mem = ""

    # 4. Pronouns & Relationships
    if sp in chars:
        pr = rpformat.resolve_pronouns(pack, state, sp, ch_participants, focus_id=focus_target(history, sp))
        z4_pronouns = "[XƯNG HÔ LƯỢT NÀY]\n" + ("\n".join(pr["lines"]) or "- Dùng xưng hô tự nhiên theo vai.")
    elif is_scene_all and scene_cids:
        sc_pr_lines = []
        for cid in scene_cids:
            pr_c = rpformat.resolve_pronouns(pack, state, cid, ch_participants, focus_id=focus_target(history, cid))
            c_lines = pr_c.get("lines") or []
            if c_lines:
                sc_pr_lines.append(f"* {name_of(pack, cid)}:")
                sc_pr_lines.extend(f"  {l}" for l in c_lines)
        z4_pronouns = "[XƯNG HÔ LƯỢT NÀY]\n" + ("\n".join(sc_pr_lines) or "- Mỗi nhân vật dùng xưng hô tự nhiên theo vai.")
    else:
        pr = {"summary_text": "", "tier": "default"}
        z4_pronouns = "[XƯNG HÔ LƯỢT NÀY]\n- Ngôi thứ ba khách quan của người dẫn truyện."

    # 5. Stage Directions & Witnesses Anti-Metagaming Boundary
    dir_lines = [f"Loại lượt: {dec.get('turn_type_label', '')}."]
    if ch_type in ("dm_text", "group_text"):
        if comm_medium == "spoken":
            dir_lines.append(prompts.render("writer.channel_voice_call", ch_type=ch_type))
        else:
            dir_lines.append(prompts.render("writer.channel_texting", ch_type=ch_type))
    else:
        dir_lines.append(prompts.get_text("writer.channel_in_person"))

    if sp in chars:
        other_main = [name_of(pack, c) for c in ch_participants if c not in ("user", sp)]
        other_suffix = f" ({', '.join(other_main)})" if other_main else ""
        dir_lines.append(prompts.render("writer.stay_in_character", speaker_name=sp_name, other_main_suffix=other_suffix))
        if dec.get("entering"):
            dir_lines.append(f"{sp_name} vừa mới bước vào cảnh ở lượt này: mở đầu tự nhiên bằng sự xuất hiện đó.")
        # Case #19: Khi co nguoi thu 3 vua vao giua canh rieng tu (private -> public dot ngot)
        co_npcs_now = rpformat.co_located_npcs(pack, state)
        if ch_type == "in_person" and len(co_npcs_now) >= 2:
            just_entered_other = any(
                other_c != sp and (
                    other_c in (dec.get("seen_physical") or [])
                    or (len(history) - int(((state.get("entities") or {}).get(other_c) or {}).get("entered_turn", -99))) <= 1
                )
                for other_c in co_npcs_now
            )
            sp_u_edge = (((state.get("relationships") or {}).get("edges") or {}).get(sp) or {}).get("user") or {}
            ov_priv = (sp_u_edge.get("overrides") or {}).get("private") or {}
            dflt_pair = sp_u_edge.get("default") or {}
            ov_pub = (sp_u_edge.get("overrides") or {}).get("public_formal") or dflt_pair
            if just_entered_other and ov_priv and (ov_priv.get("self"), ov_priv.get("call")) != (ov_pub.get("self"), ov_pub.get("call")):
                dir_lines.append("(Lưu ý diễn xuất: Có người khác vừa bước vào nên không còn ở riêng tư nữa — hãy chuyển sang xưng hô công khai một cách tự nhiên, khéo giữ ý.)")
        # Case #21: Khi User co tinh be xung ho da khoa
        if dec.get("pronoun_break"):
            dir_lines.append(f"(Lưu ý diễn xuất: Bình thường {user} dùng cách xưng hô đã khóa, nhưng ở tin vừa rồi {user} bất ngờ đổi sang kiểu '{dec['pronoun_break']}' — {sp_name} hãy phản ứng tự nhiên trước sự đổi tông giọng này.)")
    elif is_scene_all and scene_cids:
        absent_names = [name_of(pack, c["char_id"]) for c in (pack.get("cast") or []) if c["char_id"] not in scene_cids]
        absent_suffix = f" ({', '.join(absent_names)})" if absent_names else ""
        dir_lines.append(prompts.render("writer.stay_in_scene", scene_names=scene_names, absent_suffix=absent_suffix))
        seen_now = [name_of(pack, c) for c in (dec.get("seen_physical") or []) if c in scene_cids]
        if seen_now and ch_type == "in_person":
            dir_lines.append(f"{', '.join(seen_now)} vừa mới xuất hiện/bước vào cảnh ở lượt này: hãy tả sự xuất hiện đó một cách tự nhiên.")
        if dec.get("pronoun_break") and dec.get("addressee") in scene_cids:
            t_nm = name_of(pack, dec["addressee"])
            dir_lines.append(f"(Lưu ý diễn xuất: {user} vừa bất ngờ đổi xưng hô sang '{dec['pronoun_break']}' với {t_nm} — hãy để {t_nm} phản ứng tự nhiên.)")
    else:
        absent_names = [name_of(pack, c["char_id"]) for c in (pack.get("cast") or []) if c["char_id"] not in rpformat.co_located_npcs(pack, state)]
        dir_lines.append(
            "Chỉ tả cảnh/nhân vật phụ hoặc đưa nhân vật chính vắng mặt vào cảnh nếu hợp lý ("
            + (", ".join(absent_names) or "không còn ai")
            + "); nhân vật vừa vào chỉ nói tối đa 1–2 câu ngắn để chào."
        )

    # Case #9: Nguoi dung o ngoai cua (at_door) chua nhin thay trong phong
    for cid in ch_participants:
        if cid != "user" and ((state.get("entities") or {}).get(cid) or {}).get("at_door") == user_loc:
            dir_lines.append(f"{name_of(pack, cid)} hiện đang đứng Ở NGOÀI CỬA (chỉ nghe thấy tiếng vọng qua cửa, chưa nhìn thấy cảnh bên trong phòng cho tới khi cửa được mở).")

    if sp in chars:
        blind_turns = [
            f"#{m.get('turn_id', upto + idx + 1)}"
            for idx, m in enumerate(working)
            if isinstance(m.get("witnesses"), list) and sp not in m["witnesses"] and m["speaker"] != sp
        ]
        if blind_turns:
            dir_lines.append(prompts.render("writer.witness_boundary", speaker_name=sp_name, blind_turns=", ".join(blind_turns)))
    elif is_scene_all and scene_cids:
        for cid in scene_cids:
            c_blind = [
                f"#{m.get('turn_id', upto + idx + 1)}"
                for idx, m in enumerate(working)
                if isinstance(m.get("witnesses"), list) and cid not in m["witnesses"] and m["speaker"] != cid
            ]
            if c_blind:
                dir_lines.append(prompts.render("writer.witness_boundary", speaker_name=name_of(pack, cid), blind_turns=", ".join(c_blind)))

    minors = rpformat.minors_present(pack, state)
    if minors:
        dir_lines.append(prompts.render("writer.minor_boundary", minors=", ".join(minors)))

    is_continue = not history or history[-1]["speaker"] != "user"
    if is_continue:
        dir_lines.append(prompts.render("writer.flow_continue", user=user))
    else:
        dir_lines.append(prompts.render("writer.flow_reply", user=user))

    dir_lines.append(f"Nhịp kịch tính hiện tại [{vibe_label}]: {vibe_directive}")

    z4_dir = "[LƯU Ý DIỄN XUẤT]\n" + "\n".join(f"- {x}" for x in dir_lines)

    # 6. Dat nhip hoi thoai sat day de Writer bat dung giong van tu nhien
    tail_parts = []
    if history:
        last_m = history[-1]
        last_who = user if last_m["speaker"] == "user" else name_of(pack, last_m["speaker"])
        if last_m["speaker"] != "user":
            last_u = next((m for m in reversed(history[:-1]) if m.get("speaker") == "user"), None)
            if last_u:
                tail_parts.append(
                    f"Tình huống gốc do {user} vừa khởi xướng:\n{user}: {last_u.get('content', '')}\n\n"
                    f"Phản ứng vừa rồi của {last_who} ngay trước lượt của bạn:\n{last_who}: {last_m.get('content', '')}"
                )
            else:
                tail_parts.append(f"Tin vừa rồi cần đáp lại:\n{last_who}: {last_m.get('content', '')}")
        else:
            tail_parts.append(f"Tin vừa rồi cần đáp lại:\n{last_who}: {last_m.get('content', '')}")
    if sp in chars:
        tail_parts.append(prompts.render("writer.tail_character", speaker_name=sp_name))
    elif is_scene_all and scene_cids:
        tail_parts.append(prompts.render("writer.tail_scene", scene_names=scene_names, user=user))
    else:
        tail_parts.append(prompts.get_text("writer.tail_narrator"))
    z4_tail = "\n\n".join(tail_parts)

    zone4 = "\n\n".join(p for p in [z4_state, z4_lore, z4_mem, z4_dir, z4_speaker, z4_pronouns, z4_tail] if p)

    return [
        {"role": "system", "content": zone1},
        {"role": "user", "content": zone2_3},
        {"role": "user", "content": zone4},
    ]


def writer_messages(pack, state, history, dec, active_lore=None):
    """Alias tuong thich ten cu -> goi compile_context_4zones."""
    return compile_context_4zones(pack, state, history, dec, active_lore=active_lore)


# ----------------------------------------------------------------------------- Pha 5: Post-Render State, Hard Memory & Summaries
def summary_due(history, state, prev_loc=None, new_loc=None, prev_day=None, new_day=None):
    """Sawtooth trigger: >= 16 luot trong Working Memory, hoac doi canh/ngay moi (>= 4 luot)."""
    history = rp_history(history)
    upto = int(state.get("summarized_upto", state.get("tom_tat_upto", 0)))
    working_len = len(history) - upto
    if working_len >= SAWTOOTH_MAX:
        return True
    scene_switched = (prev_loc and new_loc and prev_loc != new_loc) or (prev_day is not None and new_day is not None and prev_day != new_day)
    return bool(scene_switched and working_len >= SCENE_MIN_SWITCH)


def summary_range(history, state, prev_loc=None, new_loc=None, prev_day=None, new_day=None):
    """Tra ve (upto, end) theo Sawtooth: cat khoi 8 khi >= 16, hoac cat het canh cu khi doi canh."""
    history = rp_history(history)
    upto = int(state.get("summarized_upto", state.get("tom_tat_upto", 0)))
    working_len = len(history) - upto
    if working_len >= SAWTOOTH_MAX:
        return upto, upto + (SAWTOOTH_MAX - SAWTOOTH_MIN)  # cat dung khoi 8 cu nhat -> con lai 8..
    scene_switched = (prev_loc and new_loc and prev_loc != new_loc) or (prev_day is not None and new_day is not None and prev_day != new_day)
    if scene_switched and working_len >= SCENE_MIN_SWITCH:
        return upto, max(upto, len(history) - 1)
    return upto, upto


def summary_messages(pack, state, history, end_override=None):
    """Format cung 4 muc cho Scene Summary (Python tu dien tieu de [Thoi gian | Dia diem | Co mat])."""
    history = rp_history(history)
    upto = int(state.get("summarized_upto", state.get("tom_tat_upto", 0)))
    end = end_override if end_override is not None else max(upto, len(history) - SAWTOOTH_MIN)
    slice_hist = history[upto:end]
    chunk = transcript(slice_hist, pack, n=10 ** 6)
    locs = state.get("locations") or pack.get("locations") or {}
    user_loc = ((state.get("entities") or {}).get("user") or {}).get("location_id") or state.get("dia_diem")
    loc_name = (locs.get(user_loc) or {}).get("name") or user_loc
    clk = rpformat.format_clock(state["clock"]) if isinstance(state.get("clock"), dict) else state.get("thoi_gian", "?")
    present_names = [user_name_of(pack)] + [name_of(pack, c) for c in rpformat.co_located_npcs(pack, state)]
    header = f"[{clk} | {loc_name} | Có mặt: {', '.join(present_names)}]"
    return end, header, [
        {"role": "system", "content": prompts.get_text("post_render.scene_summary_sys")},
        {
            "role": "user",
            "content": prompts.render("post_render.scene_summary_user", header=header, chunk=chunk),
        },
    ]


def arc_merge_messages(old_arc, dropped_scene_text):
    return [
        {"role": "system", "content": prompts.get_text("post_render.arc_merge_sys")},
        {
            "role": "user",
            "content": prompts.render("post_render.arc_merge_user", old_arc=old_arc or "(chưa có)", dropped_scene_text=dropped_scene_text),
        },
    ]


def apply_scene_summary(pack, state, header, body_text, end, arc_text=None):
    """Hop nhat ket qua Scene Summary vao Session State (2 tang: 5 recent_scenes -> story_arc)."""
    new_state = copy.deepcopy(_ensure_v2_state(pack, state))
    full_scene = f"{header}\n{body_text.strip()}"
    scenes = list(new_state.get("recent_scenes") or [])
    scenes.append({"header": header, "text": full_scene, "upto": end})
    dropped = None
    if len(scenes) > MAX_RECENT_SCENES:
        dropped = scenes.pop(0)
        if arc_text is not None:
            new_state["story_arc"] = arc_text.strip()
        else:
            prev_arc = (new_state.get("story_arc") or "").strip()
            new_state["story_arc"] = (prev_arc + "\n" + dropped["text"]).strip()[-1500:]
    new_state["recent_scenes"] = scenes
    new_state["summarized_upto"] = max(int(new_state.get("summarized_upto", 0)), int(end))
    return rpformat.sync_legacy_state(pack, new_state), dropped


def update_state(
    api_key,
    decide_model,
    extract_model,
    pack,
    state,
    history,
    owed=(),
    arriving=(),
    decision_provider=None,
    skip_chain=False,
):
    """Pha 5: He qua Post-Render, Hard Memory (known_by & fact_reveal), Graph & Chain."""
    history = rp_history(history)
    state = _ensure_v2_state(pack, state)
    dp = decision_provider or DecisionProvider(api_key, decide_model)
    cast = cast_by_id(pack)
    user = user_name_of(pack)
    ch = state.get("active_channel") or {"channel_type": "in_person", "participants": ["user"] + rpformat.co_located_npcs(pack, state)}
    ch_type = ch.get("channel_type", "in_person")
    ch_participants = list(ch.get("participants") or ["user"])
    channel_npcs = [c for c in ch_participants if c in cast]
    last_speaker = history[-1]["speaker"] if history else None
    is_single_chat = (len(cast) == 1)

    recent = transcript(history, pack, 3)
    st_text = f"[TRẠNG THÁI TRƯỚC]\n{state_text(pack, state)}\n\n[LƯỢT MỚI]\n{recent}"

    q = {
        "dialogue_time_delta": {
            "type": "choice",
            "criteria": DIALOGUE_TIME_DELTAS,
            "instructions": prompts.get_text("post_render.dialogue_time_delta"),
        },
        "do_cang": {
            "type": "score",
            "criteria": TENSION,
            "instructions": prompts.get_text("post_render.do_cang"),
        },
        "scene_vibe": {
            "type": "choice",
            "criteria": SCENE_VIBE_CRITERIA,
            "instructions": prompts.get_text("post_render.scene_vibe"),
        },
        "mem_worthy": {
            "type": "noul",
            "instructions": prompts.get_text("post_render.mem_worthy"),
        },
    }

    if ch_type == "in_person" and not is_single_chat:
        for cid, c in cast.items():
            q[f"co_mat__{cid}"] = {
                "type": "noul",
                "instructions": prompts.render("post_render.co_mat", char_name=c["name"], char_short=c["short"]),
            }

    if last_speaker in cast:
        q[f"mood__{last_speaker}"] = {
            "type": "choice",
            "criteria": MOODS,
            "instructions": prompts.render("post_render.mood", char_name=cast[last_speaker]["name"]),
        }
        q["rel_shift"] = {
            "type": "choice",
            "criteria": REL_SHIFTS,
            "instructions": prompts.render("post_render.rel_shift", char_name=cast[last_speaker]["name"], user=user),
        }

    # fact_reveal cho cac bi mat / su that chua lo het cho nguoi co mat / trong kenh (Case #14)
    vis_pool = set(ch_participants) | set(rpformat.co_located_npcs(pack, state))
    reveal_candidates = []
    for fact in state.get("hard_memory") or []:
        if fact.get("status") == "superseded":
            continue
        kb = set(fact.get("known_by") or [])
        if "all" in kb:
            continue
        if ("user" in kb or last_speaker in kb) and not vis_pool.issubset(kb):
            reveal_candidates.append(fact)
    for fact in reveal_candidates[:4]:
        q[f"fact_reveal__{fact['id']}"] = {
            "type": "noul",
            "instructions": prompts.render("post_render.fact_reveal", fact_text=fact["text"]),
        }

    # Phan ung day chuyen trong kenh hien tai (Case #26: khong hoi lai nguoi da noi trong cung chain)
    chain_said = recent_chain_speakers(history)
    chain_depth = max(0, len(chain_said) - 1)
    disable_chain = bool(skip_chain or last_speaker == SCENE_SPEAKER)
    if not is_single_chat and not disable_chain:
        for cid in channel_npcs:
            if cid != last_speaker and cid not in chain_said:
                q[f"chen__{cid}"] = {
                    "type": "noul",
                    "instructions": prompts.render("post_render.chen", char_name=cast[cid]["name"], char_short=cast[cid]["short"], user=user),
                }

    res = dp.ask({"noi_dung": st_text}, q)
    a = res["answers"]
    extra_cost = float(res["cost"] or 0)

    new_state = copy.deepcopy(state)
    user_loc = new_state["entities"]["user"]["location_id"]
    before_present = set(rpformat.co_located_npcs(pack, state))

    # 1. Clock Post-Render (+ Case #16: npc_chain khong cong don thoi gian)
    dt_choice = (a.get("dialogue_time_delta") or {}).get("choice", "dialogue")
    if len(chain_said) >= 2 and dt_choice in ("dialogue", "brief"):
        dt_choice = "instant"
    new_state["clock"] = rpformat.apply_time_delta(new_state["clock"], dt_choice)

    # 2. NPC co_mat (3-zone confidence band & hysteresis; chat 1-1 giu nguyen nhan vat chinh)
    if ch_type == "in_person" and not is_single_chat:
        fallback_other = next((k for k in new_state["locations"] if k != user_loc), user_loc)
        for cid in cast:
            p = float(a.get(f"co_mat__{cid}", {}).get("noul", 0))
            if cid in before_present:
                # Hysteresis: chi roi canh khi p <= CONF_LOW (0.25)
                if p <= CONF_LOW and cid != last_speaker:
                    old_loc = state["entities"][cid].get("location_id")
                    new_state["entities"][cid]["location_id"] = fallback_other if old_loc == user_loc else old_loc
            elif new_state["entities"][cid].get("at_door") == user_loc:
                # Case #9: Van dung ngoai cua, khong keo vao trong phong qua co_mat neu cua chua mo
                continue
            else:
                thresh = ARRIVE_THRESHOLD if cid in arriving else CONF_HIGH
                if p >= thresh:
                    new_state["entities"][cid]["location_id"] = user_loc
                    new_state["entities"][cid]["entered_turn"] = len(history)
        new_present = rpformat.co_located_npcs(pack, new_state)
        at_door_kept = [c for c in ch_participants if c != "user" and new_state["entities"].get(c, {}).get("at_door") == user_loc]
        new_state["active_channel"]["participants"] = ["user"] + list(dict.fromkeys(new_present + at_door_kept))
    else:
        new_present = rpformat.co_located_npcs(pack, new_state)

    # 3. Tension, Scene Vibe, Mood & Silence turns (+ Case #18 guard neu NPC dang happy)
    score_tension = max(0, min(4, int(round(float((a.get("do_cang") or {}).get("score", new_state["tension"]))))))
    sv_ans = a.get("scene_vibe") or {}
    sv_choice = sv_ans.get("choice", "")
    sv_conf = float(sv_ans.get("confidence", 0))
    if sv_choice in SCENE_VIBES and (sv_conf >= 0.50 or not new_state.get("scene_vibe")):
        new_state["scene_vibe"] = sv_choice
    else:
        new_state["scene_vibe"] = resolve_scene_vibe(new_state)[0]

    new_state["tension"] = score_tension
    if new_state["scene_vibe"] != "relaxed":
        new_state["tension"] = max(score_tension, VIBE_TO_TENSION.get(new_state["scene_vibe"], score_tension))

    if last_speaker in cast and f"mood__{last_speaker}" in a:
        m_ans = a[f"mood__{last_speaker}"]
        if float(m_ans.get("confidence", 0)) >= 0.60:
            new_state["entities"][last_speaker]["mood"] = m_ans["choice"]
    if last_speaker in cast and new_state["entities"][last_speaker].get("mood") == "happy":
        if new_state["tension"] >= 3:
            new_state["tension"] = 2
        if new_state.get("scene_vibe") == "conflict":
            new_state["scene_vibe"] = "lively"

    for cid in cast:
        ent = new_state["entities"][cid]
        if cid == last_speaker or (last_speaker == SCENE_SPEAKER and cid in ch_participants):
            ent["silence_turns"] = 0
        elif cid in new_state["active_channel"]["participants"] or cid in new_present:
            ent["silence_turns"] = int(ent.get("silence_turns", 0)) + 1

    # 4. Relationship shift
    rel_ans = a.get("rel_shift") or {}
    rel_choice = rel_ans.get("choice", "unchanged")
    if last_speaker in cast and float(rel_ans.get("confidence", 0)) >= CONF_HIGH and rel_choice in ("warmer", "colder"):
        delta = 5 if rel_choice == "warmer" else -5
        edges = new_state.setdefault("relationships", {}).setdefault("edges", {})
        if last_speaker in edges and "user" in edges[last_speaker]:
            edges[last_speaker]["user"]["level"] = max(0, min(100, int(edges[last_speaker]["user"].get("level", 50)) + delta))

    # 5. fact_reveal -> mo rong known_by (bao gom ca nguoi co mat tai cho khi dua man hinh xem - Case #14)
    revealed_ids = []
    reveal_viewers = list(dict.fromkeys(list(new_state["active_channel"]["participants"]) + list(new_present)))
    for fact in new_state.get("hard_memory") or []:
        fid = fact["id"]
        if f"fact_reveal__{fid}" in a and float(a[f"fact_reveal__{fid}"].get("noul", 0)) >= 0.75:
            kb = list(fact.get("known_by") or [])
            for p in reveal_viewers:
                if p not in kb:
                    kb.append(p)
            fact["known_by"] = kb
            fact["status"] = "revealed"
            revealed_ids.append(fid)

    # 6. mem_worthy -> Extractor JSON -> hard_memory (Case #22 supersedes_ids + Case #23 lie detection)
    mem_p = float((a.get("mem_worthy") or {}).get("noul", 0))
    added_fact = None
    extract_err = None
    if mem_p >= 0.75 and api_key:
        try:
            sys_p = prompts.get_text("post_render.mem_extractor_sys")
            valid_ids = ", ".join(["user"] + list(cast.keys()))
            active_facts = [
                f for f in new_state.get("hard_memory") or []
                if f.get("status") == "active" and ("user" in (f.get("known_by") or []) or any(e in (f.get("entities") or []) for e in ch_participants))
            ][-8:]
            secret_facts = [f for f in new_state.get("hard_memory") or [] if f.get("status") == "secret"][:5]
            extra_ctx = ""
            if active_facts:
                extra_ctx += "\n[SỰ THẬT ĐANG ACTIVE]\n" + "\n".join(f"- {f['id']}: {f['text']}" for f in active_facts)
            if secret_facts:
                extra_ctx += "\n[BÍ MẬT THẬT ĐỂ ĐỐI CHIẾU LỜI NÓI DỐI]\n" + "\n".join(f"- {f['id']}: {f['text']}" for f in secret_facts)
            obj, c = extract_json(api_key, extract_model, sys_p, f"ID thực thể: {valid_ids}{extra_ctx}\n\n{recent}", max_tokens=260)
            extra_cost += c
            sup_ids = set(obj.get("supersedes_ids") or []) if isinstance(obj, dict) else set()
            if sup_ids:
                for f in new_state.get("hard_memory") or []:
                    if f.get("id") in sup_ids and f.get("status") == "active":
                        f["status"] = "superseded"
            ftext = (obj.get("text") or "").strip() if isinstance(obj, dict) else ""
            if ftext:
                fid = f"fact_{len(new_state['hard_memory']) + 1}_{int(time.time()) % 10000}"
                added_fact = {
                    "id": fid,
                    "time": rpformat.format_clock(new_state["clock"]),
                    "text": ftext,
                    "entities": [e for e in (obj.get("entities") or []) if e == "user" or e in cast] or list(new_state["active_channel"]["participants"]),
                    "known_by": list(new_state["active_channel"]["participants"]),
                    "keywords": list(obj.get("keywords") or rpformat._auto_keywords(ftext)),
                    "status": "active",
                }
                new_state["hard_memory"].append(added_fact)
        except Exception as err:
            extract_err = str(err)

    # 7. Chain interrupt (Case #26: chong vong lap bong ban, tang nguong theo depth, moi NPC toi da 1 lan/nhip chain)
    ch_npcs_now = [c for c in new_state["active_channel"]["participants"] if c in cast]
    if disable_chain:
        chen, fresh, new_owed, owed_ok, nxt = {}, [], [], [], None
        new_state["owed"] = []
    else:
        chen = {cid: float(a[k]["noul"]) for cid in ch_npcs_now if (k := f"chen__{cid}") in a}
        fresh = [c for c in new_present if c not in before_present and c != last_speaker and c not in chain_said] if ch_type == "in_person" else []
        new_owed = list(dict.fromkeys([c for c in owed if c in ch_npcs_now and c != last_speaker and c not in chain_said] + fresh))
        new_state["owed"] = new_owed

        owed_ok = [c for c in new_owed if c not in chain_said and (c in fresh or chen.get(c, 0) >= OWED_THRESHOLD)]
        if owed_ok:
            nxt = max(owed_ok, key=lambda c: chen.get(c, 0))
        elif chain_depth >= 2:
            nxt = None
        else:
            base_th = CHAIN_THRESHOLD if chain_depth == 0 else 0.80
            quiet_th = QUIET_THRESHOLD if chain_depth == 0 else 0.65
            need = lambda c: quiet_th if int(new_state["entities"][c].get("silence_turns", 0)) >= QUIET_TURNS else base_th
            ok = [c for c in chen if c not in chain_said and chen[c] >= need(c)]
            nxt = max(ok, key=chen.get) if ok else None

    new_state = rpformat.sync_legacy_state(pack, new_state)
    th_5 = {
        "scene_vibe": "conf >= 0.50",
        "mem_worthy": ">= 0.75",
        "rel_shift": f"conf >= {CONF_HIGH}",
    }
    eff_5 = {
        "dialogue_time_delta": f"Cộng {dt_choice}",
        "do_cang": f"tension={new_state['tension']}",
        "scene_vibe": f"{SCENE_VIBES[new_state['scene_vibe']]} ({new_state['scene_vibe']})",
        "mem_worthy": f"Trích xuất fact: {added_fact['text']}" if added_fact else ("Bỏ qua" if mem_p < 0.75 else (f"Lỗi: {extract_err}" if extract_err else "Không có")),
    }
    for k in q:
        if k.startswith("co_mat__"):
            cid = k.split("__", 1)[1]
            th_5[k] = f"<= {CONF_LOW} rời | >= {ARRIVE_THRESHOLD if cid in arriving else CONF_HIGH} vào"
            eff_5[k] = "Có mặt" if cid in new_present else "Vắng mặt"
        elif k.startswith("mood__"):
            th_5[k] = "conf >= 0.60"
            eff_5[k] = MOODS.get(new_state["entities"][last_speaker].get("mood", "normal"), "")
        elif k == "rel_shift":
            eff_5[k] = rel_choice
        elif k.startswith("fact_reveal__"):
            fid = k.split("__", 1)[1]
            th_5[k] = ">= 0.75"
            eff_5[k] = "Đã lộ cho phòng" if fid in revealed_ids else "Giữ kín"
        elif k.startswith("chen__"):
            cid = k.split("__", 1)[1]
            th_5[k] = f">= {QUIET_THRESHOLD if int(new_state['entities'][cid].get('silence_turns', 0)) >= QUIET_TURNS else CHAIN_THRESHOLD}"
            eff_5[k] = "Chọn chen lời" if cid == nxt else "Im lặng"
    dec_trace_5 = _build_decision_trace("Pha 5 (Cập nhật Trạng thái & Bộ nhớ)", {"noi_dung": st_text}, q, res, th_5, eff_5)
    dec_trace_5["extractor"] = added_fact["text"] if added_fact else None
    detail = {
        "scene_vibe": new_state["scene_vibe"],
        "scene_vibe_label": SCENE_VIBES[new_state["scene_vibe"]],
        "co_mat_p": {cast[c]["name"]: round(a[f"co_mat__{c}"]["noul"], 2) for c in cast if f"co_mat__{c}" in a},
        "chen_p": {cast[c]["name"]: round(p, 2) for c, p in chen.items()},
        "next_speaker": nxt,
        "next_why": (
            ("vừa bước vào, chưa nói câu nào" if nxt in fresh else "chưa đáp lời người chơi")
            if owed_ok else (
                "im lặng lâu, muốn chen vào"
                if nxt and int(new_state["entities"][nxt].get("silence_turns", 0)) >= QUIET_TURNS
                else "muốn chen vào"
            )
        ) if nxt else None,
        "owed": [cast[c]["name"] for c in new_owed if c in cast],
        "dialogue_time_delta": dt_choice,
        "mem_worthy_p": round(mem_p, 2),
        "added_fact": added_fact["text"] if added_fact else None,
        "revealed_facts": revealed_ids,
        "extract_error": extract_err,
        "decision_trace": dec_trace_5,
    }
    return new_state, {"cost": extra_cost, "secs": res["secs"], **detail}
