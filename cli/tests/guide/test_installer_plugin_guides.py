"""Boundaries for first-party guide content owned by installer plugins."""

from __future__ import annotations

import importlib
import importlib.resources
import sys

import pytest


def test_installer_plugin_imports_do_not_read_guide_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin registration stays independent of packaged guide Markdown."""

    def forbid_guide_resource_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("plugin import must not read guide resources")

    monkeypatch.setattr(importlib.resources, "files", forbid_guide_resource_io)
    for module in ("agentworks.plugins.apt", "agentworks.plugins.install_command"):
        sys.modules.pop(module, None)
        importlib.import_module(module)
