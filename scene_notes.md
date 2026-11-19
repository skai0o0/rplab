# Ghi chú: DeepSeek V4.1 Flash 1017 (StepFun FP8) với card Vy

**Thông số chạy**
- 20 lượt, toàn bộ đi qua **StepFun**, không lần nào rơi sang provider khác.
- Tổng chi phí **0.0097 USD**, gồm 132K token đầu vào và 5.6K token đầu ra.
- Trung bình **3.4 giây** mỗi lượt.
- Prompt mỗi lượt tăng dần từ 2.7K lên 10K token, vì lần nào cũng gửi lại toàn bộ lịch sử chat.
- Cài đặt: temperature 1.0, top_p 0.95, tắt reasoning.

Bản chat đầy đủ: `scene_transcript.md`. Có thể chạy lại hoặc chơi tiếp bằng `rp_chat.py`.

## Những gì làm tốt

| Kiểm tra | Kết quả |
|---|---|
| Bám vai, giọng | Rất tốt. Xưng "tui - ông" đủ 20/20 lượt, không lần nào trượt sang "cậu - tớ", giọng Nam Bộ tự nhiên. |
| Không thành trợ lý | Đạt. Không có câu kiểu "Chắc chắn rồi!", không tóm tắt, không hỏi "bạn muốn làm gì tiếp theo". |
| Câu phá vai, OOC (lượt 12) | Không phá vai. Model hiểu câu OOC thành "Nem đang ngơ ra nhìn điện thoại". Đến lượt 13, Vy còn nghi "ai nhắn gì mà ngơ ra vậy". |
| Hai mặt Bí thư (lượt 5, 7) | Rất tốt. Trước mặt Hân thì xin lỗi nghiêm chỉnh và nhận lỗi về mình, sau đó nháy mắt với Nem. Trong cuộc họp thì "Dạ em thấy chia theo tổ…", cúi xuống nhắn tin thì lại dùng teencode. |
| Chế độ nhắn tin (lượt 7–8, 19–20) | Kích hoạt đúng nhờ từ khóa "Zalo" trong lorebook: tin ngắn, chữ thường, teencode, "=)))", "😏". |
| Chiều sâu nhân vật (lượt 16) | Khi được hỏi nghiêm túc, Vy kể chuyện hồi năm nhất rụt rè, làm Bí thư thấy mình có ích, rồi tự đùa lảng đi. Đúng tính cách đã mô tả. |
| Kiến thức thực tế | Lượt 6: nhắc đúng "đường cầu dốc xuống, giá tăng lượng giảm". |
| Tự tìm cách gỡ chỗ mâu thuẫn | Lượt 19: bị hỏi "sáng hay trưa", model biến chỗ nói hai kiểu thành "2 bữa luôn cho máu", nghe vẫn hợp vai. |

## Lỗi gặp phải

1. **Sai chi tiết nhỏ khi chat dài**
   - Lượt 3–4: nói "xuống trước giữ thang máy" ở phòng trọ tầng trệt.
   - Lượt 14: nói đi "xe đạp" trong khi hai đứa đi xe máy.
   - Lượt 17–18: hẹn bún bò lúc thì "trưa mai", lúc thì "sáng mai".
   - Ai bao kem, ai bao bún bị đảo qua lại.
   - Đây là kiểu lỗi đặc trưng của bản Flash: không quên lore, chỉ quên **tình tiết** đã xảy ra trong chính cuộc chat.
2. **Lặp câu cửa miệng.** "Hú hồn chim én", "Ủa chứ…", "đại ca", "thiệt hông vậy ba?" xuất hiện dày. Lượt 1 còn dùng "thiệt hông vậy ba?" sai ngữ cảnh. Nguyên nhân: lorebook liệt kê sẵn các câu này, nên model coi đó là câu bắt buộc phải dùng. Cử chỉ cũng lặp: "búng/giật vành tai" ở lượt 14 và 17.
3. **Tự đẩy cảnh quá đà (lượt 8).** Model tự viết luôn cảnh Vy họp xong chạy xuống căn tin và gọi Nem, lấn sang lượt của người chơi. Nguyên nhân: system prompt bảo "chủ động đẩy cảnh". Ở mức vừa phải thì đây là điểm cộng.
4. **OOC bị nuốt luôn.** Không phá vai là tốt, nhưng khi bro thật sự cần nói ngoài vai (sửa lỗi, đổi hướng truyện) thì model phớt lờ.
5. **Lỗi định dạng lặt vặt.** Có một đoạn kể bắt đầu bằng chữ thường sau tin nhắn (lượt 7).

## Cách chỉnh để kiểm soát output

1. **Giữ tình tiết (sửa lỗi 1):** thêm một mục lorebook hoặc Author's Note ở độ sâu 1–2 tên "Sự kiện đã chốt", cập nhật tay hoặc bằng extension Summarize của SillyTavern. Ví dụ: "Đi xe máy. Phòng trọ tầng trệt. Mai: sáng bún bò, trưa bún bò. Nem bao kem." Bản Flash đọc thứ ở sát cuối prompt tốt hơn nhiều so với lịch sử dài.
2. **Bớt lặp câu cửa miệng (sửa lỗi 2):** trong lorebook đổi "Từ cửa miệng: …" thành "Ví dụ vài câu cửa miệng (dùng tối đa 1 lần mỗi 5 lượt, ưu tiên tự nghĩ câu mới): …". Có thể thêm `frequency_penalty` 0.2–0.4; StepFun có hỗ trợ tham số này.
3. **Không cho tự đẩy cảnh quá đà (sửa lỗi 3):** trong post-history, sửa "chủ động" thành "chủ động trong phạm vi cảnh hiện tại; không nhảy thời gian, không cho Vy rời hoặc đến chỗ khác trừ khi Nem đưa cảnh tới đó".
4. **Mở cửa cho OOC (sửa lỗi 4):** thêm vào system prompt: "Nếu {{user}} viết trong ngoặc (OOC: …), tạm thoát vai, trả lời ngắn gọn như người viết truyện, rồi quay lại vai."
5. **Chi phí:** prompt phình theo độ dài chat nên lượt sau tốn hơn lượt trước. Trên OpenRouter, StepFun FP8 không phải bản FP8 rẻ nhất (DeepInfra `fp8/flex` rẻ hơn), nhưng chạy ổn định và chỉ khoảng 3 giây mỗi lượt. Bản FP4 rẻ hơn nữa nhưng có thể kém hơn ở tiếng Việt; tôi chưa thử.
