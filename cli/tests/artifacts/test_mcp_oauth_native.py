"""Public OAuth translation uses ordinary native entry and session ownership."""

from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactComponent, ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.mcp import apply_mcp, render_mcp
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
from agentworks.errors import ConfigError, StateError, UserAbort
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context
from tests.artifacts.test_native_mcp import read, server, table, write
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, record

PUBLIC = {"client_id": "public-client", "callback_port": 8180, "scopes": ["write:z", "read:a"]}


def http(options: dict | None = None, *, name="inventory", component: ArtifactComponent = "agent"):
    return server(
        name,
        component=component,
        definition={"type": "http", "url": "https://example.test/mcp", **(options or {})},
    )


@pytest.fixture
def transport(tmp_path):
    if sys.platform != "linux":
        pytest.skip("executes Linux guest file helpers")
    return LocalFixtureTransport(tmp_path / "guest")


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
@pytest.mark.parametrize("options", [{}, {"oauth": None}])
def test_absent_and_null_oauth_keep_registration_output(tool, options):
    expected = {"url": "https://example.test/mcp"}
    if tool == "claude":
        expected["type"] = "http"
    assert render_mcp(http(options), tool) == expected


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize(
    "oauth,claude_options,codex_options",
    [
        ({}, {}, {}),
        ({"client_id": None, "callback_port": None, "scopes": None}, {}, {}),
        ({"client_id": "public-client"}, {"clientId": "public-client"}, {"client_id": "public-client"}),
        ({"callback_port": 8180}, {"callbackPort": 8180}, {"callback_port": 8180}),
        ({"scopes": ["write:z", "read:a"]}, {"scopes": "write:z read:a"}, {}),
        (
            PUBLIC,
            {"clientId": "public-client", "callbackPort": 8180, "scopes": "write:z read:a"},
            {"client_id": "public-client", "callback_port": 8180},
        ),
    ],
)
def test_public_options_translate_without_filling_native_defaults(tool, oauth, claude_options, codex_options):
    expected = {"url": "https://example.test/mcp", "oauth": claude_options if tool == "claude" else codex_options}
    if tool == "claude":
        expected["type"] = "http"
    elif oauth.get("scopes") is not None:
        expected["scopes"] = oauth["scopes"]
    assert render_mcp(http({"oauth": oauth}), tool) == expected


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_oauth_proxy_headers_remain_unresolved(tool, monkeypatch):
    monkeypatch.setenv("PROXY_TOKEN", "private-fixture-token")
    headers = {"X-Proxy": "${PROXY_TOKEN}", "X-Public": "literal"}
    result = render_mcp(http({"oauth": {}, "headers": headers}), tool)
    if tool == "codex":
        assert result["env_http_headers"] == {"X-Proxy": "PROXY_TOKEN"}
        assert result["http_headers"] == {"X-Public": "literal"}
    else:
        assert result["headers"] == headers
    assert "private-fixture-token" not in json.dumps(result)


@pytest.mark.parametrize("oauth", [{}, {"client_id": None, "callback_port": None, "scopes": None}])
def test_grok_defaults_use_ordinary_url_registration(oauth):
    assert render_mcp(http({"oauth": oauth}), "grok") == {"url": "https://example.test/mcp"}


