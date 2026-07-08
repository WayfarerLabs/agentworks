"""Tests for ``ResolvedVMTemplate.referenced_resources()`` (Phase 1c).

The resolved template emits the env-block requirements (with inheritance
applied via the merged ``env`` dict) plus the Tailscale auth-key
requirement.
"""

from __future__ import annotations

from agentworks.env.entry import EnvEntry
from agentworks.resources.reference import SecretReference
from agentworks.vms.templates import ResolvedVMTemplate


def test_resolved_vm_template_emits_tailscale_requirement_by_default() -> None:
    tmpl = ResolvedVMTemplate(name="azure-prod")
    reqs = tmpl.referenced_resources()
    # Only the Tailscale requirement (no env block).
    assert len(reqs) == 1
    ts = reqs[0]
    assert isinstance(ts, SecretReference)
    assert ts.name == "tailscale-auth-key"
    assert ts.kind == "secret"
    assert ts.usage == "the Tailscale auth key"
    assert ts.source == ("vm-template", "azure-prod")


def test_resolved_vm_template_emits_custom_tailscale_secret_name() -> None:
    tmpl = ResolvedVMTemplate(
        name="azure-prod",
        tailscale_auth_key="custom-ts-key",
    )
    reqs = tmpl.referenced_resources()
    ts_reqs = [r for r in reqs if r.usage == "the Tailscale auth key"]
    assert len(ts_reqs) == 1
    assert ts_reqs[0].name == "custom-ts-key"


def test_resolved_vm_template_emits_env_requirements_alongside_tailscale() -> None:
    tmpl = ResolvedVMTemplate(
        name="azure-prod",
        env={
            "PLAIN": EnvEntry(key="PLAIN", value="x"),
            "API_KEY": EnvEntry(key="API_KEY", secret="api-secret"),
        },
    )
    reqs = tmpl.referenced_resources()
    # 1 env-block secret + 1 Tailscale
    assert len(reqs) == 2
    names = sorted(r.name for r in reqs)
    assert names == ["api-secret", "tailscale-auth-key"]
    # All requirements use the resolved template's source.
    assert all(r.source == ("vm-template", "azure-prod") for r in reqs)


def test_resolved_vm_template_inheritance_threads_tailscale_auth_key() -> None:
    """``_merge`` and ``_merge_template`` thread the ``tailscale_auth_key``
    field through inheritance. Verifying via the parent-overrides-via-merge
    path (a direct test of resolution, not the loader).
    """
    from agentworks.vms.templates import _merge

    parent = ResolvedVMTemplate(name="parent", tailscale_auth_key="parent-ts")
    child = ResolvedVMTemplate(name="child")
    _merge(child, parent)
    # After merge, child took parent's auth-key name.
    assert child.tailscale_auth_key == "parent-ts"
