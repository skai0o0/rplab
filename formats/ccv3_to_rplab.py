"""Chuyen character card SillyTavern (CCv2/v3, JSON hoac PNG) sang rplab-char-1.

python rp/formats/ccv3_to_rplab.py card.json [card2.png ...] [-o thu_muc_ra]
Moi card -> <id>.json (nhan vat). Neu card co scenario/first_mes -> <id>.mo_dau.json (dung khi dung world).
Sau khi chuyen: doc lai, tach khoi chu dai thanh cac muc ngan, dien xung_ho/quan_he/bi_mat, kiem tra tuoi va hu_cau.
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "webui"))
import rpformat  # noqa: E402
from server import parse_card_bytes  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cards", nargs="+")
    ap.add_argument("-o", "--out", default=".")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ids = []
    for path in a.cards:
        char, mo_dau = rpformat.ccv3_to_char(parse_card_bytes(open(path, "rb").read()), ids)
        rpformat.validate_char(char)
        ids.append(char["id"])
        out = os.path.join(a.out, f"{char['id']}.json")
        json.dump(char, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"{path} -> {out}  (tuổi: {char['tuoi']}, {len(char['them'])} mục 'them', {len(char['noi']['vi_du'])} câu mẫu)")
        if mo_dau:
            mo = os.path.join(a.out, f"{char['id']}.mo_dau.json")
            json.dump(mo_dau, open(mo, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"   cảnh mở đầu tách ra -> {mo}")
        if char["tuoi"] is None:
            print("   ! chưa đọc được tuổi: hãy điền 'tuoi'")
    print("Nhớ đọc lại từng file: tách khối dài thành mục ngắn, điền xung_ho / quan_he / bi_mat, xác nhận hu_cau và tuoi.")


if __name__ == "__main__":
    main()
