"""Build the fixed no-install bundle for bounded Linux directory inventory."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_file_inventory"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_file_objects",
    "_file_inventory",
    "_file_lock",
    "_file_wire",
    "_file_inventory_protocol",
    "_file_inventory_guest",
)

FIXED_LOADER = build_helper_modules(_PACKAGE, _MODULE_NAMES)
FIXED_SOURCE = FIXED_LOADER + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._file_inventory_guest')!r}].main(sys.argv[1]))\n"
)
