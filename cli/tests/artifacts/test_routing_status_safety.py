"""Unavailable routes and persisted diagnostics cannot authorize unsafe placement."""

import json
from dataclasses import replace

import pytest

from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
from agentworks.artifacts.routing import inspect_owner_artifacts, setup_artifacts
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.env.entry import EnvEntry
from agentworks.harness_setup.model import NativeSetupState
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.secrets.orchestration import SecretTarget
from tests.artifacts.test_routing import graph


@pytest.mark.parametrize("change", ["config", "environment"])
def test_native_only_record_compares_declarations_without_artifact_inputs(db, change):
    fixture = graph(db, active=("agent",), counts={"vm": 0, "agent": 0})
    record = fixture.save("agent")
    owner = fixture.owners["agent"]
    assert not record.artifact_inputs and not record.artifact_files
    assert inspect_owner_artifacts(db, fixture.registry, owner, "shell").status == "current"
    if change == "environment":
        owner = replace(
            owner,
            target=SecretTarget(vm={}, agent={"DRIFT": EnvEntry.model_validate("private-value")}),
        )
    else:
        record = record.model_copy(update={"declaration": {"config": {"name": "previous"}, "env": {}}})
        write_native_setup(db, "agent", "agent", NativeSetupState(records=(record,)), operation="fixture")
    before = db.instance_state.get_applied_slices("agent", "agent")
    result = inspect_owner_artifacts(db, fixture.registry, owner, "shell")
    assert result.status == "stale"
    assert "private-value" not in result.reason
    assert db.instance_state.get_applied_slices("agent", "agent") == before


@pytest.mark.parametrize("receiver", ["agent", "admin", "workspace"])
def test_unknown_capture_route_is_distinct_from_a_confirmed_empty_route(db, receiver):
    fixture = graph(db, active=("vm", receiver))
    target = fixture.owners[receiver]
    original = fixture.save("vm", routes={"vm-0": target.facet})
    routed = setup_artifacts(db, fixture.registry, target, fixture.vm, "shell")
    assert not routed.retain_previous and not routed.unresolved_routes
    assert routed.inputs.deferred
    fixture.registry.lookup("artifact-bundle", "vm-bundle").hints.clear()
    replacement = capture_owner(fixture.registry, "vm", "vm", "vm", fixture.owners["vm"].artifacts)
    write_capture(db, "vm", "vm", "vm", replacement, operation="fixture")
    routed = setup_artifacts(db, fixture.registry, target, fixture.vm, "shell")
    assert routed.retain_previous
    assert routed.unresolved_routes == {route.input_id for route in original.deferred}
    assert not routed.inputs.deferred
    fixture.save("vm")
    confirmed_empty = setup_artifacts(db, fixture.registry, target, fixture.vm, "shell")
    assert not confirmed_empty.retain_previous and not confirmed_empty.unresolved_routes


def test_missing_active_routing_record_preserves_existing_placement(db):
    fixture = graph(db, active=("vm", "agent"))
    result = setup_artifacts(db, fixture.registry, fixture.owners["agent"], fixture.vm, "shell")
    assert result.retain_previous and not result.unresolved_routes
    assert not read_native_setup(db, "vm", "vm").records
    fixture.save("vm")
    result = setup_artifacts(db, fixture.registry, fixture.owners["agent"], fixture.vm, "shell")
    assert not result.retain_previous and not result.inputs.deferred


@pytest.mark.parametrize("count", [0, 1])
def test_first_failed_outer_attempt_has_no_confirmed_empty_routing_result(db, count):
    fixture = graph(db, active=("vm", "agent"), counts={"vm": count})
    fixture.save("vm", complete=False)
    result = setup_artifacts(db, fixture.registry, fixture.owners["agent"], fixture.vm, "shell")
    assert result.retain_previous == bool(count)
    assert not result.unresolved_routes and not result.inputs.deferred


def test_legacy_failure_reasons_are_safe_and_bounded_in_human_views(db, capsys):
    fixture = graph(db, active=("agent",))
    original = fixture.save("agent")
    hostile = "\x1b[31mguest\x1b[0m\x1b]0;window\x07\nforged\r\x9dtitle\x9c" + "x" * (1024 * 1024)
    record = original.model_copy(update={"complete": False, "incomplete_reasons": (hostile,)})
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(record,)), operation="legacy")
    before = db.instance_state.get_applied_slices("agent", "agent")
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    render_artifacts(result)
    rendered = capsys.readouterr().out
    assert "\x1b" not in rendered and "\x9d" not in rendered and "\r" not in rendered
    assert len(rendered.encode()) < 16384
    assert "guest forged" in rendered
    # Machine output preserves source metadata; JSON encoding escapes controls.
    encoded = json.loads(json.dumps(inspection_data(result)))
    assert encoded["owners"][-1]["integrations"][0]["recorded_incomplete_reasons"] == [hostile]
    assert db.instance_state.get_applied_slices("agent", "agent") == before
