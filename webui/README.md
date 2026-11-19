# RP Lab: giao diện web để test API thô

Chỉ cần Python, không phải cài thêm thư viện nào. Chạy bằng cách bấm đúp `RP_Lab.bat`, hoặc:

    python rp/webui/server.py --port 7861

rồi mở http://127.0.0.1:7861. Nếu Windows báo cổng 7861 đang bận thì đổi sang `--port 7862`.

## Có gì

- **Nhà cung cấp:**
  - OpenRouter: có ô thứ tự provider, quantization và cho phép rơi sang provider khác. Mặc định là StepFun bản fp8.
  - OpenAI-compatible tùy chỉnh: nhập Base URL và key; key chỉ lưu trong trình duyệt.
  - Google AI Studio.
  - Vertex AI express (dùng API key).
- **Reasoning:** tắt / bật (để model tự quyết) / low / medium / high. Phần suy nghĩ stream trực tiếp và nằm trong mục thu gọn được.
  - Gemini: dùng `thinkingLevel`.
  - OpenRouter: dùng `reasoning.effort`.
  - OpenAI-compatible: dùng `reasoning_effort`. Khi chọn "Tắt", chế độ này không gửi tham số nào.
- **Character card:** JSON (CCv2/v3) hoặc PNG có card nhúng. Prompt được ghép giống SillyTavern với preset trống:
  - system_prompt, mô tả, tính cách, bối cảnh, persona;
  - lorebook gồm mục cố định và mục bật theo từ khóa trong 4 tin gần nhất;
  - ví dụ hội thoại, lời chào (chọn được lời chào thay thế), depth prompt, post-history.
- **Không có card:** chat thẳng với model. Ô System prompt dùng làm system.
- **Công cụ:**
  - "Xem prompt": xem nguyên văn các tin sẽ gửi đi.
  - "Request gần nhất": xem body JSON đã gửi.
  - Sửa hoặc xóa từng tin, tạo lại tin cuối (↻), dừng khi đang stream, xuất chat ra JSON.
- **Số liệu mỗi câu trả lời:** provider, token vào/ra/suy nghĩ, thời gian ra chữ đầu, tổng thời gian, chi phí (riêng OpenRouter).
- Thiết lập, card và lịch sử chat lưu trong localStorage của trình duyệt.

## Chế độ "Nháp → soát → viết lại" (2 lượt)

Chọn ở mục **Chế độ viết**. Mỗi câu trả lời được tạo qua 2 lần gọi model:
1. **Lượt 1:** viết nháp như bình thường. Bản nháp stream lên giao diện, nằm trong mục thu gọn "Bản nháp (lượt 1)".
2. **Lượt 2:** gửi lại cùng ngữ cảnh, kèm bản nháp và một checklist soát lỗi:
   - có trả lời đúng điều người chơi vừa làm không;
   - có nhảy thời gian, đổi chỗ, thêm sự kiện hay bịa chi tiết không;
   - có mâu thuẫn với tình tiết trước không;
   - có lặp câu cửa miệng hay cử chỉ không;
   - giọng văn và xưng hô có đúng không;
   - có viết hộ người chơi không.

   Model sửa các lỗi đó rồi in bản cuối. Lượt 2 được gấp đôi Max tokens, vì soát và viết lại tốn nhiều suy nghĩ hơn.
- Có thể dùng **model khác cho lượt viết lại**, ví dụ nháp bằng DeepSeek rồi viết lại bằng Gemini.
- Dòng số liệu cộng gộp token và chi phí của cả hai lượt.

Kết quả test (DeepSeek V4.1 Flash qua StepFun, card Vy bản **gốc** vốn hay lạc đề, 4 lượt sửa slide):
- Các bản nháp tự nhắc giờ hối thúc, tự đi rửa mặt hoặc dắt xe, thêm địa điểm mới ("quán cô Ba"), bịa lỗi slide, dùng ví dụ sai logic.
- Bản cuối bỏ hết các lỗi đó, sửa ví dụ cho đúng, câu văn gọn hơn.
- Đổi lại, mỗi lượt khoảng 0.0007 USD và 9–14 giây, tức gấp khoảng 2 lần chế độ thường.
- Card đã sửa (v2) cộng chế độ 2 lượt cho kết quả tốt nhất.

