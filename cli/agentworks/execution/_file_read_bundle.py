"""Build the fixed no-install source bundle for the private file-read helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import RootGuestDelivery, build_file_helper_bundle, build_root_guest_program

_PACKAGE = "_agw_file_read"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_file_snapshot_read",
    "_file_wire",
    "_file_read_protocol",
    "_file_read_guest",
)


FIXED_BUNDLE = build_file_helper_bundle(_PACKAGE, _MODULE_NAMES, "_file_read_guest")
ROOT_PROGRAM = build_root_guest_program(
    _PACKAGE, _MODULE_NAMES, "_file_read_guest", delivery=RootGuestDelivery.FIXED_PREFIX
)
