"""
Standalone Copilot Chat Console — runs in its own terminal window.
Communicates with the Blender addon via a shared JSON command file.

Blender writes auth credentials + responses to the shared file.
This console reads input from the user and writes prompts back.
"""

import json
import os
import sys
import time
import threading
import shutil
if os.name == "nt":
    import msvcrt
else:
    msvcrt = None

SHARED_DIR = os.path.join(os.environ.get("TEMP", "/tmp"), "copilot_blender_ipc")
PROMPT_FILE = os.path.join(SHARED_DIR, "prompt.json")
RESPONSE_FILE = os.path.join(SHARED_DIR, "response.json")
COMMAND_RESPONSE_FILE = os.path.join(SHARED_DIR, "command_response.json")
EVENTS_FILE = os.path.join(SHARED_DIR, "events.jsonl")
STATUS_FILE = os.path.join(SHARED_DIR, "status.json")
SHUTDOWN_FILE = os.path.join(SHARED_DIR, "shutdown.json")
CANCEL_FILE = os.path.join(SHARED_DIR, "cancel.json")

SEP = "=" * 72
_chat_active = threading.Event()
_stop_event = threading.Event()
_chat_lock = threading.Lock()
_queued_chats = []
_console_lock = threading.RLock()
_ipc_write_lock = threading.Lock()
_prompt_active = False
_input_buffer = ""
_last_prompt_lines = 0
_event_offset = 0


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_banner():
    print(f"\n{SEP}")
    print("  COPILOT BLENDER CHAT CONSOLE")
    print("  Type your message and press Enter to send.")
    print("  Commands: /help  /models  /model <id|#>  /tools  /docs  /sessions  /resume <id|#>  /new  /clear  /quit")
    print(f"{SEP}\n")


def read_status():
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def blender_shutdown_requested():
    try:
        if os.path.exists(SHUTDOWN_FILE):
            return True
        status = read_status()
        return bool(status.get("shutdown"))
    except OSError:
        return False


def start_blender_watchdog(timeout=120, active_timeout=7200):
    """Exit this console if Blender stops updating its IPC heartbeat."""
    def _watch():
        connected_seen = False
        last_seen = 0.0
        while not _stop_event.is_set():
            time.sleep(2)
            if blender_shutdown_requested():
                os._exit(0)

            status = read_status()
            heartbeat = float(status.get("timestamp", 0.0) or 0.0)
            if heartbeat > last_seen:
                last_seen = heartbeat
            if status.get("connected"):
                connected_seen = True

            if not connected_seen:
                continue

            request_active = bool(
                status.get("request_active")
                or status.get("is_thinking")
                or _chat_active.is_set()
            )
            stale_after = active_timeout if request_active else timeout
            if last_seen and time.time() - last_seen > stale_after:
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def request_model_refresh(timeout=45):
    """Ask Blender to refresh models and wait for status to change."""
    before = read_status()
    before_timestamp = before.get("timestamp", 0)
    write_prompt("", action="refresh_models")

    start = time.time()
    latest = before
    while time.time() - start < timeout:
        time.sleep(0.5)
        status = read_status()
        if not status.get("connected"):
            continue
        latest = status
        if status.get("timestamp", 0) > before_timestamp:
            return status
    return latest


def wait_for_status_update(before_timestamp=0, timeout=20):
    start = time.time()
    latest = read_status()
    while time.time() - start < timeout:
        time.sleep(0.25)
        status = read_status()
        if not status.get("connected"):
            continue
        latest = status
        if status.get("timestamp", 0) > before_timestamp:
            return status
    return latest


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with _ipc_write_lock:
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass


def write_prompt(text, action="chat", **extra):
    """Write a prompt for Blender to pick up."""
    data = {"action": action, "prompt": text, "timestamp": time.time()}
    data.update(extra)
    _atomic_write_json(PROMPT_FILE, data)


