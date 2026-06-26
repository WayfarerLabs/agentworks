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
        "found read_env_with_legacy call in agentworks.vms.initializer; "
        "Tailscale must resolve via the framework"
    )


def test_vm_manager_does_not_read_legacy_env_for_tailscale_in_collect() -> None:
    """``_collect_secrets`` in agentworks.vms.manager is the Phase-1c
    entry point that resolves the Tailscale auth key. It must not fall
    back to the legacy env-var path -- it has to use the framework.
    """
    from agentworks.vms.manager import _collect_secrets

    src = inspect.getsource(_collect_secrets)
    forbidden_call = 'read_env_with_legacy("AW_TAILSCALE_AUTH_KEY"'
    assert forbidden_call not in src, (
        "found legacy env-var fallback in _collect_secrets; the function "
        "must resolve Tailscale via the framework"
    )
    # The framework call shape is what we DO expect: build the
    # registry, look up / synthesize the SecretDecl, eager-resolve via
    # the orchestrator. (The full ``collect_secrets_for`` registry walk
    # waits for Phase 2a's VMTemplateKind to provide registry-side
    # auto-declare; Phase 1c uses a direct lookup-with-fallback.)
    assert "build_registry" in src
    assert "resolve_for_command" in src
