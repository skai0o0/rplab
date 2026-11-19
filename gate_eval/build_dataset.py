"""Gom cac cau tra loi tu gate_eval/*.json va dan nhan vang bang Gemini 3.8 Flash (thinking high).

python rp/gate_eval/build_dataset.py   -> rp/gate_eval/dataset.json
"""
import glob, json, os, sys, urllib.request

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from criteria import CRITERIA  # noqa: E402

KEY = [l.split("=", 1)[1].strip() for l in open(os.path.join(HERE, "..", "..", ".env"), encoding="utf-8")
       if l.startswith("GEMINI_API_KEY=")][0]
CHAR, USER = "Vy", "Nem"


def items():
    out = []
    for f in sorted(glob.glob(os.path.join(HERE, "*_run*.json"))):
        s = json.load(open(f, encoding="utf-8"))
        first = s["card"]["data"]["first_mes"].replace("{{user}}", USER)
        hist = [{"role": "assistant", "content": first}] + s["history"]
        for i in range(1, len(hist), 2):
            out.append({"id": f"{os.path.basename(f)[:-5]}#{(i + 1) // 2}", "prev": hist[i - 1]["content"],
                        "user": hist[i]["content"], "reply": hist[i + 1]["content"]})
    return out


def gold(it):
    qs = "\n".join(f'- "{k}": {v["q"].format(char=CHAR, user=USER)}' for k, v in CRITERIA.items())
    prompt = (f"Bạn là người chấm chất lượng roleplay. Đánh giá CÂU TRẢ LỜI của {CHAR} theo từng tiêu chí, trả lời true (CÓ) "
              f"hoặc false (KHÔNG). Cân nhắc kỹ bối cảnh: chi tiết đã có ở tin trước không tính là mới.\n\n"
              f"[Tin trước đó của {CHAR}]\n{it['prev']}\n\n[Tin cuối của {USER}]\n{it['user']}\n\n[CÂU TRẢ LỜI cần chấm]\n{it['reply']}\n\n"
              f"Tiêu chí:\n{qs}\n\nTrả về JSON: {{\"<tiêu chí>\": true/false, ..., \"ly_do\": \"một câu ngắn\"}}")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "thinkingConfig": {"thinkingLevel": "high"}}}
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    txt = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"] if not p.get("thought"))
    return json.loads(txt)


def main():
    data = items()
    for it in data:
        g = gold(it)
        it["gold"] = {k: bool(g[k]) for k in CRITERIA}
        it["gold_reason"] = g.get("ly_do", "")
        flags = [k for k in CRITERIA if it["gold"][k] != CRITERIA[k]["good"]]
        print(f"{it['id']:12} {'ĐẠT ' if not flags else 'TRƯỢT'} {','.join(flags):30} {it['gold_reason'][:110]}", flush=True)
    json.dump(data, open(os.path.join(HERE, "dataset.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(data), "mẫu")


if __name__ == "__main__":
    main()