@pytest.mark.parametrize(
    "oauth", [{"client_id": "public-client"}, {"callback_port": 8180}, {"scopes": ["write:z", "read:a"]}, PUBLIC]
)
def test_grok_explicit_public_options_cannot_render_lossily(oauth):
    with pytest.raises(ConfigError):
        render_mcp(http({"oauth": oauth}), "grok")


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_oauth_create_adopt_update_remove_options_and_withdraw_preserve_credentials(
    transport, tool, scope, monkeypatch
):
    component = "workspace" if scope == "workspace" else "agent"
    item = http({"oauth": PUBLIC}, component=component)
    root = transport.root / "project" if scope == "workspace" else transport.home
    path = root / (
        ".mcp.json"
        if tool == "claude" and scope == "workspace"
        else ".claude.json"
        if tool == "claude"
        else ".codex/config.toml"
    )
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    credential = transport.home / ".native-credentials"
    credential.write_bytes(b"private-fixture-credential")
    write(path, {"foreign": {"private": "private-fixture-value"}}, tool)
    unrelated = {p: p.read_bytes() for p in transport.root.rglob("*") if p.is_file() and p != path}
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call: UserSetupInvocation | WorkspaceSetupInvocation = replace(
        invocation(transport, checkpoints), artifacts=received(item)
    )
    if scope == "workspace":
        call = WorkspaceSetupInvocation(
            vm=call.vm,
            runner=transport,
            prior=None,
            checkpoint=checkpoints.append,
            workspace_name="fixture",
            root=str(root),
            linux_group="",
            artifacts=received(item),
        )
    publish, observed = Mock(), Mock()
    native_read = NativeFiles.read
    native_publish = NativeFiles.publish

    def reading(files, destination):
        assert destination == str(path)
        observed(destination)
        return native_read(files, destination)

    def publishing(files, destination, content, **kwargs):
        assert destination == str(path)
        assert kwargs["expected"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert kwargs["preserve_metadata"] is True
        publish(destination)
        return native_publish(files, destination, content, **kwargs)

    monkeypatch.setattr(NativeFiles, "read", reading)
    monkeypatch.setattr(NativeFiles, "publish", publishing)
    result = apply_mcp(tool, call, destination=str(path))
    assert not result.deferred and not result.files
    assert result.shared_paths == ((str(path),) if scope == "workspace" else ())
    assert publish.call_count == 1
    name = mcp_native_name(item)
    assert table(table(read(path, tool), key), name) == render_mcp(item, tool)
    first_claim = checkpoints[-1][0]
    baseline = path.read_bytes()
    adopted: list[tuple[NativeClaim, ...]] = []
    apply_mcp(tool, replace(call, checkpoint=adopted.append), destination=str(path))
    assert adopted[-1] == checkpoints[-1] and path.read_bytes() == baseline
    assert publish.call_count == 1
    updated = http({"oauth": {"client_id": "next-client", "scopes": ["read:a", "write:z"]}}, component=component)
    without_options = http(component=component)
    for current in (updated, without_options):
        assert mcp_native_name(current) == name
        result = apply_mcp(
            tool,
            replace(call, prior=record(checkpoints[-1], "codex"), artifacts=received(current)),
            destination=str(path),
        )
        assert not result.deferred
        assert table(table(read(path, tool), key), name) == render_mcp(current, tool)
        claim = checkpoints[-1][0]
        assert claim.identifier == first_claim.identifier and claim.source == first_claim.source
        assert claim.sha256 != first_claim.sha256
    apply_mcp(tool, replace(call, prior=record(checkpoints[-1], "codex"), artifacts=received()), destination=str(path))
    assert checkpoints[-1] == () and table(read(path, tool), key) == {}
    assert read(path, tool)["foreign"] == {"private": "private-fixture-value"}
    assert publish.call_count == 4
    assert observed.call_count == 5
    assert all(p.read_bytes() == data for p, data in unrelated.items())
    assert {p for p in transport.root.rglob("*") if p.is_file()} == {*unrelated, path}
    assert not list((transport.root / "tmp").iterdir())
    assert "private-fixture" not in "".join(transport.commands + transport.logged_output)


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("owned", [False, True])
def test_oauth_foreign_or_modified_table_is_preserved_during_update_and_withdraw(transport, tool, owned):
    path = transport.home / (".claude.json" if tool == "claude" else ".codex/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    item = http({"oauth": PUBLIC})
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, checkpoints), artifacts=received(item))
    apply_mcp(tool, call, destination=str(path))
    old = checkpoints[-1] if owned else ()
    document = read(path, tool)
    entry = table(table(document, key), mcp_native_name(item))
    # Same-table confidential material is an external edit, never public ownership.
    if tool == "codex":
        table(entry, "oauth")["client_secret"] = "private-fixture-value"
    else:
        entry["operator_field"] = "private-fixture-value"
    write(path, document, tool)
    baseline = path.read_bytes()
    checkpoints.clear()
    changed = http({"oauth": {"client_id": "next-client"}})
    result = apply_mcp(
        tool, replace(call, prior=record(old, "codex"), artifacts=received(changed)), destination=str(path)
    )
    assert [d.input_id for d in result.deferred] == [changed.identity]
    reports: list[str] = []
    apply_mcp(
        tool,
        replace(call, prior=record(old, "codex"), artifacts=received(), report_incomplete=reports.append),
        destination=str(path),
    )
    assert bool(reports) is owned
    assert path.read_bytes() == baseline and checkpoints == []
    assert "private-fixture-value" not in repr(result.deferred) + repr(reports)


@pytest.mark.parametrize("modified", [False, True])
def test_grok_unsupported_update_retains_bytes_claim_and_allows_independent_defaults(transport, modified):
    path = transport.home / ".grok/config.toml"
    first = http({"oauth": {}})
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, checkpoints), artifacts=received(first))
    apply_mcp("grok", call, destination=str(path))
    previous = checkpoints[-1]
    if modified:
        document = read(path)
        table(table(document, "mcp_servers"), mcp_native_name(first))["url"] = "https://operator.test/mcp"
        write(path, document)
    with path.open("a") as stream:
        stream.write("\n# fixture formatting retained\n")
    baseline = path.read_bytes()
    original_entry = table(table(read(path), "mcp_servers"), mcp_native_name(first))
    checkpoints.clear()
    unsupported = http({"oauth": PUBLIC})
    call = replace(call, prior=record(previous, "codex"), artifacts=received(unsupported))
    result = apply_mcp("grok", call, destination=str(path))
    assert [d.input_id for d in result.deferred] == [unsupported.identity]
    assert result.deferred[0].reason and path.read_bytes() == baseline and checkpoints == []
    independent = http({"oauth": {}}, name="independent")
    result = apply_mcp("grok", replace(call, artifacts=received(unsupported, independent)), destination=str(path))
    assert [d.input_id for d in result.deferred] == [unsupported.identity]
    assert previous[0] in checkpoints[-1]
    assert {c.source for c in checkpoints[-1]} == {first.origin_identity, independent.origin_identity}
    entries = table(read(path), "mcp_servers")
    assert entries[mcp_native_name(first)] == original_entry
    assert entries[mcp_native_name(independent)] == {"url": "https://example.test/mcp"}
    assert not list((transport.root / "tmp").iterdir())


