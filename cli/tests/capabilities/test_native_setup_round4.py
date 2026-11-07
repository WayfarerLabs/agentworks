"""Dispatch preserves confirmed routes and bounds diagnostics from failed facets."""

import shlex
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.capabilities.harness_integration import HarnessIntegration, ShellIntegration, harness_integration_for
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, VMSetupInvocation
from agentworks.errors import ConfigError, ConnectivityError, ExternalError, StateError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure, describe_setup_failures, sanitize_setup_diagnostic
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.origin import Origin
from agentworks.ssh import SSHLogger
from tests.artifacts.test_routing import graph
from tests.capabilities.test_native_setup_dispatch import setup_case as setup_case
from tests.native_setup_fixtures import LocalFixtureTransport


@pytest.mark.parametrize("failure_type", [ConfigError, StateError, ExternalError, ConnectivityError])
def test_each_recoverable_domain_error_keeps_claims_and_runs_sibling(setup_case, monkeypatch, failure_type):
    db, inputs, invocation, events, _ = setup_case
    original = harness_integration_for("first").user_init
    reason = uuid4().hex

    def initialize(self, call):
        result = original(self, call)
        if self.name == "first":
            raise failure_type(reason)
        return result

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert events == [("first", False), ("second", False)]
    assert [item.reason for item in failures] == [reason]
    assert state.records[0].claims and not state.records[0].complete and state.records[1].complete


