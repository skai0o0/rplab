---
name: rplab-v2-creator
description: >-
  Hướng dẫn tạo và kiểm tra Character Card (char-v2) và Group World (world-v2) chuẩn cho
  Vietnamese Decision-Driven Group RP Engine (RP Lab v2). Kích hoạt khi người dùng yêu cầu
  tạo nhân vật mới, thiết kế thế giới group chat, chuyển đổi kịch bản roleplay sang JSON,
  hoặc kiểm tra hợp lệ card/world cho RP Lab.
---

# RP Lab v2 — Hướng Dẫn Tạo Character Card (`char-v2`) & Group World (`world-v2`)

Hệ thống RP Lab v2 tách bạch dữ liệu thành **3 lớp độc lập** để một nhân vật có thể tái sử dụng ở nhiều thế giới khác nhau mà không bị dính cứng kịch bản hay bối cảnh cũ:
- **`char-v2` (Diễn viên):** Tính cách, giá trị, ngoại hình, chất giọng 2 chế độ (`in_person` / `texting`), xưng hô mặc định theo vai vế (`pronoun_defaults`) và các `modules` độc lập. **Tuyệt đối không** nhét mục tiêu kịch bản cụ thể, bí mật của thế giới, tên nhân vật khác hay `opening` vào `char-v2`.
- **`world-v2` (Sân khấu & Kịch bản):** Luật thế giới, đồng hồ khởi tạo (`clock`), cây địa điểm phân cấp (`locations`), lore phân tầng (`lore`), phân vai nhân vật (`cast` kèm `goal`, `secrets`, `start_location`), đồ thị quan hệ/xưng hô bất đối xứng (`relationships.edges`), sự kiện nền (`initial_facts`) và lời dẫn mở đầu (`opening`). Khi xuất file world trọn gói để chơi ngay, nhúng map `characters` bên trong `world-v2`.

---

## 1. Quy Tắc Cứng & Ràng Buộc Schema (Bắt Buộc Tuân Thủ)

1. **Giữ nguyên tên khóa (key) tiếng Anh** đúng như cấu trúc JSON chuẩn; chỉ viết nội dung giá trị bằng tiếng Việt (viết ở ngôi thứ ba, mô tả sự thật khách quan — **không** viết lệnh kiểu *"Hãy đóng vai..."*).
2. **Định danh (`id`):** Chỉ dùng chữ thường không dấu `a-z`, số `0-9` và dấu gạch dưới `_` (khớp `^[a-z0-9_]+$`), duy nhất trong phạm vi world/cast/lore/locations.
3. **An toàn & Độ tuổi:**
   - `meta.fictional` **bắt buộc phải là `true`** (hệ thống từ chối nạp nhân vật nếu khác `true`).
   - `meta.age` nên ghi số nguyên rõ ràng (ưu tiên `>= 18`; nếu `< 18`, hệ thống tự động chèn giới hạn cấm nội dung tình dục/lãng mạn ở mọi lượt có mặt).
4. **Giới hạn độ dài để tối ưu KV Cache & Decision Sensors:**
   - `core.one_liner`: **≤ 140 ký tự** (dùng cho Roster 1 dòng ở Zone 1 và Đạo diễn nhận diện nhanh).
   - `modules[].content`: **≤ 300 ký tự** mỗi mục (muốn nhiều chi tiết thì tách thành nhiều module nhỏ).
   - `initial_facts[].text`: **< 25 từ** mỗi mục (sự thật nguyên tử ngắn gọn).
   - `director_critical` modules: **tối đa 1–2 mục** mỗi nhân vật (tránh làm phình số câu hỏi ở Pha 1A).

---

## 2. Hướng Dẫn Thiết Kế Từng Phân Hệ Tiếng Việt

### 2.1. Cây Địa Điểm (`locations`) & Tag Riêng Tư / Công Cộng
- Lưu dạng từ điển phẳng, mỗi node trỏ `parent` (node gốc đặt `"parent": null`) để engine tự sinh breadcrumb (VD: `Phòng 304 > Khu B > KTX Hoa Phượng`) và tự kế thừa `lore_ids` từ node cha.
- **Quy tắc gắn `tags` để chọn xưng hô đúng:**
  - `public`: Nơi đông người/công cộng (căn tin, sân trường, hành lang, giảng đường) → kích hoạt tầng xưng hô `public_formal`.
  - `private_possible`: Phòng riêng hoặc góc vắng (phòng ký túc xá, phòng CLB kín, quán cà phê góc khuất) → khi `in_person` chỉ có `user` + **đúng 1 NPC** và `tension < 3`, hệ thống kích hoạt tầng `private`.
  - `indoor`: Không gian trong nhà.
  - **Lưu ý quan trọng:** Không gắn tag `public` vào phòng riêng có `private_possible`, vì `public_formal` có độ ưu tiên cao hơn `private`.
