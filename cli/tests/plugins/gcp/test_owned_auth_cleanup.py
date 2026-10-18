"""Nonterminal retirement preserves the selected credential and consumer proof."""

from __future__ import annotations

from threading import Event
from typing import Any

import pytest

from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from tests.plugins.gcp.test_native_execution_access import Control
from tests.plugins.gcp.test_native_execution_access import full as full
from tests.plugins.gcp.test_owned_auth_copy import copied, run_copy
from tests.plugins.gcp.test_owned_read_sdk import FreshCredential, observe
from tests.plugins.gcp.test_owned_read_sdk import sdk as sdk


class RefreshingCredential(FreshCredential):
    def refresh(self, request: Any) -> None:
        response = request("https://credential.invalid/token", method="POST", body=b"synthetic")
        assert response.status == 200
        self.token = "offline-refreshed-token"


@pytest.mark.parametrize("method", ["observe_power", "prepare"])
def test_first_read_refresh_debt_is_retained_before_borrower_failure(full, method):
    from agentworks.db import VMStatus

    p = full
    selected = RefreshingCredential()
    selected.token = None
    p.sdk.credential = selected
    p.sdk.failed_close = "credential-response"
    with pytest.raises(StateError):
        if method == "prepare":
            p.access.prepare(VMStatus.RUNNING, p.deadline)
        else:
            p.access.observe_power(p.deadline)
    auth = p.access._auth
    assert auth is p.access._reader._auth and auth.cleanup_incomplete and not auth.closed
    assert auth._credential is selected and len(p.sdk.adc_calls) == 1
    assert [kind for kind, _, _ in p.sdk.sends] == ["credential"]
    assert not p.access.settle(p.deadline)
    assert not auth.closed and "auth-close" not in p.calls
    p.sdk.failed_close = None
    assert p.access.settle(Deadline.after(2))
    assert auth._credential is selected and len(p.sdk.adc_calls) == 1
    assert [kind for kind, _, _ in p.sdk.sends] == ["credential"]


@pytest.mark.parametrize("debt", ["response", "session"])
def test_completed_copy_retries_its_original_without_closing_root(sdk, monkeypatch, debt):
    reader, _ = observe(sdk)
    auth = reader._auth
    assert auth._request is not None
    root = auth._request.session
    selected = auth._credential
    clone = copied(reader)
    worker, outcomes = run_copy(clone)
    worker.join(2)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    original_response, original_session = clone._responses[0], clone.session
    if debt == "response":
        sdk.failed_close = "credential-response"
    else:
        original_close = original_session.close
        attempts = []

        def close():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("copied session close")
            original_close()

        monkeypatch.setattr(original_session, "close", close)
    assert not auth.retry_cleanup(Deadline.after(2))
    assert auth.cleanup_incomplete and not auth.closed and clone in auth._copies
    assert auth._request.session is root and auth._credential is selected
    sdk.failed_close = None
    assert auth.retry_cleanup(Deadline.after(2))
    assert clone.session is None and clone._responses == [] and clone not in auth._copies
    assert not auth.closed and auth._request.session is root and auth._credential is selected
    assert any(handle is original_response for _, handle in sdk.closes)
    auth.begin(Deadline.after(2))
    auth.end()
    assert reader.close(Deadline.after(2))


def test_nonterminal_retry_leaves_valid_never_entered_copy_available(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader)
    session = clone.session
    assert reader._auth.retry_cleanup(Deadline.after(2))
    assert not clone._barred and clone.session is session and clone in reader._auth._copies
    worker, outcomes = run_copy(clone)
    worker.join(2)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    assert reader._auth.retry_cleanup(Deadline.after(2)) and reader.close(Deadline.after(2))


def test_nonterminal_retry_does_not_bar_or_close_live_copy_consumer(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader)
    entered, release = Event(), Event()

    def consuming():
        entered.set()
        assert release.wait(2)

    worker, outcomes = run_copy(clone, after=consuming)
    try:
        assert entered.wait(2)
        originals = clone.session, tuple(clone._responses)
        assert reader._auth.retry_cleanup(Deadline.after(2))
        assert not clone._barred and worker.is_alive()
        assert (clone.session, tuple(clone._responses)) == originals
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    assert reader._auth.retry_cleanup(Deadline.after(2)) and reader.close(Deadline.after(2))


def test_retry_preserves_root_control_and_attempts_independent_completed_copy(sdk, monkeypatch):
    reader, _ = observe(sdk)
    auth = reader._auth
    assert auth._request is not None
    auth.begin(Deadline.after(2))
    try:
        auth._request("https://credential.invalid/token")
    finally:
        auth.end()
    clone = copied(reader)
    worker, outcomes = run_copy(clone)
    worker.join(2)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    response = auth._request._responses[0]
    control = Control("credential response close")
    original = response.close

    def close():
        raise control

    monkeypatch.setattr(response, "close", close)
    with pytest.raises(Control) as caught:
        auth.retry_cleanup(Deadline.after(2))
    assert caught.value is control and clone.session is None and clone not in auth._copies
    assert auth._request._responses == [response] and not auth.closed
    monkeypatch.setattr(response, "close", original)
    assert auth.retry_cleanup(Deadline.after(2)) and reader.close(Deadline.after(2))


def test_retry_does_not_reobserve_or_retire_poisoned_worker(sdk, monkeypatch):
    reader, _ = observe(sdk)
    clone = copied(reader)
    worker, outcomes = run_copy(clone)
    worker.join(2)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    clone._completion_unconfirmed = True
    originals = clone.session, tuple(clone._responses)
    monkeypatch.setattr(worker, "is_alive", lambda: pytest.fail("poisoned worker observed again"))
    monkeypatch.setattr(worker, "join", lambda *a, **k: pytest.fail("poisoned worker joined again"))
    assert not reader._auth.retry_cleanup(Deadline.after(2))
    assert clone._completion_unconfirmed and (clone.session, tuple(clone._responses)) == originals
    assert not reader._auth.closed


def test_retry_does_not_clear_failed_credential_selection(sdk):
    reader, _ = observe(sdk)
    auth = reader._auth
    auth._failed = True
    assert auth.retry_cleanup(Deadline.after(2))
    assert auth._failed and not auth.closed
    with pytest.raises(StateError):
        auth.begin(Deadline.after(2))
    assert reader.close(Deadline.after(2))


@pytest.mark.parametrize("deadline,error", [(Deadline(None), ValidationError), (Deadline(0), LimitExceededError)])
def test_retry_requires_finite_remaining_budget_without_barring_auth(sdk, deadline, error):
    reader, _ = observe(sdk)
    before = list(sdk.closes)
    with pytest.raises(error):
        reader._auth.retry_cleanup(deadline)
    assert sdk.closes == before and not reader._auth.closed
    assert reader.close(Deadline.after(2))
