"""Bounded fixed-role MCP inspection protects optional invocation carriers."""

from __future__ import annotations

import json
import sys
from dataclasses import replace

import pytest

from agentworks.artifacts.model import ArtifactOrigin, ArtifactType
from agentworks.artifacts.native import mcp as mcp_module
from agentworks.artifacts.native.mcp import render_mcp, resolve_mcp_user_path, session_mcp_conflicts
from agentworks.capabilities.harness_integration.settings import SettingsObject, serialize_settings
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.errors import StateConflictError, StateError, UserAbort, ValidationError
from agentworks.execution.files import Files
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex import artifacts as codex
from tests.artifacts._fixtures import received
from tests.artifacts.test_mcp_session_delivery import mcp
from tests.artifacts.test_native_delivery import artifact, context
from tests.native_setup_fixtures import LocalFixtureTransport, forbid_files, native_cli_double, setup_fixture_user

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


@pytest.fixture
def target(tmp_path):
    return LocalFixtureTransport(tmp_path / "guest")


@pytest.mark.parametrize("override", [None, ".claude", "custom-native"])
def test_claude_wrapper_uses_actual_default_or_profile_override_mcp_role(db, target, monkeypatch, override):
    target.environment.pop("CLAUDE_CONFIG_DIR")
    root = target.home / (override or ".claude")
    if override is not None:
        with (target.home / ".profile").open("a") as profile:
            profile.write(f'export CLAUDE_CONFIG_DIR="{root}"\n')
    monkeypatch.setattr("agentworks.plugins.claude.harness_integration.probe_native", lambda *args, **kwargs: str(root))
    server = mcp()
    server = replace(server, origin=ArtifactOrigin("agent", "agent", "alice", entry=server.content.name))
    checkpoints: list[tuple[NativeClaim, ...]] = []
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=(access := target.access()),
        files=access.files(),
        prior=None,
        checkpoint=checkpoints.append,
        username="alice",
        home=str(target.home),
        artifacts=received(server),
    )
    integration = ClaudeCodeIntegration.for_setup(owner_kind="agent", owner_name="alice", facet="user", config={})
    result = integration.user_init(invocation)
    destination = root / ".claude.json" if override is not None else target.home / ".claude.json"
    assert not result.files and not result.deferred
    assert json.loads(destination.read_bytes())["mcpServers"][server.content.name] == render_mcp(server, "claude")
    assert checkpoints[-1][0].destination == str(destination)
    if override is None:
        assert not (root / ".claude.json").exists()


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("role", ["user", "project", "claude-local"])
def test_session_native_conflict_defers_transport_change_without_modifying_config(target, tool, role):
    if role == "claude-local" and tool != "claude":
        pytest.skip("Claude local project entries share its user JSON document")
    workspace = target.home / "project"
    workspace.mkdir()
    desired = mcp(metadata={"type": "http", "url": "https://mcp.example.test", "headers": {}})
    existing = render_mcp(mcp(), tool)
    name = desired.content.name
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    user = resolve_mcp_user_path(tool, target.access(), home=str(target.home), environment={})
    destination = (
        user if role != "project" else str(workspace / (".mcp.json" if tool == "claude" else ".codex/config.toml"))
    )
    document: SettingsObject = {key: {name: existing}}
    if role == "claude-local":
        document = {"projects": {str(workspace): document}}
    from pathlib import Path

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    before = serialize_settings(document, format="json" if tool == "claude" else "toml")
    path.write_bytes(before)
    current = replace(context(desired), home=str(target.home))
    module = claude if tool == "claude" else codex
    workaround = "session-mcp-config" if tool == "claude" else "session-mcp-overrides"
    result = module.session_artifacts(
        current,
        extra_args=[],
        enabled_workarounds=[workaround],
        mcp_conflicts=lambda inputs, prepared: session_mcp_conflicts(
            tool,
            target.access(),
            inputs,
            home=prepared.home,
            environment=prepared.environment,
            workspace=str(workspace),
        ),
    )
    assert not result.argv and not result.application.files
    assert {entry.input_id for entry in result.application.deferred} == {desired.identity}
    assert path.read_bytes() == before


