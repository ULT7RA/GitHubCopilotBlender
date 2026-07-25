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
import tempfile
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import auth as _auth
from . import tool_definitions as _tools
from . import tool_executor as _executor


MAX_REQUEST_PAYLOAD_CHARS = 180_000
MAX_REQUEST_BODY_BYTES = 6_000_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_REQUEST_TEXT_CHARS = 20_000
MAX_REQUEST_TOOL_RESULT_CHARS = 12_000
MAX_REQUEST_TOOL_ARGUMENT_CHARS = 4_000
MAX_STORAGE_TEXT_CHARS = 50_000
MAX_STORAGE_TOOL_RESULT_CHARS = 30_000
MAX_LIVE_TOOL_RESULT_CHARS = 24_000
MAX_INLINE_IMAGE_DATA_CHARS = 5_000_000
MAX_TRANSPORT_ATTEMPTS = 3


def _debug_log(filename: str, message: str, enabled: bool = False):
    if not enabled and os.environ.get("COPILOT_BLENDER_DEBUG") != "1":
        return
    path = os.path.join(
        os.environ.get("TEMP", tempfile.gettempdir()),
        "copilot_blender_ipc",
        filename,
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.1f} {message}\n")
    except OSError:
        pass


def _content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts).strip()
    return ""


def _latest_user_request(messages: list):
    render_prompt = "Here is the render result."
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content", ""))
        if text and not text.startswith(render_prompt):
            return msg, text
    return None, ""


def _inject_render_image(messages: list, image_path: str, user_request: str = ""):
    """Read a rendered image from disk, base64 encode it, and append as a
    user vision message so the model can see and analyze the render."""
    try:
        if not os.path.isfile(image_path):
            print(f"[CopilotAPI] Render image not found: {image_path}")
            return
        with open(image_path, "rb") as f:
            img_data = f.read()
        if len(img_data) > 4_000_000:
            print(f"[CopilotAPI] Render image too large ({len(img_data)} bytes), skipping vision injection")
            return
        b64 = base64.b64encode(img_data).decode("ascii")
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "bmp": "image/bmp", "webp": "image/webp"}.get(ext, "image/png")
        prompt = "Here is the render result. Analyze it and describe what you see."
        if user_request:
            request = user_request[:1200]
            prompt = (
                "Here is the render result for the current Blender request:\n"
                f"{request}\n\nAnalyze it against that request instead of treating the image as a new standalone prompt."
            )
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        })
        print(f"[CopilotAPI] Injected render image for vision analysis ({len(img_data)} bytes)")
    except Exception as e:
        print(f"[CopilotAPI] Failed to inject render image: {e}")

def _tool_call_ids(message: dict) -> list:
    """Return tool call IDs from an assistant message in model-provided order."""
    ids = []
    for tc in message.get("tool_calls") or []:
        if isinstance(tc, dict) and tc.get("id"):
            ids.append(tc["id"])
    return ids


def _has_tool_calls(message: dict) -> bool:
    return message.get("role") == "assistant" and bool(message.get("tool_calls"))


def _make_missing_tool_result(tool_call_id: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": "[tool result unavailable]",
    }


def _normalize_tool_message_sequence(messages: list):
    """Keep assistant tool_calls immediately followed by matching tool results.

    Claude-backed Copilot routes reject any assistant tool_use/tool_calls message
    unless all matching tool_result/tool messages are directly next in the
    conversation. Render vision messages and payload pruning can otherwise leave
    tool results separated from their parent call.
    """
    normalized = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1
            continue

        if _has_tool_calls(msg):
            ids = _tool_call_ids(msg)
            normalized.append(msg)
            i += 1

            results_by_id = {}
            deferred_messages = []
            while i < len(messages):
                next_msg = messages[i]
                if isinstance(next_msg, dict) and _has_tool_calls(next_msg):
                    break

                if isinstance(next_msg, dict) and next_msg.get("role") == "tool":
                    call_id = next_msg.get("tool_call_id")
                    if call_id in ids and call_id not in results_by_id:
                        results_by_id[call_id] = next_msg
                else:
                    deferred_messages.append(next_msg)
                i += 1

            for call_id in ids:
                normalized.append(results_by_id.get(call_id) or _make_missing_tool_result(call_id))
            normalized.extend(deferred_messages)
            continue

        # Orphaned tool messages are invalid without a directly preceding
        # assistant tool_calls block, so drop them before sending.
        if msg.get("role") != "tool":
            normalized.append(msg)
        i += 1

    messages[:] = normalized


