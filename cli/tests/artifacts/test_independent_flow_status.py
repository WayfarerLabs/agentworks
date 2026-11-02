"""Incomplete and unsupported owner evidence stays actionable without launch gates."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactSkip
from agentworks.artifacts.inspection import _integration_metadata
from agentworks.artifacts.routing import inspect_owner_artifacts, owner_remediation
from agentworks.artifacts.state import UnsupportedArtifactCaptureVersionError, read_captures
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.harness_setup.model import NativeClaim, NativeSetupState
from agentworks.harness_setup.state import (
    UnsupportedNativeSetupVersionError,
    read_native_setup,
    write_native_setup,
)
from tests.artifacts.test_routing import graph


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("active", [False, True])
def test_native_only_incomplete_facet_reports_without_requiring_artifact_inputs(db, monkeypatch, pending, active):
    from uuid import uuid4

    fixture = graph(db, active=("agent",) if active else (), counts={"vm": 0, "admin": 0, "agent": 0, "workspace": 0})
    reason = f"plugin-{uuid4().hex}: association differs"
    record = fixture.save("agent", complete=False)
    record = record.model_copy(
        update={
            "pending_cleanup": pending,
            "claims": (NativeClaim(role="plugin", identifier="retained", destination="/home/worker"),),
            "incomplete_reasons": (reason,),
        }
    )
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(record,)), operation="fixture")
    warnings = Mock()
    monkeypatch.setattr("agentworks.output.warn", warnings)
    before = db.instance_state.get_applied_slices("agent", "agent")

    result = fixture.route()
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    assert not result.inputs and not view.prepared
    assert view.status == ("incomplete" if active else "retirement") and view.record == record
    assert warnings.called
    assert reason in view.reason
    assert any(reason in call.args[0] for call in warnings.call_args_list)
    assert db.instance_state.get_applied_slices("agent", "agent") == before


def test_skipped_publication_is_incomplete_with_original_skip_evidence(db):
    fixture = graph(db, active=("agent",))
    item = next(fixture.captures["agent"].inputs.items())
    skipped = ArtifactSkip(path="/home/worker/AGENTS.md", origins=(item.origin_identity,), reason="fixture conflict")
    record = fixture.save("agent")
    record = record.model_copy(update={"skipped": (skipped,)})
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(record,)), operation="fixture")

    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    metadata = _integration_metadata(fixture.owners["agent"], "shell", view)
    assert view.status == metadata.status == "incomplete"
    assert metadata.recorded_skipped == (skipped,)
    assert not metadata.recorded_handled and not metadata.recorded_deferred
    assert view.record is not None and not view.record.pending_cleanup


def test_native_only_entry_diagnostics_reach_session_and_both_inspection_outputs_without_io(db, monkeypatch, capsys):
    import json
    from uuid import uuid4

    from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
    from agentworks.package_sources import PackageCapture

    fixture = graph(db, active=("agent",), counts={"vm": 0, "admin": 0, "agent": 0, "workspace": 0})
    reasons = tuple(f"plugin-{uuid4().hex}: source association differs" for _ in range(5))
    native_body = uuid4().hex
    record = fixture.save("agent", complete=False)
    record = record.model_copy(
        update={"incomplete_reasons": reasons, "declaration": {"config": {"private": native_body}}}
    )
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(record,)), operation="fixture")
    before = db.instance_state.get_applied_slices("agent", "agent")

    def forbidden(*args, **kwargs):
        pytest.fail("read-only diagnostics acquired sources or mutated setup state")

    monkeypatch.setattr(PackageCapture, "capture", forbidden)
    monkeypatch.setattr(type(db.instance_state), "replace_applied_slices", forbidden)
    monkeypatch.setattr("agentworks.harness_setup.dispatch.run_setup", forbidden)
    warnings = Mock()
    monkeypatch.setattr("agentworks.output.warn", warnings)

    assert not fixture.route().inputs
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["agent"], "shell")
    assert view.status == "incomplete" and view.record == record
    warning = " ".join(call.args[0] for call in warnings.call_args_list)
    assert reasons[0] in warning and "agent/agent" in warning
    assert owner_remediation(view) in warning
    inspected = inspect_artifacts(db, fixture.registry, agent_name="agent")
    metadata = inspected.owners[-1].integrations[0]
    assert metadata.recorded_incomplete_reasons == reasons
    assert owner_remediation(view) in metadata.reason
    encoded = inspection_data(inspected)
    decoded = json.loads(json.dumps(encoded))
    assert decoded["owners"][-1]["integrations"][0]["recorded_incomplete_reasons"] == list(reasons)
    render_artifacts(inspected)
    rendered = capsys.readouterr().out
    assert all(reason in rendered for reason in reasons)
    assert native_body not in json.dumps(encoded) + rendered + warning
    assert db.instance_state.get_applied_slices("agent", "agent") == before


@pytest.mark.parametrize("status", ["incomplete", "retirement"])
def test_session_inspection_repairs_its_actual_session_with_restart(db, monkeypatch, status):
    from uuid import uuid4

    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.artifacts.inspection import inspect_artifacts
    from agentworks.db import SessionMode
    from agentworks.harness_setup.model import SetupRecord
    from agentworks.package_sources import PackageCapture

    fixture = graph(db)
    name = f"review-{uuid4().hex[:8]}"
    integration = "shell" if status == "incomplete" else "codex"
    db.insert_session(name, "workspace", "default", SessionMode.AGENT, agent_name="agent", socket_path="/socket")
    reason = f"entry-{uuid4().hex}: native conflict"
    record = SetupRecord(
        component="session",
        integration=integration,
        destination_id="d" * 64,
        declaration={},
        incomplete_reasons=(reason,),
        artifact_files=()
        if status == "incomplete"
        else (OwnedArtifactFile(path="/home/worker/session/file", sha256="a" * 64, origins=("b" * 64,)),),
    )
    write_native_setup(db, "session", name, NativeSetupState(records=(record,)), operation="fixture")
    before = db.instance_state.get_applied_slices("session", name)

    def forbidden(*args, **kwargs):
        pytest.fail("session inspection acquired sources or mutated state")

    monkeypatch.setattr(PackageCapture, "capture", forbidden)
    monkeypatch.setattr(type(db.instance_state), "replace_applied_slices", forbidden)
    inspected = inspect_artifacts(db, fixture.registry, session_name=name)
    owner = inspected.owners[-1]
    metadata = next(item for item in owner.integrations if item.name == integration)
    assert owner.scope == "session" and owner.resource_name == name
    assert metadata.status == status
    assert metadata.recorded_incomplete_reasons == (reason,)
    assert f"agw session restart {name}" in metadata.reason
    assert db.instance_state.get_applied_slices("session", name) == before


@pytest.mark.parametrize("domain", [AppliedStateKey.HARNESS_NATIVE_SETUP, AppliedStateKey.ARTIFACT_INPUTS])
def test_unsupported_ancestor_evidence_preserves_domain_version_repair(db, monkeypatch, domain):
    fixture = graph(db, active=("vm",))
    db.instance_state.replace_applied_slices("vm", "vm", "fixture", {domain: VersionedPayload(999, {})})
    reader, error_type = (
        (read_native_setup, UnsupportedNativeSetupVersionError)
        if domain is AppliedStateKey.HARNESS_NATIVE_SETUP
        else (read_captures, UnsupportedArtifactCaptureVersionError)
    )
    with pytest.raises(error_type) as canonical:
        reader(db, "vm", "vm")
    before = db.instance_state.get_applied_slices("vm", "vm")
    warnings = Mock()
    monkeypatch.setattr("agentworks.output.warn", warnings)

    result = fixture.route()
    view = inspect_owner_artifacts(db, fixture.registry, fixture.owners["vm"], "shell")
    metadata = _integration_metadata(fixture.owners["vm"], "shell", view)
    assert view.status == "unavailable"
    assert view.repair_hint == owner_remediation(view) == canonical.value.hint
    assert metadata.reason == view.diagnostic
    assert {item.origin.component for item in result.inputs.items()} == {"agent", "workspace"}
    assert warnings.called
    assert db.instance_state.get_applied_slices("vm", "vm") == before
