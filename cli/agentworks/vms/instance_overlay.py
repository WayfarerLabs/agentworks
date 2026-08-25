"""VM-owned desired-overlay codec."""

from agentworks.db.instance_state import JsonObject
from agentworks.instance_overlay_codec import decode_overlay_model, encode_overlay_model
from agentworks.vms.template import VMTemplate


def decode_overlay(raw: JsonObject) -> VMTemplate:
    return decode_overlay_model(VMTemplate, "vm", raw)


def encode_overlay(declaration: VMTemplate) -> JsonObject:
    return encode_overlay_model(declaration)