def _truncate_text(text: str, max_chars: int, reason: str) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[{reason}; original length {len(text)} chars]"


def _compact_content(content, max_text_chars: int, reason: str, omit_images: bool = False):
    if isinstance(content, str):
        return _truncate_text(content, max_text_chars, reason)

    if isinstance(content, list):
        compact_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if "image_url" in part:
                if omit_images:
                    compact_parts.append({
                        "type": "text",
                        "text": "[image omitted from saved conversation resume]",
                    })
                else:
                    compact_parts.append(part)
                continue
            compact_part = dict(part)
            if isinstance(compact_part.get("text"), str):
                compact_part["text"] = _truncate_text(compact_part["text"], max_text_chars, reason)
            compact_parts.append(compact_part)
        return compact_parts

    return content


def _compact_tool_calls_for_context(msg: dict, max_argument_chars: int):
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        func = tool_call.get("function")
        if not isinstance(func, dict):
            continue
        args = func.get("arguments")
        if isinstance(args, str) and len(args) > max_argument_chars:
            func["arguments"] = json.dumps({
                "_truncated": f"tool arguments omitted from context; original length {len(args)} chars"
            })


def _compact_messages_for_request(messages: list):
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        max_chars = MAX_REQUEST_TOOL_RESULT_CHARS if role == "tool" else MAX_REQUEST_TEXT_CHARS
        msg["content"] = _compact_content(
            msg.get("content", ""),
            max_chars,
            "truncated before API request to fit model context",
            omit_images=False,
        )
        _compact_tool_calls_for_context(msg, MAX_REQUEST_TOOL_ARGUMENT_CHARS)


def _clone_json(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _request_text_size(messages: list) -> int:
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"])
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False))
        total += 100
    return total


def _serialized_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _strip_old_images(messages: list):
    """Keep only the newest inline image and replace older images with text."""
    newest_image_index = -1
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].get("content") if isinstance(messages[index], dict) else None
        has_image = isinstance(content, list) and any(
            isinstance(part, dict) and "image_url" in part for part in content
        )
        if not has_image:
            continue
        if newest_image_index < 0:
            newest_image_index = index
        else:
            messages[index]["content"] = "[Previous image omitted from this request]"


def _message_blocks(messages: list) -> list:
    """Return atomic message ranges; assistant tool calls and results stay together."""
    blocks = []
    index = 0
    while index < len(messages):
        start = index
        msg = messages[index]
        index += 1
        if isinstance(msg, dict) and _has_tool_calls(msg):
            expected = set(_tool_call_ids(msg))
            while index < len(messages):
                next_msg = messages[index]
                if not isinstance(next_msg, dict) or next_msg.get("role") != "tool":
                    break
                if next_msg.get("tool_call_id") not in expected:
                    break
                index += 1
        blocks.append((start, index))
    return blocks


def _latest_primary_user_index(messages: list) -> int:
    render_prompt = "Here is the render result"
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content", ""))
        if text and not text.startswith(render_prompt):
            return index
    return max(0, len(messages) - 1)


def _omit_oversized_images(messages: list):
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        compact_parts = []
        for part in msg["content"]:
            if not isinstance(part, dict) or "image_url" not in part:
                compact_parts.append(part)
                continue
            image_url = part.get("image_url")
            url = image_url.get("url", "") if isinstance(image_url, dict) else ""
            if len(url) > MAX_INLINE_IMAGE_DATA_CHARS:
                compact_parts.append({
                    "type": "text",
                    "text": f"[Image omitted because its encoded payload was {len(url)} characters]",
                })
            else:
                compact_parts.append(part)
        msg["content"] = compact_parts


