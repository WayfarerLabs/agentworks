"""Which outcomes of an EC2 authorize let the poke's revoke stand without a warning.

An authorize that did not return normally is revoked, and its prefix named
for checking by hand, unless its outcome is known: EC2 answered the one
request sent with a client error. These drive a real botocore EC2 client
whose sends are answered in memory (a ``before-send`` handler), since what
they pin is how botocore retries a send on its own and what it records of
that: an answer after a retry may follow a send that landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

from agentworks.errors import AgentworksError
from agentworks.plugins.aws.network import poke_ssh_allow

if TYPE_CHECKING:
    from collections.abc import Callable

_GROUP = "sg-0123456789abcdef0"
_FIRST = "198.18.0.7/32"
_SECOND = "198.51.100.0/24"
_ENDPOINT = "https://ec2.us-east-1.amazonaws.com/"

# What one send is answered with: an exception the transport raises, or an HTTP status and EC2 error code.
type _Answer = BaseException | tuple[int, str | None]


class _Body:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_kwargs: Any) -> Any:
        yield self._body


def _response(status: int, code: str | None) -> AWSResponse:
    if code is None:
        body = b"<Response><return>true</return></Response>"
    else:
        body = (
            f"<Response><Errors><Error><Code>{code}</Code><Message>m</Message></Error></Errors>"
            "<RequestID>r</RequestID></Response>"
        ).encode()
    return AWSResponse(_ENDPOINT, status, {}, _Body(body))


def _ec2(authorize: list[_Answer], *, attempts: int = 2) -> Any:
    """A real EC2 client making up to ``attempts`` sends, its authorize sends answered by ``authorize`` in order.

    Every revoke succeeds (its dry run answering ``DryRunOperation``).
    """
    session = boto3.session.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="x", region_name="us-east-1")
    client = session.client("ec2", config=Config(retries={"mode": "standard", "total_max_attempts": attempts}))
    answers = list(authorize)

    def _answer(request: Any, **_kwargs: Any) -> AWSResponse:
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body)
        if "Action=RevokeSecurityGroupIngress" in body:
            dry_run = "DryRun=true" in body
            return _response(412, "DryRunOperation") if dry_run else _response(200, None)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _response(*answer)

    client.meta.events.register("before-send.ec2.*", _answer)
    return client


def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("botocore.retries.standard.ExponentialBackoff.delay_amount", lambda *_args: 0)


def _timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url=_ENDPOINT)


def _closed() -> ConnectionClosedError:
    return ConnectionClosedError(endpoint_url=_ENDPOINT)


_REFUSED = (403, "UnauthorizedOperation")


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param(lambda: [_timeout(), _timeout()], id="read-timeout"),
        pytest.param(lambda: [_closed(), _closed()], id="connection-closed"),
        pytest.param(lambda: [_timeout(), _REFUSED], id="lost-then-refused"),
    ],
)
def test_an_authorize_whose_outcome_is_unknown_warns_naming_its_group_and_prefix(
    monkeypatch: pytest.MonkeyPatch, warnings: list[str], answers: Callable[[], list[_Answer]]
) -> None:
    _no_backoff(monkeypatch)

    with pytest.raises(AgentworksError):
        poke_ssh_allow(_ec2(answers()), _GROUP, [_FIRST])

    (warning,) = warnings
    assert _GROUP in warning
    assert _FIRST in warning


def test_a_refusal_of_the_only_send_needs_no_warning(monkeypatch: pytest.MonkeyPatch, warnings: list[str]) -> None:
    _no_backoff(monkeypatch)

    with pytest.raises(AgentworksError):
        poke_ssh_allow(_ec2([_REFUSED]), _GROUP, [_FIRST])

    assert warnings == []


def test_the_warning_names_only_the_prefix_whose_authorize_went_unanswered(
    monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    _no_backoff(monkeypatch)

    with pytest.raises(AgentworksError):
        poke_ssh_allow(_ec2([(200, None), _timeout(), _timeout()]), _GROUP, [_FIRST, _SECOND])

    (warning,) = warnings
    assert _SECOND in warning
    assert _FIRST not in warning


def test_a_server_error_answering_the_only_send_warns(monkeypatch: pytest.MonkeyPatch, warnings: list[str]) -> None:
    """A 5xx does not say EC2 never applied the request."""
    _no_backoff(monkeypatch)

    with pytest.raises(AgentworksError):
        poke_ssh_allow(_ec2([(500, "InternalError")], attempts=1), _GROUP, [_FIRST])

    (warning,) = warnings
    assert _FIRST in warning


class _Authorizes:
    """An EC2 client whose dry runs and revokes succeed, and whose authorizes answer from ``answers`` in order."""

    def __init__(self, answers: list[BaseException | None]) -> None:
        self.answers = answers

    def revoke_security_group_ingress(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("DryRun"):
            raise ClientError({"Error": {"Code": "DryRunOperation"}}, "RevokeSecurityGroupIngress")
        return {}

    def authorize_security_group_ingress(self, **_kwargs: Any) -> dict[str, Any]:
        answer = self.answers.pop(0)
        if answer is not None:
            raise answer
        return {}


def test_an_error_without_botocore_s_retry_record_warns(warnings: list[str]) -> None:
    """Without ``RetryAttempts`` nothing shows the refusal answered the only send."""
    refused = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "AuthorizeSecurityGroupIngress")

    with pytest.raises(AgentworksError):
        poke_ssh_allow(_Authorizes([refused]), _GROUP, [_FIRST])

    (warning,) = warnings
    assert _FIRST in warning


def test_an_interrupt_names_only_the_prefix_whose_authorize_it_cut_short(warnings: list[str]) -> None:
    with pytest.raises(KeyboardInterrupt):
        poke_ssh_allow(_Authorizes([None, KeyboardInterrupt()]), _GROUP, [_FIRST, _SECOND])

    (warning,) = warnings
    assert _SECOND in warning
    assert _FIRST not in warning