def test_unsupported_later_activation_cannot_mutate_earlier_integration(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    monkeypatch.setattr(harness_integration_for("second"), "user_init", HarnessIntegration.user_init)
    failure_list: list[SetupFailure] = []
    with pytest.raises(StateError):
        run_setup(db, Mock(), inputs, invocation, failures=failure_list, operation="agent-create")
    assert events == [] and failure_list == []
    assert read_native_setup(db, "agent", "agent").records == ()


@pytest.mark.parametrize("effect", ["none", "claim", "file"])
@pytest.mark.parametrize("pending", [False, True])
def test_retiring_legacy_unimplemented_facet_drops_only_effect_free_record(setup_case, monkeypatch, effect, pending):
    from agentworks.artifacts.application import OwnedArtifactFile

    db, inputs, invocation, events, _ = setup_case
    previous = SetupRecord(
        component="agent",
        integration="first",
        destination_id="a" * 64,
        declaration={},
        pending_cleanup=pending,
        incomplete_reasons=(uuid4().hex,),
        claims=(NativeClaim(role="fixture", identifier="retained", destination="home"),) if effect == "claim" else (),
        artifact_files=(OwnedArtifactFile(path="/home/user/file", sha256="a" * 64, origins=("b" * 64,)),)
        if effect == "file"
        else (),
    )
    write_native_setup(db, "agent", "agent", NativeSetupState(records=(previous,)), operation="legacy")
    monkeypatch.setattr(harness_integration_for("first"), "user_init", HarnessIntegration.user_init)
    failures: list[SetupFailure] = []
    state = run_setup(
        db, Mock(), replace(inputs, activations={}), invocation, failures=failures, operation="agent-reinit"
    )
    assert events == []
    if effect == "none":
        assert state.records == () and failures == []
    else:
        (record,) = state.records
        assert record.pending_cleanup and not record.complete
        assert record.claims == previous.claims and record.artifact_files == previous.artifact_files
        assert len(failures) == 1


@pytest.fixture
def routed_setup(db, tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("isolated Linux guest filesystem execution")
    fixture = graph(db, active=("vm", "admin", "agent"), counts={"vm": 2, "admin": 0, "agent": 0})
    origin = Origin.built_in(source="round4-dispatch")
    fixture.registry.add("harness-integration", "shell", HarnessIntegrationEntry(name="shell"), origin)
    fixture.registry.finalize(probe_host_readiness=False)
    admin = LocalFixtureTransport(tmp_path / "admin")
    agent = LocalFixtureTransport(tmp_path / "agent")
    vm_call = VMSetupInvocation(vm=fixture.vm, runner=admin, prior=None, checkpoint=lambda claims: None)
    calls = {
        "admin": UserSetupInvocation(
            vm=fixture.vm,
            runner=admin,
            prior=None,
            checkpoint=lambda claims: None,
            username="admin",
            home=str(admin.home),
        ),
        "agent": UserSetupInvocation(
            vm=fixture.vm,
            runner=agent,
            prior=None,
            checkpoint=lambda claims: None,
            username="worker",
            home=str(agent.home),
        ),
    }
    fail_vm = False

    def vm_setup(self, call):
        if fail_vm:
            call.checkpoint((NativeClaim(role="fixture", identifier="confirmed", destination="vm"),))
            recorded = read_native_setup(db, "vm", "vm").records[0]
            assert recorded.deferred == previous.deferred and recorded.artifact_inputs == previous.artifact_inputs
            raise ConfigError("fixture VM native configuration failure")
        return ArtifactApplication(
            deferred=tuple(
                ArtifactDeferral(input_id=item.identity, destination="user", reason="fixture user placement")
                for item in call.artifacts.items()
            )
        )

    def user_setup(self, call):
        items = tuple(item for item in call.artifacts.items() if not item.content.name.endswith("-1"))
        return ArtifactApplication(
            files=(
                ArtifactFile(
                    call.home + "/AGENTS.md",
                    "\n".join(item.content.text for item in items).encode(),
                    tuple(item.origin_identity for item in items),
                ),
            )
            if items
            else (),
            deferred=tuple(
                ArtifactDeferral(input_id=item.identity, destination="session", reason="fixture session placement")
                for item in call.artifacts.items()
                if item.content.name.endswith("-1")
            ),
        )

    monkeypatch.setattr(ShellIntegration, "vm_init", vm_setup)
    monkeypatch.setattr(ShellIntegration, "user_init", user_setup)
    run_setup(db, fixture.registry, fixture.owners["vm"], vm_call, failures=[], operation="vm-init")
    for component, call in calls.items():
        run_setup(db, fixture.registry, fixture.owners[component], call, failures=[], operation="fixture-init")
    previous = read_native_setup(db, "vm", "vm").records[0]
    placements = {name: (Path(call.home) / "AGENTS.md").read_bytes() for name, call in calls.items()}
    fail_vm = True
    failures: list[SetupFailure] = []
    state = run_setup(db, fixture.registry, fixture.owners["vm"], vm_call, failures=failures, operation="vm-reinit")
    return SimpleNamespace(
        fixture=fixture,
        calls=calls,
        previous=previous,
        placements=placements,
        failures=failures,
        state=state,
    )


def test_failed_vm_dispatch_preserves_routes_for_same_vm_admin_and_later_agent(routed_setup):
    case = routed_setup
    failed = case.state.records[0]
    assert not failed.complete and failed.deferred == case.previous.deferred
    assert failed.artifact_inputs == case.previous.artifact_inputs
    assert len(case.failures) == 1
    for component, call in case.calls.items():
        state = run_setup(
            case.fixture.db,
            case.fixture.registry,
            case.fixture.owners[component],
            call,
            failures=[],
            operation="fixture-reinit",
        )
        record = next(item for item in state.records if item.component == component)
        assert record.complete and record.artifact_inputs == failed.artifact_inputs
        assert (Path(call.home) / "AGENTS.md").read_bytes() == case.placements[component]
        assert record.artifact_files


@pytest.mark.parametrize("with_logger", [False, True])
def test_untrusted_large_failure_is_bounded_terminal_safe_and_redacted_before_truncation(
    setup_case, monkeypatch, tmp_path, with_logger
):
    db, inputs, invocation, events, _ = setup_case
    token = "private-" + uuid4().hex
    marker = uuid4().hex
    osc = uuid4().hex
    logger = None
    if with_logger:
        monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
        logger = SSHLogger("round4", "setup", redactions=(token,))
    invocation.runner.logger = logger
    invocation = replace(invocation, secrets={"credential": token})
    # A secret crossing the storage limit must be redacted before slicing.
    payload = (
        f"\x1b[31m{marker}\x1b[0m\n\r\x1b]0;{osc}\x07\x9d{osc}\x9c\u202e"
        + "x" * (2048 - len(marker) - 12)
        + token
        + shlex.quote(token)
        + "x" * (1024 * 1024)
        + token
    )
    original = harness_integration_for("first").user_init

    def initialize(self, call):
        if self.name == "first":
            raise ExternalError(payload, hint=payload)
        return original(self, call)

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    failure = failures[0]
    assert failure.hint is not None
    values = [failure.reason, failure.hint, *state.records[0].incomplete_reasons]
    assert all(len(value.encode()) <= 2048 for value in values)
    assert all(
        not any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value) for value in values
    )
    assert all(token not in value and token[:8] not in value and osc not in value for value in values)
    summary = describe_setup_failures(failures)
    assert len(summary.splitlines()) == 2 and marker in summary and len(summary.encode()) < 4500
    assert state == read_native_setup(db, "agent", "agent")
    if logger is not None:
        assert len(logger.warnings) == 1 and token not in logger.path.read_text()
        assert len(logger.path.read_bytes()) < 4500


def test_rendering_old_failure_text_is_safe_without_mutating_runtime_evidence():
    marker = uuid4().hex
    raw = f"\x1b[2J{marker}\n\r\x9b31m\x1b]0;title\x07" + "z" * (1024 * 1024)
    failure = SetupFailure("vm", "owner", "user", "fixture", raw, raw)
    summary = describe_setup_failures([failure])
    assert len(summary.splitlines()) == 2 and len(summary.encode()) < 4500
    assert marker in summary and "\x1b" not in summary and "\x9b" not in summary
    assert failure.reason == raw


@pytest.mark.parametrize("raw", ["\x1b]unterminated", "\x1b[31", "\x9dunterminated", "\u202e\x00\x7f"])
def test_truncated_control_sequences_cannot_reach_failure_output(raw):
    result = sanitize_setup_diagnostic(raw)
    assert "\x1b" not in result and not any(unicodedata.category(char).startswith("C") for char in result)


@pytest.mark.parametrize("capture", ["changed", "missing"])
def test_unavailable_capture_cannot_erase_placements_or_unresolved_deferrals(routed_setup, capture):
    from agentworks.artifacts.model import ArtifactGroup
    from agentworks.artifacts.state import encode_captures, read_captures
    from agentworks.db import AppliedStateKey

    case = routed_setup
    fixture = case.fixture
    snapshot = fixture.captures["vm"]
    changed = tuple(replace(item, content=replace(item.content, text=uuid4().hex)) for item in snapshot.inputs.items())
    next_snapshot = replace(
        snapshot, inputs=ArtifactGroup(snapshot.inputs.owner, hints={item.content.name: item for item in changed})
    )
    vm_call = VMSetupInvocation(
        vm=fixture.vm, runner=case.calls["admin"].runner, prior=None, checkpoint=lambda claims: None
    )
    failures: list[SetupFailure] = []
    run_setup(
        fixture.db,
        fixture.registry,
        replace(fixture.owners["vm"], artifact_snapshot=next_snapshot),
        vm_call,
        failures=failures,
        operation="vm-reinit",
    )
    assert len(failures) == 1
    if capture == "missing":
        captures = read_captures(fixture.db, "vm", "vm")
        captures.pop("vm")
        fixture.db.instance_state.replace_applied_slices(
            "vm", "vm", "fixture", {AppliedStateKey.ARTIFACT_INPUTS: encode_captures(captures)}
        )
    old_ids = set(case.previous.artifact_inputs)
    assert old_ids.isdisjoint(item.identity for item in changed)
    for component, call in case.calls.items():
        before = next(
            item
            for item in read_native_setup(
                fixture.db, "vm" if component == "admin" else "agent", "vm" if component == "admin" else "agent"
            ).records
            if item.component == component
        )
        failures = []
        state = run_setup(
            fixture.db, fixture.registry, fixture.owners[component], call, failures=failures, operation="fixture-reinit"
        )
        record = next(item for item in state.records if item.component == component)
        assert not record.complete and not record.incomplete_reasons and record.skipped
        # Known route metadata survives without certifying the unknown incoming
        # input set, whose current health is evaluated from the producer lazily.
        assert record.artifact_inputs is not None and set(record.artifact_inputs) == old_ids
        assert {item.input_id for item in record.deferred} == old_ids
        assert {item.destination for item in record.deferred} == {"session"}
        assert record.artifact_files == before.artifact_files
        assert (Path(call.home) / "AGENTS.md").read_bytes() == case.placements[component]
        assert failures == []


@pytest.mark.windows
def test_selected_unsupported_repoint_preserves_native_progress_before_core_or_secret_resolution(
    db, tmp_path, monkeypatch
):
    import hashlib

    from agentworks.agents import manager
    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.bootstrap import build_registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.secrets.resolver import Resolver
    from tests.conftest import ManifestDoc, stub_vm_ssh_identity
    from tests.orchestrated_fixtures import PLUGINS_ENABLED, proxmox_site, write_operator_config

    monkeypatch.setattr(ShellIntegration, "user_init", HarnessIntegration.user_init)
    config = write_operator_config(
        tmp_path,
        PLUGINS_ENABLED,
        manifests=[
            proxmox_site(),
            ManifestDoc("agent-template", "default", {}),
            ManifestDoc("agent-template", "unsupported", {"harness_integrations": {"shell": {}}}),
        ],
    )
    # Unselected unsupported declarations remain available for inspection.
    build_registry(config, probe_host_readiness=False, include_live_resources=False)
    stub_vm_ssh_identity(monkeypatch)
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_agent("dev", "box", "dev", template="default")
    managed = tmp_path / "AGENTS.md"
    managed.write_text(uuid4().hex)
    before_bytes = managed.read_bytes()
    previous = NativeSetupState(
        records=(
            SetupRecord(
                component="agent",
                integration="shell",
                destination_id="a" * 64,
                declaration={},
                complete=True,
                artifact_files=(
                    OwnedArtifactFile(
                        path="/home/dev/AGENTS.md", sha256=hashlib.sha256(before_bytes).hexdigest(), origins=("b" * 64,)
                    ),
                ),
            ),
        )
    )
    write_native_setup(db, "agent", "dev", previous, operation="agent-init")
    core = Mock()
    resolve = Mock()
    monkeypatch.setattr("agentworks.agents.initializer.create_agent_on_vm", core)
    monkeypatch.setattr(Resolver, "resolve", resolve)
    with pytest.raises(StateError) as caught:
        manager.reinit_agent(
            db, config, name="dev", update_template="unsupported", interaction=TtyInteractionPolicy.REFUSE
        )
    assert (caught.value.entity_kind, caught.value.entity_name) == ("agent", "dev")
    core.assert_not_called()
    resolve.assert_not_called()
    assert db.get_agent("dev").template == "unsupported"
    assert read_native_setup(db, "agent", "dev") == previous and managed.read_bytes() == before_bytes
