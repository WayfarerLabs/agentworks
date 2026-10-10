"""Concrete public copy protocol, SDK middleware and local consumer custody."""

from __future__ import annotations

from copy import deepcopy
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, cast

import pytest
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._owned_auth import _MAX_RETAINED_COPIES
from tests.plugins.gcp.test_owned_read import make_reader
from tests.plugins.gcp.test_owned_read_sdk import observe
from tests.plugins.gcp.test_owned_read_sdk import sa_document as sa_document
from tests.plugins.gcp.test_owned_read_sdk import sdk as sdk

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.plugins.gcp._native_access import GCEOwnedRead
    from agentworks.plugins.gcp._owned_auth import _OwnedRequest


def copied(reader: GCEOwnedRead, seconds: float = 5) -> _OwnedRequest:
    reader._auth.begin(Deadline.after(seconds))
    try:
        root = reader._auth._request
        assert root is not None
        return deepcopy(root)
    finally:
        reader._auth.end()


def run_copy(clone: Any, *, after: Any = None, **kwargs: Any) -> tuple[Thread, list[Any]]:
    outcomes: list[Any] = []

    def consume() -> None:
        try:
            reply = clone("https://credential.invalid/copied", **kwargs)
            outcomes.append(reply.data)
            if after is not None:
                after()
        except BaseException as error:
            outcomes.append(error)

    worker = Thread(target=consume)
    worker.start()
    return worker, outcomes


@pytest.mark.parametrize("security", [{}, {"cert": ("offline-cert", "offline-key"), "verify": "offline-ca"}])
def test_public_copy_preserves_exact_hook_and_session_security(sdk, security):
    reader, _ = observe(sdk)
    root = reader._auth._request
    assert root is not None
    root.session.headers["X-Selected-Session"] = "kept"
    root.session.cert = security.get("cert")
    root.session.verify = security.get("verify", True)

    def unrelated(response, **kwargs):
        return None

    root.session.hooks["response"].append(unrelated)
    original_hooks = tuple(root.session.hooks["response"])
    clone = copied(reader)
    assert isinstance(clone, Request) and clone is not root and clone.session is not root.session
    assert tuple(root.session.hooks["response"]) == original_hooks
    assert clone.session.hooks["response"] == [clone._response_hook, unrelated]
    assert clone.session.headers["X-Selected-Session"] == "kept"
    worker, outcomes = run_copy(clone)
    worker.join(5)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    assert sdk.sends[-1][2]["cert"] == root.session.cert
    assert sdk.sends[-1][2]["verify"] == root.session.verify
    assert root._responses == [] and len(clone._responses) == 1
    assert not reader.cleanup_incomplete
    assert reader.close(Deadline.after(5))


def test_never_entered_clone_barred_before_original_session_close(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader)
    original = clone.session
    assert reader.close(Deadline.after(5))
    assert clone.session is None and original is not None
    worker, outcomes = run_copy(clone)
    worker.join(5)
    assert not worker.is_alive() and len(outcomes) == 1 and isinstance(outcomes[0], StateError)
    assert len(sdk.sends) == 1


def test_expired_never_entered_clone_retires_at_later_explicit_operation(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader, 1)
    sdk.now = 3
    assert reader.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    assert clone.session is None and clone not in reader._auth._copies
    assert reader.close(Deadline.after(5))


def test_end_and_later_operation_do_not_rebind_valid_copy_budget(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader, 3)
    original_deadline = clone._deadline
    sdk.now = 3
    reader._auth.begin(Deadline.after(30))
    try:
        worker, outcomes = run_copy(clone)
        worker.join(5)
        assert not worker.is_alive() and outcomes == [sdk.credential_body]
        assert clone._deadline is original_deadline and sdk.sends[-1][2]["timeout"] == 1
    finally:
        reader._auth.end()
    assert reader.close(Deadline.after(5))


def test_entered_consumer_must_finish_before_response_or_session_retirement(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader)
    consuming, release = Event(), Event()

    def consume_after_return() -> None:
        consuming.set()
        assert release.wait(5)

    worker, outcomes = run_copy(clone, after=consume_after_return)
    try:
        assert consuming.wait(5)
        original_session = clone.session
        originals = tuple(clone._responses)
        assert originals and not reader.cleanup_incomplete
        assert not reader.close(Deadline.after(0.01))
        assert reader.cleanup_incomplete and clone.session is original_session
        assert tuple(clone._responses) == originals
        assert not any(handle in originals for _, handle in sdk.closes)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and outcomes == [sdk.credential_body]
    assert reader.close(Deadline.after(5))
    assert clone.session is None and clone._responses == []


