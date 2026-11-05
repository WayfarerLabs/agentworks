"""Actual user-file publication survives unacknowledged or unreadable VM routes."""

import hashlib
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.artifacts.application import (
    ArtifactApplication,
    ArtifactDeferral,
    ArtifactFile,
    ArtifactPublication,
    OwnedArtifactFile,
)
from agentworks.artifacts.declarations import ArtifactsConfig
from agentworks.artifacts.publication import publish_artifacts
from agentworks.capabilities.harness_integration import ShellIntegration
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, VMSetupInvocation
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.errors import ExternalError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeSetupState, SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure
from agentworks.origin import Origin
from agentworks.schema import CapabilityConfig
from agentworks.vms.templates import ResolvedVMTemplate
from tests.artifacts.test_routing import graph
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux user filesystem execution")


@pytest.fixture
def fallback_placements(db, tmp_path, monkeypatch):
    fixture = graph(db, active=("agent",), counts={"vm": 2, "admin": 0, "agent": 0, "workspace": 0})
    fixture.registry.add(
        "harness-integration", "shell", HarnessIntegrationEntry(name="shell"), Origin.built_in(source="private-routing")
    )
    fixture.registry.finalize(probe_host_readiness=False)
    target = LocalFixtureTransport(tmp_path / "guest")
    user = UserSetupInvocation(
        vm=fixture.vm,
        runner=target,
        prior=None,
        checkpoint=lambda claims: None,
        username="worker",
        home=str(target.home),
    )
    machine = VMSetupInvocation(vm=fixture.vm, runner=target, prior=None, checkpoint=lambda claims: None)
    control = {"template": ResolvedVMTemplate("vm", artifacts=fixture.owners["vm"].artifacts), "fail": True}
    monkeypatch.setattr("agentworks.vms.templates.resolve_live_template", lambda *a, **k: control["template"])

    def user_setup(self, call):
        return ArtifactApplication(
            files=tuple(
                ArtifactFile(
                    call.home + "/" + item.content.name + ".md", item.content.text.encode(), (item.origin_identity,)
                )
                for item in call.artifacts.items()
            )
        )

    def vm_setup(self, call):
        first, second = tuple(call.artifacts.items())
        return ArtifactApplication(
            files=(
                ArtifactFile(
                    "/opt/agentworks/artifacts/first.md", first.content.text.encode(), (first.origin_identity,)
                ),
            ),
            deferred=(ArtifactDeferral(input_id=second.identity, destination="user", reason="fixture user route"),),
        )

    monkeypatch.setattr(ShellIntegration, "user_init", user_setup)
    monkeypatch.setattr(ShellIntegration, "vm_init", vm_setup)
    real_publish = publish_artifacts

    def publish(runner, desired, previous, checkpoint, *, root=False, **kwargs):
        if not root:
            return real_publish(runner, desired, previous, checkpoint, root=False, **kwargs)
        # Only the root producer is simulated. Both descendant files are created,
        # fingerprinted and retired through the actual NativeFiles program.
        owned = tuple(
            OwnedArtifactFile(path=file.path, sha256=hashlib.sha256(file.data).hexdigest(), origins=file.origins)
            for file in desired
        )
        checkpoint(owned)
        if control["fail"]:
            raise ExternalError("fixture publication stopped after its first confirmed file")
        return ArtifactPublication(files=owned)

    monkeypatch.setattr("agentworks.harness_setup.dispatch.publish_artifacts", publish)
    initial = run_setup(db, fixture.registry, fixture.owners["agent"], user, failures=[], operation="agent-init")
    (record,) = initial.records
    assert record.complete and len(record.artifact_files) == 2
    files = {Path(file.path): Path(file.path).read_bytes() for file in record.artifact_files}
    return SimpleNamespace(
        fixture=fixture, target=target, user=user, machine=machine, control=control, files=files, original=record
    )


