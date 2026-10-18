"""Finite admission, late evidence and aggregate dependent-custody boundaries."""

from __future__ import annotations

from threading import Event, Thread

import pytest

from agentworks.db import VMStatus
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._activation import ActivationState
from tests.plugins.gcp.test_native_execution_access import Control
from tests.plugins.gcp.test_native_execution_access import full as full
from tests.plugins.gcp.test_owned_read_sdk import sdk as sdk


@pytest.mark.parametrize("deadline, error", [(Deadline(None), ValidationError), (Deadline(0), LimitExceededError)])
def test_finite_transition_required_before_provider_work(full, deadline, error):
    with pytest.raises(error):
        full.access.prepare(VMStatus.RUNNING, deadline)
    assert full.calls == [] and full.sdk.sends == []


def test_other_thread_cannot_enter_transition_past_its_budget(full):
    p = full
    entered, release = Event(), Event()

    def hold():
        with p.access._lock:
            entered.set()
            assert release.wait(2)

    holder = Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(2)
        with pytest.raises(StateError):
            p.access.observe_power(Deadline.after(0.01))
        assert p.sdk.sends == []
    finally:
        release.set()
        holder.join(2)
    assert not holder.is_alive() and p.access.settle(p.deadline)


def test_expired_settlement_bars_body_before_deadline_refusal(full):
    p = full
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    check = p.access.route_check
    with pytest.raises(LimitExceededError):
        p.access.settle(Deadline(0))
    assert p.access.route_check is None
    with pytest.raises(StateError):
        check(p.deadline)
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("method", ["power", "prepare"])
def test_late_provider_success_never_admits_next_effect(full, method):
    p = full
    p.sdk.after_raw_read = lambda: setattr(p.sdk, "now", p.deadline.expires_at)
    with pytest.raises(LimitExceededError):
        if method == "power":
            p.access.observe_power(p.deadline)
        else:
            p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert len(p.sdk.sends) == 1 and "source-read" not in p.calls and "start-post" not in p.calls
    assert p.access.settle(Deadline.after(2))


def test_late_source_success_retains_original_for_fresh_cleanup(full):
    p = full
    p.source_hook = lambda: setattr(p.sdk, "now", p.deadline.expires_at)
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access._exposure.cleanup_incomplete and "route-open" not in p.calls
    assert p.access.settle(Deadline.after(2))
    assert p.calls.index("source-close") < p.calls.index("auth-close")


def test_late_guest_fact_is_retained_without_repeating_preparation(full):
    p = full
    p.guest_hook = lambda: setattr(p.sdk, "now", p.deadline.expires_at)
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access.preparation is p.prep
    assert p.access.settle(Deadline.after(2))
    assert p.calls.count("guest-stub") == 1


@pytest.mark.parametrize("state", ["PROVISIONING", "STAGING", "RUNNING"])
def test_startup_or_missing_nat_expires_without_another_start(full, state):
    p = full
    p.current["status"] = "TERMINATED"
    p.startup = [(state, None)]
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert p.calls.count("start-post") == 1 and p.access.binding is None and "route-open" not in p.calls
    reads = len(p.sdk.sends)
    assert p.access.settle(Deadline.after(2))
    assert len(p.sdk.sends) == reads and "guest-stub" not in p.calls


def test_running_without_nat_refuses_before_source_discovery(full):
    p = full
    p.current["networkInterfaces"][0]["accessConfigs"][0].pop("natIP")
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access.binding is None and "source-read" not in p.calls and p.calls.count("instance-read") == 1
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("stage", ["read", "start", "route", "source"])
def test_retained_original_keeps_auth_until_fresh_cleanup(full, stage):
    p = full
    if stage == "read":
        p.sdk.failed_close = "service-session"
    elif stage == "start":
        p.current["status"], p.start_cleanup = "TERMINATED", True
    elif stage == "route":
        p.route_cleanup = True
    else:
        p.prefix_error = RuntimeError("source close")
    power = VMStatus.STOPPED if stage == "start" else VMStatus.RUNNING
    with pytest.raises(StateError):
        p.access.prepare(power, p.deadline)
    assert not p.access.settle(p.deadline) and "auth-close" not in p.calls
    p.sdk.failed_close, p.start_cleanup, p.route_cleanup, p.prefix_error = None, False, False, None
    assert p.access.settle(p.deadline) and p.calls[-1] == "auth-close"


