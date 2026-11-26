"""Cong cham ban nhap bang model Decisions cua OpenRouter (POST /api/alpha/decisions).

Moi tieu chi la 1 cau hoi noul (xac suat "CO"). Ban nhap DAT khi moi tieu chi tot voi do chac >= nguong
(tieu chi "tot = CO" can p >= nguong; tieu chi "tot = KHONG" can p <= 1 - nguong).
"""
import json
import re
import time
import urllib.error
import urllib.request

import prompts
import rpformat

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
JUDGE_MODELS = [
    "microsoft/microsoft-decision-1",
    "google/gemini-decision-1",
    "openai/gpt-6-luna-decisions",
    "typesafe/jev-1.14",
    "heygen/heygen-decision-1-turbo",
]

CRITERIA = {
    "tra_loi_dung": {
        "label": "trả lời đúng tin cuối",
        "q": "Câu trả lời của {char} có phản hồi hợp lý vào điều {user} vừa nói, hỏi hoặc làm ở tin cuối không? "
             "(Lưu ý: tính là CÓ kể cả khi {char} cố tình né tránh câu hỏi, đánh trống lảng, chối quanh hoặc nói dối "
             "cho đúng tính cách/bí mật của nhân vật; chỉ tính là KHÔNG khi trả lời lạc đề hoàn toàn hoặc phớt lờ như chưa nghe thấy.)",
        "good": True,
        "fix": "Phản hồi thẳng và đủ vào điều {user} vừa nói hoặc làm ở tin cuối.",
    },
    "mo_dien_bien_moi": {
        "label": "mở diễn biến mới",
        "q": "Câu trả lời có tự ý chuyển cảnh khi {user} chưa đề nghị không? Chỉ tính là CÓ nếu: nhảy thời gian, đổi địa điểm, "
             "cho nhân vật rời khỏi cảnh, hoặc đưa một nhân vật chính mới vào cảnh. (Chi tiết nhỏ làm sống cảnh như cử chỉ, "
             "đồ vật, âm thanh, người qua đường thoáng qua, câu đùa là hợp lệ, KHÔNG tính.)",
        "good": False,
        "fix": "Bỏ đoạn tự ý nhảy thời gian, đổi địa điểm, cho nhân vật rời cảnh hoặc đưa nhân vật chính mới vào. "
               "Giữ nguyên các chi tiết nhỏ làm sống cảnh.",
    },
    "viet_ho_user": {
        "label": "viết hộ người chơi",
        "q": "Câu trả lời có viết thay lời nói, suy nghĩ hoặc hành động MỚI của {user} không? "
             "(Lưu ý: việc nhắc lại hành động {user} vừa làm ở tin cuối để phản ứng — như 'cái ôm của {user} làm cô khựng lại' — là hợp lệ, KHÔNG tính là viết hộ.)",
        "good": False,
        "fix": "Bỏ những câu thoại do chính {user} nói và những hành động, suy nghĩ mới của {user}. GIỮ NGUYÊN lời thoại của các "
               "nhân vật khác nếu hợp lệ.",
    },
    "viet_thoai_nv_khac": {
        "label": "viết thoại nhân vật khác",
        "q": "Trong câu trả lời của {char}, có viết luôn lời thoại trực tiếp (trong ngoặc kép) đóng vai một nhân vật chính khác "
             "({other_chars}) thay vì để họ tự nói ở lượt sau không? (Lưu ý: việc {char} chỉ nhắc lại hoặc nhại lại vài chữ mà người kia vừa nói để phản bác như 'Bóc lột à?' là hợp lệ, KHÔNG tính là viết thoại hộ.)",
        "good": False,
        "fix": "Xóa lời thoại trực tiếp trong ngoặc kép đóng vai nhân vật chính khác ({other_chars}); chỉ giữ lời nói của {char} "
               "và để các nhân vật chính khác tự nói ở lượt sau.",
    },
    "sai_xung_ho": {
        "label": "sai xưng hô",
        "q": "Nhân vật {char} có xưng hô sai với quy định bắt buộc lượt này ({pronoun_rule}) khi nói chuyện trực tiếp với đối tượng tương ứng không? "
             "(Lưu ý: nếu trong lượt có quay sang nói với nhân vật thứ ba khác có mặt, việc đổi đại từ với người thứ ba đó là hợp lệ, không tính là sai.)",
        "good": False,
        "fix": "Chỉ sửa đại từ khi {char} nói trực tiếp với người trong quy định ({pronoun_rule}); không đổi đại từ dùng với người khác.",
    },
    "giong_tro_ly": {
        "label": "giọng trợ lý AI",
        "q": "Câu trả lời có giọng trợ lý AI thay vì giọng nhân vật không (xưng tôi - bạn, hỏi 'bạn muốn làm gì tiếp', "
             "tóm tắt, từ chối, tự nhận là AI)?",
        "good": False,
        "fix": "Viết hoàn toàn bằng giọng và xưng hô của nhân vật, không có câu kiểu trợ lý AI.",
    },
    "metagaming_leak": {
        "label": "lộ thông tin vắng mặt",
        "q": "Nhân vật {char} không có mặt ở các lượt ({blind_turns}). Trong câu trả lời này, {char} có vô tình nhắc tới, "
             "trích dẫn hoặc tỏ ra biết trước thông tin/chi tiết chỉ xảy ra ở các lượt vắng mặt đó không?",
        "good": False,
        "fix": "Xóa bỏ mọi chi tiết, lời thoại hoặc suy nghĩ cho thấy {char} biết chuyện xảy ra ở các lượt vắng mặt ({blind_turns}).",
    },
}

