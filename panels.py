"""
Blender N-panel UI for GitHub Copilot.
Provides a sidebar panel in the 3D Viewport with:
  - Auth status & sign in/out
  - Model selector dropdown
  - Chat transcript
  - Prompt input & send
  - Upload attachments
  - Action buttons (analyze, create, explain, material, script)
  - Tool execution log
  - Thinking indicator
"""

import textwrap
import time

import bpy
from bpy.types import Panel, UIList


# ── Chat Message UIList ──────────────────────────────────────────────────

class COPILOT_UL_ChatMessages(UIList):
    """Custom UIList for rendering chat messages."""
    bl_idname = "COPILOT_UL_ChatMessages"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index):
        role = item.role
        content = item.content

        if role == "system":
            row = layout.row()
            row.alert = True
            row.label(text=f"⚙ {content[:120]}", icon='INFO')
        elif role == "user":
            row = layout.row()
            row.label(text=f"You: {content[:120]}", icon='USER')
        elif role == "assistant":
            row = layout.row()
            model_tag = f" [{item.model_id}]" if item.model_id else ""
            row.label(text=f"Copilot{model_tag}: {content[:120]}", icon='LIGHT')
        else:
            layout.label(text=content[:120])


# ── Main Panel ───────────────────────────────────────────────────────────

class COPILOT_PT_MainPanel(Panel):
    """GitHub Copilot — Main panel in 3D Viewport sidebar."""
    bl_label = "GitHub Copilot"
    bl_idname = "COPILOT_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Copilot"

    def draw(self, context):
        layout = self.layout
        cp = context.scene.copilot

        # ── Auth Section ──────────────────────────────────────────────
        box = layout.box()
        row = box.row(align=True)
        if cp.is_authenticated:
            row.label(text=f"✓ {cp.username}", icon='CHECKMARK')
            row.label(text=f"({cp.sku})" if cp.sku else "")
            row.operator("copilot.sign_out", text="", icon='X')
        else:
            row.label(text=cp.auth_status, icon='ERROR')
            row.operator("copilot.sign_in", text="Sign In", icon='LINKED')

        if cp.device_code_display:
            box.label(text=f"Code: {cp.device_code_display}", icon='COPYDOWN')


class COPILOT_PT_ModelPanel(Panel):
    """Model selector sub-panel."""
    bl_label = "Model"
    bl_idname = "COPILOT_PT_ModelPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Copilot"
    bl_parent_id = "COPILOT_PT_MainPanel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        cp = context.scene.copilot

        if not cp.is_authenticated:
            layout.label(text="Sign in first", icon='LOCKED')
            return

        row = layout.row(align=True)
        row.operator("copilot.refresh_models", text="", icon='FILE_REFRESH')

        if len(cp.available_models) == 0:
            row.label(text="No models loaded")
            return

        # Model dropdown
        row.label(text=f"Active: {cp.active_model_id}")

        for i, m in enumerate(cp.available_models):
            row = layout.row(align=True)
            icon = 'RADIOBUT_ON' if m.model_id == cp.active_model_id else 'RADIOBUT_OFF'
            label = f"{m.display_name}"
            if m.vendor:
                label += f" ({m.vendor})"
            if m.multiplier > 0:
                label += f" [${m.multiplier}x]"
            caps = []
            if m.supports_tools:
                caps.append("🔧")
            if m.supports_vision:
                caps.append("👁")
            if caps:
                label += " " + "".join(caps)

            op = row.operator("copilot.select_model", text=label, icon=icon)
            op.model_id = m.model_id


