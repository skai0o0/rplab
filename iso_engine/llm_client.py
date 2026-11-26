"""Pure Python 3.11 stdlib LLM client, 3-level .env loader, JSON repair, and MockLLMClient."""

from __future__ import annotations

import copy
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

try:
    from . import db
except ImportError:
    import db


HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_VERTEX_URL_TMPL = "https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"

DEFAULT_MODELS = {
    "openrouter": "google/gemini-2.5-flash",
    "gemini": "gemini-2.5-flash",
    "vertex": "gemini-2.5-flash",
    "openai": "",
}

_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 529})

_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)


class LLMError(RuntimeError):
    """Base error raised by LLM clients."""


class LLMHTTPError(LLMError):
    """Raised on HTTP/network/API-level failures (never a JSONDecodeError)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMJSONError(LLMError):
    """Raised when an LLM response cannot be parsed or repaired into a JSON object."""


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse key=value pairs from a .env file without logging secrets."""
    parsed: dict[str, str] = {}
    if not os.path.isfile(path):
        return parsed
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                k = key.strip()
                if not k:
                    continue
                v = val.strip()
                if v and v[0] in ('"', "'"):
                    q = v[0]
                    end_idx = -1
                    if q == '"':
                        i = 1
                        while i < len(v):
                            if v[i] == "\\" and i + 1 < len(v):
                                i += 2
                                continue
                            if v[i] == '"':
                                end_idx = i
                                break
                            i += 1
                    else:
                        end_idx = v.find("'", 1)
                    if end_idx != -1:
                        rest = v[end_idx + 1:].strip()
                        if rest == "" or rest.startswith("#"):
                            v = v[1:end_idx]
                        else:
                            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
                            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                                v = v[1:-1]
                    else:
                        v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
                        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                            v = v[1:-1]
                else:
                    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
                if k not in parsed:
                    parsed[k] = v
    except OSError:
        return parsed
    return parsed


def load_env_var(
    name: str,
    default: str = "",
    *,
    start_dir: str | None = None,
    search_dirs: list[str] | None = None,
) -> str:
    """Resolve environment variable: os.environ first, then leaf-first 3-level .env lookup."""
    key_name = str(name or "").strip()
    if not key_name:
        return default

    env_val = os.environ.get(key_name)
    if env_val is not None and env_val != "":
        return env_val.strip()

    if search_dirs is not None:
        dirs = [os.path.abspath(d) for d in search_dirs if d]
    else:
        base = os.path.abspath(start_dir or HERE)
        parent = os.path.dirname(base)
        grandparent = os.path.dirname(parent)
        dirs = []
        for d in (base, parent, grandparent):
            if d and d not in dirs:
                dirs.append(d)

    for directory in dirs:
        env_path = os.path.join(directory, ".env")
        values = _parse_env_file(env_path)
        if key_name in values and values[key_name] != "":
            return values[key_name]

    return default


def strip_json_fences(raw: str) -> str:
    """Strip markdown code fences (closed or unclosed) and slice from first '{'."""
    s = (raw or "").strip()
    m_open = re.match(r"^```(?:json|JSON)?(?:[ \t]*\r?\n|(?=\{))", s)
    if m_open:
        s = s[m_open.end():]
        s = re.sub(r"\s*```\s*$", "", s).strip()
    else:
        m_json_fence = re.search(r"```(?:json|JSON)\s*\r?\n", s)
        if m_json_fence:
            after = s[m_json_fence.end():]
            if after.rstrip().endswith("```"):
                s = re.sub(r"\s*```\s*$", "", after).strip()
            else:
                m_close = re.search(r"(.*)\s*```", after, re.DOTALL)
                s = m_close.group(1).strip() if m_close else after.strip()
        else:
            fence_idx = s.find("```")
            brace_idx = s.find("{")
            if fence_idx != -1 and (brace_idx == -1 or fence_idx < brace_idx):
                m_prose = re.search(r"```(?:json|JSON)?(?:[ \t]*\r?\n|(?=\{))", s)
                if m_prose:
                    after = s[m_prose.end():]
                    if after.rstrip().endswith("```"):
                        s = re.sub(r"\s*```\s*$", "", after).strip()
                    else:
                        m_close = re.search(r"(.*)\s*```", after, re.DOTALL)
                        s = m_close.group(1).strip() if m_close else after.strip()
    idx = s.find("{")
    if idx != -1:
        s = s[idx:]
    return s


