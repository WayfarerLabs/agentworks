"""Tests for ``Registry`` lifecycle: empty -> add -> finalize -> queryable.

Also covers ``build_registry(config)`` convenience.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def test_add_rejects_names_containing_slash(tmp_path: Path) -> None:
    """'/' is banned in resource names at the single publisher choke
    point (maintainer ruling, 2026-07-05): it is reserved for kind/name
    selectors and per-resource manifest filenames. Uniform across
    sources -- TOML, YAML, and direct adds all hit the same check."""
    r = Registry.empty()
    decl = SecretDecl(name="we/ird", description="d")
    with pytest.raises(ConfigError, match="contains '/'"):
        r.add(
            "secret",
            "we/ird",
            decl,
            Origin.operator_declared(file=tmp_path / "c.toml", line=1),
        )

    # TOML source: a quoted section name with a slash loads as data but
    # is refused at publish.
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        dedent(
            f"""
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            [vm_templates."we/ird"]
            cpus = 2
            """
        )
    )
    from agentworks.bootstrap import build_registry

    with pytest.raises(ConfigError, match="contains '/'"):
        build_registry(load_config(cfg, warn_issues=False))

    # Manifest source: same rule, error cites the manifest origin.
    cfg2 = tmp_path / "c2.toml"
    cfg2.write_text(
        dedent(
            f"""
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"
            """
        )
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "r.yaml").write_text(
        dedent(
            """
            apiVersion: agentworks/v1
            kind: vm-template
            metadata:
              name: we/ird
            spec:
              cpus: 2
            """
        )
    )
    with pytest.raises(ConfigError, match="contains '/'"):
        build_registry(load_config(cfg2, warn_issues=False))


class _MissProbeKind:
    """Minimal error-miss-policy kind for the message-shape test."""

    kind = "miss-probe"
    miss_policy = "error"
    auto_declare_names = None
    category = "capability"
    description = "test-only miss probe"
    builtin_override = "reserved"

    def synthesize(self, references: object) -> object:
        raise AssertionError("never dispatched: miss_policy='error'")


@dataclass(frozen=True)
class _RefEmitter:
    """Resource double whose only job is emitting one dangling reference."""

    name: str
    origin: object = None
    references: tuple = ()  # type: ignore[type-arg]

    def referenced_resources(self) -> list[object]:
        from agentworks.resources.reference import ResourceReference

        return [
            ResourceReference(
                name="missing",
                kind="miss-probe",
                usage="the probe dependency",
                source=("miss-probe", self.name),
            )
        ]


def test_error_miss_policy_includes_reference_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The error-miss ConfigError carries the reference's usage in the
    message so the operator sees what needed the missing resource."""
    from agentworks.resources.kind import KIND_REGISTRY

    probe = _MissProbeKind()
    monkeypatch.setitem(KIND_REGISTRY, "miss-probe", probe)

    r = Registry.empty()
    r.add(
        "miss-probe",
        "seed",
        _RefEmitter(name="seed"),
        Origin.operator_declared(file=tmp_path / "c.toml", line=1),
    )
    with pytest.raises(ConfigError) as exc:
        r.finalize()
    assert "references unknown miss-probe 'missing'" in str(exc.value)
    assert "(the probe dependency)" in str(exc.value)


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


def test_build_registry_equivalent_to_manual_steps(
    example_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_registry(config)`` matches the manual publisher sequence.

    The manual side needs the bundled built-in manifests (backend rows)
    and the provider descriptors alongside ``Config.publish_to``: the
    published ``secret-config`` row's chain references the built-in
    backends, so finalize errors without them -- exactly the graph
    completeness the framework is supposed to enforce. Platform support
    is stubbed so both sides see the full four-platform graph regardless
    of the test host's OS/tooling (host gating has its own tests).
    """
    from agentworks import secrets
    from agentworks.bootstrap import build_registry
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.manifests import builtin as builtin_manifests
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    cfg = load_config(example_config, warn_issues=False)

    auto = build_registry(cfg)

    manual = Registry.empty()
    builtin_manifests.publish_to(manual)
    secrets.publish_to(manual)
    # The bundled vm-site rows (lima, wsl2) reference the vm-platform
    # capability rows, so the manual sequence needs the platform
    # publisher for the same graph-completeness reason as the backends.
    vm_platforms.publish_to(manual)
    cfg.publish_to(manual)
    manual.finalize()

    assert auto.is_finalized
    assert manual.is_finalized
    # Both should have the same operator-declared secret.
    assert auto.lookup("secret", "api-key").name == "api-key"
    assert manual.lookup("secret", "api-key").name == "api-key"


def test_build_registry_publishes_builtin_apt_and_install_entries(
    example_config: Path,
) -> None:
    """The built-in apt / install-command entries publish before the
    operator sources so any operator override (TOML or manifest) layers
    on top of the built-in base. Verified end-to-end: every apt /
    install-command kind has at least one row after build_registry, and
    those rows carry ``Origin.built_in`` with a bundled-manifest source
    (the entries ship as ``manifests/builtin/*.yaml``).
    """
    from agentworks.bootstrap import build_registry

    cfg = load_config(example_config, warn_issues=False)
    r = build_registry(cfg)

    for kind in (
        "apt-source",
        "apt-package",
        "system-install-command",
        "user-install-command",
    ):
        rows = list(r.iter_kind(kind))
        assert rows, f"expected at least one {kind} row from the bundled built-in manifests"
        # The built-in rows are built-in. Operator overrides (if any)
        # would re-publish the same name with operator-declared origin;
        # the test's example_config doesn't exercise that path.
        for row in rows:
            assert row.origin is not None
            assert row.origin.variant == "built-in"
            assert row.origin.source is not None
            assert row.origin.source.startswith("agentworks.manifests.builtin/")
            assert row.origin.source.endswith(".yaml")


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
