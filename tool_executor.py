"""
Tool executor — runs tool calls locally and returns results.
Universal file tools + Blender-specific scene/mesh/material/modifier tools.

IMPORTANT: Blender-specific tools that touch bpy must be scheduled on the main
thread (via _schedule_on_main) since bpy is not thread-safe. File I/O tools
are thread-safe and can run directly.
"""

import fnmatch
import json
import os
import re
import shutil
import stat
import time
import traceback
from datetime import datetime
from io import StringIO

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
_main_results_lock = threading.Lock()
_exec_counter = 0


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
    _dbg = os.path.join(os.environ.get("TEMP", "/tmp"), "copilot_blender_ipc", "debug_drain.log")
    with open(_dbg, "a") as _df:
        _df.write(f"{time.time():.1f} scheduled eid={eid} func={func.__name__}\n")
        _df.flush()
    deadline = time.time() + 120
    while time.time() < deadline:
        with _main_results_lock:
            if eid in _main_results:
                result = _main_results.pop(eid)
                if isinstance(result, Exception):
                    return f"Error: {result}"
                return result
        time.sleep(0.05)
    with open(_dbg, "a") as _df:
        _df.write(f"{time.time():.1f} TIMEOUT eid={eid} (30s)\n")
        _df.flush()
    return "Error: Main-thread execution timed out (30s)"


def drain_main_queue():
    """Called from the modal timer on Blender's main thread."""
    _dbg = os.path.join(os.environ.get("TEMP", "/tmp"), "copilot_blender_ipc", "debug_drain.log")
    with _main_queue_lock:
        pending = list(_main_queue)
        _main_queue.clear()

    if pending:
        with open(_dbg, "a") as _df:
            _df.write(f"{time.time():.1f} draining {len(pending)} items\n")
            _df.flush()

    for eid, func, args, kwargs in pending:
        try:
            with open(_dbg, "a") as _df:
                _df.write(f"{time.time():.1f} exec eid={eid} func={func.__name__}\n")
                _df.flush()
            result = func(*args, **kwargs)
            with open(_dbg, "a") as _df:
                _df.write(f"{time.time():.1f} done eid={eid} result_len={len(str(result))}\n")
                _df.flush()
        except Exception as e:
            result = e
            with open(_dbg, "a") as _df:
                import traceback as _tb
                _df.write(f"{time.time():.1f} ERROR eid={eid}: {e}\n{_tb.format_exc()}\n")
                _df.flush()
        with _main_results_lock:
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

    start = args.get("start_line", 1)
    end = args.get("end_line", len(lines))
    start = max(1, start)
    end = min(len(lines), end)

    numbered = []
    for i in range(start - 1, end):
        numbered.append(f"{i + 1}. {lines[i].rstrip()}")
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
    return "\n".join(entries[:500]) if entries else "(empty directory)"


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
                            if len(results) >= 200:
                                results.append("... (truncated at 200 matches)")
                                return "\n".join(results)
            except OSError:
                continue

    return "\n".join(results) if results else "No matches found."


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
    return "\n".join(lines[:500])


# ── Blender-specific tools (run on main thread) ──────────────────────────

def _tool_execute_python_script(args: dict) -> str:
    code = args.get("code", "")
    desc = args.get("description", "AI-generated script")

    def _exec():
        import sys
        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
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
            return f"Script executed successfully.\n{output}" if output else "Script executed successfully."
        except Exception:
            output = captured.getvalue()
            tb = traceback.format_exc()
            return f"Script error:\n{tb}\nOutput so far:\n{output}"
        finally:
            sys.stdout = old_stdout

    return _schedule_on_main(_exec)


def _tool_get_scene_info(args: dict) -> str:
    inc_objects = args.get("include_objects", True)
    inc_materials = args.get("include_materials", True)
    inc_render = args.get("include_render", False)

    def _gather():
        info = {"scene": bpy.context.scene.name}

        if inc_objects:
            objs = []
            for obj in bpy.context.scene.objects:
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

        if inc_materials:
            mats = []
            for mat in bpy.data.materials:
                m = {"name": mat.name, "use_nodes": mat.use_nodes}
                if mat.use_nodes and mat.node_tree:
                    m["nodes"] = [n.bl_idname for n in mat.node_tree.nodes]
                mats.append(m)
            info["materials"] = mats

        if inc_render:
            r = bpy.context.scene.render
            info["render"] = {
                "engine": r.engine,
                "resolution": [r.resolution_x, r.resolution_y],
                "fps": r.fps,
                "filepath": r.filepath,
            }

        collections = []
        for col in bpy.data.collections:
            collections.append({
                "name": col.name,
                "objects": [o.name for o in col.objects],
            })
        info["collections"] = collections

        return json.dumps(info, indent=2)

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
    obj_name = args.get("object_name", "")
    mod_type = args.get("modifier_type", "")
    props = args.get("properties", {})

    def _add():
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            return f"Error: Object '{obj_name}' not found"
        try:
            mod = obj.modifiers.new(name=mod_type, type=mod_type)
        except Exception as e:
            return f"Error adding modifier: {e}"

        for k, v in (props or {}).items():
            if hasattr(mod, k):
                try:
                    setattr(mod, k, v)
                except Exception as e:
                    return f"Error setting {k}: {e}"
        return f"Added modifier '{mod_type}' to '{obj_name}'"

    return _schedule_on_main(_add)


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
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
        scene.render.filepath = output
        scene.render.image_settings.file_format = 'PNG'

        if engine:
            scene.render.engine = engine

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
            importers[fmt]()
            return f"Imported {fmt}: {filepath}"
        except Exception as e:
            return f"Error importing: {e}"

    return _schedule_on_main(_import)


def _tool_export_asset(args: dict) -> str:
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
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            exporters[fmt]()
            return f"Exported {fmt}: {filepath}"
        except Exception as e:
            return f"Error exporting: {e}"

    return _schedule_on_main(_export)


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
    # Blender tools (main-thread via _schedule_on_main)
    "execute_python_script": _tool_execute_python_script,
    "get_scene_info": _tool_get_scene_info,
    "create_mesh": _tool_create_mesh,
    "create_material": _tool_create_material,
    "add_modifier": _tool_add_modifier,
    "render_preview": _tool_render_preview,
    "screenshot_viewport": _tool_screenshot_viewport,
    "manage_collection": _tool_manage_collection,
    "import_asset": _tool_import_asset,
    "export_asset": _tool_export_asset,
}


def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name with the given arguments. Returns result string."""
    handler = _TOOL_MAP.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'. Available: {', '.join(_TOOL_MAP.keys())}"
    try:
        return handler(args)
    except Exception as e:
        return f"Error executing {name}: {e}\n{traceback.format_exc()}"
