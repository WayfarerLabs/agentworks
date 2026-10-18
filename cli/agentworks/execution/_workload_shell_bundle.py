"""Exact packaged source for the fixed workload shell helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import RootGuestDelivery, build_helper_modules, build_root_guest_program

_PACKAGE = "agentworks.execution"
_MODULE_NAMES = ("_helper_identity", "_workload_shell_protocol", "_workload_shell_guest")

FIXED_SOURCE = build_helper_modules(_PACKAGE, _MODULE_NAMES)
FIXED_SOURCE += (
    "from agentworks.execution._workload_shell_guest import main\n"
    "raise SystemExit(main(sys.argv[1]) if len(sys.argv)==2 else 2)\n"
)
ROOT_PROGRAM = build_root_guest_program(
    _PACKAGE, _MODULE_NAMES, "_workload_shell_guest", delivery=RootGuestDelivery.INLINE
)
