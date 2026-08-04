"""The ``claude`` system plugin: the opt-in migration of the ``claude-code``
harness and the ``claude`` install-command out of the core (Phase 9, R11 /
R11.1).

The first manifest-carrying migration, so this is the first REAL-plugin
end-to-end exercise of Phase 7's manifest present-but-disabled parity: it
drives ``build_registry`` on real config (no fixture plugin injected via
``SYSTEM_PLUGINS``) and pins both halves of the bundle:

- the ``claude-code`` HARNESS: present-but-disabled with a ``system-plugin``
  origin, a ``session-template`` naming it stays ready, and
  ``ensure_harness_enabled`` refuses it at use until ``[plugins] system``;
- the ``claude`` INSTALL-COMMAND (a bundled ``user-install-command``):
  present-but-disabled (weak), so a template's ``user_install_commands =
  ["claude"]`` finalizes cleanly (never an unknown-name error) and is refused
  at use by the recipe gate with the enable hint until enabled.

Enabling ``[plugins] system = ["claude"]`` makes both consumable.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness import ensure_harness_enabled
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.resources.access import ensure_recipe_enabled
from agentworks.resources.graph import Enablement
from agentworks.resources.inspect import describe_resource, list_resources

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config


def _config(tmp_path: Path, body: str = "", *, enabled: bool = False) -> Config:
    """A real operator config; ``enabled`` toggles ``[plugins] system =
    ["claude"]`` and ``body`` appends resource declarations."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    plugins = '[plugins]\nsystem = ["claude"]\n\n' if enabled else ""
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
    return load_config(cfg, warn_issues=False)


# -- seating + the present-but-disabled rows ---------------------------------


def test_claude_seated_by_plugin() -> None:
    """The claude-code harness ships as the ``claude`` system plugin, whose
    adapter re-seats the harness class into the code registry at import (so the
    resolver can stamp it onto the graph node), and the plugin is indexed."""
    from agentworks.capabilities.harness import HARNESS_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "claude" in SYSTEM_PLUGINS
    assert "claude-code" in HARNESS_REGISTRY


def test_harness_row_is_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    """The ``claude-code`` harness row publishes present-but-disabled with a
    ``system-plugin`` origin until the operator opts in (no longer a
    built-in)."""
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("harness-integration", "claude-code")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "claude"
    assert registry.graph.enablement_of("harness-integration", "claude-code") is Enablement.disabled


def test_install_command_row_is_disabled_system_plugin_by_default(tmp_path: Path) -> None:
    """The ``claude`` user-install-command now ships (present-but-disabled)
    from the plugin bundle, gone from the built-in bundle."""
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("user-install-command", "claude")
    assert row.origin.variant == "system-plugin"
    assert row.origin.plugin == "claude"
    assert row.command == "curl -fsSL https://claude.ai/install.sh | bash"
    assert registry.graph.enablement_of("user-install-command", "claude") is Enablement.disabled


def test_shell_stays_the_default_builtin_harness(tmp_path: Path) -> None:
    """The common session path is untouched: ``shell`` remains a built-in,
    enabled harness after the migration."""
    registry = build_registry(_config(tmp_path))
    shell = registry.lookup("harness-integration", "shell")
    assert shell.origin.variant == "built-in"
    assert registry.graph.enablement_of("harness-integration", "shell") is Enablement.enabled


