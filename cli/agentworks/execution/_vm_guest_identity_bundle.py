"""Fixed source bundle for the private VM guest identity helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

FIXED_SOURCE = build_helper_modules(
    "agentworks.execution",
    ("_vm_guest_identity_protocol", "_vm_guest_identity_guest"),
)
FIXED_SOURCE += (
    "from agentworks.execution._vm_guest_identity_guest import main\n"
    "raise SystemExit(main(sys.argv[1]) if len(sys.argv)==2 else 2)\n"
)
