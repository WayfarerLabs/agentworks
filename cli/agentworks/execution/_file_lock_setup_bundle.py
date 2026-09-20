"""Build the fixed no-install source bundle for Debian lock setup."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_file_lock_setup"

FIXED_SOURCE = build_helper_modules(_PACKAGE, ("_file_lock_setup",)) + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._file_lock_setup')!r}].main())\n"
)