def test_second_consumer_of_same_copy_is_refused(sdk):
    reader, _ = observe(sdk)
    clone = copied(reader)
    first, outcomes = run_copy(clone)
    first.join(5)
    assert outcomes == [sdk.credential_body]
    second, refused = run_copy(clone)
    second.join(5)
    assert len(refused) == 1 and isinstance(refused[0], StateError)
    assert len(sdk.sends) == 2
    assert reader.close(Deadline.after(5))


def test_exact_retained_copy_capacity_refuses_before_middleware(sdk):
    reader, _ = observe(sdk)
    clones = [copied(reader) for _ in range(_MAX_RETAINED_COPIES)]
    assert len(reader._auth._copies) == 32
    with pytest.raises(StateError):
        reader.observe_power(Deadline.after(5))
    assert len(sdk.sends) == 1 and len(sdk.adc_calls) == 1
    assert reader.close(Deadline.after(5))
    assert all(clone.session is None for clone in clones)


@pytest.mark.parametrize("failure", ["copy", "wrapper"])
def test_copy_failure_caught_by_sdk_still_refuses_service_and_retains_originals(sdk, monkeypatch, sa_document, failure):
    from agentworks.plugins.gcp import _owned_auth

    factory = cast("Callable[..., Any]", service_account.Credentials.from_service_account_info)
    selected = factory(sa_document, scopes=("https://www.googleapis.com/auth/cloud-platform",))
    selected.token = "fresh-selected-token"
    sdk.credential = selected
    reader, _ = make_reader()
    retained: list[Any] = []
    ordinary_attach = _owned_auth._OwnedRequest.attach

    def attach(lane: Any, session: Any) -> None:
        if lane._deadline is None:
            ordinary_attach(lane, session)
            return
        retained.append(session)
        lane.session = session
        raise RuntimeError("inert wrapper handoff failure")

    if failure == "copy":
        monkeypatch.setattr(_owned_auth, "deepcopy", lambda *args: (_ for _ in ()).throw(RuntimeError("copy")))
    else:
        monkeypatch.setattr(_owned_auth._OwnedRequest, "attach", attach)
    with pytest.raises(StateError):
        reader.observe_power(Deadline.after(5))
    monkeypatch.setattr(_owned_auth._OwnedRequest, "attach", ordinary_attach)
    assert not sdk.sends
    if failure == "wrapper":
        assert reader._auth._copies[0].session is retained[0]
    assert reader.close(Deadline.after(5))


@pytest.mark.parametrize("failure", [None, "ordinary", "control"])
def test_actual_default_service_account_worker_join_and_original_close(sdk, sa_document, failure):
    factory = cast("Callable[..., Any]", service_account.Credentials.from_service_account_info)
    selected = factory(sa_document, scopes=("https://www.googleapis.com/auth/cloud-platform",))
    selected.token = "already-fresh-selected-token"
    sdk.credential = selected
    entered, release = Event(), Event()

    def boundary() -> None:
        entered.set()
        assert release.wait(5)

    sdk.on_boundary = boundary
    reader, power = observe(sdk)
    assert power is VMStatus.RUNNING and entered.wait(5)
    clone = reader._auth._copies[0]
    original_session, worker = clone.session, clone._worker
    try:
        assert worker is not None and worker.is_alive()
        assert not reader.cleanup_incomplete
        assert not reader.close(Deadline.after(0.01))
        assert clone.session is original_session and reader.cleanup_incomplete
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    originals = tuple(clone._responses)
    assert len(originals) == 1 and selected.regional_access_boundary == "0x1"
    primary = KeyboardInterrupt("boundary-close-control")
    if failure == "ordinary":
        sdk.failed_close = "boundary-response"
    elif failure == "control":
        sdk.close_control = primary
    if failure == "control":
        with pytest.raises(KeyboardInterrupt) as caught:
            reader.close(Deadline.after(5))
        assert caught.value is primary
    else:
        assert reader.close(Deadline.after(5)) is (failure is None)
    if failure is not None:
        assert tuple(clone._responses) == originals and reader.cleanup_incomplete
        sdk.failed_close = sdk.close_control = None
        assert reader.close(Deadline.after(5))
    assert sorted(kind for kind, _, _ in sdk.sends) == ["boundary", "service"]
    assert reader._auth._credential is selected
    assert not reader._auth._copies


