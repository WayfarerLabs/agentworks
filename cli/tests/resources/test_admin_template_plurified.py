"""Tests for Phase 2a.3's plurification of ``admin-template``.

The kind moves from singleton-conceptual to named-multi-instance,
matching the shape of the other template kinds. The operator surface is
unchanged in Phase 2a -- ``Config`` still parses only ``[admin]`` and
publishes one ``admin-template:default`` row. These tests verify the
*framework* is plurified: a hypothetical future operator surface
(e.g., ``[admin_templates.work]`` parsing landing in a follow-up SDD)
would Just Work without re-touching the framework.

What we pin:

- ``AdminConfig`` carries its own ``name`` field (default ``"default"``)
  matching the other template kinds' shape.
- ``AdminConfig.referenced_resources`` uses ``self.name`` as the source
  identity (not a hardcoded ``"default"``), so a hypothetical
  ``admin-template:work`` would emit requirements sourced at
  ``("admin-template", "work")``.
- The framework's miss policy still restricts auto-declare to
  ``"default"`` -- typo'd or unreserved names still error.
- The Registry can hold multiple ``admin-template`` rows; one
  operator-declared default coexists with a hypothetical second name
  added via a future publisher.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.agents.template import AdminConfig
from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError
from agentworks.resources import (
    KIND_REGISTRY,
    Origin,
    Registry,
)


def _write_cfg(path: Path, body: str = "") -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


# -- AdminConfig shape ------------------------------------------------------


def test_admin_config_carries_its_own_name_field() -> None:
    """The plurified AdminConfig has a ``name`` field (defaults to
    ``"default"``) like every other template kind. Operator-declared
    instances today always use ``"default"``; future plurified
    operator parsing fills this with the operator-typed name.
    """
    admin = AdminConfig()
    assert admin.name == "default"

    # The shape supports a non-default name even though no loader path
    # produces one today.
    custom = AdminConfig(name="work")
    assert custom.name == "work"


def test_admin_required_resources_sources_from_self_name() -> None:
    """``AdminConfig.referenced_resources`` emits requirements sourced at
    ``("admin-template", self.name)``, not a hardcoded ``"default"``.
    Future-plurified named admin templates inherit the right source
    identity without further changes.
    """
    custom = AdminConfig(
        name="work",
        env={"API_KEY": EnvEntry(key="API_KEY", secret="api-key")},
    )
    reqs = custom.referenced_resources()
    assert reqs  # at least the API_KEY secret requirement
    assert all(r.source == ("admin-template", "work") for r in reqs)


# -- Framework kind shape ---------------------------------------------------
# Kind attributes (kind / miss_policy / auto_declare_names) are pinned in
# tests/resources/test_kind_registry.py; this file pins only the
# plurification-specific behavior layered on top.


def test_admin_template_kind_synthesize_returns_admin_config_with_name() -> None:
    """``_AdminTemplateKind.synthesize(())`` builds an empty-defaults
    ``AdminConfig`` with ``name="default"`` -- the only reserved
    auto-declare name. Pinning the explicit ``name="default"`` because
    Phase 2a.3 added the field; a future change that drops it would
    silently regress the named-multi-instance shape.
    """
    kind = KIND_REGISTRY["admin-template"]
    result = kind.synthesize(())
    assert isinstance(result, AdminConfig)
    assert result.name == "default"
    assert result.origin is not None
    assert result.origin.variant == "auto-declared"


# -- Framework supports multi-row admin (future-surface readiness) ---------


def test_registry_can_hold_multiple_admin_template_rows(tmp_path: Path) -> None:
    """The framework treats ``admin-template`` as named-multi-instance.
    Verify the Registry can hold a default + an additional row, both
    operator-declared, finalize without errors, and look up
    independently. (Operator surface stays singleton in Phase 2a; this
    test exercises the publisher API directly to prove the framework
    is ready for plurified parsing in a future SDD.)
    """
    registry = Registry.empty()
    default = AdminConfig(name="default", shell="bash")
    work = AdminConfig(name="work", shell="zsh")
    origin = Origin.operator_declared(file=tmp_path / "c.toml", line=1)
    registry.add("admin-template", "default", default, origin)
    registry.add("admin-template", "work", work, origin)
    registry.finalize()

    assert registry.lookup("admin-template", "default").shell == "bash"
    assert registry.lookup("admin-template", "work").shell == "zsh"

    names = sorted(r.name for r in registry.iter_kind("admin-template"))
    assert names == ["default", "work"]


def test_admin_template_kind_errors_on_unreserved_name_reference(
    tmp_path: Path,
) -> None:
    """The reserved-name restriction still applies after plurification:
    a downstream Resource whose ``required_resources()`` points at
    ``admin-template:custom`` (without a matching publisher) errors
    via the framework's miss policy. Proves the plurification doesn't
    loosen the auto-declare guard -- ``"default"`` is still the only
    name the framework will synthesize on demand.
    """
    from dataclasses import dataclass

    from agentworks.resources import ResourceReference

    @dataclass(frozen=True)
    class _Stub:
        """A test resource whose required_resources points at a non-
        default admin_template name. Frozen dataclass so the Registry's
        ``dataclasses.replace(resource, origin=...)`` stamp works."""

        origin: Origin | None = None
        references: tuple = ()

        def referenced_resources(self) -> list[ResourceReference]:
            return [
                ResourceReference(
                    name="custom",
                    kind="admin-template",
                    usage="something",
                    source=("vm-template", "test"),
                )
            ]

    registry = Registry.empty()
    origin = Origin.operator_declared(file=tmp_path / "c.toml", line=1)
    registry.add("vm-template", "test", _Stub(), origin)

    with pytest.raises(ConfigError, match="only auto-declares"):
        registry.finalize()


# -- Backwards-compatibility: today's operator surface still works ---------


def test_loader_produces_admin_template_default_unchanged(tmp_path: Path) -> None:
    """A config with no ``[admin.*]`` sections still loads and produces
    an operator-declared (sentinel-line=0) ``admin-template:default``
    row at the Registry level. The plurification didn't change the
    operator-facing behavior.
    """
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)

    admin = registry.lookup("admin-template", "default")
    assert isinstance(admin, AdminConfig)
    assert admin.name == "default"
    assert admin.origin is not None
    assert admin.origin.variant == "operator-declared"


def test_loader_admin_config_section_still_parses(tmp_path: Path) -> None:
    """``[admin.config]`` still produces a single named-default
    AdminConfig. Operator-surface unchanged in Phase 2a.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [admin.config]
        shell = "fish"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.admin.name == "default"
    assert cfg.admin.shell == "fish"


def test_plurified_operator_surface_not_yet_parsed(tmp_path: Path) -> None:
    """Today's loader doesn't recognize ``[admin_templates.<name>]``;
    the section silently passes through (no top-level recognized-keys
    sweep) and ``cfg.admin`` is unchanged. Pinning this so a future
    SDD that wires the plurified parsing has a known starting state.
    """
    cfg_file = _write_cfg(
        tmp_path / "config.toml",
        """
        [admin.config]
        shell = "zsh"

        [admin_templates.work]
        shell = "bash"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    # The singleton admin still parses normally.
    assert cfg.admin.name == "default"
    assert cfg.admin.shell == "zsh"
    # The unrecognized [admin_templates.*] block doesn't produce
    # additional admin-template Resources today.
    registry = build_registry(cfg)
    names = sorted(r.name for r in registry.iter_kind("admin-template"))
    assert names == ["default"]
