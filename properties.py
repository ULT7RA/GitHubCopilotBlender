"""
Scene-level and window-level properties for the Copilot addon.
Stores chat history, auth state, and UI state as Blender properties.
"""

import bpy
from bpy.props import (
    StringProperty, BoolProperty, IntProperty, FloatProperty,
    EnumProperty, CollectionProperty
)
from bpy.types import PropertyGroup


class CopilotChatMessage(PropertyGroup):
    """Single chat message in the transcript."""
    role: StringProperty(name="Role", default="user")
    content: StringProperty(name="Content", default="")
    model_id: StringProperty(name="Model ID", default="")
    timestamp: FloatProperty(name="Timestamp", default=0.0)


class CopilotModelItem(PropertyGroup):
    """Available model entry from /models endpoint."""
    model_id: StringProperty(name="ID", default="")
    display_name: StringProperty(name="Display Name", default="")
    vendor: StringProperty(name="Vendor", default="")
    category: StringProperty(name="Category", default="")
    supports_tools: BoolProperty(name="Supports Tools", default=False)
    supports_vision: BoolProperty(name="Supports Vision", default=False)
    context_tokens: IntProperty(name="Context Window", default=0)
    output_tokens: IntProperty(name="Max Output", default=0)
    is_default: BoolProperty(name="Is Default", default=False)
    endpoint: StringProperty(name="Endpoint", default="/chat/completions")
    multiplier: FloatProperty(name="Billing Multiplier", default=0.0)


class CopilotUploadItem(PropertyGroup):
    """Pending file upload."""
    filepath: StringProperty(name="File Path", default="", subtype='FILE_PATH')
    filename: StringProperty(name="Filename", default="")


class CopilotSceneProperties(PropertyGroup):
    """Main addon state attached to bpy.types.Scene."""

    # Auth
    is_authenticated: BoolProperty(name="Authenticated", default=False)
    username: StringProperty(name="Username", default="")
    auth_status: StringProperty(name="Auth Status", default="Not signed in")
    device_code_display: StringProperty(name="Device Code", default="")
    sku: StringProperty(name="SKU", default="")

    # Connection
    api_base: StringProperty(name="API Base", default="https://api.githubcopilot.com")
    copilot_token: StringProperty(name="Copilot Token", default="", subtype='PASSWORD')
    oauth_token: StringProperty(name="OAuth Token", default="", subtype='PASSWORD')
    token_expires_at: FloatProperty(name="Token Expires At", default=0.0)

    # Model
    available_models: CollectionProperty(type=CopilotModelItem, name="Models")
    active_model_index: IntProperty(name="Active Model", default=0)
    active_model_id: StringProperty(name="Active Model ID", default="")

    # Chat
    chat_history: CollectionProperty(type=CopilotChatMessage, name="Chat History")
    prompt_text: StringProperty(name="Prompt", default="", options={'TEXTEDIT_UPDATE'})
    target_path: StringProperty(name="Target Path", default="", subtype='FILE_PATH')

    # Uploads
    pending_uploads: CollectionProperty(type=CopilotUploadItem, name="Uploads")

    # Runtime state
    is_thinking: BoolProperty(name="Is Thinking", default=False)
    thinking_text: StringProperty(name="Thinking Text", default="Copilot is thinking...")
    last_error: StringProperty(name="Last Error", default="")
    tool_log: StringProperty(name="Tool Log", default="")
    request_count: IntProperty(name="Request Count", default=0)
    last_render_path: StringProperty(name="Last Render", default="", subtype='FILE_PATH')

    # Conversation context (JSON string for message history sent to API)
    conversation_json: StringProperty(name="Conversation JSON", default="[]")


_classes = [
    CopilotChatMessage,
    CopilotModelItem,
    CopilotUploadItem,
    CopilotSceneProperties,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.copilot = bpy.props.PointerProperty(type=CopilotSceneProperties)


def unregister():
    if hasattr(bpy.types.Scene, "copilot"):
        del bpy.types.Scene.copilot
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
