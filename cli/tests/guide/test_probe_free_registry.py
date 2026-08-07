from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.bootstrap import build_registry, load_guide_registry
from agentworks.config import load_config
from agentworks.resources import KIND_REGISTRY, Origin, Registry, ResourceReference
from agentworks.resources.graph import HOST_PROBING_CAPABILITY_KINDS, Readiness

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


def test_shared_host_probe_policy_covers_every_current_probing_capability() -> None:
    assert frozenset({"vm-platform", "secret-backend"}) == HOST_PROBING_CAPABILITY_KINDS


def test_probe_free_finalize_preserves_unavailable_validation_and_deferred_materialization(
    monkeypatch,
) -> None:
    validated: list[str] = []

    @dataclass(frozen=True)
    class Source:
        name: str
        origin: Origin | None = None

        def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
            return (ResourceReference("generated", "guide-target", "required target", ("guide-source", self.name)),)

        def not_ready(self, deps: object) -> Readiness:
            raise AssertionError("probe-dependent readiness hook was invoked")

        def validate_config(self, context: object) -> None:
            validated.append(self.name)

    @dataclass(frozen=True)
    class Target:
        name: str
        origin: Origin | None = None

        def dependencies(self, context: object) -> tuple[()]:
            return ()

    class SourceKind:
        kind = "guide-source"
        miss_policy = "error"
        auto_declare_names = None
        category = "declarable"
        description = "Probe-free source."
        builtin_override = "allow"

        def synthesize(self, references: object) -> object:
            raise AssertionError("source is not auto-declared")

    class TargetKind:
        kind = "guide-target"
        miss_policy = "auto-declare"
        auto_declare_names = None
        category = "declarable"
        description = "Deferred target."
        builtin_override = "allow"

        def synthesize(self, references: object) -> Target:
            return Target("generated", Origin.auto_declared(source=("guide-source", "source")))

    monkeypatch.setitem(KIND_REGISTRY, SourceKind.kind, SourceKind())
    monkeypatch.setitem(KIND_REGISTRY, TargetKind.kind, TargetKind())
    registry = Registry.empty()
    registry.add("guide-source", "source", Source("source"), Origin.built_in(source="test"))

    registry.finalize(probe_host_readiness=False)

    assert registry.lookup("guide-target", "generated").name == "generated"
    readiness = registry.graph.readiness_of("guide-source", "source")
    assert not readiness.is_available
    assert validated == ["source"]
