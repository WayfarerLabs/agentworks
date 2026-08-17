"""The opt-in Grok Build plugin's capability and manifest surfaces."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.resources.access import ensure_recipe_enabled
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import list_resources
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.config import Config


def _config(
    tmp_path: Path,
    *,
    enabled: bool = False,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Config:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["grok"]\n\n' if enabled else ""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + plugins
    )
    if manifests:
        write_manifests(tmp_path, *manifests)
    return load_config(config_path, warn_issues=False)


def test_grok_build_is_seated_by_the_grok_plugin() -> None:
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "grok" in SYSTEM_PLUGINS
    assert "grok-build" in HARNESS_INTEGRATION_REGISTRY


def test_harness_integration_is_disabled_by_default(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("harness-integration", "grok-build")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "grok"
    assert registry.graph.enablement_of("harness-integration", "grok-build") is Enablement.disabled


def test_installer_is_disabled_by_default(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("user-install-command", "grok")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "grok"
    assert row.command == "curl -fsSL https://x.ai/cli/install.sh | bash"
    assert row.path == ["~/.grok/bin"]
    assert row.test_exec == "grok"
    assert registry.graph.enablement_of("user-install-command", "grok") is Enablement.disabled


def test_disabled_rows_are_hidden_from_default_list(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    default_rows = {(row.kind, row.name) for row in list_resources(registry).rows}
    assert ("harness-integration", "grok-build") not in default_rows
    assert ("user-install-command", "grok") not in default_rows


_GROK_TEMPLATE = ManifestDoc(
    "session-template",
    "gk",
    {"harness_integration": {"name": "grok-build"}},
    description="Grok Build session",
)


def test_disabled_harness_reference_finalizes_and_stays_ready(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, manifests=[_GROK_TEMPLATE]))
    assert registry.graph.is_ready("session-template", "gk")


def test_disabled_harness_is_refused_with_enable_hint(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, manifests=[_GROK_TEMPLATE]))
    with pytest.raises(StateError):
        ensure_harness_integration_enabled(registry, "grok-build")


def test_enabling_plugin_makes_harness_usable(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, enabled=True, manifests=[_GROK_TEMPLATE]))
    assert registry.graph.enablement_of("harness-integration", "grok-build") is Enablement.enabled
    ensure_harness_integration_enabled(registry, "grok-build")


_GROK_INSTALL_TEMPLATE = ManifestDoc("agent-template", "default", {"user_install_commands": ["grok"]})


def test_disabled_installer_reference_finalizes(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, manifests=[_GROK_INSTALL_TEMPLATE]))
    assert registry.graph.enablement_of("user-install-command", "grok") is Enablement.disabled


def test_disabled_installer_is_refused_with_enable_hint(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, manifests=[_GROK_INSTALL_TEMPLATE]))
    with pytest.raises(StateError):
        ensure_recipe_enabled(registry, "agent-template", "default")


def test_enabling_plugin_makes_installer_usable(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, enabled=True, manifests=[_GROK_INSTALL_TEMPLATE]))
    assert registry.graph.enablement_of("user-install-command", "grok") is Enablement.enabled
    ensure_recipe_enabled(registry, "agent-template", "default")


def test_operator_installer_override_wins(tmp_path: Path) -> None:
    registry = build_registry(
        _config(
            tmp_path,
            manifests=[
                ManifestDoc(
                    "user-install-command",
                    "grok",
                    {"command": "echo operator-grok"},
                    description="operator Grok installer",
                )
            ],
        )
    )
    row = registry.lookup("user-install-command", "grok")
    assert row.origin.variant == "operator-declared"
    assert row.command == "echo operator-grok"


def test_doctor_roster_lists_grok(tmp_path: Path) -> None:
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(check for check in disabled.checks if check.name == "plugin grok")
    assert row.status is Status.INFO

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row = next(check for check in enabled.checks if check.name == "plugin grok")
    assert row.status is Status.OK