@pytest.mark.parametrize("boundary", ["candidate-is-alive", "join", "post-join-is-alive"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_poisoned_owned_thread_observation_retains_copy_on_later_retry(sdk, monkeypatch, boundary, error_type):
    reader, _ = observe(sdk)
    clone = copied(reader)
    consuming, release = Event(), Event()

    def after_return() -> None:
        consuming.set()
        assert release.wait(5)

    worker, outcomes = run_copy(clone, after=after_return)
    original_join, original_alive = worker.join, worker.is_alive
    primary = error_type("unconfirmed consumer observation")
    observations: list[str] = []

    def poisoned_alive():
        observations.append("is_alive")
        if observations == ["is_alive"]:
            raise primary
        return False

    def poisoned_join(timeout=None):
        observations.append("join")
        if observations == ["join"]:
            raise primary

    def nonblocking_join(timeout=None):
        original_join(0)

    try:
        assert consuming.wait(5) and original_alive()
        original_session = clone.session
        originals = tuple(clone._responses)
        assert original_session is not None and len(originals) == 1
        if boundary == "join":
            monkeypatch.setattr(worker, "join", poisoned_join)
        else:
            monkeypatch.setattr(worker, "is_alive", poisoned_alive)
            if boundary == "post-join-is-alive":
                monkeypatch.setattr(worker, "join", nonblocking_join)
        if boundary == "candidate-is-alive":
            with pytest.raises(error_type) as caught:
                reader._auth.begin(Deadline.after(5))
            assert caught.value is primary
        elif error_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                reader.close(Deadline.after(5))
            assert caught.value is primary
        else:
            assert not reader.close(Deadline.after(5))
        assert clone._completion_unconfirmed and reader.cleanup_incomplete
        before = tuple(observations)
        assert not reader.close(Deadline.after(5))
        assert tuple(observations) == before and original_alive()
        assert clone._worker is worker and clone.session is original_session
        assert tuple(clone._responses) == originals and clone in reader._auth._copies
        assert not any(handle is original_session or handle in originals for _, handle in sdk.closes)
        with pytest.raises(StateError):
            reader.observe_power(Deadline.after(5))
        assert len(sdk.sends) == 2
    finally:
        monkeypatch.setattr(worker, "join", original_join)
        monkeypatch.setattr(worker, "is_alive", original_alive)
        release.set()
        original_join(5)
    assert not original_alive() and outcomes == [sdk.credential_body]
    assert not reader.close(Deadline.after(5))
    assert clone.session is original_session and tuple(clone._responses) == originals
    assert clone._worker is worker and clone in reader._auth._copies


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_poisoned_sdk_observation_keeps_late_arriving_original_response(sdk, monkeypatch, sa_document, error_type):
    factory = cast("Callable[..., Any]", service_account.Credentials.from_service_account_info)
    selected = factory(sa_document, scopes=("https://www.googleapis.com/auth/cloud-platform",))
    selected.token = "already-fresh-selected-token"
    sdk.credential = selected
    entered, release = Event(), Event()

    def boundary() -> None:
        entered.set()
        assert release.wait(5)

    sdk.on_boundary = boundary
    reader, power = observe(sdk)
    assert power is VMStatus.RUNNING and entered.wait(5)
    clone = reader._auth._copies[0]
    worker = clone._worker
    assert worker is not None
    original_join, original_alive = worker.join, worker.is_alive
    primary = error_type("SDK observation uncertainty")
    observations: list[bool] = []

    def poisoned_alive():
        observations.append(True)
        if len(observations) == 1:
            raise primary
        return False

    try:
        assert entered.wait(5) and original_alive()
        original_session = clone.session
        assert original_session is not None and not clone._responses
        reader._auth.begin(Deadline.after(5))
        try:
            monkeypatch.setattr(worker, "is_alive", poisoned_alive)
            with pytest.raises(error_type) as caught:
                reader._auth.apply_headers("GET", reader._url())
            assert caught.value is primary
        finally:
            reader._auth.end()
        assert clone._completion_unconfirmed and reader.cleanup_incomplete
        assert not reader.close(Deadline.after(5))
        assert observations == [True] and original_alive()
        assert clone._worker is worker and clone.session is original_session and not clone._responses
        assert not any(handle is original_session for _, handle in sdk.closes)
    finally:
        monkeypatch.setattr(worker, "is_alive", original_alive)
        release.set()
        original_join(5)
    assert not original_alive() and selected.regional_access_boundary == "0x1"
    originals = tuple(clone._responses)
    assert len(originals) == 1
    assert not reader.close(Deadline.after(5))
    assert clone.session is original_session and tuple(clone._responses) == originals
    assert clone._worker is worker and clone in reader._auth._copies
    assert not any(handle is original_session or handle in originals for _, handle in sdk.closes)
    with pytest.raises(StateError):
        reader.observe_power(Deadline.after(5))
    assert sorted(kind for kind, _, _ in sdk.sends) == ["boundary", "service"]
