"""Which outcomes of an Azure poke's create let its cleanup delete stand without a warning.

A create that did not return normally is removed, and its rule named for
removal by hand, unless Azure reported that the create succeeded. Most
cases drive the real azure-core poller and generated network client over
an in-memory transport, since what they pin is how azure-core records
what Azure answered: a failure to observe the operation becomes its own
``Failed``, and a refused request can follow an earlier try that got no
answer. Retries go to the fake rules, so the poke returns and the first
attempt's cleanup is what each case inspects.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from azure.core.credentials import AccessToken, AccessTokenInfo
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.core.pipeline.transport import HttpTransport
from azure.core.polling.base_polling import LROBasePolling
from azure.core.rest import HttpRequest
from azure.core.rest._http_response_impl import HttpResponseImpl
from azure.core.utils import CaseInsensitiveDict
from azure.mgmt.network import NetworkManagementClient

from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure.network import TRANSIENT_ALLOW_RULE_PREFIX, _RouteOperationTimeout, poke_ssh_allow
from tests._azure_platform_support import _FakeAzure, _install_fakes, _Poller

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

_PREFIX = "198.18.0.7/32"
_MONITOR = "https://management.azure.com/subscriptions/sub-A/providers/Microsoft.Network/operations/1?api-version=1"

# An answer is a status, headers and a JSON body (raw bytes when not JSON), or an error the transport raises.
_Answer = tuple[int, dict[str, str], object] | Exception

_ACCEPTED: _Answer = (201, {"Azure-AsyncOperation": _MONITOR}, {"properties": {"provisioningState": "Updating"}})


def _refused(status: int) -> _Answer:
    return (status, {}, {"error": {"code": "Refused", "message": f"status {status}"}})


def _reported(status: str) -> _Answer:
    return (200, {}, {"status": status})


class _Credential:
    def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
        return AccessToken("token", 2**31)

    def get_token_info(self, *_scopes: str, **_kwargs: object) -> AccessTokenInfo:
        return AccessTokenInfo("token", 2**31)


class _ScriptedTransport(HttpTransport):  # type: ignore[type-arg]
    """Answers each request with the next scripted answer, repeating the last; retry backoff takes no time."""

    def __init__(self, answers: list[_Answer]) -> None:
        self._answers = answers
        self.methods: list[str] = []

    def __enter__(self) -> _ScriptedTransport:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def sleep(self, duration: float) -> None:
        return None

    def send(self, request: HttpRequest, **_kwargs: object) -> HttpResponseImpl:  # type: ignore[override]
        self.methods.append(request.method)
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, headers, body = answer
        response = HttpResponseImpl(
            request=request,
            internal_response=None,
            status_code=status,
            headers=CaseInsensitiveDict(headers),
            reason="scripted",
            content_type="application/json",
            stream_download_generator=None,
        )
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        response._is_closed = True
        return response


def _poke_after(monkeypatch: pytest.MonkeyPatch, first: Callable[..., Any]) -> tuple[_FakeAzure, str, list[float]]:
    """Poke with the first create through ``first`` and any retry through the fakes.

    Returns the fakes, the first attempt's rule name and the fake clock's reading at each of its deletes.
    """
    fakes = _install_fakes(monkeypatch)
    rules = fakes.network.security_rules
    real_create, real_delete = rules.begin_create_or_update, rules.begin_delete
    first_name: list[str] = []
    deleted_at: list[float] = []

    def _create(rg: str, nsg: str, rule_name: str, rule: object) -> Any:
        if not first_name:
            first_name.append(rule_name)
            return first(rg, nsg, rule_name, rule)
        return real_create(rg, nsg, rule_name, rule)

    def _delete(rg: str, nsg: str, rule_name: str) -> Any:
        if first_name and rule_name == first_name[0]:
            deleted_at.append(fakes.clock.now)
        return real_delete(rg, nsg, rule_name)

    monkeypatch.setattr(rules, "begin_create_or_update", _create)
    monkeypatch.setattr(rules, "begin_delete", _delete)
    poke_ssh_allow(fakes.network, "rg1", "vm1", [_PREFIX])  # type: ignore[arg-type]
    assert rules.transient_names(), "the retry opens the route"
    return fakes, first_name[0], deleted_at


def _through_sdk(answers: list[_Answer]) -> Callable[..., Any]:
    """A first create sent by the generated client over a transport giving ``answers``."""
    sdk = NetworkManagementClient(
        _Credential(),  # type: ignore[arg-type]
        "sub-A",
        transport=_ScriptedTransport(answers),
        polling_interval=0,
    )
    return sdk.security_rules.begin_create_or_update


def _assert_named(captured_output: CapturedOutput, rule_name: str) -> None:
    (warning,) = captured_output.warnings
    assert rule_name in warning
    assert _PREFIX in warning


@pytest.mark.parametrize(
    "monitor",
    [
        _refused(403),
        (200, {}, {"no": "status"}),
        _reported("Failed"),
        _reported("Canceled"),
    ],
    ids=["status-read-refused", "status-read-without-status", "provider-failed", "provider-canceled"],
)
def test_an_accepted_create_that_azure_core_records_as_failed_is_named(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, monitor: _Answer
) -> None:
    """azure-core records a status read it could not use as ``Failed``, which says nothing of the create."""
    fakes, first, deleted_at = _poke_after(monkeypatch, _through_sdk([_ACCEPTED, monitor]))

    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert deleted_at == [0.0]  # a final state, if not Azure's, ends the settle at once
    _assert_named(captured_output, first)


def test_a_create_azure_reported_succeeded_is_removed_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The final read failing after Azure reported success leaves a rule that exists, so its delete is authoritative."""
    fakes, first, _deleted_at = _poke_after(
        monkeypatch, _through_sdk([_ACCEPTED, _reported("Succeeded"), (200, {}, b"{not json")])
    )

    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert captured_output.warnings == []