AUX_USER_LED = "do_nguoi_choi_dan_toi"
AUX_Q = (
    "Những sự việc mới trong câu trả lời (ai đó tới hoặc rời đi, đi sang chỗ khác, việc mới bắt đầu) có phải "
    "do {user} vừa nhắc tới, đề nghị hoặc dẫn tới trong tin cuối không?"
)


def quality(probs, criteria_cfg=None):
    """Diem 0..1 de so sanh hai ban (trung binh muc 'tot' cua cac tieu chi)."""
    if not probs:
        return 0.0
    cfg = criteria_cfg or CRITERIA
    return round(sum((p if cfg[k]["good"] else 1 - p) for k, p in probs.items()) / len(probs), 4)


def _failed_from_probs(probs, threshold, eff_skip, criteria_cfg=None):
    """Tra ve danh sach key truot: tieu chi tot=CO truot khi p < nguong, tot=KHONG chi truot khi p >= nguong."""
    cfg = criteria_cfg or CRITERIA
    out = []
    for k, p in probs.items():
        if k in eff_skip or k not in cfg:
            continue
        c_thresh = float(cfg[k].get("threshold") or threshold) if criteria_cfg else float(threshold)
        if (p < c_thresh) if cfg[k]["good"] else (p >= c_thresh):
            out.append(k)
    return out


