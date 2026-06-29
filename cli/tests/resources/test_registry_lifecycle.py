"""Tests for ``Registry`` lifecycle: empty -> add -> finalize -> queryable.

Also covers ``build_registry(config)`` convenience and the Phase-1-specific
assertion that ``build_registry`` only runs ``config.publish_to`` (no catalog
publisher yet; Phase 2b will extend ``build_registry`` and update this test).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import Origin, Registry
from agentworks.secrets.base import SecretDecl


@pytest.fixture()
def example_config(tmp_path: Path) -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            [secrets.api-key]
            description = "API key"
            """
        )
    )
    return p


def test_empty_registry_is_not_finalized() -> None:
    r = Registry.empty()
    assert not r.is_finalized


def test_add_then_finalize_makes_queryable(tmp_path: Path) -> None:
    r = Registry.empty()
    decl = SecretDecl(name="x", description="X")
    r.add(
        "secret",
        "x",
        decl,
        Origin.operator_declared(file=tmp_path / "c.toml", line=1),
    )
    assert not r.is_finalized
    r.finalize()
    assert r.is_finalized
    found = r.lookup("secret", "x")
    assert found.name == "x"
    assert found.origin is not None
    assert found.origin.variant == "operator-declared"
    assert found.origin.line == 1


def test_add_after_finalize_errors(tmp_path: Path) -> None:
    r = Registry.empty()
    r.finalize()
    with pytest.raises(RuntimeError, match="frozen"):
        r.add(
            "secret",
            "x",
            SecretDecl(name="x", description="X"),
            Origin.operator_declared(file=tmp_path / "c.toml", line=1),
        )


def test_finalize_twice_errors() -> None:
    r = Registry.empty()
    r.finalize()
    with pytest.raises(RuntimeError, match="already been finalized"):
        r.finalize()


def test_iter_kind_returns_published_resources(tmp_path: Path) -> None:
    """Published secrets land in iter_kind output. Phase 2a.1's
    always-materialized ``vm_template:default`` emits a
    ``SecretRequirement`` for ``tailscale-auth-key`` via its
    ``required_resources``, so the requirement-driven path adds
    ``tailscale-auth-key`` alongside the published a/b/c. The test
    filters to operator-declared rows to pin the published-name set
    without coupling to which framework-auto-declared rows exist.
    """
    r = Registry.empty()
    for n in ["a", "b", "c"]:
        r.add(
            "secret",
            n,
            SecretDecl(name=n, description=n.upper()),
            Origin.operator_declared(file=tmp_path / "c.toml", line=1),
        )
    r.finalize()
    operator_names = sorted(
        s.name for s in r.iter_kind("secret")
        if s.origin is not None and s.origin.variant == "operator-declared"
    )
    assert operator_names == ["a", "b", "c"]


def test_iter_kind_empty_when_kind_absent() -> None:
    r = Registry.empty()
    r.finalize()
    assert list(r.iter_kind("nonexistent")) == []


def test_build_registry_equivalent_to_manual_steps(example_config: Path) -> None:
    """``build_registry(config)`` matches a manual ``empty + publish_to +     finalize``."""
    from agentworks.bootstrap import build_registry

    cfg = load_config(example_config, warn_issues=False)

    auto = build_registry(cfg)

    manual = Registry.empty()
    cfg.publish_to(manual)
    manual.finalize()

    assert auto.is_finalized
    assert manual.is_finalized
    # Both should have the same operator-declared secret.
    assert auto.lookup("secret", "api-key").name == "api-key"
    assert manual.lookup("secret", "api-key").name == "api-key"


def test_build_registry_phase_1a_invokes_only_config_publish_to(
    example_config: Path,
) -> None:
    """Phase 1a invariant: ``build_registry`` does not invoke catalog or any
    other publisher beyond ``Config.publish_to``. This guard prevents Phase
    2b's catalog-publisher addition from going unnoticed in Phase 1; Phase
    2b updates this test to also assert ``catalog.publish_to`` runs first.
    """
    from agentworks.bootstrap import build_registry

    cfg = load_config(example_config, warn_issues=False)
    r = build_registry(cfg)

    # No catalog kinds should have been published in Phase 1a (they ship in
    # 2b). The Phase-1a-registered kinds in KIND_REGISTRY are secret,
    # admin_template, named_console_template; the Resources in the
    # Registry come from Config only.
    for catalog_kind in ("apt_package", "system_install_command", "user_install_command"):
        assert list(r.iter_kind(catalog_kind)) == []


def test_unknown_kind_in_requirement_errors_clearly(tmp_path: Path) -> None:
    """A requirement for a kind that isn't in ``KIND_REGISTRY`` errors with
    the requirement's source so operators can find the offending Resource.
    """
    from agentworks.resources.requirement import ResourceRequirement

    class _ResourceWithBogusReq:
        """Stub Resource exposing a single requirement to an unregistered kind."""

        origin = None
        usage = ()

        def required_resources(self) -> list[ResourceRequirement]:
            return [
                ResourceRequirement(
                    name="anything",
                    kind="not-a-registered-kind",
                    usage="...",
                    source=("test_kind", "test_name"),
                )
            ]

    r = Registry.empty()
    # Cheat past dataclasses.replace by inserting directly. (The real
    # publish flow always goes through frozen-dataclass Resources;
    # here we just want to trip the finalize-side lookup.)
    r._resources.setdefault("test_kind", {})["test_name"] = _ResourceWithBogusReq()

    with pytest.raises(ConfigError, match="unregistered kind"):
        r.finalize()
