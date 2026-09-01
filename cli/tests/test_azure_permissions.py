"""Azure create-time RBAC permission evaluation and runup behavior."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.errors import AuthorizationError
from agentworks.plugins.azure.permissions import (
    REQUIRED_RESOURCE_GROUP_ACTIONS,
    missing_resource_group_actions,
)
from agentworks.plugins.azure.platform import AzureVMPlatform

_CONFIG = {
    "subscription_id": "sub-123",
    "resource_group": "rg-dev",
    "region": "eastus",
    "auth": {"mode": "ambient"},
}
_DELETE_PUBLIC_IP = "Microsoft.Network/publicIPAddresses/delete"


def _block(actions: list[str], not_actions: list[str] | None = None, **extra: object) -> object:
    return SimpleNamespace(actions=actions, not_actions=not_actions or [], **extra)


@pytest.mark.parametrize(
    "actions",
    [
        pytest.param(list(REQUIRED_RESOURCE_GROUP_ACTIONS), id="exact"),
        pytest.param([action.upper() for action in REQUIRED_RESOURCE_GROUP_ACTIONS], id="case-insensitive"),
        pytest.param(["microsoft.compute/*", "MICROSOFT.NETWORK/*"], id="wildcards"),
    ],
)
def test_permission_patterns_grant_required_actions(actions: list[str]) -> None:
    assert missing_resource_group_actions([_block(actions)]) == ()


def test_not_actions_excludes_only_its_permission_block_and_ignores_data_actions() -> None:
    block = _block(["*"], [_DELETE_PUBLIC_IP], data_actions=[_DELETE_PUBLIC_IP], not_data_actions=[])

    assert missing_resource_group_actions([block]) == (_DELETE_PUBLIC_IP,)


def test_separate_permission_block_can_regrant_a_not_action() -> None:
    broad_role = _block(["*"], [_DELETE_PUBLIC_IP])
    narrow_role = _block([_DELETE_PUBLIC_IP])

    assert missing_resource_group_actions([broad_role, narrow_role]) == ()


def _wire_runup(monkeypatch: pytest.MonkeyPatch, listing: object) -> AzureVMPlatform:
    resource = SimpleNamespace(resource_groups=SimpleNamespace(check_existence=lambda _rg: True))
    authorization = SimpleNamespace(permissions=SimpleNamespace(list_for_resource_group=lambda _rg: listing))
    monkeypatch.setattr(AzureVMPlatform, "_resource_client", lambda self, az, ctx: resource)
    monkeypatch.setattr(AzureVMPlatform, "_authorization_client", lambda self, az, ctx: authorization)
    return AzureVMPlatform("az", _CONFIG)


class _TwoPageGrant:
    def __init__(self) -> None:
        self.pages_read: list[int] = []

    def __iter__(self) -> Iterator[object]:
        self.pages_read.append(1)
        yield _block(["*"], [_DELETE_PUBLIC_IP])
        self.pages_read.append(2)
        yield _block([_DELETE_PUBLIC_IP])


def test_runup_consumes_later_permission_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _TwoPageGrant()

    _wire_runup(monkeypatch, listing).runup(RunContext())

    assert listing.pages_read == [1, 2]


class _FailingLaterPage:
    def __iter__(self) -> Iterator[object]:
        yield _block(["*"], [_DELETE_PUBLIC_IP])
        raise ServiceRequestError("paging failed")


def test_later_page_failure_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)

    _wire_runup(monkeypatch, _FailingLaterPage()).runup(RunContext())

    assert len(warnings) == 1


def test_complete_permission_omission_is_fatal_before_mutation_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _wire_runup(monkeypatch, [_block(["*"], [_DELETE_PUBLIC_IP])])

    with pytest.raises(AuthorizationError) as caught:
        platform.runup(RunContext())

    assert caught.value.entity_kind == "resource-group"
    assert caught.value.entity_name == "rg-dev"
    assert platform._compute_cached == {}
    assert platform._network_cached == {}


def _forbidden() -> HttpResponseError:
    error = HttpResponseError(message="forbidden")
    error.status_code = 403
    return error


@pytest.mark.parametrize(
    "query_error",
    [
        pytest.param(_forbidden(), id="http-403"),
        pytest.param(ServiceRequestError("connection failed"), id="transport"),
    ],
)
def test_permission_query_failure_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    query_error: Exception,
) -> None:
    warnings: list[str] = []

    def _raise(_resource_group: str) -> object:
        raise query_error

    resource = SimpleNamespace(resource_groups=SimpleNamespace(check_existence=lambda _rg: True))
    authorization = SimpleNamespace(permissions=SimpleNamespace(list_for_resource_group=_raise))
    monkeypatch.setattr(AzureVMPlatform, "_resource_client", lambda self, az, ctx: resource)
    monkeypatch.setattr(AzureVMPlatform, "_authorization_client", lambda self, az, ctx: authorization)
    monkeypatch.setattr(output, "warn", warnings.append)

    AzureVMPlatform("az", _CONFIG).runup(RunContext())

    assert len(warnings) == 1


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(SimpleNamespace(actions=None, not_actions=[]), id="actions-missing"),
        pytest.param(SimpleNamespace(actions=["*"], not_actions=None), id="not-actions-missing"),
        pytest.param(SimpleNamespace(actions=[3], not_actions=[]), id="action-not-string"),
    ],
)
def test_malformed_permission_response_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)

    _wire_runup(monkeypatch, [malformed]).runup(RunContext())

    assert len(warnings) == 1
