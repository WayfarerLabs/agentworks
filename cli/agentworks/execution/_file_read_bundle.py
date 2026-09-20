"""Build the fixed no-install source bundle for the private file-read helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_file_read"
_MODULE_NAMES = ("_file_stat", "_file_paths", "_file_snapshot", "_file_read_protocol", "_file_read_guest")


FIXED_SOURCE = build_helper_modules(_PACKAGE, _MODULE_NAMES) + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._file_read_guest')!r}].main(sys.argv[1]))\n"
)
