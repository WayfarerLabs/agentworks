"""Configured role original custody and control-preserving cleanup boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import LimitExceededError, StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws._native_access import EC2OwnedAccess
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession


@pytest.fixture
def role(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    owner = _OwnedRoleSession("vm-one")
    deadline = Deadline.after(5)
    owner.begin(deadline)
    probe = SimpleNamespace(owner=owner, clock=clock, deadline=deadline, closes=[], close_error=None, fetch_error=None)

    class Original:
        def close(self):
            assert owner._client is self
            probe.closes.append(self)
            if probe.close_error is not None:
                raise probe.close_error

    original = Original()

    def fetch():
        owner._client = original
        if probe.fetch_error is not None:
            raise probe.fetch_error
        return {"offline": True}

    owner._fetcher = SimpleNamespace(fetch_credentials=fetch)
    probe.original = original
    return probe


def test_refresh_closes_original_before_return(role):
    assert role.owner._refresh() == {"offline": True}
    assert role.closes == [role.original]
    assert not role.owner.cleanup_incomplete
    assert role.owner._deadline is role.deadline


@pytest.mark.parametrize("failure", [RuntimeError("provider"), KeyboardInterrupt(), SystemExit(17)])
@pytest.mark.parametrize("cleanup", [None, RuntimeError("close"), KeyboardInterrupt(), SystemExit(19)])
def test_refresh_failure_cleanup_control_order(role, failure, cleanup):
    role.fetch_error, role.close_error = failure, cleanup
    expected = (
        failure if cleanup is None or isinstance(cleanup, Exception) or not isinstance(failure, Exception) else cleanup
    )
    with pytest.raises(type(expected)) as caught:
        role.owner._refresh()
    assert caught.value is expected
    assert role.owner.cleanup_incomplete is (cleanup is not None)
    assert role.closes == [role.original]


def test_failed_close_blocks_send_and_retry_never_fetches(role):
    role.close_error = RuntimeError("close")
    role.owner._refresh()
    with pytest.raises(StateError):
        role.owner.guard_ec2_send()
    role.owner._fetcher = SimpleNamespace(fetch_credentials=lambda: pytest.fail("cleanup replayed AssumeRole"))
    role.close_error = None
    role.owner.close_client()
    assert role.closes == [role.original, role.original]
    assert not role.owner.cleanup_incomplete


def test_deadline_and_operation_admission(role):
    role.clock[0] = 106.0
    with pytest.raises(LimitExceededError):
        role.owner._refresh()
    role.owner.end()
    with pytest.raises(StateError):
        role.owner.guard_ec2_send()
    role.owner.stop()
    role.clock[0] = 100.0
    with pytest.raises(StateError):
        role.owner.begin(Deadline.after(5))
    assert role.closes == []


@pytest.mark.parametrize("first", [RuntimeError("ec2 close"), KeyboardInterrupt(), SystemExit(21)])
@pytest.mark.parametrize("second", [None, RuntimeError("sts close"), KeyboardInterrupt()])
def test_aggregate_attempts_both_originals_and_preserves_first_control(role, first, second):
    access = EC2OwnedAccess(SimpleNamespace(name="vm-one"), SimpleNamespace(), RunContext())
    access._role_auth = role.owner
    role.owner._client = role.original
    role.close_error = second

    def close():
        raise first

    ec2 = SimpleNamespace(close=close)
    access._read_client = ec2
    expected = first if not isinstance(first, Exception) else second
    if expected is not None and not isinstance(expected, Exception):
        with pytest.raises(type(expected)) as caught:
            access._close_clients()
        assert caught.value is expected
    else:
        access._close_clients()
    assert role.closes == [role.original]
    assert access._read_client is ec2
    assert role.owner.cleanup_incomplete is (second is not None)
    assert access.cleanup_incomplete


def test_returned_sts_is_retained_before_event_registration(role):
    def register(*args):
        assert role.owner._client is role.original
        raise RuntimeError("registration")

    role.original.meta = SimpleNamespace(events=SimpleNamespace(register=register))
    role.owner._base = SimpleNamespace(create_client=lambda *args, **kwargs: role.original)
    role.owner._fetcher = SimpleNamespace(fetch_credentials=lambda: role.owner._create_sts_client("sts"))
    with pytest.raises(RuntimeError):
        role.owner._refresh()
    assert role.closes == [role.original] and not role.owner.cleanup_incomplete


def test_constructor_failure_without_return_does_not_invent_custody(role):
    def create(*args, **kwargs):
        raise KeyboardInterrupt()

    role.owner._base = SimpleNamespace(create_client=create)
    role.owner._fetcher = SimpleNamespace(fetch_credentials=lambda: role.owner._create_sts_client("sts"))
    with pytest.raises(KeyboardInterrupt):
        role.owner._refresh()
    assert role.closes == [] and not role.owner.cleanup_incomplete


@pytest.mark.parametrize("boundary", ["entry", "after-close"])
def test_original_control_survives_cleanup_python_boundaries(role, monkeypatch, boundary):
    failure = SystemExit(31)
    role.fetch_error = failure
    close = role.owner.close_client

    def interrupted():
        if boundary == "after-close":
            close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(role.owner, "close_client", interrupted)
    with pytest.raises(SystemExit) as caught:
        role.owner._refresh()
    assert caught.value is failure
    assert role.owner.cleanup_incomplete is (boundary == "entry")
