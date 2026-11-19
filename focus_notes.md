# Sửa hai lỗi: suy nghĩ ít và tự mở diễn biến mới (DeepSeek V4.1 Flash, StepFun FP8)

Kịch bản test (`focus_lines.json`): 6 câu. Nem nhờ Vy sửa slide thuyết trình, mỗi câu hỏi một việc cụ thể.

| Bản | Token suy nghĩ từng lượt | Lạc đề / tự mở diễn biến |
|---|---|---|
| card gốc, effort medium | 176, 215, 71, 71, 75, 112 | Lượt 1–2: tự nhắc giờ ngủ trưa. Lượt 4: tự nhảy tới 12h45, bỏ đi rửa mặt. Lượt 5–6: hối đi học, "lẹ lên!" |
| card gốc, effort high | 41, 97, 96, 260, 48, 40 | Bịa lỗi Nem không nhắc ("trục tung cũng ghi giá", "slide 5 vô cùng vô tận"). Liên tục nhắc giờ. **Lượt 3 trả về rỗng.** |
| **card v2, effort medium** | **462, 218, 387**, 75, 45, 81 | **Không nhảy giờ, không đổi chỗ, không thêm sự kiện.** Còn một lần đoán lỗi slide (lượt 1) nhưng dưới dạng hỏi lại. |

## Lỗi 1: suy nghĩ ít

- Với DeepSeek V4.1 Flash qua StepFun, **effort gần như không có tác dụng**: bản high còn suy nghĩ ít hơn medium và có lúc trả về rỗng. Nên để **medium**.
- Độ dài suy nghĩ do model tự quyết theo độ khó của câu: câu khó (tính co giãn) thì nghĩ khoảng 400 token, câu dễ (bấm giờ) chỉ khoảng 50 token. "Thought for 7 seconds" với hai dòng kế hoạch là bình thường của bản Flash, không phải ST cắt bớt.
- Cách giúp nó nghĩ có trọng tâm hơn: **viết các bước suy nghĩ vào Post-History Instructions** (card v2 đã làm). DeepSeek làm theo khá đúng ở các câu cần nghĩ. Đừng mong nó nghĩ dài ở câu dễ; nghĩ dài cũng không làm văn hay hơn.
- Nếu thật sự cần suy nghĩ sâu, đổi sang **Gemini 3.8 Flash** (khoảng 500–900 token mỗi lượt) hoặc **DeepSeek V4 Pro**.

## Lỗi 2: tự mở diễn biến mới

- **Nguyên nhân nằm ở card, không phải ở ST.** Card gốc ghi "chủ động" tới **3 lần**: system prompt ghi "đẩy cảnh đi tiếp (xem giờ, rủ đi, bày trò)", depth prompt ghi "chủ động hành động thay vì đứng đợi", post-history ghi "không đứng chờ". Model hiểu thành "lượt nào cũng phải có chuyện mới".
- **Card v2** thay bằng "nguyên tắc tập trung":
  - ưu tiên trả lời đúng điều Nem vừa nói hoặc vừa làm;
  - cấm nhảy thời gian, đổi địa điểm, thêm nhân vật hay sự kiện, bịa chi tiết;
  - **chất lầy nằm ở cách phản ứng**, không nằm ở việc mở chuyện mới;
  - chỉ được gợi hướng tiếp theo khi Nem bỏ ngỏ.
- Áp dụng cho card khác: tìm các chữ "chủ động", "đẩy cảnh", "không đứng chờ", "bày trò" trong system prompt, depth prompt, post-history và lorebook, rồi đổi theo kiểu trên.

## File

- `vy_card_v2.json`: card đã sửa, import thẳng vào ST được. Dùng với preset "Trống" trong `st_presets/`.
- `focus_v1_medium.json`, `focus_v1_high.json`, `focus_v2_medium.json`: toàn bộ phiên chat test, kèm phần suy nghĩ.
