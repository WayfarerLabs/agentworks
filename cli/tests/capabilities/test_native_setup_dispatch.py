"""Mutation prefixes, retirement order, and destination ownership at dispatch."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.model import NativeClaim
from agentworks.harness_setup.state import read_native_setup
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import CapabilityBlock
from agentworks.secrets.orchestration import SecretTarget
from tests.plugins._fixtures import ConformingHarnessIntegration


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    events = []
    failures = set()

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
        attachments=(CapabilityBlock.of("first"), CapabilityBlock.of("second")),
        target=SecretTarget(vm={}, agent={}),
    )
    invocation = UserSetupInvocation(
        vm=vm, runner=Mock(), prior=None, checkpoint=lambda claims: None, username="user", home="/home/user"
    )
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda call: "a" * 64)
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
        db, Mock(), replace(inputs, attachments=(CapabilityBlock.of("second"),)), invocation, operation="agent-reinit"
    )
    assert events == [("second", False), ("first", True)]
    assert [record.integration for record in read_native_setup(db, "agent", "agent").records] == ["second"]


def test_checkpoint_failure_stops_before_next_integration(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    from agentworks.harness_setup.dispatch import write_native_setup

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


def test_buffered_creation_does_not_publish_ownerless_receipts(setup_case):
    db, inputs, invocation, _, _ = setup_case
    state = run_setup(db, Mock(), inputs, invocation, operation="agent-create", buffered=True)
    assert len(state.records) == 2
    assert read_native_setup(db, "agent", "agent").records == ()


def test_changed_native_destination_refuses_before_any_mutation(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    events.clear()
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda call: "b" * 64)
    with pytest.raises(StateError):
        run_setup(db, Mock(), inputs, invocation, operation="agent-reinit")
    assert events == []
