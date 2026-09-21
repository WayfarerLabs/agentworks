"""Build the fixed no-install bundle for Linux file-object operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_file_object"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_revision_wire",
    "_file_paths",
    "_file_snapshot",
    "_file_objects",
    "_file_wire",
    "_file_object_protocol",
    "_file_object_guest",
)

FIXED_LOADER = build_helper_modules(_PACKAGE, _MODULE_NAMES)
FIXED_SOURCE = FIXED_LOADER + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._file_object_guest')!r}].main(sys.argv[1]))\n"
)
