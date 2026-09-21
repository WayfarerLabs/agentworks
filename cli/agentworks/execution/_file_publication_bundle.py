"""Build the fixed no-install bundle for private publication operations."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_file_publication"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_scratch_receipt",
    "_scratch",
    "_publication_receipt",
    "_file_publication",
    "_file_wire",
    "_scratch_wire",
    "_file_revision_wire",
    "_file_publication_wire",
    "_file_publication_protocol",
    "_file_publication_guest",
)

FIXED_LOADER = build_helper_modules(_PACKAGE, _MODULE_NAMES)
FIXED_SOURCE = FIXED_LOADER + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._file_publication_guest')!r}].main(sys.argv[1]))\n"
)
