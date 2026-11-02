"""Incomplete and unsupported owner evidence stays actionable without launch gates."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactSkip
from agentworks.artifacts.inspection import _integration_metadata
from agentworks.artifacts.routing import _remediation, inspect_owner_artifacts
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
    fixture = graph(db, active=("agent",) if active else (), counts={"vm": 0, "admin": 0, "agent": 0, "workspace": 0})
    record = fixture.save("agent", complete=False)
    record = record.model_copy(
        update={
            "pending_cleanup": pending,
            "claims": (NativeClaim(role="plugin", identifier="retained", destination="/home/worker"),),
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
    assert view.repair_hint == _remediation(view) == canonical.value.hint
    assert metadata.reason == view.diagnostic
    assert {item.origin.component for item in result.inputs.items()} == {"agent", "workspace"}
    assert warnings.called
    assert db.instance_state.get_applied_slices("vm", "vm") == before