@pytest.mark.parametrize("spelling", ["{}/", "{}//", "{}/./"])
@pytest.mark.parametrize(
    "tool,role", [("claude", "project"), ("claude", "claude-local"), ("codex", "project"), ("codex", "user")]
)
@pytest.mark.parametrize("equal", [True, False])
def test_unnormalized_stored_workspace_spelling_is_checked_at_its_normalized_location(
    target, spelling, tool, role, equal
):
    from pathlib import Path

    workspace = target.home / "project"
    workspace.mkdir()
    item = mcp()
    entry = render_mcp(
        item if equal else mcp(metadata={"type": "http", "url": "https://mcp.example.test", "headers": {}}), tool
    )
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    document: SettingsObject = {key: {item.content.name: entry}}
    if role == "project":
        path = workspace / (".mcp.json" if tool == "claude" else ".codex/config.toml")
    else:
        path = Path(resolve_mcp_user_path(tool, target.access(), home=str(target.home), environment={}))
        if role == "claude-local":
            document = {"projects": {str(workspace): document}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_settings(document, format="json" if tool == "claude" else "toml"))
    result = session_mcp_conflicts(
        tool,
        target.access(),
        received(item),
        home=str(target.home),
        environment={},
        workspace=spelling.format(workspace),
    )
    assert set(result) == (set() if equal else {item.identity})


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("spelling", ["{}/../project", "{}/pro\tject", "~/project", "project"])
def test_stored_workspace_the_file_operations_cannot_address_defers_without_reading(
    target, monkeypatch, tool, spelling
):
    def forbid_read(*args, **kwargs):
        pytest.fail("an unaddressable workspace path was used to read a config")

    monkeypatch.setattr(Files, "read", forbid_read)
    result = session_mcp_conflicts(
        tool,
        target.access(),
        received(mcp()),
        home=str(target.home),
        environment={},
        workspace=spelling.format(target.home),
    )
    assert set(result) == {mcp().identity}


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_malformed_request_raises_instead_of_deferring(target, monkeypatch, tool):
    def refuse(*args, **kwargs):
        raise ValidationError("request refused")

    monkeypatch.setattr(mcp_module, "resolve_mcp_user_path", refuse)
    with pytest.raises(ValidationError):
        session_mcp_conflicts(
            tool,
            target.access(),
            received(mcp()),
            home=str(target.home),
            environment={},
            workspace=str(target.home),
        )


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_equal_native_entry_and_unrelated_entries_allow_private_invocation(target, tool):
    from pathlib import Path

    item = mcp()
    path = Path(resolve_mcp_user_path(tool, target.access(), home=str(target.home), environment={}))
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    before = serialize_settings(
        {key: {item.content.name: render_mcp(item, tool), "unrelated": {"command": "other"}}},
        format="json" if tool == "claude" else "toml",
    )
    path.write_bytes(before)
    assert (
        session_mcp_conflicts(
            tool,
            target.access(),
            received(item),
            home=str(target.home),
            environment={},
            workspace=str(target.home / "project"),
        )
        == {}
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_malformed_native_config_advisory_contains_no_native_content(target, tool):
    from pathlib import Path

    item = mcp()
    path = Path(resolve_mcp_user_path(tool, target.access(), home=str(target.home), environment={}))
    path.parent.mkdir(parents=True, exist_ok=True)
    sensitive = "native-private-sentinel"
    path.write_text("{ invalid " + sensitive)
    result = session_mcp_conflicts(
        tool,
        target.access(),
        received(item),
        home=str(target.home),
        environment={},
        workspace=str(target.home / "project"),
    )
    assert set(result) == {item.identity} and sensitive not in repr(result)


@pytest.mark.parametrize(
    "error",
    [
        StateConflictError("database changed"),
        StateError("internal contract"),
        UserAbort("cancelled"),
        RuntimeError("internal failure"),
        KeyboardInterrupt(),
    ],
)
def test_session_native_advisory_preserves_cancellation_internal_and_persistence_failures(target, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Files, "read", fail)
    with pytest.raises(type(error)):
        session_mcp_conflicts(
            "codex",
            target.access(),
            received(mcp()),
            home=str(target.home),
            environment={},
            workspace=str(target.home / "project"),
        )


def test_claude_unreadable_mcp_role_defers_mcp_and_continues_legacy_application(db, target, monkeypatch):
    from agentworks.plugins.claude import harness_integration as plugin

    server, rule = mcp(), artifact(ArtifactType.RULE)
    root = target.home / ".claude"
    (root / ".claude.json").mkdir(parents=True)
    monkeypatch.setattr(plugin, "probe_native", lambda *args, **kwargs: str(root))
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=(access := target.access()),
        files=access.files(),
        prior=None,
        checkpoint=lambda claims: None,
        username="alice",
        home=str(target.home),
        artifacts=received(server, rule),
    )
    result = ClaudeCodeIntegration.for_setup(owner_kind="agent", owner_name="alice", facet="user", config={}).user_init(
        invocation
    )
    assert {entry.input_id for entry in result.deferred} == {server.identity}
    assert result.files and all(server.origin_identity not in file.origins for file in result.files)


