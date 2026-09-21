"""Build the fixed no-install bundle for Linux metadata operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_file_helper_bundle

_PACKAGE = "_agw_file_metadata"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_file_objects",
    "_file_metadata",
    "_file_wire",
    "_file_metadata_protocol",
    "_file_metadata_guest",
)

FIXED_BUNDLE = build_file_helper_bundle(_PACKAGE, _MODULE_NAMES, "_file_metadata_guest")
