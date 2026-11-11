"""Native MCP entries preserve foreign configuration and confirmed ownership."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts.mcp import content_from_definition, parse_definition
from agentworks.artifacts.model import ArtifactInput, ArtifactOrigin, ArtifactProvenance
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE, apply_mcp, mcp_servers, render_mcp
from agentworks.capabilities.harness_integration.settings import SettingsMapping, parse_settings, serialize_settings
from agentworks.capabilities.harness_integration.setup import WorkspaceSetupInvocation
from agentworks.errors import ExternalError, StateError
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native import setup_user, setup_workspace
from agentworks.plugins._harness_native.native_config import NativeUserConfig, NativeWorkspaceConfig
from tests.artifacts._fixtures import received
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest file helpers")


def server(name="inventory", command="server", *, definition=None, component="agent"):
    definition = definition or {"type": "stdio", "command": command}
    return ArtifactInput(
        content=content_from_definition(name, parse_definition(definition)),
        origin=ArtifactOrigin(component, component, "fixture", entry=name),
        provenance=ArtifactProvenance(),
    )


@pytest.fixture
def transport(tmp_path):
    return LocalFixtureTransport(tmp_path)


def read(path, tool="codex"):
    return parse_settings(path.read_bytes(), format="json" if tool == "claude" else "toml")


def write(path, document, tool="codex"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_settings(document, format="json" if tool == "claude" else "toml"))


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_static_render_preserves_reference_semantics(tool, monkeypatch):
    monkeypatch.setenv("TOKEN", "private-value-never-read")
    stdio = server(
        definition={
            "type": "stdio",
            "command": "inventory",
            "args": ["a path"],
            "env": {"TOKEN": "${TOKEN}", "PUBLIC": "ordinary"},
        }
    )
    http = server(
        definition={
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${TOKEN}", "X-Other": "${OTHER}", "X-Public": "ordinary"},
        }
    )
    native_stdio, native_http = render_mcp(stdio, tool), render_mcp(http, tool)
    if tool == "codex":
        assert native_stdio == {
            "command": "inventory",
            "args": ["a path"],
            "env": {"PUBLIC": "ordinary"},
            "env_vars": ["TOKEN"],
        }
        assert native_http == {
            "url": "https://example.test/mcp",
            "bearer_token_env_var": "TOKEN",
            "env_http_headers": {"X-Other": "OTHER"},
            "http_headers": {"X-Public": "ordinary"},
        }
    else:
        assert native_stdio["env"] == {"TOKEN": "${TOKEN}", "PUBLIC": "ordinary"}
        assert native_http["headers"] == http.content.metadata["headers"]
    assert "private-value-never-read" not in json.dumps([native_stdio, native_http])
    assert mcp_servers(received(stdio), tool) == {mcp_native_name(stdio): native_stdio}


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_create_adopt_update_and_entry_conflict_are_independent(transport, tool):
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    first, second = server(), server("other")
    write(path, {"foreign": {"private": "fixture-secret-value"}}, tool)
    claims = []
    call = replace(invocation(transport, claims), artifacts=received(first, second))
    result = apply_mcp(tool, call, destination=str(path))
    assert result.files == result.shared_paths == result.deferred == ()
    assert {c.source for c in claims[-1]} == {first.origin_identity, second.origin_identity}
    assert all(c.role == MCP_CLAIM_ROLE and c.destination == str(path) and len(c.sha256) == 64 for c in claims[-1])
    baseline = path.read_bytes()
    adopted = []
    apply_mcp(tool, replace(call, checkpoint=adopted.append), destination=str(path))
    assert adopted[-1] == claims[-1]
    assert path.read_bytes() == baseline
    document = read(path, tool)
    document[key][mcp_native_name(first)]["command"] = "operator-changed"
    write(path, document, tool)
    updated_first, updated_second = server(command="new"), server("other", command="new")
    updated = replace(call, prior=record(claims[-1], "codex"), artifacts=received(updated_first, updated_second))
    result = apply_mcp(tool, updated, destination=str(path))
    assert [d.input_id for d in result.deferred] == [updated_first.identity]
    assert read(path, tool)[key][mcp_native_name(first)]["command"] == "operator-changed"
    assert read(path, tool)[key][mcp_native_name(second)]["command"] == "new"
    assert read(path, tool)["foreign"] == {"private": "fixture-secret-value"}
    assert "fixture-secret-value" not in "".join(transport.commands + transport.logged_output)
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("modified,unavailable", [(False, False), (True, False), (False, True)])
def test_retirement_retains_modified_and_unavailable_ancestor_entries(transport, modified, unavailable):
    path = transport.home / ".grok/config.toml"
    item = server()
    claims, reasons = [], []
    call = replace(invocation(transport, claims), artifacts=received(item), report_incomplete=reasons.append)
    apply_mcp("grok", call, destination=str(path))
    prior = record(claims[-1], "codex")
    if modified:
        document = read(path)
        document["mcp_servers"][mcp_native_name(item)]["command"] = "operator"
        write(path, document)
    retire = replace(
        call,
        artifacts=received(),
        prior=prior,
        retain_artifact_origins=frozenset({item.origin_identity}) if unavailable else frozenset(),
    )
    apply_mcp("grok", retire, destination=str(path))
    present = mcp_native_name(item) in read(path).get("mcp_servers", {})
    assert present is (modified or unavailable)
    assert bool(reasons) is modified
    assert bool(claims[-1]) is (modified or unavailable)


def test_workspace_shared_paths_include_retained_claims_without_file_ownership(transport):
    item = server(component="workspace")
    root = transport.root / "project"
    root.mkdir()
    claims = []
    call = WorkspaceSetupInvocation(
        vm=object(),
        runner=transport,
        prior=None,
        checkpoint=claims.append,
        workspace_name="fixture",
        root=str(root),
        linux_group="",
        artifacts=received(item),
    )
    result = setup_workspace("codex", NativeWorkspaceConfig(), call)
    path = root / ".codex/config.toml"
    assert result.shared_paths == (str(path),)
    assert result.files == ()
    document = read(path)
    document["mcp_servers"][mcp_native_name(item)]["command"] = "operator"
    write(path, document)
    retained = setup_workspace(
        "codex", None, replace(call, prior=record(claims[-1], "codex"), report_incomplete=Mock())
    )
    assert retained.shared_paths == (str(path),)


def test_checkpoint_failure_and_expected_hash_conflict_escape(transport, monkeypatch):
    item = server()
    path = transport.home / ".codex/config.toml"
    call = replace(
        invocation(transport, []),
        artifacts=received(item),
        checkpoint=Mock(side_effect=ExternalError("fixture persistence failure")),
    )
    with pytest.raises(ExternalError):
        apply_mcp("codex", call, destination=str(path))
    assert mcp_native_name(item) in read(path)["mcp_servers"]
    path.unlink()
    publish = NativeFiles.publish

    def competing_write(files, destination, content, **kwargs):
        Path(destination).write_text('owner = "concurrent"\n')
        return publish(files, destination, content, **kwargs)

    monkeypatch.setattr(NativeFiles, "publish", competing_write)
    checkpoint = Mock()
    with pytest.raises(StateError):
        apply_mcp("codex", replace(call, checkpoint=checkpoint), destination=str(path))
    checkpoint.assert_not_called()
    assert read(path) == {"owner": "concurrent"}


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_joint_codex_mapping_unmasks_withdrawn_native_key_in_one_pass(transport, strategy):
    item = server()
    name = mcp_native_name(item)
    path = transport.home / ".codex/config.toml"
    claims = []
    call = replace(invocation(transport, claims), artifacts=received(item))
    setup_user("codex", NativeUserConfig(), call)
    source = transport.root / "mapping.toml"
    write(source, {"mcp_servers": {name: {"command": "source-owned", "args": []}}})
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy=strategy))
    result = setup_user("codex", config, replace(call, artifacts=received(), prior=record(claims[-1], "codex")))
    document = read(path)
    if strategy == "skip-existing":
        assert name not in document.get("mcp_servers", {})
    else:
        assert document["mcp_servers"][name]["command"] == "source-owned"
    assert claims[-1] == () and result.deferred == ()


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
@pytest.mark.parametrize("existing", [False, True])
def test_joint_codex_mapping_desired_key_obeys_actual_policy(transport, strategy, existing):
    item = server()
    name = mcp_native_name(item)
    path = transport.home / ".codex/config.toml"
    claims = []
    call = replace(invocation(transport, claims), artifacts=received(item))
    if existing:
        setup_user("codex", NativeUserConfig(), call)
        call = replace(call, prior=record(claims[-1], "codex"))
    source = transport.root / "mapping.toml"
    write(source, {"mcp_servers": {name: {"command": "source-owned", "args": []}}})
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy=strategy))
    result = setup_user("codex", config, call)
    mapping_wins = not existing or strategy in ("replace", "merge-overwrite")
    assert bool(result.deferred) is mapping_wins
    assert read(path)["mcp_servers"][name]["command"] == ("source-owned" if mapping_wins else "server")


def test_claude_user_role_and_config_override_are_independent_of_settings(transport):
    claims = []
    item = server()
    custom = transport.home / "custom-claude"
    call = replace(invocation(transport, claims, env={"CLAUDE_CONFIG_DIR": str(custom)}), artifacts=received(item))
    setup_user("claude", NativeUserConfig(), call)
    assert (custom / ".claude.json").exists()
    assert not (custom / "settings.json").exists()
    assert claims[-1][0].destination == str(custom / ".claude.json")


def test_mapping_only_skip_existing_preserves_bytes_and_replace_repairs_invalid_toml(transport):
    path = transport.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text("# preserve this comment\nmodel = 'existing'\n")
    source = transport.root / "mapping.toml"
    source.write_text('model = "mapped"\n')
    call = invocation(transport, [])
    baseline = path.read_bytes()
    setup_user("codex", NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="skip-existing")), call)
    assert path.read_bytes() == baseline
    path.write_text("malformed = [\n")
    setup_user("codex", NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="replace")), call)
    assert read(path) == {"model": "mapped"}
