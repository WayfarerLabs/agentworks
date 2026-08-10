"""The disabled-by-default ``gcp`` system plugin and ``gcp-gce`` platform."""

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

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config


_GCP_SITE = """
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gcp-dev
spec:
  platform:
    name: gcp-gce
    project_id: agentworks-dev
    zone: us-central1-a
    auth: { mode: ambient }
"""

_GCLOUD_TEMPLATE = """
apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: gcloud-tools
spec:
  system_install_commands: [gcloud-cli]
"""


def _config(tmp_path: Path, site: str = "", *, enabled: bool = False) -> Config:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["gcp"]\n' if enabled else ""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + plugins
    )
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    if site:
        (resources / "site.yaml").write_text(site)
    return load_config(cfg, warn_issues=False, warn_deprecations=False)


def test_gcp_is_seated_by_vendor_bundle() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS
    from agentworks.plugins.gcp.platform import GCEPlatform

    assert SYSTEM_PLUGINS["gcp"].capabilities == {"vm-platform": (GCEPlatform,)}
    assert SYSTEM_PLUGINS["gcp"].manifests == "agentworks.plugins.gcp"
    assert VM_PLATFORM_REGISTRY["gcp-gce"] is GCEPlatform
    assert GCEPlatform.contract_version == 2


def test_gcp_row_is_present_but_disabled_by_default(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("vm-platform", "gcp-gce")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "gcp"
    assert registry.graph.enablement_of("vm-platform", "gcp-gce") is Enablement.disabled
    assert ("vm-platform", "gcp-gce") not in {(row.kind, row.name) for row in list_resources(registry).rows}
    assert "gcp" in (describe_resource(registry, "vm-platform", "gcp-gce").disabled_reason or "")


def test_gcp_bundle_publishes_gcloud_disabled_with_exact_payload(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("system-install-command", "gcloud-cli")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "gcp"
    assert registry.graph.enablement_of("system-install-command", "gcloud-cli") is Enablement.disabled
    assert row.test_exec == "gcloud"
    assert "packages.cloud.google.com/apt/doc/apt-key.gpg" in row.command
    assert "signed-by=${keyring}" in row.command
    assert "CLOUDSDK_SKIP_PY_COMPILATION=1 apt-get install -y google-cloud-cli" in row.command


def test_gcloud_recipe_is_gated_until_gcp_is_enabled(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, _GCLOUD_TEMPLATE))
    with pytest.raises(StateError, match="enable plugin `gcp`"):
        ensure_recipe_enabled(registry, "vm-template", "gcloud-tools")

    enabled = build_registry(_config(tmp_path, _GCLOUD_TEMPLATE, enabled=True))
    ensure_recipe_enabled(enabled, "vm-template", "gcloud-tools")


def test_operator_gcloud_override_wins_while_gcp_is_disabled(tmp_path: Path) -> None:
    override = """
apiVersion: agentworks/v1
kind: system-install-command
metadata:
  name: gcloud-cli
spec:
  command: echo operator-gcloud
"""
    row = build_registry(_config(tmp_path, override)).lookup("system-install-command", "gcloud-cli")
    assert row.origin.variant == "operator-declared"
    assert row.command == "echo operator-gcloud"


def test_disabled_gcp_site_is_not_ready_and_refused_with_enable_hint(tmp_path: Path) -> None:
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, _GCP_SITE))
    reason = registry.graph.readiness_of("vm-site", "gcp-dev").reason
    assert reason is not None
    assert "enable plugin `gcp`" in reason
    with pytest.raises(StateError, match="enable plugin `gcp`"):
        resolve_site("gcp-dev", registry)


def test_enabling_gcp_makes_site_ready_and_resolvable(tmp_path: Path) -> None:
    from agentworks.plugins.gcp.platform import GCEPlatform
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, _GCP_SITE, enabled=True))
    assert registry.graph.enablement_of("vm-platform", "gcp-gce") is Enablement.enabled
    assert registry.graph.readiness_of("vm-site", "gcp-dev").reason is None
    assert isinstance(resolve_site("gcp-dev", registry), GCEPlatform)


def test_gcp_cli_is_discoverable_from_registry_surfaces(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    assert "gcloud-cli" not in {row.name for row in list_resources(registry).rows}
    assert "gcloud-cli" in {
        row.name for row in list_resources(registry, include_disabled=True).rows if row.kind == "system-install-command"
    }


def test_doctor_roster_tracks_gcp_enablement(tmp_path: Path) -> None:
    from agentworks.doctor import Status, _check_plugins

    disabled = next(check for check in _check_plugins(_config(tmp_path)).checks if check.name == "plugin gcp")
    assert disabled.status is Status.INFO
    assert "not enabled in [plugins].system" in (disabled.message or "")
    enabled = next(
        check for check in _check_plugins(_config(tmp_path, enabled=True)).checks if check.name == "plugin gcp"
    )
    assert enabled.status is Status.OK


def test_registry_drives_gcp_sample_and_guide_completion_names() -> None:
    from agentworks.guide import GuideMode
    from agentworks.guide.service import render_guide
    from agentworks.manifests.samples import sample_text

    assert "gcp-gce" in sample_text("vm-site")
    response = render_guide((), GuideMode.AGENT, names_only=True)
    assert "vm-platform/gcp-gce" in response.names
