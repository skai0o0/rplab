"""RP Lab — web UI nho de test raw API (OpenRouter / OpenAI-compatible / Google AI Studio / Vertex AI express).

  python rp/webui/server.py            -> http://127.0.0.1:7861
  python rp/webui/server.py --port 8000

Chi chay o 127.0.0.1. Key doc tu .env cua project (OPENROUTER_API_KEY, GEMINI_API_KEY, VERTEX_API_KEY);
key khong bao gio gui ve trinh duyet. Rieng "OpenAI-compatible tuy chinh" nhan base URL + key tu giao dien.
Ghep prompt tu character card giong SillyTavern (giong rp_chat.py): system_prompt + mo ta + tinh cach + boi canh
+ lorebook (constant + theo tu khoa) + vi du hoi thoai; first_mes + lich su; depth_prompt; post_history.
"""
import argparse, base64, copy, difflib, json, os, struct, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lore  # noqa: E402
import judge  # noqa: E402
import group  # noqa: E402
import commands  # noqa: E402
import prompts  # noqa: E402
import rpformat  # noqa: E402
import translator  # noqa: E402
import db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATHS = [os.path.join(HERE, "..", "..", ".env"), os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")]
SCAN_DEPTH = 4
SUMMARY_LOCK = threading.Lock()
SUMMARY_JOBS = {}  # session_key -> {running, done, end, header, body_text, arc_text, cost, error}


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
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:2000]}  [URL gọi: {url}]")


def effective_writer_options(o):
    """Dam bao tran max_tokens du rong cho thinking_content + content khi bat suy luan."""
    min_tok = 2000 if o.get("reasoning", "off") == "off" else 4096
    return {**o, "max_tokens": max(int(o.get("max_tokens", min_tok)), min_tok)}


def build_openai_body(msgs, o):
    """Dung JSON request body chuan cho OpenRouter va OpenAI-compatible (ModelArk / DeepSeek API)."""
    r = o.get("reasoning", "off")
    body = {
        "model": o.get("model"),
        "messages": msgs,
        "stream": True,
        "temperature": float(o.get("temperature", 1.0)),
        "top_p": float(o.get("top_p", 0.95)),
        "max_tokens": int(o.get("max_tokens", 1500)),
    }
    if o.get("provider") == "openrouter":
        body["usage"] = {"include": True}
        if r == "off":
            body["reasoning"] = {"enabled": False}
        elif r == "auto":
            body["reasoning"] = {"enabled": True}
        else:
            or_effort = "xhigh" if r == "max" else r
            body["reasoning"] = {"enabled": True, "effort": or_effort}
        order = [x.strip() for x in (o.get("or_providers") or "").split(",") if x.strip()]
        quants = [x.strip() for x in (o.get("or_quant") or "").split(",") if x.strip()]
        if order or quants or not o.get("or_fallbacks", True):
            body["provider"] = {
                **({"order": order} if order else {}),
                **({"quantizations": quants} if quants else {}),
                "allow_fallbacks": bool(o.get("or_fallbacks", True)),
            }
    else:
        if r == "off":
            body["thinking"] = {"type": "disabled"}
        elif r == "auto":
            body["thinking"] = {"type": "enabled"}
        else:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = r
    return body


def stream_openai(msgs, o):
    prov = o["provider"]
    if prov == "openrouter":
        url, key = "https://openrouter.ai/api/v1/chat/completions", env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("Thiếu OPENROUTER_API_KEY trong .env")
    else:
        base = (o.get("base_url") or "").strip().rstrip("/")
        if base.lower().endswith("/chat/completions"):  # nguoi dung dan nham ca endpoint -> tranh path bi lap
            base = base[: -len("/chat/completions")].rstrip("/")
        if not base:
            raise RuntimeError("Chưa nhập Base URL")
        url, key = base + "/chat/completions", o.get("api_key") or ""
    body = build_openai_body(msgs, o)
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
            elif ch.get("finish_reason") == "content_filter":
                yield {"type": "notice", "text": "Bị chặn: content_filter"}
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
    model = o["model"]
    is_25 = "gemini-2.5" in (model or "").lower()
    r = o.get("reasoning", "off")
    gen = {"temperature": float(o.get("temperature", 1.0)), "topP": float(o.get("top_p", 0.95)),
           "maxOutputTokens": int(o.get("max_tokens", 1500)) + (0 if r == "off" else 8000)}
    if r == "off":
        gen["thinkingConfig"] = {"thinkingBudget": 0} if is_25 else {"thinkingLevel": "minimal"}
    elif r != "auto":
        if is_25:
            budgets = {"minimal": 512, "low": 1024, "medium": 4096, "high": 8192, "max": 16384}
            gen["thinkingConfig"] = {"thinkingBudget": budgets.get(r, 1024), "includeThoughts": True}
        else:
            gen["thinkingConfig"] = {"thinkingLevel": ("high" if r == "max" else r), "includeThoughts": True}
    else:
        gen["thinkingConfig"] = {"includeThoughts": True}
    body = {"contents": contents, "generationConfig": gen,
            "safetySettings": [{"category": c, "threshold": o.get("safety", "BLOCK_ONLY_HIGH")} for c in
                               ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]}
    if sys_inst:
        body["systemInstruction"] = {"parts": [{"text": sys_inst}]}
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


def _resolve_turn_pronouns(pack, state, history, speaker_id, ch_parts):
    if speaker_id == group.NARRATOR:
        return {}, None
    if speaker_id == group.SCENE_SPEAKER:
        chars = pack.get("characters") or {}
        scene_cids = [c for c in (ch_parts or []) if c in chars]
        if not scene_cids:
            scene_cids = [c for c in rpformat.co_located_npcs(pack, state) if c in chars]
        lines = []
        for cid in scene_cids[:6]:
            pr_c = rpformat.resolve_pronouns(pack, state, cid, ch_parts, focus_id=group.focus_target(history, cid))
            c_lines = pr_c.get("lines") or []
            if c_lines:
                lines.append(f"* {group.name_of(pack, cid)}:")
                lines.extend(f"  {l}" for l in c_lines)
        return {"lines": lines, "focus_rule_text": "", "focus_pair": None}, None
    focus_id = group.focus_target(history, speaker_id)
    return rpformat.resolve_pronouns(pack, state, speaker_id, ch_parts, focus_id=focus_id), focus_id


