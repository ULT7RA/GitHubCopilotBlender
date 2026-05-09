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
from . import tool_executor as _executor
from .preferences import get_prefs


# ── Helpers ───────────────────────────────────────────────────────────────

def _get_cp(context):
    """Return the scene copilot properties."""
    return context.scene.copilot


def _add_chat(cp, role, content, model_id=""):
    msg = cp.chat_history.add()
    msg.role = role
    msg.content = content
    msg.model_id = model_id
    msg.timestamp = time.time()
    print(f"[CopilotIPC] _add_chat role={role} len={len(content)} model={model_id}")
    # Auto-save chat history to disk after every message
    _auth.save_chat_history(cp.chat_history)

    # Print to the Blender console so you can actually read it
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
_STATUS_FILE = os.path.join(_IPC_DIR, "status.json")
print(f"[CopilotIPC] IPC_DIR = {_IPC_DIR}")
print(f"[CopilotIPC] RESPONSE_FILE = {_RESPONSE_FILE}")
_chat_log_path = os.path.join(tempfile.gettempdir(), "copilot_blender_chat.log")
_console_proc = None


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
    data = {
        "connected": True,
        "username": cp.username,
        "active_model": cp.active_model_id,
        "model_count": len(cp.available_models),
        "timestamp": time.time(),
    }
    try:
        with open(_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _write_ipc_response(result):
    """Write a chat response for the console to pick up."""
    try:
        os.makedirs(_IPC_DIR, exist_ok=True)
        payload = json.dumps(result, ensure_ascii=False)
        with open(_RESPONSE_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        print(f"[CopilotIPC] OK wrote response.json ({len(payload)} bytes)")
    except Exception as e:
        print(f"[CopilotIPC] FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


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
    for f in (_PROMPT_FILE, _RESPONSE_FILE):
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


def _restore_chat_history(cp):
    """Load saved chat history from disk into scene properties."""
    saved = _auth.load_chat_history()
    if not saved:
        return
    # Don't duplicate — only load if chat_history is empty or has just the
    # system "Restored session" message
    if len(cp.chat_history) > 1:
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


# ── Modal timer operator (drives async results + main-thread queue) ──────

class COPILOT_OT_AsyncTimer(Operator):
    """Background modal timer that polls for async chat results and drains main-thread queue."""
    bl_idname = "copilot.async_timer"
    bl_label = "Copilot Async Timer"
    bl_options = {'INTERNAL'}

    _timer = None
    _active_request_id: int = 0
    _is_running: bool = False
    _request_start_time: float = 0.0

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Drain Blender main-thread queue (tool execution)
        _executor.drain_main_queue()

        # Proactively refresh token to prevent expiry during tool loops
        _ensure_token(context)

        # Check for prompts from the external chat console
        ipc_prompt = _check_ipc_prompt(context)
        if ipc_prompt and COPILOT_OT_AsyncTimer._active_request_id == 0:
            action = ipc_prompt.get("action", "chat")
            prompt = ipc_prompt.get("prompt", "")
            if action == "chat" and prompt:
                cp = _get_cp(context)
                cp.prompt_text = prompt
                bpy.ops.copilot.send_chat()
            elif action == "clear":
                bpy.ops.copilot.clear_chat()
            elif action == "refresh_models":
                bpy.ops.copilot.refresh_models()

        # Check for completed async chat
        if COPILOT_OT_AsyncTimer._active_request_id > 0:
            status = _api.get_chat_result(COPILOT_OT_AsyncTimer._active_request_id)
            _dbg_path = os.path.join(_IPC_DIR, "debug_timer.log")
            with open(_dbg_path, "a") as _df:
                _df.write(f"{time.time():.1f} rid={COPILOT_OT_AsyncTimer._active_request_id} status={status['status']}\n")
                _df.flush()
            if status["status"] == "done":
                result = status["result"]
                cp = _get_cp(context)
                cp.is_thinking = False

                if result and result.get("error"):
                    _add_chat(cp, "system", f"Error: {result['error']}")
                elif result:
                    model_label = result.get("model", "API:model-missing")
                    content = result.get("content", "(no response)")
                    _add_chat(cp, "assistant", content, model_id=model_label)

                    # Append tool log and extract last render path
                    if result.get("tool_log"):
                        cp.tool_log = "\n".join(result["tool_log"])
                        for entry in reversed(result["tool_log"]):
                            if "__RENDER_IMAGE__:" in entry:
                                # Extract path from tool log entry
                                idx = entry.find("__RENDER_IMAGE__:")
                                path = entry[idx + 17:].split("\n")[0].split("→")[0].strip()
                                if os.path.isfile(path):
                                    cp.last_render_path = path
                                    _load_render_preview(path)
                                break

                _api.clear_chat_result(COPILOT_OT_AsyncTimer._active_request_id)
                COPILOT_OT_AsyncTimer._active_request_id = 0
                COPILOT_OT_AsyncTimer._request_start_time = 0.0

                # Redraw UI
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            elif (COPILOT_OT_AsyncTimer._request_start_time > 0 and
                  time.time() - COPILOT_OT_AsyncTimer._request_start_time > 300):
                # Safety timeout: if request is stuck for 5 minutes, cancel it
                cp = _get_cp(context)
                cp.is_thinking = False
                _add_chat(cp, "system", "Error: Request timed out (5 min)")
                _api.clear_chat_result(COPILOT_OT_AsyncTimer._active_request_id)
                COPILOT_OT_AsyncTimer._active_request_id = 0
                COPILOT_OT_AsyncTimer._request_start_time = 0.0

        # Keep running — always poll for console input and pending requests
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
    """Start the async timer if not already running."""
    if not COPILOT_OT_AsyncTimer._is_running:
        bpy.ops.copilot.async_timer('INVOKE_DEFAULT')


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
            _add_chat(cp, "system", f"Restored session for {cp.username}")
            # Restore previous chat history from disk
            _restore_chat_history(cp)
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
                _add_chat(cp, "system", f"Signed in as {username} ({cp.sku})")
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
        prefs.cached_oauth_token = ""
        prefs.cached_username = ""
        prefs.cached_active_model = ""
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

        def _fetch():
            print(f"[CopilotModels] Fetching models from {cp.api_base}...")
            try:
                models = _api.fetch_models(cp.api_base, cp.copilot_token)
                print(f"[CopilotModels] Got {len(models)} models")
            except Exception as e:
                print(f"[CopilotModels] FETCH FAILED: {e}")
                models = []
            def _update():
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

                _add_chat(cp, "system", f"Loaded {len(cp.available_models)} models. Active: {cp.active_model_id}")
                _write_ipc_status(bpy.context)

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

    def execute(self, context):
        cp = _get_cp(context)
        prefs = get_prefs(context)
        cp.active_model_id = self.model_id
        prefs.cached_active_model = self.model_id
        for i, item in enumerate(cp.available_models):
            if item.model_id == self.model_id:
                cp.active_model_index = i
                break
        _add_chat(cp, "system", f"Model set to: {self.model_id}")
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

        if not _ensure_token(context):
            self.report({'WARNING'}, "Not authenticated — sign in first")
            return {'CANCELLED'}

        if not cp.active_model_id:
            self.report({'WARNING'}, "No model selected — refresh models first")
            return {'CANCELLED'}

        # Add user message to transcript
        display_name = prefs.user_handle or cp.username or "You"
        _add_chat(cp, "user", prompt)
        cp.prompt_text = ""

        # Build messages
        messages = [{"role": "system", "content": _api.SYSTEM_PROMPT}]

        # Conversation history (last N messages for context)
        for msg in cp.chat_history:
            if msg.role in ("user", "assistant"):
                messages.append({"role": msg.role, "content": msg.content})

        # Handle attachments
        user_content = _build_user_content(prompt, cp)
        messages[-1] = {"role": "user", "content": user_content} if isinstance(user_content, list) else messages[-1]

        # Start thinking
        cp.is_thinking = True
        cp.thinking_text = "Copilot is thinking..."
        cp.request_count += 1

        _ensure_timer(context)

        rid = _api.send_chat_async(
            api_base=cp.api_base,
            copilot_token=cp.copilot_token,
            model_id=cp.active_model_id,
            messages=messages,
            enable_tools=True,
            timeout=prefs.timeout_seconds,
            max_output_tokens=prefs.max_output_tokens,
            verbose=prefs.enable_verbose_logging,
            max_iterations=prefs.max_tool_iterations,
        )
        COPILOT_OT_AsyncTimer._active_request_id = rid
        COPILOT_OT_AsyncTimer._request_start_time = time.time()

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
        cp.chat_history.clear()
        cp.tool_log = ""
        _auth.clear_chat_history()
        _add_chat(cp, "system", "Chat cleared.")
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
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
