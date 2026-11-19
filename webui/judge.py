"""Cong cham ban nhap bang model Decisions cua OpenRouter (POST /api/alpha/decisions).

Moi tieu chi la 1 cau hoi noul (xac suat "CO"). Ban nhap DAT khi moi tieu chi tot voi do chac >= nguong
(tieu chi "tot = CO" can p >= nguong; tieu chi "tot = KHONG" can p <= 1 - nguong).
Do tren 24 mau tieng Viet (rp/gate_eval): microsoft/microsoft-decision-1 o nguong 0.8 cho xuat thang 14/24 ban nhap,
1 ban sai lot qua, khong chan oan ban tot nao; Jev 1.14 khong du chac de dung cong nay voi tieng Viet.
"""
import json, time, urllib.error, urllib.request

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
JUDGE_MODELS = ["microsoft/microsoft-decision-1", "google/gemini-decision-1", "openai/gpt-6-luna-decisions",
                "typesafe/jev-1.14", "heygen/heygen-decision-1-turbo"]

CRITERIA = {
    "tra_loi_dung": {
        "label": "trả lời đúng tin cuối",
        "q": "Câu trả lời của {char} có phản hồi trực tiếp và đúng vào điều {user} vừa nói, hỏi hoặc làm ở tin cuối không?",
        "good": True,
        "fix": "Phản hồi thẳng và đủ vào điều {user} vừa nói hoặc làm ở tin cuối.",
    },
    "mo_dien_bien_moi": {
        "label": "mở diễn biến mới",
        "q": "Câu trả lời có tự mở diễn biến mới khi {user} chưa chuyển cảnh không? Tính là CÓ nếu: nhảy thời gian, "
             "nhắc giờ để hối thúc sang việc khác, đổi địa điểm, cho nhân vật rời khỏi cảnh hoặc đi làm việc khác, "
             "thêm nhân vật hay sự kiện mới chưa có trong bối cảnh.",
        "good": False,
        "fix": "Bỏ mọi chỗ nhảy thời gian, nhắc giờ hối thúc, đổi địa điểm, cho nhân vật rời cảnh, thêm nhân vật hay sự kiện mới. "
               "Ở lại với việc đang diễn ra cho tới khi {user} chuyển cảnh.",
    },
    "viet_ho_user": {
        "label": "viết hộ người chơi",
        "q": "Câu trả lời có viết thay lời nói, suy nghĩ hoặc hành động của {user} không?",
        "good": False,
        "fix": "Bỏ những câu thoại do chính {user} nói và những hành động, suy nghĩ của {user}. GIỮ NGUYÊN lời thoại của các "
               "nhân vật khác, kể cả khi họ nói với {user} hay gọi tên {user}.",
    },
    "giong_tro_ly": {
        "label": "giọng trợ lý AI",
        "q": "Câu trả lời có giọng trợ lý AI thay vì giọng nhân vật không (xưng tôi - bạn, hỏi 'bạn muốn làm gì tiếp', "
             "tóm tắt, từ chối, tự nhận là AI)?",
        "good": False,
        "fix": "Viết hoàn toàn bằng giọng và xưng hô của nhân vật, không có câu kiểu trợ lý AI.",
    },
}


AUX_USER_LED = "do_nguoi_choi_dan_toi"
AUX_Q = ("Những sự việc mới trong câu trả lời (ai đó tới hoặc rời đi, đi sang chỗ khác, việc mới bắt đầu) có phải "
         "do {user} vừa nhắc tới, đề nghị hoặc dẫn tới trong tin cuối không?")


def quality(probs):
    """Diem 0..1 de so sanh hai ban (trung binh muc 'tot' cua cac tieu chi)."""
    if not probs:
        return 0.0
    return round(sum((p if CRITERIA[k]["good"] else 1 - p) for k, p in probs.items()) / len(probs), 4)


def judge(api_key, model, prev, user_msg, draft, char, user, threshold=0.8, skip=()):
    """Tra ve {passed, failed:[key], probs:{key:p}, cost, secs, model, error?}.
    skip: tieu chi van hoi (de hien thi) nhung khong tinh vao ket qua, vd luot 'tiep dien' khong xet 'mo_dien_bien_moi'."""
    state = {f"tin_truoc_cua_{char}": prev or "(chưa có)", f"tin_cuoi_cua_{user}": user_msg or "(chưa có)",
             f"cau_tra_loi_cua_{char}": draft}
    questions = {k: {"type": "noul", "instructions": v["q"].format(char=char, user=user)} for k, v in CRITERIA.items()}
    # cau hoi phu, khang dinh, nguyen tu (model Decisions kem voi cau phu dinh dai): su viec moi co phai do nguoi choi dan toi?
    questions[AUX_USER_LED] = {"type": "noul", "instructions": AUX_Q.format(user=user)}
    body = {"model": model, "state": state, "questions": questions}
    req = urllib.request.Request(DECISIONS_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                                          "X-Title": "RP Lab"})
    t = time.time()
    last_err = None
    for attempt in range(3):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            probs = {k: float(d["answers"][k]["noul"]) for k in CRITERIA}
            user_led = float(d["answers"].get(AUX_USER_LED, {}).get("noul", 0))
            failed = [k for k, v in CRITERIA.items() if k not in skip and
                      not ((probs[k] >= threshold) if v["good"] else (probs[k] <= 1 - threshold))]
            overridden = []
            if "mo_dien_bien_moi" in failed and user_led >= 0.5:
                failed.remove("mo_dien_bien_moi")  # dien bien moi do chinh nguoi choi dan toi -> hop le
                overridden.append("mo_dien_bien_moi")
            return {"passed": not failed, "failed": failed, "probs": probs, "threshold": threshold, "skipped": list(skip),
                    "user_led": round(user_led, 3), "overridden": overridden, "score": quality(probs),
                    "cost": (d.get("usage") or {}).get("cost"), "secs": round(time.time() - t, 2),
                    "model": d.get("model", model), "provider": d.get("provider")}
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:  # mang, JSON...
            last_err = str(e)
            break
    return {"passed": False, "failed": [], "probs": {}, "error": last_err, "secs": round(time.time() - t, 2)}


def fix_instruction(draft, failed, probs, char, user):
    """Huong dan viet lai CHI nham vao loi bi bat."""
    lines = [f"- {CRITERIA[k]['fix'].format(char=char, user=user)}" for k in failed] or \
            ["- Soát lại toàn bộ: trả lời đúng tin cuối, không mở diễn biến mới, không viết hộ, không giọng trợ lý."]
    return (f"[BƯỚC SỬA — không phải một phần của câu chuyện]\n"
            f"Bản nháp lượt tiếp theo của {char} bị bộ chấm đánh trượt:\n<<<\n{draft.strip()}\n>>>\n\n"
            f"Lỗi cần sửa:\n" + "\n".join(lines) + "\n\n"
            f"Viết lại BẢN CUỐI: giữ nguyên những phần tốt (nội dung trả lời, giọng văn, lời thoại và chi tiết đã có), chỉ sửa đúng "
            f"các lỗi trên, không cắt bớt thoại của nhân vật nếu không cần, không thêm chi tiết mới. "
            f"CHỈ in bản cuối, không nhận xét, không nhắc tới bản nháp.")
