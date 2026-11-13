"""Native session carriers preserve declared MCP names and every input's outcome."""

from __future__ import annotations

import json
import tomllib

import pytest

from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from tests.artifacts.test_mcp_session_delivery import mcp
from tests.artifacts.test_native_delivery import context


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_session_server_uses_complete_declared_key(tool):
    item = mcp(name="a" * 64)
    current = context(item)
    if tool == "claude":
        result = claude.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-config"])
        servers = json.loads(result.application.files[0].data)["mcpServers"]
    else:
        result = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-overrides"])
        servers = tomllib.loads(result.argv[1])["mcp_servers"]
    assert set(servers) == {"a" * 64}
    assert not result.application.deferred


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_equal_deferrals_share_one_named_server_with_all_origins(tool):
    first, second = mcp(owner="outer"), mcp(owner="inner")
    current = context(first, second)
    if tool == "claude":
        result = claude.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-config"])
        assert set(json.loads(result.application.files[0].data)["mcpServers"]) == {"inventory"}
        assert set(result.application.files[0].origins) == {first.origin_identity, second.origin_identity}
    else:
        result = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-overrides"])
        assert len(result.argv) == 2
        assert set(tomllib.loads(result.argv[1])["mcp_servers"]) == {"inventory"}
    assert not result.application.deferred


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_differing_same_name_inputs_all_defer_while_independent_server_applies(tool):
    first = mcp(owner="outer")
    second = mcp(owner="inner", metadata={"type": "stdio", "command": "different", "args": [], "env": {}})
    independent = mcp(owner="inner", name="independent")
    current = context(first, second, independent)
    observed = []

    def inspect(inputs, _context):
        observed.append({item.identity for item in inputs.items()})
        return {}

    if tool == "claude":
        result = claude.session_artifacts(
            current, extra_args=[], enabled_workarounds=["session-mcp-config"], mcp_conflicts=inspect
        )
        servers = json.loads(result.application.files[0].data)["mcpServers"]
        assert result.application.files[0].origins == (independent.origin_identity,)
    else:
        result = codex.session_artifacts(
            current, extra_args=[], enabled_workarounds=["session-mcp-overrides"], mcp_conflicts=inspect
        )
        assert len(result.argv) == 2
        servers = tomllib.loads(result.argv[1])["mcp_servers"]
    assert set(servers) == {"independent"}
    assert {entry.input_id for entry in result.application.deferred} == {first.identity, second.identity}
    assert observed == [{independent.identity}]


def test_codex_complete_command_deferral_retains_every_same_name_input():
    first, second = mcp(owner="outer"), mcp(owner="inner")
    current = context(first, second)
    plan = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-overrides"])
    result = codex.defer_session_mcp(plan, current.inputs, "fixture command limit")
    assert result.argv == ()
    assert {entry.input_id for entry in result.application.deferred} == {first.identity, second.identity}


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_native_conflict_is_reported_for_all_equal_same_name_inputs(tool):
    first, second = mcp(owner="outer"), mcp(owner="inner")

    def inspect(inputs, _context):
        return {item.identity: "fixture native conflict" for item in inputs.items()}

    render = claude.session_artifacts if tool == "claude" else codex.session_artifacts
    workaround = "session-mcp-config" if tool == "claude" else "session-mcp-overrides"
    result = render(context(first, second), extra_args=[], enabled_workarounds=[workaround], mcp_conflicts=inspect)
    assert result.argv == () and result.application.files == ()
    assert {entry.input_id for entry in result.application.deferred} == {first.identity, second.identity}