- Đặt `travel_min` (phút) hợp lý (1–15 phút) để đồng hồ tự cộng giờ khi di chuyển.

### 2.2. Giọng Nói 2 Chế Độ (`voice.modes`) & Xưng Hô Bất Đối Xứng (`relationships.edges`)
- **`voice.modes.in_person`:** Văn nói trực tiếp, lồng cử chỉ vào văn xuôi, lời thoại nằm trong ngoặc kép `"..."`, có `tics` (câu cửa miệng dùng thỉnh thoảng) và **3 `samples` ngắn đậm khẩu ngữ/từ đệm** để đóng vai trò mỏ neo văn phong tiếng Việt cho cả Writer lẫn Translator.
- **`voice.modes.texting`:** Tin nhắn điện thoại qua Zalo/Messenger — câu ngắn tách dòng `\n`, giữ đúng thói quen gõ của nhân vật (teencode, không dấu, viết tắt hoặc đúng chuẩn ngữ pháp nghiêm túc) cùng 3 `samples` thực tế.
- **`voice.tone`:** Chỉ mô tả nhịp nói, sắc thái, phương ngữ — **không** viết cứng cặp xưng hô vào `tone` (để ma trận quan hệ quản lý động theo ngữ cảnh).
- **`voice.pronoun_defaults`:** Khai báo 4 mức fallback (`close_peer`, `acquaintance`, `elder_formal`, `angry`) để dùng khi cạnh trong world để trống.
- **`relationships.edges` trong `world-v2`:**
  - Đồ thị **có hướng và bất đối xứng** (`edges[A][B]` độc lập với `edges[B][A]`, dùng key `"user"` cho người chơi).
  - Hệ thống chọn tầng xưng hô theo thứ tự ưu tiên cứng: **`tense` > `public_formal` > `private` > `default`**.
  - Luôn điền `default.ref_3rd` (cách nhân vật gọi tên ngôi thứ 3 khi người kia vắng mặt).
  - Bổ sung `overrides.tense` (khi `tension >= 3` hoặc `mood == "angry"`), `overrides.public_formal`, và `overrides.private` cho các cặp có sự chuyển đổi xưng hô theo hoàn cảnh (VD: đàn chị gọi `"em"` ở `default`, trêu `"cưng"` ở `private`, đổi sang `"tôi - cậu"` ở `tense`).

### 2.3. Phân Tầng Lore & Character Modules (Chống Persona Bleed)
- **World `lore`:**
  - `constant`: Luôn nạp ở Zone 1 (chỉ dùng cho 2–4 quy tắc/nền tảng tối quan trọng). Riêng các mục bảng từ vựng/thuật ngữ cho bước dịch, đặt tiêu đề chứa từ khóa **`Glossary` / `Từ vựng` / `Thuật ngữ` / `Văn phong` / `Tiếng lóng`** để Translator tự ưu tiên nạp.
  - `keyword`: Quét nguyên từ `0ms` ở Pha 2 (`whole_words: true` — nên chia nhỏ theo từng mục và dùng từ đơn hoặc cụm 2–3 từ tiếng Việt người chơi hay gõ làm `keys`; hoặc gắn vào `location.lore_ids`).
  - `director`: Đạo diễn chấm xác suất `noul` theo ngữ cảnh trừu tượng (kèm `when` mô tả rõ điều kiện bật).
- **Character `modules`:**
  - Mặc định ưu tiên `activation: "keyword"` kèm danh sách `keywords` tiếng Việt sát ngữ cảnh; tận dụng để tách các trạng thái tâm lý/tình huống riêng (ghen, giận, thân mật/NSFW...) — engine tự kích hoạt theo từ khóa và tự bật các module nhạy cảm khi `scene_vibe` lên `sensual / intimate / dramatic`.
  - Chỉ dùng `activation: "director_critical"` cho tối đa 1–2 bí mật tâm lý hoặc quá khứ trừu tượng khó bắt bằng từ khóa.

