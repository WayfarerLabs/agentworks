"""Separate fixed bundle for root-owned managed UID namespace preparation."""

from agentworks.execution._helper_bundle import RootGuestDelivery, build_root_guest_program

_PACKAGE = "_agw_file_gate_namespace"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_wire",
    "_file_effect_gate",
    "_file_effect_gate_protocol",
    "_file_gate_namespace_protocol",
    "_file_gate_namespace_guest",
)
ROOT_PROGRAM = build_root_guest_program(
    _PACKAGE, _MODULE_NAMES, "_file_gate_namespace_guest", delivery=RootGuestDelivery.FIXED_PREFIX
)
