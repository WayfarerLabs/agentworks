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
from unittest.mock import MagicMock

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.resources.access import ensure_recipe_enabled
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import describe_resource, list_resources
from agentworks.vms.initializer import _run_install_commands

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

_AWS_CLI_TEMPLATE = """
apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: aws-tools
spec:
  system_install_commands: [aws-cli]
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


def test_aws_seated_by_vendor_bundle() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "aws" in SYSTEM_PLUGINS
    assert "aws-ec2" in VM_PLATFORM_REGISTRY
    plugin = SYSTEM_PLUGINS["aws"]
    assert set(plugin.capabilities) == {"vm-platform"}
    assert plugin.manifests == "agentworks.plugins.aws"


def test_aws_ec2_row_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("vm-platform", "aws-ec2")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "aws"
    assert registry.graph.enablement_of("vm-platform", "aws-ec2") is Enablement.disabled


def test_aws_bundle_publishes_cli_disabled_with_verified_v2_payload(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("system-install-command", "aws-cli")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "aws"
    assert registry.graph.enablement_of("system-install-command", "aws-cli") is Enablement.disabled
    assert row.test_exec == "/usr/local/bin/aws"
    assert row.test_file == "/usr/local/aws-cli/.agentworks-v2-complete"
    managed_check = 'if test -f "$completion_marker" && test -x "$public_launcher" && test -x "$managed_binary"; then'
    marker_removal = 'sudo rm -f -- "$completion_marker"'
    postcondition = 'if ! test -x "$public_launcher" || ! test -x "$managed_binary"; then'
    marker_write = 'sudo install -m 0644 /dev/null "$completion_marker"'
    assert managed_check in row.command
    assert marker_removal in row.command
    assert postcondition in row.command
    assert marker_write in row.command
    assert row.command.index(managed_check) < row.command.index(marker_removal) < row.command.index("command -v aws")
    assert (
        row.command.index('sudo "$temp_root/aws/install"')
        < row.command.index(postcondition)
        < row.command.index(marker_write)
    )
    assert "aws-cli/2" in row.command
    assert "awscli-exe-linux-x86_64.zip" in row.command
    assert "awscli-exe-linux-aarch64.zip" in row.command
    assert "FB5DB77FD5C118B80511ADA8A6310ACC4672475C" in row.command
    assert "gpg --batch --verify" in row.command
    assert 'primary && $1 == "fpr"' in row.command
    assert "trap 'rm -rf \"$temp_root\"' EXIT" in row.command
    assert "trap 'exit 130' INT" in row.command
    assert 'if test -e "$install_dir" || test -L "$install_dir"; then' in row.command
    assert 'if test "$managed_update" -eq 1; then' in row.command
    assert 'sudo "$temp_root/aws/install"' in row.command
    assert '--install-dir "$install_dir" --bin-dir "$bin_dir" --update' in row.command
    assert "aws configure" not in row.command


def test_completed_managed_aws_cli_uses_short_runner_predicate_only(tmp_path: Path) -> None:
    row = build_registry(_config(tmp_path)).lookup("system-install-command", "aws-cli")
    target = MagicMock()
    target.run.return_value = MagicMock(returncode=0)
    logger = MagicMock()

    paths = _run_install_commands(
        target,
        ["aws-cli"],
        {"aws-cli": row},
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert paths == []
    [predicate] = target.run.call_args_list
    assert predicate.args[0] == ("test -x /usr/local/bin/aws && test -f /usr/local/aws-cli/.agentworks-v2-complete")
    assert predicate.kwargs == {"check": False, "timeout": 10}
    assert all(call.kwargs.get("timeout") != 120 for call in target.run.call_args_list)


def test_aws_cli_recipe_is_gated_until_aws_is_enabled(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, _AWS_CLI_TEMPLATE))
    with pytest.raises(StateError, match="enable plugin `aws`"):
        ensure_recipe_enabled(registry, "vm-template", "aws-tools")

    enabled = build_registry(_config(tmp_path, _AWS_CLI_TEMPLATE, enabled=True))
    ensure_recipe_enabled(enabled, "vm-template", "aws-tools")


def test_operator_aws_cli_override_wins_while_aws_is_disabled(tmp_path: Path) -> None:
    override = """
apiVersion: agentworks/v1
kind: system-install-command
metadata:
  name: aws-cli
spec:
  command: echo operator-aws
"""
    row = build_registry(_config(tmp_path, override)).lookup("system-install-command", "aws-cli")
    assert row.origin.variant == "operator-declared"
    assert row.command == "echo operator-aws"


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


def test_aws_cli_is_discoverable_from_registry_surfaces(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    assert "aws-cli" not in {row.name for row in list_resources(registry).rows}
    assert "aws-cli" in {
        row.name for row in list_resources(registry, include_disabled=True).rows if row.kind == "system-install-command"
    }


def test_doctor_roster_lists_the_aws_plugin(tmp_path: Path) -> None:
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(c for c in disabled.checks if c.name == "plugin aws")
    assert row.status is Status.INFO
    assert "not enabled in [plugins].system" in (row.message or "")

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row_on = next(c for c in enabled.checks if c.name == "plugin aws")
    assert row_on.status is Status.OK
