"""Fixed source bundle for the private account-database helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

FIXED_SOURCE = build_helper_modules(
    "agentworks.execution",
    ("_helper_identity", "_account_protocol", "_account_guest"),
)
FIXED_SOURCE += (
    "from agentworks.execution._account_guest import main\n"
    "raise SystemExit(main(sys.argv[1]) if len(sys.argv)==2 else 2)\n"
)
