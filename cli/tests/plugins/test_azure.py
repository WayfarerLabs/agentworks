"""The ``azure`` system plugin: the opt-in migration of the ``azure-vm``
vm-platform, the ``azdo`` git-credential provider, AND the ``az-cli``
system-install-command out of the core (Phase 11, R11 / R11.1).

The fullest migration: ONE plugin contributing THREE things across two
capability kinds plus a bundled manifest, so this is the end-to-end validation
of the whole model. It drives ``build_registry`` on real config (no fixture
plugin injected via ``SYSTEM_PLUGINS``) and pins every contribution:

- the ``azure-vm`` vm-platform ROW: present-but-disabled with a ``system-plugin``
  origin; a ``vm-site`` on it (a ``resources/`` manifest now, ADR 0022) is
  not-ready with the "enable plugin `azure`" hint and ``resolve_site`` refuses
  it. The legacy ``[azure]`` flat section is a hard error at load now (pinned in
  ``tests/vms/test_legacy_site_sections.py``), so its former "guided, not
  broken" degrade-to-hint behavior no longer applies;
- the ``azdo`` git-credential-provider ROW: present-but-disabled; a
  ``git-credential`` naming ``provider = "azdo"`` is not-ready via its R14
  propagate hook and refused at use;
- the ``az-cli`` system-install-command ROW: present-but-disabled (weak), so a
  vm-template's ``system_install_commands = ["az-cli"]`` finalizes cleanly
  (never an unknown-name error) and is refused at use by the Phase 7 recipe gate
  with the enable hint.

Enabling ``[plugins] system = ["azure"]`` makes all three consumable, from the
ONE plugin.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.resources.access import ensure_recipe_enabled
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import describe_resource, list_resources
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.config import Config

# A ``vm-site`` manifest named ``azure`` on the ``azure-vm`` platform, the
# declarative replacement for the legacy ``[azure]`` flat section (a hard error
# now, ADR 0022; pinned in ``tests/vms/test_legacy_site_sections.py``). The
# ordinary site fixture.
_AZURE_SITE = ManifestDoc(
    "vm-site",
    "azure",
    {
        "platform": {
            "name": "azure-vm",
            "subscription_id": "sub-123",
            "resource_group": "rg-agw",
            "region": "westus2",
            "auth": {"mode": "ambient"},
        }
    },
)

# An azdo git-credential (the R14 propagate hook makes the credential not-ready
# when azdo is disabled).
_AZDO_CRED = ManifestDoc("git-credential", "azdo", {"provider": {"name": "azdo", "org": "my-org"}})

# A vm-template whose system_install_commands draws on the az-cli row (the
# Phase 7 recipe gate refuses the template while azure is disabled).
_AZ_CLI_TEMPLATE = ManifestDoc("vm-template", "azcli", {"system_install_commands": ["az-cli"]})


def _config(
    tmp_path: Path,
    *,
    enabled: bool = False,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Config:
    """A real operator config; ``enabled`` toggles ``[plugins] system =
    ["azure"]`` and ``manifests`` seeds resource declarations beside it."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["azure"]\n\n' if enabled else ""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + plugins
    )
    if manifests:
        write_manifests(tmp_path, *manifests)
    return load_config(cfg, warn_issues=False, warn_deprecations=False)


# -- seating: ONE plugin, three contributions --------------------------------


def test_azure_seated_by_plugin() -> None:
    """The three contributions ship as the ONE ``azure`` system plugin, whose
    adapters re-seat both capability classes into their code registries at
    import (so resolution finds them by registry name), and the plugin is
    indexed once."""
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "azure" in SYSTEM_PLUGINS
    assert "azure-vm" in VM_PLATFORM_REGISTRY
    assert "azdo" in GIT_CREDENTIAL_PROVIDER_REGISTRY


def test_one_plugin_contributes_all_three_kinds() -> None:
    """The ``azure`` descriptor carries both capability kinds AND a manifests
    anchor: one plugin, three contributions (vm-platform + git-credential +
    the az-cli install-command)."""
    from agentworks.plugins import SYSTEM_PLUGINS

    plugin = SYSTEM_PLUGINS["azure"]
    assert set(plugin.capabilities) == {"vm-platform", "git-credential-provider"}
    assert plugin.manifests == "agentworks.plugins.azure"


