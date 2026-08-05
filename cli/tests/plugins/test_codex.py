"""The ``codex`` system plugin: the opt-in bundle of the ``codex`` harness integration
and the ``codex`` install-command, built on the ``claude`` plugin's paved
road.

Drives ``build_registry`` on real config (no fixture plugin injected via
``SYSTEM_PLUGINS``) and pins both halves of the bundle:

- the ``codex`` HARNESS INTEGRATION: present-but-disabled with a ``system-plugin``
  origin, a ``session-template`` naming it stays ready, and
  ``ensure_harness_integration_enabled`` refuses it at use until ``[plugins] system``;
- the ``codex`` INSTALL-COMMAND (a bundled ``user-install-command``):
  present-but-disabled (weak), so a template's ``user_install_commands =
  ["codex"]`` finalizes cleanly (never an unknown-name error) and is
  refused at use by the recipe gate with the enable hint until enabled.

Enabling ``[plugins] system = ["codex"]`` makes both consumable.
"""

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
from agentworks.resources.inspect import describe_resource, list_resources
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
    """A real operator config; ``enabled`` toggles ``[plugins] system =
    ["codex"]`` and ``manifests`` seeds resource declarations beside it."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["codex"]\n\n' if enabled else ""
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
    return load_config(cfg, warn_issues=False)


# -- seating + the present-but-disabled rows ---------------------------------


def test_codex_seated_by_plugin() -> None:
    """The codex harness integration ships as the ``codex`` system plugin, whose
    adapter re-seats the integration class into the code registry at import (so
    the resolver can stamp it onto the graph node), and the plugin is
    indexed."""
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "codex" in SYSTEM_PLUGINS
    assert "codex" in HARNESS_INTEGRATION_REGISTRY


def test_harness_integration_row_is_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    """The ``codex`` harness integration row publishes present-but-disabled with a
    ``system-plugin`` origin until the operator opts in."""
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("harness-integration", "codex")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "codex"
    assert registry.graph.enablement_of("harness-integration", "codex") is Enablement.disabled


def test_install_command_row_is_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    """The ``codex`` user-install-command ships (present-but-disabled)
    from the plugin bundle."""
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("user-install-command", "codex")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "codex"
    # CODEX_NON_INTERACTIVE prefixes sh (where the script runs), not curl:
    # the script prompts via /dev/tty when a TTY exists (Windows controllers
    # force one on provisioning transports), and provisioning must never
    # let an installer prompt.
    assert row.command == "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh"
    assert registry.graph.enablement_of("user-install-command", "codex") is Enablement.disabled


def test_disabled_rows_hidden_from_list_shown_by_describe(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    default_rows = {(r.kind, r.name) for r in list_resources(registry).rows}
    assert ("harness-integration", "codex") not in default_rows
    assert ("user-install-command", "codex") not in default_rows

    for kind, name in (("harness-integration", "codex"), ("user-install-command", "codex")):
        desc = describe_resource(registry, kind, name)
        assert desc.disabled_reason is not None
        assert "codex" in desc.disabled_reason


# -- the harness integration use-gate ----------------------------------------

_CODEX_TEMPLATE = ManifestDoc(
    "session-template",
    "cx",
    {"harness_integration": {"name": "codex"}},
    description="Codex session",
)


def test_session_template_naming_disabled_harness_integration_finalizes_and_stays_ready(tmp_path: Path) -> None:
    """A ``session-template`` naming ``codex`` finalizes cleanly (the
    reference lands on the present-but-disabled row, never an unknown-name
    error) and stays ready: the harness integration's disablement does not propagate
    to the template."""
    registry = build_registry(_config(tmp_path, manifests=[_CODEX_TEMPLATE]))
    assert registry.graph.is_ready("session-template", "cx")


def test_ensure_harness_integration_enabled_refuses_disabled_codex_with_hint(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, manifests=[_CODEX_TEMPLATE]))
    with pytest.raises(StateError) as exc:
        ensure_harness_integration_enabled(registry, "codex")
    assert "enable plugin `codex`" in str(exc.value)


def test_enabling_codex_lets_the_harness_integration_be_used(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, enabled=True, manifests=[_CODEX_TEMPLATE]))
    assert registry.graph.enablement_of("harness-integration", "codex") is Enablement.enabled
    ensure_harness_integration_enabled(registry, "codex")  # no raise


# -- the install-command recipe use-gate --------------------------------------

_CODEX_INSTALL_TEMPLATE = ManifestDoc("agent-template", "default", {"user_install_commands": ["codex"]})


def test_template_referencing_codex_install_finalizes_when_disabled(tmp_path: Path) -> None:
    """A template's ``user_install_commands = ["codex"]`` finalizes
    cleanly while codex is not enabled: the row is present-but-disabled,
    so the reference is valid, never an unknown-name error."""
    registry = build_registry(_config(tmp_path, manifests=[_CODEX_INSTALL_TEMPLATE]))
    assert registry.graph.enablement_of("user-install-command", "codex") is Enablement.disabled


def test_recipe_gate_refuses_disabled_codex_install_with_hint(tmp_path: Path) -> None:
    """The recipe gate refuses an (enabled) template whose closure draws on
    the disabled ``codex`` install-command, naming the disabled
    contribution and the plugin to enable, before any transport work."""
    registry = build_registry(_config(tmp_path, manifests=[_CODEX_INSTALL_TEMPLATE]))
    with pytest.raises(StateError) as exc:
        ensure_recipe_enabled(registry, "agent-template", "default")
    message = str(exc.value)
    assert "codex" in message
    assert "enable plugin `codex`" in message


def test_enabling_codex_lets_the_install_command_be_consumed(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, enabled=True, manifests=[_CODEX_INSTALL_TEMPLATE]))
    assert registry.graph.enablement_of("user-install-command", "codex") is Enablement.enabled
    ensure_recipe_enabled(registry, "agent-template", "default")  # no raise


def test_operator_override_of_codex_install_wins(tmp_path: Path) -> None:
    """An operator who declares their own ``codex`` user-install-command
    overrides the disabled plugin row with no collision error (the plugin
    row publishes weak while disabled)."""
    registry = build_registry(
        _config(
            tmp_path,
            manifests=[
                ManifestDoc(
                    "user-install-command",
                    "codex",
                    {"command": "echo operator-codex"},
                    description="operator codex installer",
                )
            ],
        )
    )
    row = registry.lookup("user-install-command", "codex")
    assert row.origin.variant == "operator-declared"
    assert row.command == "echo operator-codex"


# -- the doctor roster ---------------------------------------------------------


def test_doctor_roster_lists_the_codex_plugin(tmp_path: Path) -> None:
    """``agw doctor``'s System plugins roster lists codex, disabled by
    default and enabled once opted in (the discovery surface the enable
    hint points at)."""
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(c for c in disabled.checks if c.name == "plugin codex")
    assert row.status is Status.INFO
    assert "not enabled in [plugins].system" in (row.message or "")

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row_on = next(c for c in enabled.checks if c.name == "plugin codex")
    assert row_on.status is Status.OK
