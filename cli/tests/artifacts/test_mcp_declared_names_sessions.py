"""Native session carriers preserve declared MCP names and every input's outcome."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace

import pytest

from agentworks.artifacts.model import ArtifactInput, ArtifactType
from agentworks.artifacts.native import common
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from tests.artifacts.test_mcp_session_delivery import mcp
from tests.artifacts.test_native_delivery import artifact, context


def vm_server() -> ArtifactInput:
    item = mcp()
    return replace(item, origin=replace(item.origin, component="vm", resource_kind="vm"))


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
@pytest.mark.parametrize("same_key", [False, True])
def test_equal_definitions_share_a_server_only_at_the_same_receiving_key(tool, same_key):
    first, second = vm_server(), mcp(name="vm-inventory" if same_key else "inventory")
    current = context(first, second)
    requested = {"vm-inventory", second.content.name}
    if tool == "claude":
        result = claude.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-config"])
        assert set(json.loads(result.application.files[0].data)["mcpServers"]) == requested
        assert set(result.application.files[0].origins) == {first.origin_identity, second.origin_identity}
    else:
        result = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-overrides"])
        assert len(result.argv) == 2 * len(requested)
        assert {name for value in result.argv[1::2] for name in tomllib.loads(value)["mcp_servers"]} == requested
    assert not result.application.deferred


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_differing_same_name_inputs_all_defer_while_independent_server_applies(tool):
    first = vm_server()
    second = mcp(name="vm-inventory", metadata={"type": "stdio", "command": "different", "args": [], "env": {}})
    independent = mcp(name="independent")
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
    first, second = vm_server(), mcp(name="vm-inventory")
    current = context(first, second)
    plan = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-mcp-overrides"])
    result = codex.defer_session_mcp(plan, current.inputs, "fixture command limit")
    assert result.argv == ()
    assert {entry.input_id for entry in result.application.deferred} == {first.identity, second.identity}


def test_codex_combined_carrier_limit_defers_all_mcp_inputs_and_keeps_persona(monkeypatch):
    first, second = vm_server(), mcp(name="vm-inventory")
    independent = mcp(name="independent")
    persona = artifact(ArtifactType.AGENT)
    mcp_plan = codex.session_artifacts(
        context(first, second, independent), extra_args=[], enabled_workarounds=["session-mcp-overrides"]
    )
    persona_plan = codex.session_artifacts(
        context(persona), extra_args=[], enabled_workarounds=["session-agent-config"]
    )
    budget = max(sum(len(token.encode()) + 1 for token in plan.argv) for plan in (mcp_plan, persona_plan))
    monkeypatch.setattr(common, "_NATIVE_COMMAND_BYTES", budget)
    current = context(first, second, independent, persona)
    plan = codex.session_artifacts(
        current, extra_args=[], enabled_workarounds=["session-mcp-overrides", "session-agent-config"]
    )
    result = codex.limit_session_argv(plan, current.inputs)
    assert result.argv == persona_plan.argv
    assert result.application.files == persona_plan.application.files
    assert {entry.input_id for entry in result.application.deferred} == {
        first.identity,
        second.identity,
        independent.identity,
    }


@pytest.mark.parametrize("flag", ["-c", "--config"])
@pytest.mark.parametrize("override_key", ["vm-inventory", "inventory"])
def test_codex_raw_literal_override_matches_the_delivered_key(flag, override_key):
    first, second = vm_server(), mcp(name="vm-inventory")
    independent = mcp(name="independent")
    result = codex.session_artifacts(
        context(first, second, independent),
        extra_args=[flag, f'mcp_servers.{override_key}.command="operator"'],
        enabled_workarounds=["session-mcp-overrides"],
    )
    collides = override_key == "vm-inventory"
    assert {entry.input_id for entry in result.application.deferred} == (
        {first.identity, second.identity} if collides else set()
    )
    requested = {independent.content.name} if collides else {independent.content.name, "vm-inventory"}
    assert result.argv[::2] == ("-c",) * len(requested)
    assert {name for value in result.argv[1::2] for name in tomllib.loads(value)["mcp_servers"]} == requested


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_native_conflict_is_reported_for_all_equal_same_name_inputs(tool):
    first, second = vm_server(), mcp(name="vm-inventory")

    def inspect(inputs, _context):
        return {item.identity: "fixture native conflict" for item in inputs.items()}

    render = claude.session_artifacts if tool == "claude" else codex.session_artifacts
    workaround = "session-mcp-config" if tool == "claude" else "session-mcp-overrides"
    result = render(context(first, second), extra_args=[], enabled_workarounds=[workaround], mcp_conflicts=inspect)
    assert result.argv == () and result.application.files == ()
    assert {entry.input_id for entry in result.application.deferred} == {first.identity, second.identity}
