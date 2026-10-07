"""Build the fixed no-install source bundle for the private inline helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import RootGuestDelivery, build_helper_modules, build_root_guest_program

_PACKAGE = "_agw_inline"
_MODULE_NAMES = (
    "_helper_identity",
    "_process",
    "_evidence_wire",
    "_inline_request",
    "_inline_control",
    "_inline_guest",
)


FIXED_SOURCE = build_helper_modules(_PACKAGE, _MODULE_NAMES) + (
    f"raise SystemExit(sys.modules[{(_PACKAGE + '._inline_guest')!r}].main(sys.argv[1]))\n"
)
ROOT_PROGRAM = build_root_guest_program(_PACKAGE, _MODULE_NAMES, "_inline_guest", delivery=RootGuestDelivery.INLINE)
