"""
OAuth device-flow authentication for GitHub Copilot.
Handles: device code request → user authorization → token exchange → Copilot session token.
Token caching to disk for persistence across Blender sessions.
"""

import json
import os
import time
import uuid
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# ── GitHub OAuth constants (official Copilot client) ──────────────────────
CLIENT_ID = "Iv1.b507a08c87ecfe98"
SCOPE = "read:user"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
GRAPHQL_URL = "https://api.github.com/graphql"
DEFAULT_API_BASE = "https://api.githubcopilot.com"

# Editor identity headers (required by Copilot API)
EDITOR_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.43.2026040602",
    "Editor-Version": "vscode/1.100.2",
    "Editor-Plugin-Version": "copilot-chat/0.27.1",
    "X-GitHub-Api-Version": "2025-04-01",
}

_TOKEN_CACHE_FILENAME = "copilot_token_cache.json"
_DOCS_CONFIG_FILENAME = "copilot_docs_config.json"
_lock = threading.Lock()


def _atomic_json_write(path: str, data):
    """Write JSON in the destination directory, then atomically replace the target."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _get_cache_dir():
    """Platform-appropriate cache directory."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "github-copilot-blender")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path():
    return os.path.join(_get_cache_dir(), _TOKEN_CACHE_FILENAME)


def _docs_config_path():
    return os.path.join(_get_cache_dir(), _DOCS_CONFIG_FILENAME)


def get_default_blender_docs_path() -> str:
    """Default drop-in folder for downloaded Blender docs shipped with addon."""
    return os.path.join(os.path.dirname(__file__), "blender-docs")


def get_effective_blender_docs_path() -> str:
    """Docs path used by tools: explicit config, then default drop-in folder."""
    configured = load_blender_docs_path()
    return configured or get_default_blender_docs_path()


def save_token_cache(data: dict):
    """Persist token data to disk."""
    with _lock:
        try:
            _atomic_json_write(_cache_path(), data)
        except OSError as e:
            print(f"[CopilotAuth] Failed to save token cache: {e}")


def load_token_cache() -> dict:
    """Load cached token data from disk."""
    with _lock:
        try:
            p = _cache_path()
            if os.path.exists(p):
                with open(p, "r") as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[CopilotAuth] Failed to load token cache: {e}")
    return {}


def clear_token_cache():
    """Delete cached tokens (sign-out)."""
    with _lock:
        try:
            p = _cache_path()
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def save_blender_docs_path(path: str):
    """Persist a local Blender documentation path for retrieval tools."""
    data = {"blender_docs_path": path, "saved_at": time.time()}
    with _lock:
        try:
            _atomic_json_write(_docs_config_path(), data)
        except OSError as e:
            print(f"[CopilotAuth] Failed to save docs path: {e}")


def load_blender_docs_path() -> str:
    """Load configured local Blender documentation path, if any."""
    with _lock:
        try:
            p = _docs_config_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return str(data.get("blender_docs_path", "") or "")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[CopilotAuth] Failed to load docs path: {e}")
    return ""


def clear_blender_docs_path():
    """Delete configured local Blender documentation path."""
    with _lock:
        try:
            p = _docs_config_path()
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ── Chat history persistence ─────────────────────────────────────────────────

_HISTORY_CACHE_FILENAME = "copilot_chat_history.json"
_CONVERSATION_CACHE_FILENAME = "copilot_conversation.json"
_SESSIONS_DIRNAME = "conversations"
_ACTIVE_SESSION_FILENAME = "copilot_active_session.json"
_history_lock = threading.RLock()


def _history_path():
    return os.path.join(_get_cache_dir(), _HISTORY_CACHE_FILENAME)


def _conversation_path():
    return os.path.join(_get_cache_dir(), _CONVERSATION_CACHE_FILENAME)