@pytest.mark.parametrize(
    "answers",
    [
        [_refused(400)],
        [_refused(403)],
        [ServiceResponseError("read timed out"), _refused(403)],
        [_refused(504)],
        [ServiceRequestError("connection reset")],
    ],
    ids=["bad-request", "forbidden", "forbidden-after-unanswered-try", "gateway-timeout", "request-unsent"],
)
def test_a_create_that_raised_before_its_poller_is_named(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, answers: list[_Answer]
) -> None:
    """azure-core retries a request it got no answer to, so its last error does not prove the first try failed."""
    fakes, first, deleted_at = _poke_after(monkeypatch, _through_sdk(answers))

    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert deleted_at == [0.0]  # no poller to wait on
    _assert_named(captured_output, first)


class _TimedOut(_Poller):
    """A create whose wait runs out, with ``polling`` as its polling method."""

    def __init__(self, polling: object) -> None:
        super().__init__(None, finishes=False)
        self._polling = polling

    def polling_method(self) -> Any:
        return self._polling


def _time_out_with(monkeypatch: pytest.MonkeyPatch, polling: object) -> tuple[_FakeAzure, str]:
    fakes = _install_fakes(monkeypatch)
    rules = fakes.network.security_rules
    real_create = rules.begin_create_or_update

    def _create(rg: str, nsg: str, rule_name: str, rule: object) -> _Poller:
        real_create(rg, nsg, rule_name, rule)
        return _TimedOut(polling)

    monkeypatch.setattr(rules, "begin_create_or_update", _create)
    with pytest.raises(_RouteOperationTimeout):
        poke_ssh_allow(fakes.network, "rg1", "vm1", [_PREFIX])  # type: ignore[arg-type]
    (attempted,) = rules.transient_names()
    assert attempted.startswith(TRANSIENT_ALLOW_RULE_PREFIX)
    assert ("rg1", "vm1-nsg", attempted) in rules.deletes()
    return fakes, attempted


@pytest.mark.parametrize(
    ("final", "warns"),
    [("Succeeded", False), ("succeeded", False), ("Failed", True), ("Canceled", True), ("InProgress", True)],
)
def test_a_timed_out_create_is_named_unless_azure_reports_it_succeeded_within_the_settle(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, final: str, warns: bool
) -> None:
    """azure-core's own polling method decides which states end the settle; only success ends the doubt."""
    polling: LROBasePolling[Any, Any, Any, Any] = LROBasePolling()
    states = iter(["InProgress", "InProgress"])
    monkeypatch.setattr(polling, "status", lambda: next(states, final))

    fakes, attempted = _time_out_with(monkeypatch, polling)

    if final == "InProgress":
        assert fakes.clock.now == azure_network._UNFINISHED_CREATE_SETTLE_SECONDS
    if warns:
        _assert_named(captured_output, attempted)
    else:
        assert captured_output.warnings == []


def test_a_state_that_cannot_be_read_counts_as_unconfirmed_and_the_rule_still_goes(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A failed read of the state never skips the removal; it warns instead."""

    class _Unreadable:
        def finished(self) -> bool:
            raise RuntimeError("no state to read")

        def status(self) -> str:
            raise RuntimeError("no state to read")

    _fakes, attempted = _time_out_with(monkeypatch, _Unreadable())

    _assert_named(captured_output, attempted)