### 2.4. Phân Vai, Bí Mật, Mở Đầu & Chống Metagaming (`cast`, `secrets`, `initial_facts`, `opening`)
- Mọi mục tiêu chủ động (`goal`) đặt trong `world.cast[]` — chỉ viết **1 câu việc nền nhẹ**, tránh diễn đạt kiểu *"luôn luôn bận rộn không ngừng"* để nhân vật sẵn sàng chuyển tâm điểm ngay khi có tương tác trực tiếp hoặc biến cố.
- Mọi bí mật (`cast[].secrets[]`) bắt buộc khai báo `known_by` (thường là `[char_id]`), `reveal_when` và `keywords`. Engine sẽ giấu tuyệt đối bí mật khỏi các nhân vật không nằm trong `known_by` cho tới khi Pha 5 kích hoạt `fact_reveal`.
- Khai báo `initial_facts` cho các lịch hẹn/deadline/sự kiện đã có trước lượt 1, ghi rõ `known_by` (`["all"]` hoặc danh sách ID cụ thể).
- `opening.narration` (`first_mes`) là **tuỳ chọn**: có thể để `""` nếu người chơi muốn tự mở cảnh, hoặc chỉ viết 2–3 câu trung tính làm phao cứu sinh khi bí ý tưởng; tránh nhồi đạo cụ/việc vặt cứng nhắc dễ khiến model kẹt quán tính ở các lượt sau.

### 2.5. Tối Ưu Hóa Cho Kiến Trúc 2 Tầng (Writer ZH/EN → Translator VI) & Thiết Kế Hướng Keyword
Khi bật pipeline **Writer (ZH/EN) → Translator (VI)**, bối cảnh được lắp ghép động theo 4 Zone nên cần tách rõ **dữ liệu cho Writer** và **dữ liệu cho Translator**:
1. **Dữ liệu cho Writer (tư duy & phản xạ — viết dạng keyword `Trigger → Phản ứng`):**
   - `core.personality`, `core.character_sheet`, `modules`: ưu tiên viết dạng **gạch đầu dòng theo cấu trúc `Điều kiện / Trigger → Phản ứng tâm lý & cơ thể`** thay vì đoạn văn kể lể (paragraph).
   - **Không** nhồi luật chung kiểu *"cấm giọng AI / cấm viết hộ user / không viết thoại nhân vật khác"* vào từng card (đã có sẵn ở Zone 1 và Prompt Studio `/prompts`).
2. **Dữ liệu cho Translator (giọng nói & chất chữ tiếng Việt):**
   - `voice.modes.in_person.samples` & `voice.modes.texting.samples`: thay thế vai trò mồi văn phong của `first_mes` — đầu tư **3 câu thoại mẫu thật tự nhiên, đậm khẩu ngữ/từ đệm** cho mỗi chế độ để Translator bắt đúng nhịp tiếng Việt.
   - `rules.writing_style` trong `world-v2`: giữ gọn trong **≤ 500 ký tự**, mô tả thẳng gu văn xuôi/khẩu ngữ vì Translator đọc trực tiếp 500 ký tự đầu của mục này.
   - **Lorebook từ vựng/thuật ngữ (`Glossary`):** Các từ lóng, biệt danh, danh xưng, thuật ngữ thế giới hoặc từ vựng 18+/NSFW đặc thù nên tách thành mục lore có tiêu đề chứa `Glossary` / `Từ vựng` / `Thuật ngữ` / `Văn phong` / `Tiếng lóng`, viết gọn dạng `Từ/Khái niệm: cách dùng` (ngân sách lore truyền sang Translator tối đa ~1.500 ký tự và ưu tiên nhóm này đầu tiên).
3. **Ngoại hình (`appearance`) trực quan:**
   - Viết `appearance.summary` và `outfits` theo dạng gạch đầu dòng giàu **chi tiết thị giác & xúc giác** (dáng người, kiểu tóc, ánh mắt, đặc điểm nhận diện, mùi hương, trang phục) — chi tiết vật lý cụ thể giữ nguyên độ nét khi dịch qua 2 tầng, tránh văn hoa trừu tượng.
4. **Tên riêng & `aliases` chống sai tên:**
   - Ghi rõ tên tiếng Việt có dấu trong `meta.name` và liệt kê đầy đủ tên gọi tắt, biệt danh, chức danh vào `meta.aliases` để Đạo diễn bắt đúng `mentioned()` và Writer (khi viết nháp ZH/EN) giữ nguyên chính tả tiếng Việt, không tự phiên âm ngược.