def _prepare_messages_for_request(messages: list) -> list:
    """Build a bounded request copy without mutating persistent conversation state."""
    try:
        request_messages = _clone_json(messages)
    except (TypeError, ValueError):
        request_messages = []

    request_messages = [
        msg for msg in request_messages
        if isinstance(msg, dict) and msg.get("role") != "system"
    ]
    _normalize_tool_message_sequence(request_messages)
    _compact_messages_for_request(request_messages)
    _strip_old_images(request_messages)
    _omit_oversized_images(request_messages)
    _normalize_tool_message_sequence(request_messages)

    while _request_text_size(request_messages) > MAX_REQUEST_PAYLOAD_CHARS:
        primary_user = _latest_primary_user_index(request_messages)
        removable = None
        for start, end in _message_blocks(request_messages):
            if end <= primary_user:
                removable = (start, end)
                break
        if removable is None:
            break
        del request_messages[removable[0]:removable[1]]

    if _serialized_size(request_messages) > MAX_REQUEST_BODY_BYTES:
        for msg in request_messages:
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                continue
            if any(isinstance(part, dict) and "image_url" in part for part in msg["content"]):
                msg["content"] = "[Image omitted to keep the API request within the transport limit]"
                if _serialized_size(request_messages) <= MAX_REQUEST_BODY_BYTES:
                    break

    if _serialized_size(request_messages) > MAX_REQUEST_BODY_BYTES:
        for msg in request_messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            max_chars = 4_000 if role == "tool" else 8_000
            msg["content"] = _compact_content(
                msg.get("content", ""),
                max_chars,
                "aggressively truncated to fit API transport limit",
                omit_images=True,
            )

    _normalize_tool_message_sequence(request_messages)
    return request_messages


def _bound_tool_result(tool_name: str, result) -> str:
    text = str(result)
    if len(text) <= MAX_LIVE_TOOL_RESULT_CHARS:
        return text

    artifact_dir = os.path.join(tempfile.gettempdir(), "copilot_blender_tool_results")
    os.makedirs(artifact_dir, exist_ok=True)
    artifact_path = os.path.join(
        artifact_dir,
        f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{tool_name}.txt",
    )
    try:
        with open(artifact_path, "w", encoding="utf-8", errors="replace") as artifact:
            artifact.write(text)
        artifact_note = f" Full output saved to: {artifact_path}"
    except OSError as e:
        artifact_note = f" Full output could not be saved: {e}"

    head_chars = MAX_LIVE_TOOL_RESULT_CHARS - 5_000
    tail_chars = 4_000
    return (
        text[:head_chars]
        + f"\n\n...[{tool_name} output truncated; original length {len(text)} chars."
        + artifact_note
        + "]...\n\n"
        + text[-tail_chars:]
    )


def prepare_messages_for_storage(messages: list) -> list:
    """Return a compact, valid copy of messages safe to save for resume."""
    try:
        stored = json.loads(json.dumps(messages, ensure_ascii=False))
    except (TypeError, ValueError):
        return []

    cleaned = []
    for msg in stored:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            continue
        cleaned.append(msg)
    stored = cleaned

    for msg in stored:
        if not isinstance(msg, dict):
            continue
        msg["content"] = _compact_content(
            msg.get("content", ""),
            MAX_STORAGE_TEXT_CHARS,
            "truncated for resume",
            omit_images=True,
        )
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            content = msg["content"]
            msg["content"] = _truncate_text(content, MAX_STORAGE_TOOL_RESULT_CHARS, "tool output truncated for resume")
        _compact_tool_calls_for_context(msg, MAX_REQUEST_TOOL_ARGUMENT_CHARS)

    _normalize_tool_message_sequence(stored)
    return stored


# ── Shared request headers ────────────────────────────────────────────────

def _build_headers(copilot_token: str) -> dict:
    return {
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.100.2",
        "Editor-Plugin-Version": "copilot-chat/0.27.1",
        "User-Agent": "GitHubCopilotChat/0.43.2026040602",
        "OpenAI-Intent": "conversation-panel",
        "X-GitHub-Api-Version": "2025-04-01",
        "X-Initiator": "user",
        "X-Request-Id": str(uuid.uuid4()),
    }


# ── Model catalog ────────────────────────────────────────────────────────

