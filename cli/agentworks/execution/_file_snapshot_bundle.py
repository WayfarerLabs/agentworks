"""Build the fixed no-install bundle for private snapshot operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_file_helper_bundle

_PACKAGE = "_agw_file_snapshot"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_file_revision_wire",
    "_scratch_receipt",
    "_scratch",
    "_file_spool",
    "_scratch_root",
    "_file_wire",
    "_scratch_wire",
    "_file_snapshot_protocol",
    "_file_snapshot_guest",
)

FIXED_BUNDLE = build_file_helper_bundle(_PACKAGE, _MODULE_NAMES, "_file_snapshot_guest")
