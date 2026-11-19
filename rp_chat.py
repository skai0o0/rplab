"""Chat roleplay voi character card (CCv3) qua OpenRouter, ghep prompt giong SillyTavern (Chat Completion).

  python rp_chat.py new  --card vy_card.json --session scene.json      # bat dau, dung first_mes
  python rp_chat.py say  --session scene.json "loi cua Nem"             # 1 luot: gui va in tra loi
  python rp_chat.py show --session scene.json                           # in lai toan bo
  python rp_chat.py dump --session scene.json                           # in prompt that su se gui (debug)

Thu tu prompt (giong ST): system = [system_prompt card] + description + personality + scenario
+ lorebook (constant + entry co keyword trong 4 tin gan nhat) + vi du hoi thoai; roi first_mes + lich su;
depth_prompt chen o do sau 4; post_history_instructions la system cuoi cung.
Key doc tu ../.env (OPENROUTER_API_KEY), khong bao gio in ra.
"""
import argparse, json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "deepseek/deepseek-v4.1-flash-1017"
PROVIDER = {"order": ["stepfun/fp8"], "allow_fallbacks": False}  # khoa StepFun ban FP8
USER = "Nem"
SCAN_DEPTH = 4


def api_key():
    for p in (os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    sys.exit("Khong thay OPENROUTER_API_KEY trong .env")


def fill(text, char):
    return (text or "").replace("{{user}}", USER).replace("{{char}}", char)


def build_messages(s):
    card = s["card"]["data"]
    char = card["name"]
    hist = s["history"]
    scan = " ".join(m["content"] for m in hist[-SCAN_DEPTH:]).lower()
    lore = []
    for e in sorted(card.get("character_book", {}).get("entries", []), key=lambda e: -e.get("insertion_order", 0)):
        if not e.get("enabled", True):
            continue
        if e.get("constant") or any(k.lower() in scan for k in e.get("keys", [])):
            lore.append(e["content"])
    ex = card.get("mes_example", "").replace("<START>", "[Ví dụ hội thoại]").strip()
    parts = [card.get("system_prompt", ""),
             f"[Nhân vật: {char}]\n{card.get('description', '')}",
             f"[Tính cách]\n{card.get('personality', '')}",
             f"[Bối cảnh]\n{card.get('scenario', '')}",
             "\n\n".join(lore),
             ex]
    msgs = [{"role": "system", "content": fill("\n\n".join(p for p in parts if p), char)}]
    chat = [{"role": "assistant", "content": fill(card["first_mes"], char)}] + \
           [{"role": m["role"], "content": m["content"]} for m in hist]
    dp = card.get("extensions", {}).get("depth_prompt", {})
    if dp.get("prompt"):
        pos = max(0, len(chat) - int(dp.get("depth", 4)))
        chat.insert(pos, {"role": "system", "content": fill(dp["prompt"], char)})
    msgs += chat
    if card.get("post_history_instructions"):
        msgs.append({"role": "system", "content": fill(card["post_history_instructions"], char)})
    return msgs


def env_key(name):
    for p in (os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    sys.exit(f"Khong thay {name} trong .env")


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:1500]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            sys.exit(f"HTTP {e.code}: {msg}")


def call_openrouter(msgs, s):
    r = s.get("reasoning", "off")
    body = {"model": s.get("model") or MODEL, "messages": msgs, "provider": PROVIDER,
            "temperature": s.get("temperature", 1.0), "top_p": 0.95,
            "max_tokens": s.get("max_tokens", 700) + (0 if r == "off" else 4000),
            "reasoning": {"enabled": False} if r == "off" else {"enabled": True, **({} if r == "on" else {"effort": r})},
            "usage": {"include": True}}
    d = post("https://openrouter.ai/api/v1/chat/completions", body,
             {"Authorization": f"Bearer {env_key('OPENROUTER_API_KEY')}", "X-Title": "rp-test"})
    if "choices" not in d:
        sys.exit(json.dumps(d, ensure_ascii=False)[:1500])
    m = d["choices"][0]["message"]
    u = d.get("usage", {})
    return {"reply": (m.get("content") or "").strip(), "reasoning": m.get("reasoning") or "",
            "provider": d.get("provider"), "prompt_tokens": u.get("prompt_tokens"),
            "completion_tokens": u.get("completion_tokens"),
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"), "cost": u.get("cost")}


def call_gemini(msgs, s):
    """Gemini khong co role system giua hoi thoai: system dau -> systemInstruction; depth_prompt va
    post_history (system o giua/cuoi) gop vao tin user cuoi cung, giong cach SillyTavern xu ly Gemini."""
    sys_inst = msgs[0]["content"]
    extra = [m["content"] for m in msgs[1:] if m["role"] == "system"]
    convo = [m for m in msgs[1:] if m["role"] != "system"]
    contents = [{"role": "user", "parts": [{"text": "(Bắt đầu cảnh.)"}]}]
    for m in convo:
        contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]})
    if extra:
        contents[-1]["parts"].append({"text": "\n\n[Hướng dẫn cho người viết, không phải lời của Nem: " + " ".join(extra) + "]"})
    r = s.get("reasoning", "off")
    gen = {"temperature": s.get("temperature", 1.0), "topP": 0.95, "maxOutputTokens": s.get("max_tokens", 700) + 6000,
           "thinkingConfig": {"thinkingLevel": "minimal" if r == "off" else ("medium" if r == "on" else r),
                              "includeThoughts": r != "off"}}
    model = s.get("model") or "gemini-3.8-flash"
    d = post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
             {"systemInstruction": {"parts": [{"text": sys_inst}]}, "contents": contents, "generationConfig": gen,
              "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in
                                 ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                                  "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]},
             {"x-goog-api-key": env_key("GEMINI_API_KEY")})
    if not d.get("candidates"):
        sys.exit(json.dumps(d, ensure_ascii=False)[:1500])
    parts = d["candidates"][0].get("content", {}).get("parts", [])
    u = d.get("usageMetadata", {})
    return {"reply": "".join(p.get("text", "") for p in parts if not p.get("thought")).strip(),
            "reasoning": "".join(p.get("text", "") for p in parts if p.get("thought")).strip(),
            "provider": "google/" + d.get("modelVersion", model), "prompt_tokens": u.get("promptTokenCount"),
            "completion_tokens": u.get("candidatesTokenCount"), "reasoning_tokens": u.get("thoughtsTokenCount"),
            "cost": None}