@pytest.mark.parametrize("tool,variable", [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")])
def test_user_role_outside_home_is_refused_without_reading_it(target, tool, variable):
    target.environment[variable] = str(target.root / "external-native")
    from agentworks.errors import ConfigError

    with pytest.raises(ConfigError):
        resolve_mcp_user_path(tool, target.access(), home=str(target.home), environment={})
    result = session_mcp_conflicts(
        tool,
        target.access(),
        received(mcp()),
        home=str(target.home),
        environment={},
        workspace=str(target.home / "project"),
    )
    assert set(result) == {mcp().identity}
    assert not (target.root / "external-native").exists()


@pytest.mark.parametrize("suffix", ["/../external", "//nested"])
def test_malformed_native_home_defers_mcp_without_reading_a_config(target, monkeypatch, suffix):
    target.environment["CODEX_HOME"] = str(target.home) + suffix

    def forbid_read(*args, **kwargs):
        pytest.fail("a malformed native home was used to read a config")

    monkeypatch.setattr(Files, "read", forbid_read)
    result = session_mcp_conflicts(
        "codex",
        target.access(),
        received(mcp()),
        home=str(target.home),
        environment={},
        workspace=str(target.home / "project"),
    )
    assert set(result) == {mcp().identity}


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_unsuitable_existing_config_is_an_advisory_mcp_deferral(target, kind):
    config = target.home / ".codex/config.toml"
    config.parent.mkdir()
    sentinel = target.root / "operator-config.toml"
    sentinel.write_text('[mcp_servers.operator]\ncommand = "private-command"\n')
    if kind == "symlink":
        config.symlink_to(sentinel)
    else:
        config.mkdir()
    before = sentinel.read_bytes()
    result = session_mcp_conflicts(
        "codex",
        target.access(),
        received(mcp()),
        home=str(target.home),
        environment={},
        workspace=str(target.home / "project"),
    )
    assert set(result) == {mcp().identity}
    assert "private-command" not in repr(result)
    assert sentinel.read_bytes() == before
    assert config.is_symlink() if kind == "symlink" else config.is_dir()


def test_saved_mcp_path_is_never_io_authority_and_independent_current_entries_still_apply(target, monkeypatch):
    from pathlib import Path

    from agentworks.artifacts.native.mcp import apply_mcp
    from agentworks.capabilities.harness_integration.settings import parse_settings
    from agentworks.capabilities.harness_integration.setup import WorkspaceSetupInvocation
    from tests.native_setup_fixtures import invocation, record

    blocked, independent = mcp(name="blocked"), mcp(name="independent")
    seeded = target.home / "seed/config.toml"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    seed = replace(invocation(target, checkpoints), artifacts=received(blocked))
    apply_mcp("codex", seed, destination=str(seeded))
    sentinel = target.root / "forged-saved-destination"
    sentinel.write_bytes(seeded.read_bytes())
    before = sentinel.read_bytes()
    claim = checkpoints[-1][0].model_copy(update={"destination": str(sentinel)})
    workspace = target.home / "project"
    destination = workspace / ".codex/config.toml"
    reported: list[str] = []
    confirmed: list[tuple[NativeClaim, ...]] = []
    call = WorkspaceSetupInvocation(
        vm=seed.vm,
        access=(access := target.access()),
        files=access.files(),
        prior=record((claim,), "codex"),
        checkpoint=confirmed.append,
        report_incomplete=reported.append,
        artifacts=received(blocked, independent),
        workspace_name="ws",
        root=str(workspace),
        linux_group="",
    )
    observed = []
    read = Files.read
    update = Files.update

    def checked_read(self, path, **kwargs):
        assert Path(path) != sentinel
        observed.append(path)
        return read(self, path, **kwargs)

    def checked_update(self, path, *args, **kwargs):
        assert Path(path) != sentinel
        return update(self, path, *args, **kwargs)

    def mapping(current):
        document = {} if current is None else parse_settings(current, format="toml")
        document["theme"] = "operator"
        return serialize_settings(document, format="toml"), {"theme": "operator"}

    monkeypatch.setattr(Files, "read", checked_read)
    monkeypatch.setattr(Files, "update", checked_update)
    result = apply_mcp("codex", call, destination=str(destination), map_document=mapping)
    assert observed == [str(destination)]
    assert sentinel.read_bytes() == before and reported
    assert {entry.input_id for entry in result.deferred} == {blocked.identity}
    document = parse_settings(destination.read_bytes(), format="toml")
    assert document["theme"] == "operator" and set(document["mcp_servers"]) == {independent.content.name}
    assert claim in confirmed[-1]
    assert result.shared_paths == (str(destination),)
    assert str(sentinel) not in " ".join(reported)


def test_saved_mcp_only_retirement_never_opens_stored_path(target, monkeypatch):
    from agentworks.artifacts.native.mcp import apply_mcp
    from tests.native_setup_fixtures import invocation, record

    item = mcp()
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(target, checkpoints), artifacts=received(item))
    current = target.home / ".codex/config.toml"
    apply_mcp("codex", call, destination=str(current))
    forged = checkpoints[-1][0].model_copy(update={"destination": str(target.root / "saved-sentinel")})
    reported: list[str] = []
    retired = replace(call, prior=record((forged,), "codex"), artifacts=received(), report_incomplete=reported.append)

    forbid_files(monkeypatch, "retirement opened a native file without a current-role claim")
    result = apply_mcp("codex", retired, destination=str(current))
    assert reported and not result.files and not result.shared_paths and not result.deferred
    assert current.is_file() and not (target.root / "saved-sentinel").exists()


@pytest.mark.parametrize(
    "tool,class_name",
    [("claude", "ClaudeCodeIntegration"), ("codex", "CodexIntegration"), ("grok", "GrokBuildIntegration")],
)
def test_mcp_only_user_application_needs_no_native_cli_probe(db, target, monkeypatch, tool, class_name):
    from importlib import import_module
    from pathlib import Path

    module = import_module(f"agentworks.plugins.{tool}.harness_integration")

    def forbid(*args, **kwargs):
        pytest.fail("MCP-only registration attempted the legacy native CLI discovery probe")

    monkeypatch.setattr(module, "probe_native", forbid)
    item = mcp()
    call = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        access=(access := target.access()),
        files=access.files(),
        prior=None,
        checkpoint=lambda claims: None,
        username="alice",
        home=str(target.home),
        artifacts=received(item),
    )
    result = (
        getattr(module, class_name)
        .for_setup(owner_kind="agent", owner_name="alice", facet="user", config={})
        .user_init(call)
    )
    path = Path(resolve_mcp_user_path(tool, target.access(), home=str(target.home), environment={}))
    assert path.is_file() and not result.files and not result.deferred


