"""One actual login layout governs settings, plugins, artifacts and MCP roles."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.layout import NATIVE_USER_DIRECTORIES
from agentworks.capabilities.harness_integration.settings import parse_settings, serialize_settings
from agentworks.errors import ExternalError, StateConflictError, StateError, UserAbort
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native_cli import NativeMarket, NativePlugin, identity
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, owned
from tests.artifacts.test_native_mcp import server
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux guest login and file helpers")
TOOLS = [("claude", "ClaudeCodeIntegration"), ("codex", "CodexIntegration"), ("grok", "GrokBuildIntegration")]


@pytest.fixture
def target(tmp_path: Path) -> Iterator[LocalFixtureTransport]:
    root = tmp_path / "layout-guest"
    value = LocalFixtureTransport(root)
    yield value
    shutil.rmtree(root)


def profile(target: LocalFixtureTransport, tool: str, root: Path) -> None:
    variable, _ = NATIVE_USER_DIRECTORIES[tool]
    with (target.home / ".profile").open("a") as file:
        file.write(f"export {variable}={json.dumps(str(root))}\n")


def forbid(*args: object, **kwargs: object) -> None:
    pytest.fail("operation outside this test's native capability boundary")


@pytest.mark.parametrize("tool,class_name", TOOLS)
@pytest.mark.parametrize("newline", [False, True])
def test_profile_banner_does_not_hide_the_observed_native_layout(target, monkeypatch, tool, class_name, newline):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    root = target.home / "profile-root"
    profile(target, tool, root)
    with (target.home / ".profile").open("a") as file:
        file.write("printf 'banner\\n'\n" if newline else "printf banner\n")
    monkeypatch.setattr(module, "probe_native", forbid)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", forbid)
    config = {}
    if tool != "grok":
        source = target.root / "settings-source"
        source.write_bytes(serialize_settings({"selected": "profile"}, format="json" if tool == "claude" else "toml"))
        config = {"settings": {"source": str(source), "strategy": "merge-overwrite"}}
    item = server()
    checkpoints: list[tuple[NativeClaim, ...]] = []
    reports: list[str] = []
    call = replace(
        invocation(target, checkpoints),
        artifacts=received(item) if tool == "grok" else received(),
        report_incomplete=reports.append,
    )
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config=config
    )
    result = integration.user_init(call)
    path = root / ("settings.json" if tool == "claude" else "config.toml")
    assert not reports and not result.deferred and path.is_file()
    document = parse_settings(path.read_bytes(), format="json" if tool == "claude" else "toml")
    if tool == "grok":
        assert checkpoints[-1][0].destination == str(path)
        entries = document["mcp_servers"]
        assert isinstance(entries, dict) and mcp_native_name(item) in entries
    else:
        assert document["selected"] == "profile"


@pytest.mark.parametrize("tool,class_name", TOOLS[:2])
@pytest.mark.parametrize("mapped_environment", [False, True])
def test_settings_keep_profile_root_before_add_withdraw_and_following_reinit(
    target, monkeypatch, tool, class_name, mapped_environment
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    root = target.home / "from-profile"
    profile(target, tool, root)
    source = target.root / "mapping"
    path = root / ("settings.json" if tool == "claude" else "config.toml")
    mcp_path = root / (".claude.json" if tool == "claude" else "config.toml")
    format = "json" if tool == "claude" else "toml"
    variable, default = NATIVE_USER_DIRECTORIES[tool]
    environment = {variable: str(target.home / "from-map")} if mapped_environment else {}
    config = {"settings": {"source": str(source), "strategy": "merge-overwrite"}}
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config=config
    )
    monkeypatch.setattr(module, "probe_native", forbid)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", forbid)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    previous: tuple[NativeClaim, ...] = ()
    item = server()
    for value, inputs in enumerate((received(), received(item), received(), received())):
        source.write_bytes(serialize_settings({"value": value}, format=format))
        call = replace(invocation(target, checkpoints, prior=record(previous, tool), env=environment), artifacts=inputs)
        observed = Mock(wraps=module.user_layout)
        with monkeypatch.context() as patch:
            patch.setattr(module, "user_layout", observed)
            result = integration.user_init(call)
        assert observed.call_count == 1 and not result.files and not result.deferred
        assert parse_settings(path.read_bytes(), format=format)["value"] == value
        previous = checkpoints[-1] if checkpoints else ()
        if value == 1:
            assert previous and previous[0].destination == str(mcp_path)
        if value >= 2:
            assert previous == ()
        assert not (target.home / default).exists() and not (target.home / "from-map").exists()


@pytest.mark.parametrize("tool,class_name", TOOLS)
def test_artifact_discovery_cannot_resample_a_different_native_root(target, monkeypatch, tool, class_name):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    first, later = target.home / "first", target.home / "later"
    profile(target, tool, first)
    # An offline executable permits discovery checks but must never be executed.
    binary = target.root / "bin" / tool
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 91\n")
    binary.chmod(0o700)
    selected = Mock(wraps=module.user_layout)
    probe = module.probe_native
    roots = []

    def check(*args, **kwargs):
        roots.append(kwargs["native_root"])
        profile(target, tool, later)
        return probe(*args, **kwargs)

    monkeypatch.setattr(module, "user_layout", selected)
    monkeypatch.setattr(module, "probe_native", check)
    item = artifact(ArtifactType.AGENT)
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config={}
    )
    result = integration.user_init(replace(invocation(target, []), artifacts=received(item, server())))
    assert selected.call_count == 1 and roots == [str(first), str(first)]
    assert result.files and all(file.path.startswith(str(first) + "/") for file in result.files)
    assert not result.deferred and not later.exists()
    assert (first / (".claude.json" if tool == "claude" else "config.toml")).exists()


@pytest.mark.parametrize("tool,class_name", TOOLS[:2])
@pytest.mark.parametrize("external", [False, True])
def test_plugin_progress_settings_and_mcp_use_the_same_verified_root(target, monkeypatch, tool, class_name, external):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    root = (target.root if external else target.home) / "from-profile"
    root.mkdir()
    profile(target, tool, root)
    path = root / ("settings.json" if tool == "claude" else "config.toml")
    format = "json" if tool == "claude" else "toml"
    source = target.root / "mapping"
    source.write_bytes(serialize_settings({"theme": "mapped"}, format=format))
    market = NativeMarket("fixture", identity({"source_type": "local", "source": "/fixture-market"}))
    installed: list[NativePlugin] = []
    cli = Mock()
    cli.markets.return_value = (market,)
    cli.plugins.side_effect = lambda _markets: tuple(installed)
    cli.available.return_value = ("one@fixture",)

    def install(selector):
        installed.append(NativePlugin(selector, "user", True, identity({"marketplace": market.source})))
        path.write_bytes(serialize_settings({"native_install": True}, format=format))

    cli.install.side_effect = install
    constructor = Mock(return_value=cli)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", constructor)
    monkeypatch.setattr(module, "probe_native", forbid)
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent",
        owner_name="fixture",
        facet="user",
        config={"settings": {"source": str(source), "strategy": "merge-overwrite"}, "plugins": ["one@fixture"]},
    )
    checkpoints: list[tuple[NativeClaim, ...]] = []
    item = server()
    result = integration.user_init(replace(invocation(target, checkpoints), artifacts=received(item)))
    assert constructor.call_args.kwargs["config_root"] == str(root)
    assert cli.install.call_count == 1 and parse_settings(path.read_bytes(), format=format)["theme"] == "mapped"
    assert checkpoints and all(
        claim.destination.startswith(str(root) + "/") or claim.destination == str(root) for claim in checkpoints[-1]
    )
    assert {entry.input_id for entry in result.deferred} == ({item.identity} if external else set())
    mcp_path = root / (".claude.json" if tool == "claude" else "config.toml")
    document = parse_settings(mcp_path.read_bytes(), format=format) if mcp_path.exists() else {}
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    assert (key in document) is not external
    assert not (target.home / NATIVE_USER_DIRECTORIES[tool][1]).exists()


@pytest.mark.parametrize("tool,class_name", TOOLS)
def test_unknown_layout_retains_mcp_claims_and_never_guesses_a_settings_destination(
    target, monkeypatch, tool, class_name
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    item = server()
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(target, checkpoints), artifacts=received(item))
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config={}
    )
    integration.user_init(call)
    prior = record(checkpoints[-1], "codex")
    reports: list[str] = []
    (target.home / ".profile").write_text("exit 7\n")
    monkeypatch.setattr(NativeFiles, "__enter__", forbid)
    monkeypatch.setattr(module, "probe_native", forbid)
    config = {}
    if tool != "grok":
        source = target.root / "mapping"
        source.write_text("unused")
        config = {"settings": {"source": str(source), "strategy": "replace"}, "plugins": ["one@fixture"]}
    changed = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config=config
    )
    result = changed.user_init(replace(call, prior=prior, report_incomplete=reports.append))
    assert reports and {entry.input_id for entry in result.deferred} == {item.identity}
    assert checkpoints[-1] == prior.claims
    retired = getattr(module, class_name).for_setup(owner_kind="agent", owner_name="fixture", facet="user", config=None)
    result = retired.user_init(replace(call, artifacts=received(), prior=prior, report_incomplete=reports.append))
    assert not result.deferred and checkpoints[-1] == prior.claims


def test_unknown_layout_does_not_turn_prior_file_effects_into_an_empty_retirement_plan(target, monkeypatch):
    from agentworks.plugins.codex.harness_integration import CodexIntegration

    item = artifact(ArtifactType.AGENT)
    from agentworks.plugins.codex.artifacts import outer_artifacts

    files = outer_artifacts(
        received(item), skills_root=str(target.home / ".agents/skills"), agents_root=str(target.home / ".codex/agents")
    ).files
    previous = record((), "codex").model_copy(update={"artifact_files": tuple(owned(file) for file in files)})
    (target.home / ".profile").write_text("exit 7\n")
    monkeypatch.setattr(NativeFiles, "__enter__", forbid)
    integration = CodexIntegration.for_setup(owner_kind="agent", owner_name="fixture", facet="user", config={})
    with pytest.raises(ExternalError):
        integration.user_init(replace(invocation(target, [], prior=previous), report_incomplete=Mock()))


@pytest.mark.parametrize(
    "error",
    [
        StateConflictError("persist"),
        StateError("internal"),
        UserAbort("cancel"),
        RuntimeError("bug"),
        KeyboardInterrupt(),
    ],
)
def test_layout_failure_does_not_hide_internal_persistence_or_cancellation_errors(target, monkeypatch, error):
    from agentworks.plugins.codex.harness_integration import CodexIntegration

    monkeypatch.setattr(target, "run", Mock(side_effect=error))
    integration = CodexIntegration.for_setup(owner_kind="agent", owner_name="fixture", facet="user", config={})
    with pytest.raises(type(error)):
        integration.user_init(replace(invocation(target, []), artifacts=received(server())))


@pytest.mark.parametrize("tool,class_name", TOOLS)
def test_current_root_change_retains_old_claims_without_opening_the_recorded_role(
    target, monkeypatch, tool, class_name
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    checkpoints: list[tuple[NativeClaim, ...]] = []
    item = server()
    call = replace(invocation(target, checkpoints), artifacts=received(item))
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config={}
    )
    integration.user_init(call)
    old = checkpoints[-1]
    path = Path(old[0].destination)
    before = path.read_bytes()
    profile(target, tool, target.home / "next-root")
    retired = getattr(module, class_name).for_setup(owner_kind="agent", owner_name="fixture", facet="user", config=None)
    monkeypatch.setattr(NativeFiles, "__enter__", forbid)
    reports: list[str] = []
    result = retired.user_init(
        replace(call, artifacts=received(), prior=record(old, "codex"), report_incomplete=reports.append)
    )
    assert reports and not result.files and checkpoints[-1] == old and path.read_bytes() == before
    assert mcp_native_name(item) in before.decode()


@pytest.mark.parametrize("tool,class_name", TOOLS[:2])
@pytest.mark.parametrize("unknown", [False, True])
def test_plugin_retirement_keeps_recorded_root_policy_and_separate_current_mcp_authority(
    target, monkeypatch, tool, class_name, unknown
):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    checkpoints: list[tuple[NativeClaim, ...]] = []
    item = server()
    call = replace(invocation(target, checkpoints), artifacts=received(item))
    integration = getattr(module, class_name).for_setup(
        owner_kind="agent", owner_name="fixture", facet="user", config={}
    )
    integration.user_init(call)
    mcp = checkpoints[-1][0]
    path = Path(mcp.destination)
    before = path.read_bytes()
    _, default = NATIVE_USER_DIRECTORIES[tool]
    root = target.home / default
    plugin = NativeClaim(role="plugin", identifier="absent@fixture", destination=str(root), source="fixture")
    previous = record((mcp, plugin), tool)
    if unknown:
        (target.home / ".profile").write_text("exit 7\n")
    else:
        profile(target, tool, target.home / "next-root")
    cli = Mock()
    cli.markets.return_value = ()
    cli.plugins.return_value = ()
    constructor = Mock(return_value=cli)
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", constructor)
    monkeypatch.setattr(module, "probe_native", forbid)
    retired = getattr(module, class_name).for_setup(owner_kind="agent", owner_name="fixture", facet="user", config=None)
    reports: list[str] = []
    retired.user_init(replace(call, artifacts=received(), prior=previous, report_incomplete=reports.append))
    assert constructor.call_args.kwargs["config_root"] == str(root)
    assert reports and checkpoints[-1] == (mcp,) and path.read_bytes() == before
