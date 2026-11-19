"""RP Lab — web UI nho de test raw API (OpenRouter / OpenAI-compatible / Google AI Studio / Vertex AI express).

  python rp/webui/server.py            -> http://127.0.0.1:7861
  python rp/webui/server.py --port 8000

Chi chay o 127.0.0.1. Key doc tu .env cua project (OPENROUTER_API_KEY, GEMINI_API_KEY, VERTEX_API_KEY);
key khong bao gio gui ve trinh duyet. Rieng "OpenAI-compatible tuy chinh" nhan base URL + key tu giao dien.
Ghep prompt tu character card giong SillyTavern (giong rp_chat.py): system_prompt + mo ta + tinh cach + boi canh
+ lorebook (constant + theo tu khoa) + vi du hoi thoai; first_mes + lich su; depth_prompt; post_history.
"""
import argparse, base64, difflib, json, os, struct, sys, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lore  # noqa: E402
import judge  # noqa: E402
import group  # noqa: E402
import rpformat  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATHS = [os.path.join(HERE, "..", "..", ".env"), os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")]
SCAN_DEPTH = 4


def env(name):
    if os.environ.get(name):
        return os.environ[name]
    for p in ENV_PATHS:
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.strip().startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    return ""


# ----------------------------------------------------------------------------- character card
def parse_card_bytes(data, filename=""):
    """JSON (CCv2/v3) hoac PNG co chunk tEXt 'ccv3'/'chara' (base64 JSON)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        pos, found = 8, {}
        while pos < len(data):
            ln = struct.unpack(">I", data[pos:pos + 4])[0]
            typ = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + ln]
            if typ == b"tEXt" and b"\x00" in body:
                k, v = body.split(b"\x00", 1)
                found[k.decode("latin-1").lower()] = v
            pos += 12 + ln
        raw = found.get("ccv3") or found.get("chara")
        if not raw:
            raise ValueError("PNG khong co du lieu character card (tEXt chara/ccv3)")
        card = json.loads(base64.b64decode(raw).decode("utf-8"))
    else:
        card = json.loads(data.decode("utf-8-sig"))
    d = card.get("data") or card  # CCv1 khong co "data"
    if not d.get("name"):
        raise ValueError("Card khong co ten nhan vat")
    return {"data": d}


def fill(text, char, user):
    return (text or "").replace("{{user}}", user).replace("{{char}}", char).replace("<USER>", user).replace("<BOT>", char)


def collect_books(card, lorebooks, opts):
    books = [b for b in (lorebooks or []) if b.get("enabled", True)]
    if card and opts.get("card_lore", True) and (card["data"].get("character_book") or {}).get("entries"):
        books = [lore.normalize_book(card["data"]["character_book"], name=card["data"]["name"] + " (card)")] + books
    return books


def build_messages(card, history, user, opts, lorebooks=None):
    """history: [{role: user|assistant, content}] — da gom ca first_mes (tin assistant dau tien).
    Tra ve (messages, lore_log)."""
    char = card["data"]["name"] if card else "Assistant"
    picked, log, used = lore.activate(collect_books(card, lorebooks, opts), history,
                                      scan_depth=int(opts.get("lore_scan_depth", 4)),
                                      recursive=bool(opts.get("lore_recursive", True)),
                                      budget=int(opts.get("lore_budget", 6000)),
                                      whole_words=bool(opts.get("lore_whole_words", True)))
    before = "\n\n".join(e["content"] for e in picked if e["position"] == "before")
    after = "\n\n".join(e["content"] for e in picked if e["position"] == "after")
    d = card["data"] if card else {}
    ex = (d.get("mes_example") or "").replace("<START>", "[Ví dụ hội thoại]").strip()
    sys_prompt = opts.get("system_prompt") or d.get("system_prompt") or ""
    parts = [sys_prompt, before,
             f"[Nhân vật: {char}]\n{d.get('description', '')}" if d.get("description") else "",
             f"[Tính cách]\n{d['personality']}" if d.get("personality") else "",
             f"[Bối cảnh]\n{d['scenario']}" if d.get("scenario") else "",
             f"[Người dùng: {user}]\n{opts['persona']}" if opts.get("persona") else "",
             after, ex]
    sys_text = fill("\n\n".join(p for p in parts if p), char, user)
    msgs = [{"role": "system", "content": sys_text}] if sys_text else []
    chat = [{"role": m["role"], "content": m["content"]} for m in history]
    inserts = [(e["depth"], e["role"], fill(e["content"], char, user)) for e in picked if e["position"] == "depth"]
    dp = (d.get("extensions") or {}).get("depth_prompt") or {}
    if dp.get("prompt"):
        inserts.append((int(dp.get("depth", 4)), dp.get("role", "system"), fill(dp["prompt"], char, user)))
    for depth, role, text in sorted(inserts, key=lambda x: -x[0]):  # chen sau nhat truoc de vi tri khong lech
        chat.insert(max(0, len(chat) - depth), {"role": role if role in ("system", "user", "assistant") else "system",
                                                "content": text})
    msgs += chat
    if d.get("post_history_instructions"):
        msgs.append({"role": "system", "content": fill(d["post_history_instructions"], char, user)})
    return msgs, {"entries": log, "tokens": used}


def rewrite_instruction(draft, char, user):
    """Huong dan cho luot 2 cua che do 'nhap -> soat -> viet lai'."""
    return (
        f"[BƯỚC SOÁT VÀ VIẾT LẠI — không phải một phần của câu chuyện]\n"
        f"Dưới đây là BẢN NHÁP lượt tiếp theo của {char}:\n<<<\n{draft.strip()}\n>>>\n\n"
        f"Soát bản nháp theo từng điểm:\n"
        f"1. Có phản hồi đúng và đủ vào điều {user} vừa nói hoặc vừa làm ở tin cuối không?\n"
        f"2. Có tự nhảy thời gian, đổi địa điểm, thêm nhân vật hay sự kiện mới, hoặc bịa chi tiết mà {user} chưa nhắc không?\n"
        f"3. Có mâu thuẫn với tình tiết đã xảy ra trong cuộc trò chuyện hoặc với thông tin nhân vật không?\n"
        f"4. Có lặp lại câu cửa miệng, cử chỉ hay cách mở đầu đã dùng ở 3 lượt gần nhất không?\n"
        f"5. Giọng văn, xưng hô, tính cách có đúng như mô tả nhân vật không? Có câu nào nghe như trợ lý AI không?\n"
        f"6. Có viết hộ lời, suy nghĩ hay hành động của {user} không? Độ dài và định dạng có đúng yêu cầu không?\n\n"
        f"Lưu ý: nếu hướng dẫn nhân vật có yêu cầu kiểu 'chủ động', 'đẩy cảnh', 'không đứng chờ', hãy hiểu là chủ động "
        f"trong CÁCH phản ứng (cà khịa, góp ý, cử chỉ), không phải tự mở diễn biến mới, nhắc giờ hối thúc hay rời khỏi cảnh "
        f"khi {user} chưa chuyển cảnh. Không thêm chi tiết mới nào mà bản nháp và cuộc trò chuyện chưa có.\n\n"
        f"Sau đó viết lại BẢN CUỐI hoàn chỉnh: giữ những gì bản nháp làm tốt, sửa mọi lỗi tìm thấy, "
        f"làm câu văn tự nhiên và sống động hơn nếu được. CHỈ in bản cuối, không in nhận xét, không nhắc tới bản nháp."
    )


# ----------------------------------------------------------------------------- backends (generator of events)
def sse_lines(resp):
    buf = b""
    for chunk in iter(lambda: resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096), b""):
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                yield line[5:].strip().decode("utf-8", errors="replace")


def open_stream(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        return urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:2000]}")


def stream_openai(msgs, o):
    prov = o["provider"]
    if prov == "openrouter":
        url, key = "https://openrouter.ai/api/v1/chat/completions", env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("Thiếu OPENROUTER_API_KEY trong .env")
    else:
        base = (o.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("Chưa nhập Base URL")
        url, key = base + "/chat/completions", o.get("api_key") or ""
    body = {"model": o["model"], "messages": msgs, "stream": True,
            "temperature": float(o.get("temperature", 1.0)), "top_p": float(o.get("top_p", 0.95)),
            "max_tokens": int(o.get("max_tokens", 1500))}
    r = o.get("reasoning", "off")
    if prov == "openrouter":
        body["usage"] = {"include": True}
        body["reasoning"] = {"enabled": False} if r == "off" else ({"enabled": True} if r == "auto" else {"enabled": True, "effort": r})
        order = [x.strip() for x in (o.get("or_providers") or "").split(",") if x.strip()]
        quants = [x.strip() for x in (o.get("or_quant") or "").split(",") if x.strip()]
        if order or quants or not o.get("or_fallbacks", True):
            body["provider"] = {**({"order": order} if order else {}), **({"quantizations": quants} if quants else {}),
                                "allow_fallbacks": bool(o.get("or_fallbacks", True))}
    elif r != "off":
        body["reasoning_effort"] = "medium" if r == "auto" else r
    yield {"type": "request", "url": url, "body": body}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if prov == "openrouter":
        headers["X-Title"] = "RP Lab"
    resp = open_stream(url, body, headers)
    usage, provider_name = {}, None
    for data in sse_lines(resp):
        if data == "[DONE]":
            break
        try:
            j = json.loads(data)
        except json.JSONDecodeError:
            continue
        if j.get("error"):
            raise RuntimeError(json.dumps(j["error"], ensure_ascii=False)[:1500])
        provider_name = j.get("provider") or provider_name
        for ch in j.get("choices") or []:
            dl = ch.get("delta") or {}
            rs = dl.get("reasoning") or dl.get("reasoning_content")
            if rs:
                yield {"type": "reasoning", "text": rs}
            if dl.get("content"):
                yield {"type": "content", "text": dl["content"]}
            if ch.get("finish_reason") == "length":
                yield {"type": "notice", "text": "Bị cắt vì chạm Max tokens (token suy nghĩ cũng tính vào đây) — tăng Max tokens rồi tạo lại."}
        if j.get("usage"):
            usage = j["usage"]
    det = usage.get("completion_tokens_details") or {}
    yield {"type": "usage", "provider": provider_name, "prompt_tokens": usage.get("prompt_tokens"),
           "completion_tokens": usage.get("completion_tokens"), "reasoning_tokens": det.get("reasoning_tokens"),
           "cost": usage.get("cost")}


def to_gemini(msgs):
    """system dau -> systemInstruction; system giua/cuoi (depth, post-history) -> gop vao tin user cuoi."""
    sys_inst = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
    rest = msgs[1:] if sys_inst else msgs
    extra = [m["content"] for m in rest if m["role"] == "system"]
    convo = [m for m in rest if m["role"] != "system"]
    contents = []
    if convo and convo[0]["role"] == "assistant":
        contents.append({"role": "user", "parts": [{"text": "(Bắt đầu.)"}]})
    for m in convo:
        role = "model" if m["role"] == "assistant" else "user"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": m["content"]})
        else:
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
    if extra and contents:
        contents[-1]["parts"].append({"text": "\n\n[Hướng dẫn cho người viết: " + " ".join(extra) + "]"})
    return sys_inst, contents


def stream_gemini(msgs, o):
    sys_inst, contents = to_gemini(msgs)
    r = o.get("reasoning", "off")
    gen = {"temperature": float(o.get("temperature", 1.0)), "topP": float(o.get("top_p", 0.95)),
           "maxOutputTokens": int(o.get("max_tokens", 1500)) + (0 if r == "off" else 8000)}
    if r == "off":
        gen["thinkingConfig"] = {"thinkingLevel": "minimal"}
    elif r != "auto":
        gen["thinkingConfig"] = {"thinkingLevel": r, "includeThoughts": True}
    else:
        gen["thinkingConfig"] = {"includeThoughts": True}
    body = {"contents": contents, "generationConfig": gen,
            "safetySettings": [{"category": c, "threshold": o.get("safety", "BLOCK_ONLY_HIGH")} for c in
                               ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]}
    if sys_inst:
        body["systemInstruction"] = {"parts": [{"text": sys_inst}]}
    model = o["model"]
    if o["provider"] == "vertex":
        key = env("VERTEX_API_KEY") or env("GEMINI_API_KEY")
        url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:streamGenerateContent?alt=sse"
    else:
        key = env("GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
    if not key:
        raise RuntimeError("Thiếu GEMINI_API_KEY / VERTEX_API_KEY trong .env")
    yield {"type": "request", "url": url, "body": body}
    resp = open_stream(url, body, {"x-goog-api-key": key})
    usage, version = {}, model
    for data in sse_lines(resp):
        try:
            j = json.loads(data)
        except json.JSONDecodeError:
            continue
        if j.get("error"):
            raise RuntimeError(json.dumps(j["error"], ensure_ascii=False)[:1500])
        version = j.get("modelVersion", version)
        for c in j.get("candidates") or []:
            for p in (c.get("content") or {}).get("parts", []):
                if p.get("text"):
                    yield {"type": "reasoning" if p.get("thought") else "content", "text": p["text"]}
            if c.get("finishReason") == "MAX_TOKENS":
                yield {"type": "notice", "text": "Bị cắt vì chạm Max tokens — tăng Max tokens rồi tạo lại."}
            elif c.get("finishReason") and c["finishReason"] != "STOP":
                yield {"type": "notice", "text": f"finishReason: {c['finishReason']}"}
        if j.get("promptFeedback", {}).get("blockReason"):
            yield {"type": "notice", "text": "Bị chặn: " + j["promptFeedback"]["blockReason"]}
        if j.get("usageMetadata"):
            usage = j["usageMetadata"]
    yield {"type": "usage", "provider": f"{o['provider']}/{version}", "prompt_tokens": usage.get("promptTokenCount"),
           "completion_tokens": usage.get("candidatesTokenCount"), "reasoning_tokens": usage.get("thoughtsTokenCount"),
           "cost": None}


# ----------------------------------------------------------------------------- models list
def list_models(provider):
    if provider == "openrouter":
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as r:
            return sorted(m["id"] for m in json.loads(r.read())["data"])
    if provider in ("google", "vertex"):
        key = env("GEMINI_API_KEY")
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=500",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return sorted(m["name"].split("/", 1)[1] for m in json.loads(r.read()).get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", []))
    return []


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[rp-lab] " + fmt % args + "\n")

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _group_turn(self, req):
        """1 luot group chat: dao dien -> nguoi viet (-> cong cham) -> cap nhat trang thai. Stream SSE."""
        pack, state, o = req["pack"], dict(req["state"]), req.get("options", {})
        lorebooks = [b for b in req.get("lorebooks", []) if b.get("enabled", True)]
        history = list(req.get("history", []))
        if (req.get("user_msg") or "").strip():
            history.append({"speaker": "user", "content": req["user_msg"].strip()})
        key = env("OPENROUTER_API_KEY")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        t0, extra_cost = time.time(), 0.0
        try:
            dmodel = o.get("director_model") or "microsoft/microsoft-decision-1"
            try:
                dec = group.direct(key, dmodel, pack, state, history, force_speaker=req.get("force_speaker"),
                                   force_reason=req.get("force_reason"))
            except Exception as e:
                dec = {"speaker": group.NARRATOR, "speaker_name": "Người dẫn truyện", "speaker_why": f"đạo diễn lỗi ({e})",
                       "speaker_conf": 0, "speaker_probs": {}, "turn_type": "mo_ta", "turn_type_label": group.TURN_TYPES["mo_ta"],
                       "lore": [], "lore_used": [], "fields": [], "field_probs": {}, "cost": 0, "secs": 0}
            extra_cost += dec.get("cost") or 0
            emit({"type": "director", **dec})
            active_lore, lore_log, lore_tokens = [], [], 0
            if lorebooks:
                active_lore, lore_log, lore_tokens = lore.activate(
                    lorebooks, history,
                    scan_depth=int(o.get("lore_scan_depth", 4)),
                    recursive=bool(o.get("lore_recursive", True)),
                    budget=int(o.get("lore_budget", 6000)),
                    whole_words=bool(o.get("lore_whole_words", True))
                )
                emit({"type": "lore", "entries": lore_log, "tokens": lore_tokens})
            msgs = group.writer_messages(pack, state, history, dec, active_lore=active_lore)
            gen = stream_gemini if o.get("provider") in ("google", "vertex") else stream_openai
            usage, text = {}, ""
            # token suy nghi cung tinh vao max_tokens (DeepSeek nghi toi ~1000 token o luot kho) -> san toi thieu 2000
            o = {**o, "max_tokens": max(int(o.get("max_tokens", 1500)), 2000)}
            if o.get("gate"):
                draft, cut = "", False
                for ev in gen(msgs, o):
                    if ev["type"] == "content":
                        draft += ev["text"]
                        emit({"type": "draft", "text": ev["text"]})
                    elif ev["type"] == "usage":
                        usage = ev
                    elif ev["type"] == "request":
                        emit(ev)
                    elif ev["type"] == "notice" and "Bị cắt" in ev.get("text", ""):
                        cut = True  # ban nhap cut -> bat buoc viet lai, khong bao gio xuat ban cut
                is_continue = not history or history[-1]["speaker"] != "user"
                user_msg = history[-1]["content"] if not is_continue else "(người chơi không nói gì, muốn câu chuyện tự tiếp diễn)"
                prev = next((m["content"] for m in reversed(history) if m["speaker"] != "user"), pack.get("opening", ""))
                # luot tiep dien: khong doi "tra loi dung tin cuoi", va dien bien moi la dieu duoc mong muon;
                # luot chuyen canh do nguoi choi de nghi: duoc doi dia diem
                skip = (["tra_loi_dung", "mo_dien_bien_moi"] if is_continue else
                        ["mo_dien_bien_moi"] if dec.get("turn_type") == "chuyen_canh" else [])
                jr = judge.judge(key, o.get("judge_model") or judge.JUDGE_MODELS[0], prev, user_msg, draft,
                                 dec["speaker_name"], pack.get("user", "User"), float(o.get("judge_threshold", 0.8)), skip=skip)
                jr["labels"] = {k: v["label"] for k, v in judge.CRITERIA.items()} | {"bi_cat": "bị cắt cụt"}
                if cut:
                    jr["passed"] = False
                    jr["failed"] = list(jr.get("failed", [])) + ["bi_cat"]
                    jr["score"] = 0  # de ban viet lai luon thang khi cham lai
                emit({"type": "judge", **jr})
                extra_cost += jr.get("cost") or 0
                if (jr.get("passed") and not cut) or not draft.strip():
                    text = draft
                    emit({"type": "content", "text": draft})
                else:
                    failed = [k for k in jr.get("failed", []) if k in judge.CRITERIA]
                    fix = judge.fix_instruction(draft, failed, jr.get("probs", {}), dec["speaker_name"], pack.get("user", "User"))
                    u1 = usage
                    for ev in gen(msgs + [{"role": "system", "content": fix}], {**o, "max_tokens": int(o.get("max_tokens", 1500)) * 2}):
                        if ev["type"] == "content":
                            text += ev["text"]
                            emit(ev)
                        elif ev["type"] == "usage":
                            usage = {**ev, **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in
                                              ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")}}
                        elif ev["type"] in ("reasoning", "notice"):
                            emit(ev)
                    # cham lai ban sua; neu khong hon ban nhap thi giu ban nhap (ban sua co the cat mat thoai hay)
                    if text.strip():
                        jr2 = judge.judge(key, o.get("judge_model") or judge.JUDGE_MODELS[0], prev, user_msg, text,
                                          dec["speaker_name"], pack.get("user", "User"), float(o.get("judge_threshold", 0.8)), skip=skip)
                        extra_cost += jr2.get("cost") or 0
                        keep_draft = not jr2.get("error") and jr2.get("score", 0) < jr.get("score", 0)
                        emit({"type": "rejudge", "score_draft": jr.get("score"), "score_final": jr2.get("score"),
                              "passed": jr2.get("passed"), "failed": jr2.get("failed"), "kept": "draft" if keep_draft else "final"})
                        if keep_draft:
                            text = draft
                            emit({"type": "reset_content"})
                            emit({"type": "content", "text": draft})
            else:
                for ev in gen(msgs, o):
                    if ev["type"] == "content":
                        text += ev["text"]
                    if ev["type"] == "usage":
                        usage = ev
                    else:
                        emit(ev)
            # chong lap: ban cuoi gan nhu chep lai tin truoc -> viet lai 1 lan
            prev_ai = next((m["content"] for m in reversed(history) if m["speaker"] != "user"), "")
            if prev_ai and difflib.SequenceMatcher(None, prev_ai[:1500], text.strip()[:1500]).ratio() > 0.6:
                emit({"type": "notice", "text": "Bản vừa viết lặp lại gần nguyên văn tin trước → viết lại."})
                emit({"type": "reset_content"})
                text, u1 = "", usage
                anti = {"role": "system", "content": "Lần viết trước đã chép lại gần nguyên văn tin trước đó. Viết diễn biến MỚI, "
                                                     "tiếp nối sau tin cuối, không lặp lại câu chữ đã có."}
                for ev in gen(msgs + [anti], o):
                    if ev["type"] == "content":
                        text += ev["text"]
                        emit(ev)
                    elif ev["type"] == "usage":
                        usage = {**ev, **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in
                                          ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")}}
            history.append({"speaker": dec["speaker"], "content": text.strip()})
            emit({"type": "turn", "speaker": dec["speaker"], "speaker_name": dec["speaker_name"]})
            try:
                new_state, sinfo = group.update_state(key, dmodel, o.get("extract_model") or "cloudflare/clairic", pack, state, history,
                                                       owed=dec.get("owed", []), arriving=dec.get("arriving", []))
                extra_cost += sinfo.get("cost") or 0
                emit({"type": "state", "state": new_state, "info": sinfo})
                # tom tat dai han: du SUMMARY_EVERY luot cu chua tom tat -> goi model viet (tat reasoning) gop vao tom tat
                if group.summary_due(history, new_state):
                    end, smsgs = group.summary_messages(pack, new_state, history)
                    summ, su = "", {}
                    for ev in gen(smsgs, {**o, "reasoning": "off", "max_tokens": 900, "temperature": 0.3}):
                        if ev["type"] == "content":
                            summ += ev["text"]
                        elif ev["type"] == "usage":
                            su = ev
                    if summ.strip():
                        new_state = {**new_state, "tom_tat": summ.strip(), "tom_tat_upto": end}
                        extra_cost += su.get("cost") or 0
                        emit({"type": "state", "state": new_state, "info": {**sinfo, "summarized_upto": end}})
            except Exception as e:
                emit({"type": "notice", "text": f"Không cập nhật được trạng thái: {e}"})
            emit({**usage, "type": "usage", "secs": round(time.time() - t0, 1), "extra_cost": round(extra_cost, 7),
                  "cost": (usage.get("cost") or 0) + extra_cost if usage.get("cost") is not None else None})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                emit({"type": "error", "text": str(e)})
            except Exception:
                return
        try:
            emit({"type": "done"})
        except Exception:
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html", "/group", "/group.html", "/editor", "/editor.html"):
            page = "group.html" if self.path.startswith("/group") else "editor.html" if self.path.startswith("/editor") else "index.html"
            b = open(os.path.join(HERE, page), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/status":
            self._json({"keys": {k: bool(env(k)) for k in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "VERTEX_API_KEY")}})
        elif self.path in ("/api/group/sample", "/api/group/sample_world"):
            world = json.load(open(os.path.join(HERE, "..", "samples", "hoa_phuong_world.json"), encoding="utf-8"))
            self._json(world if self.path.endswith("_world") else rpformat.load_world(world)[0])
        elif self.path.startswith("/api/models?provider="):
            try:
                self._json({"models": list_models(self.path.split("=", 1)[1])})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/card":
            try:
                self._json({"card": parse_card_bytes(self._body())})
            except Exception as e:
                self._json({"error": f"Không đọc được card: {e}"}, 400)
            return
        if self.path == "/api/lorebook":
            try:
                raw = self._body()
                name = self.headers.get("X-Filename", "Lorebook")
                name = urllib.parse.unquote(name).rsplit(".", 1)[0]
                try:
                    obj = json.loads(raw.decode("utf-8-sig"))
                except Exception:
                    obj = parse_card_bytes(raw)
                self._json({"book": lore.normalize_book(obj, name=name)})
            except Exception as e:
                self._json({"error": f"Không đọc được lorebook: {e}"}, 400)
            return
        if self.path == "/api/preview":
            req = json.loads(self._body())
            msgs, lore_log = build_messages(req.get("card"), req.get("history", []), req.get("user") or "User",
                                            req.get("options", {}), req.get("lorebooks"))
            self._json({"messages": msgs, "lore": lore_log})
            return
        if self.path == "/api/group/load":
            try:
                pack, warns = rpformat.load_world(json.loads(self._body().decode("utf-8-sig")))
                self._json({"pack": pack, "warnings": warns})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if self.path == "/api/group/card":
            # file nhan vat rplab-char-1, hoac character card SillyTavern (JSON/PNG) -> tu chuyen sang rplab-char-1
            try:
                raw = self._body()
                ids = json.loads(urllib.parse.unquote(self.headers.get("X-Existing-Ids", "[]")))
                obj = None
                if raw[:1] in (b"{", b"\xef"):
                    obj = json.loads(raw.decode("utf-8-sig"))
                if obj and obj.get("format") == "rplab-char-1":
                    char, mo_dau, converted = obj, None, False
                    if char["id"] in ids:
                        raise rpformat.FormatError(f"id '{char['id']}' đã có trong dàn nhân vật")
                else:
                    char, mo_dau = rpformat.ccv3_to_char(parse_card_bytes(raw), ids)
                    converted = True
                self._json({"member": rpformat.char_to_internal(char), "char": char, "mo_dau": mo_dau, "converted": converted})
            except Exception as e:
                self._json({"error": f"Không đọc được nhân vật: {e}"}, 400)
            return
        if self.path == "/api/group/turn":
            self._group_turn(json.loads(self._body()))
            return
        if self.path == "/api/chat":
            req = json.loads(self._body())
            o = req.get("options", {})
            msgs, lore_log = build_messages(req.get("card"), req.get("history", []), req.get("user") or "User", o,
                                            req.get("lorebooks"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(ev):
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()

            t0 = time.time()
            try:
                emit({"type": "lore", **lore_log})
                gen = stream_gemini if o.get("provider") in ("google", "vertex") else stream_openai
                first = None
                if o.get("mode") in ("rewrite", "gate"):
                    # Luot 1: viet nhap (stream ve giao dien duoi dang "draft")
                    draft, u1, cut = "", {}, False
                    rename = {"content": "draft", "reasoning": "draft_reasoning", "request": "request1"}
                    for ev in gen(msgs, o):
                        if ev["type"] == "content":
                            draft += ev["text"]
                        if ev["type"] == "notice" and "Bị cắt" in ev.get("text", ""):
                            cut = True
                            continue  # luot 2 se viet lai, khong bao loi cho ban nhap
                        if ev["type"] == "usage":
                            u1 = ev
                            ev = {**ev, "type": "usage1", "secs": round(time.time() - t0, 1)}
                        else:
                            ev = {**ev, "type": rename.get(ev["type"], ev["type"])}
                        emit(ev)
                    if not draft.strip():
                        raise RuntimeError("Lượt nháp trả về rỗng, không có gì để soát.")
                    char = ((req.get("card") or {}).get("data") or {}).get("name") or "Assistant"
                    uname = req.get("user") or "User"
                    instruction = rewrite_instruction(draft, char, uname)
                    if o.get("mode") == "gate":
                        # Cham ban nhap bang model Decisions; dat thi xuat luon, truot thi chi sua dung loi bi bat
                        hist = req.get("history", [])
                        user_msg = next((m["content"] for m in reversed(hist) if m["role"] == "user"), "")
                        prev = next((m["content"] for m in reversed(hist[:-1]) if m["role"] == "assistant"), "")
                        jr = judge.judge(env("OPENROUTER_API_KEY"), o.get("judge_model") or judge.JUDGE_MODELS[0],
                                         prev, user_msg, draft, char, uname, float(o.get("judge_threshold", 0.8)))
                        if cut:
                            jr["passed"] = False
                            jr["failed"] = jr.get("failed", []) + ["bi_cat"]
                        jr["labels"] = {k: v["label"] for k, v in judge.CRITERIA.items()} | {"bi_cat": "bị cắt cụt"}
                        emit({"type": "judge", **jr})
                        jcost = jr.get("cost") or 0
                        if jr.get("passed"):
                            emit({"type": "content", "text": draft})
                            emit({**u1, "type": "usage", "secs": round(time.time() - t0, 1), "passes": 1,
                                  "cost": (u1.get("cost") or 0) + jcost if u1.get("cost") is not None else None,
                                  "judge_cost": jcost, "gate": "pass"})
                            emit({"type": "done"})
                            return
                        if not jr.get("error"):
                            instruction = judge.fix_instruction(draft, [k for k in jr["failed"] if k in judge.CRITERIA],
                                                                jr["probs"], char, uname)
                        u1 = {**u1, "cost": (u1.get("cost") or 0) + jcost if u1.get("cost") is not None else None}
                    # Luot 2: viet lai (che do rewrite: checklist chung; che do gate: chi sua loi bi bat)
                    msgs2 = msgs + [{"role": "system", "content": instruction}]
                    # luot viet lai suy nghi nhieu hon (soat + viet) -> nhan doi han muc de khong bi cat cut
                    o2 = {**o, "model": o.get("rewrite_model") or o["model"],
                          "max_tokens": int(o.get("max_tokens", 1500)) * 2}
                    t1 = time.time()
                    for ev in gen(msgs2, o2):
                        if ev["type"] in ("content", "reasoning") and first is None:
                            first = time.time() - t1
                        if ev["type"] == "usage":
                            ev = {**ev, "secs": round(time.time() - t0, 1), "ttft": round(first, 1) if first else None,
                                  "pass1": {k: u1.get(k) for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")}}
                            for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost"):
                                if isinstance(u1.get(k), (int, float)) and isinstance(ev.get(k), (int, float)):
                                    ev[k] = ev[k] + u1[k]
                            ev["passes"] = 2
                            if o.get("mode") == "gate":
                                ev["gate"] = "rewrite"
                        emit(ev)
                else:
                    for ev in gen(msgs, o):
                        if ev["type"] in ("content", "reasoning") and first is None:
                            first = time.time() - t0
                        if ev["type"] == "usage":
                            ev["secs"] = round(time.time() - t0, 1)
                            ev["ttft"] = round(first, 1) if first else None
                        emit(ev)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                try:
                    emit({"type": "error", "text": str(e)})
                except Exception:
                    return
            try:
                emit({"type": "done"})
            except Exception:
                pass
            return
        self.send_error(404)


def main():
    for s in (sys.stdout, sys.stderr):  # console Windows (cp1252) khong in duoc tieng Viet
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7861)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"RP Lab: http://127.0.0.1:{a.port}  (Ctrl+C để dừng)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
