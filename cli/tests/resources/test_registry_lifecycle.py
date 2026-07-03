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
    always-materialized ``vm-template:default`` emits a
    ``SecretReference`` for ``tailscale-auth-key`` via its
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
    """``build_registry(config)`` matches the manual publisher sequence.

    The manual side needs the bundled built-in manifests (backend rows)
    and the provider descriptors alongside ``Config.publish_to``: the
    published ``secret-config`` row's chain references the built-in
    backends, so finalize errors without them -- exactly the graph
    completeness the framework is supposed to enforce.
    """
    from agentworks import secrets
    from agentworks.bootstrap import build_registry
    from agentworks.manifests import builtin as builtin_manifests

    cfg = load_config(example_config, warn_issues=False)

    auto = build_registry(cfg)

    manual = Registry.empty()
    builtin_manifests.publish_to(manual)
    secrets.publish_to(manual)
    cfg.publish_to(manual)
    manual.finalize()

    assert auto.is_finalized
    assert manual.is_finalized
    # Both should have the same operator-declared secret.
    assert auto.lookup("secret", "api-key").name == "api-key"
    assert manual.lookup("secret", "api-key").name == "api-key"


def test_build_registry_publishes_catalog_before_config(
    example_config: Path,
) -> None:
    """Phase 2b invariant: ``build_registry`` invokes
    ``catalog.publish_to`` before ``Config.publish_to`` so any
    operator-declared catalog override layers on top of the
    built-in base. Verified end-to-end: every catalog kind has
    at least one row after build_registry, and those rows carry
    ``Origin.built_in(source="agentworks.catalog")``.
    """
    from agentworks.bootstrap import build_registry

    cfg = load_config(example_config, warn_issues=False)
    r = build_registry(cfg)

    for catalog_kind in (
        "apt-source",
        "apt-package",
        "system-install-command",
        "user-install-command",
    ):
        rows = list(r.iter_kind(catalog_kind))
        assert rows, f"expected at least one {catalog_kind} row from the catalog publisher"
        # The built-in catalog rows are built-in. Operator overrides
        # (if any) would re-publish the same name with operator-declared
        # origin; the test's example_config doesn't exercise that path.
        for row in rows:
            assert row.origin is not None
            assert row.origin.variant == "built-in"
            assert row.origin.source == "agentworks.catalog"


def test_unknown_kind_in_requirement_errors_clearly(tmp_path: Path) -> None:
    """A requirement for a kind that isn't in ``KIND_REGISTRY`` errors with
    the requirement's source so operators can find the offending Resource.
    """
    from agentworks.resources.reference import ResourceReference

    class _ResourceWithBogusReq:
        """Stub Resource exposing a single requirement to an unregistered kind."""

        origin = None
        usage = ()

        def referenced_resources(self) -> list[ResourceReference]:
            return [
                ResourceReference(
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
