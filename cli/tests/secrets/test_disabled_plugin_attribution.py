"""Disabled secret-backend plugin attribution through declared sources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.plugins import SYSTEM_PLUGINS
from agentworks.secrets.outcomes import (
    ResolutionDetail,
    format_outcome,
)
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import active_sources
from agentworks.secrets.verification import verify_secrets
from tests.conftest import ManifestDoc, write_cfg


def test_declared_source_accepts_string_subclass_plugin_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PluginName(str):
        pass

    plugin_name = PluginName("Vault.Plugin")
    onepassword = replace(SYSTEM_PLUGINS["onepassword"], name=plugin_name)
    monkeypatch.setattr(
        "agentworks.plugins.SYSTEM_PLUGINS",
        {**SYSTEM_PLUGINS, "onepassword": onepassword},
    )
    config_path = write_cfg(
        tmp_path,
        ManifestDoc(
            "secret-source",
            "work-op",
            {"backend": {"name": "onepassword"}},
        ),
        ManifestDoc(
            "secret",
            "token",
            {"backend_mappings": {"work-op": "op://Work/item/password"}},
            description="token",
        ),
        settings='[secret_config]\nsources = ["work-op"]\n',
    )
    config = load_config(config_path, warn_issues=False)
    registry = build_registry(config)

    (source,) = active_sources(config, registry)

    assert source.disabled_backend_plugin is plugin_name
    (outcome,) = verify_secrets(
        config,
        registry,
        ["token"],
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert outcome.detail is ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED
    assert outcome.remediation_target is plugin_name
    assert format_outcome(outcome).endswith("remediation=enable plugin `Vault.Plugin`")