def _activate_producer(case: SimpleNamespace) -> tuple[SetupInputs, SetupRecord]:
    inputs = replace(case.fixture.owners["vm"], activations={"shell": CapabilityConfig()})
    case.control["template"] = ResolvedVMTemplate(
        "vm", artifacts=inputs.artifacts, harness_integrations=dict(inputs.activations)
    )
    failures: list[SetupFailure] = []
    state = run_setup(
        case.fixture.db, case.fixture.registry, inputs, case.machine, failures=failures, operation="vm-reinit"
    )
    assert len(failures) == 1
    (record,) = state.records
    assert not record.complete and len(record.artifact_files) == 1 and not record.deferred
    return inputs, record


def test_fresh_producer_cannot_acknowledge_inputs_before_publication_finishes(fallback_placements):
    _, record = _activate_producer(fallback_placements)
    assert record.artifact_inputs is None


@pytest.mark.parametrize("legacy_marker", [False, True])
def test_partial_fresh_producer_preserves_both_existing_user_files(fallback_placements, legacy_marker):
    case = fallback_placements
    inputs, producer = _activate_producer(case)
    if legacy_marker:
        producer = producer.model_copy(
            update={"artifact_inputs": tuple(item.identity for item in case.fixture.captures["vm"].inputs.items())}
        )
        case.fixture.db.instance_state.replace_applied_slices(
            "vm",
            "vm",
            "legacy",
            {
                AppliedStateKey.HARNESS_NATIVE_SETUP: VersionedPayload(
                    3, NativeSetupState(records=(producer,)).model_dump(mode="json")
                )
            },
        )
    failures: list[SetupFailure] = []
    state = run_setup(
        case.fixture.db,
        case.fixture.registry,
        case.fixture.owners["agent"],
        case.user,
        failures=failures,
        operation="agent-reinit",
    )
    for path, before in case.files.items():
        assert path.is_file() and path.read_bytes() == before
    (record,) = state.records
    assert record.artifact_files == case.original.artifact_files
    assert not record.complete and record.skipped and failures == []

    # A later confirmed handling result authorizes real retirement of only the
    # input the VM now handles, rather than keeping an unknown route forever.
    case.control["fail"] = False
    confirmed = run_setup(
        case.fixture.db, case.fixture.registry, inputs, case.machine, failures=[], operation="vm-reinit"
    )
    assert confirmed.records[0].complete and confirmed.records[0].artifact_inputs is not None
    healed = run_setup(
        case.fixture.db,
        case.fixture.registry,
        case.fixture.owners["agent"],
        case.user,
        failures=[],
        operation="agent-reinit",
    )
    assert healed.records[0].complete and len(healed.records[0].artifact_files) == 1
    (remaining,) = healed.records[0].artifact_files
    assert Path(remaining.path).read_bytes() == case.files[Path(remaining.path)]
    assert sum(path.exists() for path in case.files) == 1


@pytest.mark.parametrize("future", ["capture", "native", "both"])
def test_unknown_vm_payload_after_clearing_bundles_cannot_retire_user_files(
    fallback_placements, future, captured_output
):
    case = fallback_placements
    case.control["template"] = ResolvedVMTemplate("vm", artifacts=ArtifactsConfig())
    payloads = {}
    if future in ("capture", "both"):
        payloads[AppliedStateKey.ARTIFACT_INPUTS] = VersionedPayload(99, {"future": True})
    if future in ("native", "both"):
        payloads[AppliedStateKey.HARNESS_NATIVE_SETUP] = VersionedPayload(99, {"future": True})
    case.fixture.db.instance_state.replace_applied_slices("vm", "vm", "future", payloads)
    state = run_setup(
        case.fixture.db,
        case.fixture.registry,
        case.fixture.owners["agent"],
        case.user,
        failures=[],
        operation="agent-reinit",
    )
    for path, before in case.files.items():
        assert path.is_file() and path.read_bytes() == before
    (record,) = state.records
    assert record.artifact_files == case.original.artifact_files
    if future == "native":
        # Inactive VM passthrough only needs the readable capture; no ownership
        # or cleanup decision is taken from the opaque old integration slice.
        assert record.complete and not record.incomplete_reasons and not record.skipped
    else:
        assert not record.complete and record.skipped
    assert captured_output.warnings
    retained = {item.key: item.payload for item in case.fixture.db.instance_state.get_applied_slices("vm", "vm")}
    assert all(retained[key] == value for key, value in payloads.items())
