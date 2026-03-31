# GitHub Copilot for Blender

Full-featured AI assistant addon for Blender with agentic tool-calling, OAuth authentication, multi-model support, and a dockable sidebar panel.

## Features

- **GitHub OAuth Device Flow** — One-click sign-in with token persistence across sessions
- **Multi-Model Support** — Dynamic model catalog from Copilot API with full model picker (GPT, Claude, Gemini, etc.)
- **Dockable N-Panel** — Sidebar panel in the 3D Viewport with chat, model selector, actions, and tool log
- **Agentic Tool-Calling** — Automatic tool-call loop: Copilot reads/writes files, creates meshes, materials, modifiers, runs Python scripts, and more
- **20 Built-in Tools**:
  - **File ops**: read, write, edit, delete, copy, move, search, list directory, get info, project structure
  - **Blender ops**: execute Python script, get scene info, create mesh, create material, add modifier, render preview, manage collections, import/export assets
- **File Uploads** — Attach images (base64 inline) and text files to messages
- **Shared Auth** — Single sign-in state shared across all addon features
- **Thinking Indicator** — Visual feedback while Copilot is processing
- **Auto-Retry** — Transport failure auto-retry with diagnostics
- **API-Truth Model Labels** — Model shown in chat is always what the API actually returned

## Requirements

- **Blender 4.2+** (tested with Blender 5.0)
- **GitHub Copilot subscription** (Free, Pro, Pro+, Business, or Enterprise)
- **Internet connection** for API calls
- No external Python packages required (uses stdlib `urllib`)

## Installation

### Method 1: Blender Extensions (Blender 4.2+)
1. Open Blender → Edit → Preferences → Add-ons
2. Click "Install from Disk..."
3. Navigate to `GitHubCopilotBlender/` folder and select it (or ZIP it first)
4. Enable "GitHub Copilot for Blender" in the addon list

### Method 2: Manual Install
1. Copy the `GitHubCopilotBlender/` folder to your Blender addons directory:
   - **Windows**: `%APPDATA%\Blender\<version>\scripts\addons\`
   - **macOS**: `~/Library/Application Support/Blender/<version>/scripts/addons/`
   - **Linux**: `~/.config/blender/<version>/scripts/addons/`
2. Restart Blender
3. Enable in Preferences → Add-ons

### Method 3: Blender-Wide Install
1. Copy `GitHubCopilotBlender/` to:
   - `<Blender Install>/scripts/addons/GitHubCopilotBlender/`
2. This makes the addon available to all users on the machine

## Quick Start

1. **Open the panel**: In the 3D Viewport, press `N` to toggle the sidebar, then click the **Copilot** tab
2. **Sign in**: Click "Sign In" — your browser opens GitHub's device authorization page. Enter the code shown in the panel
3. **Select a model**: Expand the Model section and pick your preferred model
4. **Chat**: Type in the prompt field and click Send (▶) or press Enter
5. **Use actions**: Expand Actions for quick operations (Analyze Scene, Create Object, Generate Script, etc.)

## Settings

Access via Edit → Preferences → Add-ons → GitHub Copilot for Blender:

| Setting | Default | Description |
|---------|---------|-------------|
| Request Timeout | 600s | HTTP timeout per request. Increase for complex tool-chain prompts |
| Max Tool Iterations | 0 (unlimited) | Safety cap for agentic loops |
| Require Patch Preview | On | Show diff before file changes |
| Allowed Write Roots | (empty = any) | Restrict file write locations |
| Verbose Logging | Off | Print detailed logs to console |
| Display Name | (empty) | Your name in the chat transcript |

## Tool Reference

### File Tools
| Tool | Description |
|------|-------------|
| `read_file` | Read file with line numbers |
| `write_file` | Write/create files (auto-backup) |
| `edit_file` | Find-and-replace in files |
| `list_directory` | List directory contents |
| `create_directory` | Create directories |
| `delete_file` | Delete files/empty dirs |
| `copy_file` | Copy files |
| `move_file` | Move/rename files |
| `search_files` | Regex search in files |
| `get_file_info` | File metadata |
| `get_project_structure` | Project tree overview |

### Blender Tools
| Tool | Description |
|------|-------------|
| `execute_python_script` | Run Python code in Blender context (bpy, bmesh, mathutils) |
| `get_scene_info` | Scene objects, materials, collections, render settings |
| `create_mesh` | Create primitive meshes (cube, sphere, cylinder, etc.) |
| `create_material` | Create PBR materials with Principled BSDF |
| `add_modifier` | Add modifiers (Subdivision, Mirror, Array, etc.) |
| `render_preview` | Render and save image |
| `manage_collection` | Create/rename/link collections |
| `import_asset` | Import FBX, OBJ, glTF, STL, PLY, ABC, USD |
| `export_asset` | Export to FBX, OBJ, glTF, STL, PLY, USD |

## Architecture

```
GitHubCopilotBlender/
├── __init__.py              # Entry point, bl_info, register/unregister
├── blender_manifest.toml    # Blender 4.2+ extension manifest
├── properties.py            # Scene properties (chat, auth, models, uploads)
├── preferences.py           # Addon preferences (settings, cached auth)
├── auth.py                  # OAuth device flow + token management
├── api_client.py            # Chat completions, model catalog, async wrapper
├── tool_definitions.py      # Tool JSON schemas for API
├── tool_executor.py         # Tool execution (file I/O + Blender ops)
├── operators.py             # All Blender operators
├── panels.py                # N-panel UI (sidebar)
└── README.md                # This file
```

### Key Design Decisions
- **No external dependencies** — Uses Python stdlib `urllib.request` instead of `requests`
- **Thread-safe Blender ops** — Blender-touching tools execute on main thread via queue + modal timer
- **Token persistence** — OAuth tokens cached to `~/.cache/github-copilot-blender/`
- **API-truth model labels** — Response model ID is always from the API, never guessed
- **Auto-retry** — One-time retry on transport failures

## Troubleshooting

### Addon doesn't appear
- Check Blender Console (Window → Toggle System Console on Windows) for errors
- Ensure the folder is named `GitHubCopilotBlender` (not nested)
- Restart Blender after installing

### Sign-in fails
- Check internet connection
- Ensure you have an active GitHub Copilot subscription
- Try clearing the token cache: delete `~/.cache/github-copilot-blender/`

### Tool calls fail
- Check Blender's console for Python tracebacks
- Enable "Verbose Logging" in addon preferences
- Increase "Request Timeout" for complex operations

### Model returns "FAILURE"
- Some models may be temporarily unavailable
- Try a different model from the picker
- Check if your Copilot plan supports the selected model tier

## License

MIT
