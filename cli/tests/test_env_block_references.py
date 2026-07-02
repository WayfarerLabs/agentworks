"""Tests for Phase 1b: env-block secret refs emit ``SecretReference``
via ``required_resources()``, and missing references auto-declare
through the Resource Registry's miss policy.

Replaces the strict-error behavior the env-and-secrets SDD shipped in
Phase 1 of that effort -- now a typo'd ``{ secret = "anthropic-api-ky"     }``
no longer errors at config load; the framework auto-declares
``anthropic-api-ky``, and operators see the unexpected name in
``agw secret list`` (Phase 1e) / ``agw doctor``. Runtime resolution
surfaces "no active backend resolved" if no backend yields a value.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import (
    AdminConfig,
    AgentTemplate,
    SessionTemplate,
    VMTemplate,
    WorkspaceTemplate,
    load_config,
)
from agentworks.env.entry import EnvEntry


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(body)
    )
    return p


# -- EnvEntry.referenced_resources --------------------------------------------


def test_env_entry_plaintext_returns_empty_list() -> None:
    entry = EnvEntry(key="FOO", value="bar")
    assert entry.referenced_resources(("admin-template", "default")) == []


def test_env_entry_secret_ref_emits_secret_requirement() -> None:
    entry = EnvEntry(key="API_KEY", secret="anthropic-api-key")
    reqs = entry.referenced_resources(("admin-template", "default"))
    assert len(reqs) == 1
    req = reqs[0]
    assert req.name == "anthropic-api-key"
    assert req.kind == "secret"
    assert req.usage == "the API_KEY env var"
    assert req.source == ("admin-template", "default")


# -- Resource-type required_resources() aggregation -------------------------


def test_admin_config_required_resources_aggregates_env() -> None:
    admin = AdminConfig(
        env={
            "A": EnvEntry(key="A", secret="sec-a"),
            "B": EnvEntry(key="B", value="plain"),
            "C": EnvEntry(key="C", secret="sec-c"),
        }
    )
    reqs = admin.referenced_resources()
    assert {r.name for r in reqs} == {"sec-a", "sec-c"}
    assert all(r.source == ("admin-template", "default") for r in reqs)


def test_vm_template_required_resources_uses_template_name_in_source() -> None:
    """VMTemplate emits an env-block requirement plus the framework's
    Phase-1c-added Tailscale auth-key requirement (default name).
    """
    tmpl = VMTemplate(
        name="azure-prod",
        env={"KEY": EnvEntry(key="KEY", secret="ts-key")},
    )
    reqs = tmpl.referenced_resources()
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


def test_workspace_template_required_resources() -> None:
    tmpl = WorkspaceTemplate(
        name="default",
        env={"K": EnvEntry(key="K", secret="ws-secret")},
    )
    reqs = tmpl.referenced_resources()
    assert reqs[0].source == ("workspace-template", "default")


def test_agent_template_required_resources() -> None:
    tmpl = AgentTemplate(
        name="claude",
        env={"K": EnvEntry(key="K", secret="claude-key")},
    )
    reqs = tmpl.referenced_resources()
    assert reqs[0].source == ("agent-template", "claude")


def test_session_template_required_resources_with_none_env() -> None:
    """``SessionTemplate.env`` is ``Optional`` (uniquely so among the
    template kinds). ``required_resources()`` handles ``env=None``
    without erroring.
    """
    tmpl = SessionTemplate(name="t", env=None)
    assert tmpl.referenced_resources() == []


def test_session_template_required_resources_with_secrets() -> None:
    tmpl = SessionTemplate(
        name="claude-coder",
        env={"K": EnvEntry(key="K", secret="cc-secret")},
    )
    reqs = tmpl.referenced_resources()
    assert reqs[0].source == ("session-template", "claude-coder")


# -- End-to-end: undeclared secret auto-declares through the framework -------


def test_undeclared_env_secret_auto_declares_through_build_registry(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """The defining behavior of Phase 1b: a typo'd or otherwise
    undeclared env-block secret no longer errors at config load; the
    Registry auto-declares it and tags it with the source.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        API_KEY = { secret = "anthropic-api-ky" }
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    auto = registry.lookup("secret", "anthropic-api-ky")
    assert auto.origin is not None
    assert auto.origin.variant == "auto-declared"
    assert auto.origin.source == ("admin-template", "default")
    # Usage carries the env-var key so operators see what referenced it.
    assert len(auto.references) == 1
    assert auto.references[0].usage == "the API_KEY env var"


def test_operator_declared_secret_referenced_from_env_gets_usage_populated(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """A secret operator-typed in ``[secrets.X]`` AND referenced from an
    env block ends up with usage attached after finalize. Origin stays
    operator-declared (publish-time stamp wins); usage records the
    requirement source.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.shared-key]
        description = "Used by both admin and a template"

        [admin.env]
        ADMIN_KEY = { secret = "shared-key" }

        [vm_templates.azure-prod]
        cpus = 2

        [vm_templates.azure-prod.env]
        TEMPLATE_KEY = { secret = "shared-key" }
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    decl = registry.lookup("secret", "shared-key")
    assert decl.origin is not None
    assert decl.origin.variant == "operator-declared"
    # Two incoming requirements; both contribute UsageEntries.
    assert len(decl.references) == 2
    sources = sorted(u.source for u in decl.references)
    assert sources == [
        ("admin-template", "default"),
        ("vm-template", "azure-prod"),
    ]


def test_multiple_env_refs_from_one_resource_each_contribute_usage(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        KEY_A = { secret = "shared" }
        KEY_B = { secret = "shared" }
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    auto = registry.lookup("secret", "shared")
    # Both env vars contribute one ReferenceEntry each.
    assert len(auto.references) == 2
    texts = sorted(u.usage for u in auto.references)
    assert texts == ["the KEY_A env var", "the KEY_B env var"]
