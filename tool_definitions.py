"""
Tool definitions sent to the Copilot API.
Contains both universal file tools and Blender-specific tools.
"""


def _capability_tool_definitions() -> list:
    """Focused, multi-action Blender capability schemas."""
    number3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
    value = {
        "description": "JSON-compatible scalar or array value",
        "oneOf": [
            {"type": "number"}, {"type": "boolean"},
            {"type": "string"}, {"type": "array", "items": {"type": "number"}},
        ],
    }

    def tool(name, description, properties, required=()):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                    "additionalProperties": False,
                },
            },
        }

    return [
        tool("inspect_object", "Inspect one Blender object with bounded structured detail.", {
            "object_name": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {"type": "string", "enum": [
                    "mesh_topology", "materials", "modifiers", "armature",
                    "vertex_groups", "animation", "constraints",
                ]},
                "description": "Optional detail sections; all sections are included when omitted.",
            },
            "max_items": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Per-section item cap (default 100)"},
        }, ["object_name"]),
        tool("validate_mesh", "Analyze a mesh for common topology and normal problems.", {
            "object_name": {"type": "string"},
            "merge_distance": {"type": "number", "minimum": 0, "description": "Near-duplicate threshold (default 0.0001)"},
            "max_samples": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Sample index cap (default 20)"},
        }, ["object_name"]),
        tool("edit_mesh", "Perform a focused mesh edit while preserving Blender selection and mode.", {
            "object_name": {"type": "string"},
            "action": {"type": "string", "enum": [
                "merge_by_distance", "recalculate_normals", "shade_smooth", "shade_flat",
                "triangulate", "dissolve_degenerate", "delete_loose", "subdivide",
                "bevel", "solidify",
            ]},
            "distance": {"type": "number", "minimum": 0},
            "cuts": {"type": "integer", "minimum": 1, "maximum": 100},
            "smooth": {"type": "number"},
            "offset": {"type": "number"},
            "segments": {"type": "integer", "minimum": 1, "maximum": 64},
            "thickness": {"type": "number"},
            "selected_only": {"type": "boolean", "description": "Operate on selected mesh elements when possible"},
        }, ["object_name", "action"]),
        tool("manage_object", "Select, transform, organize, duplicate, or remove scene objects.", {
            "action": {"type": "string", "enum": [
                "select", "set_active", "transform", "rename", "duplicate", "delete",
                "parent", "unparent", "set_origin", "hide", "show",
            ]},
            "object_name": {"type": "string"},
            "object_names": {"type": "array", "items": {"type": "string"}, "maxItems": 500},
            "parent_name": {"type": "string"},
            "new_name": {"type": "string"},
            "location": number3, "rotation": number3, "scale": number3,
            "selected_only": {"type": "boolean"},
            "additive": {"type": "boolean"},
            "keep_transform": {"type": "boolean"},
            "origin_type": {"type": "string", "enum": ["ORIGIN_GEOMETRY", "ORIGIN_CURSOR", "ORIGIN_CENTER_OF_MASS", "ORIGIN_CENTER_OF_VOLUME", "GEOMETRY_ORIGIN"]},
            "hide_render": {"type": "boolean"},
        }, ["action"]),
        tool("manage_modifier", "List, add, edit, apply, remove, or reorder an object's modifiers.", {
            "action": {"type": "string", "enum": ["list", "add", "set", "apply", "remove", "move_up", "move_down"]},
            "object_name": {"type": "string"},
            "modifier_name": {"type": "string"},
            "modifier_type": {"type": "string"},
            "properties": {"type": "object", "description": "Modifier RNA properties to set"},
        }, ["action", "object_name"]),
        tool("manage_armature", "Inspect or perform common armature, bone, constraint, and skinning operations.", {
            "action": {"type": "string", "enum": [
                "inspect", "create", "add_bone", "edit_bone", "parent_bone",
                "add_constraint", "bind_mesh", "normalize_weights",
            ]},
            "armature_name": {"type": "string"},
            "object_name": {"type": "string", "description": "Mesh object, or armature fallback"},
            "bone_name": {"type": "string"},
            "new_name": {"type": "string"},
            "parent_name": {"type": "string"},
            "target_name": {"type": "string"},
            "subtarget": {"type": "string"},
            "head": number3, "tail": number3,
            "roll": {"type": "number"},
            "use_connect": {"type": "boolean"},
            "constraint_type": {"type": "string"},
            "properties": {"type": "object"},
            "automatic_weights": {"type": "boolean"},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 500},
        }, ["action"]),
        tool("manage_animation", "Manage keyframes, actions, interpolation, drivers, and frame ranges.", {
            "action": {"type": "string", "enum": [
                "set_keyframe", "delete_keyframe", "inspect_action", "set_frame_range",
                "set_interpolation", "create_driver", "remove_driver",
            ]},
            "object_name": {"type": "string"},
            "data_path": {"type": "string"},
            "frame": {"type": "number"},
            "value": value,
            "index": {"type": "integer", "minimum": -1},
            "interpolation": {"type": "string", "enum": ["CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART", "QUINT", "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC"]},
            "expression": {"type": "string"},
            "variables": {"type": "array", "items": {"type": "object"}, "maxItems": 32},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 500},
        }, ["action"]),
        tool("manage_material_nodes", "Inspect and edit shader nodes, links, material assignment, and image textures.", {
            "action": {"type": "string", "enum": [
                "inspect", "add_node", "remove_node", "set_input", "link", "unlink",
                "assign_material", "load_image_texture",
            ]},
            "material_name": {"type": "string"},
            "object_name": {"type": "string"},
            "node_name": {"type": "string"},
            "node_type": {"type": "string"},
            "from_node": {"type": "string"}, "to_node": {"type": "string"},
            "from_socket": {"description": "Socket name or index", "oneOf": [{"type": "string"}, {"type": "integer"}]},
            "to_socket": {"description": "Socket name or index", "oneOf": [{"type": "string"}, {"type": "integer"}]},
            "socket": {"description": "Input socket name or index", "oneOf": [{"type": "string"}, {"type": "integer"}]},
            "value": value,
            "path": {"type": "string"},
            "location": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 500},
        }, ["action"]),
        tool("manage_render", "Inspect/configure rendering, create cameras/lights, or render a still.", {
            "action": {"type": "string", "enum": ["inspect", "set", "create_camera", "create_light", "set_active_camera", "render_still"]},
            "object_name": {"type": "string"},
            "name": {"type": "string"},
            "location": number3, "rotation": number3,
            "light_type": {"type": "string", "enum": ["POINT", "SUN", "SPOT", "AREA"]},
            "energy": {"type": "number"},
            "color": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "settings": {"type": "object", "description": "Render settings such as engine, resolution_x/y, percentage, fps, filepath, file_format, samples, film_transparent"},
            "filepath": {"type": "string"},
        }, ["action"]),
        tool("export_game_asset", "Export with target-appropriate Unity FBX, Unreal FBX, or game glTF settings.", {
            "preset": {"type": "string", "enum": ["UNITY_FBX", "UNREAL_FBX", "GLTF_GAME"]},
            "filepath": {"type": "string"},
            "selected_only": {"type": "boolean"},
            "include_animation": {"type": "boolean"},
            "apply_transform": {"type": "boolean"},
        }, ["preset", "filepath"]),
        tool("manage_undo", "Push an undo checkpoint or undo the latest Blender operation.", {
            "action": {"type": "string", "enum": ["push", "undo"]},
            "message": {"type": "string"},
        }, ["action"]),
    ]


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
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the web for current information. Use this when the user asks for recent facts, "
                    "external references, or anything likely to have changed since model training."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Web search query"},
                        "limit": {"type": "integer", "description": "Maximum results to return, 1-10 (default 5)"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_blender_docs",
                "description": (
                    "Search official Blender documentation. Use for Blender Python API, modifier, node, "
                    "operator, shader, geometry nodes, rendering, and UI workflow questions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Blender documentation topic to search"},
                        "doc_area": {
                            "type": "string",
                            "description": "Documentation area to search (default all)",
                            "enum": ["all", "api", "manual", "dev"],
                        },
                        "docs_path": {
                            "type": "string",
                            "description": "Optional local Blender docs folder/file path. If omitted, uses configured /docs path.",
                        },
                        "limit": {"type": "integer", "description": "Maximum results to return, 1-10 (default 6)"},
                    },
                    "required": ["query"],
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
                "name": "get_blender_version",
                "description": (
                    "Get the current Blender runtime version, Python version, build metadata, platform, "
                    "and whether Blender is running in background mode."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_blender_api",
                "description": (
                    "Inspect the live Blender Python API built into the current Blender process. "
                    "Use this to check bpy/bmesh/mathutils classes, functions, operators, docstrings, "
                    "RNA properties, and available members for the installed Blender version."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "API path starting with bpy, bmesh, or mathutils, e.g. bpy.ops.mesh.primitive_cube_add or bpy.types.Object",
                        },
                        "include_members": {"type": "boolean", "description": "Include dir() member names (default true)"},
                        "member_filter": {"type": "string", "description": "Optional substring filter for member names"},
                        "max_members": {"type": "integer", "minimum": 1, "maximum": 150, "description": "Max members/properties to return (default 80)"},
                    },
                    "required": ["target"],
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
                        "max_items": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Maximum objects/materials/collections returned (default 100)"},
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
        *_capability_tool_definitions(),
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
                            "enum": ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "CYCLES", "BLENDER_WORKBENCH"],
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
        {
            "type": "function",
            "function": {
                "name": "view_image",
                "description": "View a local image file from the user's computer. Use this when the user shares a file path to an image (screenshot, reference, texture, etc.) and you need to see it. The image will be sent to you for visual analysis. Supports PNG, JPG, BMP, WebP, TIFF.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string", "description": "Absolute path to the image file on the local filesystem"},
                    },
                    "required": ["filepath"],
                    "additionalProperties": False,
                },
            },
        },
    ]
