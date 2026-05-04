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
    "User-Agent": "GitHubCopilotChat/0.27.1",
    "Editor-Version": "vscode/1.103.2",
    "Editor-Plugin-Version": "copilot-chat/0.27.1",
    "X-GitHub-Api-Version": "2025-04-01",
}

_TOKEN_CACHE_FILENAME = "copilot_token_cache.json"
_lock = threading.Lock()


def _get_cache_dir():
    """Platform-appropriate cache directory."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "github-copilot-blender")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path():
    return os.path.join(_get_cache_dir(), _TOKEN_CACHE_FILENAME)


def save_token_cache(data: dict):
    """Persist token data to disk."""
    with _lock:
        try:
            with open(_cache_path(), "w") as f:
                json.dump(data, f, indent=2)
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


# ── Chat history persistence ─────────────────────────────────────────────────

_HISTORY_CACHE_FILENAME = "copilot_chat_history.json"
_history_lock = threading.Lock()


def _history_path():
    return os.path.join(_get_cache_dir(), _HISTORY_CACHE_FILENAME)


def save_chat_history(chat_history):
    """Serialize a Blender CollectionProperty of CopilotChatMessage to disk."""
    import time as _time
    messages = []
    for msg in chat_history:
        messages.append({
            "role": msg.role,
            "content": msg.content,
            "model_id": msg.model_id,
            "timestamp": msg.timestamp,
        })
    data = {"version": 1, "saved_at": _time.time(), "messages": messages}
    with _history_lock:
        try:
            with open(_history_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print(f"[CopilotAuth] Failed to save chat history: {e}")


def load_chat_history() -> list:
    """Load chat history from disk. Returns list of dicts."""
    with _history_lock:
        try:
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
            p = _history_path()
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
