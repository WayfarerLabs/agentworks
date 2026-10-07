"""Fixed no-install bundle for private guest file-gate control."""

from __future__ import annotations

from agentworks.execution._helper_bundle import RootGuestDelivery, build_file_helper_bundle, build_root_guest_program

_PACKAGE = "_agw_file_effect_gate"
_MODULE_NAMES = (
    "_helper_identity",
    "_vm_guest_identity_protocol",
    "_vm_guest_identity_guest",
    "_file_wire",
    "_file_effect_gate",
    "_file_gate_control",
    "_file_effect_gate_protocol",
    "_file_effect_gate_guest",
)

FIXED_BUNDLE = build_file_helper_bundle(_PACKAGE, _MODULE_NAMES, "_file_effect_gate_guest")
ROOT_PROGRAM = build_root_guest_program(
    _PACKAGE, _MODULE_NAMES, "_file_effect_gate_guest", delivery=RootGuestDelivery.FIXED_PREFIX
)
