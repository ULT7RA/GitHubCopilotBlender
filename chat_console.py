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

SHARED_DIR = os.path.join(os.environ.get("TEMP", "/tmp"), "copilot_blender_ipc")
PROMPT_FILE = os.path.join(SHARED_DIR, "prompt.json")
RESPONSE_FILE = os.path.join(SHARED_DIR, "response.json")
STATUS_FILE = os.path.join(SHARED_DIR, "status.json")

SEP = "=" * 72


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_banner():
    print(f"\n{SEP}")
    print("  COPILOT BLENDER CHAT CONSOLE")
    print("  Type your message and press Enter to send.")
    print("  Commands: /clear  /models  /quit")
    print(f"{SEP}\n")


def read_status():
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def write_prompt(text, action="chat"):
    """Write a prompt for Blender to pick up."""
    os.makedirs(SHARED_DIR, exist_ok=True)
    data = {"action": action, "prompt": text, "timestamp": time.time()}
    with open(PROMPT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_response():
    """Read response written by Blender."""
    try:
        if os.path.exists(RESPONSE_FILE):
            with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(RESPONSE_FILE)
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def wait_for_response(timeout=600):
    """Block until Blender writes a response."""
    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        resp = read_response()
        if resp:
            return resp

        # Show thinking animation
        dots = (dots + 1) % 4
        sys.stdout.write(f"\r  Thinking{'.' * dots}{'   '}")
        sys.stdout.flush()
        time.sleep(0.5)

    sys.stdout.write("\r" + " " * 40 + "\r")
    return {"content": "[Timed out waiting for response]", "error": "timeout"}


def print_response(resp):
    """Pretty-print a Copilot response."""
    sys.stdout.write("\r" + " " * 40 + "\r")  # Clear thinking dots

    content = resp.get("content", "")
    model = resp.get("model", "")
    error = resp.get("error")
    tool_log = resp.get("tool_log", [])

    if error:
        print(f"\n  [ERROR] {error}\n")
        return

    if tool_log:
        print(f"\n  [Tools used: {len(tool_log)}]")

    tag = f" [{model}]" if model else ""
    print(f"\n{SEP}")
    print(f"  COPILOT{tag}:")
    print(SEP)
    for line in content.split("\n"):
        print(f"  {line}")
    print()


def main():
    os.makedirs(SHARED_DIR, exist_ok=True)
    clear_screen()
    print_banner()

    # Wait for Blender to be connected
    print("  Waiting for Blender connection...")
    for _ in range(120):
        status = read_status()
        if status.get("connected"):
            user = status.get("username", "")
            model = status.get("active_model", "")
            n_models = status.get("model_count", 0)
            print(f"  Connected as {user}")
            print(f"  {n_models} models available. Active: {model}")
            print()
            break
        time.sleep(0.5)
    else:
        print("  [WARNING] No Blender connection detected. You can still type — ")
        print("  messages will be sent when Blender picks them up.\n")

    # Main chat loop
    while True:
        try:
            user_input = input("  YOU > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() == "/quit":
            print("  Goodbye!")
            break
        elif user_input.lower() == "/clear":
            write_prompt("", action="clear")
            clear_screen()
            print_banner()
            continue
        elif user_input.lower() == "/models":
            write_prompt("", action="refresh_models")
            print("  Refreshing models...")
            time.sleep(2)
            status = read_status()
            n = status.get("model_count", 0)
            active = status.get("active_model", "none")
            print(f"  {n} models. Active: {active}\n")
            continue

        # Print what user typed
        print(f"\n{SEP}")
        print(f"  YOU:")
        print(SEP)
        for line in user_input.split("\n"):
            print(f"  {line}")
        print()

        # Send to Blender
        write_prompt(user_input, action="chat")

        # Wait for response
        resp = wait_for_response()
        if resp:
            print_response(resp)


if __name__ == "__main__":
    main()