---

## 3. Cấu Trúc JSON Chuẩn Để Tạo Mới

### 3.1. Template Character Card (`char-v2`)

```json
{
  "format": "char-v2",
  "id": "ten_id_khong_dau",
  "meta": {
    "name": "Họ và Tên đầy đủ",
    "aliases": ["Tên gọi tắt", "Biệt danh", "Chức danh"],
    "age": 20,
    "gender": "nữ | nam | khác",
    "fictional": true
  },
  "core": {
    "one_liner": "Mô tả nhận diện 1 câu ≤ 140 ký tự.",
    "personality": [
      "Lớp vỏ / thường ngày: điềm tĩnh, hay cà khịa nhẹ để giữ khoảng cách",
      "Trigger bị trêu hoặc áp sát → tai đỏ, nói cứng nhưng ánh mắt né tránh, tay bấu vạt áo",
      "Trigger áp lực hoặc cao trào → bỏ giọng khách sáo, phản ứng bộc trực theo cảm xúc"
    ],
    "values": "Giá trị cốt lõi hoặc nguyên tắc sống (1 câu ngắn).",
    "boundaries": [
      "Giới hạn cứng nhân vật tuyệt đối không vượt qua."
    ]
  },
  "voice": {
    "dialect": "Nam Bộ | Bắc Bộ | Trung Bộ | Trung tính",
    "tone": "Nhịp nói, sắc thái đặc trưng (KHÔNG ghi cứng cặp xưng hô vào đây).",
    "pronoun_defaults": {
      "close_peer":   { "self": "tui", "call": "ông/bà" },
      "acquaintance": { "self": "mình", "call": "bạn" },
      "elder_formal": { "self": "em", "call": "anh/chị/thầy/cô" },
      "angry":        { "self": "tôi", "call": "ông/bà" }
    },
    "modes": {
      "in_person": {
        "style": "Cách nói chuyện trực tiếp, cử chỉ hay đi kèm lời nói.",
        "tics": ["Câu cửa miệng 1", "Câu cửa miệng 2"],
        "samples": [
          "\"Câu thoại mẫu 1 đặt trong ngoặc kép đậm chất khẩu ngữ đời thường.\"",
          "\"Câu thoại mẫu 2 thể hiện nhịp ngắt câu và từ đệm đặc trưng.\"",
          "\"Câu thoại mẫu 3 lúc ngại hoặc gắt nhẹ.\""
        ]
      },
      "texting": {
        "style": "Cách nhắn tin điện thoại (tách dòng ngắn, viết tắt, dấu câu...).",
        "samples": [
          "dòng tin nhắn mẫu 1\ndòng tin nhắn mẫu 2",
          "tin nhắn mẫu 3 lúc vội hoặc trêu"
        ]
      }
    }
  },
  "appearance": {
    "summary": "Chi tiết thị giác & xúc giác ≤ 250 ký tự (dáng người, tóc, ánh mắt, đặc điểm nhận diện, mùi hương).",
    "outfits": {
      "default": "Trang phục mặc định thường ngày (chất liệu, màu sắc, dáng áo/váy).",
      "casual": "Trang phục thoải mái khi ở nhà."
    }
  },
  "modules": [
    {
      "id": "mod_backstory_1",
      "type": "backstory | habit | skill | secret",
      "activation": "keyword",
      "when": "Khi nhắc tới quá khứ hoặc sở thích.",
      "keywords": ["từ khóa 1", "từ khóa 2"],
      "content": "Nội dung ngắn gọn ≤ 300 ký tự."
    },
    {
      "id": "mod_secret_1",
      "type": "secret",
      "activation": "director_critical",
      "when": "Điều kiện ngữ cảnh để Đạo diễn bật (tối đa 1–2 mục).",
      "keywords": ["từ khóa 1"],
      "content": "Góc khuất tâm lý ≤ 300 ký tự."
    }
  ]
}
```

### 3.2. Template Group World (`world-v2` Tự Chứa `characters`)

