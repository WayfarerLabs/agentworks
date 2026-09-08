"""Readiness selects actual owners and applies integration-chosen severity."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.capabilities.harness_integration.setup import (
    SetupEvidence,
    SetupGap,
    SetupReadiness,
    UserSetupInvocation,
)
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.db import Database
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
        invocation = UserSetupInvocation(
            vm=vm, runner=Mock(), prior=None, checkpoint=lambda claims: None, username="user", home="/home/user"
        )
        record = SetupRecord(
            component="agent",
            integration="shell",
            destination_id="a" * 64,
            declaration=inputs.declaration(inputs.attachments[0]),
            complete=True,
        )
        write_native_setup(db, "agent", "one", NativeSetupState(records=(record,)), operation="agent-reinit")
        monkeypatch.setattr("agentworks.harness_setup.readiness.destination_id", lambda invocation: "a" * 64)
        assert evaluate_setup(db, inputs, "shell", invocation, remediation="repair").current
        other = evaluate_setup(db, replace(inputs, name="two"), "shell", invocation, remediation="repair")
        assert other.status == "absent"
        for update, expected in (({"complete": False}, "incomplete"), ({"destination_id": "b" * 64}, "stale")):
            changed = record.model_copy(update=update)
            write_native_setup(db, "agent", "one", NativeSetupState(records=(changed,)), operation="agent-reinit")
            assert evaluate_setup(db, inputs, "shell", invocation, remediation="repair").status == expected
    finally:
        db.close()