def test_grok_unsupported_new_entry_never_creates_config_or_claim(transport):
    path = transport.home / ".grok/config.toml"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    item = http({"oauth": PUBLIC})
    result = apply_mcp(
        "grok", replace(invocation(transport, checkpoints), artifacts=received(item)), destination=str(path)
    )
    assert [d.input_id for d in result.deferred] == [item.identity]
    assert not path.exists() and not checkpoints
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("error", [RuntimeError("internal"), StateError("internal"), UserAbort("cancelled")])
def test_renderer_internal_and_cancellation_failures_escape_without_publishing(transport, monkeypatch, error):
    path = transport.home / ".codex/config.toml"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    monkeypatch.setattr("agentworks.artifacts.native.mcp.render_mcp", Mock(side_effect=error))
    with pytest.raises(type(error)):
        apply_mcp(
            "codex",
            replace(invocation(transport, checkpoints), artifacts=received(http({"oauth": PUBLIC}))),
            destination=str(path),
        )
    assert not path.exists() and not checkpoints


@pytest.mark.parametrize("boundary", ["mapping", "checkpoint"])
def test_config_errors_outside_rendering_remain_strict(transport, boundary):
    path = transport.home / ".codex/config.toml"
    call = replace(invocation(transport, []), artifacts=received(http({"oauth": PUBLIC})))
    failure = Mock(side_effect=ConfigError("fixture failure"))
    if boundary == "checkpoint":
        call = replace(call, checkpoint=failure)
    with pytest.raises(ConfigError):
        apply_mcp("codex", call, destination=str(path), map_document=failure if boundary == "mapping" else None)
    assert path.exists() is (boundary == "checkpoint")


@pytest.mark.parametrize(
    "tool,module,workaround", [("claude", claude, "session-mcp-config"), ("codex", codex, "session-mcp-overrides")]
)
@pytest.mark.parametrize("oauth", [{}, {"scopes": ["write:z", "read:a"]}, PUBLIC])
def test_session_carriers_round_trip_public_options(tool, module, workaround, oauth):
    item = http({"oauth": oauth, "headers": {"X-Proxy": "${PROXY_TOKEN}"}})
    current = replace(context(item), environment={"PROXY_TOKEN": "private-fixture-token"})
    result = module.session_artifacts(current, extra_args=[], enabled_workarounds=[workaround])
    assert not result.application.deferred
    if tool == "claude":
        (file,) = result.application.files
        assert file.path.startswith(current.directory + "/") and file.origins == (item.origin_identity,)
        actual = json.loads(file.data)["mcpServers"][mcp_native_name(item)]
        assert b"private-fixture-token" not in file.data
    else:
        assert result.application.files == () and result.argv[0] == "-c"
        actual = tomllib.loads(result.argv[1])["mcp_servers"][mcp_native_name(item)]
        assert "private-fixture-token" not in repr(result.argv)
    assert actual == render_mcp(item, tool)


@pytest.mark.parametrize(
    "module,workaround,extra_args",
    [
        (claude, "session-mcp-config", ["--mcp-config=operator.json"]),
        (claude, "session-mcp-config", ["--bare"]),
        (codex, "session-mcp-overrides", ["-c", "mcp_servers={}"]),
        (codex, "session-mcp-overrides", ["--profile", "operator"]),
    ],
)
def test_oauth_session_definitions_keep_existing_raw_and_profile_conflicts(module, workaround, extra_args):
    item = http({"oauth": PUBLIC})
    result = module.session_artifacts(context(item), extra_args=extra_args, enabled_workarounds=[workaround])
    assert not result.argv and not result.application.files
    assert [d.input_id for d in result.application.deferred] == [item.identity]


def test_large_public_oauth_options_keep_existing_command_budget_and_legacy_delivery():
    item = http({"oauth": {"client_id": "x" * 40000}})
    skill = artifact(ArtifactType.SKILL)
    result = codex.session_artifacts(
        context(item, skill), extra_args=[], enabled_workarounds=["session-mcp-overrides", "session-home-skills"]
    )
    assert not result.argv and result.application.files
    assert [d.input_id for d in result.application.deferred] == [item.identity]
    assert all(item.origin_identity not in file.origins for file in result.application.files)