def _model_supported_endpoints(model: dict) -> list:
    endpoints = model.get("supported_endpoints") or model.get("endpoints") or []
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    if not isinstance(endpoints, list):
        endpoints = []
    endpoints = [str(endpoint or "").strip() for endpoint in endpoints if str(endpoint or "").strip()]
    return endpoints or ["/chat/completions"]


def _choose_model_endpoint(endpoints: list) -> str:
    if "/chat/completions" in endpoints:
        return "/chat/completions"
    return endpoints[0] if endpoints else "/chat/completions"


def fetch_models(api_base: str, copilot_token: str) -> list:
    """
    GET {api_base}/models → list of model dicts.
    Each dict: {id, display_name, vendor, category, supports_tools, supports_vision,
                context_tokens, output_tokens, is_default, endpoint, multiplier}
    """
    url = f"{api_base.rstrip('/')}/models"
    retryable_http = {408, 429, 500, 502, 503, 504}
    last_error = ""
    data = None
    for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
        req = Request(url, headers=_build_headers(copilot_token), method="GET")
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"Model catalog exceeded {MAX_RESPONSE_BYTES} bytes")
            data = json.loads(raw.decode("utf-8"))
            break
        except HTTPError as e:
            body = e.read(100_000).decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code}: {body}"
            if e.code not in retryable_http or attempt >= MAX_TRANSPORT_ATTEMPTS:
                raise RuntimeError(f"Model fetch failed: {last_error}") from e
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as e:
            last_error = str(e)
            if attempt >= MAX_TRANSPORT_ATTEMPTS:
                raise RuntimeError(
                    f"Model fetch failed after {MAX_TRANSPORT_ATTEMPTS} attempts: {last_error}"
                ) from e
        time.sleep(min(4.0, 2.0 ** (attempt - 1)))

    if isinstance(data, list):
        raw_models = data
    elif isinstance(data, dict):
        raw_models = data.get("data") or data.get("models") or []
    else:
        raise RuntimeError(f"Model fetch returned an unexpected {type(data).__name__} response")

    models = []
    seen_ids = set()
    for m in raw_models:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        model_id = str(m["id"])
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)

        caps = m.get("capabilities", {})
        supports = caps.get("supports", {})
        limits = caps.get("limits", {})
        endpoints = _model_supported_endpoints(m)
        supports_tools = supports.get("tool_calls", caps.get("tool_calls"))
        if supports_tools is None:
            supports_tools = True
        supports_vision = supports.get("vision", caps.get("vision", False))
        reasoning_efforts = supports.get("reasoning_effort", [])
        if isinstance(reasoning_efforts, str):
            reasoning_efforts = [reasoning_efforts]
        if not isinstance(reasoning_efforts, list):
            reasoning_efforts = []

        models.append({
            "id": model_id,
            "display_name": m.get("name", model_id),
            "vendor": m.get("vendor", ""),
            "category": m.get("model_picker_category", ""),
            "supports_tools": bool(supports_tools),
            "supports_vision": bool(supports_vision),
            "context_tokens": limits.get("max_context_window_tokens", m.get("max_context_window_tokens", 0)),
            "output_tokens": limits.get("max_output_tokens", m.get("max_output_tokens", 0)),
            "is_default": m.get("is_chat_default", False),
            "endpoint": _choose_model_endpoint(endpoints),
            "supported_endpoints": endpoints,
            "reasoning_efforts": [str(value) for value in reasoning_efforts if value],
            "supports_reasoning": bool(reasoning_efforts),
            "multiplier": m.get("billing", {}).get("multiplier", 0),
        })

    return models

# ── Chat completions ─────────────────────────────────────────────────────

_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def _normalize_reasoning_effort(value: str) -> str:
    value = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": "",
        "default": "",
        "auto": "",
        "model-default": "",
        "min": "minimal",
        "minimum": "minimal",
        "extra-high": "xhigh",
        "maximum": "max",
    }
    value = aliases.get(value, value)
    return value if value in _REASONING_EFFORTS else ""