class COPILOT_PT_ChatPanel(Panel):
    """Chat transcript and prompt."""
    bl_label = "Chat"
    bl_idname = "COPILOT_PT_ChatPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Copilot"
    bl_parent_id = "COPILOT_PT_MainPanel"

    def draw(self, context):
        layout = self.layout
        cp = context.scene.copilot

        # Chat transcript
        box = layout.box()
        if len(cp.chat_history) == 0:
            box.label(text="No messages yet", icon='BLANK1')
        else:
            # Show last N messages to avoid UI overload
            msgs = list(cp.chat_history)
            show = msgs[-20:]  # Last 20
            for msg in show:
                _draw_message(box, msg)

        # Thinking indicator
        if cp.is_thinking:
            row = layout.row()
            row.alert = True
            row.label(text=f"⏳ {cp.thinking_text}", icon='SORTTIME')

        # Prompt
        layout.separator()
        row = layout.row(align=True)
        row.prop(cp, "prompt_text", text="")
        row.operator("copilot.send_chat", text="", icon='PLAY')

        # Upload row
        row = layout.row(align=True)
        row.operator("copilot.upload_files", text="Upload", icon='FILEBROWSER')
        if len(cp.pending_uploads) > 0:
            row.operator("copilot.clear_uploads", text=f"Clear ({len(cp.pending_uploads)})", icon='X')
            for up in cp.pending_uploads:
                layout.label(text=f"  📎 {up.filename}", icon='BLANK1')

        # Clear / Copy
        row = layout.row(align=True)
        row.operator("copilot.clear_chat", text="Clear", icon='TRASH')
        row.operator("copilot.copy_response", text="Copy Last", icon='COPYDOWN')


class COPILOT_PT_ActionsPanel(Panel):
    """Quick action buttons."""
    bl_label = "Actions"
    bl_idname = "COPILOT_PT_ActionsPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Copilot"
    bl_parent_id = "COPILOT_PT_MainPanel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout

        col = layout.column(align=True)
        col.operator("copilot.analyze_scene", text="Analyze Scene", icon='SCENE_DATA')
        col.operator("copilot.create_object", text="Create Object", icon='MESH_CUBE')
        col.operator("copilot.generate_script", text="Generate Script", icon='TEXT')
        col.operator("copilot.explain_selected", text="Explain Selected", icon='QUESTION')
        col.operator("copilot.suggest_material", text="Suggest Material", icon='MATERIAL')


class COPILOT_PT_ToolLogPanel(Panel):
    """Tool execution log."""
    bl_label = "Tool Log"
    bl_idname = "COPILOT_PT_ToolLogPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Copilot"
    bl_parent_id = "COPILOT_PT_MainPanel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        cp = context.scene.copilot

        if cp.tool_log:
            box = layout.box()
            for line in cp.tool_log.split("\n")[-30:]:  # Last 30 entries
                if line.strip():
                    box.label(text=line[:120])
        else:
            layout.label(text="No tool calls yet")


# ── Message drawing helper ───────────────────────────────────────────────

def _draw_message(layout, msg):
    """Draw a single chat message in the panel."""
    role = msg.role
    content = msg.content

    if role == "system":
        row = layout.row()
        row.scale_y = 0.7
        row.label(text=f"⚙ {content[:200]}", icon='INFO')
    elif role == "user":
        box = layout.box()
        box.label(text="You:", icon='USER')
        _wrap_text(box, content, width=50)
    elif role == "assistant":
        box = layout.box()
        model_tag = f" [{msg.model_id}]" if msg.model_id else ""
        box.label(text=f"Copilot{model_tag}:", icon='LIGHT')
        _wrap_text(box, content, width=50)


def _wrap_text(layout, text, width=50):
    """Word-wrap text into label rows."""
    lines = text.split("\n")
    count = 0
    for line in lines:
        if count > 40:  # Truncate very long messages in UI
            layout.label(text="... (truncated)")
            break
        if len(line) <= width:
            row = layout.row()
            row.scale_y = 0.7
            row.label(text=line)
            count += 1
        else:
            wrapped = textwrap.wrap(line, width=width)
            for wl in wrapped:
                row = layout.row()
                row.scale_y = 0.7
                row.label(text=wl)
                count += 1


# ── Registration ─────────────────────────────────────────────────────────

_classes = [
    COPILOT_UL_ChatMessages,
    COPILOT_PT_MainPanel,
    COPILOT_PT_ModelPanel,
    COPILOT_PT_ChatPanel,
    COPILOT_PT_ActionsPanel,
    COPILOT_PT_ToolLogPanel,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