def judge(
    api_key,
    model,
    prev,
    user_msg,
    draft,
    char,
    user,
    threshold=0.6,
    skip=(),
    other_chars_text="",
    pronoun_rule_text="",
    is_narrator=False,
    blind_turns_text="",
):
    """Tra ve {passed, failed:[key], probs:{key:p}, cost, secs, model, error?}."""
    eff_skip = set(skip or ())
    if is_narrator:
        eff_skip.update(("viet_thoai_nv_khac", "sai_xung_ho", "metagaming_leak"))
    if not (other_chars_text or "").strip():
        eff_skip.add("viet_thoai_nv_khac")
    if not (pronoun_rule_text or "").strip():
        eff_skip.add("sai_xung_ho")
    if not (blind_turns_text or "").strip():
        eff_skip.add("metagaming_leak")

    state = {
        f"tin_truoc_cua_{char}": prev or "(chưa có)",
        f"tin_cuoi_cua_{user}": user_msg or "(chưa có)",
        f"cau_tra_loi_cua_{char}": draft,
    }
    if other_chars_text and not is_narrator:
        state["nhan_vat_chinh_khac"] = other_chars_text
    if pronoun_rule_text and not is_narrator:
        state["quy_dinh_xung_ho"] = pronoun_rule_text
    if blind_turns_text and not is_narrator:
        state["cac_luot_vang_mat"] = blind_turns_text

    criteria_cfg = prompts.get_criteria_config(default_threshold=threshold)
    fmt_args = {
        "char": char,
        "user": user,
        "other_chars": other_chars_text or "các nhân vật khác",
        "pronoun_rule": pronoun_rule_text or "quy định xưng hô",
        "blind_turns": blind_turns_text or "(không có)",
    }
    questions = {}
    for k, v in criteria_cfg.items():
        if k in ("viet_thoai_nv_khac", "sai_xung_ho", "metagaming_leak") and k in eff_skip:
            continue
        questions[k] = {"type": "noul", "instructions": prompts.safe_format(v["q"], **fmt_args)}
    aux_q = prompts.get_text("judge.do_nguoi_choi_dan_toi", AUX_Q)
    questions[AUX_USER_LED] = {"type": "noul", "instructions": prompts.safe_format(aux_q, **fmt_args)}

    body = {"model": model, "state": state, "questions": questions}
    req = urllib.request.Request(
        DECISIONS_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "RP Lab"},
    )
    t = time.time()
    last_err = None
    for attempt in range(3):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            probs = {k: float(d["answers"][k]["noul"]) for k in questions if k in criteria_cfg}
            user_led = float(d["answers"].get(AUX_USER_LED, {}).get("noul", 0))
            failed = _failed_from_probs(probs, threshold, eff_skip, criteria_cfg=criteria_cfg)
            overridden = []
            if "mo_dien_bien_moi" in failed and user_led >= 0.5:
                failed.remove("mo_dien_bien_moi")
                overridden.append("mo_dien_bien_moi")
            score = quality(probs, criteria_cfg=criteria_cfg)
            # Phan tang loi nang / vung do du: neu khong co tieu chi nao vi pham ro (>= 0.50 loi hoac <= 0.40 tot)
            # va diem trung binh >= 0.72 -> cho qua luon de khong ton tien viet lai chi vi muc map me 0.41..0.49
            soft_passed = []
            if failed and score >= 0.72:
                severe = [
                    k for k in failed
                    if (probs[k] <= 0.40 if criteria_cfg[k]["good"] else probs[k] >= 0.50)
                ]
                if not severe:
                    soft_passed = list(failed)
                    failed = []
            rows = []
            for k, qobj in questions.items():
                if k == AUX_USER_LED:
                    rows.append({
                        "key": k, "type": "noul", "question": qobj["instructions"],
                        "answer": f"p={user_led:.2f}", "threshold": ">= 0.50 (bỏ qua mở diễn biến mới)",
                        "effect": "Đã bỏ qua mo_dien_bien_moi" if "mo_dien_bien_moi" in overridden else "—",
                    })
                    continue
                p = probs.get(k, 0.0)
                c_thresh = float(criteria_cfg[k].get("threshold") or threshold)
                op = ">=" if criteria_cfg[k]["good"] else "<"
                status = (
                    "Bỏ qua (user dẫn tới)" if k in overridden
                    else ("Cho qua vùng phân vân" if k in soft_passed
                          else ("TRƯỢT ✗" if k in failed else "ĐẠT ✓"))
                )
                rows.append({
                    "key": k, "type": "noul", "question": qobj["instructions"],
                    "answer": f"p={p:.2f}", "threshold": f"{op} {c_thresh:.2f}",
                    "effect": status,
                })
            dec_trace = {
                "phase": "Pha 4 (Cổng chấm Quality Gate)",
                "state": state,
                "rows": rows,
                "model": d.get("model", model),
                "secs": round(time.time() - t, 2),
                "cost": (d.get("usage") or {}).get("cost"),
            }
            return {
                "passed": not failed,
                "failed": failed,
                "soft_passed": soft_passed,
                "probs": probs,
                "threshold": threshold,
                "skipped": sorted(eff_skip),
                "user_led": round(user_led, 3),
                "overridden": overridden,
                "score": score,
                "decision_trace": dec_trace,
                "cost": (d.get("usage") or {}).get("cost"),
                "secs": round(time.time() - t, 2),
                "model": d.get("model", model),
                "provider": d.get("provider"),
            }
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = str(e)
            break
    return {"passed": False, "failed": [], "probs": {}, "error": last_err, "secs": round(time.time() - t, 2)}


_COMPOUND_NON_PRONOUNS = (
    "đàn ông", "ông trời", "ông bà", "ông già", "bà già", "ông chủ", "bà chủ",
    "ông nội", "bà nội", "ông ngoại", "bà ngoại", "ông thầy", "bà cô",
    "ông chú", "ông bác", "ông bảo vệ", "bà bán", "ông bán", "cậu ấm",
    "anh em", "chị em", "đàn anh", "đàn chị", "anh trai", "chị gái", "anh chàng",
)


