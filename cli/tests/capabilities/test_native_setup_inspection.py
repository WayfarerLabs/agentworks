"""Native receipts remain visible and value-free on live describe and doctor."""

import json

import pytest

from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.doctor import Status
from agentworks.doctor_state import check_database
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import write_native_setup
from agentworks.instance_description import instance_state_data


@pytest.mark.parametrize("kind", ["vm", "agent", "workspace"])
@pytest.mark.parametrize("mode", ["complete", "pending", "future", "malformed"])
def test_native_receipts_are_inspected_without_io_or_payload_values(db, make_config, monkeypatch, kind, mode):
    from agentworks.agents.manager import agent_description
    from agentworks.instance_description import load_instance_description_registry
    from agentworks.vms.manager.inspect import _vm_instance_state
    from agentworks.workspaces.manager import workspace_description

    db.insert_vm("box", site="lima-local", hostname="box")
    db.insert_agent("dev", "box", "agt-dev")
    db.insert_workspace("work", "/srv/work", "box", "ws-work")
    name = {"vm": "box", "agent": "dev", "workspace": "work"}[kind]
    component = "admin" if kind == "vm" else kind
    if mode in ("complete", "pending"):
        write_native_setup(
            db,
            kind,
            name,
            NativeSetupState(
                records=(
                    SetupRecord(
                        component=component,
                        integration="shell",
                        destination_id="a" * 64,
                        declaration={"config": {"private": "config-value-sentinel"}},
                        complete=mode == "complete",
                        pending_cleanup=mode == "pending",
                        claims=(NativeClaim(role="settings", identifier="settings", destination="/private/path"),),
                    ),
                )
            ),
            operation="fixture",
        )
    else:
        db.instance_state.replace_applied_slices(
            kind,
            name,
            "fixture",
            {AppliedStateKey.HARNESS_NATIVE_SETUP: VersionedPayload(99 if mode == "future" else 1, {"invalid": True})},
        )
    original = db.instance_state.get_applied_slices(kind, name)
    monkeypatch.setattr("agentworks.db.DB_PATH", db.path)
    config = make_config()
    if kind == "vm":
        registry = load_instance_description_registry(db, config, "vm", "box")
        state, _ = _vm_instance_state(registry, db.get_vm("box"), db.instance_state.inspect_owner_state("vm", "box"))
    elif kind == "agent":
        state = agent_description(db, config, name="dev").instance_state
    else:
        state = workspace_description(db, config, "work").instance_state
    fact = next(item for item in state.lifecycle_evidence if item.key == AppliedStateKey.HARNESS_NATIVE_SETUP.value)
    data = json.dumps(instance_state_data(state))
    assert "config-value-sentinel" not in data and "/private/path" not in data
    assert fact.status == ("recorded" if mode in ("complete", "pending") else "unavailable")
    if mode == "future":
        assert state.unconsumed_records
    group = check_database(config)
    outcome = next(
        check
        for check in group.checks
        if check.instance_state is not None
        and check.instance_state.record_key == AppliedStateKey.HARNESS_NATIVE_SETUP.value
    )
    assert (
        outcome.status
        == {"complete": Status.OK, "pending": Status.WARN, "future": Status.INFO, "malformed": Status.FAIL}[mode]
    )
    assert db.instance_state.get_applied_slices(kind, name) == original