## Định dạng nhân vật và thế giới riêng (`rplab-char-1`, `rplab-world-1`)

Đặc tả đầy đủ ở `rp/formats/README.md`, schema ở `rp/formats/rplab-char-1.schema.json`.
- **Tách khỏi cảnh:** file nhân vật không có `scenario`, `first_mes` hay lời chào. Cảnh mở đầu, địa điểm, luật và lore nằm trong file world.
- **Mỗi mục ngắn và độc lập:**
  - luôn nạp: `mot_dong`, `tinh_cach`, `muc_tieu`, `gioi_han`, `noi` (giọng, câu cửa miệng, câu mẫu);
  - chỉ nạp phần liên quan tới người có mặt: `noi.xung_ho` và `quan_he`, khai báo theo từng người, kể cả `nguoi_choi`;
  - đạo diễn chọn nạp: `ngoai_hinh`, `qua_khu`, từng `bi_mat` (kèm `lo_khi`, điều kiện lộ) và từng mục trong `them`.
- **Bắt buộc `tuoi` và `hu_cau: true`.** World có nhân vật `hu_cau` khác true sẽ bị từ chối. Nhân vật dưới 18 tuổi có mặt thì người viết tự nhận luật cấm nội dung tình dục hoặc lãng mạn với họ.
- **Bộ mẫu:** `rp/samples/hoa_phuong_world.json`; các nhân vật cũng có file riêng trong `rp/samples/chars/`.
- **Chuyển card SillyTavern:** `python rp/formats/ccv3_to_rplab.py card.json -o ra/`. Trang group cũng tự chuyển khi thêm nhân vật bằng card SillyTavern, rồi cho tải file mới về để chỉnh.
- **Kết quả thực tế** (cùng kịch bản): nhân vật đổi xưng hô theo từng người (Tuấn "tao - mày" với Nem, "tui - bà" với Hân); mục tiêu khiến nhân vật tự hành động (Hân vào là đếm giờ và đòi chương 2); bí mật chỉ lộ khi đúng điều kiện.

## Trình soạn bộ group chat: http://127.0.0.1:7861/editor

Đây là trang form để soạn file `rplab-world-1` mà không phải viết JSON tay.
- **Cột trái:**
  - các phần của world: Chung & người chơi, Địa điểm, Lore, Cảnh mở đầu;
  - danh sách nhân vật;
  - mục Lỗi & cảnh báo.
- **Form nhân vật:** chia theo cách nạp (luôn nạp / giọng nói / xưng hô và quan hệ / đạo diễn chọn khi cần).
  - Ô xưng hô và quan hệ tự có một dòng cho người chơi và cho từng nhân vật khác.
  - Có bộ đếm ký tự: mô tả một dòng tối đa 140, các mục khác nên dưới 300.
- **Đổi id nhân vật:** xưng hô, quan hệ của người khác và danh sách có mặt lúc mở đầu tự cập nhật theo.
- **Mở file:** nhận world, file nhân vật `rplab-char-1`, hoặc card SillyTavern (.json/.png, tự chuyển).
  - `first_mes` có thể lấy làm lời dẫn mở đầu.
  - `scenario` có thể thêm vào lore.
- **Lưu:**
  - Bản đang soạn tự lưu trong trình duyệt.
  - "Tải world .json" xuất file đã bỏ các ô trống.
  - "Tải nhân vật .json" xuất riêng một nhân vật để dùng ở world khác.
- **▶ Chơi ngay:** nhờ máy chủ kiểm tra world, rồi mở trang group với world đó. Cuộc chơi dở ở trang group sẽ bị thay (có hỏi trước).

## Group chat có "đạo diễn" (bản thử): http://127.0.0.1:7861/group

Trang riêng, mở bằng link "Group chat →" ở trang chính. Nạp **gói thế giới** (định dạng `rplab-group-1`, mẫu hư cấu ở `rp/samples/hoa_phuong_group.json`). Gói gồm:
- luật thế giới;
- địa điểm;
- **nhân vật với card tách thành trường**: ngoại hình, tính cách, cách nói, quan hệ, quá khứ, bí mật;
- lorebook;
- trạng thái ban đầu.

