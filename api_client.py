"""
Copilot API client — chat completions, model catalog, tool-call loop.
All HTTP is done via stdlib urllib (no external dependencies).
"""

import json
import os
import time
import traceback
import uuid
import base64
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import auth as _auth
from . import tool_definitions as _tools
from . import tool_executor as _executor


def _inject_render_image(messages: list, image_path: str):
    """Read a rendered image from disk, base64 encode it, and append as a
    user vision message so the model can see and analyze the render."""
    try:
        if not os.path.isfile(image_path):
            print(f"[CopilotAPI] Render image not found: {image_path}")
            return
        with open(image_path, "rb") as f:
            img_data = f.read()
        if len(img_data) > 20_000_000:  # 20MB safety cap
            print(f"[CopilotAPI] Render image too large ({len(img_data)} bytes), skipping vision injection")
            return
        b64 = base64.b64encode(img_data).decode("ascii")
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "bmp": "image/bmp", "webp": "image/webp"}.get(ext, "image/png")
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Here is the render result. Analyze it and describe what you see."},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        })
        print(f"[CopilotAPI] Injected render image for vision analysis ({len(img_data)} bytes)")
    except Exception as e:
        print(f"[CopilotAPI] Failed to inject render image: {e}")

# ── Shared request headers ────────────────────────────────────────────────

def _build_headers(copilot_token: str) -> dict:
    return {
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.96.0",
        "Editor-Plugin-Version": "copilot-chat/0.24.2",
        "User-Agent": "GitHubCopilotChat/0.43.2026040602",
        "OpenAI-Intent": "conversation-panel",
        "X-GitHub-Api-Version": "2025-04-01",
        "X-Initiator": "user",
        "X-Request-Id": str(uuid.uuid4()),
    }


# ── Model catalog ────────────────────────────────────────────────────────

def fetch_models(api_base: str, copilot_token: str) -> list:
    """
    GET {api_base}/models → list of model dicts.
    Each dict: {id, display_name, vendor, category, supports_tools, supports_vision,
                context_tokens, output_tokens, is_default, endpoint, multiplier}
    """
    url = f"{api_base}/models"
    req = Request(url, headers=_build_headers(copilot_token), method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError) as e:
        print(f"[CopilotAPI] Model fetch failed: {e}")
        return []

    models = []
    for m in data.get("data", []):
        if not m.get("model_picker_enabled", True):
            continue
        policy = m.get("policy", {})
        if policy.get("state") not in (None, "enabled"):
            continue

        caps = m.get("capabilities", {})
        supports = caps.get("supports", {})
        limits = caps.get("limits", {})

        models.append({
            "id": m["id"],
            "display_name": m.get("name", m["id"]),
            "vendor": m.get("vendor", ""),
            "category": m.get("model_picker_category", ""),
            "supports_tools": supports.get("tool_calls", False),
            "supports_vision": supports.get("vision", False),
            "context_tokens": limits.get("max_context_window_tokens", 0),
            "output_tokens": limits.get("max_output_tokens", 0),
            "is_default": m.get("is_chat_default", False),
            "endpoint": (
                "/chat/completions"
                if "/chat/completions" in m.get("supported_endpoints", ["/chat/completions"])
                else m.get("supported_endpoints", ["/chat/completions"])[0]
            ),
            "multiplier": m.get("billing", {}).get("multiplier", 0),
        })

    return models


# ── System prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are inside Blender. This is a persistent multi-turn conversation. "
    "You have tools available. Use them proactively instead of asking the user to check things. "
    "STRICT FORMATTING RULES — NEVER BREAK THESE: "
    "Never use markdown. No headers, no bold, no code fences, no bullet points. "
    "Never use emojis. Not a single one. "
    "Write in plain conversational English like a coworker talking to you. "
    "Just talk normally. Explain things in sentences and paragraphs."
)


# ── Chat completions ─────────────────────────────────────────────────────