def balance_brackets(snippet: str) -> tuple[list[str], bool]:
    """Scan JSON snippet outside string literals and return (closing_stack, in_string)."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in snippet:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
    return stack, in_str


def _strip_odd_trailing_backslashes(s: str) -> str:
    cnt = 0
    i = len(s) - 1
    while i >= 0 and s[i] == "\\":
        cnt += 1
        i -= 1
    if cnt % 2 == 1:
        return s[:-1]
    return s


def _strip_incomplete_escape(s: str) -> str:
    """Strip trailing incomplete \\u[0-9a-fA-F]{0,3} or \\x[0-9a-fA-F]? when preceded by an unescaped backslash."""
    s = _strip_odd_trailing_backslashes(s)
    m = re.search(r"(\\+)(u[0-9a-fA-F]{0,3}|x[0-9a-fA-F]?)$", s)
    if m and len(m.group(1)) % 2 == 1:
        s = s[: m.start(1) + len(m.group(1)) - 1]
    return s


def _parse_or_repair_json_with_flag(raw_text: str) -> tuple[dict[str, Any], bool]:
    """Parse raw text into a dict, applying string-aware fence/comma/truncation repair."""
    base = strip_json_fences(raw_text)
    if not base or not base.startswith("{"):
        raise json.JSONDecodeError("No JSON object found in response", raw_text or "", 0)

    candidates: list[tuple[str, bool]] = [
        (base, False),
        (re.sub(r",\s*([}\]])", r"\1", base), True),
    ]
    missing_comma_fixed = re.sub(
        r'([}\]"0-9]|true|false|null)[ \t]*\r?\n[ \t]*(")',
        r"\1,\n\2",
        base,
    )
    missing_comma_fixed = re.sub(r",\s*([}\]])", r"\1", missing_comma_fixed)
    candidates.append((missing_comma_fixed, True))

    for cand, was_repaired in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj, was_repaired
        except json.JSONDecodeError:
            pass

    # String-aware truncation recovery
    cut_points: list[int] = []
    in_str = False
    esc = False
    for i, ch in enumerate(base):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in ("}", "]", ","):
                cut_points.append(i + 1)

    if len(base) not in cut_points:
        cut_points.append(len(base))

    for cut in reversed(cut_points):
        sub = base[:cut].rstrip(" \t\r\n,:")
        if not sub:
            continue
        stack, still_str = balance_brackets(sub)
        if still_str:
            sub = _strip_incomplete_escape(sub) + '"'
            stack, _ = balance_brackets(sub)
        sub = re.sub(r",\s*$", "", sub)
        if stack:
            sub = sub + "".join(reversed(stack))
        sub = re.sub(r",\s*([}\]])", r"\1", sub)
        try:
            obj = json.loads(sub)
            if isinstance(obj, dict):
                return obj, True
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("Unable to parse or repair JSON object", base, 0)


def parse_or_repair_json(raw_text: str) -> dict[str, Any]:
    """Parse or repair JSON text into a dictionary."""
    obj, _ = _parse_or_repair_json_with_flag(raw_text)
    return obj


def to_gemini_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI-style messages list into (system_instruction, gemini_contents)."""
    sys_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    seen_non_system = False

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip().lower()
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if role == "system" and not seen_non_system:
            if text:
                sys_parts.append(text)
            continue
        seen_non_system = True
        if role == "system":
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].append({"text": f"[System Note]\n{text}"})
            else:
                contents.append({"role": "user", "parts": [{"text": f"[System Note]\n{text}"}]})
            continue
        gem_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gem_role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": gem_role, "parts": [{"text": text}]})

    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "(Bắt đầu.)"}]})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Bắt đầu."}]})

    return "\n\n".join(sys_parts).strip(), contents