Mỗi lượt (`group.py`) chạy theo bốn bước:
1. **Đạo diễn:** 1 lần gọi Decisions, mặc định Microsoft-Decision-1, khoảng 1–2 giây và 0.00004 USD. Đạo diễn quyết định:
   - **ai lên tiếng**: chọn giữa các nhân vật đang có mặt và người dẫn truyện. Luật dự phòng: người chơi gọi tên ai mà đạo diễn chắc dưới 0.6 thì người đó được ưu tiên;
   - **loại lượt**: thoại, hành động, mô tả, hay chuyển cảnh;
   - **mục lore nào cần**: hỏi có/không cho từng mục, câu hỏi kèm nội dung mục, nên bắt được cả khi không ai nhắc đúng từ khóa;
   - **trường nào của card cần nạp**. Tính cách và cách nói luôn được nạp; các trường khác chỉ nạp khi liên quan. Ví dụ hỏi Tuấn về báo cáo thì trường "bí mật: làm mất file" bật 0.88, còn rủ đi ăn thì 0.01.
2. **Người viết:** model chính, nhận prompt chỉ gồm những gì đạo diễn chọn cộng với trạng thái thế giới và diễn biến gần đây. Người dẫn truyện được cho nhân vật phụ nói, và được đưa nhân vật chính khác vào cảnh.
3. **Cổng chấm** (tùy chọn): như trên, có hai ngoại lệ. Lượt "▶ Tiếp diễn" không xét "trả lời đúng tin cuối" và "mở diễn biến mới", vì diễn biến mới chính là điều người chơi muốn. Lượt chuyển cảnh do người chơi đề nghị thì được đổi địa điểm.
4. **Cập nhật trạng thái:** chạy song song hai việc.
   - Decisions: ai đang có mặt (hỏi có/không từng nhân vật), địa điểm (chọn trong danh sách), độ căng.
   - Extractions (`POST /api/alpha/extractions`, model `cloudflare/clairic`): thời gian.

   Extractions **lấy nguyên đoạn chữ có trong văn bản**, không tóm tắt. Vì vậy nó chỉ hợp với trường ngắn như thời gian hay địa danh; trích danh sách thì ra các mảnh vô nghĩa.

Ngoài ra còn chặn lặp: nếu bản cuối giống tin trước trên 60% thì tự viết lại một lần.

**Giao diện:**
- **Cột trái:** bảng trạng thái thế giới (địa điểm, thời gian, độ căng, ai có mặt) và danh sách nhân vật.
- **Dưới mỗi lượt:** ba mục thu gọn "Đạo diễn" (xác suất ai nói, trường và lore được nạp), "Chấm" và "Trạng thái sau lượt".
- **Nút:** "▶ Tiếp diễn" để truyện tự chạy, "↶ Hoàn tác" khôi phục cả trạng thái, và xuất JSON.
- **Trên từng tin** (hiện rõ khi rê chuột):
  - "↻ tạo lại": viết lại câu đó, vẫn do đúng nhân vật đó nói. Trạng thái thế giới quay về trước câu đó. Nếu phía sau còn tin khác thì các tin đó bị xoá (có hỏi trước).
  - "sửa": chỉ sửa chữ.
  - "xoá": xoá một tin. Nếu là tin cuối thì trạng thái cũng quay về trước tin đó.

**Kết quả test 5 lượt** (DeepSeek V4.1 Flash): đạo diễn chọn đúng người, cảnh chuyển xuống căn tin khi người chơi rủ, nhân vật phụ (cô Sáu) xuất hiện qua lore, Hân được đưa vào khi được hỏi tới. Mỗi lượt khoảng 0.0005–0.0013 USD và 9–17 giây; phần đạo diễn, chấm và trạng thái chỉ khoảng 0.00013–0.00025 USD.