def test_unknown_retirement_role_skips_mcp_io_and_retains_claims(target, monkeypatch):
    from agentworks.artifacts.native.layout import resolve_user_layout
    from agentworks.artifacts.native.mcp import apply_mcp, user_mcp_location
    from tests.native_setup_fixtures import invocation, record

    item = mcp()
    checkpoints: list[tuple[NativeClaim, ...]] = []
    seeded = replace(invocation(target, checkpoints), artifacts=received(item))
    current = target.home / ".codex/config.toml"
    apply_mcp("codex", seeded, destination=str(current))
    old = checkpoints[-1]
    reported: list[str] = []
    retired = replace(seeded, prior=record(old, "codex"), artifacts=received(), report_incomplete=reported.append)
    target.environment["CODEX_HOME"] = str(target.root / "outside-home")
    layout = resolve_user_layout("codex", target.access(), home=retired.home, environment=retired.environment)
    destination, skip, deferred = user_mcp_location(layout, retired, retiring=True)
    assert destination is None and skip and not deferred and reported

    forbid_files(monkeypatch, "unverified native MCP role attempted config I/O")
    setup_fixture_user("codex", None, retired, mcp_destination=destination, skip_mcp=skip)
    assert current.exists() and checkpoints[-1] == old


def test_unknown_mcp_role_preserves_confirmed_claims_through_plugin_progress_and_settings(target, monkeypatch):
    from unittest.mock import Mock

    from agentworks.artifacts.native.mcp import apply_mcp
    from agentworks.capabilities.harness_integration.settings import SettingsMapping, parse_settings
    from agentworks.plugins._harness_native.native_cli import NativeMarket, NativePlugin, identity
    from agentworks.plugins._harness_native.native_config import NativeUserConfig
    from tests.native_setup_fixtures import invocation, record

    item = mcp()
    path = target.home / ".codex/config.toml"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(target, checkpoints), artifacts=received(item))
    apply_mcp("codex", call, destination=str(path))
    old = checkpoints[-1]
    checkpoints.clear()
    source = target.root / "mapped.toml"
    source.write_text('theme="operator"\n')
    market = NativeMarket("fixture", identity({"source_type": "local", "source": "/fixture-market"}))
    installed: list[NativePlugin] = []
    cli = native_cli_double()
    cli.markets.return_value = (market,)
    cli.plugins.side_effect = lambda _markets: tuple(installed)
    cli.available.return_value = ("one@fixture",)

    def install(selector):
        installed.append(NativePlugin(selector, "user", True, identity({"marketplace": market.source})))
        document = parse_settings(path.read_bytes(), format="toml")
        document["plugins"] = {selector: {"enabled": True}}
        path.write_bytes(serialize_settings(document, format="toml"))

    cli.install.side_effect = install
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", Mock(return_value=cli))
    changed = replace(
        item,
        content=replace(
            item.content, metadata_json=json.dumps({"type": "stdio", "command": "changed", "args": [], "env": {}})
        ),
    )
    setup_fixture_user(
        "codex",
        NativeUserConfig(
            settings=SettingsMapping(source=str(source), strategy="merge-overwrite"), plugins=["one@fixture"]
        ),
        replace(call, prior=record(old, "codex"), artifacts=received(changed)),
        skip_mcp=True,
    )
    assert checkpoints and all(old[0] in prefix for prefix in checkpoints)
    document = parse_settings(path.read_bytes(), format="toml")
    plugins, servers = document["plugins"], document["mcp_servers"]
    assert isinstance(plugins, dict) and isinstance(servers, dict)
    plugin, server = plugins["one@fixture"], servers[item.content.name]
    assert isinstance(plugin, dict) and isinstance(server, dict)
    assert document["theme"] == "operator" and plugin["enabled"] is True
    assert server["command"] == "inventory-server"
