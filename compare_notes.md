# So sánh: DeepSeek V4.1 Flash (tắt và bật reasoning) với Gemini 3.8 Flash (thinking)

Ba cấu hình dùng chung card Vy, chung 20 câu của Nem (`script_lines.json`), temperature 1.0. Bản đặt cạnh nhau đầy đủ theo từng lượt ở `compare_3way.md`.

## Số liệu

| Cấu hình | Giây/lượt | Token thinking | Chi phí 20 lượt |
|---|---|---|---|
| DeepSeek, reasoning tắt (StepFun FP8) | 3.6 | 0 | **0.009 USD** |
| DeepSeek, reasoning bật (StepFun FP8) | 5.3 | 2.8K (khoảng 140 mỗi lượt) | 0.013 USD |
| Gemini 3.8 Flash, thinking medium | 13.3 | 11.2K (khoảng 560 mỗi lượt) | **khoảng 0.134 USD** |

Gemini đắt khoảng 10–15 lần DeepSeek và chậm khoảng 3 lần. Token thinking tính giá như token đầu ra. Chi phí Gemini là ước tính theo giá công bố 0.75 / 3.75 USD mỗi 1M token, giá ưu đãi tới 31/12/2026; từ 2027 giá tăng gấp đôi.

## Văn phong

- **Gemini 3.8 Flash** có văn phong hay nhất rõ rệt.
  - Từ vựng tiếng Việt giàu và có hình ảnh: "bẻ đôi đôi đũa tre nghe một tiếng tách giòn rụm", "ăn nguyên con vịt vô sổ thầy Phúc", "phần công đức vô lượng t thưởng cho ô", "tui quẹt momo trả".
  - Câu thoại có nhịp, nói giọng miền Nam nghe tự nhiên như người thật.
  - Chế độ nhắn tin sạch nhất: chỉ có các tin nhắn ngắn, không kèm đoạn kể chuyện.
  - Nó bỏ định dạng `*hành động*` của card và chuyển sang văn xuôi thường. Nếu bro muốn giữ dấu sao thì phải nhắc trong prompt.
- **DeepSeek bật reasoning** khá hơn bản tắt thấy rõ.
  - Câu dài hơn khoảng 25%, có lớp lang, chi tiết đời thường hợp lý hơn (giấu điện thoại dưới gầm bàn họp, "chắc tui bị khổ dâm rồi").
  - Phần suy nghĩ ngắn, bằng tiếng Việt, đúng việc: tránh lặp cử chỉ, chọn hành động mới cho nhân vật.
- **DeepSeek tắt reasoning** ổn nhưng phẳng nhất: hay dùng câu mẫu ("Thiệt hông vậy ba" xuất hiện ngay lượt 1), ý tưởng cho từng lượt ít hơn.

## Bám vai và câu phá vai (lượt 12)

Cả ba **đều không phá vai**. Cả ba đều tóm tắt lại các sự kiện **bằng giọng của Vy**, kiểu "ông ngủ dậy lú hả, để tui nhắc lại nè". So với lần chạy trước (reasoning tắt, câu chữ hơi khác), lần này DeepSeek chịu tóm tắt chứ không lờ đi.
- Gemini khéo nhất: Vy áp tay lên trán Nem coi có sốt không, rồi kể lại một mạch bằng lời của Vy.
- DeepSeek bật reasoning kể chi tiết nhất nhưng thêm vào một chuyện không có ("thi lọc danh sách ai xong trước thua bao kem").
- Phần suy nghĩ của DeepSeek ở lượt này chuyển sang tiếng Anh ("The user is asking OOC…"). Nó tự phân tích rồi chọn cách tóm tắt trong vai. Phần suy nghĩ của Gemini luôn bằng tiếng Anh, viết ở ngôi thứ nhất như thể nó là Vy ("I'm supposed to be Vy…").

## Lặp từ và xưng hô

- Không cấu hình nào trượt sang "cậu - tớ", "tôi" hay "Chắc chắn rồi!".
- Đếm cả 20 lượt: "đại ca" xuất hiện ở DeepSeek tắt 5 lần, DeepSeek bật 7 lần, Gemini 8 lần. "Ủa chứ" xuất hiện 2–3 lần ở cả ba. Đều ở mức chấp nhận được.
- "Hú hồn chim én" không lặp lại lần nào. Lần chạy trước nó bị lặp, có lẽ do lượt mở đầu đẩy nó lên.

## Kết luận

- **Nếu cần chất lượng văn chương tiếng Việt:** Gemini 3.8 Flash với thinking hơn hẳn. Đúng như bro nói, khác biệt nhìn thấy ngay.
- **Nếu chat nhiều và cần rẻ:** DeepSeek V4.1 Flash **bật reasoning**. Chỉ đắt hơn bản tắt khoảng 40%, nhanh hơn Gemini, và đỡ "gò bó" rõ rệt. Không có lý do gì để tắt reasoning khi roleplay.
- **Cách kết hợp:** dùng Gemini cho các cảnh quan trọng hoặc mở đầu, dùng DeepSeek bật reasoning cho các lượt chat thường. SillyTavern cho đổi model giữa chừng mà không mất lịch sử.