def send_chat(
    api_base: str,
    copilot_token: str,
    model_id: str,
    messages: list,
    enable_tools: bool = True,
    timeout: int = 600,
    max_output_tokens: int = 16384,
    on_tool_call=None,
    verbose: bool = False,
    max_iterations: int = 25,
) -> dict:
    """
    Blocking chat completion with automatic tool-call loop.
    Returns {"content": str, "model": str, "usage": dict, "error": str|None,
             "tool_log": list[str]}

    on_tool_call(tool_name, tool_args, tool_result) — optional progress callback.
    """
    url = f"{api_base}/chat/completions"
    headers = _build_headers(copilot_token)

    tool_defs = _tools.get_blender_tool_definitions() if enable_tools else []
    tool_log = []
    iteration = 0
    max_iter = max_iterations if max_iterations > 0 else 25

    # ── Strip old render images to prevent payload bloat ──
    # Keep only the most recent render image in the conversation
    last_render_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        c = messages[i].get("content")
        if isinstance(c, list) and any(
            isinstance(p, dict) and "image_url" in p for p in c
        ):
            if last_render_idx == -1:
                last_render_idx = i  # keep this one
            else:
                # Replace old render with a text-only placeholder
                messages[i] = {
                    "role": messages[i]["role"],
                    "content": "[Previous render image removed to save space]",
                }

    # ── Conversation trimming ──
    MAX_PAYLOAD_CHARS = 800_000
    MIN_MSGS_KEEP = 6

    def _estimate_size(msgs):
        total = 0
        for m in msgs:
            c = m.get("content", "")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        total += len(part.get("text", ""))
                        total += len(part.get("url", ""))
            total += 100  # JSON overhead
        return total

    while True:
        # Trim old messages if payload is too large
        while (_estimate_size(messages) > MAX_PAYLOAD_CHARS
               and len(messages) > MIN_MSGS_KEEP):
            messages.pop(1)  # Remove oldest non-system message

        body = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.1,
            "top_p": 1,
            "max_tokens": max_output_tokens,
        }
        if tool_defs and enable_tools:
            body["tools"] = tool_defs
            body["tool_choice"] = "auto"

        payload = json.dumps(body).encode("utf-8")
        req = Request(url, data=payload, headers=headers, method="POST")

        if verbose:
            print(f"[CopilotAPI] POST {url} model={model_id} iter={iteration} msgs={len(messages)}")

        try:
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            return {
                "content": "",
                "model": model_id,
                "usage": {},
                "error": f"HTTP {e.code}: {err_body}",
                "tool_log": tool_log,
            }
        except (URLError, TimeoutError, OSError) as e:
            # Auto-retry once on transport failure
            if iteration == 0:
                if verbose:
                    print(f"[CopilotAPI] Transport error, retrying: {e}")
                time.sleep(2)
                try:
                    with urlopen(req, timeout=timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                except Exception as e2:
                    return {
                        "content": "",
                        "model": model_id,
                        "usage": {},
                        "error": f"Request failed after retry: {e2}",
                        "tool_log": tool_log,
                    }
            else:
                return {
                    "content": "",
                    "model": model_id,
                    "usage": {},
                    "error": f"Request failed: {e}",
                    "tool_log": tool_log,
                }

        # Parse response
        api_model = data.get("model", model_id)
        usage = data.get("usage", {})
        choices = data.get("choices", [])

        # Collect content and tool_calls across all choices (Claude multi-choice)
        content_parts = []
        all_tool_calls = []
        finish_reason = "stop"

        for choice in choices:
            msg = choice.get("message", {})
            if msg.get("content"):
                content_parts.append(msg["content"])
            if msg.get("tool_calls"):
                all_tool_calls.extend(msg["tool_calls"])
            fr = choice.get("finish_reason", "")
            if fr == "tool_calls":
                finish_reason = "tool_calls"

        combined_content = "\n".join(content_parts)

        if not all_tool_calls or not enable_tools:
            return {
                "content": combined_content,
                "model": api_model,
                "usage": usage,
                "error": None,
                "tool_log": tool_log,
            }

        # ── Execute tool calls ────────────────────────────────────────
        # Append assistant message with tool_calls to conversation
        assistant_msg = {"role": "assistant"}
        if combined_content:
            assistant_msg["content"] = combined_content
        assistant_msg["tool_calls"] = all_tool_calls
        messages.append(assistant_msg)

        for tc in all_tool_calls:
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                tool_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}

            if verbose:
                print(f"[CopilotAPI] Tool call: {tool_name}({json.dumps(tool_args)[:200]})")

            # Execute
            result = _executor.execute_tool(tool_name, tool_args)
            log_entry = f"[{tool_name}] {json.dumps(tool_args)[:100]} → {str(result)[:200]}"
            tool_log.append(log_entry)

            if on_tool_call:
                on_tool_call(tool_name, tool_args, result)

            # Append tool result
            tool_result_str = str(result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": tool_name,
                "content": tool_result_str,
            })

            # If a tool produced a render image, inject it as a vision message
            # so the model can see and analyze what it rendered
            if tool_result_str.startswith("__RENDER_IMAGE__:"):
                image_path = tool_result_str.split("\n")[0].replace("__RENDER_IMAGE__:", "").strip()
                _inject_render_image(messages, image_path)

        iteration += 1

        # Check iteration cap (0 = unlimited)
        if max_iter > 0 and iteration >= max_iter:
            # Force final answer
            messages.append({
                "role": "system",
                "content": "Tool-call iteration limit reached. Provide your final answer now without calling any more tools.",
            })
            enable_tools = False
            continue

    # Should not reach here
    return {
        "content": combined_content,
        "model": api_model,
        "usage": usage,
        "error": None,
        "tool_log": tool_log,
    }