def turn(s, text):
    s["history"].append({"role": "user", "content": text})
    t = time.time()
    res = (call_gemini if s.get("backend") == "gemini" else call_openrouter)(build_messages(s), s)
    res["secs"] = round(time.time() - t, 1)
    s["history"].append({"role": "assistant", "content": res.pop("reply")})
    s["turns"].append(res)
    return s["history"][-1]["content"], res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["new", "say", "show", "dump", "replay"])
    ap.add_argument("text", nargs="?", help="say: loi cua Nem; replay: file json danh sach loi")
    ap.add_argument("--card")
    ap.add_argument("--session", required=True)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--backend", choices=["openrouter", "gemini"], default="openrouter")
    ap.add_argument("--model", help="mac dinh: deepseek v4.1 flash 1017 / gemini-3.8-flash")
    ap.add_argument("--reasoning", default="off", help="off | on | low | medium | high")
    a = ap.parse_intermixed_args()
    sp = a.session if os.path.isabs(a.session) else os.path.join(HERE, a.session)
    if a.cmd == "new":
        card = json.load(open(a.card if os.path.isabs(a.card) else os.path.join(HERE, a.card), encoding="utf-8"))
        s = {"card": card, "history": [], "turns": [], "temperature": a.temperature,
             "backend": a.backend, "model": a.model, "reasoning": a.reasoning}
        json.dump(s, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(fill(card["data"]["first_mes"], card["data"]["name"]))
        return
    if a.cmd == "replay":
        s = json.load(open(sp, encoding="utf-8"))
        lines = json.load(open(a.text if os.path.isabs(a.text) else os.path.join(HERE, a.text), encoding="utf-8"))
        for i, line in enumerate(lines[len(s["history"]) // 2:], start=len(s["history"]) // 2 + 1):
            _, res = turn(s, line)
            json.dump(s, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"[{i}/{len(lines)}] {res['provider']} in {res['prompt_tokens']} out {res['completion_tokens']} "
                  f"think {res['reasoning_tokens']} {res['secs']}s", flush=True)
        return
    s = json.load(open(sp, encoding="utf-8"))
    if a.cmd == "show":
        print(fill(s["card"]["data"]["first_mes"], s["card"]["data"]["name"]))
        for m in s["history"]:
            print(f"\n--- {USER if m['role'] == 'user' else s['card']['data']['name']} ---\n{m['content']}")
        return
    if a.cmd == "dump":
        print(json.dumps(build_messages(s), ensure_ascii=False, indent=1))
        return
    reply, res = turn(s, a.text)
    json.dump(s, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(reply)
    print(f"\n[{res['provider']} | in {res['prompt_tokens']} out {res['completion_tokens']} "
          f"think {res['reasoning_tokens']} | ${res['cost']} | {res['secs']}s]")


if __name__ == "__main__":
    main()
