"""Build the fixed no-install bundle for Linux metadata operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_file_metadata"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_file_objects",
    "_file_lock",
    "_file_metadata",
    "_file_wire",
    "_file_metadata_protocol",
    "_file_metadata_guest",
)

FIXED_LOADER = build_helper_modules(_PACKAGE, _MODULE_NAMES)
FIXED_SOURCE = FIXED_LOADER + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._file_metadata_guest')!r}].main(sys.argv[1]))\n"
)
