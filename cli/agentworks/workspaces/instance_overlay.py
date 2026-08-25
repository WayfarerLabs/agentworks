"""Workspace-owned desired-overlay codec."""

from agentworks.db.instance_state import JsonObject
from agentworks.instance_overlay_codec import decode_overlay_model, encode_overlay_model
from agentworks.workspaces.template import WorkspaceTemplate


def decode_overlay(raw: JsonObject) -> WorkspaceTemplate:
    return decode_overlay_model(WorkspaceTemplate, "workspace", raw)


def encode_overlay(declaration: WorkspaceTemplate) -> JsonObject:
    return encode_overlay_model(declaration)
