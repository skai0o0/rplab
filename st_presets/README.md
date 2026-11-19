# Preset SillyTavern "trống" (dành cho ST 1.19.0)

Hai preset Chat Completion dựng từ `Default.json` của ST 1.19.0 (bản gốc lưu ở `_st_default_1.19.0.json` để đối chiếu). Mục đích là để ST không tự chèn thêm lời nào, chỉ gửi đi những gì card của bro viết.

| File | Model |
|---|---|
| `Trong - DeepSeek V4.1 Flash (StepFun FP8).json` | OpenRouter `deepseek/deepseek-v4.1-flash-1017`, khóa provider **StepFun**, quantization **fp8**, tắt fallback |
| `Trong - Gemini 3.8 Flash.json` | Google AI Studio `gemini-3.8-flash` |

## Đã dọn những gì

- **Main Prompt, NSFW/Auxiliary, Post-History Instructions:** để trống. Khi card có `system_prompt` và `post_history_instructions`, ST lấy hai trường đó điền vào hai chỗ này.
- **Enhance Definitions, NSFW:** tắt.
- **`[Start a new Chat]`:** bỏ. Dòng đánh dấu trước ví dụ hội thoại đổi thành `[Ví dụ hội thoại]`.
- **Các câu nhắc phụ:** Continue nudge, Group nudge, Impersonation dịch sang tiếng Việt. Ba câu này chỉ được gửi khi bro bấm Continue hoặc Impersonate, hoặc khi chat nhóm.
- **Thứ tự prompt:** Main (system prompt của card) → World Info (before) → Persona → Description → Personality → Scenario → World Info (after) → Ví dụ hội thoại → Lịch sử chat → Post-History (của card). Depth prompt hay Character's Note của card do ST tự chèn ở độ sâu card đã đặt.
- **Thông số:**
  - temperature 1.0, top_p 0.95, tắt hết penalty;
  - context 32K;
  - max tokens 2500 (đã tính cả token reasoning);
  - reasoning **medium**, hiện phần suy nghĩ;
  - Prompt Post-Processing để None, giống script `rp_chat.py`.

## Cách dùng

1. ST → biểu tượng ⚙️ **AI Response Configuration** → nút **Import preset** → chọn file JSON.
2. **User Settings** → bật **"Prefer Char. Prompt"** và **"Prefer Char. Instructions"**. Mặc định ST đã bật sẵn. Nếu tắt, card sẽ không ghi đè được Main và Post-History, và hai mục này sẽ trống.
3. Khi import card `vy_card.json` hoặc card khác có lorebook nhúng, bấm **đồng ý import embedded lorebook** khi ST hỏi. Không có lorebook thì chế độ nhắn tin và bối cảnh trường học sẽ không kích hoạt.
4. Preset DeepSeek: kiểm tra mục API Connections, provider phải hiện "StepFun" với quantization "fp8". Nếu danh sách provider của OpenRouter đổi tên, chọn lại bằng tay.
5. Nếu có Persona thì giữ Persona ngắn. Không có Persona thì mục đó tự trống.

## Kiểm tra prompt thực tế

Bấm ⓘ (Prompt Itemization) trên một tin nhắn bất kỳ. Phần "System" và "Extensions" chỉ nên có nội dung từ card và lorebook, không có chữ nào khác. So với lệnh:

    python rp/rp_chat.py dump --session rp/cmp_ds_on.json