def _sessions_dir():
    d = os.path.join(_get_cache_dir(), _SESSIONS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _safe_session_id(session_id: str) -> str:
    session_id = str(session_id or "").strip()
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_", "."))
    return safe or "default"


def _active_session_path():
    return os.path.join(_get_cache_dir(), _ACTIVE_SESSION_FILENAME)


def _session_path(session_id: str = None):
    return os.path.join(_sessions_dir(), f"{_safe_session_id(session_id or get_active_session_id())}.json")


def _content_preview(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts).strip()
    return ""


def _title_from_history(messages: list, fallback: str = "New chat") -> str:
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            title = _content_preview(msg.get("content", ""))
            if title:
                title = " ".join(title.split())
                return title[:80]
    return fallback


def get_active_session_id() -> str:
    session_id = "default"
    with _history_lock:
        try:
            p = _active_session_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                session_id = _safe_session_id(data.get("active_session_id", "")) or "default"
                if session_id:
                    return session_id
        except (OSError, json.JSONDecodeError) as e:
            print(f"[CopilotAuth] Failed to load active conversation: {e}")
        try:
            _atomic_json_write(_active_session_path(), {"active_session_id": session_id})
        except OSError as e:
            print(f"[CopilotAuth] Failed to save active conversation: {e}")
    return session_id


def set_active_session_id(session_id: str):
    session_id = _safe_session_id(session_id)
    with _history_lock:
        try:
            _atomic_json_write(_active_session_path(), {"active_session_id": session_id})
        except OSError as e:
            print(f"[CopilotAuth] Failed to save active conversation: {e}")
    return session_id


def _legacy_session_data(session_id: str) -> dict:
    if _safe_session_id(session_id) != "default":
        return {}

    data = {
        "version": 1,
        "id": "default",
        "title": "Default chat",
        "created_at": 0,
        "updated_at": 0,
        "chat_history": [],
        "conversation_messages": [],
    }
    try:
        if os.path.exists(_history_path()):
            with open(_history_path(), "r", encoding="utf-8") as f:
                history = json.load(f)
            data["chat_history"] = history.get("messages", [])
            data["updated_at"] = max(data["updated_at"], history.get("saved_at", 0))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[CopilotAuth] Failed to load legacy chat history: {e}")

    try:
        if os.path.exists(_conversation_path()):
            with open(_conversation_path(), "r", encoding="utf-8") as f:
                conversation = json.load(f)
            data["conversation_messages"] = conversation.get("messages", [])
            data["updated_at"] = max(data["updated_at"], conversation.get("saved_at", 0))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[CopilotAuth] Failed to load legacy conversation: {e}")

    data["title"] = _title_from_history(data["chat_history"], data["title"])
    return data if data["chat_history"] or data["conversation_messages"] else {}


def _load_session_data(session_id: str = None) -> dict:
    session_id = _safe_session_id(session_id or get_active_session_id())
    p = _session_path(session_id)
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("id", session_id)
                data.setdefault("chat_history", [])
                data.setdefault("conversation_messages", [])
                return data
    except (OSError, json.JSONDecodeError) as e:
        print(f"[CopilotAuth] Failed to load conversation session {session_id}: {e}")
    return _legacy_session_data(session_id)


def _write_session_data(data: dict):
    session_id = _safe_session_id(data.get("id", get_active_session_id()))
    now = time.time()
    data["id"] = session_id
    data.setdefault("version", 1)
    data.setdefault("created_at", now)
    data["updated_at"] = now
    data.setdefault("title", _title_from_history(data.get("chat_history", [])))
    data.setdefault("chat_history", [])
    data.setdefault("conversation_messages", [])
    _atomic_json_write(_session_path(session_id), data)


def _save_session_fields(fields: dict):
    with _history_lock:
        session_id = get_active_session_id()
        data = _load_session_data(session_id) or {"id": session_id}
        data.update(fields)
        history = data.get("chat_history", [])
        current_title = data.get("title", "")
        if not current_title or current_title in ("New chat", "Default chat"):
            data["title"] = _title_from_history(history, current_title or "New chat")
        _write_session_data(data)


def create_conversation_session(title: str = "") -> str:
    session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    title = " ".join(str(title or "").split())[:80] or "New chat"
    with _history_lock:
        data = {
            "version": 1,
            "id": session_id,
            "title": title,
            "created_at": time.time(),
            "updated_at": time.time(),
            "chat_history": [],
            "conversation_messages": [],
        }
        _write_session_data(data)
        set_active_session_id(session_id)
    return session_id


def switch_conversation_session(session_id: str) -> bool:
    session_id = _safe_session_id(session_id)
    with _history_lock:
        if not os.path.exists(_session_path(session_id)) and not _legacy_session_data(session_id):
            return False
        set_active_session_id(session_id)
        return True


def list_conversation_sessions() -> list:
    sessions = {}
    active = get_active_session_id()
    with _history_lock:
        try:
            for name in os.listdir(_sessions_dir()):
                if not name.endswith(".json"):
                    continue
                session_id = name[:-5]
                data = _load_session_data(session_id)
                if not data:
                    continue
                sessions[session_id] = {
                    "id": session_id,
                    "title": data.get("title") or _title_from_history(data.get("chat_history", [])),
                    "updated_at": data.get("updated_at", 0),
                    "message_count": len(data.get("chat_history", [])),
                    "active": session_id == active,
                }
        except OSError as e:
            print(f"[CopilotAuth] Failed to list conversations: {e}")

        legacy = _legacy_session_data("default")
        if legacy and "default" not in sessions:
            sessions["default"] = {
                "id": "default",
                "title": legacy.get("title", "Default chat"),
                "updated_at": legacy.get("updated_at", 0),
                "message_count": len(legacy.get("chat_history", [])),
                "active": active == "default",
            }

    return sorted(sessions.values(), key=lambda item: item.get("updated_at", 0), reverse=True)


def save_chat_history(chat_history):
    """Serialize a Blender CollectionProperty of CopilotChatMessage to disk."""
    messages = []
    for msg in chat_history:
        messages.append({
            "role": msg.role,
            "content": msg.content,
            "model_id": msg.model_id,
            "timestamp": msg.timestamp,
        })
    save_chat_history_messages(messages)


def _conversation_turn_count(messages: list) -> int:
    return sum(1 for msg in messages or [] if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"))


def _message_key(msg: dict):
    if not isinstance(msg, dict):
        return ("", "")
    return (str(msg.get("role", "") or ""), _content_preview(msg.get("content", "")))


def _merge_message_history(existing: list, incoming: list) -> list:
    """Merge a shorter in-memory transcript into saved history without clobbering it."""
    existing = [msg for msg in (existing or []) if isinstance(msg, dict)]
    incoming = [msg for msg in (incoming or []) if isinstance(msg, dict)]
    if not existing:
        return incoming
    if not incoming:
        return existing

    system_msg = None
    if incoming and incoming[0].get("role") == "system":
        system_msg = incoming[0]
        incoming = incoming[1:]
    elif existing and existing[0].get("role") == "system":
        system_msg = existing[0]

    existing_body = existing[1:] if existing and existing[0].get("role") == "system" else existing
    incoming_body = incoming
    if not incoming_body:
        merged = existing_body
    else:
        existing_keys = [_message_key(msg) for msg in existing_body]
        incoming_keys = [_message_key(msg) for msg in incoming_body]

        if len(incoming_keys) <= len(existing_keys) and (
            incoming_keys == existing_keys[:len(incoming_keys)]
            or incoming_keys == existing_keys[-len(incoming_keys):]
        ):
            merged = existing_body
        elif len(existing_keys) <= len(incoming_keys) and existing_keys == incoming_keys[:len(existing_keys)]:
            merged = incoming_body
        else:
            overlap = 0
            for size in range(min(len(existing_keys), len(incoming_keys)), 0, -1):
                if existing_keys[-size:] == incoming_keys[:size]:
                    overlap = size
                    break
            merged = existing_body + incoming_body[overlap:]

    return ([system_msg] if system_msg else []) + merged


def save_chat_history_messages(messages: list):
    """Persist visible chat messages without letting status-only state clobber a session."""
    import time as _time
    normalized = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        normalized.append({
            "role": str(msg.get("role", "user") or "user"),
            "content": str(msg.get("content", "") or ""),
            "model_id": str(msg.get("model_id", "") or ""),
            "timestamp": float(msg.get("timestamp", 0.0) or 0.0),
        })

    new_turns = _conversation_turn_count(normalized)
    if new_turns == 0:
        return

    with _history_lock:
        try:
            existing = _load_session_data(get_active_session_id()) or {}
            existing_history = existing.get("chat_history", [])
            existing_turns = _conversation_turn_count(existing_history)
            if existing_turns > new_turns:
                normalized = _merge_message_history(existing_history, normalized)
            data = {"version": 1, "saved_at": _time.time(), "messages": normalized}
            _save_session_fields({"chat_history": normalized})
            _atomic_json_write(_history_path(), data)
        except OSError as e:
            print(f"[CopilotAuth] Failed to save chat history: {e}")


def load_chat_history() -> list:
    """Load chat history from disk. Returns list of dicts."""
    with _history_lock:
        try:
            session = _load_session_data()
            if session:
                return session.get("chat_history", [])
            p = _history_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("messages", [])
        except (OSError, json.JSONDecodeError) as e:
            print(f"[CopilotAuth] Failed to load chat history: {e}")
    return []


def clear_chat_history():
    """Delete saved chat history."""
    with _history_lock:
        try:
            _save_session_fields({"chat_history": [], "title": "New chat"})
            p = _history_path()
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def save_conversation_messages(messages: list):
    """Persist the full API conversation used for context resume."""
    new_turns = _conversation_turn_count(messages)
    if new_turns == 0:
        return
    with _history_lock:
        try:
            existing = _load_session_data(get_active_session_id()) or {}
            existing_messages = existing.get("conversation_messages", [])
            existing_turns = _conversation_turn_count(existing_messages)
            if existing_turns > new_turns:
                messages = _merge_message_history(existing_messages, messages)
            data = {"version": 1, "saved_at": time.time(), "messages": messages}
            _save_session_fields({"conversation_messages": messages})
            _atomic_json_write(_conversation_path(), data)
        except OSError as e:
            print(f"[CopilotAuth] Failed to save conversation: {e}")


def load_conversation_messages() -> list:
    """Load the full API conversation used for context resume."""
    with _history_lock:
        try:
            session = _load_session_data()
            if session:
                messages = session.get("conversation_messages", [])
                return messages if isinstance(messages, list) else []
            p = _conversation_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                messages = data.get("messages", [])
                return messages if isinstance(messages, list) else []
        except (OSError, json.JSONDecodeError) as e:
            print(f"[CopilotAuth] Failed to load conversation: {e}")
    return []


def clear_conversation_messages():
    """Delete saved API conversation context."""
    with _history_lock:
        try:
            _save_session_fields({"conversation_messages": []})
            p = _conversation_path()
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _http_json(url: str, data: dict = None, headers: dict = None, method: str = "POST") -> dict:
    """Simple JSON HTTP helper using stdlib urllib."""
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    hdrs.update(EDITOR_HEADERS)
    if headers:
        hdrs.update(headers)

    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(url, data=body, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text)
        except json.JSONDecodeError:
            return {"error": body_text, "http_status": e.code}
    except URLError as e:
        return {"error": str(e.reason)}


# ── Step 1: Request device code ───────────────────────────────────────────
def request_device_code() -> dict:
    """
    POST /login/device/code → returns device_code, user_code, verification_uri, interval.
    """
    result = _http_json(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": SCOPE})
    return result


# ── Step 2: Poll for OAuth access token ───────────────────────────────────
def poll_for_access_token(device_code: str, interval: int = 5,
                          timeout: int = 900, callback=None):
    """
    Blocking poll (run in a thread). Calls callback(token_data, error) on completion.
    token_data = {"access_token": "ghu_...", ...} on success.
    """
    start = time.time()
    while time.time() - start < timeout:
        result = _http_json(ACCESS_TOKEN_URL, {
            "client_id": CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })

        if "access_token" in result:
            if callback:
                callback(result, None)
            return result

        error = result.get("error", "")
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        elif error == "slow_down":
            interval = min(interval + 5, 30)
            time.sleep(interval)
            continue
        elif error == "expired_token":
            if callback:
                callback(None, "Device code expired. Please try again.")
            return None
        elif error == "access_denied":
            if callback:
                callback(None, "Authorization denied by user.")
            return None
        else:
            if callback:
                callback(None, f"Unexpected error: {result}")
            return None

    if callback:
        callback(None, "Timed out waiting for authorization.")
    return None


# ── Step 3: Exchange for Copilot session token ────────────────────────────
def fetch_copilot_token(oauth_token: str) -> dict:
    """
    GET /copilot_internal/v2/token → session token with endpoints, capabilities.
    Returns dict with keys: token, expires_at, refresh_in, endpoints, chat_enabled, sku, ...
    """
    req = Request(COPILOT_TOKEN_URL, headers={
        "Authorization": f"token {oauth_token}",
        "Accept": "application/json",
        **EDITOR_HEADERS,
    }, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return {"error": body_text, "http_status": e.code}
    except URLError as e:
        return {"error": str(e.reason)}


def fetch_username(oauth_token: str) -> str:
    """Fetch GitHub username via GraphQL API."""
    query = '{"query": "query { viewer { login } }"}'
    req = Request(GRAPHQL_URL, data=query.encode("utf-8"), headers={
        "Authorization": f"bearer {oauth_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **EDITOR_HEADERS,
    }, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", {}).get("viewer", {}).get("login", "")
    except Exception:
        return ""


# ── Token refresh logic ──────────────────────────────────────────────────
def ensure_valid_copilot_token(oauth_token: str, current_token: str,
                                expires_at: float) -> dict:
    """
    If Copilot session token is expired or about to expire (within 60s),
    fetch a new one. Returns updated token dict or None on failure.
    """
    now = time.time()
    if current_token and expires_at > now + 60:
        return None  # Still valid

    result = fetch_copilot_token(oauth_token)
    if "token" in result:
        return result
    return None


# ── Full sign-in flow (threaded) ─────────────────────────────────────────
def start_device_flow(on_code_ready, on_complete, on_error):
    """
    Non-blocking device flow. Callbacks are called from background thread —
    caller must use Blender's thread-safe mechanisms to update UI.

    on_code_ready(user_code: str, verification_uri: str)
    on_complete(oauth_token: str, username: str, copilot_token_data: dict)
    on_error(message: str)
    """

    def _flow():
        try:
            # Step 1: Get device code
            dc = request_device_code()
            if "error" in dc:
                on_error(f"Device code request failed: {dc.get('error')}")
                return

            user_code = dc.get("user_code", "")
            verification_uri = dc.get("verification_uri", "https://github.com/login/device")
            device_code = dc.get("device_code", "")
            interval = dc.get("interval", 5)

            on_code_ready(user_code, verification_uri)

            # Step 2: Poll for OAuth token
            token_result = poll_for_access_token(device_code, interval)
            if not token_result or "access_token" not in token_result:
                on_error("Failed to obtain OAuth token.")
                return

            oauth_token = token_result["access_token"]

            # Step 3: Get username
            username = fetch_username(oauth_token)

            # Step 4: Get Copilot session token
            copilot_data = fetch_copilot_token(oauth_token)
            if "error" in copilot_data:
                on_error(f"Copilot token failed: {copilot_data.get('error')}")
                return

            # Cache to disk
            cache = {
                "oauth_token": oauth_token,
                "username": username,
                "copilot_token": copilot_data.get("token", ""),
                "expires_at": copilot_data.get("expires_at", 0),
                "api_base": copilot_data.get("endpoints", {}).get("api", DEFAULT_API_BASE),
                "sku": copilot_data.get("sku", ""),
                "chat_enabled": copilot_data.get("chat_enabled", False),
            }
            save_token_cache(cache)

            on_complete(oauth_token, username, copilot_data)

        except Exception as e:
            on_error(f"Auth flow exception: {e}")

    t = threading.Thread(target=_flow, daemon=True)
    t.start()
    return t


def try_restore_session() -> dict:
    """
    Try to restore a previous session from disk cache.
    Returns cache dict if valid, empty dict if not.
    """
    cache = load_token_cache()
    if not cache or not cache.get("oauth_token"):
        return {}

    # Check if Copilot token is still valid
    expires_at = cache.get("expires_at", 0)
    if expires_at > time.time() + 60:
        return cache

    # Try to refresh
    refreshed = fetch_copilot_token(cache["oauth_token"])
    if "token" in refreshed:
        cache["copilot_token"] = refreshed["token"]
        cache["expires_at"] = refreshed.get("expires_at", 0)
        cache["api_base"] = refreshed.get("endpoints", {}).get("api", cache.get("api_base", DEFAULT_API_BASE))
        save_token_cache(cache)
        return cache

    return {}