# -- all three rows present-but-disabled by default --------------------------


def test_all_three_rows_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    """Each of the three azure contributions publishes present-but-disabled
    with a ``system-plugin`` origin whose plugin is ``azure``, until the
    operator opts in (no longer built-in)."""
    registry = build_registry(_config(tmp_path))
    for kind, name in (
        ("vm-platform", "azure-vm"),
        ("git-credential-provider", "azdo"),
        ("system-install-command", "az-cli"),
    ):
        row = registry.lookup(kind, name)
        assert row.origin.variant == "system-plugin", (kind, name)
        assert row.origin.plugin == "azure", (kind, name)
        assert registry.graph.enablement_of(kind, name) is Enablement.disabled, (kind, name)

    # Content pin (the manifest move must preserve the row byte-for-byte): the
    # az-cli install-command's actual command + test_exec, so a typo in the
    # plugin's install-commands.yaml is caught here, not at real VM setup.
    az_cli = registry.lookup("system-install-command", "az-cli")
    assert az_cli.command == "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
    assert az_cli.test_exec == "az"


def test_core_capabilities_stay_builtin(tmp_path: Path) -> None:
    """The common local path is untouched: the core platforms and the github
    provider remain built-in, enabled rows after the migration."""
    registry = build_registry(_config(tmp_path))
    for kind, name in (
        ("vm-platform", "lima"),
        ("vm-platform", "wsl2"),
        ("git-credential-provider", "github"),
    ):
        row = registry.lookup(kind, name)
        assert row.origin.variant == "built-in", (kind, name)
        assert registry.graph.enablement_of(kind, name) is Enablement.enabled, (kind, name)


def test_disabled_rows_hidden_from_list_shown_by_describe(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    default_rows = {(r.kind, r.name) for r in list_resources(registry).rows}
    for kind, name in (
        ("vm-platform", "azure-vm"),
        ("git-credential-provider", "azdo"),
        ("system-install-command", "az-cli"),
    ):
        assert (kind, name) not in default_rows, (kind, name)
        desc = describe_resource(registry, kind, name)
        assert desc.disabled_reason is not None, (kind, name)
        assert "azure" in desc.disabled_reason, (kind, name)


# -- the vm-site use-gate (R14, platform propagate) --------------------------


def test_site_on_disabled_azure_is_not_ready_with_hint(tmp_path: Path) -> None:
    """A ``vm-site`` on the ``azure-vm`` platform is not-ready while the plugin
    is disabled, and the readiness reason names the plugin to enable (the fold
    propagates the platform's disabled mark to the site)."""
    registry = build_registry(_config(tmp_path, manifests=[_AZURE_SITE]))
    assert not registry.graph.is_ready("vm-site", "azure")
    reason = registry.graph.readiness_of("vm-site", "azure").reason
    assert reason is not None
    assert "enable plugin `azure`" in reason


def test_resolve_site_refuses_disabled_azure_with_hint(tmp_path: Path) -> None:
    """``resolve_site`` (the chokepoint every VM operation passes through)
    refuses an azure site while the plugin is disabled, before any work."""
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, manifests=[_AZURE_SITE]))
    with pytest.raises(StateError) as exc:
        resolve_site("azure", registry)
    assert "enable plugin `azure`" in str(exc.value)


# The R11.1 "guided, not broken" pin for the LEGACY ``[azure]`` flat section is
# retired: under ADR 0022 that section is a hard error at load, not a
# silently-disabled site, so the "lands on the disabled row" premise is
# structurally gone. The hard error (carrying the vm-site migration guidance) is
# pinned by ``tests/vms/test_legacy_site_sections.py``; the disabled-site enable
# hint stays covered by ``test_site_on_disabled_azure_is_not_ready_with_hint``.


# -- the git-credential use-gate (R14, provider propagate) -------------------


def test_credential_on_disabled_azdo_is_not_ready_with_hint(tmp_path: Path) -> None:
    """A ``git-credential`` naming ``provider = "azdo"`` is not-ready while the
    plugin is disabled: the provider's disabled mark propagates to the
    credential via its R14 ``not_ready`` hook, with the enable hint."""
    registry = build_registry(_config(tmp_path, manifests=[_AZDO_CRED]))
    assert not registry.graph.is_ready("git-credential", "azdo")
    reason = registry.graph.readiness_of("git-credential", "azdo").reason
    assert reason is not None
    assert "enable plugin `azure`" in reason


