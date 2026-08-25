"""Agent-owned desired-overlay codec."""

from agentworks.agents.template import AgentTemplate
from agentworks.db.instance_state import JsonObject
from agentworks.instance_overlay_codec import decode_overlay_model, encode_overlay_model


def decode_overlay(raw: JsonObject) -> AgentTemplate:
    return decode_overlay_model(AgentTemplate, "agent", raw)


def encode_overlay(declaration: AgentTemplate) -> JsonObject:
    return encode_overlay_model(declaration)
