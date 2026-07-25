"""
Tool executor — runs tool calls locally and returns results.
Universal file tools + Blender-specific scene/mesh/material/modifier tools.

IMPORTANT: Blender-specific tools that touch bpy must be scheduled on the main
thread (via _schedule_on_main) since bpy is not thread-safe. File I/O tools
are thread-safe and can run directly.
"""

import fnmatch
import html
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import traceback
from datetime import datetime
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from . import auth as _auth

# bpy is only available inside Blender; guard for static analysis
try:
    import bpy
    import bmesh
    from mathutils import Vector
    _HAS_BPY = True
except ImportError:
    _HAS_BPY = False

# ── Queued main-thread execution ──────────────────────────────────────────
# Blender-scene tools must run on the main thread. We queue them and the
# modal timer operator drains the queue.

import threading
_main_queue = []
_main_queue_lock = threading.Lock()
_main_results = {}
_abandoned_main_ids = set()
_main_results_lock = threading.Lock()
_tool_runtime = threading.local()
_exec_counter = 0
MAIN_THREAD_TIMEOUT_SECONDS = 3600
DEFAULT_OUTPUT_LIMIT = 20_000


def _debug_log(message):
    if os.environ.get("COPILOT_BLENDER_DEBUG") != "1":
        return
    path = os.path.join(
        os.environ.get("TEMP", tempfile.gettempdir()),
        "copilot_blender_ipc",
        "debug_drain.log",
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.1f} {message}\n")
    except OSError:
        pass


