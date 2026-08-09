"""The ``aws`` system plugin: the opt-in EC2 vm-platform.

A capability-only plugin in the proxmox mould (no bundled manifest, no
install-command). It drives ``build_registry`` on real config and pins that its
one contribution, the ``aws-ec2`` vm-platform, publishes present-but-disabled with
a ``system-plugin`` origin until the operator opts in with
``[plugins] system = ["aws"]``, at which point a ``vm-site`` on it becomes
ready and resolvable.
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

# A vm-site on the aws-ec2 platform (ambient credentials: no secret declared).
_AWS_SITE = """
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: aws-dev
spec:
  platform:
    name: aws-ec2
    region: us-east-1
    auth: { mode: ambient }
"""


def _config(tmp_path: Path, site: str = "", *, enabled: bool = False) -> Config:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["aws"]\n' if enabled else ""
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


def test_aws_seated_by_plugin() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "aws" in SYSTEM_PLUGINS
    assert "aws-ec2" in VM_PLATFORM_REGISTRY
    plugin = SYSTEM_PLUGINS["aws"]
    assert set(plugin.capabilities) == {"vm-platform"}
    # No bundled manifest: EC2 talks to AWS in-process, so it ships no
    # install-command (unlike the azure plugin's az-cli).
    assert plugin.manifests is None


def test_aws_ec2_row_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("vm-platform", "aws-ec2")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "aws"
    assert registry.graph.enablement_of("vm-platform", "aws-ec2") is Enablement.disabled


def test_disabled_aws_ec2_hidden_from_list_shown_by_describe(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    default_rows = {(r.kind, r.name) for r in list_resources(registry).rows}
    assert ("vm-platform", "aws-ec2") not in default_rows
    desc = describe_resource(registry, "vm-platform", "aws-ec2")
    assert desc.disabled_reason is not None
    assert "aws" in desc.disabled_reason


def test_site_on_disabled_aws_is_not_ready_with_hint(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, _AWS_SITE))
    assert not registry.graph.is_ready("vm-site", "aws-dev")
    reason = registry.graph.readiness_of("vm-site", "aws-dev").reason
    assert reason is not None
    assert "enable plugin `aws`" in reason


def test_resolve_site_refuses_disabled_aws_with_hint(tmp_path: Path) -> None:
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, _AWS_SITE))
    with pytest.raises(StateError) as exc:
        resolve_site("aws-dev", registry)
    assert "enable plugin `aws`" in str(exc.value)


def test_enabling_aws_makes_the_site_ready_and_resolvable(tmp_path: Path) -> None:
    from agentworks.capabilities.vm_platform.base import VMPlatform
    from agentworks.vms.sites import resolve_site

    registry = build_registry(_config(tmp_path, _AWS_SITE, enabled=True))
    assert registry.graph.enablement_of("vm-platform", "aws-ec2") is Enablement.enabled
    assert registry.graph.readiness_of("vm-site", "aws-dev").reason is None

    platform = resolve_site("aws-dev", registry)  # no raise
    assert isinstance(platform, VMPlatform)
    assert platform.name == "aws-ec2"


def test_doctor_roster_lists_the_aws_plugin(tmp_path: Path) -> None:
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(c for c in disabled.checks if c.name == "plugin aws")
    assert row.status is Status.INFO
    assert "not enabled in [plugins].system" in (row.message or "")

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row_on = next(c for c in enabled.checks if c.name == "plugin aws")
    assert row_on.status is Status.OK