def test_disabled_rows_hidden_from_list_shown_by_describe(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    default_rows = {(r.kind, r.name) for r in list_resources(registry).rows}
    assert ("harness-integration", "claude-code") not in default_rows
    assert ("user-install-command", "claude") not in default_rows

    for kind, name in (("harness-integration", "claude-code"), ("user-install-command", "claude")):
        desc = describe_resource(registry, kind, name)
        assert desc.disabled_reason is not None
        assert "claude" in desc.disabled_reason


# -- the harness use-gate (R14, the secret model) ----------------------------

_CC_TEMPLATE = """
[session_templates.cc]
harness = "claude-code"
description = "Claude Code session"
"""


def test_session_template_naming_disabled_harness_finalizes_and_stays_ready(tmp_path: Path) -> None:
    """A ``session-template`` naming ``claude-code`` finalizes cleanly (the
    reference lands on the present-but-disabled row, never an unknown-name
    error) and stays ready: the harness's disablement does not propagate to the
    template (mirroring how a secret stays ready while its backends are gated)."""
    registry = build_registry(_config(tmp_path, _CC_TEMPLATE))
    assert registry.graph.is_ready("session-template", "cc")


def test_ensure_harness_enabled_refuses_disabled_claude_code_with_hint(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, _CC_TEMPLATE))
    with pytest.raises(StateError) as exc:
        ensure_harness_enabled(registry, "claude-code")
    assert "enable plugin `claude`" in str(exc.value)


def test_enabling_claude_lets_the_harness_be_used(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, _CC_TEMPLATE, enabled=True))
    assert registry.graph.enablement_of("harness-integration", "claude-code") is Enablement.enabled
    ensure_harness_enabled(registry, "claude-code")  # no raise


# -- the install-command recipe use-gate (Phase 7 manifest parity) -----------

_CLAUDE_INSTALL_TEMPLATE = """
[agent_templates.default]
user_install_commands = ["claude"]
"""


def test_template_referencing_claude_install_finalizes_when_disabled(tmp_path: Path) -> None:
    """The parity crux: a template's ``user_install_commands = ["claude"]``
    finalizes cleanly while claude is not enabled. Before the migration an
    unknown name here was a hard ``references unknown user-install-command``
    error; now the row is present-but-disabled, so the reference is valid."""
    registry = build_registry(_config(tmp_path, _CLAUDE_INSTALL_TEMPLATE))
    assert registry.graph.enablement_of("user-install-command", "claude") is Enablement.disabled


def test_recipe_gate_refuses_disabled_claude_install_with_hint(tmp_path: Path) -> None:
    """The recipe gate refuses an (enabled) template whose closure draws on the
    disabled ``claude`` install-command, naming the disabled contribution and
    the plugin to enable, before any transport work."""
    registry = build_registry(_config(tmp_path, _CLAUDE_INSTALL_TEMPLATE))
    with pytest.raises(StateError) as exc:
        ensure_recipe_enabled(registry, "agent-template", "default")
    message = str(exc.value)
    assert "claude" in message
    assert "enable plugin `claude`" in message


def test_enabling_claude_lets_the_install_command_be_consumed(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path, _CLAUDE_INSTALL_TEMPLATE, enabled=True))
    assert registry.graph.enablement_of("user-install-command", "claude") is Enablement.enabled
    ensure_recipe_enabled(registry, "agent-template", "default")  # no raise


def test_operator_override_of_claude_install_wins(tmp_path: Path) -> None:
    """An operator who declares their own ``claude`` user-install-command
    overrides the disabled plugin row with no collision error (the plugin row
    publishes weak while disabled)."""
    body = """
    [user_install_commands.claude]
    description = "operator claude installer"
    command = "echo operator-claude"
    """
    registry = build_registry(_config(tmp_path, body))
    row = registry.lookup("user-install-command", "claude")
    assert row.origin.variant == "operator-declared"
    assert row.command == "echo operator-claude"


# -- the doctor roster -------------------------------------------------------


def test_doctor_roster_lists_the_claude_plugin(tmp_path: Path) -> None:
    """``agw doctor``'s System plugins roster lists claude, disabled by default
    and enabled once opted in (the discovery surface the enable hint points
    at)."""
    from agentworks.doctor import Status, _check_plugins

    disabled = _check_plugins(_config(tmp_path))
    row = next(c for c in disabled.checks if c.name == "plugin claude")
    assert row.status is Status.INFO
    assert "not enabled in [plugins].system" in (row.message or "")

    enabled = _check_plugins(_config(tmp_path, enabled=True))
    row_on = next(c for c in enabled.checks if c.name == "plugin claude")
    assert row_on.status is Status.OK
