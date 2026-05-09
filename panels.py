"""
Blender UI for GitHub Copilot.
  - Compact N-panel sidebar (auth + launch button)
  - Pop-out chat: large dialog with chat display + input bar + controls
"""

import textwrap
import time

import bpy
from bpy.types import Panel, Operator
from bpy.props import StringProperty


# ── Main Sidebar Panel (compact — auth + launch only) ────────────────────

class COPILOT_PT_MainPanel(Panel):
    """GitHub Copilot — compact sidebar launcher."""
    bl_label = "GitHub Copilot"
    bl_idname = "COPILOT_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Copilot"

    def draw(self, context):
        layout = self.layout
        cp = context.scene.copilot

        box = layout.box()
        if cp.is_authenticated:
            row = box.row(align=True)
            row.label(text=cp.username, icon='CHECKMARK')
            row.label(text=f"({cp.sku})" if cp.sku else "")
            row.operator("copilot.sign_out", text="", icon='X')

            layout.separator(factor=0.5)
            col = layout.column()
            col.scale_y = 2.0
            col.operator("copilot.popout_chat", text="Copilot Blender Kit", icon='WINDOW')

            if cp.is_thinking:
                layout.separator(factor=0.3)
                row = layout.row()
                row.alert = True
                row.label(text=cp.thinking_text, icon='SORTTIME')
        else:
            row = box.row(align=True)
            row.label(text=cp.auth_status or "Not signed in", icon='ERROR')
            col = box.column()
            col.scale_y = 1.5
            col.operator("copilot.sign_in", text="Sign In to GitHub", icon='LINKED')
            if cp.device_code_display:
                box.label(text=f"Code: {cp.device_code_display}", icon='COPYDOWN')


# ── Pop-Out Chat Dialog ──────────────────────────────────────────────────

class COPILOT_OT_PopoutChat(Operator):
    """Open the full Copilot chat window."""
    bl_idname = "copilot.popout_chat"
    bl_label = "Copilot Blender Kit"
    bl_options = {'REGISTER'}

    chat_input: StringProperty(
        name="",
        default="",
        description="Type your message here",
    )

    def execute(self, context):
        prompt = self.chat_input.strip()
        if prompt:
            cp = context.scene.copilot
            cp.prompt_text = prompt
            self.chat_input = ""
            bpy.ops.copilot.send_chat()
        # Re-open the dialog so it stays visible after sending
        bpy.ops.copilot.popout_chat('INVOKE_DEFAULT')
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=700)

    def draw(self, context):
        layout = self.layout
        cp = context.scene.copilot

        if not cp.is_authenticated:
            layout.label(text="Sign in from the sidebar first", icon='LOCKED')
            return

        # Header: model + controls
        row = layout.row(align=True)
        row.label(text=f"Model: {cp.active_model_id}", icon='LIGHT')
        row.operator("copilot.refresh_models", text="", icon='FILE_REFRESH')
        row.operator("copilot.clear_chat", text="", icon='TRASH')
        row.operator("copilot.copy_response", text="", icon='COPYDOWN')

        layout.separator(factor=0.3)

        # ── Chat transcript (fills the dialog width) ──
        box = layout.box()
        if len(cp.chat_history) == 0:
            box.label(text="No messages yet.", icon='BLANK1')
            box.label(text="Type in the Chat box below, then click OK to send.", icon='FORWARD')
        else:
            msgs = list(cp.chat_history)
            show = msgs[-40:]
            for msg in show:
                _draw_message(box, msg, width=90)

        # Thinking indicator
        if cp.is_thinking:
            row = layout.row()
            row.alert = True
            row.label(text=cp.thinking_text, icon='SORTTIME')

        # ── Render preview (if available) ──
        if cp.last_render_path:
            img = bpy.data.images.get("CopilotRender")
            if img:
                layout.separator(factor=0.3)
                preview_box = layout.box()
                preview_box.label(text="Last Render:", icon='IMAGE_DATA')
                preview_box.template_preview(img)

        layout.separator(factor=0.5)

        # ── Chat input bar ──
        input_box = layout.box()
        input_box.label(text="Chat:", icon='OUTLINER_DATA_GP_LAYER')
        row = input_box.row(align=True)
        row.scale_y = 2.2
        row.prop(self, "chat_input", text="")

        # ── Upload row ──
        row = layout.row(align=True)
        row.operator("copilot.upload_files", text="Upload", icon='FILEBROWSER')
        if len(cp.pending_uploads) > 0:
            row.operator("copilot.clear_uploads", text=f"Clear ({len(cp.pending_uploads)})", icon='X')

        # ── Action buttons ──
        row = layout.row(align=True)
        row.operator("copilot.analyze_scene", text="Analyze", icon='SCENE_DATA')
        row.operator("copilot.create_object", text="Create", icon='MESH_CUBE')
        row.operator("copilot.generate_script", text="Script", icon='TEXT')
        row.operator("copilot.explain_selected", text="Explain", icon='QUESTION')
        row.operator("copilot.suggest_material", text="Material", icon='MATERIAL')

        # Upload file list
        if len(cp.pending_uploads) > 0:
            for up in cp.pending_uploads:
                layout.label(text=f"  {up.filename}", icon='BLANK1')


# ── Message drawing helper ───────────────────────────────────────────────

def _draw_message(layout, msg, width=90):
    """Draw a single chat message."""
    role = msg.role
    content = msg.content

    if role == "system":
        row = layout.row()
        row.scale_y = 0.5
        row.label(text=content[:200], icon='INFO')
    elif role == "user":
        box = layout.box()
        box.label(text="You:", icon='USER')
        _wrap_text(box, content, width=width)
    elif role == "assistant":
        box = layout.box()
        model_tag = f" [{msg.model_id}]" if msg.model_id else ""
        box.label(text=f"Copilot{model_tag}:", icon='LIGHT')
        _wrap_text(box, content, width=width)


def _wrap_text(layout, text, width=90):
    """Word-wrap text into label rows."""
    lines = text.split("\n")
    count = 0
    max_lines = 100
    for line in lines:
        if count > max_lines:
            layout.label(text="... (truncated)")
            break
        if len(line) <= width:
            row = layout.row()
            row.scale_y = 0.6
            row.label(text=line)
            count += 1
        else:
            wrapped = textwrap.wrap(line, width=width)
            for wl in wrapped:
                if count > max_lines:
                    layout.label(text="... (truncated)")
                    break
                row = layout.row()
                row.scale_y = 0.6
                row.label(text=wl)
                count += 1


def _refresh_chat_text(context):
    """No-op kept for compatibility with operators.py auto-refresh call."""
    pass


# ── Registration ─────────────────────────────────────────────────────────

_classes = [
    COPILOT_OT_PopoutChat,
    COPILOT_PT_MainPanel,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
