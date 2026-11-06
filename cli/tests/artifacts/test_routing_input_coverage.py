"""Routing acknowledgements apply only to the input identities they covered."""

import sys

import pytest

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.declarations import HintArtifactSpec
from agentworks.artifacts.inspection import inspect_artifacts
from agentworks.artifacts.routing import setup_artifacts
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.model import NativeSetupState
from agentworks.harness_setup.readiness import evaluate_setup
from agentworks.harness_setup.state import write_native_setup
from agentworks.origin import Origin
from agentworks.vms.templates import ResolvedVMTemplate
from tests.artifacts.test_routing import graph
from tests.native_setup_fixtures import LocalFixtureTransport


@pytest.mark.parametrize("receiver", ["admin", "agent", "workspace"])
@pytest.mark.parametrize("previous_count", [0, 1])
def test_previous_input_table_cannot_confirm_a_larger_capture(db, receiver, previous_count):
    fixture = graph(db, active=("vm", receiver), counts={"vm": previous_count})
    previous = fixture.save("vm")
    fixture.registry.lookup("artifact-bundle", "vm-bundle").hints["added"] = HintArtifactSpec(text="new input")
    captured = capture_owner(fixture.registry, "vm", "vm", "vm", fixture.owners["vm"].artifacts)
    write_capture(db, "vm", "vm", "vm", captured, operation="vm-reinit")
    write_native_setup(
        db,
        "vm",
        "vm",
        NativeSetupState(records=(previous.model_copy(update={"complete": False}),)),
        operation="vm-reinit",
    )
    result = setup_artifacts(db, fixture.registry, fixture.owners[receiver], fixture.vm, "shell")
    assert result.retain_previous and result.unavailable_reasons
    assert not result.unresolved_routes


@pytest.mark.parametrize("receiver", ["agent", "workspace"])
@pytest.mark.parametrize("readable", [True, False])
def test_session_join_reports_vm_routes_not_covered_by_active_receiver(db, receiver, readable):
    fixture = graph(db, active=("vm", receiver), counts={"agent": 0, "workspace": 0})
    fixture.save("vm", routes={"vm-0": fixture.owners[receiver].facet})
    fixture.save(receiver)
    if not readable:
        db._conn.execute(
            "DELETE FROM instance_records WHERE instance_kind = 'vm' "
            "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
        )
        db._conn.commit()
    result = fixture.route()
    assert result.retain_previous and result.unavailable_reasons
    assert not result.inputs.deferred


@pytest.mark.parametrize("receiver", ["agent", "workspace"])
def test_upstream_failure_cannot_make_stale_receiver_current(db, receiver):
    fixture = graph(db, active=("vm", receiver), counts={receiver: 0})
    previous = fixture.save("vm", routes={"vm-0": fixture.owners[receiver].facet})
    fixture.save(receiver)
    selector = {"agent_name": "agent"} if receiver == "agent" else {"workspace_name": "workspace"}
    before = inspect_artifacts(db, fixture.registry, **selector).owners[-1].integrations[0]
    assert before.status == "stale"
    fixture.registry.lookup("artifact-bundle", "vm-bundle").hints["vm-0"] = HintArtifactSpec(text="changed")
    captured = capture_owner(fixture.registry, "vm", "vm", "vm", fixture.owners["vm"].artifacts)
    write_capture(db, "vm", "vm", "vm", captured, operation="vm-reinit")
    write_native_setup(
        db,
        "vm",
        "vm",
        NativeSetupState(records=(previous.model_copy(update={"complete": False}),)),
        operation="vm-reinit",
    )
    changes = db._conn.total_changes
    after = inspect_artifacts(db, fixture.registry, **selector).owners[-1].integrations[0]
    assert after.status != "current"
    assert db._conn.total_changes == changes


@pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux user filesystem execution")
@pytest.mark.parametrize("producer", ["active", "inactive"])
@pytest.mark.parametrize("previous_receiver", [False, True])
def test_receiver_records_unknown_full_input_set_without_confirming_partial_inputs(
    db, tmp_path, monkeypatch, producer, previous_receiver
):
    fixture = graph(db, active=("vm", "agent") if producer == "active" else ("agent",), counts={"agent": 0})
    if previous_receiver:
        fixture.save("agent", inherited=fixture.captures["vm"].inputs)
    if producer == "active":
        record = fixture.save("vm", complete=False).model_copy(update={"artifact_inputs": None})
        write_native_setup(db, "vm", "vm", NativeSetupState(records=(record,)), operation="vm-reinit")
    else:
        db._conn.execute(
            "DELETE FROM instance_records WHERE instance_kind = 'vm' "
            "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
        )
        db._conn.commit()
    fixture.registry.add(
        "harness-integration", "shell", HarnessIntegrationEntry(name="shell"), Origin.built_in(source="coverage")
    )
    fixture.registry.finalize(probe_host_readiness=False)
    monkeypatch.setattr(
        "agentworks.vms.templates.resolve_live_template",
        lambda *args, **kwargs: ResolvedVMTemplate(
            "vm",
            artifacts=fixture.owners["vm"].artifacts,
            harness_integrations=dict(fixture.owners["vm"].activations),
        ),
    )
    monkeypatch.setattr(ShellIntegration, "user_init", lambda self, call: ArtifactApplication())
    runner = LocalFixtureTransport(tmp_path / "guest")
    invocation = UserSetupInvocation(
        vm=fixture.vm,
        runner=runner,
        prior=None,
        checkpoint=lambda claims: None,
        username="worker",
        home=str(runner.home),
    )
    state = run_setup(db, fixture.registry, fixture.owners["agent"], invocation, failures=[], operation="agent-init")
    (record,) = state.records
    assert not record.complete and record.incomplete_reasons
    assert not record.artifact_files and not record.skipped
    assert record.artifact_inputs is None
    monkeypatch.setattr(
        "agentworks.harness_setup.readiness.destination_id", lambda *args, **kwargs: record.destination_id
    )
    assert (
        evaluate_setup(db, fixture.owners["agent"], "shell", vm=fixture.vm, runner=runner, remediation="fixture").status
        == "incomplete"
    )
    routed = fixture.route()
    assert routed.retain_previous and routed.unavailable_reasons


@pytest.mark.parametrize("recorded", [False, True])
def test_unknown_vm_passthrough_with_no_confirmed_receiver_warns_session(db, recorded):
    fixture = graph(db, active=("agent",), counts={"agent": 0})
    if recorded:
        receiver = fixture.save("agent", complete=False).model_copy(update={"artifact_inputs": None})
        write_native_setup(db, "agent", "agent", NativeSetupState(records=(receiver,)), operation="agent-init")
    db._conn.execute(
        "DELETE FROM instance_records WHERE instance_kind = 'vm' "
        "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
    )
    db._conn.commit()
    routed = fixture.route()
    assert routed.retain_previous and routed.unavailable_reasons
