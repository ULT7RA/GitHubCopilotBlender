"""
Blender operators for the GitHub Copilot addon.
Handles: auth, chat, model refresh, uploads, actions, and the async modal timer.
"""

import json
import os
import sys
import time
import webbrowser
import base64
import mimetypes
import threading

import bpy
from bpy.props import StringProperty, BoolProperty, IntProperty, EnumProperty
from bpy.types import Operator

from . import auth as _auth
from . import api_client as _api
from . import tool_definitions as _tool_defs
from . import tool_executor as _executor
from .preferences import get_prefs


# ── Helpers ───────────────────────────────────────────────────────────────

def _get_cp(context):
    """Return the scene copilot properties."""
    return context.scene.copilot


def _add_chat(cp, role, content, model_id="", emit_ipc=True):
    msg = cp.chat_history.add()
    msg.role = role
    msg.content = content
    msg.model_id = model_id
    msg.timestamp = time.time()
    print(f"[CopilotIPC] _add_chat role={role} len={len(content)} model={model_id}")
    # Auto-save chat history to disk after every message
    _auth.save_chat_history(cp.chat_history)

    # Print to the IPC console so you can actually read it.
    if emit_ipc:
        _print_to_console(role, content, model_id)

    # Auto-refresh the pop-out chat text display
    try:
        from . import panels as _panels
        _panels._refresh_chat_text(bpy.context)
    except Exception:
        pass


def _print_to_console(role, content, model_id=""):
    """Print chat messages to the dedicated console via IPC."""
    # Also write to the IPC response file for the console to display
    if role == "assistant":
        _write_ipc_response({
            "content": content,
            "model": model_id,
            "error": None,
            "tool_log": [],
        })
    elif role == "system" and "Error:" in content:
        _write_ipc_response({
            "content": "",
            "model": "",
            "error": content,
            "tool_log": [],
        })


import subprocess
import tempfile

# IPC paths for console communication
_IPC_DIR = os.path.join(os.environ.get("TEMP", "/tmp"), "copilot_blender_ipc")
_PROMPT_FILE = os.path.join(_IPC_DIR, "prompt.json")
_RESPONSE_FILE = os.path.join(_IPC_DIR, "response.json")
_COMMAND_RESPONSE_FILE = os.path.join(_IPC_DIR, "command_response.json")
_EVENTS_FILE = os.path.join(_IPC_DIR, "events.jsonl")
_STATUS_FILE = os.path.join(_IPC_DIR, "status.json")
_SHUTDOWN_FILE = os.path.join(_IPC_DIR, "shutdown.json")
_CANCEL_FILE = os.path.join(_IPC_DIR, "cancel.json")
print(f"[CopilotIPC] IPC_DIR = {_IPC_DIR}")
print(f"[CopilotIPC] RESPONSE_FILE = {_RESPONSE_FILE}")
_chat_log_path = os.path.join(tempfile.gettempdir(), "copilot_blender_chat.log")
_console_proc = None
_pending_model_response = False
_startup_restore_done = False
_ipc_event_lock = threading.Lock()
_ipc_cancel_stop = threading.Event()
_ipc_cancel_thread = None
_REASONING_STRENGTHS = {
    "default": "Model default",
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Maximum",
}
_REASONING_ALIASES = {
    "auto": "default",
    "model-default": "default",
    "model_default": "default",
    "min": "minimal",
    "minimum": "minimal",
    "extra-high": "xhigh",
    "maximum": "max",
}


def _atomic_write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _start_ipc_cancel_watcher():
    """Watch cancellation IPC independently of Blender's potentially busy main thread."""
    global _ipc_cancel_thread
    if _ipc_cancel_thread is not None and _ipc_cancel_thread.is_alive():
        return
    _ipc_cancel_stop.clear()

    def _watch():
        while not _ipc_cancel_stop.wait(0.1):
            if not os.path.exists(_CANCEL_FILE):
                continue
            try:
                with open(_CANCEL_FILE, "r", encoding="utf-8") as handle:
                    signal = json.load(handle)
                os.remove(_CANCEL_FILE)
            except (OSError, json.JSONDecodeError):
                continue
            if not signal.get("cancel"):
                continue
            request_id = COPILOT_OT_AsyncTimer._active_request_id
            if request_id > 0:
                _api.cancel_chat_request(request_id)

    _ipc_cancel_thread = threading.Thread(
        target=_watch,
        name="CopilotBlenderCancelWatcher",
        daemon=True,
    )
    _ipc_cancel_thread.start()


def _stop_ipc_cancel_watcher():
    global _ipc_cancel_thread
    _ipc_cancel_stop.set()
    thread = _ipc_cancel_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    _ipc_cancel_thread = None
    try:
        if os.path.exists(_CANCEL_FILE):
            os.remove(_CANCEL_FILE)
    except OSError:
        pass


def _load_render_preview(path):
    """Load a render image into bpy.data.images for inline preview."""
    try:
        img_name = "CopilotRender"
        if img_name in bpy.data.images:
            bpy.data.images[img_name].filepath = path
            bpy.data.images[img_name].reload()
        else:
            bpy.data.images.load(path, check_existing=False)
            bpy.data.images[-1].name = img_name
    except Exception as e:
        print(f"[CopilotChat] Failed to load render preview: {e}")


def _write_ipc_status(context):
    """Write current status so the console knows we're connected."""
    cp = _get_cp(context)
    os.makedirs(_IPC_DIR, exist_ok=True)
    models = []
    for i, item in enumerate(cp.available_models):
        models.append({
            "index": i + 1,
            "id": item.model_id,
            "display_name": item.display_name,
            "vendor": item.vendor,
            "category": item.category,
            "supports_tools": item.supports_tools,
            "supports_vision": item.supports_vision,
            "is_default": item.is_default,
            "endpoint": item.endpoint,
            "supported_endpoints": item.supported_endpoints,
            "reasoning_efforts": item.reasoning_efforts,
            "supports_reasoning": item.supports_reasoning,
            "active": item.model_id == cp.active_model_id,
        })
    sessions = _auth.list_conversation_sessions()
    active_session_id = _auth.get_active_session_id()
    active_session_title = ""
    for session in sessions:
        if session.get("id") == active_session_id:
            active_session_title = session.get("title", "")
            break
    data = {
        "connected": True,
        "username": cp.username,
        "active_model": cp.active_model_id,
        "reasoning_strength": cp.reasoning_strength,
        "reasoning_label": _reasoning_strength_label(cp.reasoning_strength),
        "models": models,
        "active_session_id": active_session_id,
        "active_session_title": active_session_title,
        "sessions": sessions,
        "last_error": cp.last_error,
        "is_thinking": bool(cp.is_thinking),
        "request_active": COPILOT_OT_AsyncTimer._active_request_id > 0,
        "timestamp": time.time(),
    }
    try:
        _atomic_write_json(_STATUS_FILE, data)
    except OSError:
        pass


