"""Session-owned desired-overlay codec."""

from agentworks.db.instance_state import JsonObject
from agentworks.instance_overlay_codec import decode_overlay_model, encode_overlay_model
from agentworks.sessions.template import SessionTemplate


def decode_overlay(raw: JsonObject) -> SessionTemplate:
    return decode_overlay_model(SessionTemplate, "session", raw)


def encode_overlay(declaration: SessionTemplate) -> JsonObject:
    return encode_overlay_model(declaration)
