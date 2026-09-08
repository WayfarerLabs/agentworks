"""Readiness selects actual owners and applies integration-chosen severity."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.capabilities.harness_integration.setup import (
    SetupEvidence,
    SetupGap,
    SetupReadiness,
)
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeSetupState, SetupRecord
from agentworks.harness_setup.readiness import (
    RequiredSetupMissingError,
    enforce_setup_gaps,
    evaluate_setup,
    require_setup_ready,
)
from agentworks.harness_setup.state import write_native_setup
from agentworks.schema import CapabilityBlock
from agentworks.secrets.orchestration import SecretTarget


def test_default_session_setup_check_reads_no_ancestor_state():
    integration = ShellIntegration(
        "default",
        {},
        session_name="s",
        vm_name="vm",
        workspace_name="w",
        workspace_path="/w",
        target=None,
        admin=True,
        state={},
    )
    db = Mock()
    require_setup_ready(db, Mock(), integration, vm=Mock(), workspace=Mock(), agent_name=None, runner=Mock())
    assert db.mock_calls == []


def test_evidence_is_loaded_once_per_invocation():
    evidence = SetupEvidence("absent", "agent", "one", "repair")
    lookup = Mock(return_value=evidence)
    readiness = SetupReadiness(lookup, Mock())
    assert readiness.user is readiness.user is evidence
    lookup.assert_called_once_with("user")
    next_operation = SetupReadiness(lookup, Mock())
    assert next_operation.user is evidence
    assert lookup.call_count == 2


def test_integration_severity_controls_refusal(captured_output):
    evidence = SetupEvidence("absent", "agent", "one", "fixture remediation")
    enforce_setup_gaps((SetupGap(evidence, "recommended", "fixture reason"),))
    assert len(captured_output.warnings) == 1
    with pytest.raises(RequiredSetupMissingError) as caught:
        enforce_setup_gaps((SetupGap(evidence, "required", "fixture reason"),))
    assert caught.value.entity_kind == "agent"
    assert caught.value.entity_name == "one"
    assert caught.value.hint == evidence.remediation


def test_only_actual_user_current_receipt_satisfies_evidence(tmp_path, monkeypatch):
    db = Database(tmp_path / "state.db")
    try:
        vm = db.insert_vm("vm", site="local", hostname="vm")
        inputs = SetupInputs(
            kind="agent",
            name="one",
            component="agent",
            attachments=(CapabilityBlock.of("shell"),),
            target=SecretTarget(vm={}, agent={}),
        )
        runner = Mock()
        record = SetupRecord(
            component="agent",
            integration="shell",
            destination_id="a" * 64,
            declaration=inputs.declaration(inputs.attachments[0]),
            complete=True,
        )
        write_native_setup(db, "agent", "one", NativeSetupState(records=(record,)), operation="agent-reinit")
        monkeypatch.setattr("agentworks.harness_setup.readiness.destination_id", lambda *args, **kwargs: "a" * 64)
        assert evaluate_setup(
            db, inputs, "shell", vm=vm, runner=runner, location="/home/user", username="user", remediation="repair"
        ).current
        other = evaluate_setup(
            db,
            replace(inputs, name="two"),
            "shell",
            vm=vm,
            runner=runner,
            location="/home/user",
            username="user",
            remediation="repair",
        )
        assert other.status == "absent"
        for update, expected in (({"complete": False}, "incomplete"), ({"destination_id": "b" * 64}, "stale")):
            changed = record.model_copy(update=update)
            write_native_setup(db, "agent", "one", NativeSetupState(records=(changed,)), operation="agent-reinit")
            assert (
                evaluate_setup(
                    db,
                    inputs,
                    "shell",
                    vm=vm,
                    runner=runner,
                    location="/home/user",
                    username="user",
                    remediation="repair",
                ).status
                == expected
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "malformed_gap", [object(), SetupGap(SetupEvidence("absent", "agent", "one", "repair"), "misspelled", "invalid")]
)
def test_invalid_plugin_prerequisite_never_permits_launch(malformed_gap, captured_output, monkeypatch):
    integration = ShellIntegration(
        "default",
        {},
        session_name="s",
        vm_name="vm",
        workspace_name="w",
        workspace_path="/w",
        target=None,
        admin=True,
        state={},
    )
    monkeypatch.setattr(integration, "check_setup", Mock(return_value=(malformed_gap,)))
    with pytest.raises(StateError):
        require_setup_ready(Mock(), Mock(), integration, vm=Mock(), workspace=Mock(), agent_name=None, runner=Mock())
    assert captured_output.warnings == []


@pytest.mark.parametrize(
    ("location", "username", "recorded"),
    [
        (None, None, "47722e928296ee2a77034faecef2937ab49068c9b2ee6e4fbd9bf6a3abdbb655"),
        ("/home/alice", "alice", "8c2ae87c0e6661337de37bc6ee57132aa93410a083b40401752459b4be41fa85"),
        ("/work/project", None, "1a4d8d9b6541c3cfc2f6a550d0257aa906988dea5a1e846950833b1412ac6b02"),
    ],
)
def test_destination_probe_preserves_previously_recorded_identities(db, location, username, recorded):
    # Receipt identities captured from the prior invocation-based API.
    vm = replace(db.insert_vm("fixture", site="local", hostname="fixture"), created_at="2026-01-01T00:00:00Z")
    runner = Mock()
    runner.run.side_effect = lambda command, **kwargs: Mock(
        stdout="machine-fixture\n" if command == "cat /etc/machine-id" else "1:2:1000\n"
    )
    assert destination_id(vm, runner, location=location, username=username) == recorded
