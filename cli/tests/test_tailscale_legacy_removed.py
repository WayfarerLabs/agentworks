"""Source-level tripwires that Phase 1c's legacy ``AW_TAILSCALE_AUTH_KEY``
removal stayed removed.

These read the source rather than calling the code: behavior tests live in
``test_vm_create_tailscale_eager_resolve.py``. The source-level checks
protect against accidental reintroduction of the env-var fallback (or the
inline prompt) during refactors.
"""

from __future__ import annotations

import inspect
from pathlib import Path

# Modules that previously contained the legacy fallback. After Phase 1c they
# should reference the env-var name only in operator-facing strings (CLI
# help text, comment-only references describing the removal).
_MODULES_TO_SCAN = (
    "agentworks.vms.initializer",
    "agentworks.vms.manager",
    "agentworks.secrets.resolver",
)


def _read_module_source(name: str) -> str:
    import importlib

    mod = importlib.import_module(name)
    src_path = inspect.getfile(mod)
    return Path(src_path).read_text()


def test_initializer_has_no_read_env_with_legacy_for_tailscale() -> None:
    src = _read_module_source("agentworks.vms.initializer")
    # The function _resolve_tailscale_auth_key is gone; the helper it used
    # is too.
    assert "_resolve_tailscale_auth_key" not in src, (
        "Phase 1c removed _resolve_tailscale_auth_key; reintroducing it "
        "would bypass the framework's Tailscale resolution path"
    )
    # The legacy env-var name and the helper call shape are both gone.
    assert "AW_TAILSCALE_AUTH_KEY" not in src, (
        "found legacy env var name AW_TAILSCALE_AUTH_KEY in "
        "agentworks.vms.initializer; Tailscale must resolve via the "
        "framework"
    )
    assert "read_env_with_legacy" not in src, (
        "found read_env_with_legacy call in agentworks.vms.initializer; Tailscale must resolve via the framework"
    )


def test_vm_manager_module_does_not_reference_legacy_tailscale_env_var() -> None:
    """``AW_TAILSCALE_AUTH_KEY`` is gone from ``agentworks.vms.manager``
    too. The only places the name should appear are operator-facing
    strings (CLI help text in ``cli/commands/vm.py``, etc.).
    """
    src = _read_module_source("agentworks.vms.manager")
    assert "AW_TAILSCALE_AUTH_KEY" not in src, (
        "found legacy env var name AW_TAILSCALE_AUTH_KEY in "
        "agentworks.vms.manager; ``--ignore-env`` must mask the "
        "framework's AW_SECRET_<NAME> convention, not the removed "
        "legacy name"
    )


def test_vm_manager_does_not_read_legacy_env_for_tailscale_in_collect() -> None:
    """``create_vm`` resolves the Tailscale auth key through the
    operation's resolver at the preflight boundary. It must not fall
    back to the legacy env-var path -- it has to use the framework.
    """
    from agentworks.vms.manager import create_vm

    src = inspect.getsource(create_vm)
    forbidden_call = 'read_env_with_legacy("AW_TAILSCALE_AUTH_KEY"'
    assert forbidden_call not in src, (
        "found legacy env-var fallback in create_vm; the create path must resolve Tailscale via the framework"
    )
    # The framework call shape is what we DO expect: the vm-template
    # node's preflight (run by the sweep) registers + predicts the key
    # on the resolver, the one boundary pass resolves it, and the value
    # comes from the cache via scoped delivery.
    assert "vm_template_node" in src
    assert "preflight_all" in src
    assert "resolver.resolve()" in src
