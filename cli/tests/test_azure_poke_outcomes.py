"""Which outcomes of an Azure poke's create let its cleanup delete stand without a warning.

A create that did not return normally is removed, and its rule named for
removal by hand, unless its outcome is known: Azure refused its only
request, or reported that it succeeded. Most cases drive the real
azure-core poller and generated network client over an in-memory
``requests`` adapter, since what they pin is how azure-core sends the
create and records what Azure answered: the request goes once, and a
failure to observe the operation becomes its own ``Failed``. Retries go
to the fake rules, so the poke returns and the first attempt's cleanup is
what each case inspects.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import pytest
import requests
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport
from azure.core.polling.base_polling import LROBasePolling
from azure.mgmt.network import NetworkManagementClient
from requests.adapters import BaseAdapter

from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure.network import TRANSIENT_ALLOW_RULE_PREFIX, _RouteOperationTimeout, poke_ssh_allow
from tests._azure_platform_support import _FakeAzure, _install_fakes, _Poller

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

_PREFIX = "198.18.0.7/32"
_MONITOR = "https://management.azure.com/subscriptions/sub-A/providers/Microsoft.Network/operations/1?api-version=1"

# An answer is a status, headers and a JSON body (raw bytes when not JSON), or an error the adapter raises.
_Answer = tuple[int, dict[str, str], object] | Exception

_ACCEPTED: _Answer = (201, {"Azure-AsyncOperation": _MONITOR}, {"properties": {"provisioningState": "Updating"}})


def _refused(status: int) -> _Answer:
    return (status, {}, {"error": {"code": "Refused", "message": f"status {status}"}})


def _reported(status: str) -> _Answer:
    return (200, {}, {"status": status})


class _Credential:
    def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
        return AccessToken("token", 2**31)


class _ScriptedAdapter(BaseAdapter):
    """Answers each request with the next scripted answer, repeating the last, and records its method."""

    def __init__(self, answers: list[_Answer]) -> None:
        super().__init__()
        self._answers = answers
        self.methods: list[str] = []

    def send(self, request: requests.PreparedRequest, *_args: object, **_kwargs: object) -> requests.Response:
        self.methods.append(str(request.method))
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, headers, body = answer
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.raw = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        response.request = request
        response.url = str(request.url)
        return response

    def close(self) -> None:
        return None


def _poke_after(monkeypatch: pytest.MonkeyPatch, first: Callable[..., Any]) -> tuple[_FakeAzure, str, list[float]]:
    """Poke with the first create through ``first`` and any retry through the fakes.

    Returns the fakes, the first attempt's rule name and the fake clock's reading at each of its deletes.
    """
    fakes = _install_fakes(monkeypatch)
    rules = fakes.network.security_rules
    real_create, real_delete = rules.begin_create_or_update, rules.begin_delete
    first_name: list[str] = []
    deleted_at: list[float] = []

    def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> Any:
        if not first_name:
            first_name.append(rule_name)
            return first(rg, nsg, rule_name, rule, **options)
        return real_create(rg, nsg, rule_name, rule, **options)

    def _delete(rg: str, nsg: str, rule_name: str, **options: Any) -> Any:
        if first_name and rule_name == first_name[0]:
            deleted_at.append(fakes.clock.now)
        return real_delete(rg, nsg, rule_name, **options)

    monkeypatch.setattr(rules, "begin_create_or_update", _create)
    monkeypatch.setattr(rules, "begin_delete", _delete)
    poke_ssh_allow(fakes.network, "rg1", "vm1", [_PREFIX])  # type: ignore[arg-type]
    assert rules.transient_names(), "the retry opens the route"
    return fakes, first_name[0], deleted_at


def _through_sdk(answers: list[_Answer]) -> tuple[Callable[..., Any], _ScriptedAdapter]:
    """A create sent by the generated client over an adapter giving ``answers``, and that adapter."""
    adapter = _ScriptedAdapter(answers)
    session = requests.Session()
    session.mount("https://", adapter)
    sdk = NetworkManagementClient(
        _Credential(),  # type: ignore[arg-type]
        "sub-A",
        transport=RequestsTransport(session=session, session_owner=False),
        retry_backoff_factor=0,
    )
    return sdk.security_rules.begin_create_or_update, adapter


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
    create, _adapter = _through_sdk([_ACCEPTED, monitor])
    fakes, first, deleted_at = _poke_after(monkeypatch, create)

    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert deleted_at == [0.0]  # a final state, if not Azure's, ends the settle at once
    _assert_named(captured_output, first)


def test_a_create_azure_reported_succeeded_is_removed_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The final read failing after Azure reported success leaves a rule that exists, so its delete is authoritative."""
    create, _adapter = _through_sdk([_ACCEPTED, _reported("Succeeded"), (200, {}, b"{not json")])
    fakes, first, _deleted_at = _poke_after(monkeypatch, create)

    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert captured_output.warnings == []


@pytest.mark.parametrize("status", [400, 403, 409], ids=["bad-request", "forbidden", "conflict"])
def test_a_create_azure_refused_is_removed_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, status: int
) -> None:
    """The create's request is sent once, so Azure's refusal of it says the rule never exists."""
    create, adapter = _through_sdk([_refused(status)])
    fakes, first, deleted_at = _poke_after(monkeypatch, create)

    assert adapter.methods == ["PUT"]
    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert deleted_at == [0.0]  # no poller to wait on
    assert captured_output.warnings == []


@pytest.mark.parametrize(
    "answers",
    [
        [requests.exceptions.ReadTimeout("read timed out"), _refused(403)],
        [requests.exceptions.ConnectionError("connection reset")],
        [_refused(504)],
    ],
    ids=["unanswered", "request-unsent", "gateway-timeout"],
)
def test_a_create_azure_did_not_refuse_before_its_poller_is_named(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, answers: list[_Answer]
) -> None:
    """Without Azure's 4xx answer the only try may have reached the network provider; nothing is resent to find out."""
    create, adapter = _through_sdk(answers)
    fakes, first, deleted_at = _poke_after(monkeypatch, create)

    assert adapter.methods == ["PUT"]
    assert ("rg1", "vm1-nsg", first) in fakes.network.security_rules.deletes()
    assert deleted_at == [0.0]  # no poller to wait on
    _assert_named(captured_output, first)


def test_the_creates_status_reads_keep_azure_cores_retries(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Only the create's own request goes unretried; a status read Azure could not serve is read again."""
    create, adapter = _through_sdk([_ACCEPTED, _refused(503), _reported("Succeeded"), (200, {}, {"name": "rule"})])
    fakes = _install_fakes(monkeypatch)
    rules = fakes.network.security_rules
    monkeypatch.setattr(rules, "begin_create_or_update", create)

    poke_ssh_allow(fakes.network, "rg1", "vm1", [_PREFIX])  # type: ignore[arg-type]

    assert adapter.methods == ["PUT", "GET", "GET", "GET"]
    assert rules.deletes() == []
    assert captured_output.warnings == []


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

    def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
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
