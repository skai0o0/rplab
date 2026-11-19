"""Tieu chi cham ban nhap roleplay — dung chung cho nhan vang (LLM) va cac model Decisions."""

CRITERIA = {
    "tra_loi_dung": {
        "q": "Câu trả lời của {char} có phản hồi trực tiếp và đúng vào điều {user} vừa nói, hỏi hoặc làm ở tin cuối không?",
        "good": True,
    },
    "mo_dien_bien_moi": {
        "q": "Câu trả lời có tự mở diễn biến mới khi {user} chưa chuyển cảnh không? Tính là CÓ nếu: nhảy thời gian, "
             "nhắc giờ để hối thúc sang việc khác, đổi địa điểm, cho nhân vật rời khỏi cảnh hoặc đi làm việc khác, "
             "thêm nhân vật hay sự kiện mới chưa có trong bối cảnh.",
        "good": False,
    },
    "viet_ho_user": {
        "q": "Câu trả lời có viết thay lời nói, suy nghĩ hoặc hành động của {user} không?",
        "good": False,
    },
    "giong_tro_ly": {
        "q": "Câu trả lời có giọng trợ lý AI thay vì giọng nhân vật không (xưng tôi - bạn, hỏi 'bạn muốn làm gì tiếp', "
             "tóm tắt, từ chối, tự nhận là AI)?",
        "good": False,
    },
}


def passes(labels):
    """labels: {criterion: bool (True = CÓ)} -> ban nhap dat tat ca tieu chi?"""
    return all(labels[k] == v["good"] for k, v in CRITERIA.items())