```json
{
  "format": "world-v2",
  "id": "id_the_gioi_khong_dau",
  "meta": {
    "name": "Tên Thế Giới / Bối Cảnh",
    "genre": "Thể loại"
  },
  "rules": {
    "writing_style": "Văn xuôi tiếng Việt đời thường ≤ 500 ký tự, câu gọn giàu cảm quan, khẩu ngữ tự nhiên, không sến, cử chỉ lồng vào câu văn, thoại đặt trong ngoặc kép \"...\".",
    "world_rules": [
      "Mọi nhân vật đều hư cấu và đã trưởng thành.",
      "Tôn trọng bối cảnh thực tế và quyền biết thông tin của từng nhân vật.",
      "Không tự ý nhảy thời gian hay đổi địa điểm khi người chơi chưa đề nghị."
    ]
  },
  "user": {
    "id": "user",
    "name": "Tên người chơi",
    "aliases": ["Tên gọi khác"],
    "persona": "Mô tả ngắn gọn vai trò của người chơi trong thế giới này."
  },
  "clock": {
    "mode": "calendar",
    "start": { "year": 2027, "month": 9, "day": 15, "hour": 8, "minute": 30 }
  },
  "locations": {
    "khu_vuc_goc": {
      "name": "Tên Khu Vực Lớn",
      "parent": null,
      "desc": "Mô tả tổng quan.",
      "tags": ["campus"],
      "travel_min": 0,
      "lore_ids": ["lore_chung_1"]
    },
    "phong_rieng_1": {
      "name": "Tên Phòng Riêng",
      "parent": "khu_vuc_goc",
      "desc": "Không gian kín đáo (gắn private_possible, không gắn public).",
      "tags": ["indoor", "private_possible"],
      "travel_min": 3,
      "lore_ids": []
    },
    "khu_cong_cong_1": {
      "name": "Tên Khu Công Cộng",
      "parent": "khu_vuc_goc",
      "desc": "Nơi đông người (gắn public).",
      "tags": ["indoor", "public"],
      "travel_min": 5,
      "lore_ids": ["lore_dia_diem_1"]
    }
  },
  "lore": [
    {
      "id": "lore_chung_1",
      "title": "Bối cảnh chung cố định",
      "activation": "constant",
      "keys": [],
      "when": "",
      "content": "Thông tin nền luôn nạp ở Zone 1."
    },
    {
      "id": "lore_glossary_1",
      "title": "Glossary Từ vựng & Tiếng lóng",
      "activation": "constant",
      "keys": [],
      "when": "",
      "content": "Thuật ngữ/Slang: cách dùng tự nhiên (Translator ưu tiên đọc mục có tiêu đề Glossary/Từ vựng/Thuật ngữ)."
    },
    {
      "id": "lore_dia_diem_1",
      "title": "Đặc điểm địa điểm / nhân vật phụ",
      "activation": "keyword",
      "keys": ["từ khóa 1", "từ khóa 2"],
      "when": "",
      "content": "Tự bật khi nhắc từ khóa hoặc đứng ở địa điểm gắn lore_ids."
    },
    {
      "id": "lore_tinh_huong_1",
      "title": "Sự kiện / Luật theo tình huống",
      "activation": "director",
      "keys": ["từ khóa gợi ý"],
      "when": "Điều kiện ngữ cảnh để Đạo diễn bật mục này.",
      "content": "Nội dung sự thật về thế giới."
    }
  ],
  "characters": {
    "char_a": {
      "format": "char-v2",
      "id": "char_a",
      "meta": { "name": "Họ Tên A", "aliases": ["A", "Biệt danh A"], "age": 20, "gender": "nữ", "fictional": true },
      "core": {
        "one_liner": "Mô tả 1 câu ≤ 140 ký tự.",
        "personality": [
          "Lớp vỏ: thái độ thường ngày",
          "Trigger → Phản ứng tâm lý và cơ thể đặc trưng"
        ],
        "values": "Nguyên tắc sống.",
        "boundaries": []
      },
      "voice": {
        "dialect": "Nam Bộ",
        "tone": "Sắc thái giọng nói (không ghi cứng cặp xưng hô).",
        "pronoun_defaults": {
          "close_peer":   { "self": "tui", "call": "ông/bà" },
          "acquaintance": { "self": "mình", "call": "bạn" },
          "elder_formal": { "self": "em", "call": "anh/chị" },
          "angry":        { "self": "tôi", "call": "ông/bà" }
        },
        "modes": {
          "in_person": { "style": "Lối nói trực tiếp.", "tics": ["Câu cửa miệng"], "samples": ["\"Câu thoại mẫu 1.\"", "\"Câu thoại mẫu 2.\"", "\"Câu thoại mẫu 3.\""] },
          "texting": { "style": "Lối nhắn tin.", "samples": ["tin nhắn 1\ntin nhắn 2", "tin nhắn 3"] }
        }
      },
      "appearance": { "summary": "Ngoại hình trực quan (tóc, mắt, dáng, mùi hương).", "outfits": { "default": "Mặc định", "casual": "Ở nhà" } },
      "modules": [
        { "id": "mod_a_1", "type": "backstory", "activation": "keyword", "when": "", "keywords": ["từ khóa"], "content": "Trigger → phản ứng ≤ 300 ký tự." }
      ]
    }
  },
  "cast": [
    {
      "char_id": "char_a",
      "role_in_world": "Vai trò cụ thể trong kịch bản thế giới này.",
      "start_location": "phong_rieng_1",
      "start_outfit": "default",
      "goal": "Việc nền nhẹ nhàng lúc mở đầu (1 câu ngắn, dễ gác lại khi có tương tác).",
      "secrets": [
        {
          "id": "sec_char_a_01",
          "text": "Bí mật nhân vật đang giấu.",
          "known_by": ["char_a"],
          "reveal_when": "Điều kiện để bí mật lộ ra.",
          "keywords": ["từ khóa 1", "từ khóa 2"]
        }
      ]
    }
  ],
  "relationships": {
    "auto_bootstrap": true,
    "edges": {
      "char_a": {
        "user": {
          "tag": "quan hệ chiều char_a -> user",
          "level": 75,
          "default": { "self": "tui", "call": "ông", "ref_3rd": "Tên người chơi" },
          "overrides": {
            "private":       { "self": "em", "call": "anh" },
            "public_formal": { "self": "mình", "call": "bạn" },
            "tense":         { "self": "tôi", "call": "anh" }
          }
        }
      },
      "user": {
        "char_a": {
          "tag": "quan hệ chiều user -> char_a",
          "level": 75,
          "default": { "self": "tui", "call": "bà", "ref_3rd": "A" },
          "overrides": {}
        }
      }
    }
  },
  "initial_facts": [
    {
      "id": "fact_init_1",
      "time": "trước mở đầu",
      "text": "Sự kiện hoặc deadline đã tồn tại trước lượt 1 (< 25 từ).",
      "entities": ["user", "char_a"],
      "known_by": ["all"],
      "keywords": ["từ khóa 1"],
      "status": "active"
    }
  ],
  "opening": {
    "user_start_location": "phong_rieng_1",
    "tension": 1,
    "channel_type": "in_person",
    "narration": "*Tuỳ chọn (có thể để rỗng \"\" nếu người chơi muốn tự mở cảnh, hoặc viết 2–3 câu trung tính nêu thời gian, địa điểm và không khí mở đầu).*"
  }
}
```

