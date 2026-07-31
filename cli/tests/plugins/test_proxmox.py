"""The ``proxmox`` system plugin: the opt-in migration of the ``proxmox``
vm-platform (and its ``proxmox_api`` sibling) out of the core (Phase 10, R11 /
R11.1).

A capability-only migration (no bundled manifests), so this drives
``build_registry`` on real config (no fixture plugin injected via
``SYSTEM_PLUGINS``) and pins the vm-platform bundle end to end:

- the ``proxmox`` vm-platform ROW: present-but-disabled with a ``system-plugin``
  origin until the operator opts in (the core built-in platforms are untouched);
- a ``vm-site`` on the ``proxmox`` platform: not-ready with the
  "enable plugin `proxmox`" hint, and ``resolve_site`` refuses it at use, until
  ``[plugins] system = ["proxmox"]``. The deprecated legacy ``[proxmox]``
  flat-section site gets the SAME hint, not an unknown-name hard error (a
  feature: legacy configs are guided, not broken).

Enabling ``[plugins] system = ["proxmox"]`` makes the site ready and
resolvable.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import describe_resource, list_resources

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config

# A well-formed legacy ``[proxmox]`` flat section: it auto-declares a vm-site
# named ``proxmox`` on the ``proxmox`` platform. Used both as the ordinary
# site fixture and as the legacy-section breaking-change pin.
_PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""


def _config(tmp_path: Path, body: str = "", *, enabled: bool = False) -> Config:
    """A real operator config; ``enabled`` toggles ``[plugins] system =
    ["proxmox"]`` and ``body`` appends resource declarations."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["proxmox"]\n\n' if enabled else ""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + plugins
        + dedent(body)
    )
    return load_config(cfg, warn_issues=False, warn_deprecations=False)


# -- seating + the present-but-disabled row ----------------------------------


def test_proxmox_seated_by_plugin() -> None:
    """The proxmox platform ships as the ``proxmox`` system plugin, whose
    adapter re-seats the platform class into the code registry at import (so
    site resolution can construct it), and the plugin is indexed."""
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "proxmox" in SYSTEM_PLUGINS
    assert "proxmox" in VM_PLATFORM_REGISTRY


def test_platform_row_is_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    """The ``proxmox`` vm-platform row publishes present-but-disabled with a
    ``system-plugin`` origin until the operator opts in (no longer a
    built-in)."""
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("vm-platform", "proxmox")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "proxmox"
    assert registry.graph.enablement_of("vm-platform", "proxmox") is Enablement.disabled


def test_core_platforms_stay_builtin(tmp_path: Path) -> None:
    """The common local path is untouched: the core platforms remain built-in,
    enabled rows after the migration. (``azure-vm`` is no longer among them: it
    migrated to the ``azure`` system plugin in Phase 11.)"""
    registry = build_registry(_config(tmp_path))
    for name in ("lima", "wsl2"):
        row = registry.lookup("vm-platform", name)
        assert row.origin.variant == "built-in"
        assert registry.graph.enablement_of("vm-platform", name) is Enablement.enabled


def test_disabled_row_hidden_from_list_shown_by_describe(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    default_rows = {(r.kind, r.name) for r in list_resources(registry).rows}
    assert ("vm-platform", "proxmox") not in default_rows

    desc = describe_resource(registry, "vm-platform", "proxmox")
    assert desc.disabled_reason is not None
    assert "proxmox" in desc.disabled_reason


# -- the vm-site use-gate (R14) ----------------------------------------------


def test_site_on_disabled_proxmox_is_not_ready_with_hint(tmp_path: Path) -> None:
    """A ``vm-site`` on the ``proxmox`` platform is not-ready while the plugin
    is disabled, and the readiness reason names the plugin to enable (the fold
    propagates the platform's disabled mark to the site)."""
    registry = build_registry(_config(tmp_path, _PROXMOX_SECTION))
    assert not registry.graph.is_ready("vm-site", "proxmox")
    reason = registry.graph.readiness_of("vm-site", "proxmox").reason
    assert reason is not None
    assert "enable plugin `proxmox`" in reason


def test_resolve_site_refuses_disabled_proxmox_with_hint(tmp_path: Path) -> None:
    """``resolve_site`` (the chokepoint every VM operation passes through)
    refuses a proxmox site while the plugin is disabled, before any work."""
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, _PROXMOX_SECTION))
    with pytest.raises(StateError) as exc:
        resolve_site("proxmox", registry)
    assert "enable plugin `proxmox`" in str(exc.value)


def test_legacy_proxmox_section_is_guided_not_broken(tmp_path: Path) -> None:
    """The guided breaking change (R11.1): a deprecated legacy ``[proxmox]``
    flat-section site lands on the present-but-disabled row, so it degrades to
    the enable hint rather than an unknown-name hard error."""
    registry = build_registry(_config(tmp_path, _PROXMOX_SECTION))
    # The site is present (published from the legacy section), just not ready.
    assert registry.lookup("vm-site", "proxmox") is not None
    reason = registry.graph.readiness_of("vm-site", "proxmox").reason
    assert reason is not None and "enable plugin `proxmox`" in reason


def test_enabling_proxmox_makes_the_site_ready_and_resolvable(tmp_path: Path) -> None:
    """With ``[plugins] system = ["proxmox"]`` the platform row enables, the
    site becomes ready, and ``resolve_site`` constructs the platform."""
    from agentworks.capabilities.vm_platform.base import VMPlatform
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, _PROXMOX_SECTION, enabled=True))
    assert registry.graph.enablement_of("vm-platform", "proxmox") is Enablement.enabled
    assert registry.graph.is_ready("vm-site", "proxmox")
    platform = resolve_site("proxmox", registry)  # no raise
    assert isinstance(platform, VMPlatform)
    assert platform.name == "proxmox"


# -- the doctor roster -------------------------------------------------------


def test_doctor_roster_lists_the_proxmox_plugin(tmp_path: Path) -> None:
    """``agw doctor``'s System plugins roster lists proxmox, disabled by default
    and enabled once opted in (the discovery surface the enable hint points
    at)."""
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(c for c in disabled.checks if c.name == "plugin proxmox")
    assert row.status is Status.INFO
    assert "not enabled in [plugins].system" in (row.message or "")

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row_on = next(c for c in enabled.checks if c.name == "plugin proxmox")
    assert row_on.status is Status.OK