class BaseLLMClient:
    """Abstract base LLM client with thread-safe cost tracking."""

    def __init__(self) -> None:
        self._cost_lock = threading.Lock()
        self._total_cost: float = 0.0

    def _record_cost(self, cost_usd: float | None) -> float:
        amt = max(0.0, float(cost_usd or 0.0))
        with self._cost_lock:
            self._total_cost += amt
        return amt

    def get_total_cost(self) -> float:
        with self._cost_lock:
            return round(self._total_cost, 8)

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 2500,
        schema_hint: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raise NotImplementedError

    def complete_text(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1500,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


def _normalize_provider(provider: str) -> str:
    p = str(provider or "openrouter").strip().lower()
    if p == "google":
        return "gemini"
    if p in ("openai-compatible", "openai_compatible", "custom"):
        return "openai"
    if p in ("openrouter", "gemini", "vertex", "openai"):
        return p
    raise ValueError(f"Unsupported LLM provider: {provider!r}")


class LiveLLMClient(BaseLLMClient):
    """Non-streaming stdlib HTTP LLM client for OpenRouter, Gemini, Vertex, and OpenAI-compatible APIs."""

    def __init__(
        self,
        provider: str = "openrouter",
        model: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 120.0,
        max_http_retries: int = 1,
        retry_backoff: float = 0.5,
        reasoning: str = "off",
    ) -> None:
        super().__init__()
        self.provider = _normalize_provider(provider)
        self.model = str(model or "").strip() or DEFAULT_MODELS.get(self.provider, "")
        self._explicit_api_key = api_key
        self.base_url = str(base_url or "").strip()
        self.timeout = float(timeout)
        self.max_http_retries = max(0, int(max_http_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.reasoning = str(reasoning or "off").strip().lower()

    def _resolve_api_key(self) -> str:
        if self._explicit_api_key is not None:
            key = str(self._explicit_api_key).strip()
        elif self.provider == "openrouter":
            key = load_env_var("OPENROUTER_API_KEY")
        elif self.provider == "gemini":
            key = load_env_var("GEMINI_API_KEY")
        elif self.provider == "vertex":
            key = load_env_var("VERTEX_API_KEY") or load_env_var("GEMINI_API_KEY")
        else:
            key = load_env_var("OPENAI_API_KEY")

        if self.provider in ("openrouter", "gemini", "vertex") and not key:
            raise LLMError(f"Missing API key for provider '{self.provider}'")
        return key

    def _resolve_url(self, model: str) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if self.provider == "openrouter":
            if not base:
                return DEFAULT_OPENROUTER_URL
            return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

        if self.provider == "openai":
            if not base:
                base = load_env_var("OPENAI_BASE_URL").strip().rstrip("/")
            if not base:
                raise LLMError("Missing base_url for openai-compatible provider")
            return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

        use_model = model or self.model or DEFAULT_MODELS[self.provider]
        if self.provider == "gemini":
            if not base:
                return DEFAULT_GEMINI_URL_TMPL.format(model=use_model)
            if ":generateContent" in base:
                return base
            if base.endswith("/v1beta") or base.endswith("/v1"):
                return f"{base}/models/{use_model}:generateContent"
            return f"{base}/v1beta/models/{use_model}:generateContent"

        # vertex
        if not base:
            return DEFAULT_VERTEX_URL_TMPL.format(model=use_model)
        if ":generateContent" in base:
            return base
        return f"{base}/models/{use_model}:generateContent"

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = self._resolve_url(model)
        if self.provider in ("openrouter", "openai"):
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            if self.provider == "openrouter":
                headers["X-Title"] = "iso_engine"
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
            }
            if self.provider == "openrouter":
                body["usage"] = {"include": True}
                if self.reasoning == "off":
                    body["reasoning"] = {"enabled": False}
            elif self.reasoning == "off":
                body["thinking"] = {"type": "disabled"}
            return url, headers, body

        # gemini / vertex
        sys_inst, contents = to_gemini_messages(messages)
        gen_cfg: dict[str, Any] = {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_tokens),
        }
        if self.reasoning == "off":
            ml = model.lower()
            if "gemini-3" in ml or "gemini-4" in ml:
                gen_cfg["thinkingConfig"] = {"thinkingLevel": "minimal"}
            else:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        body = {
            "contents": contents,
            "generationConfig": gen_cfg,
            "safetySettings": [
                {"category": cat, "threshold": "BLOCK_ONLY_HIGH"}
                for cat in _SAFETY_CATEGORIES
            ],
        }
        if sys_inst:
            body["systemInstruction"] = {"parts": [{"text": sys_inst}]}
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        return url, headers, body

    def _post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(self.max_http_retries + 1):
            req = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    raise LLMHTTPError("Empty response body from LLM endpoint", status_code=getattr(resp, "status", 200))
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LLMHTTPError(f"Non-JSON HTTP payload from LLM endpoint: {exc}") from exc
                if not isinstance(data, dict):
                    raise LLMHTTPError("LLM endpoint returned non-object JSON payload")
                if data.get("error"):
                    err_obj = data["error"]
                    msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                    raise LLMHTTPError(f"Provider error: {msg}")
                return data
            except urllib.error.HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                last_err = LLMHTTPError(f"HTTP {exc.code}: {err_body or exc.reason}", status_code=exc.code)
                if exc.code in _TRANSIENT_HTTP_CODES and attempt < self.max_http_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise last_err from exc
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                last_err = LLMHTTPError(f"Network error calling LLM endpoint: {exc}")
                if attempt < self.max_http_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise last_err from exc

        raise last_err or LLMHTTPError("LLM HTTP request failed")

    def _extract_text_and_meta(self, data: dict[str, Any], model: str) -> tuple[str, dict[str, Any]]:
        if self.provider in ("openrouter", "openai"):
            choices = data.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
            content = msg.get("content")
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and not part.get("thought")
                )
            else:
                text = str(content or "")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            comp_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
            cost = self._record_cost(usage.get("cost"))
            meta = {
                "provider": self.provider,
                "model": str(data.get("model") or model),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "reasoning_tokens": int(comp_details.get("reasoning_tokens") or 0),
                "cost_usd": cost,
            }
            return text, meta

        # gemini / vertex
        candidates = data.get("candidates") or []
        parts = []
        if candidates and isinstance(candidates[0], dict):
            c_obj = candidates[0].get("content") or {}
            if isinstance(c_obj, dict) and isinstance(c_obj.get("parts"), list):
                parts = c_obj["parts"]
        text = "".join(
            str(p.get("text") or "")
            for p in parts
            if isinstance(p, dict) and p.get("text") and not p.get("thought")
        )
        um = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        meta = {
            "provider": self.provider,
            "model": model,
            "prompt_tokens": int(um.get("promptTokenCount") or 0),
            "completion_tokens": int(um.get("candidatesTokenCount") or 0),
            "reasoning_tokens": int(um.get("thoughtsTokenCount") or 0),
            "cost_usd": 0.0,
        }
        return text, meta

    def complete_text(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1500,
    ) -> tuple[str, dict[str, Any]]:
        api_key = self._resolve_api_key()
        use_model = str(model or self.model or DEFAULT_MODELS.get(self.provider, "")).strip()
        url, headers, body = self._build_request(
            messages,
            model=use_model,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
        )
        data = self._post_json(url, headers, body)
        return self._extract_text_and_meta(data, use_model)

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 2500,
        schema_hint: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        json_instr = "Trả về duy nhất 1 JSON object hợp lệ ({...}), không markdown, không giải thích thêm."
        if schema_hint:
            json_instr = f"{json_instr} Schema: {schema_hint}"

        req_msgs = copy.deepcopy(list(messages or []))
        if req_msgs and isinstance(req_msgs[0], dict) and req_msgs[0].get("role") == "system":
            req_msgs[0]["content"] = f"{req_msgs[0].get('content', '')}\n\n{json_instr}".strip()
        else:
            req_msgs.insert(0, {"role": "system", "content": json_instr})

        raw1, meta1 = self.complete_text(
            req_msgs,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            obj, repaired = _parse_or_repair_json_with_flag(raw1)
            meta1["repaired"] = repaired
            meta1["retried"] = False
            return obj, meta1
        except (json.JSONDecodeError, ValueError):
            retry_msgs = list(req_msgs)
            if raw1:
                retry_msgs.append({"role": "assistant", "content": raw1[:800]})
            retry_msgs.append({
                "role": "user",
                "content": (
                    "Phản hồi trước không phải JSON object hợp lệ. "
                    "Hãy trả về DUY NHẤT 1 JSON object {...} hợp lệ ngay."
                    + (f" Schema: {schema_hint}" if schema_hint else "")
                ),
            })
            raw2, meta2 = self.complete_text(
                retry_msgs,
                model=model,
                temperature=0.1,
                max_tokens=max_tokens,
            )
            try:
                obj2, repaired2 = _parse_or_repair_json_with_flag(raw2)
            except (json.JSONDecodeError, ValueError) as exc:
                raise LLMJSONError(f"Failed to parse or repair JSON after retry: {exc}") from exc

            merged_meta = {
                "provider": meta2.get("provider", self.provider),
                "model": meta2.get("model", model or self.model),
                "prompt_tokens": int(meta1.get("prompt_tokens", 0)) + int(meta2.get("prompt_tokens", 0)),
                "completion_tokens": int(meta1.get("completion_tokens", 0)) + int(meta2.get("completion_tokens", 0)),
                "reasoning_tokens": int(meta1.get("reasoning_tokens", 0)) + int(meta2.get("reasoning_tokens", 0)),
                "cost_usd": float(meta1.get("cost_usd", 0.0)) + float(meta2.get("cost_usd", 0.0)),
                "repaired": repaired2,
                "retried": True,
            }
            return obj2, merged_meta


class MockLLMClient(BaseLLMClient):
    """Thread-safe deterministic mock LLM client for automated testing."""

    def __init__(
        self,
        responses: list[Any] | dict[str, Any] | Callable[..., Any] | None = None,
        *,
        default_json: dict[str, Any] | None = None,
        default_text: str = "",
        task_responses: dict[str, Any] | None = None,
        cost_per_call: float = 0.0,
        strict: bool = False,
    ) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.call_count: int = 0
        self.strict: bool = bool(strict)
        self.default_json: dict[str, Any] = copy.deepcopy(default_json) if default_json is not None else {}
        self.default_text: str = str(default_text or "")
        self.task_responses: dict[str, Any] = dict(task_responses or {})
        self.cost_per_call: float = float(cost_per_call)

        if isinstance(responses, list):
            self._queue: list[Any] | None = list(responses)
            self._callable: Callable[..., Any] | None = None
        elif callable(responses):
            self._queue = None
            self._callable = responses
        elif isinstance(responses, dict):
            self._queue = None
            self._callable = None
            self.task_responses.update(responses)
        else:
            self._queue = None
            self._callable = None

    def set_responses(self, responses: list[Any]) -> None:
        with self._lock:
            self._queue = list(responses)

    def _next_payload(
        self,
        kind: str,
        messages: list[dict[str, Any]],
        *,
        model: str | None,
        temperature: float,
        max_tokens: int,
        schema_hint: str,
    ) -> Any:
        with self._lock:
            self.call_count += 1
            self.calls.append({
                "kind": kind,
                "messages": copy.deepcopy(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "schema_hint": schema_hint,
            })
            if self._queue is not None:
                if self._queue:
                    return self._queue.pop(0)
                if self.strict:
                    raise LLMError("MockLLMClient response queue exhausted")

        if self._callable is not None:
            return self._callable(messages, model=model, schema_hint=schema_hint)

        if self.task_responses:
            joined = json.dumps(messages, ensure_ascii=False)
            for key, val in self.task_responses.items():
                if (schema_hint and key in schema_hint) or key in joined:
                    return val(messages, model=model, schema_hint=schema_hint) if callable(val) else val

        if kind == "text":
            if self.default_text:
                return self.default_text
            if self.default_json:
                return json.dumps(self.default_json, ensure_ascii=False)
            return ""
        return copy.deepcopy(self.default_json)

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 2500,
        schema_hint: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = self._next_payload(
            "json",
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            schema_hint=schema_hint,
        )
        repaired = False
        if isinstance(raw, str):
            result, repaired = _parse_or_repair_json_with_flag(raw)
        elif isinstance(raw, dict):
            result = copy.deepcopy(raw)
        else:
            raise LLMJSONError(f"MockLLMClient expected dict or JSON str, got {type(raw).__name__}")

        cost = self._record_cost(self.cost_per_call)
        meta = {
            "provider": "mock",
            "model": model or "mock-model",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": cost,
            "repaired": repaired,
            "retried": False,
        }
        return result, meta

    def complete_text(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1500,
    ) -> tuple[str, dict[str, Any]]:
        raw = self._next_payload(
            "text",
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            schema_hint="",
        )
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        cost = self._record_cost(self.cost_per_call)
        meta = {
            "provider": "mock",
            "model": model or "mock-model",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": cost,
            "repaired": False,
            "retried": False,
        }
        return text, meta


def create_llm_client(
    settings: dict[str, Any] | None = None,
    db_path: str | None = None,
    *,
    role: str = "compile",
) -> BaseLLMClient:
    """Instantiate LiveLLMClient from settings dict or persisted SQLite settings."""
    cfg = dict(settings) if isinstance(settings, dict) else db.get_settings(db_path=db_path)
    provider = str(cfg.get("provider") or "openrouter").strip()
    model_key = "prefetch_model" if role == "prefetch" else "compile_model"
    model = str(cfg.get(model_key) or cfg.get("compile_model") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip() or None
    api_key = cfg.get("api_key")
    return LiveLLMClient(provider=provider, model=model, api_key=api_key, base_url=base_url)
