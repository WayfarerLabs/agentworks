"""GCE create rollback while an insert's outcome is unknown: what it keeps, and what it reports."""

from __future__ import annotations

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.vm_platform import ssh_exposure
from agentworks.errors import AgentworksError
from tests._gcp_platform_fakes import FakeClients, api_error, gce_ctx, gce_guest, gce_platform, gce_request


@pytest.fixture(autouse=True)
def _egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: "198.18.0.7")


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    caught: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", caught.append)
    return caught


def _rule_names(cache: FakeClients) -> tuple[str, str]:
    """The deny and allow names the create inserted, in that order."""
    deny, allow = (str(request.firewall_resource.name) for request in cache.firewalls.insert_requests[:2])
    return deny, allow


_UNANSWERED = api_error(api_exceptions.ServiceUnavailable, "lost")


@pytest.mark.parametrize(
    ("wait_failure", "insert_failures", "raised", "operation"),
    [
        (TimeoutError(), [], AgentworksError, "operation-insert-201"),
        (KeyboardInterrupt("first"), [], KeyboardInterrupt, "operation-insert-201"),
        (None, [_UNANSWERED, _UNANSWERED], AgentworksError, None),
    ],
    ids=("wait-times-out", "wait-interrupted", "insert-unanswered"),
)
def test_an_instance_insert_still_unresolved_keeps_the_deny_and_reports_it(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    wait_failure: BaseException | None,
    insert_failures: list[BaseException | None],
    raised: type[BaseException],
    operation: str | None,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    cache.instances.insert_lands = False  # accepted, but the instance has not appeared yet
    cache.instances.insert_failures = list(insert_failures)
    if wait_failure is not None:
        cache.instances.insert_wait_failures = [wait_failure]

    with pytest.raises(raised):
        platform.create(gce_request(), gce_ctx())

    deny, allow = _rule_names(cache)
    assert set(cache.firewalls.resources) == {deny}
    assert [request.firewall for request in cache.firewalls.delete_requests] == [allow]
    message = "\n".join(warnings)
    assert deny in message
    if operation is not None:
        assert operation in message
        assert "'201'" in message  # the operation's target, the instance's expected provider ID


@pytest.mark.parametrize("landed", [False, True], ids=("never-created", "created-then-failed"))
def test_a_completed_instance_insert_cleans_up_fully(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    landed: bool,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    cache.instances.insert_lands = landed
    cache.instances.insert_operation_errors = [compute_v1.Error(errors=[compute_v1.Errors(code="QUOTA_EXCEEDED")])]

    with pytest.raises(AgentworksError):
        platform.create(gce_request(), gce_ctx())

    assert cache.firewalls.resources == {}
    assert cache.instances.resource is None
    assert warnings == []


def test_an_unresolved_allow_insert_warns_that_the_rule_may_still_appear(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    cache.firewalls.insert_failures = [None, _UNANSWERED, _UNANSWERED]

    with pytest.raises(AgentworksError):
        platform.create(gce_request(), gce_ctx())

    _deny, allow = _rule_names(cache)
    assert cache.firewalls.resources == {}
    assert len(warnings) == 1
    assert allow in warnings[0]


def test_a_refused_allow_insert_needs_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    cache.firewalls.insert_failures = [None, api_error(api_exceptions.PermissionDenied, "denied")]

    with pytest.raises(AgentworksError):
        platform.create(gce_request(), gce_ctx())

    assert cache.firewalls.resources == {}
    assert warnings == []
