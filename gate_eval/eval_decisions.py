"""Do do chinh xac cua cac model Decisions (OpenRouter) so voi nhan vang trong dataset.json.

python rp/gate_eval/eval_decisions.py
"""
import json, os, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from criteria import CRITERIA, passes  # noqa: E402

KEY = [l.split("=", 1)[1].strip() for l in open(os.path.join(HERE, "..", "..", ".env"), encoding="utf-8")
       if l.startswith("OPENROUTER_API_KEY=")][0]
MODELS = ["typesafe/jev-1.14", "google/gemini-decision-1", "openai/gpt-6-luna-decisions", "microsoft/microsoft-decision-1"]
CHAR, USER = "Vy", "Nem"
# sua tay 2 nhan vang sai cua Gemini (xem giai thich trong README)
OVERRIDES = {"v1_run1#5": {"mo_dien_bien_moi": True}, "v1_run1#6": {"mo_dien_bien_moi": False},
             "v1_run2#3": {"mo_dien_bien_moi": True}, "v1_run2#4": {"mo_dien_bien_moi": True},
             "v2_run1#5": {"mo_dien_bien_moi": True}}


def ask(model, it):
    body = {"model": model,
            "state": {f"tin_truoc_cua_{CHAR}": it["prev"], f"tin_cuoi_cua_{USER}": it["user"], f"cau_tra_loi_cua_{CHAR}": it["reply"]},
            "questions": {k: {"type": "noul", "instructions": v["q"].format(char=CHAR, user=USER)} for k, v in CRITERIA.items()}}
    req = urllib.request.Request("https://openrouter.ai/api/alpha/decisions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "X-Title": "RP Lab"})
    for attempt in range(3):
        try:
            t = time.time()
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            return {"p": {k: d["answers"][k]["noul"] for k in CRITERIA}, "cost": d["usage"].get("cost") or 0,
                    "secs": time.time() - t}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"}


def main():
    data = json.load(open(os.path.join(HERE, "dataset.json"), encoding="utf-8"))
    for it in data:
        it["gold"].update(OVERRIDES.get(it["id"], {}))
        it["ok"] = passes(it["gold"])
    json.dump(data, open(os.path.join(HERE, "dataset.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n_bad = sum(not it["ok"] for it in data)
    print(f"{len(data)} mẫu: {len(data) - n_bad} đạt, {n_bad} trượt\n")
    results = {}
    with ThreadPoolExecutor(8) as ex:
        futs = {(m, it["id"]): ex.submit(ask, m, it) for m in MODELS for it in data}
        for key, f in futs.items():
            results[key] = f.result()
    json.dump({f"{m}|{i}": r for (m, i), r in results.items()}, open(os.path.join(HERE, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"{'model':32} {'đúng/tiêu chí':>13} {'bắt lỗi':>8} {'cho qua oan':>12} {'tự quyết ≥90%':>14} {'sai khi tự quyết':>17} {'giây':>5} {'$/lần':>9}")
    for m in MODELS:
        rs = [(it, results[(m, it["id"])]) for it in data]
        errs = [r for _, r in rs if "error" in r]
        rs = [(it, r) for it, r in rs if "error" not in r]
        crit_ok = sum((r["p"][k] >= 0.5) == it["gold"][k] for it, r in rs for k in CRITERIA)
        pred_ok = lambda r, th=0.5: all((r["p"][k] >= th) if v["good"] else (r["p"][k] < 1 - th) for k, v in CRITERIA.items())
        caught = sum(not pred_ok(r) for it, r in rs if not it["ok"])
        false_pass = sum(pred_ok(r) for it, r in rs if not it["ok"])
        # cong 90%: tu xuat khi moi tieu chi deu tot voi do chac >= 0.9
        auto = [(it, r) for it, r in rs if pred_ok(r, 0.9)]
        auto_wrong = sum(not it["ok"] for it, _ in auto)
        cost = sum(r["cost"] for _, r in rs) / max(1, len(rs))
        secs = sum(r["secs"] for _, r in rs) / max(1, len(rs))
        print(f"{m:32} {crit_ok:>6}/{len(rs) * len(CRITERIA):<6} {caught:>4}/{n_bad:<3} {false_pass:>8}/{n_bad:<3} "
              f"{len(auto):>8}/{len(rs):<5} {auto_wrong:>12}/{len(auto):<4} {secs:>5.2f} {cost:>9.7f}" + (f"  lỗi API: {len(errs)}" if errs else ""))
    print("\nChi tiết xác suất 'mở diễn biến mới' (gold T=trượt):")
    for it in data:
        row = " ".join(f"{results[(m, it['id'])].get('p', {}).get('mo_dien_bien_moi', -1):.2f}" for m in MODELS)
        print(f"  {it['id']:12} gold={'T' if it['gold']['mo_dien_bien_moi'] else '.'}  {row}")


if __name__ == "__main__":
    main()
