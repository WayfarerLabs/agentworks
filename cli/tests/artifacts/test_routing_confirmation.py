"""Confirmed routing stays independent from native application health."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from agentworks.artifacts.application import ArtifactFile, ArtifactSkip
from agentworks.artifacts.inspection import inspect_artifacts
from agentworks.artifacts.publication import publish_artifacts
from agentworks.artifacts.routing import setup_artifacts
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.db import AppliedStateKey, SessionMode, VersionedPayload
from agentworks.errors import StateError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeSetupState, SetupRecord
from agentworks.harness_setup.state import write_native_setup
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from tests.artifacts.test_routing import graph
from tests.native_setup_fixtures import LocalFixtureTransport


@pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")
@pytest.mark.parametrize("receiver", ["admin", "agent", "workspace"])
@pytest.mark.parametrize("failure", ["hook", "skip"])
def test_confirmed_empty_deferrals_do_not_hold_receiver_content(db, tmp_path, receiver, failure):
    fixture = graph(db, active=("vm", receiver))
    record = fixture.save("vm", complete=False)
    source = next(fixture.captures["vm"].inputs.items())
    changes = (
        {"incomplete_reasons": ("fixture native failure",)}
        if failure == "hook"
        else {
            "skipped": (
                ArtifactSkip(path="/fixture/skipped", origins=(source.origin_identity,), reason="fixture skip"),
            )
        }
    )
    record = record.model_copy(update=changes)
    write_native_setup(db, "vm", "vm", NativeSetupState(records=(record,)), operation="fixture")
    routed = setup_artifacts(db, fixture.registry, fixture.owners[receiver], fixture.vm, "shell")
    assert not routed.retain_previous and not routed.unresolved_routes
    assert not routed.inputs.deferred

    # Exercise the publisher with this routing decision: own content can update
    # and retire even though the VM's unrelated native application is incomplete.
    transport = LocalFixtureTransport(tmp_path / receiver)
    assert routed.inputs.local is not None
    item = next(routed.inputs.local.items())
    path = transport.home / "own-rule"
    retired = transport.home / "own-skill"
    files = (
        ArtifactFile(str(path), b"old rule", (item.origin_identity,)),
        ArtifactFile(str(retired), b"old skill", (item.origin_identity,)),
    )
    previous = publish_artifacts(transport, files, (), lambda _: None, roots=(str(transport.home),)).files
    updated = publish_artifacts(
        transport,
        (replace(files[0], data=b"new rule"),),
        previous,
        lambda _: None,
        roots=(str(transport.home),),
        retain_previous=routed.retain_previous,
    )
    assert path.read_bytes() == b"new rule" and not retired.exists()
    assert len(updated.files) == 1 and not updated.skipped


@pytest.mark.parametrize("destination", ["user", "workspace", "session", None])
def test_unreadable_capture_only_holds_recorded_receiver_routes(db, destination):
    fixture = graph(db, active=("vm", "agent", "workspace"))
    record = fixture.save("vm", routes={} if destination is None else {"vm-0": destination}, complete=False)
    db._conn.execute(
        "DELETE FROM instance_records WHERE instance_kind = 'vm' "
        "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
    )
    db._conn.commit()
    for component, facet in (("agent", "user"), ("workspace", "workspace")):
        result = setup_artifacts(db, fixture.registry, fixture.owners[component], fixture.vm, "shell")
        assert result.retain_previous == (destination == facet)
        assert result.unresolved_routes == frozenset(
            route.input_id for route in record.deferred if route.destination == facet
        )


def test_unavailable_inactive_vm_only_flows_to_user(db):
    fixture = graph(db, active=("agent", "workspace"))
    db._conn.execute(
        "DELETE FROM instance_records WHERE instance_kind = 'vm' "
        "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
    )
    db._conn.commit()
    assert setup_artifacts(db, fixture.registry, fixture.owners["agent"], fixture.vm, "shell").retain_previous
    assert not setup_artifacts(db, fixture.registry, fixture.owners["workspace"], fixture.vm, "shell").retain_previous


@pytest.mark.parametrize("destination", ["user", "workspace", "session"])
@pytest.mark.parametrize("capture", ["changed", "missing"])
def test_unknown_session_route_survives_inactive_passthrough(db, destination, capture):
    fixture = graph(db, active=("vm",))
    record = fixture.save("vm", routes={"vm-0": destination})
    original = record.deferred[0].input_id
    if capture == "changed":
        bundle = fixture.registry.lookup("artifact-bundle", "vm-bundle")
        bundle.hints["vm-0"] = type(bundle.hints["vm-0"])(text="changed VM input")
        refreshed = capture_owner(fixture.registry, "vm", "vm", "vm", fixture.owners["vm"].artifacts)
        write_capture(db, "vm", "vm", "vm", refreshed, operation="fixture")
    else:
        db._conn.execute(
            "DELETE FROM instance_records WHERE instance_kind = 'vm' "
            "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
        )
        db._conn.commit()
    result = fixture.route()
    assert result.unresolved_routes == {original}
    assert result.retain_previous and result.unavailable_reasons
    assert all(item.origin.component != "vm" for item in result.inputs.items())
    assert any("vm/vm" in reason and original[:12] in reason for reason in result.unavailable_reasons)


def test_confirmed_user_table_does_not_propagate_opaque_inactive_vm_capture(db):
    fixture = graph(db, active=("agent",))
    fixture.save("agent", inherited=fixture.captures["vm"].inputs)
    db._conn.execute(
        "DELETE FROM instance_records WHERE instance_kind = 'vm' "
        "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
    )
    db._conn.commit()
    result = fixture.route()
    assert not result.retain_previous and not result.unresolved_routes


def test_unconfirmed_session_graph_is_visible_in_read_only_inspection(db):
    fixture = graph(db)
    db.insert_session("review", "workspace", "default", SessionMode.AGENT, agent_name="agent", socket_path="/socket")
    fixture.registry.lookup("vm-template", "vm").artifacts.bundles.append("not-captured")
    owner = SetupInputs(
        "session",
        "review",
        "session",
        {"shell": CapabilityConfig()},
        SecretTarget(vm={}, agent={}, workspace={}, session={}),
    )
    prepared = fixture.route().inputs
    record = SetupRecord(
        component="session",
        integration="shell",
        destination_id="d" * 64,
        declaration=owner.declaration("shell", CapabilityConfig()),
        complete=True,
        artifact_inputs=tuple(item.identity for item in prepared.items()),
    )
    write_native_setup(db, "session", "review", NativeSetupState(records=(record,)), operation="fixture")
    # An unavailable declaration does not make an empty session result healthy.
    before = db._conn.total_changes
    result = inspect_artifacts(db, fixture.registry, session_name="review")
    assert result.owners[-1].integrations[0].status != "current"
    assert db._conn.total_changes == before


@pytest.mark.parametrize("missing", [False, True])
def test_active_user_without_result_reports_unconfirmed_vm_inputs_even_with_no_local_bundle(db, missing):
    fixture = graph(db, active=("vm", "agent"), counts={"agent": 0})
    fixture.save("vm", routes={"vm-0": "user"})
    if missing:
        db._conn.execute(
            "DELETE FROM instance_records WHERE instance_kind = 'vm' "
            "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
        )
        db._conn.commit()
    result = fixture.route()
    assert result.retain_previous and not result.unresolved_routes
    assert any("agent/agent" in reason for reason in result.unavailable_reasons)


@pytest.mark.parametrize("active", [False, True])
def test_opaque_native_result_does_not_become_confirmed_empty_routing(db, active):
    fixture = graph(db, active=("vm", "agent") if active else ("agent",), counts={"vm": 0})
    db.instance_state.replace_applied_slices(
        "vm", "vm", "future", {AppliedStateKey.HARNESS_NATIVE_SETUP: VersionedPayload(99, {"future": True})}
    )
    before = db.instance_state.get_applied_slices("vm", "vm")
    routed = setup_artifacts(db, fixture.registry, fixture.owners["agent"], fixture.vm, "shell")
    assert routed.retain_previous == active and not routed.unresolved_routes
    assert routed.inputs.local and not routed.inputs.deferred
    assert db.instance_state.get_applied_slices("vm", "vm") == before


def test_capture_state_error_keeps_its_specific_repair_hint(db, monkeypatch):
    from agentworks.artifacts.routing import inspect_owner_artifacts

    fixture = graph(db)
    unavailable = StateError("fixture unreadable capture", hint="fixture recovery sentinel")

    def refuse(*args, **kwargs):
        raise unavailable

    monkeypatch.setattr("agentworks.artifacts.routing.read_captures", refuse)
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    assert view.capture_status == "unavailable" and view.repair_hint is unavailable.hint