def detect_pronoun_slip(draft, pack, state, speaker_id, ch_participants, focus_id):
    """Hau kiem Regex 0ms tren phan loi thoai '...': bat dai tu cam nguoc chieu voi focus_id."""
    if not draft or not pack or speaker_id not in (pack.get("characters") or {}):
        return None
    pr = rpformat.resolve_pronouns(pack, state, speaker_id, ch_participants, focus_id=focus_id)
    focus_pair = pr.get("focus_pair")
    if not focus_pair:
        return None
    quotes = re.findall(r'["“]([^"”]+)["”]', draft)
    if not quotes:
        return None

    cleaned = " ".join(quotes)
    # Loai tru cum tu ghep co dinh khong phai dai tu ngoi 2
    for phrase in sorted(_COMPOUND_NON_PRONOUNS, key=len, reverse=True):
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", " ", cleaned, flags=re.I)

    # Loai tru cum <dai tu> <Ten/Alias> (vd "ong Tuan", "ong Nem", "ba Vy", "chi Khanh")
    all_names = set()
    user_obj = pack.get("user") or {}
    if isinstance(user_obj, dict):
        if user_obj.get("name"):
            all_names.add(user_obj["name"])
            all_names.add(user_obj["name"].split()[-1])
        all_names.update(a for a in (user_obj.get("aliases") or []) if a)
    for c in (pack.get("characters") or {}).values():
        meta = c.get("meta") or {}
        if meta.get("name"):
            all_names.add(meta["name"])
            all_names.add(meta["name"].split()[-1])
        all_names.update(a for a in (meta.get("aliases") or []) if a)

    for nm in sorted((n for n in all_names if str(n).strip()), key=len, reverse=True):
        cleaned = re.sub(
            rf"\b(?:ông|bà|anh|chị|em|thầy|cô|chú|bác)\s+{re.escape(str(nm).strip())}\b",
            " ",
            cleaned,
            flags=re.I,
        )

    valid_calls = {p["call"].lower() for p in pr["pairs"] if p.get("call")} | {
        p["call"].split()[0].lower() for p in pr["pairs"] if p.get("call")
    }
    valid_selfs = {p["self"].lower() for p in pr["pairs"] if p.get("self")}
    allowed_all = valid_calls | valid_selfs

    check_words = {"ông", "bà", "cậu", "tớ", "tôi"}
    if len(pr["pairs"]) == 1:
        check_words.update(("anh", "chị", "tao", "mày"))

    for w in focus_pair.get("forbidden") or []:
        if w in check_words and w not in allowed_all and re.search(rf"\b{re.escape(w)}\b", cleaned, re.I):
            return {
                "slipped": w,
                "target_name": focus_pair["target_name"],
                "self": focus_pair["self"],
                "call": focus_pair["call"],
            }
    return None


def fix_instruction(draft, failed, probs, char, user, other_chars_text="", pronoun_rule_text="", focus_pair=None, blind_turns_text=""):
    """Huong dan viet lai CHI nham vao loi bi bat."""
    if list(failed or ()) == ["sai_xung_ho"]:
        t_name = s_p = c_p = ""
        if isinstance(focus_pair, dict) and focus_pair.get("target_name"):
            t_name = focus_pair["target_name"]
            s_p = focus_pair.get("self", "")
            c_p = focus_pair.get("call", "")
        elif pronoun_rule_text:
            m = re.search(r"với\s+(.+?):\s+xưng\s+'(.+?)'\s*-\s*gọi\s+'(.+?)'", pronoun_rule_text)
            if m:
                t_name, s_p, c_p = m.group(1), m.group(2), m.group(3)
        if t_name and s_p and c_p:
            return prompts.render(
                "judge.surgical_fix", char=char, draft=draft.strip(), target_name=t_name, self=s_p, call=c_p
            )

    criteria_cfg = prompts.get_criteria_config()
    fmt_args = {
        "char": char,
        "user": user,
        "other_chars": other_chars_text or "các nhân vật khác",
        "pronoun_rule": pronoun_rule_text or "quy định xưng hô",
        "blind_turns": blind_turns_text or "mà nhân vật không chứng kiến",
    }
    lines = [
        f"- {prompts.safe_format(criteria_cfg[k]['fix'], **fmt_args)}" for k in failed if k in criteria_cfg
    ] or [
        "- Soát lại toàn bộ: trả lời đúng tin cuối, không mở diễn biến mới, không viết hộ, không sai xưng hô, không giọng trợ lý."
    ]
    keep_colloquial = (
        ""
        if "sai_xung_ho" in (failed or ())
        else " TUYỆT ĐỐI giữ nguyên đại từ xưng hô khẩu ngữ của nhân vật (như tui-ông/bà, tao-mày, tôi-cậu, em-anh, chị-em), KHÔNG tự ý đổi sang 'mình - bạn' hay 'tôi - bạn'."
    )
    return prompts.render(
        "judge.general_fix",
        char=char,
        draft=draft.strip(),
        error_lines="\n".join(lines),
        keep_colloquial=keep_colloquial,
    )