def _gate_inputs(pack, state, history, dec, turn_source):
    """Chuan bi du lieu chung cho Pha 4 Gate (gom skip Case #28 va blind_turns Case #24)."""
    is_narr = dec["speaker"] == group.NARRATOR
    is_scene_all = dec["speaker"] == group.SCENE_SPEAKER
    ch_parts = (state.get("active_channel") or {}).get("participants") or []
    is_chain = (turn_source == "npc_chain")
    is_continue = (not history or history[-1]["speaker"] != "user") and not is_chain
    last_user_msg = next((m["content"] for m in reversed(history) if m["speaker"] == "user"), "")
    if is_chain and history and history[-1]["speaker"] != "user":
        prev_npc_name = group.name_of(pack, history[-1]["speaker"])
        user_msg = (last_user_msg or "(chưa có)") + f"\n(Sau đó {prev_npc_name} vừa nói/làm: {history[-1]['content']})"
    elif is_continue:
        user_msg = "(người chơi không nói gì, muốn câu chuyện tự tiếp diễn)"
    else:
        user_msg = history[-1]["content"]
    op_nar = (pack.get("opening") or {}).get("narration", "") if isinstance(pack.get("opening"), dict) else (pack.get("opening") or "")
    if is_scene_all:
        prev = next((m["content"] for m in reversed(history) if m["speaker"] != "user"), op_nar)
    else:
        prev = next((m["content"] for m in reversed(history) if m["speaker"] == dec["speaker"]), op_nar)
    skip = (
        ["tra_loi_dung", "mo_dien_bien_moi"] if (is_continue or is_chain)
        else (["mo_dien_bien_moi"] if (dec.get("turn_type") == "chuyen_canh" or dec.get("user_pass_ball")) else [])
    )
    if is_scene_all:
        for k_sk in ("viet_thoai_nv_khac", "sai_xung_ho", "metagaming_leak"):
            if k_sk not in skip:
                skip.append(k_sk)
    other_chars_text = "" if is_scene_all else ", ".join(group.name_of(pack, c) for c in ch_parts if c not in ("user", dec["speaker"]))
    blind_turns_text = "" if (is_narr or is_scene_all) else ", ".join(
        f"#{m.get('turn_id', i + 1)}"
        for i, m in enumerate(history[-5:])
        if isinstance(m.get("witnesses"), list) and dec["speaker"] not in m["witnesses"] and m.get("speaker") != dec["speaker"]
    )
    return user_msg, prev, skip, other_chars_text, blind_turns_text


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

    def _spawn_async_summary(self, session_key, pack, state, history, o, gen, end_override, prev_day=None):
        with SUMMARY_LOCK:
            cur = SUMMARY_JOBS.get(session_key)
            if cur and cur.get("running"):
                return None
            job_id = f"{session_key}_{end_override}_{int(time.time())}"
            SUMMARY_JOBS[session_key] = {"running": True, "done": False, "job_id": job_id, "end": end_override}

        def worker():
            try:
                end, header, smsgs = group.summary_messages(pack, state, history, end_override=end_override)
                summ, su = "", {}
                for ev in gen(smsgs, {**o, "reasoning": "off", "max_tokens": 900, "temperature": 0.3}):
                    if ev["type"] == "content":
                        summ += ev["text"]
                    elif ev["type"] == "usage":
                        su = ev
                arc_text, su2 = None, {}
                if summ.strip() and len(state.get("recent_scenes") or []) >= group.MAX_RECENT_SCENES:
                    dropped = state["recent_scenes"][0]
                    dropped_txt = dropped if isinstance(dropped, str) else dropped.get("text", "")
                    amsgs = group.arc_merge_messages(state.get("story_arc", ""), dropped_txt)
                    abuf = ""
                    for ev in gen(amsgs, {**o, "reasoning": "off", "max_tokens": 500, "temperature": 0.2}):
                        if ev["type"] == "content":
                            abuf += ev["text"]
                        elif ev["type"] == "usage":
                            su2 = ev
                    if abuf.strip():
                        arc_text = abuf.strip()
                cost = ((su.get("cost") or 0) + (su2.get("cost") or 0))
                with SUMMARY_LOCK:
                    SUMMARY_JOBS[session_key] = {
                        "running": False, "done": True, "job_id": job_id, "end": end,
                        "header": header, "body_text": summ.strip(), "arc_text": arc_text, "cost": cost,
                    }
            except Exception as err:
                with SUMMARY_LOCK:
                    SUMMARY_JOBS[session_key] = {"running": False, "done": True, "job_id": job_id, "error": str(err)}

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return job_id

    def _group_turn(self, req):
        """1 luot group chat v2 (5 Pha): Pha 1 -> EX1 -> Pha 2 -> Pha 3 -> Pha 4 -> Pha 5 + Async Summary. Stream SSE."""
        raw_pack = req["pack"]
        pack = raw_pack if raw_pack.get("format") == "world-v2" else rpformat.load_world(raw_pack)[0]
        state = group._ensure_v2_state(pack, dict(req.get("state") or {}))
        o = req.get("options", {})
        lorebooks = [b for b in req.get("lorebooks", []) if b.get("enabled", True)]
        history = group.rp_history(req.get("history", []))
        history_before = copy.deepcopy(history)
        state_before = copy.deepcopy(state)
        user_input = (req.get("user_msg") or "").strip()
        if user_input and commands.OOC_INLINE_RE.match(user_input):
            self._group_command({**req, "command": commands.normalize_ooc_input(user_input)})
            return
        if user_input:
            history.append({"speaker": "user", "content": user_input, "turn_id": len(history) + 1})

        key = env("OPENROUTER_API_KEY")
        session_key = req.get("session_id") or pack.get("id") or "default"
        turn_source = req.get("turn_source") or ("npc_chain" if (req.get("force_speaker") and "phản ứng dây chuyền" in (req.get("force_reason") or "")) else "user")
        channel_override = req.get("channel_override") or "auto"
        extract_model = o.get("extract_model") or (o.get("model") if o.get("provider") == "openrouter" else "deepseek/deepseek-v4.1-flash-1017")
        user_display = group.user_name_of(pack)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        t0, extra_cost = time.time(), 0.0
        try:
            # Dau luot: hop nhat Async Scene Summary tu bo dem cho (neu da xong duoi SUMMARY_LOCK)
            merged_job = None
            with SUMMARY_LOCK:
                job = SUMMARY_JOBS.get(session_key)
                if job and job.get("done"):
                    SUMMARY_JOBS.pop(session_key, None)
                    merged_job = job
            if merged_job and not merged_job.get("error") and merged_job.get("body_text"):
                if int(merged_job.get("end", 0)) > int(state.get("summarized_upto", 0)):
                    state, _ = group.apply_scene_summary(
                        pack, state, merged_job["header"], merged_job["body_text"],
                        merged_job["end"], arc_text=merged_job.get("arc_text"),
                    )
                    state["summary_sync"] = {"pending": False, "last_job_id": merged_job.get("job_id")}
                    extra_cost += merged_job.get("cost") or 0
                    emit({"type": "state", "state": state, "info": {"merged_async_summary": True, "summarized_upto": merged_job["end"]}})

            prev_loc = (state.get("entities") or {}).get("user", {}).get("location_id")
            prev_day = (state.get("clock") or {}).get("day")

            emit({"type": "phase", "phase": "1", "label": "Pha 1: Đang phân tích & định tuyến..."})
            dmodel = o.get("director_model") or "microsoft/microsoft-decision-1"
            group_mode = "scene" if o.get("group_mode") == "scene" else "single"
            try:
                dec = group.direct(
                    key, dmodel, pack, state, history,
                    force_speaker=req.get("force_speaker"),
                    force_reason=req.get("force_reason"),
                    turn_source=turn_source,
                    channel_override=channel_override,
                    group_mode=group_mode,
                )
            except Exception as e:
                dec = {
                    "turn_source": turn_source,
                    "group_mode": group_mode,
                    "channel_type": (state.get("active_channel") or {}).get("channel_type", "in_person"),
                    "speaker": group.NARRATOR, "speaker_name": "Người dẫn truyện",
                    "speaker_why": f"đạo diễn lỗi ({e})", "speaker_conf": 0, "speaker_probs": {},
                    "arriving": [], "entering": False, "moving_companions": [],
                    "user_time_delta": "instant", "user_move_to": "stay",
                    "turn_type": "mo_ta", "turn_type_label": group.TURN_TYPES["mo_ta"],
                    "lore": [], "lore_used": [], "active_modules": [], "cost": 0, "secs": 0,
                }
            extra_cost += dec.get("cost") or 0

            # EX1 Pre-Render Update (Chi chay cho turn_source == "user")
            if turn_source == "user":
                state, ex_cost = group.apply_pre_render_updates(key, extract_model, pack, state, history, dec)
                extra_cost += ex_cost
                # Cap nhat witnesses & channel_type cho tin user vua gui
                if user_input and history and history[-1]["speaker"] == "user":
                    history[-1]["channel_type"] = state["active_channel"]["channel_type"]
                    history[-1]["witnesses"] = list(state["active_channel"]["participants"])
                emit({"type": "state", "state": state, "info": {"pre_render": True}})

            emit({"type": "director", **{k: v for k, v in dec.items() if k != "decision_trace"}})

            emit({"type": "phase", "phase": "2", "label": "Pha 2: Đang biên dịch ngữ cảnh 4 Zone..."})
            active_lore, lore_log, lore_tokens = [], [], 0
            if lorebooks:
                active_lore, lore_log, lore_tokens = lore.activate(
                    lorebooks, history,
                    scan_depth=int(o.get("lore_scan_depth", 4)),
                    recursive=bool(o.get("lore_recursive", True)),
                    budget=int(o.get("lore_budget", 6000)),
                    whole_words=bool(o.get("lore_whole_words", True)),
                )
                emit({"type": "lore", "entries": lore_log, "tokens": lore_tokens})

            msgs = group.compile_context_4zones(pack, state, history, dec, active_lore=active_lore)
            use_trans = translator.is_enabled(o)
            if use_trans:
                msgs = translator.apply_pivot_instruction(msgs, pack, state, dec, o)
            zones_info = prompts.split_4zones(msgs)
            gen = stream_gemini if o.get("provider") in ("google", "vertex") else stream_openai
            usage, text = {}, ""
            draft_text, draft_reasoning, final_reasoning = "", "", ""
            req_pass1, req_pass2 = None, None
            jr_out, jr2_out = None, None
            trans_meta = None
            o = effective_writer_options(o)

            ch_parts = (state.get("active_channel") or {}).get("participants") or []
            is_narr = dec["speaker"] == group.NARRATOR
            is_scene_all = dec["speaker"] == group.SCENE_SPEAKER
            pr_info, focus_id = _resolve_turn_pronouns(pack, state, history, dec["speaker"], ch_parts)
            pronoun_rule_text = pr_info.get("focus_rule_text", "")
            focus_pair = pr_info.get("focus_pair")

            stream_map = {"openai": stream_openai, "gemini": stream_gemini, "has_gemini_key": bool(env("GEMINI_API_KEY"))}

            def _translate(raw_src, stream_to_content=False, extra_fix_note=""):
                nonlocal extra_cost, trans_meta
                if not (raw_src or "").strip():
                    return ""
                pv = translator.pivot_of(o)
                lbl = (
                    "Pha 3b: Đang viết văn từ dàn ý (OUTLINE → VI)..."
                    if pv == "outline"
                    else f"Pha 3b: Đang biên dịch ({pv.upper()} → VI)..."
                )
                emit({"type": "phase", "phase": "3b", "label": lbl})
                vi_text, tmeta = translator.run_translate(
                    raw_src, pack, state, history, dec, pr_info, o, stream_map,
                    on_chunk=(lambda ch: emit({"type": "content", "text": ch})) if stream_to_content else None,
                    extra_fix_note=extra_fix_note,
                    on_reset=(lambda: emit({"type": "reset_content"})) if stream_to_content else None,
                    active_lore=active_lore,
                )
                extra_cost += tmeta.get("cost") or 0
                trans_meta = tmeta
                emit({"type": "translation", **{k: v for k, v in tmeta.items() if k != "request_body"}})
                return vi_text

            emit({"type": "phase", "phase": "3", "label": "Pha 3: Đang viết..."})
            raw_draft = ""
            if o.get("gate"):
                draft, cut = "", False
                for ev in gen(msgs, o):
                    if ev["type"] == "content":
                        draft += ev["text"]
                        emit({"type": "draft", "text": ev["text"]})
                    elif ev["type"] == "reasoning":
                        draft_reasoning += ev["text"]
                        emit(ev)
                    elif ev["type"] == "usage":
                        usage = ev
                    elif ev["type"] == "request":
                        req_pass1 = ev.get("body")
                        emit(ev)
                    elif ev["type"] == "notice" and "Bị cắt" in ev.get("text", ""):
                        cut = True
                raw_draft = draft
                if use_trans and raw_draft.strip():
                    draft = _translate(raw_draft, stream_to_content=False)
                draft_text = draft
                user_msg, prev, skip, other_chars_text, blind_turns_text = _gate_inputs(pack, state, history, dec, turn_source)
                jthresh = float(o.get("judge_threshold", 0.6))

                emit({"type": "phase", "phase": "4", "label": "Pha 4: Đang rà soát cổng chấm..."})
                slip = judge.detect_pronoun_slip(draft, pack, state, dec["speaker"], ch_parts, focus_id) if (not is_narr and not is_scene_all) else None
                jr = judge.judge(
                    key, o.get("judge_model") or judge.JUDGE_MODELS[0], prev, user_msg, draft,
                    dec["speaker_name"], user_display, jthresh, skip=skip,
                    other_chars_text=other_chars_text, pronoun_rule_text=pronoun_rule_text, is_narrator=is_narr,
                    blind_turns_text=blind_turns_text,
                )
                jr["labels"] = {k: v["label"] for k, v in judge.CRITERIA.items()} | {"bi_cat": "bị cắt cụt"}
                if slip and "sai_xung_ho" not in (jr.get("failed") or []):
                    jr["passed"] = False
                    jr["failed"] = list(jr.get("failed", [])) + ["sai_xung_ho"]
                    jr.setdefault("probs", {})["sai_xung_ho"] = 1.0
                    jr["regex_pronoun_slip"] = slip["slipped"]
                    if jr.get("decision_trace"):
                        jr["decision_trace"]["rows"].append({
                            "key": "regex_pronoun_slip", "type": "regex",
                            "question": "Hậu kiểm Regex 0ms trên lời thoại ngoặc kép",
                            "answer": f"Phát hiện từ cấm: {slip['slipped']}", "threshold": "0ms",
                            "effect": "Ép sửa phẫu thuật (sai_xung_ho)",
                        })
                if cut:
                    jr["passed"] = False
                    jr["failed"] = list(jr.get("failed", [])) + ["bi_cat"]
                    jr["score"] = 0
                jr_out = jr
                emit({"type": "judge", **{k: v for k, v in jr.items() if k != "decision_trace"}})
                extra_cost += jr.get("cost") or 0
                if (jr.get("passed") and not cut) or not draft.strip():
                    text = draft
                    final_reasoning = draft_reasoning
                    emit({"type": "content", "text": draft})
                else:
                    failed = [k for k in jr.get("failed", []) if k in judge.CRITERIA]
                    if use_trans and failed == ["sai_xung_ho"] and not cut and raw_draft.strip():
                        emit({"type": "phase", "phase": "4b", "label": "Pha 4: Đang sửa lại xưng hô ở bước biên dịch..."})
                        fix_note = pronoun_rule_text or ""
                        if slip:
                            fix_note += f"\nTừ cấm vừa bị trượt: '{slip['slipped']}' — tuyệt đối không dùng."
                        text = _translate(raw_draft, stream_to_content=True, extra_fix_note=fix_note)
                        final_reasoning = draft_reasoning
                    else:
                        emit({"type": "phase", "phase": "4b", "label": "Pha 4: Đang viết lại sửa lỗi..."})
                        fix = judge.fix_instruction(
                            draft, failed, jr.get("probs", {}), dec["speaker_name"], user_display,
                            other_chars_text=other_chars_text, pronoun_rule_text=pronoun_rule_text, focus_pair=focus_pair,
                            blind_turns_text=blind_turns_text,
                        )
                        u1 = usage
                        rw_reasoning = ""
                        rw_raw = ""
                        for ev in gen(msgs + [{"role": "system", "content": fix}], {**o, "max_tokens": int(o.get("max_tokens", 1500)) * 2}):
                            if ev["type"] == "content":
                                if use_trans:
                                    rw_raw += ev["text"]
                                else:
                                    text += ev["text"]
                                    emit(ev)
                            elif ev["type"] == "request":
                                req_pass2 = ev.get("body")
                            elif ev["type"] == "usage":
                                usage = {
                                    **ev,
                                    **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")},
                                }
                            elif ev["type"] == "reasoning":
                                rw_reasoning += ev["text"]
                                emit(ev)
                            elif ev["type"] == "notice":
                                emit(ev)
                        final_reasoning = rw_reasoning or draft_reasoning
                        if use_trans and rw_raw.strip():
                            text = _translate(rw_raw, stream_to_content=True)
                    if text.strip():
                        jr2 = judge.judge(
                            key, o.get("judge_model") or judge.JUDGE_MODELS[0], prev, user_msg, text,
                            dec["speaker_name"], user_display, jthresh, skip=skip,
                            other_chars_text=other_chars_text, pronoun_rule_text=pronoun_rule_text, is_narrator=is_narr,
                            blind_turns_text=blind_turns_text,
                        )
                        jr2_out = jr2
                        extra_cost += jr2.get("cost") or 0
                        keep_draft = not jr2.get("error") and jr2.get("score", 0) < jr.get("score", 0)
                        emit({
                            "type": "rejudge", "score_draft": jr.get("score"), "score_final": jr2.get("score"),
                            "passed": jr2.get("passed"), "failed": jr2.get("failed"), "kept": "draft" if keep_draft else "final",
                        })
                        if keep_draft:
                            text = draft
                            final_reasoning = draft_reasoning
                            emit({"type": "reset_content"})
                            emit({"type": "content", "text": draft})
            else:
                if use_trans:
                    for ev in gen(msgs, o):
                        if ev["type"] == "content":
                            raw_draft += ev["text"]
                            emit({"type": "draft", "text": ev["text"]})
                        elif ev["type"] == "reasoning":
                            final_reasoning += ev["text"]
                            emit(ev)
                        elif ev["type"] == "request":
                            req_pass1 = ev.get("body")
                            emit(ev)
                        elif ev["type"] == "usage":
                            usage = ev
                        else:
                            emit(ev)
                    draft_text = raw_draft
                    if raw_draft.strip():
                        text = _translate(raw_draft, stream_to_content=True)
                else:
                    for ev in gen(msgs, o):
                        if ev["type"] == "content":
                            text += ev["text"]
                        elif ev["type"] == "reasoning":
                            final_reasoning += ev["text"]
                        elif ev["type"] == "request":
                            req_pass1 = ev.get("body")
                        if ev["type"] == "usage":
                            usage = ev
                        else:
                            emit(ev)
                slip = judge.detect_pronoun_slip(text, pack, state, dec["speaker"], ch_parts, focus_id) if (not is_narr and not is_scene_all and text.strip()) else None
                if slip:
                    emit({"type": "notice", "text": f"Phát hiện trượt đại từ '{slip['slipped']}' → đang sửa phẫu thuật..."})
                    emit({"type": "reset_content"})
                    if use_trans and raw_draft.strip():
                        fix_note = (pronoun_rule_text or "") + f"\nTừ cấm vừa bị trượt: '{slip['slipped']}' — tuyệt đối không dùng."
                        text = _translate(raw_draft, stream_to_content=True, extra_fix_note=fix_note)
                    else:
                        fix = judge.fix_instruction(
                            text, ["sai_xung_ho"], {}, dec["speaker_name"], user_display,
                            pronoun_rule_text=pronoun_rule_text, focus_pair=focus_pair,
                        )
                        text, u1 = "", usage
                        for ev in gen(msgs + [{"role": "system", "content": fix}], {**o, "max_tokens": int(o.get("max_tokens", 1500)) * 2}):
                            if ev["type"] == "content":
                                text += ev["text"]
                                emit(ev)
                            elif ev["type"] == "request":
                                req_pass2 = ev.get("body")
                            elif ev["type"] == "usage":
                                usage = {
                                    **ev,
                                    **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")},
                                }
                            elif ev["type"] == "reasoning":
                                final_reasoning += ev["text"]
                                emit(ev)
                            elif ev["type"] == "notice":
                                emit(ev)

            # Chong lap
            prev_ai = next((m["content"] for m in reversed(history) if m["speaker"] != "user"), "")
            if prev_ai and difflib.SequenceMatcher(None, prev_ai[:1500], text.strip()[:1500]).ratio() > 0.6:
                emit({"type": "notice", "text": "Bản vừa viết lặp lại gần nguyên văn tin trước → viết lại."})
                emit({"type": "reset_content"})
                text, u1 = "", usage
                anti = {
                    "role": "system",
                    "content": prompts.get_text("judge.anti_repeat"),
                }
                rw_anti = ""
                for ev in gen(msgs + [anti], o):
                    if ev["type"] == "content":
                        if use_trans:
                            rw_anti += ev["text"]
                        else:
                            text += ev["text"]
                            emit(ev)
                    elif ev["type"] == "request":
                        req_pass2 = ev.get("body")
                    elif ev["type"] == "reasoning":
                        final_reasoning += ev["text"]
                        emit(ev)
                    elif ev["type"] == "usage":
                        usage = {
                            **ev,
                            **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")},
                        }
                if use_trans and rw_anti.strip():
                    text = _translate(rw_anti, stream_to_content=True)

            turn_ch = (state.get("active_channel") or {}).get("channel_type", "in_person")
            turn_witnesses = list((state.get("active_channel") or {}).get("participants") or [])
            ai_turn_id = len(history) + 1
            history.append({
                "turn_id": ai_turn_id,
                "speaker": dec["speaker"],
                "content": text.strip(),
                "channel_type": turn_ch,
                "witnesses": turn_witnesses,
            })
            emit({
                "type": "turn",
                "turn_id": ai_turn_id,
                "speaker": dec["speaker"],
                "speaker_name": dec["speaker_name"],
                "channel_type": turn_ch,
                "witnesses": turn_witnesses,
                "user_witnesses": turn_witnesses if user_input else None,
            })

            emit({"type": "phase", "phase": "5", "label": "Pha 5: Đang cập nhật hệ quả & bộ nhớ..."})
            final_state = state
            sinfo_trace = None
            try:
                new_state, sinfo = group.update_state(
                    key, dmodel, extract_model, pack, state, history,
                    owed=dec.get("owed", []), arriving=dec.get("arriving", []),
                    skip_chain=(group_mode == "scene" or is_scene_all),
                )
                sinfo_trace = sinfo.pop("decision_trace", None)
                extra_cost += sinfo.get("cost") or 0
                new_loc = (new_state.get("entities") or {}).get("user", {}).get("location_id")
                new_day = (new_state.get("clock") or {}).get("day")
                if group.summary_due(history, new_state, prev_loc=prev_loc, new_loc=new_loc, prev_day=prev_day, new_day=new_day):
                    upto, end = group.summary_range(history, new_state, prev_loc=prev_loc, new_loc=new_loc, prev_day=prev_day, new_day=new_day)
                    if end > upto:
                        job_id = self._spawn_async_summary(session_key, pack, new_state, history, o, gen, end, prev_day=prev_day)
                        if job_id:
                            new_state["summary_sync"] = {"pending": True, "last_job_id": job_id}
                            sinfo["async_summary_scheduled"] = end
                final_state = new_state
                emit({"type": "state", "state": new_state, "info": sinfo})
            except Exception as e:
                emit({"type": "notice", "text": f"Không cập nhật được trạng thái: {e}"})

            usage_out = {
                **usage,
                "secs": round(time.time() - t0, 1),
                "extra_cost": round(extra_cost, 7),
                "cost": (usage.get("cost") or 0) + extra_cost if usage.get("cost") is not None else None,
            }
            jr_clean = {k: v for k, v in jr_out.items() if k != "decision_trace"} if jr_out else None
            dec_traces = [
                x for x in (
                    dec.get("decision_trace"),
                    jr_out.get("decision_trace") if jr_out else None,
                    jr2_out.get("decision_trace") if jr2_out else None,
                    sinfo_trace,
                ) if x
            ]
            trace_obj = {
                "session_id": session_key,
                "turn_id": ai_turn_id,
                "ts": int(time.time()),
                "speaker": dec["speaker"],
                "speaker_name": dec["speaker_name"],
                "channel_type": turn_ch,
                "user_msg": user_input,
                "zones_info": zones_info,
                "decision_traces": dec_traces,
                "state_diff": prompts.compute_state_diff(pack, state_before, final_state),
                "requests": {
                    "pass1": req_pass1,
                    "pass2": req_pass2,
                    **({"translate": trans_meta.get("request_body")} if trans_meta and trans_meta.get("request_body") else {}),
                },
                "replay_ctx": {
                    "state_before": state_before,
                    "history_before": history_before,
                    "user_msg": user_input,
                    "force_speaker": dec["speaker"],
                    "turn_source": turn_source,
                    "channel_override": channel_override,
                    "group_mode": group_mode,
                },
                "output": {
                    "draft": draft_text,
                    "final": text.strip(),
                    "reasoning": final_reasoning,
                    "usage": usage_out,
                    "judge": jr_clean,
                    **({"translation": {k: v for k, v in trans_meta.items() if k != "request_body"}} if trans_meta else {}),
                },
            }
            z_meta = prompts.save_trace(session_key, trace_obj)
            emit({"type": "xray", "turn_id": ai_turn_id, **z_meta})

            emit({"type": "phase", "phase": "done", "label": ""})
            emit({**usage_out, "type": "usage"})
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

    def _group_command(self, req):
        """Thuc thi lenh ngoai bang (in_context=False). Stream SSE."""
        raw_pack = req["pack"]
        pack = raw_pack if raw_pack.get("format") == "world-v2" else rpformat.load_world(raw_pack)[0]
        state = group._ensure_v2_state(pack, dict(req.get("state") or {}))
        o = req.get("options", {})
        res = commands.execute_command(req.get("command", ""), pack, state, req.get("history", []))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        t0 = time.time()
        try:
            emit({"type": "cmd_meta", **{k: res.get(k) for k in ("cmd", "title", "summary", "snapshot", "needs_llm")}})
            if res.get("mutated_state"):
                emit({"type": "state", "state": res["mutated_state"], "info": {"from_command": True}})
            if res.get("needs_llm") and res.get("msgs"):
                emit({"type": "phase", "phase": "cmd", "label": f"⌘ Đang chạy {res.get('cmd', '/command')}..."})
                gen = stream_gemini if o.get("provider") in ("google", "vertex") else stream_openai
                cmd_opts = {**o, **(res.get("llm_options") or {})}
                usage = {}
                for ev in gen(res["msgs"], cmd_opts):
                    if ev["type"] == "content":
                        emit(ev)
                    elif ev["type"] == "usage":
                        usage = ev
                    elif ev["type"] == "notice":
                        emit(ev)
                if usage:
                    emit({**usage, "type": "usage", "secs": round(time.time() - t0, 1)})
            emit({"type": "phase", "phase": "done", "label": ""})
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

    def _group_replay(self, req):
        """Single-Turn A/B Replay: chay lai 1 luot voi prompt hien tai trong Registry, khong ghi de state/history."""
        raw_pack = req["pack"]
        pack = raw_pack if raw_pack.get("format") == "world-v2" else rpformat.load_world(raw_pack)[0]
        session_id = req.get("session_id") or pack.get("id") or "default"
        turn_id = req.get("turn_id")
        orig_trace = prompts.load_trace(session_id, int(turn_id)) if turn_id is not None else None
        rctx = req.get("replay_ctx") or (orig_trace or {}).get("replay_ctx")
        if not rctx:
            raise RuntimeError("Không tìm thấy ngữ cảnh lưu lại (replay_ctx) cho lượt này.")

        state = group._ensure_v2_state(pack, copy.deepcopy(rctx["state_before"]))
        history = group.rp_history(copy.deepcopy(rctx.get("history_before") or []))
        user_input = (rctx.get("user_msg") or "").strip()
        if user_input:
            history.append({"speaker": "user", "content": user_input, "turn_id": len(history) + 1})

        o = req.get("options", {})
        lorebooks = [b for b in req.get("lorebooks", []) if b.get("enabled", True)]
        key = env("OPENROUTER_API_KEY")
        dmodel = o.get("director_model") or "microsoft/microsoft-decision-1"
        extract_model = o.get("extract_model") or (o.get("model") if o.get("provider") == "openrouter" else "deepseek/deepseek-v4.1-flash-1017")
        user_display = group.user_name_of(pack)
        turn_source = rctx.get("turn_source") or "user"
        channel_override = rctx.get("channel_override") or "auto"
        group_mode = rctx.get("group_mode") or ("scene" if o.get("group_mode") == "scene" else "single")

        t0, extra_cost = time.time(), 0.0
        dec = group.direct(
            key, dmodel, pack, state, history,
            force_speaker=rctx.get("force_speaker"),
            force_reason="A/B Replay",
            turn_source=turn_source,
            channel_override=channel_override,
            group_mode=group_mode,
        )
        extra_cost += dec.get("cost") or 0
        if turn_source == "user":
            state, ex_cost = group.apply_pre_render_updates(key, extract_model, pack, state, history, dec)
            extra_cost += ex_cost

        active_lore = []
        if lorebooks:
            active_lore, _, _ = lore.activate(
                lorebooks, history,
                scan_depth=int(o.get("lore_scan_depth", 4)),
                recursive=bool(o.get("lore_recursive", True)),
                budget=int(o.get("lore_budget", 6000)),
                whole_words=bool(o.get("lore_whole_words", True)),
            )

        msgs = group.compile_context_4zones(pack, state, history, dec, active_lore=active_lore)
        use_trans = translator.is_enabled(o)
        if use_trans:
            msgs = translator.apply_pivot_instruction(msgs, pack, state, dec, o)
        gen = stream_gemini if o.get("provider") in ("google", "vertex") else stream_openai
        o = effective_writer_options(o)

        ch_parts = (state.get("active_channel") or {}).get("participants") or []
        is_narr = dec["speaker"] == group.NARRATOR
        is_scene_all = dec["speaker"] == group.SCENE_SPEAKER
        pr_info, focus_id = _resolve_turn_pronouns(pack, state, history, dec["speaker"], ch_parts)
        pronoun_rule_text = pr_info.get("focus_rule_text", "")
        focus_pair = pr_info.get("focus_pair")

        stream_map = {"openai": stream_openai, "gemini": stream_gemini, "has_gemini_key": bool(env("GEMINI_API_KEY"))}
        trans_meta = None

        def _replay_translate(raw_src, extra_fix_note=""):
            nonlocal extra_cost, trans_meta
            if not (raw_src or "").strip():
                return ""
            vi_txt, tmeta = translator.run_translate(
                raw_src, pack, state, history, dec, pr_info, o, stream_map,
                extra_fix_note=extra_fix_note,
                active_lore=active_lore,
            )
            extra_cost += tmeta.get("cost") or 0
            trans_meta = tmeta
            return vi_txt

        draft_text, text, draft_reasoning, final_reasoning = "", "", "", ""
        usage, req_pass1, req_pass2 = {}, None, None
        jr_out, jr2_out = None, None
        cut = False
        for ev in gen(msgs, o):
            if ev["type"] == "content":
                draft_text += ev["text"]
            elif ev["type"] == "reasoning":
                draft_reasoning += ev["text"]
            elif ev["type"] == "request":
                req_pass1 = ev.get("body")
            elif ev["type"] == "usage":
                usage = ev
            elif ev["type"] == "notice" and "Bị cắt" in ev.get("text", ""):
                cut = True

        raw_draft = draft_text
        if use_trans and raw_draft.strip():
            draft_text = _replay_translate(raw_draft)

        if o.get("gate"):
            user_msg, prev, skip, other_chars_text, blind_turns_text = _gate_inputs(pack, state, history, dec, turn_source)
            jthresh = float(o.get("judge_threshold", 0.6))
            slip = judge.detect_pronoun_slip(draft_text, pack, state, dec["speaker"], ch_parts, focus_id) if (not is_narr and not is_scene_all) else None
            jr = judge.judge(
                key, o.get("judge_model") or judge.JUDGE_MODELS[0], prev, user_msg, draft_text,
                dec["speaker_name"], user_display, jthresh, skip=skip,
                other_chars_text=other_chars_text, pronoun_rule_text=pronoun_rule_text, is_narrator=is_narr,
                blind_turns_text=blind_turns_text,
            )
            jr["labels"] = {k: v["label"] for k, v in judge.CRITERIA.items()} | {"bi_cat": "bị cắt cụt"}
            if slip and "sai_xung_ho" not in (jr.get("failed") or []):
                jr["passed"] = False
                jr["failed"] = list(jr.get("failed", [])) + ["sai_xung_ho"]
                jr.setdefault("probs", {})["sai_xung_ho"] = 1.0
                jr["regex_pronoun_slip"] = slip["slipped"]
            if cut:
                jr["passed"] = False
                jr["failed"] = list(jr.get("failed", [])) + ["bi_cat"]
                jr["score"] = 0
            jr_out = jr
            extra_cost += jr.get("cost") or 0
            if (jr.get("passed") and not cut) or not draft_text.strip():
                text, final_reasoning = draft_text, draft_reasoning
            else:
                failed = [k for k in jr.get("failed", []) if k in judge.CRITERIA]
                if use_trans and failed == ["sai_xung_ho"] and not cut and raw_draft.strip():
                    fix_note = pronoun_rule_text or ""
                    if slip:
                        fix_note += f"\nTừ cấm vừa bị trượt: '{slip['slipped']}' — tuyệt đối không dùng."
                    text = _replay_translate(raw_draft, extra_fix_note=fix_note)
                    final_reasoning = draft_reasoning
                else:
                    fix = judge.fix_instruction(
                        draft_text, failed, jr.get("probs", {}), dec["speaker_name"], user_display,
                        other_chars_text=other_chars_text, pronoun_rule_text=pronoun_rule_text, focus_pair=focus_pair,
                        blind_turns_text=blind_turns_text,
                    )
                    u1, rw_reasoning, rw_raw = usage, "", ""
                    for ev in gen(msgs + [{"role": "system", "content": fix}], {**o, "max_tokens": int(o.get("max_tokens", 1500)) * 2}):
                        if ev["type"] == "content":
                            if use_trans:
                                rw_raw += ev["text"]
                            else:
                                text += ev["text"]
                        elif ev["type"] == "reasoning":
                            rw_reasoning += ev["text"]
                        elif ev["type"] == "request":
                            req_pass2 = ev.get("body")
                        elif ev["type"] == "usage":
                            usage = {
                                **ev,
                                **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")},
                            }
                    final_reasoning = rw_reasoning or draft_reasoning
                    if use_trans and rw_raw.strip():
                        text = _replay_translate(rw_raw)
                if text.strip():
                    jr2 = judge.judge(
                        key, o.get("judge_model") or judge.JUDGE_MODELS[0], prev, user_msg, text,
                        dec["speaker_name"], user_display, jthresh, skip=skip,
                        other_chars_text=other_chars_text, pronoun_rule_text=pronoun_rule_text, is_narrator=is_narr,
                        blind_turns_text=blind_turns_text,
                    )
                    jr2_out = jr2
                    extra_cost += jr2.get("cost") or 0
                    if not jr2.get("error") and jr2.get("score", 0) < jr.get("score", 0):
                        text, final_reasoning = draft_text, draft_reasoning
        else:
            text, final_reasoning = draft_text, draft_reasoning
            slip = judge.detect_pronoun_slip(text, pack, state, dec["speaker"], ch_parts, focus_id) if (not is_narr and text.strip()) else None
            if slip:
                if use_trans and raw_draft.strip():
                    fix_note = (pronoun_rule_text or "") + f"\nTừ cấm vừa bị trượt: '{slip['slipped']}' — tuyệt đối không dùng."
                    text = _replay_translate(raw_draft, extra_fix_note=fix_note)
                else:
                    fix = judge.fix_instruction(
                        text, ["sai_xung_ho"], {}, dec["speaker_name"], user_display,
                        pronoun_rule_text=pronoun_rule_text, focus_pair=focus_pair,
                    )
                    text, u1 = "", usage
                    for ev in gen(msgs + [{"role": "system", "content": fix}], {**o, "max_tokens": int(o.get("max_tokens", 1500)) * 2}):
                        if ev["type"] == "content":
                            text += ev["text"]
                        elif ev["type"] == "reasoning":
                            final_reasoning += ev["text"]
                        elif ev["type"] == "request":
                            req_pass2 = ev.get("body")
                        elif ev["type"] == "usage":
                            usage = {
                                **ev,
                                **{k: (ev.get(k) or 0) + (u1.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cost")},
                            }

        secs = round(time.time() - t0, 1)
        jr_clean = {k: v for k, v in jr_out.items() if k != "decision_trace"} if jr_out else None
        return {
            "original": (orig_trace or {}).get("output"),
            "original_zones": (orig_trace or {}).get("zones_info"),
            "replay": {
                "text": text.strip(),
                "draft": draft_text,
                "reasoning": final_reasoning,
                "usage": {
                    **usage,
                    "secs": secs,
                    "extra_cost": round(extra_cost, 7),
                    "cost": (usage.get("cost") or 0) + extra_cost if usage.get("cost") is not None else None,
                },
                "judge": jr_clean,
                **({"translation": {k: v for k, v in trans_meta.items() if k != "request_body"}} if trans_meta else {}),
                "zones_info": prompts.split_4zones(msgs),
                "request_body": req_pass2 or req_pass1,
                "decision_traces": [
                    x for x in (
                        dec.get("decision_trace"),
                        jr_out.get("decision_trace") if jr_out else None,
                        jr2_out.get("decision_trace") if jr2_out else None,
                    ) if x
                ],
                "secs": secs,
            },
        }

    def do_GET(self):
        path_only = self.path.split("?", 1)[0]
        if path_only in ("/", "/index.html", "/group", "/group.html", "/editor", "/editor.html", "/prompts", "/prompts.html"):
            page = (
                "group.html" if path_only.startswith("/group")
                else "editor.html" if path_only.startswith("/editor")
                else "prompts.html" if path_only.startswith("/prompts")
                else "index.html"
            )
            b = open(os.path.join(HERE, page), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif path_only == "/api/prompts":
            self._json(prompts.get_studio_payload())
        elif path_only.startswith("/api/db/"):
            self._handle_db_get(path_only)
        elif path_only == "/api/group/trace":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = (qs.get("session") or ["default"])[0]
            try:
                tid = int((qs.get("turn") or ["0"])[0])
            except ValueError:
                self._json({"error": "turn không hợp lệ"}, 400)
                return
            tr = prompts.load_trace(sid, tid)
            if not tr:
                self._json({"error": "Chưa có dữ liệu X-Ray cho lượt này (chỉ lưu với các lượt chạy từ bản cập nhật mới)."}, 404)
            else:
                self._json(tr)
        elif self.path == "/api/status":
            self._json({"keys": {k: bool(env(k)) for k in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "VERTEX_API_KEY")}})
        elif self.path.split("?", 1)[0] in ("/api/group/sample", "/api/group/sample_world"):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            world_key = (qs.get("world") or ["hoa_phuong"])[0]
            fname = "dai_hoc_ben_nghe_world.json" if world_key == "ben_nghe" else "hoa_phuong_world.json"
            world = json.load(open(os.path.join(HERE, "..", "samples", fname), encoding="utf-8"))
            pack, _ = rpformat.load_world(world)
            if parsed.path.endswith("_world"):
                # Gan kem characters da nap de editor co du du lieu chinh sua
                out = dict(world)
                out["characters"] = pack["characters"]
                self._json(out)
            else:
                self._json(pack)
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
        if self.path in ("/api/single/card", "/api/single/load"):
            try:
                if self.path == "/api/single/card":
                    raw = self._body()
                    obj = json.loads(raw.decode("utf-8-sig")) if raw[:1] in (b"{", b"\xef") else None
                    card_obj = obj if (obj and obj.get("format") in ("rplab-char-1", "char-v2")) else parse_card_bytes(raw)
                    hdr = self.headers.get("X-User-Opts")
                    opts = json.loads(urllib.parse.unquote(hdr)) if hdr else {}
                else:
                    opts = json.loads(self._body().decode("utf-8-sig"))
                    sample = (opts.get("sample") or "").strip()
                    if sample:
                        spath = (
                            os.path.join(HERE, "..", "samples", "world_sim_card.json")
                            if sample == "world_sim"
                            else os.path.join(HERE, "..", "samples", "chars", f"{sample}.json")
                        )
                        if not os.path.exists(spath):
                            raise FileNotFoundError(f"Không tìm thấy mẫu '{sample}'")
                        card_obj = json.load(open(spath, encoding="utf-8"))
                    else:
                        card_obj = opts.get("raw_card")
                g_idx = opts.get("greeting_idx", 0)
                pack = rpformat.build_single_world_pack(
                    card_obj,
                    user_name=opts.get("user") or "Nem",
                    user_gender=opts.get("gender") or "nam",
                    user_persona=opts.get("persona") or "",
                    custom_style=opts.get("writing_style") or "",
                    greeting_idx=int(0 if g_idx is None else g_idx),
                    include_card_lore=bool(opts.get("card_lore", True)),
                )
                self._json({"pack": pack, "state": pack["state"], "raw_card": card_obj})
            except Exception as e:
                self._json({"error": f"Không đọc được card / phiên 1-1: {e}"}, 400)
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
        if self.path == "/api/group/bootstrap":
            try:
                req = json.loads(self._body().decode("utf-8-sig"))
                raw_pack = req.get("pack") or {}
                pack = raw_pack if raw_pack.get("format") == "world-v2" else rpformat.load_world(raw_pack)[0]
                state = group._ensure_v2_state(pack, req["state"]) if req.get("state") else rpformat.bootstrap_session(pack)
                cost = 0.0
                force_refresh = bool(req.get("force_refresh", False))
                if req.get("use_llm", True) and (force_refresh or (pack.get("relationships") or {}).get("auto_bootstrap", True)):
                    key = env("OPENROUTER_API_KEY")
                    if key:
                        o = req.get("options") or {}
                        ex_model = o.get("extract_model") or (o.get("model") if o.get("provider") == "openrouter" else "deepseek/deepseek-v4.1-flash-1017")
                        state, cost = group.bootstrap_relationships_llm(key, ex_model, pack, state, force_refresh=force_refresh)
                state = rpformat.sync_legacy_state(pack, state)
                self._json({"pack": pack, "state": state, "cost": cost})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if self.path == "/api/group/card":
            # file nhan vat char-v2, rplab-char-1, hoac card SillyTavern (JSON/PNG) -> chuan hoa char-v2
            try:
                raw = self._body()
                ids = json.loads(urllib.parse.unquote(self.headers.get("X-Existing-Ids", "[]")))
                obj = None
                if raw[:1] in (b"{", b"\xef"):
                    obj = json.loads(raw.decode("utf-8-sig"))
                if obj and obj.get("format") in ("rplab-char-1", "char-v2"):
                    char_raw, mo_dau, converted = obj, None, False
                    cid = (char_raw.get("id") or "").strip() or rpformat.slug(((char_raw.get("meta") or {}).get("name")) or char_raw.get("name") or "nhan_vat")
                    char_raw["id"] = cid
                    if cid in ids:
                        raise rpformat.FormatError(f"id '{cid}' đã có trong dàn nhân vật")
                else:
                    char_raw, mo_dau = rpformat.ccv3_to_char(parse_card_bytes(raw), ids)
                    converted = True
                member = rpformat.char_to_internal(char_raw)
                self._json({
                    "member": member,
                    "char": member["v2"],
                    "char_v1": char_raw if char_raw.get("format") == "rplab-char-1" else None,
                    "mo_dau": mo_dau,
                    "converted": converted,
                })
            except Exception as e:
                self._json({"error": f"Không đọc được nhân vật: {e}"}, 400)
            return
        if self.path == "/api/group/turn":
            self._group_turn(json.loads(self._body()))
            return
        if self.path == "/api/group/command":
            self._group_command(json.loads(self._body()))
            return
        if self.path == "/api/prompts":
            try:
                req = json.loads(self._body().decode("utf-8-sig"))
                if req.get("action") == "reset":
                    self._json(prompts.reset_custom(req.get("keys")))
                else:
                    self._json(prompts.save_custom(req.get("updates") or {}))
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if self.path == "/api/group/preview":
            try:
                req = json.loads(self._body().decode("utf-8-sig"))
                raw_pack = req["pack"]
                pack = raw_pack if raw_pack.get("format") == "world-v2" else rpformat.load_world(raw_pack)[0]
                state = group._ensure_v2_state(pack, dict(req.get("state") or {}))
                history = group.rp_history(req.get("history", []))
                user_input = (req.get("user_msg") or "").strip()
                if user_input:
                    history.append({"speaker": "user", "content": user_input, "turn_id": len(history) + 1})
                o = req.get("options", {})
                lorebooks = [b for b in req.get("lorebooks", []) if b.get("enabled", True)]
                active_lore = []
                if lorebooks:
                    active_lore, _, _ = lore.activate(
                        lorebooks, history,
                        scan_depth=int(o.get("lore_scan_depth", 4)),
                        recursive=bool(o.get("lore_recursive", True)),
                        budget=int(o.get("lore_budget", 6000)),
                        whole_words=bool(o.get("lore_whole_words", True)),
                    )
                cast_map = group.cast_by_id(pack)
                sp = req.get("force_speaker")
                if not sp or (sp not in cast_map and sp not in (group.NARRATOR, group.SCENE_SPEAKER)):
                    if o.get("group_mode") == "scene" and len(cast_map) > 1:
                        sp = group.SCENE_SPEAKER
                    else:
                        named = [cid for cid in rpformat.co_located_npcs(pack, state) if user_input and group.mentioned(cast_map[cid], user_input)]
                        co = rpformat.co_located_npcs(pack, state)
                        sp = named[0] if named else (co[0] if co else (next(iter(cast_map), group.NARRATOR)))
                dec = {
                    "speaker": sp,
                    "speaker_name": group.name_of(pack, sp),
                    "turn_type_label": "Đối thoại là chính (Xem trước)",
                    "lore_used": [],
                    "active_modules": [],
                }
                msgs = group.compile_context_4zones(pack, state, history, dec, active_lore=active_lore)
                if translator.is_enabled(o):
                    msgs = translator.apply_pivot_instruction(msgs, pack, state, dec, o)
                zi = prompts.split_4zones(msgs)
                o = effective_writer_options(o)
                if o.get("provider") in ("google", "vertex"):
                    req_preview = {
                        "model": o.get("model"),
                        "messages": msgs,
                        "temperature": float(o.get("temperature", 1.0)),
                        "top_p": float(o.get("top_p", 0.95)),
                        "max_tokens": o["max_tokens"],
                    }
                else:
                    req_preview = build_openai_body(msgs, o)
                self._json({
                    "speaker": sp,
                    "speaker_name": dec["speaker_name"],
                    "zones_info": zi,
                    "messages": msgs,
                    "request_preview": req_preview,
                })
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if self.path == "/api/group/replay":
            try:
                self._json(self._group_replay(json.loads(self._body().decode("utf-8-sig"))))
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if self.path.startswith("/api/db/"):
            self._handle_db_post(self.path.split("?", 1)[0])
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

    def _handle_db_get(self, path_only):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if path_only == "/api/db/sessions":
                mode = (qs.get("mode") or [None])[0] or None
                self._json({"sessions": db.list_sessions(mode=mode)})
            elif path_only == "/api/db/session":
                sid = (qs.get("id") or [""])[0]
                sess = db.get_session(sid)
                if not sess:
                    self._json({"error": "Không tìm thấy phiên lưu"}, 404)
                else:
                    self._json({"session": sess})
            elif path_only == "/api/db/settings":
                scope = (qs.get("scope") or ["single"])[0]
                self._json({"data": db.get_settings(scope)})
            elif path_only == "/api/db/lorebooks":
                scope = (qs.get("scope") or ["single"])[0]
                self._json({"books": db.get_lorebooks(scope)})
            elif path_only == "/api/db/editor":
                wid = (qs.get("id") or ["current"])[0]
                self._json({"draft": db.get_editor_world(wid)})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_db_post(self, path_only):
        try:
            req = json.loads(self._body().decode("utf-8-sig") or "{}")
            if path_only == "/api/db/session":
                self._json(
                    db.upsert_session(
                        req.get("id") or "",
                        req.get("mode") or "single",
                        req.get("title") or "",
                        req.get("pack"),
                        req.get("state") or {},
                        req.get("history") or [],
                    )
                )
            elif path_only == "/api/db/session/rename":
                res = db.rename_session(req.get("id") or "", req.get("title") or "")
                if not res:
                    self._json({"error": "Không tìm thấy phiên lưu hoặc tên trống"}, 404)
                else:
                    self._json(res)
            elif path_only == "/api/db/session/delete":
                sid = req.get("id") or ""
                self._json({"ok": db.delete_session(sid), "id": sid})
            elif path_only == "/api/db/settings":
                self._json(db.save_settings(req.get("scope") or "single", req.get("data") or {}))
            elif path_only == "/api/db/lorebooks":
                self._json(db.save_lorebooks(req.get("scope") or "single", req.get("books") or []))
            elif path_only == "/api/db/editor":
                self._json(db.save_editor_world(req.get("world") or {}, world_id=req.get("id") or "current"))
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 400)


def main():
    for s in (sys.stdout, sys.stderr):  # console Windows (cp1252) khong in duoc tieng Viet
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7861)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Dia chi bind. Mac dinh 127.0.0.1 (chi may nay); 0.0.0.0 = moi giao dien; hoac IP Tailscale")
    ap.add_argument("--listen", action="store_true", help="Tuong duong --host 0.0.0.0")
    a = ap.parse_args()
    host = "0.0.0.0" if a.listen else a.host
    db.init_db()
    srv = ThreadingHTTPServer((host, a.port), Handler)
    if host not in ("127.0.0.1", "localhost"):
        print(f"[CANH BAO] Dang lang nghe tren {host}: server KHONG co dang nhap, ai vao duoc se dung duoc API key cua ban. "
              f"Chi nen mo qua Tailscale.", flush=True)
    print(f"RP Lab: http://{'127.0.0.1' if host == '0.0.0.0' else host}:{a.port}  (Ctrl+C để dừng)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