**Bổ sung sau lượt chơi thử đầu tiên:**
- **Cổng chấm hiểu việc do người chơi dẫn tới.** Có thêm một câu hỏi phụ dạng khẳng định: "sự việc mới có phải do người chơi vừa nhắc tới, đề nghị hay dẫn tới không?". Nếu ≥ 0.5 thì "mở diễn biến mới" không tính là lỗi.
  - Thử gộp điều kiện này vào câu hỏi gốc bằng câu phủ định dài thì model Decisions rối hẳn: chặn oan 12/17 câu tốt.
  - Tách thành câu hỏi riêng rồi kết hợp bằng code: chặn oan 2/17, vẫn bắt 9/10 lỗi.
- **Chấm lại bản sửa:** nếu bản sửa có điểm thấp hơn bản nháp thì giữ bản nháp. Hướng dẫn sửa dặn giữ nguyên thoại của các nhân vật khác, vì trước đây bản sửa xóa sạch thoại của Vy và Hân.
- **Phản ứng dây chuyền:** cài đặt "số lượt tự chen vào", mặc định 1. Khi cập nhật trạng thái, hệ thống hỏi thêm từng nhân vật đang có mặt "có lý do rõ ràng để lên tiếng ngay không?". Ai ≥ 0.75 thì tự nói tiếp, đạo diễn bị ép chọn người đó. Người vừa bước vào cảnh trong lượt đó cũng được hỏi.
- **Nhận ra người vừa xuất hiện kể cả khi không gọi tên** ("thấy lớp phó đến"):
  - Mỗi lượt, đạo diễn hỏi từng nhân vật vắng mặt xem họ có vừa tới hoặc đang đứng ở cửa không. Câu hỏi kèm mô tả một dòng của nhân vật, nên model khớp được chức danh với đúng người.
  - Câu hỏi "ai đang có mặt" và "ai muốn chen vào" khi cập nhật trạng thái cũng kèm mô tả này. Người đứng ở cửa và thấy mọi người được tính là có mặt.
  - Người vừa bước vào mà chưa nói câu nào **luôn** được lên tiếng ở lượt nối tiếp.
- **Nới luật nối lượt:** ngưỡng "muốn chen vào" hạ từ 0.75 xuống 0.6, và mặc định cho tối đa 2 lượt nối tiếp sau mỗi lượt (trước là 1).
- **"Người chơi đang nói với ai?"** Đạo diễn hỏi riêng câu này mỗi khi người chơi vừa nhắn. Model Decisions làm tốt câu hỏi một bước hơn là câu phải suy luận nhiều bước.
  - Nếu model chắc ≥ 0.55 rằng người chơi đang nói với một nhân vật, nhân vật đó lên tiếng, ghi đè lựa chọn "ai nói tiếp".
  - Nhắc tên ở ngôi thứ ba ("Tuấn nó đang bệnh") không tính là đang nói với người đó.
- **Chưa đáp lời:** người bị hỏi mà lượt đó người khác nói thì được ưu tiên chen vào ngay sau. Chỉ cần điểm "muốn chen" ≥ 0.2.
- **Im lặng lâu:** trạng thái đếm số lượt mỗi người có mặt mà không nói.
  - Đạo diễn thấy con số này và ưu tiên người im lâu khi người chơi nói chung với cả nhóm.
  - Người im từ 3 lượt trở lên được chen vào với ngưỡng 0.35 thay vì 0.75.