def send_status_command(action, text="", timeout=20, **extra):
    before = read_status()
    write_prompt(text, action=action, **extra)
    return wait_for_status_update(before.get("timestamp", 0), timeout=timeout)


def _read_response_file(path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(path)
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def read_response():
    """Read a completed chat response written by Blender."""
    return _read_response_file(RESPONSE_FILE)


def read_command_response():
    """Read a slash-command response without racing the chat response channel."""
    return _read_response_file(COMMAND_RESPONSE_FILE)


def clear_response_file():
    try:
        if os.path.exists(RESPONSE_FILE):
            os.remove(RESPONSE_FILE)
    except OSError:
        pass


def clear_command_response_file():
    try:
        if os.path.exists(COMMAND_RESPONSE_FILE):
            os.remove(COMMAND_RESPONSE_FILE)
    except OSError:
        pass


def clear_event_file():
    global _event_offset
    _event_offset = 0
    try:
        if os.path.exists(EVENTS_FILE):
            with open(EVENTS_FILE, "w", encoding="utf-8"):
                pass
    except OSError:
        pass


def read_events():
    global _event_offset
    events = []
    try:
        if not os.path.exists(EVENTS_FILE):
            return events
        size = os.path.getsize(EVENTS_FILE)
        if size < _event_offset:
            _event_offset = 0
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            f.seek(_event_offset)
            chunk = f.read()
            if not chunk:
                return events
            next_offset = f.tell()
        if not chunk.endswith("\n"):
            last_newline = chunk.rfind("\n")
            if last_newline < 0:
                return events
            complete_chunk = chunk[:last_newline + 1]
            _event_offset += last_newline + 1
        else:
            complete_chunk = chunk
            _event_offset = next_offset
        for line in complete_chunk.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events


def wait_for_command_response(timeout=180):
    """Wait for Blender's separate slash-command response channel."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = read_command_response()
        if response:
            return response
        time.sleep(0.1)
    return {"content": "[Timed out waiting for response]", "error": "timeout"}


def send_command_and_wait(text, timeout=90):
    clear_command_response_file()
    write_prompt(text, action="chat")
    return wait_for_command_response(timeout=timeout)


def request_is_active():
    status = read_status()
    return bool(status.get("request_active") or status.get("is_thinking") or _chat_active.is_set())


def _prompt_text():
    if request_is_active():
        dots = "." * (int(time.time() * 2) % 4)
        return f"  Thinking{dots:<3}  YOU > "
    return "  YOU > "


def _console_width():
    return max(20, shutil.get_terminal_size((80, 20)).columns)


def _render_prompt_lines():
    prompt = _prompt_text()
    width = max(1, _console_width() - 1)
    text = prompt + _input_buffer
    if not text:
        return [""]
    return [text[i:i + width] for i in range(0, len(text), width)]


def _clear_prompt_render():
    global _last_prompt_lines
    if _last_prompt_lines <= 0:
        return
    sys.stdout.write("\r")
    for _ in range(_last_prompt_lines - 1):
        sys.stdout.write("\x1b[A")
    sys.stdout.write("\r")
    for i in range(_last_prompt_lines):
        sys.stdout.write("\x1b[2K")
        if i < _last_prompt_lines - 1:
            sys.stdout.write("\x1b[B")
    for _ in range(_last_prompt_lines - 1):
        sys.stdout.write("\x1b[A")
    sys.stdout.write("\r")
    _last_prompt_lines = 0


def _redraw_prompt():
    global _last_prompt_lines
    if not _prompt_active:
        return
    lines = _render_prompt_lines()
    with _console_lock:
        _clear_prompt_render()
        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()
        _last_prompt_lines = len(lines)


def _clear_prompt_line():
    with _console_lock:
        _clear_prompt_render()
        sys.stdout.flush()


def read_user_input():
    if msvcrt is None:
        return input(_prompt_text()).strip()

    global _prompt_active, _input_buffer
    _input_buffer = ""
    _prompt_active = True
    _redraw_prompt()

    while True:
        _redraw_prompt()
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue

        ch = msvcrt.getwch()
        if ch == "\x03":
            _prompt_active = False
            _clear_prompt_line()
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            text = _input_buffer.strip()
            _prompt_active = False
            with _console_lock:
                _clear_prompt_line()
                if text:
                    print(f"  YOU > {text}")
            return text
        if ch == "\b":
            _input_buffer = _input_buffer[:-1]
            _redraw_prompt()
            continue
        if ch in ("\x00", "\xe0"):
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue
        if ch >= " ":
            _input_buffer += ch
            _redraw_prompt()


def wait_until_idle(timeout=8):
    start = time.time()
    while time.time() - start < timeout:
        status = read_status()
        if not status.get("request_active") and not status.get("is_thinking"):
            return
        time.sleep(0.1)


def send_chat_now(text):
    try:
        if os.path.exists(CANCEL_FILE):
            os.remove(CANCEL_FILE)
    except OSError:
        pass
    clear_response_file()
    clear_event_file()
    _chat_active.set()
    write_prompt(text, action="chat")


def queue_chat(text):
    with _chat_lock:
        _queued_chats.append(text)
    print("  [queued] Message will send after the current response. Ctrl+C cancels the active request.\n")


def send_next_queued_chat():
    wait_until_idle()
    with _chat_lock:
        if not _queued_chats:
            return
        text = _queued_chats.pop(0)
    print(f"\n  [sending queued] {text}\n")
    send_chat_now(text)


def request_cancel():
    with _chat_lock:
        _queued_chats.clear()
    _chat_active.clear()
    _atomic_write_json(CANCEL_FILE, {"cancel": True, "timestamp": time.time()})
    print("\n  Cancel requested.\n")


def start_response_watcher():
    def _watch():
        while not _stop_event.is_set():
            for event in read_events():
                print_event(event)
            resp = read_response()
            if not resp:
                time.sleep(0.2)
                continue
            _chat_active.clear()
            print_response(resp)
            send_next_queued_chat()

    threading.Thread(target=_watch, daemon=True).start()


def print_event(event):
    with _console_lock:
        _clear_prompt_line()
        event_type = event.get("type", "")
        iteration = event.get("iteration")
        step = f" step {iteration}" if iteration else ""
        if event_type == "tool_batch":
            count = event.get("tool_count", 0)
            print(f"\n  [agent{step}] running {count} Blender action(s)")
        elif event_type == "tool_start":
            print(f"\n  [tool{step}] running {event.get('tool_name', '(unknown)')}")
        elif event_type == "tool_result":
            print(f"\n  [tool{step}] finished {event.get('tool_name', '(unknown)')}")
        elif event_type == "transport_retry":
            attempt = event.get("attempt", "?")
            maximum = event.get("max_attempts", "?")
            delay = event.get("delay_seconds", 0)
            print(f"\n  [agent{step}] connection retry {attempt}/{maximum} in {delay:g}s")
        elif event_type == "repeat_guard":
            print(f"\n  [agent{step}] stopped a repeated tool loop and requested the final answer")
        elif event_type == "iteration_cap":
            print(f"\n  [agent] tool iteration cap reached ({event.get('max_iterations')}); asking for final answer")
        else:
            print(f"\n  [agent] {event}")
        print()
        _redraw_prompt()


def print_response(resp):
    """Pretty-print a Copilot response."""
    with _console_lock:
        _clear_prompt_line()

        content = resp.get("content", "")
        model = resp.get("model", "")
        error = resp.get("error")
        tool_log = resp.get("tool_log", [])

        if error:
            if content:
                tag = f" [{model}]" if model else ""
                print(f"\n{SEP}")
                print(f"  COPILOT{tag} (partial):")
                print(SEP)
                for line in content.split("\n"):
                    print(f"  {line}")
            print(f"\n  [ERROR] {error}\n")
            _redraw_prompt()
            return

        if tool_log:
            print(f"\n  [Completed {len(tool_log)} Blender action(s)]")

        tag = f" [{model}]" if model else ""
        print(f"\n{SEP}")
        print(f"  COPILOT{tag}:")
        print(SEP)
        for line in content.split("\n"):
            print(f"  {line}")
        print()
        _redraw_prompt()


def print_command_response(resp):
    with _console_lock:
        _clear_prompt_line()
        if not resp:
            print("  [ERROR] No response from Blender\n")
            _redraw_prompt()
            return
        if resp.get("error"):
            print(f"\n  [ERROR] {resp['error']}\n")
            _redraw_prompt()
            return
        content = resp.get("content", "")
        print()
        for line in content.split("\n"):
            print(f"  {line}")
        print()
        _redraw_prompt()


def print_help():
    print("\n  Slash commands:")
    print("    /models                 Refresh and list available models")
    print("    /model                  Show active model and reasoning strength")
    print("    /model <id|number> [reasoning]")
    print("                           Select model and optional reasoning: default/none/minimal/low/medium/high/xhigh/max")
    print("    /reasoning [strength]   Show or set reasoning strength")
    print("    /tools                  List tools available to tool-capable models")
    print("    /docs                   Show local Blender docs path")
    print("    /docs <path>            Set downloaded Blender docs folder/file path")
    print("    /docs clear             Clear local docs path")
    print("    /sessions               List saved conversations with numbers")
    print("    /conversations          Same as /sessions")
    print("    /history                Show saved messages for the active conversation")
    print("    /resume <id|number>     Resume a saved conversation from /sessions")
    print("    /new [title]            Start a new saved conversation")
    print("    /cancel                 Cancel the active request (Ctrl+C also cancels)")
    print("    /clear                  Clear the active conversation")
    print("    /quit                   Exit the console\n")


def print_models(status):
    models = status.get("models") or []
    active = status.get("active_model") or "none"
    reasoning = status.get("reasoning_label") or status.get("reasoning_strength") or "Model default"
    if not models:
        print(f"  Active model: {active}")
        print(f"  Reasoning strength: {reasoning}")
        print("  No model list is loaded. Run /models to refresh.")
        if status.get("last_error"):
            print(f"  Last error: {status['last_error']}")
        print()
        return

    print(f"  Active model: {active}")
    print(f"  Reasoning strength: {reasoning}")
    print("  Use /model <id|number> [default|none|minimal|low|medium|high|xhigh|max]")
    for model in models:
        marker = "*" if model.get("active") else " "
        default = " default" if model.get("is_default") else ""
        tools = " tools" if model.get("supports_tools") else ""
        vision = " vision" if model.get("supports_vision") else ""
        reasoning_options = model.get("reasoning_efforts") or ""
        reasoning = f" reasoning={reasoning_options}" if reasoning_options else ""
        endpoint = f" {model.get('endpoint')}" if model.get("endpoint") and model.get("endpoint") != "/chat/completions" else ""
        display = model.get("display_name") or model.get("id")
        print(f"  {marker} {model.get('index', '?'):>2}. {model.get('id')} - {display} ({model.get('vendor', '')}{default}{tools}{vision}{reasoning}{endpoint})")
    print()


def print_sessions(status):
    sessions = status.get("sessions") or []
    active_id = status.get("active_session_id") or "default"
    if not sessions:
        print("  No saved conversations yet. Use /new to start one, or just send a message.\n")
        return

    print(f"  Saved conversations. Active: {active_id}")
    for i, session in enumerate(sessions, start=1):
        marker = "*" if session.get("active") else " "
        title = session.get("title") or "(untitled)"
        count = session.get("message_count", 0)
        print(f"  {marker} {i:>2}. {session.get('id')} - {title} ({count} messages)")
    print()


def resolve_session_argument(arg, status):
    arg = str(arg or "").strip()
    sessions = status.get("sessions") or []
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx].get("id", arg)
    return arg


def main():
    os.makedirs(SHARED_DIR, exist_ok=True)
    start_blender_watchdog()
    start_response_watcher()
    clear_screen()
    print_banner()

    # Wait for Blender to be connected
    print("  Waiting for Blender connection...")
    for _ in range(120):
        status = read_status()
        if status.get("connected"):
            user = status.get("username", "")
            session_title = status.get("active_session_title") or status.get("active_session_id") or "default"
            print(f"  Connected as {user}")
            print(f"  Conversation: {session_title}")
            print()
            history = send_command_and_wait("/history", timeout=10)
            print_command_response(history)
            break
        time.sleep(0.5)
    else:
        print("  [WARNING] No Blender connection detected. You can still type — ")
        print("  messages will be sent when Blender picks them up.\n")

    # Main chat loop
    while True:
        try:
            user_input = read_user_input()
        except KeyboardInterrupt:
            request_cancel()
            continue
        except EOFError:
            print("\n  Goodbye!")
            break

        if not user_input:
            continue

        parts = user_input.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/quit":
            print("  Goodbye!")
            break
        elif command == "/help":
            print_help()
            continue
        elif command == "/clear":
            status = send_status_command("clear", timeout=10)
            if status.get("last_error"):
                print(f"  [ERROR] {status['last_error']}\n")
                continue
            clear_screen()
            print_banner()
            continue
        elif command == "/cancel":
            request_cancel()
            continue
        elif command == "/models":
            print("  Refreshing models...")
            resp = send_command_and_wait("/models")
            print_command_response(resp)
            continue
        elif command == "/model":
            if not arg:
                status = send_status_command("status", timeout=5)
                print_models(status)
                continue
            resp = send_command_and_wait(user_input, timeout=10)
            print_command_response(resp)
            continue
        elif command == "/reasoning":
            resp = send_command_and_wait(user_input, timeout=10)
            print_command_response(resp)
            continue
        elif command == "/tools":
            resp = send_command_and_wait("/tools")
            print_command_response(resp)
            continue
        elif command == "/docs":
            resp = send_command_and_wait(user_input)
            print_command_response(resp)
            continue
        elif command in ("/sessions", "/conversations", "/chats"):
            status = send_status_command("status", timeout=5)
            print_sessions(status)
            print("  Resume one with: /resume <number>\n")
            continue
        elif command == "/history":
            resp = send_command_and_wait("/history", timeout=10)
            print_command_response(resp)
            continue
        elif command == "/resume":
            if not arg:
                status = send_status_command("status", timeout=5)
                print_sessions(status)
                print("  Usage: /resume <id|number>\n")
                continue
            status = send_status_command("status", timeout=5)
            session_id = resolve_session_argument(arg, status)
            status = send_status_command("resume_session", session_id, timeout=10, session_id=session_id)
            if status.get("last_error"):
                print(f"  [ERROR] {status['last_error']}\n")
            else:
                title = status.get("active_session_title") or status.get("active_session_id")
                print(f"  Resumed: {title}\n")
                history = send_command_and_wait("/history", timeout=10)
                print_command_response(history)
            continue
        elif command == "/new":
            status = send_status_command("new_session", arg, timeout=10, title=arg)
            if status.get("last_error"):
                print(f"  [ERROR] {status['last_error']}\n")
            else:
                title = status.get("active_session_title") or status.get("active_session_id")
                print(f"  Started new conversation: {title}\n")
            continue
        elif user_input.startswith("/"):
            print(f"  Unknown command: {command}. Type /help for commands.\n")
            continue

        # Send to Blender
        status = read_status()
        if not status.get("active_model"):
            print("  Models are still loading; refreshing...")
            status = request_model_refresh()
            if not status.get("active_model"):
                print("  [ERROR] No active model is available yet. Try /models again after Blender finishes loading.\n")
                continue
        if request_is_active():
            queue_chat(user_input)
            continue
        send_chat_now(user_input)
        print("  Sent. You can keep typing; Ctrl+C cancels the active request.\n")


if __name__ == "__main__":
    main()