def _write_ipc_response(result):
    """Write a chat response for the console to pick up."""
    try:
        _atomic_write_json(_RESPONSE_FILE, result)
        payload_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        print(f"[CopilotIPC] OK wrote response.json ({payload_size} bytes)")
    except Exception as e:
        print(f"[CopilotIPC] FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


def _write_ipc_event(event):
    """Append a live agent progress event for the external console."""
    try:
        os.makedirs(_IPC_DIR, exist_ok=True)
        event = dict(event or {})
        event["timestamp"] = time.time()
        payload = json.dumps(event, ensure_ascii=False)
        with _ipc_event_lock:
            with open(_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(payload + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception as e:
        print(f"[CopilotIPC] Failed to write agent event: {e}")


def _clear_ipc_events():
    try:
        os.makedirs(_IPC_DIR, exist_ok=True)
        with _ipc_event_lock:
            with open(_EVENTS_FILE, "w", encoding="utf-8"):
                pass
    except OSError:
        pass


def _write_ipc_chat_result(result):
    """Write completed background chat result directly to IPC."""
    if not result or result.get("_ipc_response_written"):
        return
    if result.get("error"):
        _write_ipc_response({
            "content": result.get("content", ""),
            "model": result.get("model", ""),
            "error": result.get("error"),
            "tool_log": result.get("tool_log", []),
        })
    else:
        _write_ipc_response({
            "content": result.get("content", "(no response)"),
            "model": result.get("model", ""),
            "error": None,
            "tool_log": result.get("tool_log", []),
        })
    result["_ipc_response_written"] = True


def _cancel_active_request(context, notify=True):
    rid = COPILOT_OT_AsyncTimer._active_request_id
    if rid > 0:
        _api.cancel_chat_request(rid)
    try:
        cp = _get_cp(context)
        cp.is_thinking = rid > 0
        cp.thinking_text = "Cancelling..." if rid > 0 else ""
    except Exception:
        pass
    if notify:
        _write_command_response("Cancellation requested." if rid > 0 else "No active request to cancel.")
    _write_ipc_status(context)


def _write_ipc_shutdown():
    try:
        payload = {"shutdown": True, "timestamp": time.time()}
        _atomic_write_json(_SHUTDOWN_FILE, payload)
        _atomic_write_json(
            _STATUS_FILE,
            {"connected": False, "shutdown": True, "timestamp": time.time()},
        )
    except OSError:
        pass


def _terminate_chat_console():
    """Terminate the console process spawned by this Blender session."""
    global _console_proc
    if _console_proc is None:
        return
    try:
        if _console_proc.poll() is None:
            _console_proc.terminate()
            try:
                _console_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _console_proc.kill()
    except Exception as e:
        print(f"[CopilotChat] Failed to terminate console: {e}")
    finally:
        _console_proc = None


def _write_command_response(content, error=None):
    try:
        _atomic_write_json(_COMMAND_RESPONSE_FILE, {
            "content": "" if error else content,
            "model": "command",
            "error": error,
            "tool_log": [],
        })
    except Exception as exc:
        print(f"[CopilotIPC] Failed to write command response: {exc}")


def _normalize_reasoning_strength(value: str) -> str:
    value = str(value or "").strip().lower().replace("_", "-")
    value = _REASONING_ALIASES.get(value, value)
    return value if value in _REASONING_STRENGTHS else ""


def _reasoning_strength_label(value: str) -> str:
    return _REASONING_STRENGTHS.get(_normalize_reasoning_strength(value), _REASONING_STRENGTHS["default"])


def _reasoning_help_text() -> str:
    return "default, none, minimal, low, medium, high, xhigh, max"


def _split_model_and_reasoning(selector: str):
    selector = str(selector or "").strip()
    parts = selector.split()
    if len(parts) <= 1:
        return selector, ""
    reasoning = _normalize_reasoning_strength(parts[-1])
    if not reasoning:
        return selector, ""
    return " ".join(parts[:-1]).strip(), reasoning


def _set_reasoning_strength(context, value: str, emit_chat=True) -> bool:
    cp = _get_cp(context)
    prefs = get_prefs(context)
    reasoning = _normalize_reasoning_strength(value)
    if not reasoning:
        cp.last_error = f"Reasoning strength must be one of: {_reasoning_help_text()}"
        if emit_chat:
            _add_chat(cp, "system", f"Error: {cp.last_error}")
        _write_ipc_status(context)
        return False
    cp.reasoning_strength = reasoning
    prefs.cached_reasoning_strength = reasoning
    cp.last_error = ""
    if emit_chat:
        _add_chat(cp, "system", f"Reasoning strength set to: {_reasoning_strength_label(reasoning)}")
    _write_ipc_status(context)
    return True


def _format_reasoning_text(cp) -> str:
    return (
        f"Reasoning strength: {_reasoning_strength_label(cp.reasoning_strength)}\n"
        f"Options: {_reasoning_help_text()}\n"
        "Use /reasoning <strength> or /model <id|number> <strength>."
    )


def _format_models_text(cp):
    if len(cp.available_models) == 0:
        message = (
            f"Active model: {cp.active_model_id or 'none'}\n"
            f"Reasoning strength: {_reasoning_strength_label(cp.reasoning_strength)}\n"
            "No model list is loaded."
        )
        if cp.last_error:
            message += f"\nLast error: {cp.last_error}"
        return message

    lines = [
        f"Active model: {cp.active_model_id or 'none'}",
        f"Reasoning strength: {_reasoning_strength_label(cp.reasoning_strength)}",
        f"Use /model <id|number> [{_reasoning_help_text()}]",
    ]
    for i, item in enumerate(cp.available_models, start=1):
        marker = "*" if item.model_id == cp.active_model_id else " "
        details = []
        if item.vendor:
            details.append(item.vendor)
        if item.is_default:
            details.append("default")
        if item.supports_tools:
            details.append("tools")
        if item.supports_vision:
            details.append("vision")
        if item.supports_reasoning and item.reasoning_efforts:
            details.append(f"reasoning={item.reasoning_efforts}")
        if item.endpoint and item.endpoint != "/chat/completions":
            details.append(item.endpoint)
        suffix = f" ({', '.join(details)})" if details else ""
        display = item.display_name or item.model_id
        lines.append(f"{marker} {i}. {item.model_id} - {display}{suffix}")
    return "\n".join(lines)


def _active_model_item(cp):
    for item in cp.available_models:
        if item.model_id == cp.active_model_id:
            return item
    return None


def _active_model_endpoint(cp) -> str:
    item = _active_model_item(cp)
    return (item.endpoint if item else "") or "/chat/completions"


def _active_model_supports_tools(cp) -> bool:
    item = _active_model_item(cp)
    return True if item is None else bool(item.supports_tools)


def _format_sessions_text():
    sessions = _auth.list_conversation_sessions()
    active = _auth.get_active_session_id()
    if not sessions:
        return "No saved conversations yet. Use /new to start one, or just send a message."

    lines = [f"Saved conversations. Active: {active}"]
    for i, session in enumerate(sessions, start=1):
        marker = "*" if session.get("active") else " "
        title = session.get("title") or "(untitled)"
        count = session.get("message_count", 0)
        lines.append(f"{marker} {i}. {session.get('id')} - {title} ({count} messages)")
    return "\n".join(lines)


def _format_history_text(limit=80):
    messages = [
        msg for msg in _auth.load_chat_history()
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant")
    ]
    if not messages:
        return "No saved messages in the active conversation."

    shown = messages[-limit:]
    lines = [f"Saved conversation history ({len(shown)} of {len(messages)} messages):"]
    for msg in shown:
        role = "YOU" if msg.get("role") == "user" else "COPILOT"
        content = str(msg.get("content", "") or "")
        lines.append(f"{role} > {content}")
    return "\n".join(lines)


def _format_tools_text():
    tools = _tool_defs.get_blender_tool_definitions()
    names = []
    for tool in tools:
        func = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = func.get("name", "")
        if name:
            names.append(name)
    return f"{len(names)} tools available to tool-capable models:\n" + "\n".join(f"- {name}" for name in names)


def _format_docs_text():
    configured = _auth.load_blender_docs_path()
    default_path = _auth.get_default_blender_docs_path()
    effective = configured or default_path
    exists = os.path.exists(os.path.expanduser(effective))
    status = "found" if exists else "not found yet"
    source = "configured override" if configured else "automatic default"
    return (
        f"Local Blender docs path: {effective} ({status}, {source})\n"
        f"Default drop-in folder: {default_path}\n"
        "Put downloaded HTML docs under that folder and the model can search them automatically. "
        "Use /docs <path> only if you want a different folder."
    )


def _check_ipc_prompt(context):
    """Check if the console has written a prompt for us. Returns prompt dict or None."""
    try:
        if os.path.exists(_PROMPT_FILE):
            with open(_PROMPT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(_PROMPT_FILE)
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _spawn_chat_console():
    """Spawn the dedicated chat console window."""
    global _console_proc
    if _console_proc is not None and _console_proc.poll() is None:
        return  # Already running

    # Clean up old IPC files
    os.makedirs(_IPC_DIR, exist_ok=True)
    for f in (
        _PROMPT_FILE,
        _RESPONSE_FILE,
        _COMMAND_RESPONSE_FILE,
        _SHUTDOWN_FILE,
        _CANCEL_FILE,
    ):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass

    console_script = os.path.join(os.path.dirname(__file__), "chat_console.py")
    try:
        _console_proc = subprocess.Popen(
            [sys.executable, console_script],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print(f"[CopilotChat] Console spawned (PID {_console_proc.pid})")
    except Exception as e:
        print(f"[CopilotChat] Failed to spawn console: {e}")


def _restore_cached_session_state(context, spawn_console=True):
    """Restore cached auth/session state on add-on startup without user clicks."""
    cp = _get_cp(context)
    prefs = get_prefs(context)
    cache = _auth.try_restore_session()
    if not cache or not cache.get("copilot_token"):
        return False

    cp.is_authenticated = True
    cp.oauth_token = cache.get("oauth_token", "")
    cp.copilot_token = cache.get("copilot_token", "")
    cp.token_expires_at = cache.get("expires_at", 0)
    cp.username = cache.get("username", "")
    cp.api_base = cache.get("api_base", _auth.DEFAULT_API_BASE)
    cp.sku = cache.get("sku", "")
    cp.auth_status = f"Signed in as {cp.username}"
    prefs.cached_oauth_token = cp.oauth_token
    prefs.cached_username = cp.username
    if prefs.cached_active_model and not cp.active_model_id:
        cp.active_model_id = prefs.cached_active_model
    cached_reasoning = _normalize_reasoning_strength(prefs.cached_reasoning_strength)
    if cached_reasoning:
        cp.reasoning_strength = cached_reasoning

    _load_active_session_into_scene(cp)
    _ensure_timer(context)
    _write_ipc_status(context)
    if spawn_console:
        _spawn_chat_console()
    if len(cp.available_models) == 0:
        try:
            bpy.ops.copilot.refresh_models()
        except Exception as e:
            print(f"[CopilotStartup] Model refresh failed: {e}")
    return True


def _startup_restore_tick():
    global _startup_restore_done
    if _startup_restore_done:
        return None
    try:
        _startup_restore_done = True
        _restore_cached_session_state(bpy.context, spawn_console=True)
    except Exception as e:
        print(f"[CopilotStartup] Cached session restore failed: {e}")
        import traceback
        traceback.print_exc()
    return None


def _restore_chat_history(cp, force=False):
    """Load saved chat history from disk into scene properties."""
    saved = _auth.load_chat_history()
    if not saved and not force:
        return
    # Don't duplicate — only load if chat_history is empty or has just the
    # system "Restored session" message
    if not force and len(cp.chat_history) > 1:
        return
    # Clear the "Restored session" system message we just added
    cp.chat_history.clear()
    for item in saved:
        msg = cp.chat_history.add()
        msg.role = item.get("role", "user")
        msg.content = item.get("content", "")
        msg.model_id = item.get("model_id", "")
        msg.timestamp = item.get("timestamp", 0.0)
    print(f"[CopilotChat] Restored {len(saved)} messages from previous session")


def _history_messages_for_context(cp) -> list:
    scene_items = [
        {"role": msg.role, "content": msg.content}
        for msg in cp.chat_history
        if msg.role in ("user", "assistant")
    ]
    saved_items = [
        {"role": msg.get("role"), "content": msg.get("content", "")}
        for msg in _auth.load_chat_history()
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant")
    ]
    items = saved_items if len(saved_items) > len(scene_items) else scene_items

    messages = []
    for msg in items:
        messages.append({"role": msg["role"], "content": msg.get("content", "")})
    return messages


def _same_user_content(existing, new_content) -> bool:
    if isinstance(new_content, str):
        return existing == new_content
    if isinstance(new_content, list) and new_content:
        first = new_content[0]
        if isinstance(first, dict):
            return existing == first.get("text", "")
    return False


def _load_conversation_from_scene(cp) -> list:
    try:
        messages = json.loads(cp.conversation_json or "[]")
    except json.JSONDecodeError:
        return []
    return messages if isinstance(messages, list) else []


def _store_conversation(cp, messages: list):
    # Keep persisted context aligned with the visible chat transcript. Raw API
    # messages can contain tool_call/tool/render-image fragments; if those get
    # saved as the source of truth, later turns can resume with no user context.
    transcript = _history_messages_for_context(cp)
    source = transcript if len(transcript) > 1 else messages
    stored = _api.prepare_messages_for_storage(source)
    cp.conversation_json = json.dumps(stored, ensure_ascii=False)
    _auth.save_conversation_messages(stored)


def _restore_conversation(cp, force=False):
    """Load saved API conversation context used for true chat resume."""
    if not force and _load_conversation_from_scene(cp):
        return

    saved = _auth.load_conversation_messages()
    if saved:
        _store_conversation(cp, saved)
        print(f"[CopilotChat] Restored {len(saved)} API conversation messages")
        return

    history_context = _history_messages_for_context(cp)
    if len(history_context) > 1:
        _store_conversation(cp, history_context)


def _load_active_session_into_scene(cp):
    _restore_chat_history(cp, force=True)
    _restore_conversation(cp, force=True)


def _resolve_model_selector(cp, selector: str) -> str:
    selector = str(selector or "").strip()
    if not selector:
        return ""

    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(cp.available_models):
            return cp.available_models[idx].model_id

    lowered = selector.lower()
    exact = []
    partial = []
    for item in cp.available_models:
        model_id = item.model_id
        display = item.display_name or model_id
        if lowered in (model_id.lower(), display.lower()):
            exact.append(model_id)
        elif lowered in model_id.lower() or lowered in display.lower():
            partial.append(model_id)

    if len(exact) == 1:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    return ""


def _select_model_from_ipc(context, selector: str):
    cp = _get_cp(context)
    prefs = get_prefs(context)
    selector, reasoning = _split_model_and_reasoning(selector)
    model_id = _resolve_model_selector(cp, selector)
    if not model_id:
        cp.last_error = f"Model not found or ambiguous: {selector}"
        _add_chat(cp, "system", f"Error: {cp.last_error}")
        _write_ipc_status(context)
        return

    cp.active_model_id = model_id
    prefs.cached_active_model = model_id
    if reasoning:
        cp.reasoning_strength = reasoning
        prefs.cached_reasoning_strength = reasoning
    for i, item in enumerate(cp.available_models):
        if item.model_id == model_id:
            cp.active_model_index = i
            break
    cp.last_error = ""
    _add_chat(cp, "system", f"Model set to: {model_id} (reasoning: {_reasoning_strength_label(cp.reasoning_strength)})")
    _write_ipc_status(context)


def _resolve_session_selector(selector: str) -> str:
    selector = str(selector or "").strip()
    sessions = _auth.list_conversation_sessions()
    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]["id"]

    lowered = selector.lower()
    exact = [s["id"] for s in sessions if lowered in (s["id"].lower(), s.get("title", "").lower())]
    if len(exact) == 1:
        return exact[0]
    partial = [s["id"] for s in sessions if lowered in s["id"].lower() or lowered in s.get("title", "").lower()]
    if len(partial) == 1:
        return partial[0]
    return ""


def _resume_session_from_ipc(context, selector: str):
    cp = _get_cp(context)
    if COPILOT_OT_AsyncTimer._active_request_id > 0:
        cp.last_error = "Cannot switch conversations while a request is running. Use /cancel first."
        _write_ipc_status(context)
        return False
    session_id = _resolve_session_selector(selector)
    if not session_id or not _auth.switch_conversation_session(session_id):
        cp.last_error = f"Conversation not found or ambiguous: {selector}"
        _add_chat(cp, "system", f"Error: {cp.last_error}")
        _write_ipc_status(context)
        return False

    cp.last_error = ""
    cp.conversation_json = "[]"
    _load_active_session_into_scene(cp)
    _add_chat(cp, "system", f"Resumed conversation: {session_id}")
    _write_ipc_status(context)
    return True


def _new_session_from_ipc(context, title: str):
    cp = _get_cp(context)
    if COPILOT_OT_AsyncTimer._active_request_id > 0:
        cp.last_error = "Cannot start a new conversation while a request is running. Use /cancel first."
        _write_ipc_status(context)
        return False
    session_id = _auth.create_conversation_session(title)
    cp.chat_history.clear()
    cp.conversation_json = "[]"
    cp.tool_log = ""
    cp.last_error = ""
    _add_chat(cp, "system", f"Started new conversation: {session_id}")
    _write_ipc_status(context)
    return True


def _handle_slash_command(context, prompt: str) -> bool:
    """Handle slash commands even when they arrive as normal chat text."""
    cp = _get_cp(context)
    prompt = str(prompt or "").strip()
    if not prompt.startswith("/"):
        return False

    cp.prompt_text = ""
    parts = prompt.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "/help":
        _write_command_response(
            "Slash commands:\n"
            "/models - refresh and list available models\n"
            "/model - show active model and model list\n"
            "/model <id|number> [default|none|minimal|low|medium|high|xhigh|max] - select a model/reasoning strength\n"
            "/reasoning [default|none|minimal|low|medium|high|xhigh|max] - show or set reasoning strength\n"
            "/tools - list tools available to tool-capable models\n"
            "/docs - show automatic docs folder / configured override\n"
            "/docs <path> - override downloaded Blender docs folder/file path\n"
            "/docs clear - remove override and use automatic docs folder\n"
            "/sessions - list saved conversations\n"
            "/history - show saved messages for the active conversation\n"
            "/resume <id|number> - resume a saved conversation\n"
            "/new [title] - start a new saved conversation\n"
            "/cancel - cancel the active request\n"
            "/clear - clear the active conversation"
        )
        return True

    if command == "/tools":
        _write_command_response(_format_tools_text())
        return True

    if command == "/docs":
        if not arg:
            _write_command_response(_format_docs_text())
            return True
        if arg.lower() in ("clear", "unset", "none"):
            _auth.clear_blender_docs_path()
            _write_command_response(f"Docs override cleared. Using automatic folder: {_auth.get_default_blender_docs_path()}")
            return True
        path = os.path.abspath(os.path.expanduser(arg.strip('"')))
        if not os.path.exists(path):
            _write_command_response("", f"Docs path not found: {path}")
            return True
        _auth.save_blender_docs_path(path)
        _write_command_response(f"Local Blender docs path set to: {path}")
        return True

    if command == "/models":
        global _pending_model_response
        if not _ensure_token(context):
            _write_command_response("", "Not authenticated - sign in first")
            return True
        _pending_model_response = True
        bpy.ops.copilot.refresh_models()
        return True

    if command == "/model":
        if arg:
            _select_model_from_ipc(context, arg)
            if cp.last_error:
                _write_command_response("", cp.last_error)
            else:
                _write_command_response(
                    f"Active model: {cp.active_model_id}\n"
                    f"Reasoning strength: {_reasoning_strength_label(cp.reasoning_strength)}"
                )
        else:
            _write_command_response(_format_models_text(cp))
        return True

    if command == "/reasoning":
        if not arg:
            _write_command_response(_format_reasoning_text(cp))
        elif _set_reasoning_strength(context, arg):
            _write_command_response(f"Reasoning strength: {_reasoning_strength_label(cp.reasoning_strength)}")
        else:
            _write_command_response("", cp.last_error)
        return True

    if command == "/sessions":
        _write_command_response(_format_sessions_text())
        _write_ipc_status(context)
        return True

    if command == "/history":
        _write_command_response(_format_history_text())
        return True

    if command == "/resume":
        if not arg:
            _write_command_response(_format_sessions_text() + "\n\nUsage: /resume <id|number>")
            return True
        _resume_session_from_ipc(context, arg)
        if cp.last_error:
            _write_command_response("", cp.last_error)
        else:
            history = _format_history_text()
            _write_command_response(f"Resumed conversation: {_auth.get_active_session_id()}\n\n{history}")
        return True

    if command == "/new":
        if _new_session_from_ipc(context, arg):
            _write_command_response(f"Started new conversation: {_auth.get_active_session_id()}")
        else:
            _write_command_response("", cp.last_error)
        return True

    if command == "/cancel":
        _cancel_active_request(context)
        return True

    if command == "/clear":
        result = bpy.ops.copilot.clear_chat()
        if 'FINISHED' in result:
            _write_command_response("Chat cleared.")
        else:
            _write_command_response("", cp.last_error or "Chat could not be cleared.")
        return True

    _write_command_response("", f"Unknown command: {command}. Type /help for commands.")
    return True


def _build_messages_for_send(cp, user_content):
    # Use the visible user/assistant transcript as the request context. This is
    # CLI-like behavior: tool outputs stay available through tools/logs, but the
    # next conversational turn is grounded in what the user actually saw.
    messages = _history_messages_for_context(cp)
    if messages and messages[-1].get("role") == "user" and _same_user_content(messages[-1].get("content", ""), user_content):
        messages[-1]["content"] = user_content
    else:
        messages.append({"role": "user", "content": user_content})
    return messages


def _ensure_token(context) -> bool:
    """Ensure Copilot session token is valid, refresh if needed. Returns True if valid."""
    cp = _get_cp(context)
    if not cp.oauth_token:
        return False
    refreshed = _auth.ensure_valid_copilot_token(
        cp.oauth_token, cp.copilot_token, cp.token_expires_at
    )
    if refreshed:
        cp.copilot_token = refreshed.get("token", "")
        cp.token_expires_at = refreshed.get("expires_at", 0)
        cp.api_base = refreshed.get("endpoints", {}).get("api", cp.api_base)
    return bool(cp.copilot_token)


# ── Timer processing (drives async results + main-thread queue) ──────────

def _process_async_tick(context):
    """Poll IPC, drain tool work, and publish completed chat results."""
    global _pending_model_response

    now = time.time()
    if now - COPILOT_OT_AsyncTimer._last_status_write >= 2.0:
        _write_ipc_status(context)
        COPILOT_OT_AsyncTimer._last_status_write = now

    # Drain Blender main-thread queue (tool execution)
    _executor.drain_main_queue()

    # Proactively refresh token to prevent expiry during tool loops
    _ensure_token(context)

    # Check for prompts from the external chat console
    ipc_prompt = _check_ipc_prompt(context)
    if ipc_prompt:
        action = ipc_prompt.get("action", "chat")
        prompt = ipc_prompt.get("prompt", "")
        if action == "cancel" or (action == "chat" and str(prompt).strip().lower() == "/cancel"):
            _cancel_active_request(context)
        elif action == "status":
            _write_ipc_status(context)
        elif (
            action == "chat"
            and prompt
            and str(prompt).strip().split(maxsplit=1)[0].lower()
            in {"/history", "/sessions", "/conversations", "/tools", "/docs", "/model", "/reasoning"}
        ):
            _handle_slash_command(context, prompt)
        elif COPILOT_OT_AsyncTimer._active_request_id > 0:
            error = "A request is already running. Use /cancel to stop it."
            if action in {"clear", "refresh_models", "models", "select_model", "resume_session", "new_session"}:
                cp = _get_cp(context)
                cp.last_error = error
                _write_ipc_status(context)
            else:
                _write_command_response("", error)
        elif action == "chat" and prompt:
            cp = _get_cp(context)
            cp.prompt_text = prompt
            bpy.ops.copilot.send_chat()
        elif action == "clear":
            bpy.ops.copilot.clear_chat()
        elif action == "refresh_models":
            bpy.ops.copilot.refresh_models()
        elif action == "models":
            _pending_model_response = True
            bpy.ops.copilot.refresh_models()
        elif action == "select_model":
            selector = ipc_prompt.get("model") or prompt
            reasoning = ipc_prompt.get("reasoning")
            if reasoning:
                selector = f"{selector} {reasoning}"
            _select_model_from_ipc(context, selector)
        elif action == "resume_session":
            _resume_session_from_ipc(context, ipc_prompt.get("session_id") or prompt)
        elif action == "new_session":
            _new_session_from_ipc(context, ipc_prompt.get("title") or prompt)

    # Check for completed async chat
    if COPILOT_OT_AsyncTimer._active_request_id > 0:
        status = _api.get_chat_result(COPILOT_OT_AsyncTimer._active_request_id)

        if status["status"] == "unknown":
            cp = _get_cp(context)
            cp.is_thinking = False
            error_text = "Background request state was lost; the active conversation was preserved."
            _add_chat(cp, "system", f"Error: {error_text}")
            _write_ipc_chat_result({
                "content": "",
                "model": cp.active_model_id,
                "error": error_text,
                "tool_log": [],
            })
            COPILOT_OT_AsyncTimer._active_request_id = 0
            COPILOT_OT_AsyncTimer._request_start_time = 0.0
            _write_ipc_status(context)
            return

        if status["status"] == "done":
            result = status["result"]
            cp = _get_cp(context)
            cp.is_thinking = False
            ipc_already_written = bool(result and result.get("_ipc_response_written"))

            if result and result.get("error"):
                if result.get("messages"):
                    _store_conversation(cp, result["messages"])
                if result.get("content"):
                    _add_chat(
                        cp,
                        "assistant",
                        result["content"],
                        model_id=result.get("model", ""),
                        emit_ipc=False,
                    )
                _add_chat(cp, "system", f"Error: {result['error']}", emit_ipc=not ipc_already_written)
            elif result:
                model_label = result.get("model", "API:model-missing")
                content = result.get("content", "(no response)")
                _add_chat(cp, "assistant", content, model_id=model_label, emit_ipc=not ipc_already_written)
                if result.get("messages"):
                    _store_conversation(cp, result["messages"])

                # Append tool log and extract last render path
                if result.get("tool_log"):
                    cp.tool_log = "\n".join(result["tool_log"])
                    for entry in reversed(result["tool_log"]):
                        if "__RENDER_IMAGE__:" in entry:
                            idx = entry.find("__RENDER_IMAGE__:")
                            path = entry[idx + 17:].split("\n")[0].split("→")[0].strip()
                            if os.path.isfile(path):
                                cp.last_render_path = path
                                _load_render_preview(path)
                            break

            completed_request_id = COPILOT_OT_AsyncTimer._active_request_id
            _api.discard_chat_result(completed_request_id)
            COPILOT_OT_AsyncTimer._active_request_id = 0
            COPILOT_OT_AsyncTimer._request_start_time = 0.0
            _write_ipc_status(context)

            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


def _app_timer_tick():
    try:
        _process_async_tick(bpy.context)
    except Exception as e:
        print(f"[CopilotTimer] App timer error: {e}")
        import traceback
        traceback.print_exc()
    return 0.1


# ── Modal timer operator (fallback for older Blender event paths) ────────

class COPILOT_OT_AsyncTimer(Operator):
    """Background modal timer that polls for async chat results and drains main-thread queue."""
    bl_idname = "copilot.async_timer"
    bl_label = "Copilot Async Timer"
    bl_options = {'INTERNAL'}

    _timer = None
    _active_request_id: int = 0
    _is_running: bool = False
    _request_start_time: float = 0.0
    _last_status_write: float = 0.0

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        _process_async_tick(context)
        return {'PASS_THROUGH'}

    def execute(self, context):
        if COPILOT_OT_AsyncTimer._is_running:
            return {'CANCELLED'}
        COPILOT_OT_AsyncTimer._is_running = True
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        COPILOT_OT_AsyncTimer._is_running = False
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


def _ensure_timer(context):
    """Start the persistent async timer if not already running."""
    try:
        if not bpy.app.timers.is_registered(_app_timer_tick):
            bpy.app.timers.register(_app_timer_tick, first_interval=0.1, persistent=True)
    except Exception as e:
        print(f"[CopilotTimer] Failed to register app timer: {e}")


# ── Sign In ──────────────────────────────────────────────────────────────

class COPILOT_OT_SignIn(Operator):
    """Start GitHub OAuth device flow sign-in."""
    bl_idname = "copilot.sign_in"
    bl_label = "Sign In to GitHub Copilot"

    def execute(self, context):
        cp = _get_cp(context)
        prefs = get_prefs(context)

        # Try restore from disk cache first
        cache = _auth.try_restore_session()
        if cache and cache.get("copilot_token"):
            cp.is_authenticated = True
            cp.oauth_token = cache.get("oauth_token", "")
            cp.copilot_token = cache.get("copilot_token", "")
            cp.token_expires_at = cache.get("expires_at", 0)
            cp.username = cache.get("username", "")
            cp.api_base = cache.get("api_base", _auth.DEFAULT_API_BASE)
            cp.sku = cache.get("sku", "")
            cp.auth_status = f"Signed in as {cp.username}"
            prefs.cached_oauth_token = cp.oauth_token
            prefs.cached_username = cp.username
            cached_reasoning = _normalize_reasoning_strength(prefs.cached_reasoning_strength)
            if cached_reasoning:
                cp.reasoning_strength = cached_reasoning
            _load_active_session_into_scene(cp)
            self.report({'INFO'}, f"Signed in as {cp.username}")
            # Auto-fetch models
            bpy.ops.copilot.refresh_models()
            # Spawn console and start timer
            _spawn_chat_console()
            _ensure_timer(context)
            _write_ipc_status(context)
            return {'FINISHED'}

        # Start device flow
        cp.auth_status = "Starting device flow..."
        _add_chat(cp, "system", "Starting GitHub sign-in...")

        def on_code_ready(user_code, verification_uri):
            # Called from background thread — schedule UI update
            def _update():
                cp.device_code_display = user_code
                cp.auth_status = f"Enter code: {user_code} at {verification_uri}"
                _add_chat(cp, "system", f"Go to {verification_uri} and enter: {user_code}")
            with _executor._main_queue_lock:
                _executor._main_queue.append((0, _update, (), {}))

            # Auto-open browser
            try:
                webbrowser.open(verification_uri)
            except Exception:
                pass

        def on_complete(oauth_token, username, copilot_data):
            def _update():
                cp.is_authenticated = True
                cp.oauth_token = oauth_token
                cp.copilot_token = copilot_data.get("token", "")
                cp.token_expires_at = copilot_data.get("expires_at", 0)
                cp.username = username
                cp.api_base = copilot_data.get("endpoints", {}).get("api", _auth.DEFAULT_API_BASE)
                cp.sku = copilot_data.get("sku", "")
                cp.auth_status = f"Signed in as {username}"
                cp.device_code_display = ""
                prefs.cached_oauth_token = oauth_token
                prefs.cached_username = username
                cached_reasoning = _normalize_reasoning_strength(prefs.cached_reasoning_strength)
                if cached_reasoning:
                    cp.reasoning_strength = cached_reasoning
                _load_active_session_into_scene(cp)
                # Auto-fetch models
                bpy.ops.copilot.refresh_models()
                _spawn_chat_console()
                _write_ipc_status(bpy.context)
            with _executor._main_queue_lock:
                _executor._main_queue.append((0, _update, (), {}))

        def on_error(message):
            def _update():
                cp.auth_status = f"Auth error: {message}"
                cp.device_code_display = ""
                _add_chat(cp, "system", f"Auth error: {message}")
            with _executor._main_queue_lock:
                _executor._main_queue.append((0, _update, (), {}))

        _ensure_timer(context)
        _auth.start_device_flow(on_code_ready, on_complete, on_error)
        return {'FINISHED'}


# ── Sign Out ─────────────────────────────────────────────────────────────

class COPILOT_OT_SignOut(Operator):
    """Sign out and clear cached tokens."""
    bl_idname = "copilot.sign_out"
    bl_label = "Sign Out"

    def execute(self, context):
        cp = _get_cp(context)
        prefs = get_prefs(context)

        cp.is_authenticated = False
        cp.oauth_token = ""
        cp.copilot_token = ""
        cp.username = ""
        cp.auth_status = "Signed out"
        cp.device_code_display = ""
        cp.available_models.clear()
        cp.active_model_id = ""
        cp.reasoning_strength = "default"
        prefs.cached_oauth_token = ""
        prefs.cached_username = ""
        prefs.cached_active_model = ""
        prefs.cached_reasoning_strength = "default"
        _auth.clear_token_cache()
        _add_chat(cp, "system", "Signed out.")
        self.report({'INFO'}, "Signed out")
        return {'FINISHED'}


# ── Refresh Models ───────────────────────────────────────────────────────

class COPILOT_OT_RefreshModels(Operator):
    """Fetch available models from the Copilot API."""
    bl_idname = "copilot.refresh_models"
    bl_label = "Refresh Models"

    def execute(self, context):
        cp = _get_cp(context)
        prefs = get_prefs(context)

        if not _ensure_token(context):
            self.report({'WARNING'}, "Not authenticated")
            return {'CANCELLED'}

        api_base = str(cp.api_base)
        copilot_token = str(cp.copilot_token)

        def _fetch():
            print(f"[CopilotModels] Fetching models from {api_base}...")
            error = ""
            try:
                models = _api.fetch_models(api_base, copilot_token)
                print("[CopilotModels] Model fetch completed")
                if not models:
                    error = "Model refresh returned an empty model list"
            except Exception as e:
                print(f"[CopilotModels] FETCH FAILED: {e}")
                error = f"Model refresh failed: {e}"
                models = []
            def _update():
                cp.last_error = error
                cp.available_models.clear()
                for m in models:
                    item = cp.available_models.add()
                    item.model_id = m["id"]
                    item.display_name = m["display_name"]
                    item.vendor = m.get("vendor", "")
                    item.category = m.get("category", "")
                    item.supports_tools = m.get("supports_tools", False)
                    item.supports_vision = m.get("supports_vision", False)
                    item.context_tokens = m.get("context_tokens", 0)
                    item.output_tokens = m.get("output_tokens", 0)
                    item.is_default = m.get("is_default", False)
                    item.endpoint = m.get("endpoint", "/chat/completions")
                    item.supported_endpoints = ",".join(m.get("supported_endpoints", []) or [item.endpoint])
                    item.reasoning_efforts = ",".join(m.get("reasoning_efforts", []))
                    item.supports_reasoning = bool(m.get("supports_reasoning", False))
                    item.multiplier = m.get("multiplier", 0)

                # Restore cached or pick default
                found = False
                if prefs.cached_active_model:
                    for i, item in enumerate(cp.available_models):
                        if item.model_id == prefs.cached_active_model:
                            cp.active_model_index = i
                            cp.active_model_id = item.model_id
                            found = True
                            break

                if not found:
                    for i, item in enumerate(cp.available_models):
                        if item.is_default:
                            cp.active_model_index = i
                            cp.active_model_id = item.model_id
                            found = True
                            break

                if not found and len(cp.available_models) > 0:
                    cp.active_model_index = 0
                    cp.active_model_id = cp.available_models[0].model_id
                    found = True

                if found:
                    prefs.cached_active_model = cp.active_model_id
                else:
                    cp.active_model_index = 0
                    cp.active_model_id = ""

                _add_chat(cp, "system", f"Model list refreshed. Active: {cp.active_model_id}")
                _write_ipc_status(bpy.context)
                global _pending_model_response
                if _pending_model_response:
                    _pending_model_response = False
                    _write_command_response(_format_models_text(cp))

            with _executor._main_queue_lock:
                _executor._main_queue.append((0, _update, (), {}))

        _ensure_timer(context)
        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        return {'FINISHED'}


# ── Select Model ─────────────────────────────────────────────────────────

class COPILOT_OT_SelectModel(Operator):
    """Set the active model."""
    bl_idname = "copilot.select_model"
    bl_label = "Select Model"

    model_id: StringProperty(default="")
    reasoning_strength: StringProperty(default="")

    def execute(self, context):
        cp = _get_cp(context)
        prefs = get_prefs(context)
        cp.active_model_id = self.model_id
        prefs.cached_active_model = self.model_id
        reasoning = _normalize_reasoning_strength(self.reasoning_strength)
        if reasoning:
            cp.reasoning_strength = reasoning
            prefs.cached_reasoning_strength = reasoning
        for i, item in enumerate(cp.available_models):
            if item.model_id == self.model_id:
                cp.active_model_index = i
                break
        cp.last_error = ""
        _add_chat(cp, "system", f"Model set to: {self.model_id} (reasoning: {_reasoning_strength_label(cp.reasoning_strength)})")
        _write_ipc_status(context)
        return {'FINISHED'}


# ── Send Chat ────────────────────────────────────────────────────────────

class COPILOT_OT_SendChat(Operator):
    """Send a chat message to Copilot."""
    bl_idname = "copilot.send_chat"
    bl_label = "Send"

    def execute(self, context):
        cp = _get_cp(context)
        prefs = get_prefs(context)
        prompt = cp.prompt_text.strip()

        if not prompt:
            self.report({'WARNING'}, "Prompt is empty")
            return {'CANCELLED'}

        if _handle_slash_command(context, prompt):
            return {'FINISHED'}

        if COPILOT_OT_AsyncTimer._active_request_id > 0:
            cp.last_error = "A request is already running. Use /cancel to stop it."
            self.report({'WARNING'}, cp.last_error)
            _write_ipc_status(context)
            return {'CANCELLED'}

        if not _ensure_token(context):
            self.report({'WARNING'}, "Not authenticated — sign in first")
            return {'CANCELLED'}

        if not cp.active_model_id:
            self.report({'WARNING'}, "No model selected — refresh models first")
            _write_command_response("", "No model selected — refresh models first")
            return {'CANCELLED'}

        if len(cp.available_models) == 0:
            cp.last_error = "No model list is loaded. Run /models to refresh before sending."
            self.report({'WARNING'}, cp.last_error)
            _write_command_response("", cp.last_error)
            _write_ipc_status(context)
            return {'CANCELLED'}

        if _active_model_item(cp) is None:
            cp.last_error = f"Selected model is not available from GitHub: {cp.active_model_id}. Run /models and select one of the listed models."
            _add_chat(cp, "system", f"Error: {cp.last_error}")
            self.report({'WARNING'}, cp.last_error)
            _write_command_response("", cp.last_error)
            _write_ipc_status(context)
            return {'CANCELLED'}

        active_model = _active_model_item(cp)
        reasoning = _normalize_reasoning_strength(cp.reasoning_strength)
        supported_reasoning = [
            value.strip()
            for value in (active_model.reasoning_efforts if active_model else "").split(",")
            if value.strip()
        ]
        if reasoning != "default" and active_model and not active_model.supports_reasoning:
            cp.last_error = (
                f"Model {cp.active_model_id} does not support a reasoning override. "
                "Use reasoning 'default'."
            )
            self.report({'WARNING'}, cp.last_error)
            _write_command_response("", cp.last_error)
            _write_ipc_status(context)
            return {'CANCELLED'}
        if reasoning != "default" and supported_reasoning and reasoning not in supported_reasoning:
            cp.last_error = (
                f"Model {cp.active_model_id} does not support reasoning '{reasoning}'. "
                f"Supported values: {', '.join(supported_reasoning)}."
            )
            self.report({'WARNING'}, cp.last_error)
            _write_command_response("", cp.last_error)
            _write_ipc_status(context)
            return {'CANCELLED'}

        cp.last_error = ""
        # Add user message to transcript
        _add_chat(cp, "user", prompt)
        cp.prompt_text = ""

        # Build API conversation from saved resume state, then append this turn.
        user_content = _build_user_content(prompt, cp)
        messages = _build_messages_for_send(cp, user_content)

        # Start thinking
        cp.is_thinking = True
        cp.thinking_text = "Copilot is thinking..."
        cp.request_count += 1

        _ensure_timer(context)

        request_holder = {"rid": 0}
        request_transcript = json.loads(json.dumps(messages, ensure_ascii=False))

        def _on_agent_event(event):
            request_id = int((event or {}).get("request_id", 0) or 0)
            holder_rid = int(request_holder.get("rid", 0) or 0)
            if holder_rid and request_id != holder_rid:
                return
            if holder_rid and COPILOT_OT_AsyncTimer._active_request_id != request_id:
                return
            _write_ipc_event(event)

        def _on_done(request_id, result):
            completed = [
                msg for msg in request_transcript
                if isinstance(msg, dict) and msg.get("role") in ("system", "user", "assistant")
            ]
            visible_chat = []
            for msg in completed:
                if msg.get("role") in ("user", "assistant"):
                    visible_chat.append({
                        "role": msg.get("role"),
                        "content": msg.get("content", ""),
                        "model_id": "",
                        "timestamp": time.time(),
                    })
            if result and not result.get("error") and result.get("content"):
                content = result.get("content", "")
                completed.append({"role": "assistant", "content": content})
                visible_chat.append({
                    "role": "assistant",
                    "content": content,
                    "model_id": result.get("model", ""),
                    "timestamp": time.time(),
                })
            elif result and result.get("error"):
                if result.get("content"):
                    completed.append({
                        "role": "assistant",
                        "content": result.get("content", ""),
                    })
                    visible_chat.append({
                        "role": "assistant",
                        "content": result.get("content", ""),
                        "model_id": result.get("model", ""),
                        "timestamp": time.time(),
                    })
                visible_chat.append({
                    "role": "system",
                    "content": f"Error: {result.get('error')}",
                    "model_id": "",
                    "timestamp": time.time(),
                })
            if completed:
                _auth.save_conversation_messages(
                    _api.prepare_messages_for_storage(completed)
                )
            if visible_chat:
                _auth.save_chat_history_messages(visible_chat)
            if request_holder.get("rid") == request_id and COPILOT_OT_AsyncTimer._active_request_id == request_id:
                _write_ipc_chat_result(result)

        effective_timeout = max(30, min(int(prefs.timeout_seconds or 600), 3600))
        effective_max_tokens = max(1024, min(int(prefs.max_output_tokens or 16384), 128000))
        if active_model and active_model.output_tokens > 0:
            effective_max_tokens = min(effective_max_tokens, active_model.output_tokens)
        configured_iterations = max(0, min(int(prefs.max_tool_iterations or 0), 5000))
        effective_iterations = configured_iterations or 40
        if reasoning:
            cp.reasoning_strength = reasoning
            prefs.cached_reasoning_strength = reasoning

        _clear_ipc_events()
        rid = _api.send_chat_async(
            api_base=cp.api_base,
            copilot_token=cp.copilot_token,
            model_id=cp.active_model_id,
            messages=messages,
            enable_tools=_active_model_supports_tools(cp),
            endpoint=_active_model_endpoint(cp),
            timeout=effective_timeout,
            max_output_tokens=effective_max_tokens,
            reasoning_effort="" if reasoning == "default" else reasoning,
            on_agent_event=_on_agent_event,
            on_done=_on_done,
            verbose=prefs.enable_verbose_logging,
            max_iterations=effective_iterations,
        )
        request_holder["rid"] = rid
        COPILOT_OT_AsyncTimer._active_request_id = rid
        COPILOT_OT_AsyncTimer._request_start_time = time.time()
        _write_ipc_status(context)

        # Clear uploads after send
        cp.pending_uploads.clear()

        return {'FINISHED'}


def _build_user_content(prompt: str, cp):
    """Build user message content, optionally with image attachments."""
    if len(cp.pending_uploads) == 0:
        return prompt

    parts = [{"type": "text", "text": prompt}]

    for upload in cp.pending_uploads:
        fpath = upload.filepath
        if not os.path.isfile(fpath):
            parts[0]["text"] += f"\n\n[Attachment not found: {upload.filename}]"
            continue

        mime, _ = mimetypes.guess_type(fpath)
        if mime and mime.startswith("image/"):
            # Base64 inline image
            fsize = os.path.getsize(fpath)
            if fsize > 4 * 1024 * 1024:
                parts[0]["text"] += f"\n\n[Image too large (>4MB): {upload.filename}]"
                continue
            with open(fpath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })
        else:
            # Text file — inline
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(24000)
                parts[0]["text"] += f"\n\n--- {upload.filename} ---\n```\n{content}\n```"
            except OSError:
                parts[0]["text"] += f"\n\n[Failed to read: {upload.filename}]"

    return parts


# ── Clear Chat ───────────────────────────────────────────────────────────

class COPILOT_OT_ClearChat(Operator):
    """Clear the chat transcript."""
    bl_idname = "copilot.clear_chat"
    bl_label = "Clear Chat"

    def execute(self, context):
        cp = _get_cp(context)
        if COPILOT_OT_AsyncTimer._active_request_id > 0:
            cp.last_error = "Cannot clear the conversation while a request is running. Use /cancel first."
            self.report({'WARNING'}, cp.last_error)
            _write_ipc_status(context)
            return {'CANCELLED'}
        had_saved_turns = any(msg.role in ("user", "assistant") for msg in cp.chat_history)
        if had_saved_turns:
            _auth.create_conversation_session("New chat")
        cp.chat_history.clear()
        cp.tool_log = ""
        cp.conversation_json = "[]"
        cp.last_error = ""
        _add_chat(cp, "system", "Chat cleared.")
        _write_ipc_status(context)
        return {'FINISHED'}


# ── Upload Files ─────────────────────────────────────────────────────────

class COPILOT_OT_UploadFiles(Operator):
    """Attach files to the next message."""
    bl_idname = "copilot.upload_files"
    bl_label = "Upload Files"

    filepath: StringProperty(subtype='FILE_PATH')
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    filter_glob: StringProperty(
        default="*.*",
        options={'HIDDEN'},
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        cp = _get_cp(context)
        for f in self.files:
            fpath = os.path.join(self.directory, f.name)
            item = cp.pending_uploads.add()
            item.filepath = fpath
            item.filename = f.name
        _add_chat(cp, "system", f"Attached {len(self.files)} file(s)")
        return {'FINISHED'}


class COPILOT_OT_ClearUploads(Operator):
    """Clear all pending file attachments."""
    bl_idname = "copilot.clear_uploads"
    bl_label = "Clear Uploads"

    def execute(self, context):
        cp = _get_cp(context)
        cp.pending_uploads.clear()
        return {'FINISHED'}


# ── Action buttons ───────────────────────────────────────────────────────

class COPILOT_OT_AnalyzeScene(Operator):
    """Ask Copilot to analyze the current Blender scene."""
    bl_idname = "copilot.analyze_scene"
    bl_label = "Analyze Scene"

    def execute(self, context):
        cp = _get_cp(context)
        cp.prompt_text = (
            "Analyze the current Blender scene. List all objects, their types, materials, "
            "modifiers, and overall scene structure. Suggest improvements or issues."
        )
        bpy.ops.copilot.send_chat()
        return {'FINISHED'}


class COPILOT_OT_GenerateScript(Operator):
    """Ask Copilot to generate a Blender Python script."""
    bl_idname = "copilot.generate_script"
    bl_label = "Generate Script"

    def execute(self, context):
        cp = _get_cp(context)
        if not cp.prompt_text.strip():
            cp.prompt_text = "Generate a Blender Python script that "
            self.report({'INFO'}, "Type what the script should do, then send")
            return {'CANCELLED'}
        # Prefix the prompt
        cp.prompt_text = f"Generate a Blender Python script: {cp.prompt_text}"
        bpy.ops.copilot.send_chat()
        return {'FINISHED'}


class COPILOT_OT_CreateObject(Operator):
    """Ask Copilot to create a 3D object or scene."""
    bl_idname = "copilot.create_object"
    bl_label = "Create Object"

    def execute(self, context):
        cp = _get_cp(context)
        if not cp.prompt_text.strip():
            cp.prompt_text = "Create a "
            self.report({'INFO'}, "Describe the object to create, then send")
            return {'CANCELLED'}
        cp.prompt_text = (
            f"Create the following in the Blender scene using tools (create_mesh, "
            f"create_material, add_modifier, or execute_python_script for complex geometry): "
            f"{cp.prompt_text}"
        )
        bpy.ops.copilot.send_chat()
        return {'FINISHED'}


class COPILOT_OT_ExplainSelected(Operator):
    """Ask Copilot to explain the selected object(s)."""
    bl_idname = "copilot.explain_selected"
    bl_label = "Explain Selected"

    def execute(self, context):
        cp = _get_cp(context)
        selected = [obj.name for obj in context.selected_objects]
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        cp.prompt_text = (
            f"Use get_scene_info to examine these selected objects: {', '.join(selected)}. "
            f"Explain their properties, materials, modifiers, and suggest improvements."
        )
        bpy.ops.copilot.send_chat()
        return {'FINISHED'}


class COPILOT_OT_SuggestMaterial(Operator):
    """Ask Copilot to suggest and create materials for selected objects."""
    bl_idname = "copilot.suggest_material"
    bl_label = "Suggest Material"

    def execute(self, context):
        cp = _get_cp(context)
        selected = [obj.name for obj in context.selected_objects]
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        cp.prompt_text = (
            f"Create and assign appropriate PBR materials for these objects: "
            f"{', '.join(selected)}. Use create_material tool with realistic settings."
        )
        bpy.ops.copilot.send_chat()
        return {'FINISHED'}


class COPILOT_OT_CopyResponse(Operator):
    """Copy the last assistant response to clipboard."""
    bl_idname = "copilot.copy_response"
    bl_label = "Copy Last Response"

    def execute(self, context):
        cp = _get_cp(context)
        for msg in reversed(list(cp.chat_history)):
            if msg.role == "assistant":
                context.window_manager.clipboard = msg.content
                self.report({'INFO'}, "Copied to clipboard")
                return {'FINISHED'}
        self.report({'WARNING'}, "No response to copy")
        return {'CANCELLED'}


# ── Classes to register ──────────────────────────────────────────────────

_classes = [
    COPILOT_OT_AsyncTimer,
    COPILOT_OT_SignIn,
    COPILOT_OT_SignOut,
    COPILOT_OT_RefreshModels,
    COPILOT_OT_SelectModel,
    COPILOT_OT_SendChat,
    COPILOT_OT_ClearChat,
    COPILOT_OT_UploadFiles,
    COPILOT_OT_ClearUploads,
    COPILOT_OT_AnalyzeScene,
    COPILOT_OT_GenerateScript,
    COPILOT_OT_CreateObject,
    COPILOT_OT_ExplainSelected,
    COPILOT_OT_SuggestMaterial,
    COPILOT_OT_CopyResponse,
]


def register():
    global _startup_restore_done
    _startup_restore_done = False
    for cls in _classes:
        bpy.utils.register_class(cls)
    try:
        _start_ipc_cancel_watcher()
        _ensure_timer(bpy.context)
        if not bpy.app.timers.is_registered(_startup_restore_tick):
            bpy.app.timers.register(_startup_restore_tick, first_interval=0.5, persistent=True)
    except Exception as e:
        print(f"[CopilotTimer] Failed to start app timer on register: {e}")


def unregister():
    _stop_ipc_cancel_watcher()
    try:
        if bpy.app.timers.is_registered(_startup_restore_tick):
            bpy.app.timers.unregister(_startup_restore_tick)
        if bpy.app.timers.is_registered(_app_timer_tick):
            bpy.app.timers.unregister(_app_timer_tick)
    except Exception as e:
        print(f"[CopilotTimer] Failed to unregister app timer: {e}")
    _write_ipc_shutdown()
    _terminate_chat_console()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