- **Nhân vật đang tới:** nhân vật vắng mặt mà người chơi nhắc tên trong 2 tin gần nhất được coi là "đang tới". Đạo diễn được hỏi về họ như người có mặt. Nếu đạo diễn vẫn chọn người dẫn truyện trong khi người chơi vừa nói với họ, hệ thống chuyển lượt cho người đang tới có xác suất cao nhất.
- **Chống cụt chữ:** max_tokens của writer tối thiểu 2000 (mặc định 2500). Nếu bản nháp vẫn bị cắt (finish_reason = length), cổng chấm đánh trượt `bi_cat` và bắt viết lại.
- **Trạng thái có quán tính:** vào cảnh cần ≥ 0.5, rời cảnh cần < 0.2, để không mất nhân vật chỉ vì ai đó hỏi "Tuấn đâu rồi?".
- **Tóm tắt dài hạn:** người viết chỉ nhận 14 lượt gần nhất nguyên văn. Cứ đủ 8 lượt cũ hơn thì model viết (tắt reasoning) gộp chúng vào bản tóm tắt; bản tóm tắt hiện ở cột trái.
- **Thời gian giữ thứ trong tuần** khi Extractions chỉ trả về giờ.
- **Lời thoại đặt trong ngoặc kép.** Lượt của người dẫn truyện được nạp cách xưng hô của từng nhân vật.
- **Thêm nhân vật từ character card** (JSON hoặc PNG) vào gói đang chơi. Card thường được tách thành các trường mô tả, tính cách, cách nói (lấy từ system prompt và ví dụ hội thoại). Card có `extensions.rp_fields` thì dùng nguyên các trường đó. Lorebook nhúng trong card được gộp vào lore chung.
- **Import Lorebook riêng vào group chat:** Nạp file World Info (.json từ SillyTavern), khối character_book hoặc card (.json, .png). Bật/tắt từng lorebook, xem chi tiết từng mục, tự động quét theo từ khóa / cố định / đệ quy và giới hạn ngân sách token (chạy qua engine `lore.py`); có nút "gộp" để chuyển các mục vào gói thế giới cho đạo diễn chọn trực tiếp. Dưới mỗi lượt chat có mục thu gọn "Lorebook: N mục bật" hiển thị rõ ràng.

**Còn tồn tại:**
- Nhân vật đang nói đôi khi viết luôn thoại của nhân vật chính khác, thay vì để người đó tự nói ở lượt sau.
- Bộ chấm còn hay nhầm "viết hộ người chơi" ở lượt của người dẫn truyện; nhờ chấm lại nên thường vẫn giữ được bản nháp.
- Thời gian ít khi được cập nhật.

## Chế độ "Nháp → chấm → chỉ viết lại khi trượt" (cổng chấm)

Chỉ tốn thêm lượt viết lại khi thật sự cần.
1. Model chính viết nháp.
2. Một **model Decisions** của OpenRouter (`POST /api/alpha/decisions`) chấm bản nháp bằng 4 câu hỏi có/không. Model này không sinh chữ, chỉ trả về xác suất, mất khoảng 1–2 giây và khoảng 0.00003 USD mỗi lần:
   - có trả lời đúng tin cuối của người chơi không;
   - có tự mở diễn biến mới không;
   - có viết hộ người chơi không;
   - có giọng trợ lý AI không.
3. **Mọi tiêu chí đạt với độ chắc ≥ ngưỡng** (mặc định 0.8): xuất thẳng bản nháp, không gọi model lần hai.
4. **Trượt, hoặc bộ chấm không đủ chắc:** gọi lại model chính, nhưng **chỉ yêu cầu sửa đúng lỗi bị bắt**. Bản nháp bị cắt cụt vì hết token cũng tính là trượt.
5. Dưới mỗi câu trả lời có dòng "Chấm: ĐẠT/TRƯỢT", bấm vào xem xác suất từng tiêu chí.

Tiêu chí và cách gọi nằm trong `judge.py`.

**Chọn model chấm.** Đo trên 24 câu trả lời tiếng Việt có nhãn, bộ dữ liệu và script ở `rp/gate_eval/`:

| Model chấm | Bắt lỗi | Chặn oan câu tốt | Cổng 0.8: xuất thẳng / lọt lỗi | $ mỗi lần |
|---|---|---|---|---|
| **microsoft/microsoft-decision-1** (mặc định) | 9/10 | 0/14 | 14/24 / 1 | 0.000031 |
| google/gemini-decision-1 | 9/10 | 0/14 | 10/24 / 1 | 0.000091 |
| openai/gpt-6-luna-decisions | 8/10 | 3/14 | 12/24 / 2 | 0.000135 |
| typesafe/jev-1.14 | 9/10 | 4/14 | 0/24 (không bao giờ đủ chắc với tiếng Việt) | 0.000047 |

Nhãn chuẩn ban đầu do Gemini 3.8 Flash (thinking high) dán, sau đó sửa tay 5/24 chỗ sai. Các model Decisions thường đúng ở đúng những chỗ Gemini dán nhãn sai. 24 mẫu là ít, nên coi đây là số đo sơ bộ.

