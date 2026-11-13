"""MCP session carriers remain opt-in, invocation-local and independently deferrable."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace

import pytest

from agentworks.artifacts.model import ArtifactContent, ArtifactInput, ArtifactOrigin, ArtifactProvenance, ArtifactType
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.grok import artifacts as grok
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context


def mcp(*, name: str = "inventory", owner: str = "s1", metadata: dict | None = None) -> ArtifactInput:
    return ArtifactInput(
        ArtifactContent(
            ArtifactType.MCP_SERVER,
            name,
            metadata_json=json.dumps(
                metadata or {"type": "stdio", "command": "inventory-server", "args": [], "env": {}}
            ),
        ),
        ArtifactProvenance(),
        ArtifactOrigin("session", "session", owner, bundle="team", entry=name),
    )


@pytest.mark.parametrize("render", [claude.session_artifacts, codex.session_artifacts, grok.session_artifacts])
def test_mcp_defaults_to_terminal_deferral_without_files_or_argv(render):
    item = mcp()
    result = render(context(item), extra_args=[])
    assert result.argv == () and result.application.files == ()
    assert [(entry.input_id, entry.destination) for entry in result.application.deferred] == [
        (item.identity, "session")
    ]


def test_claude_private_json_contains_static_refs_and_no_skill_plugin():
    item = mcp(metadata={"type": "stdio", "command": "server", "args": ["--inventory"], "env": {"TOKEN": "${TOKEN}"}})
    current = replace(context(item), environment={"TOKEN": "resolved-secret"})
    result = claude.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-config"])
    assert result.argv == ("--mcp-config", f"{current.directory}/mcp.json")
    assert len(result.application.files) == 1
    file = result.application.files[0]
    assert file.path.startswith(current.directory + "/")
    assert file.origins == (item.origin_identity,)
    assert file.native_identity is None and file.package_root is None
    server = json.loads(file.data)["mcpServers"][item.content.name]
    assert server["args"] == ["--inventory"] and server["env"] == {"TOKEN": "${TOKEN}"}
    assert b"resolved-secret" not in file.data
    assert "--strict-mcp-config" not in result.argv
    assert not result.application.deferred


@pytest.mark.parametrize("raw", [["--bare"], ["--mcp-config=x.json"], ["--strict-mcp-config"]])
def test_claude_mcp_raw_conflicts_defer_without_changing_launch(raw):
    item = mcp()
    result = claude.session_artifacts(context(item), extra_args=raw, enabled_workarounds=["session-mcp-config"])
    assert result.argv == () and result.application.files == ()
    assert {entry.input_id for entry in result.application.deferred} == {item.identity}


def test_claude_mcp_private_path_tracks_run_while_native_name_stays_stable():
    item = mcp()
    before = context(item)
    after = replace(before, run_id="next", directory=before.directory + "-next")
    a, b = [
        claude.session_artifacts(value, extra_args=[], enabled_workarounds=["session-mcp-config"])
        for value in (before, after)
    ]
    assert a.application.files[0].path != b.application.files[0].path
    assert json.loads(a.application.files[0].data) == json.loads(b.application.files[0].data)


def test_codex_mcp_uses_complete_server_tables_and_static_environment_names():
    item = mcp(metadata={"type": "http", "url": "https://mcp.example.test/path", "headers": {"X.Token": "${TOKEN}"}})
    result = codex.session_artifacts(
        replace(context(item), environment={"TOKEN": "resolved-secret"}),
        extra_args=[],
        enabled_workarounds=["session-mcp-overrides"],
    )
    assert result.application.files == () and not result.application.deferred
    assert result.argv and all(result.argv[index] == "-c" for index in range(0, len(result.argv), 2))
    servers = [tomllib.loads(value)["mcp_servers"][item.content.name] for value in result.argv[1::2]]
    assert servers == [{"url": "https://mcp.example.test/path", "env_http_headers": {"X.Token": "TOKEN"}}]
    assert "resolved-secret" not in " ".join(result.argv)
    assert all(value.startswith("mcp_servers.") for value in result.argv[1::2])


def test_codex_mcp_conflicting_parent_table_is_advisory():
    item = mcp()
    result = codex.session_artifacts(
        context(item), extra_args=["-c", "mcp_servers={}"], enabled_workarounds=["session-mcp-overrides"]
    )
    assert result.argv == ()
    assert {entry.input_id for entry in result.application.deferred} == {item.identity}


def test_codex_conflicting_server_defers_only_that_entry():
    blocked, allowed = mcp(name="blocked"), mcp(name="allowed")
    result = codex.session_artifacts(
        context(blocked, allowed),
        extra_args=["--config", f'mcp_servers.{blocked.content.name}.command="operator"'],
        enabled_workarounds=["session-mcp-overrides"],
    )
    assert {entry.input_id for entry in result.application.deferred} == {blocked.identity}
    assert allowed.content.name in " ".join(result.argv)
    assert blocked.content.name not in " ".join(result.argv)


def test_codex_unrelated_raw_config_does_not_block_mcp():
    item = mcp()
    result = codex.session_artifacts(
        context(item),
        extra_args=["-c", 'mcp_servers.other.command="other"'],
        enabled_workarounds=["session-mcp-overrides"],
    )
    assert result.argv and not result.application.deferred


@pytest.mark.parametrize("payload", ["x" * 40000])
def test_codex_large_optional_mcp_does_not_gate_legacy_artifacts(payload):
    server = mcp(metadata={"type": "stdio", "command": "server", "args": [payload], "env": {}})
    skill = artifact(ArtifactType.SKILL)
    result = codex.session_artifacts(
        context(server, skill),
        extra_args=[],
        enabled_workarounds=["session-mcp-overrides", "session-home-skills"],
    )
    assert result.application.files and not result.argv
    assert {entry.input_id for entry in result.application.deferred} == {server.identity}


@pytest.mark.parametrize(
    "module,workaround",
    [(claude, "session-skill-plugin"), (codex, "session-home-skills"), (grok, "session-home-skills")],
)
def test_old_opt_in_never_claims_or_embeds_mcp(module, workaround):
    server, skill = mcp(), artifact(ArtifactType.SKILL)
    result = module.session_artifacts(context(server, skill), extra_args=[], enabled_workarounds=[workaround])
    assert result.application.files
    assert {entry.input_id for entry in result.application.deferred} == {server.identity}
    assert all(server.origin_identity not in file.origins for file in result.application.files)
    assert all(server.content.name.encode() not in file.data for file in result.application.files)


def test_codex_vm_mcp_uses_user_placement_without_machine_files():
    server = mcp()
    result = codex.vm_artifacts(received(server))
    assert not result.files
    assert [(entry.input_id, entry.destination) for entry in result.deferred] == [(server.identity, "user")]


@pytest.mark.parametrize(
    "implementation_name,module_name",
    [("ClaudeCodeIntegration", "claude"), ("CodexIntegration", "codex"), ("GrokBuildIntegration", "grok")],
)
@pytest.mark.parametrize("facet", ["user", "workspace"])
def test_outer_facets_compose_entry_deferrals_and_shared_paths_with_legacy_files(
    db, monkeypatch, implementation_name, module_name, facet
):
    from importlib import import_module

    from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
    from tests.conftest import _FakeTarget

    module = import_module(f"agentworks.plugins.{module_name}.harness_integration")
    implementation = getattr(module, implementation_name)
    if facet == "user":
        from agentworks.artifacts.native.layout import NativeUserLayout

        monkeypatch.setattr(
            module,
            "user_layout",
            lambda *args, **kwargs: NativeUserLayout("/home/alice/.native", "/home/alice/.native/config.toml"),
        )
    server, rule = mcp(), artifact(ArtifactType.RULE)
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: "/home/alice/.native")
    shared = ("/workspace/.native-config",) if facet == "workspace" else ()
    native = ArtifactApplication(
        deferred=(
            ArtifactDeferral(input_id=server.identity, destination="session", reason="operator native conflict"),
        ),
        shared_paths=shared,
    )
    observed = []

    def native_setup(*args, **kwargs):
        observed.append((args, kwargs))
        return native

    method = "apply_mcp" if module_name == "grok" else "setup_" + facet
    monkeypatch.setattr(module, method, native_setup)
    common = {
        "vm": db.insert_vm("vm", "site", "host"),
        "runner": _FakeTarget(),
        "prior": None,
        "checkpoint": lambda claims: None,
        "artifacts": received(server, rule),
    }
    invocation = (
        UserSetupInvocation(**common, username="alice", home="/home/alice")
        if facet == "user"
        else WorkspaceSetupInvocation(**common, workspace_name="ws", root="/workspace", linux_group="ws")
    )
    integration = implementation.for_setup(owner_kind="agent", owner_name="alice", facet=facet, config={})
    result = getattr(integration, facet + "_init")(invocation)
    assert len(observed) == 1
    assert result.deferred == native.deferred and result.shared_paths == shared
    assert result.files and {origin for file in result.files for origin in file.origins} == {rule.origin_identity}
    assert all(".native-config" not in file.path for file in result.files)


@pytest.mark.parametrize(
    "module_name,class_name", [("claude", "ClaudeCodeIntegration"), ("grok", "GrokBuildIntegration")]
)
def test_vm_mcp_defers_to_user_without_native_file_claims(module_name, class_name):
    from importlib import import_module
    from types import SimpleNamespace

    implementation = getattr(import_module(f"agentworks.plugins.{module_name}.harness_integration"), class_name)
    integration = implementation.for_setup(owner_kind="vm", owner_name="vm", facet="vm", config={})
    server = mcp()
    result = integration.vm_init(
        SimpleNamespace(artifacts=received(server), environment={}, secrets={}, runner=object())
    )
    assert not result.files
    assert [(entry.input_id, entry.destination) for entry in result.deferred] == [(server.identity, "user")]


@pytest.mark.parametrize(
    "module_name,class_name,workaround",
    [("claude", "ClaudeCodeIntegration", "session-mcp-config"), ("codex", "CodexIntegration", "session-mcp-overrides")],
)
def test_start_rebinds_mcp_to_current_inputs_and_run_without_accumulating_argv(
    monkeypatch, module_name, class_name, workaround
):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from tests.artifacts.test_rendered_launch_args import integration
    from tests.conftest import _FakeResult, _FakeTarget

    server = mcp()
    first = context(server)
    selected = integration(module_name, class_name, {"enabled_workarounds": [workaround]}, artifacts=first)
    monkeypatch.setattr(
        f"agentworks.plugins.{module_name}.harness_integration.session_mcp_conflicts", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *args, **kwargs: "/home/alice/.native")
    runner = _FakeTarget({".thread": _FakeResult(1)})
    a = selected.start(RunContext(admin_target=runner), intent=HarnessLaunchIntent.CREATE)
    if module_name == "claude":
        assert first.directory + "/mcp.json" in a.command
    else:
        assert server.content.name in a.command
    second_server = replace(
        server,
        content=replace(
            server.content,
            metadata_json=json.dumps({"type": "stdio", "command": "replacement-server", "args": [], "env": {}}),
        ),
    )
    second = replace(first, inputs=received(second_server), directory=first.directory + "-next", run_id="next")
    selected.prepare_artifacts(second)
    b = selected.start(RunContext(admin_target=runner), intent=HarnessLaunchIntent.RESUME_OR_NEW)
    assert not b.artifacts.deferred
    if module_name == "claude":
        assert second.directory + "/mcp.json" in b.command and first.directory + "/mcp.json" not in b.command
        assert (
            json.loads(b.artifacts.files[0].data)["mcpServers"][server.content.name]["command"] == "replacement-server"
        )
        assert b.command.count("--mcp-config") == 1
    else:
        assert "replacement-server" in b.command and "inventory-server" not in b.command
        assert not b.artifacts.files


@pytest.mark.parametrize("raw", [["--profile", "operator"], ["-p", "operator"], ["--profile=operator"], ["-poperator"]])
def test_codex_explicit_profile_defers_optional_mcp_without_native_inspection(raw):
    item = mcp()

    def forbid(*args):
        pytest.fail("explicit profile MCP reached partial native inspection")

    result = codex.session_artifacts(
        context(item), extra_args=raw, enabled_workarounds=["session-mcp-overrides"], mcp_conflicts=forbid
    )
    assert not result.argv and not result.application.files
    assert {entry.input_id for entry in result.application.deferred} == {item.identity}


def test_codex_modeled_profile_remains_usable_with_optional_mcp_deferred(monkeypatch):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from tests.artifacts.test_rendered_launch_args import integration
    from tests.conftest import _FakeResult, _FakeTarget

    item = mcp()
    selected = integration(
        "codex",
        "CodexIntegration",
        {"profile": "operator", "enabled_workarounds": ["session-mcp-overrides"]},
        artifacts=context(item),
    )

    def forbid(*args, **kwargs):
        pytest.fail("modeled profile MCP reached partial native inspection")

    monkeypatch.setattr("agentworks.plugins.codex.harness_integration.session_mcp_conflicts", forbid)
    result = selected.start(
        RunContext(admin_target=_FakeTarget({".thread": _FakeResult(1)})), intent=HarnessLaunchIntent.CREATE
    )
    assert {entry.input_id for entry in result.artifacts.deferred} == {item.identity}
    assert "--profile operator" in result.command or "-p operator" in result.command
    assert item.content.name not in result.command
