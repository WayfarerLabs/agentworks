"""Mutation prefixes, retirement order, and destination ownership at dispatch."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import CapabilityBlock
from agentworks.secrets.orchestration import SecretTarget
from tests.plugins._fixtures import ConformingHarnessIntegration


@pytest.fixture
def facet_case(db, monkeypatch, component):
    vm = db.insert_vm("vm", site="local", hostname="vm")
    inputs = SetupInputs(
        kind="vm" if component in {"vm", "admin"} else component,
        name="owner",
        component=component,
        activations=(),
        target=SecretTarget(vm={}),
    )
    common = dict(vm=vm, runner=Mock(), prior=None, checkpoint=Mock())
    invocation: SetupInvocation
    if component == "vm":
        invocation = VMSetupInvocation(**common)
    elif component == "workspace":
        invocation = WorkspaceSetupInvocation(
            **common, workspace_name="owner", root="/work/owner", linux_group="workspace"
        )
    else:
        invocation = UserSetupInvocation(**common, username=component, home=f"/home/{component}")
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr(
        "agentworks.harness_setup.dispatch.ensure_harness_integration_enabled", lambda registry, name: None
    )
    return inputs, invocation


@pytest.mark.parametrize(
    ("component", "integration"),
    [(component, name) for component in ("vm", "admin", "agent", "workspace") for name in ("shell", "grok-build")]
    + [("vm", name) for name in ("claude-code", "codex")],
)
@pytest.mark.parametrize("buffered", [False, True])
def test_unimplemented_activation_fails_without_successful_record(db, facet_case, integration, buffered):
    inputs, invocation = facet_case
    inputs = replace(inputs, activations=(CapabilityBlock.of(integration),))
    with pytest.raises(StateError) as error:
        run_setup(db, Mock(), inputs, invocation, operation="fixture-setup", buffered=buffered)
    assert error.value.entity_kind == inputs.kind
    assert error.value.entity_name == inputs.name
    records = read_native_setup(db, inputs.kind, inputs.name).records
    if buffered:
        assert records == ()
    else:
        (record,) = records
        assert record.integration == integration and record.component == inputs.component
        assert not record.complete and record.claims == ()
    assert invocation.runner.method_calls == []


@pytest.mark.parametrize("component", ["vm", "admin", "agent", "workspace"])
def test_inactive_integrations_remain_absent(db, facet_case, monkeypatch):
    inputs, invocation = facet_case
    monkeypatch.setattr(
        "agentworks.harness_setup.dispatch.destination_id", lambda *a, **k: pytest.fail("no active setup")
    )
    state = run_setup(db, Mock(), inputs, invocation, operation="fixture-setup")
    assert state.records == read_native_setup(db, inputs.kind, inputs.name).records == ()
    assert invocation.runner.method_calls == []


@pytest.mark.parametrize("component", ["admin", "agent", "workspace"])
@pytest.mark.parametrize("integration", ["claude-code", "codex"])
def test_implemented_default_setup_completes_without_native_changes(db, facet_case, integration):
    inputs, invocation = facet_case
    inputs = replace(inputs, activations=(CapabilityBlock.of(integration),))
    for _ in range(2):
        state = run_setup(db, Mock(), inputs, invocation, operation="fixture-setup")
        (record,) = state.records
        assert record.complete and record.claims == ()
        assert record == read_native_setup(db, inputs.kind, inputs.name).records[0]
    assert invocation.runner.method_calls == []


@pytest.mark.parametrize("component", ["vm", "admin", "agent", "workspace"])
def test_retirement_removes_legacy_claim_free_unsupported_record(db, facet_case):
    inputs, invocation = facet_case
    previous = SetupRecord(
        component=inputs.component, integration="shell", destination_id="a" * 64, declaration={}, complete=True
    )
    write_native_setup(db, inputs.kind, inputs.name, NativeSetupState(records=(previous,)), operation="fixture-setup")
    state = run_setup(db, Mock(), inputs, invocation, operation="fixture-setup")
    assert state.records == read_native_setup(db, inputs.kind, inputs.name).records == ()
    assert invocation.runner.method_calls == []


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    events = []
    failures: set[str] = set()

    class First(ConformingHarnessIntegration):
        name = "first"
        description = "Native dispatch fixture"

        def user_init(self, invocation):
            events.append((self.name, self.retiring))
            claims = () if self.retiring else (NativeClaim(role="plugin", identifier=self.name, destination="home"),)
            invocation.checkpoint(claims)
            if self.name in failures:
                raise RuntimeError("injected interruption")

    class Second(First):
        name = "second"
        description = "Second dispatch fixture"

    db = Database(tmp_path / "state.db")
    vm = db.insert_vm("vm", site="local", hostname="vm")
    inputs = SetupInputs(
        kind="agent",
        name="agent",
        component="agent",
        activations=(CapabilityBlock.of("first"), CapabilityBlock.of("second")),
        target=SecretTarget(vm={}, agent={}),
    )
    invocation = UserSetupInvocation(
        vm=vm, runner=Mock(), prior=None, checkpoint=lambda claims: None, username="user", home="/home/user"
    )
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr(
        "agentworks.harness_setup.dispatch.ensure_harness_integration_enabled", lambda registry, name: None
    )
    with seated_plugin(Plugin(name="dispatch-fixture", capabilities={"harness-integration": (First, Second)})):
        yield db, inputs, invocation, events, failures
    db.close()


def test_failure_checkpoints_prefix_and_retry_completes(setup_case):
    db, inputs, invocation, events, failures = setup_case
    failures.add("first")
    with pytest.raises(RuntimeError):
        run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    state = read_native_setup(db, "agent", "agent")
    assert [record.integration for record in state.records] == ["first"]
    assert not state.records[0].complete
    assert state.records[0].claims[0].identifier == "first"
    assert events == [("first", False)]
    failures.clear()
    run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    assert all(record.complete for record in read_native_setup(db, "agent", "agent").records)


def test_retirement_follows_desired_order_and_preserves_other_records(setup_case):
    db, inputs, invocation, events, _ = setup_case
    run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    events.clear()
    run_setup(
        db, Mock(), replace(inputs, activations=(CapabilityBlock.of("second"),)), invocation, operation="agent-reinit"
    )
    assert events == [("second", False), ("first", True)]
    assert [record.integration for record in read_native_setup(db, "agent", "agent").records] == ["second"]


def test_checkpoint_failure_stops_before_next_integration(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    from agentworks.harness_setup.state import write_native_setup

    original = write_native_setup
    writes = 0

    def fail_second(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("fixture persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr("agentworks.harness_setup.dispatch.write_native_setup", fail_second)
    with pytest.raises(OSError):
        run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    assert events == [("first", False)]
    assert not read_native_setup(db, "agent", "agent").records[0].complete


def test_buffered_creation_does_not_publish_ownerless_applied_state(setup_case):
    db, inputs, invocation, _, _ = setup_case
    state = run_setup(db, Mock(), inputs, invocation, operation="agent-create", buffered=True)
    assert len(state.records) == 2
    assert read_native_setup(db, "agent", "agent").records == ()


def test_changed_destination_can_be_reconciled_by_owning_integrations(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    events.clear()
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *args, **kwargs: "b" * 64)
    run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    assert events == [("first", False), ("second", False)]
    assert all(
        record.complete and record.destination_id == "b" * 64
        for record in read_native_setup(db, "agent", "agent").records
    )


def test_applied_state_omits_env_values_but_detects_declaration_changes(setup_case):
    from agentworks.env.entry import EnvEntry

    db, inputs, invocation, _, _ = setup_case
    inputs = replace(
        inputs,
        target=SecretTarget(
            vm={}, agent={"LITERAL": EnvEntry("private-literal"), "TOKEN": EnvEntry({"secret": "token-name"})}
        ),
    )
    run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    record = read_native_setup(db, "agent", "agent").records[0]
    encoded = record.model_dump_json()
    assert "private-literal" not in encoded
    assert "token-name" in encoded
    changed = replace(
        inputs,
        target=SecretTarget(
            vm={}, agent={"LITERAL": EnvEntry("changed-literal"), "TOKEN": EnvEntry({"secret": "token-name"})}
        ),
    )
    assert changed.declaration(inputs.activations[0]) != record.declaration
