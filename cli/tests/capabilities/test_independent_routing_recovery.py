"""Missing ancestor input cannot retire files or poison local setup forever."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactFile
from agentworks.artifacts.declarations import HintArtifactSpec
from agentworks.artifacts.inspection import inspect_artifacts
from agentworks.artifacts.routing import setup_artifacts
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.capabilities.harness_integration import ShellIntegration
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.model import NativeSetupState
from agentworks.harness_setup.state import write_native_setup
from agentworks.schema import CapabilityConfig
from tests.artifacts.test_routing import graph
from tests.capabilities.test_unknown_route_metadata import _invoke, _registry
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux guest file publication")


@pytest.mark.parametrize("damage", ["missing", "version", "malformed", "stale"])
def test_capture_without_current_retirement_authority_holds_previous_inherited_files(db, tmp_path, damage):
    fixture = graph(db, active=("agent",), counts={"agent": 0})
    _registry(fixture)
    target = LocalFixtureTransport(tmp_path / "guest")
    invocation = _invoke(fixture, target, "agent")
    inputs = fixture.owners["agent"]
    before = run_setup(db, fixture.registry, inputs, invocation, failures=[], operation="initial").records[0]
    snapshots = {file.path: Path(file.path).read_bytes() for file in before.artifact_files}
    assert snapshots
    # The VM now claims to have handled everything itself, without a deferral.
    # A missing/stale capture must not certify an empty input set at the user.
    vm_inputs = fixture.registry.lookup("vm-template", "vm")
    vm_inputs.harness_integrations["shell"] = CapabilityConfig()
    fixture.owners["vm"] = replace(fixture.owners["vm"], activations={"shell": CapabilityConfig()})
    fixture.save("vm")
    if damage == "missing":
        db._conn.execute(
            "DELETE FROM instance_records WHERE instance_kind='vm' "
            "AND instance_name='vm' AND record_key='artifact-inputs'"
        )
        db._conn.commit()
    elif damage in ("version", "malformed"):
        db.instance_state.replace_applied_slices(
            "vm",
            "vm",
            "damage",
            {AppliedStateKey.ARTIFACT_INPUTS: VersionedPayload(99 if damage == "version" else 3, {})},
        )
    else:
        fixture.registry.lookup("artifact-bundle", "vm-bundle").hints.clear()
        empty = capture_owner(fixture.registry, "vm", "vm", "vm", vm_inputs.artifacts)
        write_capture(db, "vm", "vm", "vm", empty, operation="reduced")
        fixture.registry.lookup("artifact-bundle", "vm-bundle").hints["vm-0"] = HintArtifactSpec(text="restored")
    assert setup_artifacts(db, fixture.registry, inputs, fixture.vm, "shell").retain_previous
    current = run_setup(db, fixture.registry, inputs, invocation, failures=[], operation="retry").records[0]
    assert current.skipped and not current.complete
    assert {file.path: Path(file.path).read_bytes() for file in current.artifact_files} == snapshots
    assert inspect_artifacts(db, fixture.registry, agent_name="agent").owners[-1].integrations[0].status != "current"


@pytest.mark.parametrize("previous_capture_replaced", [False, True])
def test_unknown_vm_holds_only_unclassified_origins_while_local_changes_converge(
    db, tmp_path, monkeypatch, previous_capture_replaced
):
    fixture = graph(db, active=("vm", "agent"), counts={"agent": 2})
    _registry(fixture)
    fixture.save("vm", routes={"vm-0": "user"})
    target = LocalFixtureTransport(tmp_path / "guest")
    invocation = _invoke(fixture, target, "agent")

    def apply(self, call):
        return ArtifactApplication(
            files=tuple(
                ArtifactFile(str(target.home / item.content.name), item.content.text.encode(), (item.origin_identity,))
                for item in call.artifacts.items()
            )
        )

    monkeypatch.setattr(ShellIntegration, "user_init", apply)
    inputs = fixture.owners["agent"]
    initial = run_setup(db, fixture.registry, inputs, invocation, failures=[], operation="initial").records[0]
    vm_bytes = (target.home / "vm-0").read_bytes()
    hints = fixture.registry.lookup("artifact-bundle", "agent-bundle").hints
    hints["agent-0"] = HintArtifactSpec(text="local replacement")
    del hints["agent-1"]
    snapshot = capture_owner(fixture.registry, "agent", "agent", "agent", inputs.artifacts)
    retry = replace(inputs, artifact_snapshot=snapshot)
    if previous_capture_replaced:
        # An interrupted earlier attempt can already have replaced the capture.
        # Its removed file's origin hash then has no supported owner attribution.
        write_capture(db, "agent", "agent", "agent", snapshot, operation="earlier-attempt")
    old = fixture.save("vm", complete=False)
    write_native_setup(
        db, "vm", "vm", NativeSetupState(records=(old.model_copy(update={"artifact_inputs": None}),)), operation="fail"
    )
    actual = run_setup(db, fixture.registry, retry, invocation, failures=[], operation="retry").records[0]
    assert (target.home / "vm-0").read_bytes() == vm_bytes
    assert (target.home / "agent-0").read_bytes() == b"local replacement"
    assert (target.home / "agent-1").exists() is previous_capture_replaced
    assert {item.path for item in actual.skipped} == {str(target.home / "vm-0")} | (
        {str(target.home / "agent-1")} if previous_capture_replaced else set()
    )
    assert len(actual.artifact_files) == len(initial.artifact_files) - int(not previous_capture_replaced)
    # Once the upstream owner confirms its route, an ordinary agent reinit can
    # safely retire the obsolete file even if its local attribution was lost.
    fixture.save("vm", routes={"vm-0": "user"})
    healed = run_setup(db, fixture.registry, retry, invocation, failures=[], operation="retry-after-vm-reinit").records[
        0
    ]
    assert healed.complete and not healed.skipped
    assert not (target.home / "agent-1").exists()


@pytest.mark.parametrize("next_route", [None, "workspace"])
def test_fresh_workspace_local_setup_is_not_permanently_failed_by_unknown_incoming_routes(db, tmp_path, next_route):
    fixture = graph(db, active=("vm", "workspace"))
    _registry(fixture)
    failed = fixture.save("vm", complete=False).model_copy(update={"artifact_inputs": None})
    write_native_setup(db, "vm", "vm", NativeSetupState(records=(failed,)), operation="fail")
    target = LocalFixtureTransport(tmp_path / "guest")
    invocation = _invoke(fixture, target, "workspace")
    inputs = fixture.owners["workspace"]
    result = run_setup(db, fixture.registry, inputs, invocation, failures=[], operation="create").records[0]
    assert result.complete and not result.incomplete_reasons and not result.skipped
    before = inspect_artifacts(db, fixture.registry, workspace_name="workspace").owners[-1].integrations[0]
    assert before.status != "current"
    fixture.save("vm", routes=None if next_route is None else {"vm-0": next_route})
    state_before = db.instance_state.get_applied_slices("workspace", "workspace")
    after = inspect_artifacts(db, fixture.registry, workspace_name="workspace").owners[-1].integrations[0]
    # No native application is needed when the healed VM handles its own input.
    # Newly routed content still needs actual workspace application and stays stale.
    assert after.status == ("current" if next_route is None else "stale")
    assert db.instance_state.get_applied_slices("workspace", "workspace") == state_before
