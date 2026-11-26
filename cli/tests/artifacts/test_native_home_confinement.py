"""Each account's native tool homes lie inside its own physical HOME, or user setup writes nothing."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.layout import (
    NATIVE_USER_DIRECTORIES,
    NativeRootNotBeneathHomeError,
    resolve_user_layout,
)
from agentworks.capabilities.harness_integration.settings import serialize_settings
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native_cli import NativeMarket, NativePlugin, identity
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact
from tests.artifacts.test_native_mcp import server
from tests.native_setup_fixtures import LocalFixtureAccount, forbid_files, invocation, native_cli_double, record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux guest login and file helpers")
TOOLS = [("claude", "ClaudeCodeIntegration"), ("codex", "CodexIntegration"), ("grok", "GrokBuildIntegration")]


@pytest.fixture
def target(tmp_path: Path) -> Iterator[LocalFixtureAccount]:
    root = tmp_path / "confinement-guest"
    value = LocalFixtureAccount(root)
    yield value
    shutil.rmtree(root)


def profile(target: LocalFixtureAccount, tool: str, root: Path) -> None:
    variable, _ = NATIVE_USER_DIRECTORIES[tool]
    with (target.home / ".profile").open("a") as file:
        file.write(f"export {variable}='{root}'\n")


def default_root(target: LocalFixtureAccount, tool: str) -> Path:
    """The root the fixture's login selects without a profile override."""
    variable, default = NATIVE_USER_DIRECTORIES[tool]
    return Path(target.environment.get(variable, str(target.home / default)))


def forbid(*args: object, **kwargs: object) -> None:
    pytest.fail("a native home outside HOME was opened")


@pytest.mark.parametrize("tool,class_name", TOOLS)
@pytest.mark.parametrize("spelling", ["override", "linked"])
def test_a_root_outside_home_leaves_setup_incomplete_and_writes_nothing(
    target, monkeypatch, tool, class_name, spelling
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    shared = target.root / "shared"
    shared.mkdir()
    if spelling == "override":
        root = shared
        profile(target, tool, shared)
    else:
        root = default_root(target, tool)
        root.symlink_to(shared)
    config = {}
    if tool != "grok":
        source = target.root / "mapping"
        source.write_bytes(serialize_settings({"theme": "mapped"}, format="json" if tool == "claude" else "toml"))
        config = {"settings": {"source": str(source), "strategy": "merge-overwrite"}, "plugins": ["one@fixture"]}
    forbid_files(monkeypatch)
    monkeypatch.setattr(module, "probe_native", forbid)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", forbid)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    reports: list[str] = []
    call = replace(
        invocation(target, checkpoints),
        artifacts=received(artifact(ArtifactType.AGENT), server()),
        report_incomplete=reports.append,
    )
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config=config
    )
    result = integration.user_init(call)
    assert len(reports) == 1 and str(root) in reports[0]
    assert not result.files and not checkpoints and not any(shared.iterdir())


@pytest.mark.parametrize("tool", [tool for tool, _ in TOOLS])
@pytest.mark.parametrize("case", ["linked inside", "home linked", "home linked, root spelled physically"])
def test_a_root_inside_the_physical_home_is_accepted(target, tool, case):
    home = target.home
    if case == "linked inside":
        root = default_root(target, tool)
        (home / ".dotfiles" / tool).mkdir(parents=True)
        root.symlink_to(home / ".dotfiles" / tool)
    else:
        home = target.root / "home-link"
        home.symlink_to(target.home)
        target.environment["HOME"] = str(home)
        root = (home if case == "home linked" else target.home) / "native"
        profile(target, tool, root)
    layout = resolve_user_layout(tool, target.access(), home=str(home), environment={})
    assert layout.root == str(root)


@pytest.mark.parametrize("tool", [tool for tool, _ in TOOLS])
@pytest.mark.parametrize("case", ["home linked, root outside", "root is HOME itself"])
def test_a_root_not_beneath_the_physical_home_is_refused(target, tool, case):
    home = target.home
    if case == "root is HOME itself":
        root = home
    else:
        home = target.root / "home-link"
        home.symlink_to(target.home)
        target.environment["HOME"] = str(home)
        root = target.root / "native"
    profile(target, tool, root)
    with pytest.raises(NativeRootNotBeneathHomeError, match=str(root)):
        resolve_user_layout(tool, target.access(), home=str(home), environment={})


def _outside_claims(destination: Path) -> tuple[NativeClaim, ...]:
    return (
        NativeClaim(role="marketplace", identifier="old", destination=str(destination), source="old"),
        NativeClaim(role="plugin", identifier="kept@old", destination=str(destination), source="old"),
    )


@pytest.mark.parametrize("tool,class_name", TOOLS[:2])
def test_claims_recorded_outside_home_are_dropped_and_setup_proceeds_with_the_current_root(
    target, monkeypatch, capsys, tool, class_name
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    shared = target.root / "shared"
    shared.mkdir()
    root = default_root(target, tool)
    root.mkdir()
    market = NativeMarket("fixture", identity({"source_type": "local", "source": "/fixture-market"}))
    installed: list[NativePlugin] = []
    cli = native_cli_double()
    cli.markets.return_value = (market,)
    cli.plugins.side_effect = lambda _markets: tuple(installed)
    cli.available.return_value = ("one@fixture",)
    cli.install.side_effect = lambda selector: installed.append(
        NativePlugin(selector, "user", True, identity({"marketplace": market.source}))
    )
    constructor = Mock(return_value=cli)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", constructor)
    monkeypatch.setattr(module, "probe_native", forbid)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = invocation(target, checkpoints, prior=record(_outside_claims(shared), tool))
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config={"plugins": ["one@fixture"]}
    )
    integration.user_init(call)
    assert str(shared) in capsys.readouterr().err
    assert [kwargs["config_root"] for _, kwargs in constructor.call_args_list] == [str(root)]
    assert cli.install.call_count == 1 and not any(shared.iterdir())
    assert [(claim.role, claim.destination) for claim in checkpoints[-1]] == [("plugin", str(root))]


@pytest.mark.parametrize("tool,class_name", TOOLS[:2])
@pytest.mark.parametrize("current", ["inside", "refused"])
def test_retiring_drops_claims_recorded_outside_home_without_reconciling_them(
    target, monkeypatch, capsys, tool, class_name, current
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    shared = target.root / "shared"
    shared.mkdir()
    recorded = shared
    if current == "refused":
        # The recorded root is the current one, spelled inside HOME but linked outside it.
        recorded = default_root(target, tool)
        recorded.symlink_to(shared)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", forbid)
    monkeypatch.setattr(module, "probe_native", forbid)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = invocation(target, checkpoints, prior=record(_outside_claims(recorded), tool))
    retired = getattr(module, class_name).for_setup(owner_kind="agent", owner_name="fixture", facet="user", config=None)
    retired.user_init(replace(call, report_incomplete=Mock()))
    assert str(recorded) in capsys.readouterr().err
    assert checkpoints[-1] == () and not any(shared.iterdir())