def _bounded_text(text, max_chars=DEFAULT_OUTPUT_LIMIT, artifact_prefix=None):
    """Keep tool responses bounded, optionally preserving the full text."""
    text = str(text)
    max_chars = max(1000, int(max_chars))
    if len(text) <= max_chars:
        return text
    artifact = ""
    if artifact_prefix:
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", artifact_prefix)
        artifact = os.path.join(
            tempfile.gettempdir(),
            f"{safe_prefix}_{os.getpid()}_{time.time_ns()}.txt",
        )
        try:
            with open(artifact, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(text)
        except OSError:
            artifact = ""
    notice = f"\n...[truncated {len(text) - max_chars} chars"
    if artifact:
        notice += f"; full output saved to {artifact}"
    notice += "]...\n"
    available = max_chars - len(notice)
    head = max(1, available * 2 // 3)
    tail = max(1, available - head)
    return text[:head] + notice + text[-tail:]


def _json_result(data, max_chars=DEFAULT_OUTPUT_LIMIT):
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    preview_chars = max(1000, max_chars // 4)
    return json.dumps({
        "output_truncated": True,
        "original_chars": len(text),
        "head": text[:preview_chars],
        "tail": text[-preview_chars:],
    }, indent=2, ensure_ascii=False)


def _schedule_on_main(func, *args, **kwargs):
    """
    Schedule a function to run on Blender's main thread.
    Returns a future-like ID that can be polled via _get_main_result.
    For simplicity in the synchronous tool-call flow, we block until done.
    """
    global _exec_counter
    with _main_queue_lock:
        _exec_counter += 1
        eid = _exec_counter
        _main_queue.append((eid, func, args, kwargs))

    # Block until result is ready (the modal timer will execute it)
    _debug_log(f"scheduled eid={eid} func={func.__name__}")
    deadline = time.time() + MAIN_THREAD_TIMEOUT_SECONDS
    while time.time() < deadline:
        cancel_event = getattr(_tool_runtime, "cancel_event", None)
        if cancel_event and cancel_event.is_set():
            _abandon_main_execution(eid)
            return "Error: Tool execution cancelled"
        with _main_results_lock:
            if eid in _main_results:
                result = _main_results.pop(eid)
                if isinstance(result, Exception):
                    return f"Error: {result}"
                return result
        time.sleep(0.05)
    completed = _abandon_main_execution(eid)
    if completed is not None:
        if isinstance(completed, Exception):
            return f"Error: {completed}"
        return completed
    _debug_log(f"TIMEOUT eid={eid} ({MAIN_THREAD_TIMEOUT_SECONDS}s)")
    return f"Error: Main-thread execution timed out ({MAIN_THREAD_TIMEOUT_SECONDS}s)"


def _abandon_main_execution(eid):
    """Remove queued work or discard the eventual result if execution already began."""
    with _main_queue_lock:
        was_queued = any(item[0] == eid for item in _main_queue)
        _main_queue[:] = [item for item in _main_queue if item[0] != eid]
    with _main_results_lock:
        completed = _main_results.pop(eid, None)
        if completed is None and not was_queued:
            _abandoned_main_ids.add(eid)
        return completed


def drain_main_queue():
    """Called from the modal timer on Blender's main thread."""
    with _main_queue_lock:
        pending = list(_main_queue)
        _main_queue.clear()

    if pending:
        _debug_log(f"draining {len(pending)} items")

    for eid, func, args, kwargs in pending:
        try:
            _debug_log(f"exec eid={eid} func={func.__name__}")
            result = func(*args, **kwargs)
            _debug_log(f"done eid={eid} result_len={len(str(result))}")
        except Exception as e:
            result = e
            _debug_log(f"ERROR eid={eid}: {e}\n{traceback.format_exc()}")
        with _main_results_lock:
            if eid in _abandoned_main_ids:
                _abandoned_main_ids.remove(eid)
            else:
                _main_results[eid] = result


# ── Resolve path helper ──────────────────────────────────────────────────

def _resolve_path(path: str) -> str:
    """Resolve a path relative to the blend file directory or CWD."""
    if os.path.isabs(path):
        return path
    if _HAS_BPY and bpy.data.filepath:
        base = os.path.dirname(bpy.data.filepath)
    else:
        base = os.getcwd()
    return os.path.normpath(os.path.join(base, path))


# ── File tools ────────────────────────────────────────────────────────────

def _tool_read_file(args: dict) -> str:
    path = _resolve_path(args["path"])
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"Error reading file: {e}"

    explicit_range = args.get("start_line") is not None or args.get("end_line") is not None
    start = args.get("start_line", 1)
    end = args.get("end_line", len(lines))
    if not explicit_range:
        end = min(end, start + 399)
    start = max(1, start)
    end = min(len(lines), end)

    numbered = []
    total_chars = 0
    max_chars = 24_000
    for i in range(start - 1, end):
        line = f"{i + 1}. {lines[i].rstrip()}"
        total_chars += len(line) + 1
        if total_chars > max_chars:
            numbered.append(
                f"...[read_file output truncated at {max_chars} chars; "
                "use start_line/end_line or search_blender_docs for narrower results]"
            )
            break
        numbered.append(line)
    if not explicit_range and end < len(lines):
        numbered.append(
            f"...[showing first {end - start + 1} of {len(lines)} lines; "
            "use start_line/end_line for a specific range]"
        )
    return "\n".join(numbered)


def _tool_write_file(args: dict) -> str:
    path = _resolve_path(args["path"])
    content = args.get("content", "")
    # Backup existing
    if os.path.isfile(path):
        bak = path + ".copilot_bak"
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File written: {path} ({len(content)} chars)"
    except OSError as e:
        return f"Error writing file: {e}"


def _tool_edit_file(args: dict) -> str:
    path = _resolve_path(args["path"])
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    if not os.path.isfile(path):
        return f"Error: File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"Error reading file: {e}"

    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str not found in {path}"
    if count > 1:
        return f"Error: old_str found {count} times (must be unique)"

    # Backup
    bak = path + ".copilot_bak"
    try:
        shutil.copy2(path, bak)
    except OSError:
        pass

    new_content = content.replace(old_str, new_str, 1)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"Edit applied to {path}"
    except OSError as e:
        return f"Error writing file: {e}"


def _tool_list_directory(args: dict) -> str:
    path = _resolve_path(args.get("path", "."))
    recursive = args.get("recursive", False)
    max_depth = args.get("max_depth", 2)

    if not os.path.isdir(path):
        return f"Error: Not a directory: {path}"

    entries = []

    def _walk(d, depth):
        if depth > max_depth:
            return
        try:
            items = sorted(os.listdir(d))
        except OSError:
            return
        for item in items:
            if item.startswith("."):
                continue
            full = os.path.join(d, item)
            rel = os.path.relpath(full, path)
            if os.path.isdir(full):
                entries.append(f"[DIR]  {rel}/")
                if recursive:
                    _walk(full, depth + 1)
            else:
                sz = os.path.getsize(full)
                entries.append(f"[FILE] {rel} ({sz} bytes)")

    _walk(path, 1)
    if not entries:
        return "(empty directory)"
    return "\n".join(entries[:250] + ([f"... (truncated; {len(entries) - 250} more entries)"] if len(entries) > 250 else []))


def _tool_create_directory(args: dict) -> str:
    path = _resolve_path(args["path"])
    try:
        os.makedirs(path, exist_ok=True)
        return f"Directory created: {path}"
    except OSError as e:
        return f"Error: {e}"


def _tool_delete_file(args: dict) -> str:
    path = _resolve_path(args["path"])
    try:
        if os.path.isfile(path):
            os.remove(path)
            return f"Deleted file: {path}"
        elif os.path.isdir(path):
            os.rmdir(path)
            return f"Deleted directory: {path}"
        else:
            return f"Error: Path not found: {path}"
    except OSError as e:
        return f"Error: {e}"


def _tool_copy_file(args: dict) -> str:
    src = _resolve_path(args["source"])
    dst = _resolve_path(args["destination"])
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return f"Copied {src} → {dst}"
    except OSError as e:
        return f"Error: {e}"


def _tool_move_file(args: dict) -> str:
    src = _resolve_path(args["source"])
    dst = _resolve_path(args["destination"])
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return f"Moved {src} → {dst}"
    except OSError as e:
        return f"Error: {e}"


def _tool_search_files(args: dict) -> str:
    pattern = args.get("pattern", "")
    base = _resolve_path(args.get("path", "."))
    file_pattern = args.get("file_pattern", "*")
    case_sensitive = args.get("case_sensitive", True)

    if not pattern:
        return "Error: pattern is required"

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: Invalid regex: {e}"

    results = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if not fnmatch.fnmatch(fname, file_pattern):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = os.path.relpath(fpath, base)
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= 100:
                                results.append("... (truncated at 100 matches)")
                                return _bounded_text("\n".join(results), 24_000)
            except OSError:
                continue

    return _bounded_text("\n".join(results), 24_000) if results else "No matches found."


def _tool_get_file_info(args: dict) -> str:
    path = _resolve_path(args["path"])
    if not os.path.exists(path):
        return f"Error: Path not found: {path}"
    st = os.stat(path)
    info = {
        "path": path,
        "type": "directory" if os.path.isdir(path) else "file",
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
        "created": datetime.fromtimestamp(st.st_ctime).isoformat(),
    }
    return json.dumps(info, indent=2)


def _tool_get_project_structure(args: dict) -> str:
    base = _resolve_path(args.get("path", "."))
    max_depth = args.get("max_depth", 3)
    lines = [f"Project root: {base}\n"]

    def _walk(d, depth, prefix=""):
        if depth > max_depth:
            return
        try:
            items = sorted(os.listdir(d))
        except OSError:
            return
        for item in items:
            if item.startswith("."):
                continue
            full = os.path.join(d, item)
            if os.path.isdir(full):
                lines.append(f"{prefix}📁 {item}/")
                _walk(full, depth + 1, prefix + "  ")
            else:
                sz = os.path.getsize(full)
                lines.append(f"{prefix}📄 {item} ({sz} B)")

    _walk(base, 1)
    if len(lines) > 250:
        return "\n".join(lines[:250] + [f"... (truncated; {len(lines) - 250} more entries)"])
    return "\n".join(lines)


# ── Web/documentation tools ───────────────────────────────────────────────

def _http_get_text(url: str, timeout: int = 20) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "GitHubCopilotBlender/1.0 (+https://github.com)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _configured_docs_path() -> str:
    return os.environ.get("BLENDER_DOCS_PATH", "").strip() or _auth.get_effective_blender_docs_path().strip()


def _doc_title(path: str, text: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if match:
        title = _strip_html(match.group(1))
        if title:
            return title
    return os.path.basename(path)


def _text_snippet(text: str, query_terms: list, max_chars: int = 500) -> str:
    clean = _strip_html(text)
    lower = clean.lower()
    positions = [lower.find(term) for term in query_terms if term and lower.find(term) >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - max_chars // 3)
    snippet = clean[start:start + max_chars]
    if start > 0:
        snippet = "... " + snippet
    if start + max_chars < len(clean):
        snippet += " ..."
    return snippet


def _iter_doc_files(root: str):
    allowed_ext = {".html", ".htm", ".txt", ".md", ".rst", ".py"}
    if os.path.isfile(root):
        if os.path.splitext(root)[1].lower() in allowed_ext:
            yield root
        return

    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"_static", "_images"}]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() not in allowed_ext:
                continue
            yield os.path.join(dirpath, filename)
            scanned += 1
            if scanned >= 5000:
                return


def _search_local_blender_docs(query: str, limit: int, docs_path: str = "") -> list:
    root = docs_path.strip() or _configured_docs_path()
    if not root:
        return []
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.exists(root):
        return []

    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_\.]+", query) if len(term) > 1]
    if not terms:
        return []

    results = []
    for path in _iter_doc_files(root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(1_000_000)
        except OSError:
            continue

        lower = text.lower()
        score = sum(lower.count(term) for term in terms)
        if score <= 0:
            continue

        results.append({
            "score": score,
            "path": path,
            "relative_path": os.path.relpath(path, root) if os.path.isdir(root) else os.path.basename(path),
            "title": _doc_title(path, text),
            "snippet": _text_snippet(text, terms),
        })

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def _extract_duckduckgo_results(page: str, limit: int) -> list:
    results = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page):
        url = html.unescape(match.group("url"))
        title = _strip_html(match.group("title"))
        snippet = _strip_html(match.group("snippet"))
        if not title:
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _tool_web_search(args: dict) -> str:
    query = args.get("query", "").strip()
    limit = int(args.get("limit", 5) or 5)
    limit = max(1, min(limit, 10))
    if not query:
        return "Error: query is required"

    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        page = _http_get_text(url)
    except HTTPError as e:
        return f"Error: Web search HTTP {e.code}"
    except (URLError, TimeoutError, OSError) as e:
        return f"Error: Web search failed: {e}"

    results = _extract_duckduckgo_results(page, limit)
    if not results:
        return "No web search results found."
    return json.dumps({"query": query, "results": results}, indent=2, ensure_ascii=False)


def _tool_search_blender_docs(args: dict) -> str:
    query = args.get("query", "").strip()
    limit = int(args.get("limit", 6) or 6)
    limit = max(1, min(limit, 10))
    doc_area = args.get("doc_area", "all")
    docs_path = args.get("docs_path", "")
    if not query:
        return "Error: query is required"

    local_docs_path = docs_path
    if not local_docs_path and doc_area in ("api", "manual", "dev"):
        area_path = os.path.join(_configured_docs_path(), doc_area)
        if os.path.exists(area_path):
            local_docs_path = area_path

    local_results = _search_local_blender_docs(query, limit, local_docs_path)
    if local_results:
        return json.dumps({
            "query": query,
            "source": "local_blender_docs",
            "docs_path": os.path.abspath(os.path.expanduser(local_docs_path or _configured_docs_path())),
            "results": local_results,
        }, indent=2, ensure_ascii=False)

    if doc_area == "api":
        search_query = f"site:docs.blender.org/api/current {query}"
    elif doc_area == "manual":
        search_query = f"site:docs.blender.org/manual/en/latest {query}"
    elif doc_area == "dev":
        search_query = f"site:developer.blender.org/docs {query}"
    else:
        search_query = f"site:docs.blender.org Blender {query}"

    return _tool_web_search({"query": search_query, "limit": limit})


# ── Blender-specific tools (run on main thread) ──────────────────────────

def _tool_execute_python_script(args: dict) -> str:
    code = args.get("code", "")
    desc = args.get("description", "AI-generated script")
    cancel_event = getattr(_tool_runtime, "cancel_event", None)

    def _exec():
        import sys
        old_stdout = sys.stdout
        old_trace = sys.gettrace()
        sys.stdout = captured = StringIO()
        try:
            _push_undo(f"Copilot script: {desc}")
            if cancel_event:
                def _cancel_trace(frame, event, arg):
                    if cancel_event.is_set():
                        raise RuntimeError("Script execution cancelled")
                    return _cancel_trace
                sys.settrace(_cancel_trace)
            ns = {"bpy": bpy, "C": bpy.context, "D": bpy.data, "__name__": "__copilot_script__"}
            try:
                import bmesh as _bmesh
                ns["bmesh"] = _bmesh
            except ImportError:
                pass
            try:
                from mathutils import Vector as _Vec, Matrix as _Mat, Euler as _Eul, Quaternion as _Quat
                ns.update({"Vector": _Vec, "Matrix": _Mat, "Euler": _Eul, "Quaternion": _Quat})
            except ImportError:
                pass

            exec(compile(code, f"<copilot:{desc}>", "exec"), ns)
            output = captured.getvalue()
            result = f"Script executed successfully.\n{output}" if output else "Script executed successfully."
            return _bounded_text(result, 20_000, "copilot_python_output")
        except Exception:
            output = captured.getvalue()
            tb = traceback.format_exc()
            return _bounded_text(
                f"Script error:\n{tb}\nOutput so far:\n{output}",
                20_000,
                "copilot_python_error",
            )
        finally:
            sys.settrace(old_trace)
            sys.stdout = old_stdout

    return _schedule_on_main(_exec)


def _tool_get_blender_version(args: dict) -> str:
    def _gather():
        info = {
            "version_string": bpy.app.version_string,
            "version": list(bpy.app.version),
            "python_version": sys.version,
            "platform": sys.platform,
            "binary_path": bpy.app.binary_path,
            "background": bpy.app.background,
        }
        for attr in ("build_branch", "build_commit_date", "build_commit_time", "build_hash", "build_type"):
            if hasattr(bpy.app, attr):
                value = getattr(bpy.app, attr)
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                info[attr] = value
        return _json_result(info)

    if not _HAS_BPY:
        return "Error: Blender Python module is not available"
    return _schedule_on_main(_gather)


def _tool_inspect_blender_api(args: dict) -> str:
    target = args.get("target", "bpy").strip()
    include_members = args.get("include_members", True)
    member_filter = str(args.get("member_filter", "") or "").lower()
    max_members = int(args.get("max_members", 80) or 80)
    max_members = max(1, min(max_members, 150))

    def _inspect():
        import mathutils as _mathutils
        roots = {"bpy": bpy, "bmesh": bmesh, "mathutils": _mathutils}
        parts = target.split(".")
        if not parts or parts[0] not in roots:
            return "Error: target must start with bpy, bmesh, or mathutils"

        obj = roots[parts[0]]
        current_path = parts[0]
        try:
            for part in parts[1:]:
                if not part:
                    continue
                obj = getattr(obj, part)
                current_path += f".{part}"
        except Exception as e:
            return f"Error: Could not resolve {current_path}.{part}: {e}"

        info = {
            "target": target,
            "type": type(obj).__name__,
            "repr": repr(obj)[:500],
        }

        doc = getattr(obj, "__doc__", None)
        if doc:
            doc_text = str(doc).strip()
            info["doc"] = doc_text[:3000]
            info["doc_truncated"] = len(doc_text) > 3000

        rna = None
        if hasattr(obj, "bl_rna"):
            rna = getattr(obj, "bl_rna", None)
        elif hasattr(obj, "get_rna_type"):
            try:
                rna = obj.get_rna_type()
            except Exception:
                rna = None

        if rna:
            info["rna_identifier"] = getattr(rna, "identifier", "")
            info["rna_name"] = getattr(rna, "name", "")
            info["rna_description"] = getattr(rna, "description", "")
            try:
                props = []
                for prop in rna.properties:
                    props.append({
                        "identifier": prop.identifier,
                        "name": prop.name,
                        "type": prop.type,
                        "description": prop.description,
                    })
                    if len(props) >= max_members:
                        break
                info["rna_properties"] = props
            except Exception:
                pass

        if include_members:
            members = []
            for name in dir(obj):
                if name.startswith("__"):
                    continue
                if member_filter and member_filter not in name.lower():
                    continue
                members.append(name)
                if len(members) >= max_members:
                    break
            info["members"] = members

        return _json_result(info)

    if not _HAS_BPY:
        return "Error: Blender Python module is not available"
    return _schedule_on_main(_inspect)


def _tool_get_scene_info(args: dict) -> str:
    inc_objects = args.get("include_objects", True)
    inc_materials = args.get("include_materials", True)
    inc_render = args.get("include_render", False)
    max_items = max(1, min(int(args.get("max_items", 100) or 100), 500))

    def _gather():
        info = {"scene": bpy.context.scene.name, "max_items": max_items, "truncation": {}}

        if inc_objects:
            objs = []
            all_objects = list(bpy.context.scene.objects)
            for obj in all_objects[:max_items]:
                o = {
                    "name": obj.name,
                    "type": obj.type,
                    "location": list(obj.location),
                    "visible": obj.visible_get(),
                }
                if obj.modifiers:
                    o["modifiers"] = [m.type for m in obj.modifiers]
                if obj.data and hasattr(obj.data, "materials"):
                    o["materials"] = [m.name for m in obj.data.materials if m]
                objs.append(o)
            info["objects"] = objs
            info["truncation"]["objects"] = {
                "total": len(all_objects), "returned": len(objs), "truncated": len(all_objects) > len(objs),
            }

        if inc_materials:
            mats = []
            all_materials = list(bpy.data.materials)
            for mat in all_materials[:max_items]:
                m = {"name": mat.name, "use_nodes": mat.use_nodes}
                if mat.use_nodes and mat.node_tree:
                    nodes = list(mat.node_tree.nodes)
                    m["nodes"] = [n.bl_idname for n in nodes[:max_items]]
                    m["nodes_truncated"] = len(nodes) > max_items
                mats.append(m)
            info["materials"] = mats
            info["truncation"]["materials"] = {
                "total": len(all_materials), "returned": len(mats), "truncated": len(all_materials) > len(mats),
            }

        if inc_render:
            r = bpy.context.scene.render
            info["render"] = {
                "engine": r.engine,
                "resolution": [r.resolution_x, r.resolution_y],
                "fps": r.fps,
                "filepath": r.filepath,
            }

        collections = []
        all_collections = list(bpy.data.collections)
        for col in all_collections[:max_items]:
            names = [o.name for o in col.objects]
            collections.append({
                "name": col.name,
                "objects": names[:max_items],
                "objects_truncated": len(names) > max_items,
            })
        info["collections"] = collections
        info["truncation"]["collections"] = {
            "total": len(all_collections), "returned": len(collections),
            "truncated": len(all_collections) > len(collections),
        }

        return _json_result(info)

    return _schedule_on_main(_gather)


def _tool_create_mesh(args: dict) -> str:
    primitive = args.get("primitive", "cube")
    name = args.get("name", "")
    location = tuple(args.get("location", [0, 0, 0]))
    scale = tuple(args.get("scale", [1, 1, 1]))
    size = args.get("size", 1.0)

    def _create():
        ops_map = {
            "cube": lambda: bpy.ops.mesh.primitive_cube_add(size=size, location=location),
            "uv_sphere": lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=size, location=location),
            "ico_sphere": lambda: bpy.ops.mesh.primitive_ico_sphere_add(radius=size, location=location),
            "cylinder": lambda: bpy.ops.mesh.primitive_cylinder_add(radius=size, location=location),
            "cone": lambda: bpy.ops.mesh.primitive_cone_add(radius1=size, location=location),
            "plane": lambda: bpy.ops.mesh.primitive_plane_add(size=size, location=location),
            "torus": lambda: bpy.ops.mesh.primitive_torus_add(location=location),
            "monkey": lambda: bpy.ops.mesh.primitive_monkey_add(size=size, location=location),
        }
        if primitive not in ops_map:
            return f"Error: Unknown primitive: {primitive}"

        _push_undo("Copilot create mesh")
        ops_map[primitive]()
        obj = bpy.context.active_object
        if name:
            obj.name = name
        obj.scale = scale
        return f"Created mesh '{obj.name}' ({primitive}) at {list(obj.location)}"

    return _schedule_on_main(_create)


def _tool_create_material(args: dict) -> str:
    mat_name = args.get("name", "Material")
    base_color = args.get("base_color", [0.8, 0.8, 0.8, 1.0])
    metallic = args.get("metallic", 0.0)
    roughness = args.get("roughness", 0.5)
    assign_to = args.get("assign_to", "")

    def _create():
        _push_undo("Copilot create material")
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = base_color[:4] if len(base_color) >= 4 else base_color + [1.0]
            bsdf.inputs["Metallic"].default_value = metallic
            bsdf.inputs["Roughness"].default_value = roughness

        result = f"Created material '{mat_name}'"
        if assign_to:
            obj = bpy.data.objects.get(assign_to)
            if obj and obj.data:
                if obj.data.materials:
                    obj.data.materials[0] = mat
                else:
                    obj.data.materials.append(mat)
                result += f" and assigned to '{assign_to}'"
            else:
                result += f" (warning: object '{assign_to}' not found)"
        return result

    return _schedule_on_main(_create)


def _tool_add_modifier(args: dict) -> str:
    compat_args = dict(args)
    compat_args["action"] = "add"
    result = _tool_manage_modifier(compat_args)
    if result.startswith("{"):
        try:
            data = json.loads(result)
            return f"Added modifier '{data['modifier']['name']}' to '{data['object']}'"
        except (KeyError, ValueError):
            pass
    return result


def _object_or_error(name):
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    return obj


def _capture_context():
    return {
        "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        "selected": [obj.name for obj in bpy.context.selected_objects],
        "mode": bpy.context.mode,
    }


def _ensure_object_mode():
    active = bpy.context.view_layer.objects.active
    if active and active.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def _restore_context(state):
    _ensure_object_mode()
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for name in state["selected"]:
        obj = bpy.data.objects.get(name)
        if obj:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
    active = bpy.data.objects.get(state["active"]) if state["active"] else None
    bpy.context.view_layer.objects.active = active
    if active and state["mode"] != 'OBJECT':
        mode = {
            "EDIT_MESH": "EDIT", "EDIT_ARMATURE": "EDIT", "EDIT_CURVE": "EDIT",
            "PAINT_WEIGHT": "WEIGHT_PAINT", "PAINT_VERTEX": "VERTEX_PAINT",
            "PAINT_TEXTURE": "TEXTURE_PAINT",
        }.get(state["mode"], state["mode"])
        try:
            bpy.ops.object.mode_set(mode=mode)
        except RuntimeError:
            pass


def _push_undo(message):
    try:
        bpy.ops.ed.undo_push(message=message)
        return True
    except RuntimeError:
        return False


def _set_rna_properties(owner, properties):
    changed = {}
    for key, value in (properties or {}).items():
        if not hasattr(owner, key):
            raise ValueError(f"Property '{key}' is not available on {type(owner).__name__}")
        prop = owner.bl_rna.properties.get(key) if hasattr(owner, "bl_rna") else None
        if prop and getattr(prop, "type", "") == 'POINTER' and isinstance(value, str):
            fixed_type = getattr(prop, "fixed_type", None)
            if fixed_type and fixed_type.identifier == "Object":
                value = _object_or_error(value)
        try:
            setattr(owner, key, value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Could not set '{key}': {exc}") from exc
        changed[key] = value.name if hasattr(value, "name") else value
    return changed


def _tool_inspect_object(args: dict) -> str:
    name = args.get("object_name", "")
    requested = set(args.get("sections") or [
        "mesh_topology", "materials", "modifiers", "armature",
        "vertex_groups", "animation", "constraints",
    ])
    limit = max(1, min(int(args.get("max_items", 100) or 100), 500))

    def _inspect():
        try:
            obj = _object_or_error(name)
        except ValueError as exc:
            return f"Error: {exc}"
        result = {
            "name": obj.name, "type": obj.type,
            "location": list(obj.location), "rotation_euler": list(obj.rotation_euler),
            "scale": list(obj.scale), "dimensions": list(obj.dimensions),
            "parent": obj.parent.name if obj.parent else None,
            "visible": obj.visible_get(), "hide_render": obj.hide_render,
            "collections": [col.name for col in obj.users_collection][:limit],
            "bounds": {"max_items": limit, "truncated_sections": []},
        }
        if "mesh_topology" in requested and obj.type == 'MESH':
            mesh = obj.data
            result["mesh_topology"] = {
                "vertices": len(mesh.vertices), "edges": len(mesh.edges),
                "polygons": len(mesh.polygons), "loops": len(mesh.loops),
                "uv_layers": [uv.name for uv in mesh.uv_layers][:limit],
                "color_attributes": [a.name for a in mesh.color_attributes][:limit],
                "shape_keys": [k.name for k in mesh.shape_keys.key_blocks][:limit] if mesh.shape_keys else [],
            }
        if "materials" in requested and hasattr(obj.data, "materials"):
            items = [{"slot": i, "name": mat.name if mat else None} for i, mat in enumerate(obj.data.materials)]
            result["materials"] = items[:limit]
            if len(items) > limit:
                result["bounds"]["truncated_sections"].append("materials")
        if "modifiers" in requested:
            items = [{"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport,
                      "show_render": mod.show_render} for mod in obj.modifiers]
            result["modifiers"] = items[:limit]
            if len(items) > limit:
                result["bounds"]["truncated_sections"].append("modifiers")
        if "armature" in requested:
            arm_obj = obj if obj.type == 'ARMATURE' else obj.find_armature()
            if arm_obj:
                bones = [{
                    "name": bone.name, "parent": bone.parent.name if bone.parent else None,
                    "head_local": list(bone.head_local), "tail_local": list(bone.tail_local),
                    "use_deform": bone.use_deform,
                } for bone in arm_obj.data.bones]
                result["armature"] = {"object": arm_obj.name, "bones": bones[:limit],
                                      "bones_truncated": len(bones) > limit}
        if "vertex_groups" in requested:
            groups = [{"name": group.name, "index": group.index} for group in obj.vertex_groups]
            result["vertex_groups"] = groups[:limit]
            if len(groups) > limit:
                result["bounds"]["truncated_sections"].append("vertex_groups")
        if "animation" in requested:
            anim = obj.animation_data
            action = anim.action if anim else None
            result["animation"] = {
                "action": action.name if action else None,
                "drivers": len(anim.drivers) if anim else 0,
                "nla_tracks": [track.name for track in anim.nla_tracks][:limit] if anim else [],
            }
        if "constraints" in requested:
            constraints = [{"name": con.name, "type": con.type, "mute": con.mute,
                            "influence": con.influence} for con in obj.constraints]
            result["constraints"] = constraints[:limit]
            if len(constraints) > limit:
                result["bounds"]["truncated_sections"].append("constraints")
        return _json_result(result)

    return _schedule_on_main(_inspect)


def _tool_validate_mesh(args: dict) -> str:
    name = args.get("object_name", "")
    threshold = max(0.0, float(args.get("merge_distance", 0.0001) or 0.0001))
    sample_limit = max(1, min(int(args.get("max_samples", 20) or 20), 100))

    def _validate():
        obj = bpy.data.objects.get(name)
        if not obj:
            return f"Error: Object '{name}' not found"
        if obj.type != 'MESH':
            return f"Error: Object '{name}' is not a mesh"
        state = _capture_context()
        bm = bmesh.new()
        try:
            _ensure_object_mode()
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            non_manifold = [e.index for e in bm.edges if len(e.link_faces) != 2]
            boundary = [e.index for e in bm.edges if len(e.link_faces) == 1]
            loose_edges = [e.index for e in bm.edges if not e.link_faces]
            loose_verts = [v.index for v in bm.verts if not v.link_edges]
            zero_edges = [e.index for e in bm.edges if e.calc_length() <= 1.0e-12]
            degenerate = [f.index for f in bm.faces if f.calc_area() <= 1.0e-12]
            doubles = bmesh.ops.find_doubles(bm, verts=bm.verts, dist=threshold).get("targetmap", {})
            inconsistent = []
            for edge in bm.edges:
                if len(edge.link_faces) != 2:
                    continue
                directions = []
                for face in edge.link_faces:
                    loop = next((loop for loop in face.loops if loop.edge == edge), None)
                    if loop:
                        directions.append(loop.vert == edge.verts[0])
                if len(directions) == 2 and directions[0] == directions[1]:
                    inconsistent.append(edge.index)
            center = sum((v.co for v in bm.verts), Vector((0.0, 0.0, 0.0))) / max(1, len(bm.verts))
            inward = [f.index for f in bm.faces if f.normal.dot(f.calc_center_median() - center) < -1.0e-8]

            def issue(indices):
                return {"count": len(indices), "sample_indices": indices[:sample_limit],
                        "samples_truncated": len(indices) > sample_limit}

            return _json_result({
                "object": obj.name, "thresholds": {"duplicate_distance": threshold},
                "topology": {"vertices": len(bm.verts), "edges": len(bm.edges), "faces": len(bm.faces)},
                "issues": {
                    "non_manifold_edges": issue(non_manifold),
                    "boundary_edges": issue(boundary),
                    "loose_vertices": issue(loose_verts),
                    "loose_edges": issue(loose_edges),
                    "degenerate_faces": issue(degenerate),
                    "zero_length_edges": issue(zero_edges),
                    "duplicate_near_vertices": {
                        "count": len(doubles),
                        "sample_indices": [v.index for v in list(doubles)[:sample_limit]],
                        "samples_truncated": len(doubles) > sample_limit,
                    },
                    "inconsistent_normal_edges": issue(inconsistent),
                    "possible_inward_faces": issue(inward),
                },
                "notes": ["Possible inward faces use a centroid heuristic and may be noisy on open or concave meshes."],
            })
        except Exception as exc:
            return f"Error validating mesh '{name}': {exc}"
        finally:
            bm.free()
            _restore_context(state)

    return _schedule_on_main(_validate)


def _tool_edit_mesh(args: dict) -> str:
    name = args.get("object_name", "")
    action = args.get("action", "")

    def _edit():
        obj = bpy.data.objects.get(name)
        if not obj:
            return f"Error: Object '{name}' not found"
        if obj.type != 'MESH':
            return f"Error: Object '{name}' is not a mesh"
        state = _capture_context()
        bm = None
        undo_pushed = False
        try:
            _ensure_object_mode()
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            undo_pushed = _push_undo(f"Copilot edit_mesh: {action}")
            before = {"vertices": len(obj.data.vertices), "edges": len(obj.data.edges), "faces": len(obj.data.polygons)}
            if action in {"shade_smooth", "shade_flat"}:
                smooth = action == "shade_smooth"
                selected_only = bool(args.get("selected_only", False))
                affected = 0
                for poly in obj.data.polygons:
                    if not selected_only or poly.select:
                        poly.use_smooth = smooth
                        affected += 1
                obj.data.update()
            else:
                bm = bmesh.new()
                bm.from_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                selected_only = bool(args.get("selected_only", False))
                verts = [v for v in bm.verts if v.select] if selected_only else list(bm.verts)
                edges = [e for e in bm.edges if e.select] if selected_only else list(bm.edges)
                faces = [f for f in bm.faces if f.select] if selected_only else list(bm.faces)
                affected = 0
                if action == "merge_by_distance":
                    result = bmesh.ops.remove_doubles(bm, verts=verts, dist=float(args.get("distance", 0.0001)))
                    affected = len(result.get("targetmap", {}))
                elif action == "recalculate_normals":
                    bmesh.ops.recalc_face_normals(bm, faces=faces)
                    affected = len(faces)
                elif action == "triangulate":
                    result = bmesh.ops.triangulate(bm, faces=faces)
                    affected = len(result.get("faces", []))
                elif action == "dissolve_degenerate":
                    result = bmesh.ops.dissolve_degenerate(
                        bm, edges=edges, dist=float(args.get("distance", 0.0001)),
                    )
                    affected = len(result.get("region", []))
                elif action == "delete_loose":
                    loose_edges = [e for e in edges if not e.link_faces]
                    loose_verts = [v for v in verts if not v.link_edges]
                    affected = len(loose_edges) + len(loose_verts)
                    if loose_edges:
                        bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')
                    loose_verts = [v for v in bm.verts if (not selected_only or v.select) and not v.link_edges]
                    if loose_verts:
                        bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')
                elif action == "subdivide":
                    result = bmesh.ops.subdivide_edges(
                        bm, edges=edges, cuts=max(1, int(args.get("cuts", 1))),
                        smooth=float(args.get("smooth", 0.0)),
                    )
                    affected = len(result.get("geom_inner", []))
                elif action == "bevel":
                    result = bmesh.ops.bevel(
                        bm, geom=edges, offset=float(args.get("offset", 0.1)),
                        segments=max(1, int(args.get("segments", 1))), affect='EDGES',
                    )
                    affected = len(result.get("faces", []))
                elif action == "solidify":
                    result = bmesh.ops.solidify(
                        bm, geom=faces, thickness=float(args.get("thickness", 0.1)),
                    )
                    affected = len(result.get("geom", []))
                else:
                    return f"Error: Unknown edit_mesh action '{action}'"
                bm.to_mesh(obj.data)
                obj.data.update()
            after = {"vertices": len(obj.data.vertices), "edges": len(obj.data.edges), "faces": len(obj.data.polygons)}
            return _json_result({"action": action, "object": obj.name, "affected": affected,
                                 "before": before, "after": after, "undo_pushed": undo_pushed})
        except Exception as exc:
            return f"Error editing mesh '{name}' with '{action}': {exc}"
        finally:
            if bm:
                bm.free()
            _restore_context(state)

    return _schedule_on_main(_edit)


def _tool_manage_object(args: dict) -> str:
    action = args.get("action", "")

    def _manage():
        names = list(args.get("object_names") or [])
        if args.get("object_name") and args["object_name"] not in names:
            names.append(args["object_name"])
        if args.get("selected_only"):
            names = [obj.name for obj in bpy.context.selected_objects]
        objects = [bpy.data.objects.get(name) for name in names]
        missing = [name for name, obj in zip(names, objects) if obj is None]
        if missing:
            return f"Error: Objects not found: {', '.join(missing)}"
        objects = [obj for obj in objects if obj]
        if action not in {"select"} and not objects:
            return "Error: object_name, object_names, or selected_only is required"
        state = _capture_context()
        try:
            if action in {"rename", "parent", "unparent", "hide", "show"}:
                _push_undo(f"Copilot object {action}")
            if action == "select":
                if not args.get("additive", False):
                    for obj in bpy.context.selected_objects:
                        obj.select_set(False)
                for obj in objects:
                    obj.select_set(True)
                return _json_result({"action": action, "selected": [o.name for o in bpy.context.selected_objects]})
            if action == "set_active":
                bpy.context.view_layer.objects.active = objects[0]
                objects[0].select_set(True)
            elif action == "transform":
                _push_undo("Copilot transform objects")
                for obj in objects:
                    if "location" in args:
                        obj.location = args["location"]
                    if "rotation" in args:
                        obj.rotation_euler = args["rotation"]
                    if "scale" in args:
                        obj.scale = args["scale"]
            elif action == "rename":
                if len(objects) != 1 or not args.get("new_name"):
                    return "Error: rename requires one object and new_name"
                old = objects[0].name
                objects[0].name = args["new_name"]
                state["selected"] = [objects[0].name if item == old else item for item in state["selected"]]
                if state["active"] == old:
                    state["active"] = objects[0].name
                names = [objects[0].name]
                return _json_result({"action": action, "old_name": old, "new_name": objects[0].name})
            elif action == "duplicate":
                _push_undo("Copilot duplicate objects")
                copies = []
                for obj in objects:
                    clone = obj.copy()
                    if obj.data:
                        clone.data = obj.data.copy()
                    (obj.users_collection[0] if obj.users_collection else bpy.context.scene.collection).objects.link(clone)
                    copies.append(clone)
                if len(copies) == 1 and args.get("new_name"):
                    copies[0].name = args["new_name"]
                return _json_result({"action": action, "created": [o.name for o in copies]})
            elif action == "delete":
                _ensure_object_mode()
                _push_undo("Copilot delete objects")
                removed = [obj.name for obj in objects]
                for obj in objects:
                    bpy.data.objects.remove(obj, do_unlink=True)
                return _json_result({"action": action, "deleted": removed})
            elif action == "parent":
                parent = bpy.data.objects.get(args.get("parent_name", ""))
                if not parent:
                    return f"Error: Parent object '{args.get('parent_name', '')}' not found"
                for obj in objects:
                    world = obj.matrix_world.copy()
                    obj.parent = parent
                    if args.get("keep_transform", True):
                        obj.matrix_world = world
            elif action == "unparent":
                for obj in objects:
                    world = obj.matrix_world.copy()
                    obj.parent = None
                    if args.get("keep_transform", True):
                        obj.matrix_world = world
            elif action == "set_origin":
                _ensure_object_mode()
                _push_undo("Copilot set origin")
                for obj in bpy.context.selected_objects:
                    obj.select_set(False)
                for obj in objects:
                    obj.select_set(True)
                bpy.context.view_layer.objects.active = objects[0]
                bpy.ops.object.origin_set(type=args.get("origin_type", "ORIGIN_GEOMETRY"))
            elif action in {"hide", "show"}:
                hidden = action == "hide"
                for obj in objects:
                    obj.hide_set(hidden)
                    if "hide_render" in args:
                        obj.hide_render = bool(args["hide_render"]) if hidden else False
            else:
                return f"Error: Unknown manage_object action '{action}'"
            return _json_result({"action": action, "objects": [obj.name for obj in objects]})
        except Exception as exc:
            return f"Error managing object ({action}): {exc}"
        finally:
            if action not in {"select", "set_active", "delete"}:
                _restore_context(state)

    return _schedule_on_main(_manage)


def _tool_manage_modifier(args: dict) -> str:
    action = args.get("action", "")
    object_name = args.get("object_name", "")

    def _manage():
        obj = bpy.data.objects.get(object_name)
        if not obj:
            return f"Error: Object '{object_name}' not found"
        if action == "list":
            return _json_result({"object": obj.name, "modifiers": [
                {"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport,
                 "show_render": mod.show_render} for mod in obj.modifiers
            ]})
        state = _capture_context()
        try:
            if action == "add":
                _push_undo("Copilot add modifier")
                mod_type = args.get("modifier_type", "")
                if not mod_type:
                    return "Error: modifier_type is required for add"
                mod = obj.modifiers.new(name=args.get("modifier_name") or mod_type, type=mod_type)
                try:
                    changed = _set_rna_properties(mod, args.get("properties"))
                except Exception:
                    obj.modifiers.remove(mod)
                    raise
                return _json_result({"action": action, "object": obj.name,
                                     "modifier": {"name": mod.name, "type": mod.type}, "properties": changed})
            name = args.get("modifier_name", "")
            mod = obj.modifiers.get(name)
            if not mod:
                return f"Error: Modifier '{name}' not found on '{obj.name}'"
            if action == "set":
                _push_undo("Copilot set modifier")
                changed = _set_rna_properties(mod, args.get("properties"))
            elif action in {"apply", "move_up", "move_down"}:
                _ensure_object_mode()
                for selected in bpy.context.selected_objects:
                    selected.select_set(False)
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                _push_undo(f"Copilot modifier {action}")
                if action == "apply":
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                elif action == "move_up":
                    bpy.ops.object.modifier_move_up(modifier=mod.name)
                else:
                    bpy.ops.object.modifier_move_down(modifier=mod.name)
                changed = {}
            elif action == "remove":
                _push_undo("Copilot remove modifier")
                obj.modifiers.remove(mod)
                changed = {}
            else:
                return f"Error: Unknown manage_modifier action '{action}'"
            return _json_result({"action": action, "object": obj.name, "modifier": name,
                                 "properties": changed})
        except Exception as exc:
            return f"Error managing modifier on '{object_name}': {exc}"
        finally:
            _restore_context(state)

    return _schedule_on_main(_manage)


def _tool_manage_armature(args: dict) -> str:
    action = args.get("action", "")

    def _armature_object():
        name = args.get("armature_name") or args.get("object_name", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Armature object '{name}' not found")
        if obj.type != 'ARMATURE':
            raise ValueError(f"Object '{name}' is not an armature")
        return obj

    def _manage():
        limit = max(1, min(int(args.get("max_items", 100) or 100), 500))
        if action == "create":
            name = args.get("armature_name") or args.get("object_name") or "Armature"
            state = _capture_context()
            try:
                _ensure_object_mode()
                _push_undo("Copilot create armature")
                data = bpy.data.armatures.new(name)
                arm = bpy.data.objects.new(name, data)
                bpy.context.collection.objects.link(arm)
                arm.show_in_front = True
                if args.get("bone_name"):
                    bpy.context.view_layer.objects.active = arm
                    arm.select_set(True)
                    bpy.ops.object.mode_set(mode='EDIT')
                    bone = data.edit_bones.new(args["bone_name"])
                    bone.head = args.get("head", [0, 0, 0])
                    bone.tail = args.get("tail", [0, 0, 1])
                return _json_result({"action": action, "armature": arm.name})
            except Exception as exc:
                return f"Error creating armature: {exc}"
            finally:
                _restore_context(state)
        try:
            arm = _armature_object()
        except ValueError as exc:
            return f"Error: {exc}"
        if action == "inspect":
            bones = [{
                "name": bone.name, "parent": bone.parent.name if bone.parent else None,
                "head_local": list(bone.head_local), "tail_local": list(bone.tail_local),
                "use_connect": bone.use_connect, "use_deform": bone.use_deform,
                "constraints": [{"name": con.name, "type": con.type} for con in arm.pose.bones[bone.name].constraints][:limit],
            } for bone in arm.data.bones]
            return _json_result({"armature": arm.name, "bones": bones[:limit],
                                 "total_bones": len(bones), "truncated": len(bones) > limit})
        state = _capture_context()
        try:
            _ensure_object_mode()
            for obj in bpy.context.selected_objects:
                obj.select_set(False)
            arm.select_set(True)
            bpy.context.view_layer.objects.active = arm
            _push_undo(f"Copilot armature: {action}")
            if action in {"add_bone", "edit_bone", "parent_bone"}:
                bpy.ops.object.mode_set(mode='EDIT')
                bones = arm.data.edit_bones
                if action == "add_bone":
                    name = args.get("bone_name") or "Bone"
                    bone = bones.new(name)
                else:
                    bone = bones.get(args.get("bone_name", ""))
                    if not bone:
                        return f"Error: Bone '{args.get('bone_name', '')}' not found"
                if action != "parent_bone":
                    if "head" in args:
                        bone.head = args["head"]
                    elif action == "add_bone":
                        bone.head = [0, 0, 0]
                    if "tail" in args:
                        bone.tail = args["tail"]
                    elif action == "add_bone":
                        bone.tail = [0, 0, 1]
                    if "roll" in args:
                        bone.roll = float(args["roll"])
                    if args.get("new_name"):
                        bone.name = args["new_name"]
                parent_name = args.get("parent_name")
                if action == "parent_bone" or parent_name:
                    parent = bones.get(parent_name or "")
                    if not parent:
                        return f"Error: Parent bone '{parent_name}' not found"
                    if parent == bone:
                        return "Error: A bone cannot parent itself"
                    bone.parent = parent
                    bone.use_connect = bool(args.get("use_connect", False))
                result_name = bone.name
                bpy.ops.object.mode_set(mode='OBJECT')
                return _json_result({"action": action, "armature": arm.name, "bone": result_name})
            if action == "add_constraint":
                ctype = args.get("constraint_type", "")
                if not ctype:
                    return "Error: constraint_type is required"
                bone_name = args.get("bone_name")
                owner = arm.pose.bones.get(bone_name) if bone_name else arm
                if bone_name and not owner:
                    return f"Error: Pose bone '{bone_name}' not found"
                con = owner.constraints.new(type=ctype)
                target_name = args.get("target_name")
                if target_name:
                    con.target = _object_or_error(target_name)
                if args.get("subtarget") and hasattr(con, "subtarget"):
                    con.subtarget = args["subtarget"]
                changed = _set_rna_properties(con, args.get("properties"))
                return _json_result({"action": action, "constraint": con.name,
                                     "type": con.type, "properties": changed})
            if action == "bind_mesh":
                mesh = _object_or_error(args.get("object_name", ""))
                if mesh.type != 'MESH':
                    return f"Error: Object '{mesh.name}' is not a mesh"
                for obj in bpy.context.selected_objects:
                    obj.select_set(False)
                mesh.select_set(True)
                arm.select_set(True)
                bpy.context.view_layer.objects.active = arm
                if args.get("automatic_weights", True):
                    bpy.ops.object.parent_set(type='ARMATURE_AUTO')
                else:
                    mesh.parent = arm
                    mod = mesh.modifiers.new(name="Armature", type='ARMATURE')
                    mod.object = arm
                return _json_result({"action": action, "mesh": mesh.name, "armature": arm.name,
                                     "automatic_weights": bool(args.get("automatic_weights", True))})
            if action == "normalize_weights":
                mesh = _object_or_error(args.get("object_name", ""))
                if mesh.type != 'MESH':
                    return f"Error: Object '{mesh.name}' is not a mesh"
                changed = 0
                groups = list(mesh.vertex_groups)
                for vertex in mesh.data.vertices:
                    weights = [(group, group.weight(vertex.index)) for group in groups
                               if any(link.group == group.index for link in vertex.groups)]
                    total = sum(weight for _, weight in weights)
                    if total > 0:
                        for group, weight in weights:
                            group.add([vertex.index], weight / total, 'REPLACE')
                        changed += 1
                return _json_result({"action": action, "mesh": mesh.name, "vertices_normalized": changed})
            return f"Error: Unknown manage_armature action '{action}'"
        except Exception as exc:
            return f"Error managing armature ({action}): {exc}"
        finally:
            _restore_context(state)

    return _schedule_on_main(_manage)


def _action_fcurves(action, obj=None):
    if not action:
        return []
    if hasattr(action, "fcurves"):
        try:
            return list(action.fcurves)
        except Exception:
            pass
    curves = []
    slot = getattr(getattr(obj, "animation_data", None), "action_slot", None)
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            bags = getattr(strip, "channelbags", None)
            if bags is not None:
                for bag in bags:
                    curves.extend(list(bag.fcurves))
            elif slot and hasattr(strip, "channelbag"):
                bag = strip.channelbag(slot)
                if bag:
                    curves.extend(list(bag.fcurves))
    return curves


def _assign_data_path(owner, data_path, value, index=-1):
    if data_path.startswith('["') and data_path.endswith('"]'):
        owner[data_path[2:-2]] = value
        return
    if "." in data_path:
        parent_path, attribute = data_path.rsplit(".", 1)
        parent = owner.path_resolve(parent_path)
    else:
        parent, attribute = owner, data_path
    current = getattr(parent, attribute)
    if index is not None and index >= 0:
        current[index] = value
    else:
        setattr(parent, attribute, value)


def _tool_manage_animation(args: dict) -> str:
    action = args.get("action", "")

    def _manage():
        if action == "set_frame_range":
            _push_undo("Copilot set frame range")
            start = int(args.get("start", bpy.context.scene.frame_start))
            end = int(args.get("end", bpy.context.scene.frame_end))
            if end < start:
                return "Error: end must be greater than or equal to start"
            bpy.context.scene.frame_start = start
            bpy.context.scene.frame_end = end
            return _json_result({"action": action, "start": start, "end": end})
        obj = bpy.data.objects.get(args.get("object_name", ""))
        if not obj:
            return f"Error: Object '{args.get('object_name', '')}' not found"
        data_path = args.get("data_path", "")
        index = int(args.get("index", -1) if args.get("index") is not None else -1)
        try:
            if action != "inspect_action":
                _push_undo(f"Copilot animation: {action}")
            if action == "set_keyframe":
                if not data_path or "frame" not in args:
                    return "Error: data_path and frame are required"
                if "value" in args:
                    _assign_data_path(obj, data_path, args["value"], index)
                options = {"data_path": data_path, "frame": float(args["frame"])}
                if index >= 0:
                    options["index"] = index
                success = obj.keyframe_insert(**options)
                return _json_result({"action": action, "object": obj.name, "data_path": data_path,
                                     "frame": args["frame"], "inserted": bool(success)})
            if action == "delete_keyframe":
                if not data_path or "frame" not in args:
                    return "Error: data_path and frame are required"
                options = {"data_path": data_path, "frame": float(args["frame"])}
                if index >= 0:
                    options["index"] = index
                success = obj.keyframe_delete(**options)
                return _json_result({"action": action, "object": obj.name, "deleted": bool(success)})
            anim = obj.animation_data
            current_action = anim.action if anim else None
            curves = _action_fcurves(current_action, obj)
            if action == "inspect_action":
                limit = max(1, min(int(args.get("max_items", 100) or 100), 500))
                items = []
                for curve in curves[:limit]:
                    points = [{"frame": point.co.x, "value": point.co.y,
                               "interpolation": point.interpolation}
                              for point in curve.keyframe_points[:limit]]
                    items.append({"data_path": curve.data_path, "array_index": curve.array_index,
                                  "keyframes": points,
                                  "keyframes_truncated": len(curve.keyframe_points) > limit})
                return _json_result({
                    "object": obj.name, "action": current_action.name if current_action else None,
                    "frame_range": list(current_action.frame_range) if current_action else None,
                    "fcurves": items, "total_fcurves": len(curves),
                    "fcurves_truncated": len(curves) > limit,
                })
            if action == "set_interpolation":
                interpolation = args.get("interpolation", "BEZIER")
                changed = 0
                for curve in curves:
                    if data_path and curve.data_path != data_path:
                        continue
                    if index >= 0 and curve.array_index != index:
                        continue
                    for point in curve.keyframe_points:
                        if "frame" not in args or abs(point.co.x - float(args["frame"])) < 1.0e-4:
                            point.interpolation = interpolation
                            changed += 1
                return _json_result({"action": action, "object": obj.name, "changed": changed})
            if action == "create_driver":
                if not data_path:
                    return "Error: data_path is required"
                fcurve = obj.driver_add(data_path, index) if index >= 0 else obj.driver_add(data_path)
                if isinstance(fcurve, (list, tuple)):
                    if not fcurve:
                        return "Error: No driver curve was created"
                    fcurve = fcurve[0]
                driver = fcurve.driver
                driver.type = 'SCRIPTED'
                driver.expression = args.get("expression", "0")
                for spec in args.get("variables") or []:
                    variable = driver.variables.new()
                    variable.name = spec.get("name", "var")
                    variable.type = spec.get("type", "SINGLE_PROP")
                    target = variable.targets[0]
                    target.id = _object_or_error(spec["target_object"]) if spec.get("target_object") else obj
                    if variable.type == 'SINGLE_PROP':
                        target.data_path = spec.get("data_path", "")
                    elif variable.type == 'TRANSFORMS':
                        target.bone_target = spec.get("bone_target", "")
                        target.transform_type = spec.get("transform_type", "LOC_X")
                        target.transform_space = spec.get("transform_space", "WORLD_SPACE")
                return _json_result({"action": action, "object": obj.name, "data_path": data_path,
                                     "index": index, "expression": driver.expression,
                                     "variables": len(driver.variables)})
            if action == "remove_driver":
                if not data_path:
                    return "Error: data_path is required"
                removed = obj.driver_remove(data_path, index) if index >= 0 else obj.driver_remove(data_path)
                return _json_result({"action": action, "object": obj.name, "removed": bool(removed)})
            return f"Error: Unknown manage_animation action '{action}'"
        except Exception as exc:
            return f"Error managing animation ({action}): {exc}"

    return _schedule_on_main(_manage)


def _material_from_args(args, create=False):
    name = args.get("material_name", "")
    material = bpy.data.materials.get(name) if name else None
    if not material and create and name:
        material = bpy.data.materials.new(name)
    obj = bpy.data.objects.get(args.get("object_name", "")) if args.get("object_name") else None
    if not material and obj and obj.active_material:
        material = obj.active_material
    if not material:
        raise ValueError(f"Material '{name}' not found")
    return material


def _node_socket(sockets, identifier):
    if isinstance(identifier, int):
        if identifier < 0 or identifier >= len(sockets):
            raise ValueError(f"Socket index {identifier} is out of range")
        return sockets[identifier]
    socket = sockets.get(str(identifier))
    if not socket:
        raise ValueError(f"Socket '{identifier}' not found")
    return socket


def _socket_value(value):
    return value


def _tool_manage_material_nodes(args: dict) -> str:
    action = args.get("action", "")

    def _manage():
        if action == "assign_material":
            obj = bpy.data.objects.get(args.get("object_name", ""))
            if not obj or not hasattr(obj.data, "materials"):
                return f"Error: Mesh-capable object '{args.get('object_name', '')}' not found"
            try:
                material = _material_from_args(args, create=True)
            except ValueError as exc:
                return f"Error: {exc}"
            _push_undo("Copilot assign material")
            if obj.data.materials:
                obj.data.materials[0] = material
            else:
                obj.data.materials.append(material)
            return _json_result({"action": action, "object": obj.name, "material": material.name})
        try:
            material = _material_from_args(args)
        except ValueError as exc:
            return f"Error: {exc}"
        material.use_nodes = True
        tree = material.node_tree
        nodes = tree.nodes
        links = tree.links
        limit = max(1, min(int(args.get("max_items", 100) or 100), 500))
        try:
            if action == "inspect":
                node_items = []
                for node in list(nodes)[:limit]:
                    node_items.append({
                        "name": node.name, "type": node.bl_idname, "label": node.label,
                        "location": list(node.location),
                        "inputs": [{"name": socket.name, "type": socket.type,
                                    "linked": socket.is_linked}
                                   for socket in list(node.inputs)[:limit]],
                        "outputs": [{"name": socket.name, "type": socket.type,
                                     "linked": socket.is_linked}
                                    for socket in list(node.outputs)[:limit]],
                    })
                link_items = [{
                    "from_node": link.from_node.name, "from_socket": link.from_socket.name,
                    "to_node": link.to_node.name, "to_socket": link.to_socket.name,
                } for link in list(links)[:limit]]
                return _json_result({"material": material.name, "nodes": node_items,
                                     "total_nodes": len(nodes), "nodes_truncated": len(nodes) > limit,
                                     "links": link_items, "total_links": len(links),
                                     "links_truncated": len(links) > limit})
            _push_undo(f"Copilot material nodes: {action}")
            if action == "add_node":
                node_type = args.get("node_type", "")
                if not node_type:
                    return "Error: node_type is required"
                node = nodes.new(type=node_type)
                if args.get("node_name"):
                    node.name = args["node_name"]
                if args.get("location"):
                    node.location = args["location"]
                return _json_result({"action": action, "material": material.name,
                                     "node": node.name, "type": node.bl_idname})
            if action == "remove_node":
                node = nodes.get(args.get("node_name", ""))
                if not node:
                    return f"Error: Node '{args.get('node_name', '')}' not found"
                name = node.name
                nodes.remove(node)
                return _json_result({"action": action, "material": material.name, "node": name})
            if action == "set_input":
                node = nodes.get(args.get("node_name", ""))
                if not node:
                    return f"Error: Node '{args.get('node_name', '')}' not found"
                socket = _node_socket(node.inputs, args.get("socket"))
                if not hasattr(socket, "default_value"):
                    return f"Error: Socket '{socket.name}' has no editable default value"
                socket.default_value = _socket_value(args.get("value"))
                return _json_result({"action": action, "node": node.name, "socket": socket.name})
            if action == "link":
                from_node = nodes.get(args.get("from_node", ""))
                to_node = nodes.get(args.get("to_node", ""))
                if not from_node or not to_node:
                    return "Error: from_node and to_node must name existing nodes"
                out_socket = _node_socket(from_node.outputs, args.get("from_socket"))
                in_socket = _node_socket(to_node.inputs, args.get("to_socket"))
                link = links.new(out_socket, in_socket)
                return _json_result({"action": action, "from": [link.from_node.name, link.from_socket.name],
                                     "to": [link.to_node.name, link.to_socket.name]})
            if action == "unlink":
                removed = 0
                for link in list(links):
                    if args.get("from_node") and link.from_node.name != args["from_node"]:
                        continue
                    if args.get("to_node") and link.to_node.name != args["to_node"]:
                        continue
                    if "from_socket" in args and link.from_socket != _node_socket(link.from_node.outputs, args["from_socket"]):
                        continue
                    if "to_socket" in args and link.to_socket != _node_socket(link.to_node.inputs, args["to_socket"]):
                        continue
                    links.remove(link)
                    removed += 1
                return _json_result({"action": action, "removed_links": removed})
            if action == "load_image_texture":
                path = _resolve_path(args.get("path", ""))
                if not os.path.isfile(path):
                    return f"Error: Image not found: {path}"
                target = None
                if args.get("to_node"):
                    target = nodes.get(args["to_node"])
                    if not target:
                        return f"Error: Target node '{args['to_node']}' not found"
                image = bpy.data.images.load(path, check_existing=True)
                node = nodes.new(type='ShaderNodeTexImage')
                node.image = image
                if args.get("node_name"):
                    node.name = args["node_name"]
                if args.get("location"):
                    node.location = args["location"]
                if target:
                    links.new(_node_socket(node.outputs, args.get("from_socket", "Color")),
                              _node_socket(target.inputs, args.get("to_socket", "Base Color")))
                return _json_result({"action": action, "material": material.name,
                                     "node": node.name, "image": image.name, "path": path})
            return f"Error: Unknown manage_material_nodes action '{action}'"
        except Exception as exc:
            return f"Error managing material nodes ({action}): {exc}"

    return _schedule_on_main(_manage)


def _render_settings(scene):
    render = scene.render
    data = {
        "engine": render.engine,
        "resolution_x": render.resolution_x, "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "fps": render.fps, "filepath": render.filepath,
        "file_format": render.image_settings.file_format,
        "film_transparent": render.film_transparent,
        "camera": scene.camera.name if scene.camera else None,
    }
    if scene.render.engine == 'CYCLES':
        data["samples"] = scene.cycles.samples
    return data


def _set_render_engine(render, value):
    available = {
        item.identifier
        for item in render.bl_rna.properties["engine"].enum_items
    }
    aliases = {
        "BLENDER_EEVEE_NEXT": "BLENDER_EEVEE",
        "BLENDER_EEVEE": "BLENDER_EEVEE_NEXT",
    }
    engine = str(value)
    if engine not in available:
        engine = aliases.get(engine, engine)
    if engine not in available:
        raise ValueError(
            f"Render engine '{value}' is unavailable; choose from {', '.join(sorted(available))}"
        )
    render.engine = engine


def _tool_manage_render(args: dict) -> str:
    action = args.get("action", "")

    def _manage():
        scene = bpy.context.scene
        try:
            if action == "inspect":
                return _json_result({"scene": scene.name, "render": _render_settings(scene)})
            _push_undo(f"Copilot render: {action}")
            if action == "set":
                settings = args.get("settings") or {}
                render = scene.render
                setters = {
                    "engine": lambda v: _set_render_engine(render, v),
                    "resolution_x": lambda v: setattr(render, "resolution_x", int(v)),
                    "resolution_y": lambda v: setattr(render, "resolution_y", int(v)),
                    "resolution_percentage": lambda v: setattr(render, "resolution_percentage", int(v)),
                    "fps": lambda v: setattr(render, "fps", int(v)),
                    "filepath": lambda v: setattr(render, "filepath", v),
                    "file_format": lambda v: setattr(render.image_settings, "file_format", v),
                    "film_transparent": lambda v: setattr(render, "film_transparent", bool(v)),
                    "samples": lambda v: setattr(scene.cycles, "samples", int(v)),
                }
                unknown = [key for key in settings if key not in setters]
                if unknown:
                    return f"Error: Unsupported render settings: {', '.join(unknown)}"
                for key, value in settings.items():
                    setters[key](value)
                return _json_result({"action": action, "render": _render_settings(scene)})
            if action == "create_camera":
                name = args.get("name") or args.get("object_name") or "Camera"
                data = bpy.data.cameras.new(name)
                obj = bpy.data.objects.new(name, data)
                bpy.context.collection.objects.link(obj)
                obj.location = args.get("location", [0, 0, 0])
                obj.rotation_euler = args.get("rotation", [0, 0, 0])
                return _json_result({"action": action, "camera": obj.name})
            if action == "create_light":
                name = args.get("name") or args.get("object_name") or "Light"
                data = bpy.data.lights.new(name=name, type=args.get("light_type", "POINT"))
                data.energy = float(args.get("energy", 1000))
                if args.get("color"):
                    data.color = args["color"]
                obj = bpy.data.objects.new(name, data)
                bpy.context.collection.objects.link(obj)
                obj.location = args.get("location", [0, 0, 0])
                obj.rotation_euler = args.get("rotation", [0, 0, 0])
                return _json_result({"action": action, "light": obj.name, "type": data.type})
            if action == "set_active_camera":
                obj = _object_or_error(args.get("object_name", ""))
                if obj.type != 'CAMERA':
                    return f"Error: Object '{obj.name}' is not a camera"
                scene.camera = obj
                return _json_result({"action": action, "camera": obj.name})
            if action == "render_still":
                path = args.get("filepath") or (args.get("settings") or {}).get("filepath") or scene.render.filepath
                if not path:
                    path = os.path.join(tempfile.gettempdir(), "copilot_render.png")
                path = _resolve_path(path)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                scene.render.filepath = path
                bpy.ops.render.render(write_still=True)
                absolute = bpy.path.abspath(scene.render.filepath)
                return f"__RENDER_IMAGE__:{absolute}\n" + _json_result({
                    "action": action, "filepath": absolute, "render": _render_settings(scene),
                })
            return f"Error: Unknown manage_render action '{action}'"
        except Exception as exc:
            return f"Error managing render ({action}): {exc}"

    return _schedule_on_main(_manage)


def _tool_export_game_asset(args: dict) -> str:
    preset = args.get("preset", "")
    if not args.get("filepath"):
        return "Error: filepath is required"
    filepath = os.path.abspath(_resolve_path(args.get("filepath", "")))
    selected = bool(args.get("selected_only", False))
    animation = bool(args.get("include_animation", True))
    apply_transform = bool(args.get("apply_transform", True))

    def _export():
        try:
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            if preset == "UNITY_FBX":
                bpy.ops.export_scene.fbx(
                    filepath=filepath, use_selection=selected, axis_forward='-Z', axis_up='Y',
                    global_scale=1.0, apply_unit_scale=True, use_space_transform=apply_transform,
                    bake_space_transform=False, add_leaf_bones=False, bake_anim=animation,
                )
            elif preset == "UNREAL_FBX":
                bpy.ops.export_scene.fbx(
                    filepath=filepath, use_selection=selected, axis_forward='-Y', axis_up='Z',
                    global_scale=1.0, apply_unit_scale=True, use_space_transform=apply_transform,
                    bake_space_transform=False, add_leaf_bones=False, bake_anim=animation,
                )
            elif preset == "GLTF_GAME":
                bpy.ops.export_scene.gltf(
                    filepath=filepath, use_selection=selected, export_animations=animation,
                    export_apply=apply_transform,
                )
            else:
                return f"Error: Unknown game export preset '{preset}'"
            return _json_result({"preset": preset, "filepath": filepath, "selected_only": selected,
                                 "include_animation": animation, "apply_transform": apply_transform})
        except Exception as exc:
            return f"Error exporting game asset ({preset}): {exc}"

    return _schedule_on_main(_export)


def _tool_manage_undo(args: dict) -> str:
    action = args.get("action", "")

    def _manage():
        try:
            if action == "push":
                bpy.ops.ed.undo_push(message=args.get("message", "Copilot checkpoint"))
            elif action == "undo":
                bpy.ops.ed.undo()
            else:
                return f"Error: Unknown undo action '{action}'"
            return _json_result({"action": action, "message": args.get("message")})
        except RuntimeError as exc:
            return f"Error managing undo ({action}): {exc}"

    return _schedule_on_main(_manage)


def _tool_render_preview(args: dict) -> str:
    output = args.get("output_path", "")
    res_x = args.get("resolution_x", 960)
    res_y = args.get("resolution_y", 540)
    engine = args.get("engine", "")
    samples = args.get("samples", 64)

    # Use temp dir if no explicit path or Blender-relative path
    if not output or output.startswith("//"):
        import tempfile
        output = os.path.join(tempfile.gettempdir(), "copilot_render_preview.png")

    def _render():
        scene = bpy.context.scene
        _push_undo("Copilot render preview")
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
        scene.render.filepath = output
        scene.render.image_settings.file_format = 'PNG'

        if engine:
            _set_render_engine(scene.render, engine)

        if scene.render.engine == 'CYCLES':
            scene.cycles.samples = samples
        elif hasattr(scene, 'eevee'):
            scene.eevee.taa_render_samples = samples

        bpy.ops.render.render(write_still=True)
        abs_path = bpy.path.abspath(output)
        # __RENDER_IMAGE__ marker tells the API client to send this image
        # back to the model for visual analysis
        return f"__RENDER_IMAGE__:{abs_path}\nRendered to: {abs_path} ({res_x}x{res_y}, {scene.render.engine}, {samples} samples)"

    return _schedule_on_main(_render)


def _tool_screenshot_viewport(args: dict) -> str:
    """Capture a screenshot of the 3D viewport."""
    import tempfile
    output = args.get("output_path", os.path.join(tempfile.gettempdir(), "copilot_viewport_screenshot.png"))

    def _capture():
        # Find 3D viewport area
        area_3d = None
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area_3d = area
                break
        if not area_3d:
            return "Error: No 3D viewport found"

        # Use offscreen render of the viewport
        for space in area_3d.spaces:
            if space.type == 'VIEW_3D':
                # Use Blender's built-in screenshot
                override = bpy.context.copy()
                override['area'] = area_3d
                with bpy.context.temp_override(**override):
                    bpy.ops.screen.screenshot_area(filepath=output)
                return f"__RENDER_IMAGE__:{output}\nViewport screenshot saved to: {output}"

        return "Error: Could not capture viewport"

    return _schedule_on_main(_capture)


def _tool_manage_collection(args: dict) -> str:
    action = args.get("action", "")
    name = args.get("name", "")
    new_name = args.get("new_name", "")
    obj_name = args.get("object_name", "")

    def _manage():
        _push_undo(f"Copilot collection: {action}")
        if action == "create":
            if name in bpy.data.collections:
                return f"Collection '{name}' already exists"
            col = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(col)
            return f"Created collection '{name}'"

        elif action == "rename":
            col = bpy.data.collections.get(name)
            if not col:
                return f"Error: Collection '{name}' not found"
            col.name = new_name
            return f"Renamed collection '{name}' → '{new_name}'"

        elif action == "link_object":
            col = bpy.data.collections.get(name)
            obj = bpy.data.objects.get(obj_name)
            if not col:
                return f"Error: Collection '{name}' not found"
            if not obj:
                return f"Error: Object '{obj_name}' not found"
            if obj.name not in col.objects:
                col.objects.link(obj)
            return f"Linked '{obj_name}' to collection '{name}'"

        elif action == "unlink_object":
            col = bpy.data.collections.get(name)
            obj = bpy.data.objects.get(obj_name)
            if not col:
                return f"Error: Collection '{name}' not found"
            if not obj:
                return f"Error: Object '{obj_name}' not found"
            if obj.name in col.objects:
                col.objects.unlink(obj)
            return f"Unlinked '{obj_name}' from collection '{name}'"

        return f"Error: Unknown action: {action}"

    return _schedule_on_main(_manage)


def _tool_import_asset(args: dict) -> str:
    filepath = _resolve_path(args.get("filepath", ""))
    fmt = args.get("format", "").upper()
    if not filepath or not os.path.isfile(filepath):
        return f"Error: Asset not found: {filepath}"

    if not fmt:
        ext = os.path.splitext(filepath)[1].lower()
        fmt = {
            ".fbx": "FBX", ".obj": "OBJ", ".gltf": "GLTF", ".glb": "GLTF",
            ".stl": "STL", ".ply": "PLY", ".abc": "ABC", ".usd": "USD",
            ".usda": "USD", ".usdc": "USD",
        }.get(ext, "")

    def _import():
        importers = {
            "FBX": lambda: bpy.ops.import_scene.fbx(filepath=filepath),
            "OBJ": lambda: bpy.ops.wm.obj_import(filepath=filepath),
            "GLTF": lambda: bpy.ops.import_scene.gltf(filepath=filepath),
            "STL": lambda: bpy.ops.wm.stl_import(filepath=filepath),
            "PLY": lambda: bpy.ops.wm.ply_import(filepath=filepath),
            "ABC": lambda: bpy.ops.wm.alembic_import(filepath=filepath),
            "USD": lambda: bpy.ops.wm.usd_import(filepath=filepath),
        }
        if fmt not in importers:
            return f"Error: Unsupported format: {fmt}"
        try:
            _push_undo(f"Copilot import {fmt}")
            importers[fmt]()
            return f"Imported {fmt}: {filepath}"
        except Exception as e:
            return f"Error importing: {e}"

    return _schedule_on_main(_import)


def _tool_export_asset(args: dict) -> str:
    if not args.get("filepath"):
        return "Error: filepath is required"
    filepath = _resolve_path(args.get("filepath", ""))
    fmt = args.get("format", "").upper()
    selected_only = args.get("selected_only", False)

    def _export():
        exporters = {
            "FBX": lambda: bpy.ops.export_scene.fbx(filepath=filepath, use_selection=selected_only),
            "OBJ": lambda: bpy.ops.wm.obj_export(filepath=filepath, export_selected_objects=selected_only),
            "GLTF": lambda: bpy.ops.export_scene.gltf(filepath=filepath, use_selection=selected_only),
            "STL": lambda: bpy.ops.wm.stl_export(filepath=filepath, export_selected_objects=selected_only),
            "PLY": lambda: bpy.ops.wm.ply_export(filepath=filepath),
            "USD": lambda: bpy.ops.wm.usd_export(filepath=filepath, selected_objects_only=selected_only),
        }
        if fmt not in exporters:
            return f"Error: Unsupported format: {fmt}"
        try:
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            exporters[fmt]()
            return f"Exported {fmt}: {filepath}"
        except Exception as e:
            return f"Error exporting: {e}"

    return _schedule_on_main(_export)


def _tool_view_image(args: dict) -> str:
    """View a local image file — sends it to the model for visual analysis."""
    filepath = _resolve_path(args.get("filepath", ""))
    if not filepath or not os.path.isfile(filepath):
        return f"Error: Image not found: {filepath}"
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif"):
        return f"Error: Not a supported image format: {ext}"
    size = os.path.getsize(filepath)
    if size > 20_000_000:
        return f"Error: Image too large ({size // 1024 // 1024}MB). Max 20MB."
    return f"__RENDER_IMAGE__:{filepath}\nViewing image: {filepath} ({size // 1024}KB)"


# ── Tool dispatch ─────────────────────────────────────────────────────────

_TOOL_MAP = {
    # File tools (thread-safe)
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "list_directory": _tool_list_directory,
    "create_directory": _tool_create_directory,
    "delete_file": _tool_delete_file,
    "copy_file": _tool_copy_file,
    "move_file": _tool_move_file,
    "search_files": _tool_search_files,
    "get_file_info": _tool_get_file_info,
    "get_project_structure": _tool_get_project_structure,
    "web_search": _tool_web_search,
    "search_blender_docs": _tool_search_blender_docs,
    # Blender tools (main-thread via _schedule_on_main)
    "execute_python_script": _tool_execute_python_script,
    "get_blender_version": _tool_get_blender_version,
    "inspect_blender_api": _tool_inspect_blender_api,
    "get_scene_info": _tool_get_scene_info,
    "create_mesh": _tool_create_mesh,
    "create_material": _tool_create_material,
    "add_modifier": _tool_add_modifier,
    "inspect_object": _tool_inspect_object,
    "validate_mesh": _tool_validate_mesh,
    "edit_mesh": _tool_edit_mesh,
    "manage_object": _tool_manage_object,
    "manage_modifier": _tool_manage_modifier,
    "manage_armature": _tool_manage_armature,
    "manage_animation": _tool_manage_animation,
    "manage_material_nodes": _tool_manage_material_nodes,
    "manage_render": _tool_manage_render,
    "export_game_asset": _tool_export_game_asset,
    "manage_undo": _tool_manage_undo,
    "render_preview": _tool_render_preview,
    "screenshot_viewport": _tool_screenshot_viewport,
    "manage_collection": _tool_manage_collection,
    "import_asset": _tool_import_asset,
    "export_asset": _tool_export_asset,
    "view_image": _tool_view_image,
}


def execute_tool(name: str, args: dict, cancel_event=None) -> str:
    """Execute a tool by name with the given arguments. Returns result string."""
    handler = _TOOL_MAP.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'. Available: {', '.join(_TOOL_MAP.keys())}"
    previous_cancel_event = getattr(_tool_runtime, "cancel_event", None)
    _tool_runtime.cancel_event = cancel_event
    try:
        return handler(args)
    except Exception as e:
        return _bounded_text(
            f"Error executing {name}: {e}\n{traceback.format_exc()}",
            DEFAULT_OUTPUT_LIMIT,
            f"copilot_{name}_error",
        )
    finally:
        _tool_runtime.cancel_event = previous_cancel_event
