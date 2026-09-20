"""Build the fixed no-install source bundle for the private inline helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_PACKAGE = "_agw_inline"
_MODULE_NAMES = ("_process", "_evidence_wire", "_inline_request", "_inline_control", "_inline_guest")


FIXED_SOURCE = build_helper_modules(_PACKAGE, _MODULE_NAMES) + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._inline_guest')!r}].main(sys.argv[1]))\n"
)
