"""
Tool definitions sent to the Copilot API.
Contains both universal file tools and Blender-specific tools.
"""


def get_blender_tool_definitions() -> list:
    """Return the full list of tool JSON schemas for the API."""
    return [
        # ── Universal file tools ──────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the contents of a file. Returns file content with line numbers. Use start_line/end_line for large files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (relative to blend file directory, or absolute)"},
                        "start_line": {"type": "integer", "description": "Optional 1-based start line"},
                        "end_line": {"type": "integer", "description": "Optional 1-based end line"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file. Creates the file if it doesn't exist. Backs up existing files before overwriting.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to write"},
                        "content": {"type": "string", "description": "Full file content to write"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace a specific string in a file. old_str must match exactly one occurrence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "old_str": {"type": "string", "description": "Exact string to find (must be unique in file)"},
                        "new_str": {"type": "string", "description": "Replacement string"},
                    },
                    "required": ["path", "old_str", "new_str"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "List files and subdirectories in a directory. Returns names with [DIR] or [FILE] prefix.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path"},
                        "recursive": {"type": "boolean", "description": "If true, list recursively (default false)"},
                        "max_depth": {"type": "integer", "description": "Max recursion depth (default 2)"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_directory",
                "description": "Create a directory (and parents if needed).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path to create"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": "Delete a file or empty directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to delete"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "copy_file",
                "description": "Copy a file to a new location.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source file path"},
                        "destination": {"type": "string", "description": "Destination file path"},
                    },
                    "required": ["source", "destination"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "move_file",
                "description": "Move or rename a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source path"},
                        "destination": {"type": "string", "description": "Destination path"},
                    },
                    "required": ["source", "destination"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "Search for a text pattern in files. Returns matching lines with file paths and line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Text or regex pattern to search"},
                        "path": {"type": "string", "description": "Directory to search in (default: project root)"},
                        "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py')"},
                        "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default true)"},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_file_info",
                "description": "Get file metadata: size, modification time, type.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_project_structure",
                "description": "Get an overview of the project directory: top-level files and directories with sizes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Root directory (default: blend file directory)"},
                        "max_depth": {"type": "integer", "description": "Max depth to traverse (default 3)"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },

        # ── Blender-specific tools ────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "execute_python_script",
                "description": (
                    "Execute a Python script in the Blender context. "
                    "The script has access to bpy, mathutils, bmesh, and all Blender modules. "
                    "Use this for creating/modifying meshes, materials, modifiers, armatures, animations, "
                    "node trees, drivers, constraints, particle systems, physics, and any Blender operation. "
                    "Returns stdout output and any error traceback."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python code to execute in Blender"},
                        "description": {"type": "string", "description": "Brief description of what the script does"},
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_scene_info",
                "description": (
                    "Get detailed information about the current Blender scene: "
                    "objects (name, type, location, modifiers), materials, collections, "
                    "render settings, active camera, world settings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_objects": {"type": "boolean", "description": "Include object list (default true)"},
                        "include_materials": {"type": "boolean", "description": "Include materials (default true)"},
                        "include_render": {"type": "boolean", "description": "Include render settings (default false)"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_mesh",
                "description": (
                    "Create a primitive mesh object (cube, sphere, cylinder, cone, plane, torus, monkey) "
                    "and add it to the scene."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "primitive": {
                            "type": "string",
                            "description": "Mesh type: cube, uv_sphere, ico_sphere, cylinder, cone, plane, torus, monkey",
                            "enum": ["cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "plane", "torus", "monkey"],
                        },
                        "name": {"type": "string", "description": "Object name (optional)"},
                        "location": {
                            "type": "array", "items": {"type": "number"},
                            "description": "Location [x, y, z] (default [0,0,0])",
                        },
                        "scale": {
                            "type": "array", "items": {"type": "number"},
                            "description": "Scale [x, y, z] (default [1,1,1])",
                        },
                        "size": {"type": "number", "description": "Size/radius parameter (default 1.0)"},
                    },
                    "required": ["primitive"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_material",
                "description": (
                    "Create a new material with Principled BSDF shader. "
                    "Optionally assign it to a named object."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Material name"},
                        "base_color": {
                            "type": "array", "items": {"type": "number"},
                            "description": "RGBA base color [r, g, b, a] values 0-1 (default [0.8, 0.8, 0.8, 1.0])",
                        },
                        "metallic": {"type": "number", "description": "Metallic value 0-1 (default 0.0)"},
                        "roughness": {"type": "number", "description": "Roughness value 0-1 (default 0.5)"},
                        "assign_to": {"type": "string", "description": "Object name to assign material to (optional)"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_modifier",
                "description": "Add a modifier to a named object.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "object_name": {"type": "string", "description": "Name of the object"},
                        "modifier_type": {
                            "type": "string",
                            "description": (
                                "Modifier type identifier (e.g. SUBSURF, MIRROR, ARRAY, BEVEL, "
                                "SOLIDIFY, BOOLEAN, DECIMATE, REMESH, SMOOTH, SHRINKWRAP, etc.)"
                            ),
                        },
                        "properties": {
                            "type": "object",
                            "description": "Key-value pairs of modifier properties to set",
                        },
                    },
                    "required": ["object_name", "modifier_type"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "render_preview",
                "description": "Render the current scene and get a visual image back for analysis. After rendering, you will see the result image and can evaluate the scene visually to suggest improvements to lighting, materials, composition, etc.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string", "description": "Output image file path (default: //render_preview.png)"},
                        "resolution_x": {"type": "integer", "description": "Width in pixels (default 960)"},
                        "resolution_y": {"type": "integer", "description": "Height in pixels (default 540)"},
                        "engine": {
                            "type": "string",
                            "description": "Render engine: EEVEE, CYCLES, WORKBENCH",
                            "enum": ["BLENDER_EEVEE_NEXT", "CYCLES", "BLENDER_WORKBENCH"],
                        },
                        "samples": {"type": "integer", "description": "Render samples (default 64)"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "screenshot_viewport",
                "description": "Capture a screenshot of the 3D viewport as it currently appears (including wireframes, overlays, selections). Returns the image for visual analysis. Faster than a full render — use this to quickly check your work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string", "description": "Output image file path (default: auto temp path)"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "manage_collection",
                "description": "Create, rename, or link objects to a collection.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Action: create, rename, link_object, unlink_object",
                            "enum": ["create", "rename", "link_object", "unlink_object"],
                        },
                        "name": {"type": "string", "description": "Collection name"},
                        "new_name": {"type": "string", "description": "New name (for rename action)"},
                        "object_name": {"type": "string", "description": "Object to link/unlink"},
                    },
                    "required": ["action", "name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "import_asset",
                "description": "Import a file into the Blender scene (FBX, OBJ, glTF, STL, PLY, ABC, USD).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string", "description": "Path to import file"},
                        "format": {
                            "type": "string",
                            "description": "File format hint (auto-detected from extension if omitted)",
                            "enum": ["FBX", "OBJ", "GLTF", "STL", "PLY", "ABC", "USD"],
                        },
                    },
                    "required": ["filepath"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "export_asset",
                "description": "Export the scene or selected objects to a file (FBX, OBJ, glTF, STL, PLY, USD).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string", "description": "Output file path"},
                        "format": {
                            "type": "string",
                            "description": "Export format",
                            "enum": ["FBX", "OBJ", "GLTF", "STL", "PLY", "USD"],
                        },
                        "selected_only": {"type": "boolean", "description": "Export selected objects only (default false)"},
                    },
                    "required": ["filepath", "format"],
                    "additionalProperties": False,
                },
            },
        },
    ]