**Chạy thật** (DeepSeek V4.1 Flash qua StepFun, 6 lượt sửa slide):
- **Card Vy gốc:** 2/6 lượt xuất thẳng. 4 bản nháp lạc đề (nhắc giờ ngủ, hối thúc, kéo cuộc họp chi đoàn vào) đều bị chặn, bản viết lại bỏ đúng phần lạc đề mà vẫn giữ phần góp ý.
- **Card Vy v2:** 5/6 lượt xuất thẳng. Một lượt bị đẩy đi viết lại vì bộ chấm phân vân (0.47).
- **Chi phí:** lượt qua thẳng khoảng 0.0005–0.0008 USD; lượt viết lại khoảng 0.0007–0.0009 USD.

## Lorebook (World Info)

Ở cột trái (ở cả trang **chat đơn** và **group chat**), mục **Lorebook** nạp được một hoặc nhiều file `.json` hoặc file card (`.json` / `.png`): World Info xuất từ SillyTavern, khối `character_book`, hoặc chính file card (lấy lorebook nhúng trong card). Mỗi file bật/tắt riêng được, bấm **xem** để xem danh sách mục. Lorebook dùng được cả khi không có card. Ở group chat, mỗi lorebook còn có nút **gộp** để chuyển nhanh các mục vào gói thế giới hiện tại.

**Cách engine (`lore.py`) bật một mục:**
1. Mục **cố định** (constant) luôn bật.
2. Quét N tin gần nhất. Mục bật khi có một **từ khóa chính** khớp (không phân biệt hoa thường; viết dạng `/mẫu/i` thì hiểu là regex) **và** thỏa điều kiện **từ khóa phụ**. Điều kiện theo `selectiveLogic` của ST: 0 = có ít nhất một, 1 = không đủ tất cả, 2 = không có cái nào, 3 = có đủ tất cả.
3. **Khớp nguyên từ** bật sẵn, rất quan trọng với tiếng Việt: không có nó thì "Hân" sẽ khớp nhầm vào "n**hân** vật". Mục nào trong file có ghi `matchWholeWords` thì theo mục đó.
4. **Đệ quy** (tối đa 3 vòng): nội dung các mục đã bật được quét tiếp để bật mục khác. Ví dụ: nhắc tới Tuấn thì mục Tuấn bật; trong mục Tuấn có tên Hân, nên mục Hân bật theo. Mục có `preventRecursion` không kéo theo mục khác; mục có `excludeRecursion` không bị kéo vào qua đệ quy.
5. **Xác suất** (`useProbability`) và **ngân sách token**: ưu tiên mục cố định trước, rồi tới mục có order cao hơn.

**Vị trí chèn:**
- *before*: trước mô tả nhân vật (position 0 của ST).
- *after*: sau bối cảnh và persona (position 1, mặc định).
- *depth*: chèn vào lịch sử ở độ sâu N, với role system/user/assistant (position 4).
- Các vị trí khác của ST (Author's Note, ví dụ hội thoại, outlet) đều được đưa về *after*.

**Chưa hỗ trợ:** sticky, cooldown, delay, nhóm (inclusion group), thời gian hiệu lực. Đây là các tính năng nâng cao của ST.

**Debug:** mỗi câu trả lời có mục thu gọn "Lorebook: N mục bật", ghi rõ từng mục bật vì từ khóa nào, hay đệ quy từ mục nào, hay bị bỏ vì ngân sách. Nút "Xem prompt" cũng liệt kê các mục này ở đầu.

File mẫu (nhân vật hư cấu): `rp/samples/world_sim_card.json` và `rp/samples/hoa_phuong_world_info.json`.

## Key

Đọc từ `.env` của project: `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `VERTEX_API_KEY`. Nếu không có `VERTEX_API_KEY` thì dùng `GEMINI_API_KEY`. Key không bao giờ được gửi ra trình duyệt. Server chỉ nghe ở 127.0.0.1.

## Ghi chú với Gemini

Gemini không có tin system ở giữa cuộc hội thoại. Vì vậy system prompt đầu tiên được đặt vào `systemInstruction`, còn depth prompt và post-history được gộp vào cuối tin user gần nhất, giống cách SillyTavern xử lý.
