"""Boundaries for first-party guide content owned by installer plugins."""

from __future__ import annotations

import importlib
import importlib.resources
import sys

import pytest

from agentworks.guide import (
    ActionList,
    ConsentBoundary,
)
from agentworks.guide import service as guide_service


def test_installer_plugin_imports_are_guide_io_free_and_import_order_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin registration is I/O-free and guide submodules cannot shadow loaders."""

    def forbid_guide_resource_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("plugin import must not read guide resources")

    monkeypatch.setattr(importlib.resources, "files", forbid_guide_resource_io)
    for module in ("agentworks.plugins.apt", "agentworks.plugins.install_command"):
        sys.modules.pop(module, None)
        importlib.import_module(module)

    monkeypatch.undo()
    for module in (
        "agentworks.plugins.apt.guide_contributions",
        "agentworks.plugins.install_command.guide_contributions",
    ):
        importlib.import_module(module)
    importlib.reload(guide_service)

    catalog = guide_service.build_authored_catalog()
    assert {"plugin/apt/overview", "plugin/install-command/overview"} <= set(catalog.names())


def test_installer_plugin_verification_actions_are_manual_config_reads() -> None:
    apt_contributions = importlib.import_module("agentworks.plugins.apt.guide_contributions").guide_contributions
    install_command_contributions = importlib.import_module(
        "agentworks.plugins.install_command.guide_contributions"
    ).guide_contributions

    for contribution in (*apt_contributions(), *install_command_contributions()):
        actions = next(block.actions for block in contribution.blocks if isinstance(block, ActionList))
        verification = next(action for action in actions if str(action.id).startswith("verify-"))
        assert verification.consent is ConsentBoundary.READ_CONFIGURED_STATE
        assert verification.command is None
        assert verification.manual_steps is not None
        assert [(item.name, item.required, item.sensitive) for item in verification.required_inputs] == [
            ("CONFIG_PATH", True, False)
        ]
