"""Which outcomes of a GCE route's insert make its removal name the rule for a check by hand.

An insert whose outcome stayed unknown may still land after a removal that
found nothing, so that removal warns, naming the rule's identity and its
sources. One whose outcome is known (Google Cloud refused its only request,
its operation completed, or the rule was seen) does not, nor does a removal
that found the rule and deleted it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import compute_v1

from agentworks.errors import AuthorizationError, ConnectivityError, TokenRejectedError
from agentworks.plugins.gcp.errors import GCEIndeterminateOperationError, GCEOperationError
from tests._gcp_platform_fakes import FakeOperation, api_error, gce_ctx, gce_guest, gce_platform, gce_request
from tests.plugins.gcp.test_platform import _vm

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from agentworks.plugins.gcp.platform import GCEPlatform
    from tests._gcp_platform_fakes import FakeClients
    from tests.conftest import CapturedOutput


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> tuple[GCEPlatform, FakeClients, VMRow]:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    return platform, cache, _vm(platform.create(gce_request(), gce_ctx()).platform_metadata)


def _open(platform: GCEPlatform, vm: VMRow, raised: type[BaseException]) -> None:
    with pytest.raises(raised), platform._transient_route(vm, gce_ctx()):
        pytest.fail("the body must not run when the insert does not return")


def _route(cache: FakeClients) -> compute_v1.Firewall:
    return cache.firewalls.insert_requests[-1].firewall_resource


def _assert_named(captured_output: CapturedOutput, route: compute_v1.Firewall, identity: str) -> None:
    (warning,) = captured_output.warnings
    assert identity in warning
    assert all(prefix in warning for prefix in route.source_ranges)


@pytest.mark.parametrize(
    ("first", "retry", "raised"),
    [
        (TimeoutError("no answer"), TimeoutError("no answer"), ConnectivityError),
        (ConnectionError("reset"), api_error(auth_exceptions.RefreshError, "token"), TokenRejectedError),
        (KeyboardInterrupt(), None, KeyboardInterrupt),
    ],
    ids=["unanswered-twice", "retry-refused-after-no-answer", "interrupted"],
)
def test_an_insert_whose_outcome_stayed_unknown_is_named_when_its_removal_finds_nothing(
    created: tuple[GCEPlatform, FakeClients, VMRow],
    captured_output: CapturedOutput,
    first: BaseException,
    retry: BaseException | None,
    raised: type[BaseException],
) -> None:
    """A refusal of the same-requestId retry answers the retry alone: the first try may still land."""
    platform, cache, vm = created
    cache.firewalls.insert_failures = [first, retry]

    _open(platform, vm, raised)

    _assert_named(captured_output, _route(cache), _route(cache).name)


def test_an_insert_refused_on_its_only_try_is_not_named(
    created: tuple[GCEPlatform, FakeClients, VMRow], captured_output: CapturedOutput
) -> None:
    platform, cache, vm = created
    cache.firewalls.insert_failures = [api_error(api_exceptions.PermissionDenied, "denied")]

    _open(platform, vm, AuthorizationError)

    assert captured_output.warnings == []


def _answered_with(monkeypatch: pytest.MonkeyPatch, cache: FakeClients, **operation: Any) -> None:
    """The insert answers with an operation shaped by ``operation``, and its rule never appears."""
    insert = cache.firewalls.insert

    def _insert(**kwargs: Any) -> FakeOperation:
        accepted = insert(**kwargs)
        cache.firewalls.resources.pop(kwargs["request"].firewall_resource.name)
        shape = {
            "request_id": accepted.client_operation_id,
            "operation_type": "insert",
            "target_link": accepted.target_link,
            "target_id": accepted.target_id,
            **operation,
        }
        return FakeOperation(**shape)

    monkeypatch.setattr(cache.firewalls, "insert", _insert)


def test_an_insert_whose_operation_completed_with_a_failure_is_not_named(
    monkeypatch: pytest.MonkeyPatch, created: tuple[GCEPlatform, FakeClients, VMRow], captured_output: CapturedOutput
) -> None:
    platform, cache, vm = created
    _answered_with(
        monkeypatch,
        cache,
        failure=RuntimeError("operation failed"),
        status=compute_v1.Operation.Status.DONE,
        error=compute_v1.Error(errors=[compute_v1.Errors(code="UNKNOWN_CODE")]),
    )

    _open(platform, vm, GCEOperationError)

    assert captured_output.warnings == []


def test_an_insert_whose_operation_identity_never_arrived_is_named(
    monkeypatch: pytest.MonkeyPatch, created: tuple[GCEPlatform, FakeClients, VMRow], captured_output: CapturedOutput
) -> None:
    """The operation exists, so the insert may land, but nothing ties a rule to it."""
    platform, cache, vm = created
    _answered_with(monkeypatch, cache, request_id="someone-else")

    _open(platform, vm, GCEOperationError)

    _assert_named(captured_output, _route(cache), _route(cache).name)


def test_an_insert_whose_wait_ran_out_is_named_by_its_provider_id(
    monkeypatch: pytest.MonkeyPatch, created: tuple[GCEPlatform, FakeClients, VMRow], captured_output: CapturedOutput
) -> None:
    platform, cache, vm = created
    _answered_with(monkeypatch, cache, failure=TimeoutError("still running"))

    _open(platform, vm, GCEIndeterminateOperationError)

    (request_id,) = {request.request_id for request in cache.firewalls.insert_requests[-1:]}
    assert request_id
    (warning,) = captured_output.warnings
    assert str(cache.firewalls._next_id - 1) in warning


def test_a_removal_that_found_and_deleted_the_rule_does_not_name_it(
    monkeypatch: pytest.MonkeyPatch, created: tuple[GCEPlatform, FakeClients, VMRow], captured_output: CapturedOutput
) -> None:
    """The wait ran out before the rule showed, then the removal found it: nothing is left to land."""
    platform, cache, vm = created
    cache.firewalls.insert_wait_failures = [TimeoutError("still running")]
    get = cache.firewalls.get
    reads: list[str] = []

    def _late_get(*, firewall: str, **kwargs: Any) -> compute_v1.Firewall:
        reads.append(firewall)
        if reads.count(firewall) == 2:  # the insert's reconcile, after the name check
            raise api_error(api_exceptions.NotFound, "not yet")
        return get(firewall=firewall, **kwargs)

    monkeypatch.setattr(cache.firewalls, "get", _late_get)

    _open(platform, vm, GCEIndeterminateOperationError)

    assert _route(cache).name not in cache.firewalls.resources
    assert captured_output.warnings == []


def test_a_failure_in_the_routes_body_does_not_name_the_rule(
    created: tuple[GCEPlatform, FakeClients, VMRow], captured_output: CapturedOutput
) -> None:
    platform, cache, vm = created

    with pytest.raises(RuntimeError), platform._transient_route(vm, gce_ctx()):
        raise RuntimeError("the script failed")

    assert _route(cache).name not in cache.firewalls.resources
    assert captured_output.warnings == []
