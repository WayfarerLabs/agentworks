"""Native observations preserve identities without adopting unrelated entries."""

from __future__ import annotations

from typing import cast

import pytest

from agentworks.capabilities.harness_integration.native_cli import NativeCLI, identity
from agentworks.capabilities.harness_integration.native_files import NativeFiles


class InventoryFiles:
    def read(self, path: str) -> bytes:
        return b'[marketplaces.local]\nsource_type="local"\nsource="/market"\nref="release"\n'


def test_codex_inventory_preserves_unknown_sources_and_complete_registered_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = NativeCLI("codex", cast(NativeFiles, InventoryFiles()), home="/user", config_root="/user/.codex")
    markets = {
        "marketplaces": [
            {"name": "builtin"},
            {
                "name": "local",
                "marketplaceSource": {"sourceType": "local", "source": "/market"},
            },
        ]
    }
    plugins = {
        "installed": [
            {"pluginId": "remote@cloud", "enabled": True},
            {"pluginId": "bundled@builtin", "enabled": True},
            {"pluginId": "one@local", "version": "3", "enabled": False},
        ]
    }
    monkeypatch.setattr(cli, "command", lambda args, **kwargs: markets if "marketplace" in args else plugins)
    observed = cli.markets()
    assert observed[0].source is None
    registered = identity({"source_type": "local", "source": "/market", "ref": "release"})
    assert observed[1].source == registered
    installed = cli.plugins(observed)
    assert [item.source for item in installed] == [
        None,
        None,
        identity({"marketplace": registered, "version": "3"}),
    ]
    assert [item.identifier for item in installed] == ["remote@cloud", "bundled@builtin", "one@local"]
    assert installed[-1].enabled is False

    plugins["installed"][-1]["version"] = "4"
    assert cli.plugins(observed)[-1].source == identity({"marketplace": registered, "version": "4"})
