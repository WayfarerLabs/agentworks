"""Build the fixed no-install bundle for private stage operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_file_helper_bundle

_PACKAGE = "_agw_file_stage"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_paths",
    "_scratch_receipt",
    "_scratch",
    "_file_wire",
    "_scratch_wire",
    "_file_stage_protocol",
    "_file_stage_guest",
)

FIXED_BUNDLE = build_file_helper_bundle(_PACKAGE, _MODULE_NAMES, "_file_stage_guest")