def _preview_value(value, max_chars: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    text = " ".join(str(text).split())
    if len(text) > max_chars:
        return text[:max_chars - 3] + "..."
    return text


def _build_endpoint_url(api_base: str, endpoint: str) -> str:
    endpoint = str(endpoint or "/chat/completions").strip() or "/chat/completions"
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return f"{api_base.rstrip('/')}/{endpoint.lstrip('/')}"


def _uses_responses_protocol(endpoint: str) -> bool:
    endpoint_path = str(endpoint or "").split("?", 1)[0].rstrip("/").lower()
    return endpoint_path.endswith("/responses")


def _responses_message_content(content, role: str):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append({
                "type": "input_text" if role == "user" else "output_text",
                "text": text,
            })
            continue
        image_url = part.get("image_url")
        if role == "user" and isinstance(image_url, dict) and image_url.get("url"):
            parts.append({
                "type": "input_image",
                "image_url": image_url["url"],
            })
    return parts


def _messages_to_responses_input(messages: list) -> list:
    """Convert chat-completions history to OpenAI Responses API input items."""
    response_input = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in ("user", "assistant"):
            content = msg.get("content")
            if content not in (None, "", []):
                response_input.append({
                    "role": role,
                    "content": _responses_message_content(content, role),
                })
            if role == "assistant":
                for tool_call in msg.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    func = tool_call.get("function")
                    if not isinstance(func, dict) or not func.get("name"):
                        continue
                    arguments = func.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    response_input.append({
                        "type": "function_call",
                        "call_id": tool_call.get("id", ""),
                        "name": func["name"],
                        "arguments": arguments,
                    })
        elif role == "tool":
            response_input.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": str(msg.get("content", "")),
            })
    return response_input


def _tools_to_responses_format(tool_defs: list) -> list:
    tools = []
    for tool in tool_defs:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function")
        if tool.get("type") != "function" or not isinstance(func, dict) or not func.get("name"):
            continue
        converted = {
            "type": "function",
            "name": func["name"],
            "parameters": func.get("parameters") or {"type": "object", "properties": {}},
        }
        if func.get("description"):
            converted["description"] = func["description"]
        tools.append(converted)
    return tools


def _parse_responses_output(data: dict):
    content_parts = []
    tool_calls = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    content_parts.append(text)
        elif item_type == "function_call":
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": arguments,
                },
            })

    if not content_parts and isinstance(data.get("output_text"), str) and data["output_text"]:
        content_parts.append(data["output_text"])
    return "\n".join(content_parts), tool_calls


