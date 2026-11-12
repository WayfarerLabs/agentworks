"""Dispatch preserves unavailable ancestor entries while local removals converge."""

from __future__ import annotations

import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.mcp import content_from_definition, parse_definition
from agentworks.artifacts.model import (
    ArtifactComponent,
    ArtifactInput,
    ArtifactInputs,
    ArtifactOrigin,
    ArtifactProvenance,
)
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.mcp import apply_mcp
from agentworks.artifacts.routing import RoutingResult
from agentworks.artifacts.state import CapturedArtifacts, read_captures, write_capture
from agentworks.capabilities.harness_integration.settings import parse_settings
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure
from agentworks.harness_setup.state import write_native_setup
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from tests.artifacts._fixtures import group, received
from tests.native_setup_fixtures import LocalFixtureTransport
from tests.plugins._fixtures import ConformingHarnessIntegration

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest native files")


def item(component: ArtifactComponent, name: str, command: str = "fixture") -> ArtifactInput:
    return ArtifactInput(
        content=content_from_definition(name, parse_definition({"type": "stdio", "command": command})),
        provenance=ArtifactProvenance(),
        origin=ArtifactOrigin(component, component, "fixture", entry=name),
    )


@pytest.mark.parametrize("unavailable", ["unknown", "unresolved", "none"])
def test_ancestor_claim_sources_are_retained_without_retaining_removed_local_definitions(
    db, tmp_path, monkeypatch, unavailable
):
    target = LocalFixtureTransport(tmp_path / "guest")
    vm = db.insert_vm("vm", site="fixture", hostname="vm")
    path = target.home / ".codex/config.toml"
    ancestor, removed, current = item("vm", "ancestor"), item("agent", "removed"), item("agent", "current")
    checkpoints: list[tuple[NativeClaim, ...]] = []
    invocation = UserSetupInvocation(
        vm=vm,
        runner=target,
        prior=None,
        checkpoint=checkpoints.append,
        username="fixture",
        home=str(target.home),
        artifacts=received(ancestor, removed, current),
    )
    apply_mcp("codex", invocation, destination=str(path))
    previous = SetupRecord(
        component="agent",
        integration="mcp-retention-fixture",
        destination_id="a" * 64,
        declaration={},
        claims=checkpoints[-1],
        complete=True,
    )
    write_native_setup(db, "agent", "fixture", NativeSetupState(records=(previous,)), operation="fixture-baseline")
    old_capture = CapturedArtifacts("b" * 64, group(removed))
    write_capture(db, "agent", "fixture", "agent", old_capture, operation="fixture-baseline")
    updated = item("agent", "current", command="updated")
    new_capture = CapturedArtifacts("c" * 64, group(updated))
    inputs = SetupInputs(
        kind="agent",
        name="fixture",
        component="agent",
        activations={"mcp-retention-fixture": CapabilityConfig.model_validate({})},
        target=SecretTarget(vm={}),
        artifact_snapshot=new_capture,
    )
    routing = RoutingResult(
        ArtifactInputs(local=new_capture.inputs),
        routing_unknown=unavailable == "unknown",
        unresolved_routes=frozenset({ancestor.identity}) if unavailable == "unresolved" else frozenset(),
    )
    monkeypatch.setattr("agentworks.artifacts.routing.setup_artifacts", lambda *args: routing)
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr("agentworks.harness_setup.dispatch.ensure_harness_integration_enabled", lambda *args: None)
    observed: list[frozenset[str]] = []

    class RetentionMCP(ConformingHarnessIntegration):
        name = "mcp-retention-fixture"
        description = "Native ancestor retention fixture"

        def user_init(self, call: UserSetupInvocation) -> ArtifactApplication:
            observed.append(call.retain_artifact_origins)
            return apply_mcp("codex", call, destination=str(path))

    with seated_plugin(Plugin(name="mcp-retention-plugin", capabilities={"harness-integration": (RetentionMCP,)})):
        failures: list[SetupFailure] = []
        state = run_setup(
            db,
            Mock(),
            inputs,
            replace(invocation, artifacts=ArtifactInputs()),
            operation="agent-reinit",
            failures=failures,
        )
        assert failures == []
        expected = frozenset({ancestor.origin_identity}) if unavailable != "none" else frozenset()
        assert observed == [expected]
        assert removed.origin_identity not in observed[0] and current.origin_identity not in observed[0]
        servers = parse_settings(path.read_bytes(), format="toml")["mcp_servers"]
        assert isinstance(servers, dict)
        assert mcp_native_name(removed) not in servers
        assert (mcp_native_name(ancestor) in servers) is (unavailable != "none")
        current_entry = servers[mcp_native_name(current)]
        assert isinstance(current_entry, dict) and current_entry["command"] == "updated"
        assert {claim.source for claim in state.records[0].claims} == {current.origin_identity} | set(expected)
        assert read_captures(db, "agent", "fixture")["agent"].inputs == new_capture.inputs
        routing = replace(routing, routing_unknown=False, unresolved_routes=frozenset())
        state = run_setup(db, Mock(), inputs, invocation, operation="agent-reinit", failures=failures)
        assert observed[-1] == frozenset() and failures == []
        assert {claim.source for claim in state.records[0].claims} == {current.origin_identity}
        remaining = parse_settings(path.read_bytes(), format="toml")["mcp_servers"]
        assert isinstance(remaining, dict) and mcp_native_name(ancestor) not in remaining
    assert not list((target.root / "tmp").iterdir())
