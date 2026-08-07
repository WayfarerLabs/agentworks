from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from agentworks.bootstrap import build_registry, load_guide_registry
from agentworks.config import load_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config


def _config(tmp_path: Path) -> Config:
    public_key = tmp_path / "id.pub"
    private_key = tmp_path / "id"
    public_key.write_text("ssh-ed25519 test")
    private_key.write_text("private test key")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[operator]\nssh_public_key = "{public_key}"\nssh_private_key = "{private_key}"\n')
    return load_config(config_path, warn_issues=False)


def test_guide_registry_does_not_call_host_tool_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)

    def denied(command: str) -> str | None:
        raise AssertionError(f"host tool probe called for {command}")

    monkeypatch.setattr(shutil, "which", denied)
    registry = load_guide_registry(config)

    readiness = registry.graph.readiness_of("vm-site", "lima-local")
    assert not readiness.is_available
    assert readiness.reason == "host readiness unavailable: guide does not inspect the workstation"


def test_path_cannot_change_guide_readiness_facts(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    empty_path = tmp_path / "empty"
    fake_path = tmp_path / "fake"
    empty_path.mkdir()
    fake_path.mkdir()
    executable = fake_path / "limactl"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    monkeypatch.setenv("PATH", str(empty_path))
    guide_without_tool = load_guide_registry(config)
    ordinary_without_tool = build_registry(config)
    monkeypatch.setenv("PATH", str(fake_path))
    guide_with_tool = load_guide_registry(config)
    ordinary_with_tool = build_registry(config)

    assert guide_without_tool.graph.readiness_of("vm-site", "lima-local") == guide_with_tool.graph.readiness_of(
        "vm-site", "lima-local"
    )
    assert not ordinary_without_tool.graph.readiness_of("vm-site", "lima-local").is_ready
    assert ordinary_with_tool.graph.readiness_of("vm-site", "lima-local").is_ready