# ── Threaded wrapper for non-blocking chat ────────────────────────────────

_pending_results = {}
_result_lock = threading.Lock()
_result_counter = 0


def send_chat_async(
    api_base, copilot_token, model_id, messages,
    enable_tools=True, timeout=600, max_output_tokens=16384,
    on_tool_call=None, verbose=False,
    max_iterations=0,
) -> int:
    """
    Start a chat completion in a background thread.
    Returns a request_id. Poll with get_chat_result(request_id).
    """
    global _result_counter
    with _result_lock:
        _result_counter += 1
        rid = _result_counter
        _pending_results[rid] = {"status": "pending", "result": None}

    def _run():
        _dbg = os.path.join(os.environ.get("TEMP", "/tmp"), "copilot_blender_ipc", "debug_thread.log")
        def _log(msg):
            with open(_dbg, "a") as _f:
                _f.write(f"{time.time():.1f} [{rid}] {msg}\n")
                _f.flush()
        _log(f"Thread started. model={model_id} msgs={len(messages)} tools={enable_tools}")
        try:
            result = send_chat(
                api_base, copilot_token, model_id, messages,
                enable_tools=enable_tools, timeout=timeout,
                max_output_tokens=max_output_tokens,
                on_tool_call=on_tool_call, verbose=verbose,
                max_iterations=max_iterations,
            )
            _log(f"send_chat returned: error={result.get('error')} content_len={len(result.get('content',''))}")
        except Exception as e:
            _log(f"EXCEPTION: {e}\n{traceback.format_exc()}")
            result = {
                "content": "",
                "model": model_id,
                "usage": {},
                "error": f"Unhandled exception in chat thread: {e}",
                "tool_log": [traceback.format_exc()],
            }
        with _result_lock:
            _pending_results[rid] = {"status": "done", "result": result}
        _log(f"Result stored. status=done")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return rid


def get_chat_result(request_id: int) -> dict:
    """
    Check if a background chat is done.
    Returns {"status": "pending"|"done", "result": dict|None}
    """
    with _result_lock:
        return _pending_results.get(request_id, {"status": "unknown", "result": None})


def clear_chat_result(request_id: int):
    with _result_lock:
        _pending_results.pop(request_id, None)
