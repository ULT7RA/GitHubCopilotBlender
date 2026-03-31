"""
Addon preferences — persistent settings stored in Blender user prefs.
"""

import bpy
from bpy.types import AddonPreferences
from bpy.props import (
    StringProperty, IntProperty, BoolProperty, FloatProperty,
    EnumProperty
)


def _get_addon_id():
    """Return the addon package name for preferences lookup."""
    return __package__


class GitHubCopilotPreferences(AddonPreferences):
    bl_idname = __package__

    # --- Connection ---
    timeout_seconds: IntProperty(
        name="Request Timeout (seconds)",
        description="HTTP timeout for API requests. Increase for complex tool-chain prompts",
        default=600,
        min=30,
        max=3600,
    )

    max_output_tokens: IntProperty(
        name="Max Output Tokens",
        description="Maximum tokens in API response. Increase for longer model responses",
        default=16384,
        min=1024,
        max=128000,
    )

    # --- Execution ---
    max_tool_iterations: IntProperty(
        name="Max Tool-Call Iterations (0 = Unlimited)",
        description="Safety cap for agentic tool-calling loops. 0 means unlimited",
        default=0,
        min=0,
        max=5000,
    )

    # --- Safety ---
    require_patch_preview: BoolProperty(
        name="Require Patch Preview",
        description="Show diff preview before applying file changes",
        default=True,
    )

    allowed_write_roots: StringProperty(
        name="Allowed Write Roots",
        description="Comma-separated directory roots where file writes are permitted (empty = any)",
        default="",
    )

    # --- Logging ---
    enable_verbose_logging: BoolProperty(
        name="Verbose Logging",
        description="Print detailed HTTP/tool logs to Blender console",
        default=False,
    )

    # --- Display ---
    user_handle: StringProperty(
        name="Display Name",
        description="Your name shown in the chat transcript",
        default="",
    )

    # --- Cached auth (persisted across sessions) ---
    cached_oauth_token: StringProperty(
        name="Cached OAuth Token",
        default="",
        subtype='PASSWORD',
        options={'HIDDEN'},
    )
    cached_username: StringProperty(
        name="Cached Username",
        default="",
        options={'HIDDEN'},
    )
    cached_active_model: StringProperty(
        name="Cached Active Model",
        default="",
        options={'HIDDEN'},
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Connection", icon='LINKED')
        box.prop(self, "timeout_seconds")
        box.prop(self, "max_output_tokens")

        box = layout.box()
        box.label(text="Execution", icon='PLAY')
        box.prop(self, "max_tool_iterations")
        box.prop(self, "require_patch_preview")
        box.prop(self, "allowed_write_roots")

        box = layout.box()
        box.label(text="Display", icon='USER')
        box.prop(self, "user_handle")

        box = layout.box()
        box.label(text="Logging", icon='TEXT')
        box.prop(self, "enable_verbose_logging")

        # Auth status
        box = layout.box()
        box.label(text="Auth Cache", icon='LOCKED')
        if self.cached_oauth_token:
            box.label(text=f"Signed in as: {self.cached_username or '(unknown)'}")
            box.label(text=f"Active model: {self.cached_active_model or '(none)'}")
        else:
            box.label(text="Not signed in")


def get_prefs(context=None):
    """Convenience: return the addon preferences object."""
    if context is None:
        context = bpy.context
    return context.preferences.addons[_get_addon_id()].preferences


_classes = [GitHubCopilotPreferences]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