---

## 4. Quy Trình Tạo & Kiểm Tra Hợp Lệ (Verification)

1. **Khi tạo xong file JSON**, kiểm tra ngay bằng `rpformat.py` (không đoán bằng mắt):
   - **Kiểm tra `char-v2`:**
     ```bash
     python -c "import sys, json; sys.path.insert(0, 'rp/webui'); import rpformat; rpformat.validate_char_v2(json.load(open('duong_dan_char.json', encoding='utf-8'))); print('char-v2 OK')"
     ```
   - **Kiểm tra `world-v2` & Turn 0 Bootstrap:**
     ```bash
     python -c "import sys, json; sys.path.insert(0, 'rp/webui'); import rpformat; p, w = rpformat.load_world(json.load(open('duong_dan_world.json', encoding='utf-8'))); s = rpformat.bootstrap_session(p); print('world-v2 OK:', p['meta']['name'], '| cast:', len(p['cast']), '| locs:', len(p['locations']), '| clock:', rpformat.format_clock(s['clock']), '| warnings:', w)"
     ```
2. **Kiểm tra chéo trước khi bàn giao:**
   - Mọi `char_id` trong `cast` đều có mặt trong `characters` (hoặc tồn tại file `rp/samples/chars/<char_id>.json`).
   - Mọi `start_location` và `opening.user_start_location` đều tồn tại trong `locations`.
   - Nếu `opening.narration` có nội dung, `clock.start` khớp ngày/thứ/giờ được mô tả trong `opening.narration`; nếu có từ 2 nhân vật trở lên ở `user_start_location`, đảm bảo `opening.narration` nhắc tự nhiên sự hiện diện của họ (còn nhân vật ở địa điểm khác phù hợp để người chơi đi gặp hoặc nhắn tin `dm_text`/`group_text`).
