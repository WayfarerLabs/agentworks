"""Build the fixed no-install bundle for Linux file-object operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import RootGuestDelivery, build_file_helper_bundle, build_root_guest_program

_PACKAGE = "_agw_file_object"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_revision_wire",
    "_file_paths",
    "_file_snapshot",
    "_file_snapshot_read",
    "_file_objects",
    "_file_wire",
    "_file_object_protocol",
    "_file_object_guest",
)

FIXED_BUNDLE = build_file_helper_bundle(_PACKAGE, _MODULE_NAMES, "_file_object_guest")
ROOT_PROGRAM = build_root_guest_program(
    _PACKAGE, _MODULE_NAMES, "_file_object_guest", delivery=RootGuestDelivery.FIXED_PREFIX
)
