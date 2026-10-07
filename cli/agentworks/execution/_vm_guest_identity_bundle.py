"""Fixed source bundle for the private VM guest identity helper."""

from __future__ import annotations

from agentworks.execution._helper_bundle import build_helper_modules

_LOADER_SOURCE = build_helper_modules(
    "agentworks.execution",
    ("_vm_guest_identity_protocol", "_vm_guest_identity_guest"),
)
FIXED_SOURCE = _LOADER_SOURCE + (
    "from agentworks.execution._vm_guest_identity_guest import main\n"
    "raise SystemExit(main(sys.argv[1]) if len(sys.argv)==2 else 2)\n"
)

# Named admission has already dropped credentials before this body is loaded.
# Keep the loader's namespace separate so its module names cannot replace main.
_NAMED_BODY_SOURCE = (
    "def main(nonce):\n"
    " scope={'__name__':'__main__'}\n"
    f" exec(compile({_LOADER_SOURCE!r},'<agentworks-guest-identity-loader>','exec'),scope)\n"
    " guest=scope['sys'].modules['agentworks.execution._vm_guest_identity_guest']\n"
    " guest._bind_init_reader(_agw_read_init)\n"
    " return guest.main(nonce)\n"
)