def send_chat(
    api_base: str,
    copilot_token: str,
    model_id: str,
    messages: list,
    enable_tools: bool = True,
    endpoint: str = "/chat/completions",
    timeout: int = 600,
    max_output_tokens: int = 16384,
    reasoning_effort: str = "",
    on_tool_call=None,
    on_agent_event=None,
    cancel_event=None,
    verbose: bool = False,
    max_iterations: int = 40,
) -> dict:
    """
    Blocking chat completion with automatic tool-call loop.
    Returns {"content": str, "model": str, "usage": dict, "error": str|None,
             "tool_log": list[str], "messages": list|None}

    on_tool_call(tool_name, tool_args, tool_result) — optional progress callback.
    """
    url = _build_endpoint_url(api_base, endpoint)
    uses_responses = _uses_responses_protocol(endpoint) or _uses_responses_protocol(url)
    headers = _build_headers(copilot_token)

    tool_defs = _tools.get_blender_tool_definitions() if enable_tools else []
    tool_log = []
    iteration = 0
    max_iter = max(1, int(max_iterations or 40))
    previous_tool_batch = None
    repeated_batch_count = 0
    last_model_content = ""

    def _is_cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    def _emit_agent_event(event_type: str, **data):
        if not on_agent_event:
            return
        event = {"type": event_type}
        event.update(data)
        try:
            on_agent_event(event)
        except Exception as e:
            if verbose:
                print(f"[CopilotAPI] Agent event callback failed: {e}")

    messages[:] = [
        msg for msg in messages
        if isinstance(msg, dict) and msg.get("role") != "system"
    ]
    _normalize_tool_message_sequence(messages)
    _, current_user_text = _latest_user_request(messages)

    def _result_with_error(error_text: str, model=None, usage=None):
        return {
            "content": last_model_content,
            "model": model or model_id,
            "usage": usage or {},
            "error": error_text,
            "tool_log": tool_log,
            "messages": prepare_messages_for_storage(messages),
        }

    def _request_json(payload: bytes):
        retryable_http = {408, 429, 500, 502, 503, 504}
        last_error = None
        for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
            if _is_cancelled():
                return None, "Request cancelled"

            attempt_headers = dict(headers)
            attempt_headers["X-Request-Id"] = str(uuid.uuid4())
            req = Request(url, data=payload, headers=attempt_headers, method="POST")
            try:
                with urlopen(req, timeout=max(30, int(timeout or 600))) as resp:
                    raw = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return None, f"API response exceeded {MAX_RESPONSE_BYTES} bytes"
                return json.loads(raw.decode("utf-8")), None
            except HTTPError as e:
                err_body = e.read(200_000).decode("utf-8", errors="replace")
                last_error = f"HTTP {e.code}: {err_body}"
                if e.code not in retryable_http or attempt >= MAX_TRANSPORT_ATTEMPTS:
                    return None, last_error
                retry_after = e.headers.get("Retry-After", "") if e.headers else ""
                try:
                    delay = min(30.0, max(1.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = min(8.0, 2.0 ** (attempt - 1))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last_error = str(e)
                if attempt >= MAX_TRANSPORT_ATTEMPTS:
                    step = iteration + 1
                    return None, (
                        f"Request failed after {MAX_TRANSPORT_ATTEMPTS} attempts "
                        f"during agent step {step}: {last_error}"
                    )
                delay = min(8.0, 2.0 ** (attempt - 1))

            _emit_agent_event(
                "transport_retry",
                iteration=iteration + 1,
                attempt=attempt + 1,
                max_attempts=MAX_TRANSPORT_ATTEMPTS,
                delay_seconds=delay,
                error=_preview_value(last_error, 300),
            )
            if verbose:
                print(
                    f"[CopilotAPI] Request attempt {attempt} failed; "
                    f"retrying in {delay:.1f}s: {last_error}"
                )
            end_time = time.time() + delay
            while time.time() < end_time:
                if _is_cancelled():
                    return None, "Request cancelled"
                time.sleep(0.1)

        return None, last_error or "Request failed"


    while True:
        if _is_cancelled():
            return _result_with_error("Request cancelled")

        request_messages = _prepare_messages_for_request(messages)

        normalized_reasoning = _normalize_reasoning_effort(reasoning_effort)
        if uses_responses:
            body = {
                "model": model_id,
                "input": _messages_to_responses_input(request_messages),
                "max_output_tokens": max_output_tokens,
                "stream": False,
            }
            if normalized_reasoning:
                body["reasoning"] = {"effort": normalized_reasoning}
            if tool_defs and enable_tools:
                body["tools"] = _tools_to_responses_format(tool_defs)
                body["tool_choice"] = "auto"
        else:
            body = {
                "model": model_id,
                "messages": request_messages,
                "temperature": 0.1,
                "top_p": 1,
                "max_tokens": max_output_tokens,
            }
            if normalized_reasoning:
                body["reasoning_effort"] = normalized_reasoning
            if tool_defs and enable_tools:
                body["tools"] = tool_defs
                body["tool_choice"] = "auto"

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_REQUEST_BODY_BYTES:
            return _result_with_error(
                f"Prepared API request is {len(payload)} bytes, exceeding the "
                f"{MAX_REQUEST_BODY_BYTES}-byte transport limit"
            )

        _debug_log(
            "debug_thread.log",
            f"payload={len(payload)} request_msgs={len(request_messages)} "
            f"stored_msgs={len(messages)} iter={iteration}",
            verbose,
        )

        if verbose:
            print(
                f"[CopilotAPI] POST {url} model={model_id} iter={iteration} "
                f"msgs={len(request_messages)} bytes={len(payload)}"
            )

        data, request_error = _request_json(payload)
        if request_error:
            return _result_with_error(request_error)
        if not isinstance(data, dict):
            return _result_with_error(
                f"Copilot API returned an invalid {type(data).__name__} response"
            )
        if data.get("error"):
            return _result_with_error(
                f"Copilot API error: {_preview_value(data.get('error'), 1000)}"
            )

        # Parse response
        api_model = data.get("model", model_id)
        usage = data.get("usage", {})
        if uses_responses:
            combined_content, all_tool_calls = _parse_responses_output(data)
            response_has_output = bool(data.get("output"))
        else:
            choices = data.get("choices", [])
            if not isinstance(choices, list):
                return _result_with_error(
                    f"Copilot API returned an invalid choices payload ({type(choices).__name__})",
                    model=api_model,
                    usage=usage,
                )

            # Collect content and tool calls across all choices.
            content_parts = []
            all_tool_calls = []
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                msg = choice.get("message", {})
                if not isinstance(msg, dict):
                    continue
                response_content = msg.get("content")
                if isinstance(response_content, str) and response_content:
                    content_parts.append(response_content)
                elif isinstance(response_content, list):
                    response_text = _content_text(response_content)
                    if response_text:
                        content_parts.append(response_text)
                if isinstance(msg.get("tool_calls"), list):
                    all_tool_calls.extend(msg["tool_calls"])
            combined_content = "\n".join(content_parts)
            response_has_output = bool(choices)

        all_tool_calls = [tc for tc in all_tool_calls if isinstance(tc, dict)]
        if combined_content:
            last_model_content = combined_content

        if not all_tool_calls or not enable_tools:
            if combined_content:
                messages.append({"role": "assistant", "content": combined_content})
            _normalize_tool_message_sequence(messages)
            if not combined_content:
                status = str(data.get("status", "") or "")
                details = data.get("incomplete_details")
                if status and status != "completed":
                    return _result_with_error(
                        f"Copilot response ended with status '{status}': "
                        f"{_preview_value(details, 500)}",
                        model=api_model,
                        usage=usage,
                    )
            if not combined_content and not response_has_output:
                return _result_with_error(
                    "Copilot API returned no response output",
                    model=api_model,
                    usage=usage,
                )
            return {
                "content": combined_content,
                "model": api_model,
                "usage": usage,
                "error": None,
                "tool_log": tool_log,
                "messages": prepare_messages_for_storage(messages),
            }

        # ── Execute tool calls ────────────────────────────────────────
        _emit_agent_event(
            "tool_batch",
            iteration=iteration + 1,
            tool_count=len(all_tool_calls),
            content_preview=_preview_value(combined_content, 500) if combined_content else "",
        )

        # Append assistant message with tool_calls to conversation
        assistant_msg = {"role": "assistant"}
        if combined_content:
            assistant_msg["content"] = combined_content
        assistant_msg["tool_calls"] = all_tool_calls
        messages.append(assistant_msg)

        batch_signatures = []
        for tc in all_tool_calls:
            func = tc.get("function") if isinstance(tc, dict) else {}
            func = func if isinstance(func, dict) else {}
            raw_arguments = func.get("arguments", "{}")
            if isinstance(raw_arguments, str):
                try:
                    signature_args = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    signature_args = raw_arguments
            else:
                signature_args = raw_arguments
            batch_signatures.append(
                f"{func.get('name', '')}:"
                f"{json.dumps(signature_args, sort_keys=True, ensure_ascii=False)}"
            )
        batch_signature = tuple(batch_signatures)
        if batch_signature and batch_signature == previous_tool_batch:
            repeated_batch_count += 1
        else:
            repeated_batch_count = 1
        previous_tool_batch = batch_signature
        suppress_repeated_batch = repeated_batch_count > 2

        render_image_paths = []
        repeated_tool_call = False
        for tool_index, tc in enumerate(all_tool_calls):
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id") or f"blender_call_{iteration + 1}_{tool_index + 1}"
            tc["id"] = tc_id
            func = tc.get("function", {})
            if not isinstance(func, dict):
                func = {}
            tool_name = func.get("name", "")
            raw_arguments = func.get("arguments", "{}")
            if isinstance(raw_arguments, dict):
                tool_args = raw_arguments
            else:
                try:
                    tool_args = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError):
                    tool_args = {}
            if not isinstance(tool_args, dict):
                tool_args = {}

            if verbose:
                print(f"[CopilotAPI] Tool call: {tool_name}({json.dumps(tool_args)[:200]})")

            if _is_cancelled():
                return _result_with_error("Request cancelled", model=api_model, usage=usage)

            _emit_agent_event(
                "tool_start",
                iteration=iteration + 1,
                tool_name=tool_name,
                args_preview=_preview_value(tool_args, 500),
            )

            if suppress_repeated_batch:
                repeated_tool_call = True
                result = (
                    "Repeated identical tool batch suppressed after two executions. "
                    "Use the existing results and provide a final answer."
                )
            else:
                result = _executor.execute_tool(
                    tool_name,
                    tool_args,
                    cancel_event=cancel_event,
                )

            tool_result_str = _bound_tool_result(tool_name, result)
            log_entry = (
                f"[{tool_name}] {json.dumps(tool_args, ensure_ascii=False)[:100]} "
                f"→ {tool_result_str[:200]}"
            )
            tool_log.append(log_entry)

            _emit_agent_event(
                "tool_result",
                iteration=iteration + 1,
                tool_name=tool_name,
                result_preview=_preview_value(tool_result_str, 700),
            )

            if on_tool_call:
                on_tool_call(tool_name, tool_args, tool_result_str)

            # Append tool result
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
                render_image_paths.append(image_path)

        for image_path in render_image_paths:
            _inject_render_image(messages, image_path, current_user_text)

        iteration += 1

        if repeated_tool_call:
            _emit_agent_event(
                "repeat_guard",
                iteration=iteration,
                message="Repeated identical tool batch suppressed; requesting final answer",
            )
            enable_tools = False

        if iteration >= max_iter:
            _emit_agent_event(
                "iteration_cap",
                iteration=iteration,
                max_iterations=max_iter,
            )
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
_cancel_events = {}
_result_lock = threading.Lock()
_result_counter = 0


def send_chat_async(
    api_base, copilot_token, model_id, messages,
    enable_tools=True, endpoint="/chat/completions", timeout=600, max_output_tokens=16384,
    reasoning_effort="",
    on_tool_call=None, on_agent_event=None, on_done=None, verbose=False,
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
        cancel_event = threading.Event()
        _pending_results[rid] = {"status": "pending", "result": None}
        _cancel_events[rid] = cancel_event

    def _emit_agent_event(event):
        if not on_agent_event:
            return
        event = dict(event or {})
        event["request_id"] = rid
        on_agent_event(event)

    def _run():
        def _log(msg):
            _debug_log("debug_thread.log", f"[{rid}] {msg}", verbose)
        _log(f"Thread started. model={model_id} msgs={len(messages)} tools={enable_tools}")
        try:
            result = send_chat(
                api_base, copilot_token, model_id, messages,
                enable_tools=enable_tools, endpoint=endpoint, timeout=timeout,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                on_tool_call=on_tool_call,
                on_agent_event=_emit_agent_event,
                cancel_event=cancel_event,
                verbose=verbose,
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
                "messages": prepare_messages_for_storage(messages),
            }
        with _result_lock:
            still_pending = rid in _pending_results
            if still_pending:
                _pending_results[rid] = {"status": "completing", "result": result}
        if on_done and still_pending:
            try:
                on_done(rid, result)
            except Exception as callback_error:
                _log(f"on_done callback failed: {callback_error}\n{traceback.format_exc()}")
        with _result_lock:
            if rid in _pending_results:
                _pending_results[rid] = {"status": "done", "result": result}
            _cancel_events.pop(rid, None)
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
    """Legacy helper: cancel and immediately discard a request."""
    with _result_lock:
        cancel_event = _cancel_events.get(request_id)
        if cancel_event:
            cancel_event.set()
        _pending_results.pop(request_id, None)
        _cancel_events.pop(request_id, None)


def cancel_chat_request(request_id: int) -> bool:
    """Request cancellation while keeping state until the worker returns."""
    with _result_lock:
        cancel_event = _cancel_events.get(request_id)
        if not cancel_event:
            return False
        cancel_event.set()
        return True


def discard_chat_result(request_id: int):
    """Remove a completed request after the UI has consumed and persisted it."""
    with _result_lock:
        _pending_results.pop(request_id, None)
        _cancel_events.pop(request_id, None)
