"""Tests for Phase 2a.3's plurification of ``admin-template``.

The kind moves from singleton-conceptual to named-multi-instance,
matching the shape of the other template kinds. These tests verify the
*framework* is plurified: named admin-template declarations (as manifests
now that config.toml is settings only, ADR 0022) publish as independent
rows without re-touching the framework.

What we pin:

- ``AdminConfig`` carries its own ``name`` field (default ``"default"``)
  matching the other template kinds' shape.
- ``AdminConfig.dependencies`` uses ``self.name`` as the source
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

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.env import EnvEntry
from agentworks.errors import ConfigError
from agentworks.resources import (
    Origin,
    Registry,
)
from agentworks.resources.graph import FinalizeContext
from agentworks.vms.admin import AdminConfig
from tests.conftest import ManifestDoc, write_cfg, write_manifests


def _write_cfg(path: Path, body: str = "") -> Path:
    """``write_cfg`` under this file's path-taking spelling."""
    return write_cfg(path.parent, settings=body, filename=path.name)


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


def test_admin_dependencies_sources_from_self_name() -> None:
    """``AdminConfig.dependencies`` emits requirements sourced at
    ``("admin-template", self.name)``, not a hardcoded ``"default"``.
    Future-plurified named admin templates inherit the right source
    identity without further changes.
    """
    custom = AdminConfig(
        name="work",
        env={"API_KEY": EnvEntry({"secret": "api-key"})},
    )
    reqs = custom.dependencies(FinalizeContext())
    assert reqs  # at least the API_KEY secret requirement
    assert all(r.source == ("admin-template", "work") for r in reqs)


# -- Framework kind shape ---------------------------------------------------
# Kind attributes (kind / miss_policy / auto_declare_names) are pinned in
# tests/resources/test_kind_registry.py; this file pins only the
# plurification-specific behavior layered on top.


# What ``_AdminTemplateKind.synthesize(())`` builds is pinned in
# ``test_kind_synthesize_empty.py``, beside the other kinds' answers to
# the same call, and with the framework source this file never asserted.


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
    a downstream Resource whose ``dependencies()`` points at
    ``admin-template:custom`` (without a matching publisher) errors
    via the framework's miss policy. Proves the plurification doesn't
    loosen the auto-declare guard -- ``"default"`` is still the only
    name the framework will synthesize on demand.
    """
    from dataclasses import dataclass

    from agentworks.resources import ResourceReference

    @dataclass(frozen=True)
    class _Stub:
        """A test resource whose dependencies points at a non-
        default admin_template name. Frozen dataclass so the Registry's
        ``dataclasses.replace(resource, origin=...)`` stamp works."""

        origin: Origin | None = None
        references: tuple = ()

        def dependencies(self, context: object) -> list[ResourceReference]:
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


# -- Operator surface: admin-template defaults -----------------------------


# That an undeclared admin-template still lands as an auto-declared
# ``default`` is ``test_singleton_publishing.py``'s subject, where it is
# asserted with the provenance and the row count too. What this file is
# for is the PLURAL shape: several rows under one kind.


def test_admin_config_manifest_still_produces_named_default(tmp_path: Path) -> None:
    """An admin-template manifest declaring ``shell = "fish"`` still
    produces a single named-default ``AdminConfig`` with that shell:
    the admin-template default surface, now declared as a manifest
    (config.toml is settings only, ADR 0022).
    """
    cfg_file = _write_cfg(tmp_path / "config.toml")
    write_manifests(cfg_file.parent, ManifestDoc("admin-template", "default", {"shell": "fish"}))
    cfg = load_config(cfg_file, warn_issues=False)
    admin = build_registry(cfg).lookup("admin-template", "default")
    assert admin.name == "default"
    assert admin.shell == "fish"
