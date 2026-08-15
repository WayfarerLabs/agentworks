"""Tests for Phase 1b: env-block secret refs emit ``SecretReference``
via ``dependencies()``, and missing references auto-declare
through the Resource Registry's miss policy.

Replaces the strict-error behavior the env-and-secrets SDD shipped in
Phase 1 of that effort -- now a typo'd ``{ secret = "anthropic-api-ky"     }``
no longer errors at config load; the framework auto-declares
``anthropic-api-ky``, and operators see the unexpected name in
``agw secret list`` (Phase 1e) / ``agw doctor``. Runtime resolution
surfaces an unavailable secret if no active source yields a value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.env.entry import EnvEntry
from agentworks.resources.graph import FinalizeContext
from agentworks.sessions.template import SessionTemplate
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _write_cfg(
    tmp_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """``write_cfg`` under this file's keyword spelling."""
    return write_cfg(tmp_path, *manifests, settings=settings, filename="c.toml")


# -- EnvEntry.referenced_resources --------------------------------------------


def test_env_entry_plaintext_returns_empty_list() -> None:
    entry = EnvEntry({"value": "bar"})
    assert entry.referenced_resources("API_KEY", ("admin-template", "default")) == []


def test_env_entry_secret_ref_emits_secret_requirement() -> None:
    entry = EnvEntry({"secret": "anthropic-api-key"})
    reqs = entry.referenced_resources("API_KEY", ("admin-template", "default"))
    assert len(reqs) == 1
    req = reqs[0]
    assert req.name == "anthropic-api-key"
    assert req.kind == "secret"
    assert req.usage == "the API_KEY env var"
    assert req.source == ("admin-template", "default")


# -- Resource-type dependencies() aggregation -------------------------


def test_admin_config_dependencies_aggregates_env() -> None:
    admin = AdminConfig(
        env={
            "A": EnvEntry({"secret": "sec-a"}),
            "B": EnvEntry({"value": "plain"}),
            "C": EnvEntry({"secret": "sec-c"}),
        }
    )
    reqs = admin.dependencies(FinalizeContext())
    assert {r.name for r in reqs} == {"sec-a", "sec-c"}
    assert all(r.source == ("admin-template", "default") for r in reqs)


def test_vm_template_dependencies_uses_template_name_in_source() -> None:
    """VMTemplate emits an env-block requirement plus the framework's
    Phase-1c-added Tailscale auth-key requirement (default name).
    """
    tmpl = VMTemplate(
        name="azure-prod",
        env={"KEY": EnvEntry({"secret": "ts-key"})},
    )
    reqs = tmpl.dependencies(FinalizeContext())
    # 1 env-block + 1 tailscale (Phase 1c)
    assert len(reqs) == 2
    # All requirements carry the template's source.
    assert all(r.source == ("vm-template", "azure-prod") for r in reqs)
    # The env-block requirement is for the secret `ts-key`.
    env_reqs = [r for r in reqs if r.name == "ts-key"]
    assert len(env_reqs) == 1
    # The Tailscale requirement uses the default secret name when the
    # template doesn't override `tailscale_auth_key`.
    ts_reqs = [r for r in reqs if r.name == "tailscale-auth-key"]
    assert len(ts_reqs) == 1
    assert ts_reqs[0].usage == "the Tailscale auth key"


def test_workspace_template_dependencies() -> None:
    tmpl = WorkspaceTemplate(
        name="default",
        env={"K": EnvEntry({"secret": "ws-secret"})},
    )
    reqs = tmpl.dependencies(FinalizeContext())
    assert reqs[0].source == ("workspace-template", "default")


def test_agent_template_dependencies() -> None:
    tmpl = AgentTemplate(
        name="claude",
        env={"K": EnvEntry({"secret": "claude-key"})},
    )
    reqs = tmpl.dependencies(FinalizeContext())
    assert reqs[0].source == ("agent-template", "claude")


def test_session_template_dependencies_with_omitted_env() -> None:
    """``SessionTemplate.env`` defaults to an empty table, like the other
    three template kinds, so an omitted env is a declaration with no
    entries rather than a ``None`` every reader has to fold. Omitting it
    yields no edges; writing ``env: null`` is now a type error."""
    tmpl = SessionTemplate(name="t")
    assert tmpl.env == {}
    assert tmpl.dependencies(FinalizeContext()) == []
    with pytest.raises(ValidationError):
        SessionTemplate(name="t", env=None)  # type: ignore[arg-type]  # the point of the test


def test_session_template_dependencies_with_secrets() -> None:
    tmpl = SessionTemplate(
        name="claude-coder",
        env={"K": EnvEntry({"secret": "cc-secret"})},
    )
    reqs = tmpl.dependencies(FinalizeContext())
    assert reqs[0].source == ("session-template", "claude-coder")


# -- End-to-end: undeclared secret auto-declares through the framework -------


def test_undeclared_env_secret_auto_declares_through_build_registry(tmp_path: Path) -> None:
    """The defining behavior of Phase 1b: a typo'd or otherwise
    undeclared env-block secret no longer errors at config load; the
    Registry auto-declares it and tags it with the source.
    """
    cfg = _write_cfg(
        tmp_path,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"API_KEY": {"secret": "anthropic-api-ky"}}})],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    auto = registry.lookup("secret", "anthropic-api-ky")
    assert auto.origin is not None
    assert auto.origin.variant == "auto-declared"
    assert auto.origin.source == ("admin-template", "default")
    # Usage carries the env-var key so operators see what referenced it.
    dependents = registry.graph.dependents_of("secret", "anthropic-api-ky")
    assert len(dependents) == 1
    assert dependents[0].usage == "the API_KEY env var"


def test_operator_declared_secret_referenced_from_env_gets_usage_populated(tmp_path: Path) -> None:
    """A secret operator-typed in ``[secrets.X]`` AND referenced from an
    env block ends up with usage attached after finalize. Origin stays
    operator-declared (publish-time stamp wins); usage records the
    requirement source.
    """
    cfg = _write_cfg(
        tmp_path,
        manifests=[
            ManifestDoc("secret", "shared-key", description="Used by both admin and a template"),
            ManifestDoc("admin-template", "default", {"env": {"ADMIN_KEY": {"secret": "shared-key"}}}),
            ManifestDoc("vm-template", "azure-prod", {"cpus": 2, "env": {"TEMPLATE_KEY": {"secret": "shared-key"}}}),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    decl = registry.lookup("secret", "shared-key")
    assert decl.origin is not None
    assert decl.origin.variant == "operator-declared"
    # Two incoming requirements; both contribute ReferenceEntries.
    dependents = registry.graph.dependents_of("secret", "shared-key")
    assert len(dependents) == 2
    sources = sorted(u.source for u in dependents)
    assert sources == [
        ("admin-template", "default"),
        ("vm-template", "azure-prod"),
    ]


def test_multiple_env_refs_from_one_resource_each_contribute_usage(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        manifests=[
            ManifestDoc(
                "admin-template",
                "default",
                {"env": {"KEY_A": {"secret": "shared"}, "KEY_B": {"secret": "shared"}}},
            )
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    registry.lookup("secret", "shared")
    # Both env vars contribute one ReferenceEntry each.
    dependents = registry.graph.dependents_of("secret", "shared")
    assert len(dependents) == 2
    texts = sorted(u.usage for u in dependents)
    assert texts == ["the KEY_A env var", "the KEY_B env var"]
