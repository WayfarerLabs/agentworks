"""VM-owned desired-overlay codecs."""

from agentworks.db.instance_state import JsonObject
from agentworks.instance_overlay_codec import decode_overlay_model, encode_overlay_model
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate


def decode_overlay(raw: JsonObject) -> VMTemplate:
    return decode_overlay_model(VMTemplate, "vm", raw)


def encode_overlay(declaration: VMTemplate) -> JsonObject:
    return encode_overlay_model(declaration, "vm")


def decode_admin_overlay(raw: JsonObject) -> AdminConfig:
    return decode_overlay_model(
        AdminConfig,
        "VM admin",
        raw,
        validation_context={"partial_declaration": True},
        entity_kind="vm",
    )


def encode_admin_overlay(declaration: AdminConfig) -> JsonObject:
    return encode_overlay_model(declaration, "VM admin")
