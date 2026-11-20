"""Which outcomes of an EC2 authorize let the poke's revoke stand without a warning.

The poke authorizes through a client that sends each request once
(``single_send_config``), so an authorize's one answer is all there is. One
that did not return normally is revoked, and its prefix named for checking
by hand, unless its outcome is known: EC2 refused that send, with a client
error or a throttling answer. These drive a real botocore EC2 client whose
sends are answered in memory (a ``before-send`` handler), since what they
pin is how botocore sends and classifies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

from agentworks.errors import AgentworksError
from agentworks.plugins.aws.network import poke_ssh_allow, single_send_config

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


class _EC2:
    """A real EC2 client built with the poke's own config, its authorize sends answered by ``authorize`` in order.

    Every revoke succeeds (its dry run answering ``DryRunOperation``), and
    ``authorize_sends`` counts the authorize sends that went out.
    """

    def __init__(self, authorize: list[_Answer]) -> None:
        session = boto3.session.Session(
            aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="x", region_name="us-east-1"
        )
        self.client = session.client("ec2", config=single_send_config())
        self.answers = list(authorize)
        self.authorize_sends = 0
        self.client.meta.events.register("before-send.ec2.*", self._answer)

    def _answer(self, request: Any, **_kwargs: Any) -> AWSResponse:
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body)
        if "Action=RevokeSecurityGroupIngress" in body:
            dry_run = "DryRun=true" in body
            return _response(412, "DryRunOperation") if dry_run else _response(200, None)
        self.authorize_sends += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _response(*answer)

    def poke(self, prefixes: list[str]) -> None:
        poke_ssh_allow(self.client, _GROUP, prefixes, authorize=self.client)


def _timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url=_ENDPOINT)


def _closed() -> ConnectionClosedError:
    return ConnectionClosedError(endpoint_url=_ENDPOINT)


_ADDED = (200, None)
_DUPLICATE = (400, "InvalidPermission.Duplicate")
_REFUSED = (403, "UnauthorizedOperation")


@pytest.mark.parametrize(
    "answer", [_timeout, _closed, lambda: (500, "InternalError")], ids=("timeout", "closed", "500")
)
def test_an_authorize_whose_only_send_went_unanswered_warns_naming_its_group_and_prefix(
    warnings: list[str], answer: Callable[[], _Answer]
) -> None:
    """A lost answer, or a server error, does not say EC2 never applied the request, and nothing resends it."""
    ec2 = _EC2([answer()])

    with pytest.raises(AgentworksError):
        ec2.poke([_FIRST])

    assert ec2.authorize_sends == 1
    (warning,) = warnings
    assert _GROUP in warning
    assert _FIRST in warning


@pytest.mark.parametrize(
    "answer",
    [_REFUSED, (503, "RequestLimitExceeded"), (400, "Throttling")],
    ids=("refused", "request-limit-exceeded", "throttling"),
)
def test_a_refusal_or_throttling_of_the_only_send_fails_without_a_warning(warnings: list[str], answer: _Answer) -> None:
    """EC2 throttles a request before acting on it, so a throttled authorize added nothing."""
    ec2 = _EC2([answer])

    with pytest.raises(AgentworksError):
        ec2.poke([_FIRST])

    assert ec2.authorize_sends == 1
    assert warnings == []


@pytest.mark.parametrize("answer", [_ADDED, _DUPLICATE], ids=("added", "duplicate"))
def test_an_authorize_answered_on_its_only_send_stands_without_a_warning(warnings: list[str], answer: _Answer) -> None:
    ec2 = _EC2([answer])

    ec2.poke([_FIRST])

    assert warnings == []


def test_the_warning_names_only_the_prefix_whose_authorize_went_unanswered(warnings: list[str]) -> None:
    with pytest.raises(AgentworksError):
        _EC2([_ADDED, _timeout()]).poke([_FIRST, _SECOND])

    (warning,) = warnings
    assert _SECOND in warning
    assert _FIRST not in warning


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


def test_an_error_without_its_http_status_warns(warnings: list[str]) -> None:
    """Without the status nothing shows EC2 refused the send."""
    refused = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "AuthorizeSecurityGroupIngress")
    ec2 = _Authorizes([refused])

    with pytest.raises(AgentworksError):
        poke_ssh_allow(ec2, _GROUP, [_FIRST], authorize=ec2)

    (warning,) = warnings
    assert _FIRST in warning


def test_an_interrupt_names_only_the_prefix_whose_authorize_it_cut_short(warnings: list[str]) -> None:
    ec2 = _Authorizes([None, KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        poke_ssh_allow(ec2, _GROUP, [_FIRST, _SECOND], authorize=ec2)

    (warning,) = warnings
    assert _SECOND in warning
    assert _FIRST not in warning
