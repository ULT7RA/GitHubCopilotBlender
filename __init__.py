"""
GitHub Copilot for Blender — Full agentic AI assistant with tool-calling.
OAuth device-flow auth, multi-model support, dockable N-panel with chat,
file/mesh/material/modifier/script tools, upload support, and shared auth state.
"""

bl_info = {
    "name": "GitHub Copilot for Blender",
    "description": "AI-powered assistant with agentic tool-calling for Blender",
    "author": "GitHub Copilot Integration",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "category": "Development",
    "location": "View3D > Sidebar > Copilot",
    "doc_url": "",
    "tracker_url": "",
}

import bpy

from . import properties
from . import preferences
from . import operators
from . import panels

_modules = [properties, preferences, operators, panels]


def register():
    for mod in _modules:
        mod.register()
    print("[GitHubCopilot] Addon registered")


def unregister():
    for mod in reversed(_modules):
        mod.unregister()
    print("[GitHubCopilot] Addon unregistered")


if __name__ == "__main__":
    register()
