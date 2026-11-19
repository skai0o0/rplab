# Định dạng nhân vật và thế giới của RP Lab

Character card của SillyTavern (CCv2/v3) có ba điểm không hợp với hệ thống có "đạo diễn":
1. **Nhân vật bị dính chặt vào một cảnh:** `scenario`, `first_mes` và `alternate_greetings` nằm ngay trong card. Muốn đưa nhân vật sang thế giới khác hay vào group chat thì phải sửa card.
2. **Mọi thứ dồn vào vài ô chữ dài:** `description` và `personality` là những khối văn bản lớn. Model phải nhận cả khối dù lượt đó chỉ cần một ý.
3. **Nhiều trường thừa hoặc trùng nhau:** `creator_notes`, `creatorcomment`, `talkativeness`, `fav`, `avatar`; dữ liệu `data` lặp lại ở ngoài; lorebook nhúng với cả chục trường cấu hình.

RP Lab tách làm hai loại file:
- **`rplab-char-1`:** chỉ mô tả **một nhân vật**, không có cảnh, không có lời chào. Mỗi mục ngắn và độc lập, để đạo diễn chọn nạp từng mục.
- **`rplab-world-1`:** mô tả **thế giới và cảnh mở đầu**: luật, địa điểm, lore, người chơi, trạng thái ban đầu. Danh sách nhân vật nằm ở đây, nhúng trực tiếp hoặc tham chiếu theo `id`.

Một nhân vật dùng được trong nhiều thế giới; một thế giới dùng được với nhiều dàn nhân vật.

---

## `rplab-char-1`: nhân vật

```jsonc
{
  "format": "rplab-char-1",
  "id": "vy",                         // a-z0-9_, duy nhất trong thế giới
  "ten": "Lê Phương Vy",
  "goi": ["Vy", "bà Bí thư"],         // tên gọi khác, để nhận ra khi được nhắc tới
  "tuoi": 20,                          // bắt buộc; dưới 18 thì hệ thống tự thêm giới hạn nội dung
  "hu_cau": true,                      // bắt buộc true: nhân vật hư cấu, không dựa trên người thật

  "mot_dong": "Bí thư chi đoàn, lầy lội, mồm mép, bạn thân của Nem.",   // ≤ 140 ký tự, luôn nạp
  "tinh_cach": "…",                    // luôn nạp
  "muc_tieu": "…",                     // điều nhân vật đang muốn hoặc lo lúc này; luôn nạp, đây là thứ khiến nhân vật tự hành động
  "gioi_han": ["…"],                   // luật cứng của riêng nhân vật; luôn nạp

  "noi": {                             // luôn nạp, nhưng chỉ phần liên quan tới người có mặt
    "xung_ho": { "mac_dinh": "tui - ông/bà", "nguoi_choi": "tui - ông", "han": "tui - bà" },
    "giong": "Khẩu ngữ Nam Bộ, nhanh, hay cà khịa.",
    "cau_cua_mieng": ["Ủa chứ…", "Nói nghe nè…"],   // được nhắc là chỉ dùng thỉnh thoảng
    "vi_du": ["\"Ủa chứ ông tính bỏ đói tui hả?\""] // 1–3 câu mẫu ngắn
  },

  "quan_he": {                         // chỉ nạp quan hệ với những người đang có mặt
    "nguoi_choi": "Bạn thân thuần túy từ năm nhất.",
    "han": "Đồng nghiệp ban cán sự, hay tung hứng.",
    "tuan": "Hay bắt Tuấn đi tình nguyện."
  },

  "ngoai_hinh": "…",                   // đạo diễn chọn nạp khi cần
  "qua_khu": "…",                      // đạo diễn chọn nạp khi cần
  "bi_mat": [                          // mỗi bí mật là một mục riêng, kèm điều kiện lộ
    { "noi_dung": "Quỹ chi đoàn còn thiếu 300 nghìn.", "lo_khi": "bị hỏi thẳng về tiền quỹ hoặc chuyến tình nguyện" }
  ],
  "them": { "so_thich": "…", "do_vat": "…" }   // mục tự do, mỗi khóa là một mục đạo diễn chọn được
}
```

**Quy ước:**
- Mỗi mục nên dưới khoảng 300 ký tự. Muốn viết dài thì tách thành nhiều mục trong `them`.
- Viết bằng ngôi thứ ba và mô tả sự thật. Không viết lệnh kiểu "hãy đóng vai…"; lệnh là việc của hệ thống.
- Người chơi luôn có khóa `nguoi_choi` trong `xung_ho` và `quan_he`, nên một nhân vật đi với người chơi nào cũng được.
- Bỏ được mọi mục không cần, trừ `format`, `id`, `ten`, `tuoi`, `hu_cau`, `mot_dong` và `tinh_cach`.

**Mục nào được nạp lúc nào:**

| Luôn nạp khi nhân vật lên tiếng | Nạp theo người có mặt | Đạo diễn quyết định (theo ngữ cảnh) |
|---|---|---|
| `mot_dong`, `tinh_cach`, `muc_tieu`, `gioi_han`, `noi.giong`, `noi.cau_cua_mieng`, `noi.vi_du` | `noi.xung_ho`, `quan_he` | `ngoai_hinh`, `qua_khu`, từng `bi_mat`, từng mục trong `them` |

---

## `rplab-world-1`: thế giới và cảnh mở đầu

```jsonc
{
  "format": "rplab-world-1",
  "ten": "Ký túc xá Hoa Phượng",
  "luat": "Văn phong, luật thế giới, giới hạn chung…",
  "nguoi_choi": { "ten": "Nem", "mo_ta": "Sinh viên năm hai, mới chuyển vào phòng 304." },
  "dia_diem": { "phong_304": "Phòng 304 khu B: …", "can_tin": "…" },
  "lore": [ { "id": "co_sau", "tieu_de": "Cô Sáu căn tin", "noi_dung": "…" } ],
  "nhan_vat": [ { "format": "rplab-char-1", "id": "vy", "...": "…" } ],   // nhúng trực tiếp, hoặc chỉ ghi "vy" rồi nạp file riêng
  "mo_dau": {
    "loi_dan": "*Mười hai giờ rưỡi trưa thứ Hai…*",
    "trang_thai": { "dia_diem": "phong_304", "thoi_gian": "12 giờ 30 trưa thứ Hai", "co_mat": ["tuan"], "do_cang": 1 }
  }
}
```

Muốn có cảnh mở đầu khác thì tạo một world khác hoặc chỉ sửa phần `mo_dau`; không phải đụng vào nhân vật.

---

## Chuyển từ card SillyTavern

`python rp/formats/ccv3_to_rplab.py card.json [card.png ...] -o thu_muc_ra/`

Lệnh này tạo một file `rplab-char-1` cho mỗi card:
- `description` thành `tinh_cach` (hoặc vào `them.mo_ta` nếu quá dài);
- `personality` gộp vào `tinh_cach`;
- câu thoại của nhân vật trong `mes_example` thành `noi.vi_du`;
- lorebook nhúng thành các mục trong `them`;
- `scenario` và `first_mes` được tách sang một file `*.mo_dau.json` riêng để dùng khi dựng world.

Card cũ thường viết thành khối dài, nên nên đọc lại và tách thành các mục ngắn sau khi chuyển. Trình chuyển đặt `hu_cau: true` và `tuoi` theo những gì đọc được trong card, hoặc `null` nếu không thấy. **Hãy tự kiểm tra hai trường này.**
