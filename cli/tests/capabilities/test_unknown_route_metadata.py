"""Unavailable upstream IDs survive real setup publication and later session joins."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.artifacts.declarations import HintArtifactSpec
from agentworks.artifacts.routing import owner_remediation, session_ancestor_views, setup_artifacts
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.capabilities.harness_integration import ShellIntegration
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.outcomes import SetupFailure
from agentworks.origin import Origin
from agentworks.schema import CapabilityConfig
from tests.artifacts.test_routing import Graph, graph
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


def _invoke(
    fixture: Graph, target: LocalFixtureTransport, component: str
) -> UserSetupInvocation | WorkspaceSetupInvocation:
    if component == "workspace":
        import grp
        import os

        fixture.db.update_workspace_path("workspace", str(target.home))
        workspace = fixture.db.get_workspace("workspace")
        assert workspace is not None
        fixture.workspace = workspace
        return WorkspaceSetupInvocation(
            vm=fixture.vm,
            access=(recorded := target.access()),
            files=recorded.files(),
            prior=None,
            checkpoint=lambda _: None,
            workspace_name="workspace",
            root=str(target.home),
            linux_group=grp.getgrgid(os.getgid()).gr_name,
        )
    return UserSetupInvocation(
        vm=fixture.vm,
        access=(recorded := target.access()),
        files=recorded.files(),
        prior=None,
        checkpoint=lambda _: None,
        username="worker",
        home=str(target.home),
    )


def _registry(fixture: Graph) -> None:
    fixture.registry.add(
        "harness-integration", "shell", HarnessIntegrationEntry(name="shell"), Origin.built_in(source="unknown-routing")
    )
    fixture.registry.finalize(probe_host_readiness=False)


def _missing_vm_capture(fixture: Graph) -> None:
    fixture.db._conn.execute(
        "DELETE FROM instance_records WHERE instance_kind = 'vm' "
        "AND instance_name = 'vm' AND record_key = 'artifact-inputs'"
    )
    fixture.db._conn.commit()


@pytest.mark.parametrize("component", ["agent", "workspace"])
@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("own_inputs", [False, True])
def test_unreadable_explicit_input_is_forwarded_even_without_previous_result(
    db, tmp_path, monkeypatch, component, previous, own_inputs
):
    facet = "user" if component == "agent" else "workspace"
    fixture = graph(
        db,
        active=("vm", component),
        counts={
            "vm": 1,
            "agent": int(own_inputs) if component == "agent" else 0,
            "workspace": int(own_inputs) if component == "workspace" else 0,
        },
    )
    _registry(fixture)
    fixture.save("vm", routes={"vm-0": facet})
    source = fixture.captures["vm"]
    missing = next(source.inputs.items())
    target = LocalFixtureTransport(tmp_path / "guest")
    invocation = _invoke(fixture, target, component)
    inputs = fixture.owners[component]
    failures: list[SetupFailure] = []
    if previous:
        # Produce a real prior deferral-only result for A, plus ordinary files
        # for local inputs. The next attempt must deduplicate A's metadata.
        def defer_upstream(self, call):
            return ArtifactApplication(
                files=tuple(
                    ArtifactFile(
                        str(target.home / item.content.name), item.content.text.encode(), (item.origin_identity,)
                    )
                    for item in call.artifacts.items()
                    if item.origin.component == component
                ),
                deferred=tuple(
                    ArtifactDeferral(input_id=item.identity, destination="session", reason="fixture upstream deferral")
                    for item in call.artifacts.items()
                    if item.origin.component == "vm"
                ),
            )

        method = "user_init" if component == "agent" else "workspace_init"
        with monkeypatch.context() as prior_hook:
            prior_hook.setattr(ShellIntegration, method, defer_upstream)
            prior = run_setup(db, fixture.registry, inputs, invocation, failures=failures, operation="initial-setup")
        assert prior.records[0].complete and len(prior.records[0].deferred) == 1
    _missing_vm_capture(fixture)
    if own_inputs:
        fixture.registry.lookup("artifact-bundle", f"{component}-bundle").hints["independent-new"] = HintArtifactSpec(
            text="INDEPENDENT-LOCAL-CONTENT"
        )
        captured = capture_owner(fixture.registry, inputs.kind, inputs.name, component, inputs.artifacts)
        write_capture(db, inputs.kind, inputs.name, component, captured, operation="local-refresh")
    expected_routing = setup_artifacts(db, fixture.registry, inputs, fixture.vm, "shell")
    assert expected_routing.unresolved_routes == {missing.identity}
    actual = run_setup(db, fixture.registry, inputs, invocation, failures=failures, operation="retry-setup")
    record = actual.records[0]
    assert failures == [] and record.complete
    assert record.artifact_inputs is not None and missing.identity in record.artifact_inputs
    assert [item.input_id for item in record.deferred] == [missing.identity]
    assert record.deferred[0].destination == "session"
    assert not record.incomplete_reasons
    assert expected_routing.unavailable_reasons[0] in record.deferred[0].reason
    if own_inputs:
        assert any(
            Path(file.path).read_bytes().rstrip(b"\n") == b"INDEPENDENT-LOCAL-CONTENT" for file in record.artifact_files
        )
    routed = fixture.route()
    assert routed.unresolved_routes == {missing.identity} and routed.retain_previous
    views = session_ancestor_views(db, fixture.registry, fixture.vm, fixture.workspace, "agent", "shell")
    receiver = views[1] if component == "agent" else views[2]
    source_repair, receiver_repair = owner_remediation(views[0]), owner_remediation(receiver)
    reason = next(reason for reason in routed.unavailable_reasons if record.deferred[0].reason in reason)
    assert reason.index(source_repair) < reason.index(receiver_repair)

    # Restoring the source and applying the receiving facet replaces unknown
    # metadata with the known current handling result, without stale deferrals.
    write_capture(db, "vm", "vm", "vm", source, operation="source-repair")
    healed = run_setup(db, fixture.registry, inputs, invocation, failures=failures, operation="healed-setup").records[0]
    assert healed.complete and not healed.deferred and not healed.incomplete_reasons
    assert healed.artifact_inputs is not None and missing.identity in healed.artifact_inputs
    assert not fixture.route().unresolved_routes


@pytest.mark.parametrize("previous", [False, True])
def test_unknown_input_set_records_source_repair_for_fresh_and_existing_receivers(db, tmp_path, previous):
    fixture = graph(db, active=("agent",), counts={"workspace": 0})
    _registry(fixture)
    target = LocalFixtureTransport(tmp_path / "guest")
    invocation = _invoke(fixture, target, "agent")
    inputs = fixture.owners["agent"]
    failures: list[SetupFailure] = []
    before = None
    if previous:
        before = run_setup(
            db, fixture.registry, inputs, invocation, failures=failures, operation="initial-setup"
        ).records[0]
    fixture.registry.lookup("vm-template", "vm").harness_integrations["shell"] = CapabilityConfig()
    expected = setup_artifacts(db, fixture.registry, inputs, fixture.vm, "shell")
    assert expected.retain_previous and not expected.unresolved_routes and expected.unavailable_reasons
    actual = run_setup(db, fixture.registry, inputs, invocation, failures=failures, operation="retry-setup").records[0]
    assert failures == []
    assert actual.complete is (not previous)
    assert actual.artifact_inputs is not None
    assert not actual.incomplete_reasons
    if previous:
        assert before is not None and actual.skipped
        assert actual.artifact_files == before.artifact_files
    else:
        assert actual.artifact_files and not actual.skipped


def test_saved_unavailable_route_reason_is_terminal_safe_and_does_not_rewrite_state(db, captured_output):
    from agentworks.harness_setup.model import NativeSetupState
    from agentworks.harness_setup.state import write_native_setup

    fixture = graph(db, active=("vm", "agent"), counts={"agent": 0, "workspace": 0})
    fixture.save("vm", routes={"vm-0": "user"})
    previous = fixture.save("agent", inherited=fixture.captures["vm"].inputs, routes={"vm-0": "session"})
    hostile = "\x1b[31mguest\x1b[0m\x1b]0;window-title\x07\n" + "x" * (1024 * 1024)
    previous = previous.model_copy(update={"deferred": (previous.deferred[0].model_copy(update={"reason": hostile}),)})
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(previous,)), operation="legacy")
    _missing_vm_capture(fixture)
    before = db.instance_state.get_applied_slices("agent", "agent")
    result = fixture.route()
    assert result.unresolved_routes == {previous.deferred[0].input_id}
    assert result.unavailable_reasons and captured_output.warnings
    for reason in result.unavailable_reasons:
        assert len(reason.encode()) <= 2048
        assert "\x1b" not in reason and "\x07" not in reason and "\n" not in reason
    assert all("\x1b" not in message and "\x07" not in message for message in captured_output.warnings)
    assert db.instance_state.get_applied_slices("agent", "agent") == before