def test_resolve_git_credential_refuses_disabled_azdo(tmp_path: Path) -> None:
    """The provider-construction chokepoint refuses an azdo credential while the
    plugin is disabled, before any credential materials are built."""
    from agentworks.vms.initializer import resolve_git_credential_providers

    registry = build_registry(_config(tmp_path, manifests=[_AZDO_CRED]))
    with pytest.raises(StateError) as exc:
        resolve_git_credential_providers(registry, ["azdo"])
    assert "not ready" in str(exc.value)


# -- the install-command recipe use-gate (Phase 7 manifest parity) -----------


def test_template_referencing_az_cli_finalizes_when_disabled(tmp_path: Path) -> None:
    """The parity crux: a vm-template's ``system_install_commands = ["az-cli"]``
    finalizes cleanly while azure is not enabled. Before the migration an
    unknown name here was a hard ``references unknown system-install-command``
    error; now the row is present-but-disabled, so the reference is valid."""
    registry = build_registry(_config(tmp_path, manifests=[_AZ_CLI_TEMPLATE]))
    assert registry.graph.enablement_of("system-install-command", "az-cli") is Enablement.disabled


def test_recipe_gate_refuses_disabled_az_cli_with_hint(tmp_path: Path) -> None:
    """The recipe gate refuses a vm-template whose closure draws on the disabled
    ``az-cli`` install-command, naming the plugin to enable, before any
    transport work."""
    registry = build_registry(_config(tmp_path, manifests=[_AZ_CLI_TEMPLATE]))
    with pytest.raises(StateError) as exc:
        ensure_recipe_enabled(registry, "vm-template", "azcli")
    message = str(exc.value)
    assert "az-cli" in message
    assert "enable plugin `azure`" in message


def test_operator_override_of_az_cli_wins(tmp_path: Path) -> None:
    """An operator who declares their own ``az-cli`` system-install-command
    overrides the disabled plugin row with no collision error (the plugin row
    publishes weak while disabled)."""
    registry = build_registry(
        _config(
            tmp_path,
            manifests=[
                ManifestDoc(
                    "system-install-command",
                    "az-cli",
                    {"command": "echo operator-az"},
                    description="operator az installer",
                )
            ],
        )
    )
    row = registry.lookup("system-install-command", "az-cli")
    assert row.origin.variant == "operator-declared"
    assert row.command == "echo operator-az"


# -- enabling makes all three consumable, from the one plugin ----------------


def test_enabling_azure_makes_all_three_work(tmp_path: Path) -> None:
    """With ``[plugins] system = ["azure"]`` all three contributions enable:
    the vm-site becomes ready and resolvable, the git-credential becomes ready
    and constructible, and the vm-template's az-cli reference passes the recipe
    gate. One opt-in, three kinds."""
    from agentworks.capabilities.vm_platform.base import VMPlatform
    from agentworks.vms.initializer import resolve_git_credential_providers
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, enabled=True, manifests=[_AZURE_SITE, _AZDO_CRED, _AZ_CLI_TEMPLATE]))

    for kind, name in (
        ("vm-platform", "azure-vm"),
        ("git-credential-provider", "azdo"),
        ("system-install-command", "az-cli"),
    ):
        assert registry.graph.enablement_of(kind, name) is Enablement.enabled, (kind, name)

    platform = resolve_site("azure", registry)  # no raise
    assert isinstance(platform, VMPlatform)
    assert platform.name == "azure-vm"

    providers = resolve_git_credential_providers(registry, ["azdo"])  # no raise
    assert providers["azdo"].name == "azdo"

    ensure_recipe_enabled(registry, "vm-template", "azcli")  # no raise


# -- the doctor roster -------------------------------------------------------


def test_doctor_roster_lists_the_azure_plugin(tmp_path: Path) -> None:
    """``agw doctor``'s System plugins roster lists azure (ONE plugin, its
    description), disabled by default and enabled once opted in (the discovery
    surface the enable hint points at)."""
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(c for c in disabled.checks if c.name == "plugin azure")
    assert row.status is Status.INFO
    assert "not enabled in [plugins].system" in (row.message or "")

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row_on = next(c for c in enabled.checks if c.name == "plugin azure")
    assert row_on.status is Status.OK