def test_lost_start_ack_never_invents_monitor_or_replays(full):
    p = full
    p.current["status"] = "TERMINATED"
    p.start_error = Control("start request escaped before ACK")
    with pytest.raises(Control) as caught:
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert caught.value is p.start_error
    assert not p.access.settle(p.deadline)
    assert p.calls.count("start-post") == 1 and "start-monitor" not in p.calls and "auth-close" not in p.calls
    assert p.access._start.state is ActivationState.UNKNOWN and p.owner.list_pending_lifecycle_obligations()


def test_reader_control_does_not_skip_route_settlement(full, monkeypatch):
    p = full
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    control = Control("reader close")
    original = p.access._reader.close_read_client

    def fail(deadline):
        original(deadline)
        raise control

    monkeypatch.setattr(p.access._reader, "close_read_client", fail)
    with pytest.raises(Control) as caught:
        p.access.settle(p.deadline)
    assert caught.value is control
    assert "route-settle" in p.calls and "auth-close" not in p.calls
    assert not p.owner.list_pending_lifecycle_obligations()
    monkeypatch.setattr(p.access._reader, "close_read_client", original)
    assert p.access.settle(p.deadline)


def test_ordinary_cleanup_failure_does_not_replace_later_control(full):
    p = full
    p.current["status"] = "TERMINATED"
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    p.route_settle_error = RuntimeError("route")
    p.start_settle_error = Control("start")
    with pytest.raises(Control) as caught:
        p.access.settle(p.deadline)
    assert caught.value is p.start_settle_error and "auth-close" not in p.calls
    p.route_settle_error = p.start_settle_error = None
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("child", ["route", "start"])
def test_positive_settlement_retires_close_debt_and_survives_outer_release(full, monkeypatch, child):
    p = full
    p.current["status"] = "TERMINATED"
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    dependent = getattr(p.access, "_" + child)
    original_close, original_settle = dependent.close, dependent.settle

    def close(deadline):
        setattr(p, child + "_cleanup", True)
        return original_close(deadline)

    def settle(deadline):
        setattr(p, child + "_cleanup", False)
        dependent.cleanup_incomplete = False
        return original_settle(deadline)

    monkeypatch.setattr(dependent, "close", close)
    monkeypatch.setattr(dependent, "settle", settle)
    assert p.access.settle(p.deadline) and getattr(p.access, "_" + child + "_settled")
    assert p.calls[-1] == "auth-close"
    p.owner.seal_lifecycle_obligations()
    p.owner.record_effects_resolved()
    p.owner.close()
    assert p.database.operations.inspect(p.owner.ownership.scope) is None
    calls = list(p.calls)
    monkeypatch.setattr(dependent, "settle", lambda _: pytest.fail("settled dependent revisited"))
    monkeypatch.setattr(
        p.database.operations, "inspect_lifecycle_obligation", lambda *a, **k: pytest.fail("released ledger revisited")
    )
    assert p.access.settle(p.deadline) and p.calls == calls


@pytest.mark.parametrize("child", ["route", "start"])
def test_positive_child_settlement_is_memoized_before_prior_close_control(full, monkeypatch, child):
    p = full
    p.current["status"] = "TERMINATED"
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    dependent = getattr(p.access, "_" + child)
    control = Control("close")

    def close(deadline):
        raise control

    monkeypatch.setattr(dependent, "close", close)
    with pytest.raises(Control) as caught:
        p.access.settle(p.deadline)
    assert caught.value is control and getattr(p.access, "_" + child + "_settled")
    monkeypatch.setattr(dependent, "settle", lambda _: pytest.fail("settled dependent revisited"))
    assert p.access.settle(p.deadline)
