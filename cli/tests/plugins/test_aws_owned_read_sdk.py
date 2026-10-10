"""Locked SDK EC2 serialization, signing, parsing and cleanup, with offline sends."""

from __future__ import annotations

import gc
import socket
import weakref
from collections.abc import Iterator
from contextlib import closing
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import VMStatus
from agentworks.errors import (
    AuthorizationError,
    LimitExceededError,
    StateError,
    TokenRejectedError,
)
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws import _native_access
from agentworks.plugins.aws._native_access import EC2OwnedAccess
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform

INSTANCE = "i-0123456789abcdef0"
ACCOUNT = "123456789012"
REGION = "us-east-1"
REQUEST = "offline-provider-request-id"
ENDPOINT = "https://ec2.us-east-1.amazonaws.com"
SUCCESS = f"""<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
<requestId>{REQUEST}</requestId><reservationSet><item><reservationId>r-offline</reservationId>
<ownerId>{ACCOUNT}</ownerId><instancesSet><item><instanceId>{INSTANCE}</instanceId>
<instanceState><code>16</code><name>running</name></instanceState><ipAddress>192.0.2.7</ipAddress>
</item></instancesSet></item></reservationSet></DescribeInstancesResponse>""".encode()


class Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def stream(self, amt: int | None = None, decode_content: bool = False) -> Iterator[bytes]:
        yield self.body


def error_response(code: str) -> bytes:
    return f"""<Response><Errors><Error><Code>{code}</Code><Message>offline failure</Message>
    </Error></Errors><RequestID>{REQUEST}</RequestID></Response>""".encode()


@pytest.fixture
def sdk_owned_read(tmp_path, monkeypatch):
    import boto3
    import botocore.session
    from botocore.awsrequest import AWSResponse
    from botocore.config import Config
    from botocore.httpsession import URLLib3Session

    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls: list[list[str]] = []
    sleeps: list[float] = []
    replies: list[Any] = []
    clients: list[Any] = []
    closes: list[str] = []
    vm = SimpleNamespace(
        name="vm-one",
        platform_metadata={
            "instance_id": INSTANCE,
            "region": REGION,
            "account_id": ACCOUNT,
        },
    )
    platform = EC2Platform("offline-aws", {"region": REGION, "auth": {"mode": "ambient"}})
    access = EC2OwnedAccess(vm, platform, RunContext())
    probe = SimpleNamespace(
        access=access,
        calls=calls,
        sleeps=sleeps,
        replies=replies,
        clients=clients,
        closes=closes,
        clock=clock,
        send_hook=lambda: None,
        close_hook=lambda: None,
    )

    def deny(*args, **kwargs):
        pytest.fail("unexpected real network operation")

    def send(http_session, request):
        # Retain operation fields only, never signing or credential headers.
        data = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
        assert request.method == "POST" and request.url in {ENDPOINT, ENDPOINT + "/"}
        assert data["Action"] == ["DescribeInstances"] and data["InstanceId.1"] == [INSTANCE]
        assert "InstanceId.2" not in data
        calls.append(data["Action"])
        probe.send_hook()
        assert replies
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, body, headers = reply
        return AWSResponse(request.url, status, {"content-type": "text/xml", **headers}, Raw(body))

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(URLLib3Session, "send", send)
    monkeypatch.setattr("botocore.endpoint.time.sleep", sleeps.append)
    config_path, credentials_path = (
        tmp_path / "empty-config",
        tmp_path / "empty-credentials",
    )
    config_path.touch(mode=0o600)
    credentials_path.touch(mode=0o600)
    core = botocore.session.Session(session_vars={"profile": (None, None, None, None)})
    core.set_config_variable("config_file", str(config_path))
    core.set_config_variable("credentials_file", str(credentials_path))
    core.set_config_variable("region", REGION)
    session = boto3.Session(
        botocore_session=core,
        aws_access_key_id="FAKEOFFLINEACCESSKEY",
        aws_secret_access_key="fake-offline-secret-never-valid",
        region_name=REGION,
    )
    assert core.get_config_variable("profile") is None
    assert core.full_config == {"profiles": {}, "services": {}, "sso_sessions": {}}
    original_client = session.client

    def construct(service, **kwargs):
        config = kwargs["config"]
        client = original_client(
            service,
            endpoint_url=ENDPOINT,
            verify=True,
            config=config.merge(Config(proxies={})),
            region_name=kwargs["region_name"],
        )
        assert 0 < client.meta.config.connect_timeout <= 5
        assert client.meta.config.read_timeout == client.meta.config.connect_timeout
        assert client.meta.config.retries == {
            "total_max_attempts": 1,
            "mode": "standard",
        }
        assert client._endpoint.http_session._verify is True
        close = client.close

        def observed_close():
            assert access._read_client is client
            closes.append("close")
            close()
            probe.close_hook()

        client.close = observed_close
        clients.append(client)
        return client

    monkeypatch.setattr(session, "client", construct)
    monkeypatch.setattr(_native_access, "_build_ambient_session", lambda region: session)
    probe.original_client = original_client
    return probe


def test_stock_sdk_describe_success_uses_one_attempt_and_original_close(sdk_owned_read):
    probe = sdk_owned_read
    probe.send_hook = lambda: pytest.fail("missing original custody") if not probe.access.cleanup_incomplete else None
    for observe, expected in (
        (probe.access.observe_power, VMStatus.RUNNING),
        (
            probe.access.observe_locator,
            ProviderLocator(f"aws-ec2:{ACCOUNT}:{REGION}:{INSTANCE}"),
        ),
        (probe.access.observe_public_endpoint, "192.0.2.7"),
    ):
        probe.replies.append((200, SUCCESS, {}))
        assert observe(Deadline.after(5)) == expected
    assert probe.calls == [["DescribeInstances"]] * 3 and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"] * 3 and len(probe.clients) == 3
    assert not probe.access.cleanup_incomplete


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (500, "InternalError", EC2Error),
        (503, "ServiceUnavailable", EC2Error),
        (429, "RequestLimitExceeded", EC2Error),
        (301, "MovedPermanently", EC2Error),
        (307, "TemporaryRedirect", EC2Error),
        (308, "PermanentRedirect", EC2Error),
        (401, "AuthFailure", TokenRejectedError),
        (403, "UnauthorizedOperation", AuthorizationError),
        (400, "InvalidInstanceID.NotFound", None),
    ],
)
def test_stock_sdk_http_failure_never_retries_or_redirects(sdk_owned_read, status, code, expected):
    from agentworks.errors import NotFoundError

    probe = sdk_owned_read
    probe.replies.append(
        (
            status,
            error_response(code),
            {"location": "https://unexpected.invalid/redirect"},
        )
    )
    with pytest.raises(expected or NotFoundError) as caught:
        probe.access.observe_locator(Deadline.after(5))
    assert caught.value.__cause__.response["ResponseMetadata"]["RetryAttempts"] == 0
    assert caught.value.__cause__.response["Error"]["Code"] == code
    assert probe.calls == [["DescribeInstances"]] and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"] and not probe.access.cleanup_incomplete


@pytest.mark.parametrize(
    "exception_name",
    ["EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"],
)
def test_stock_sdk_transport_failure_uses_one_attempt(sdk_owned_read, exception_name):
    import botocore.exceptions

    probe = sdk_owned_read
    primary = getattr(botocore.exceptions, exception_name)(endpoint_url=ENDPOINT)
    probe.replies.append(primary)
    with pytest.raises(EC2Error) as caught:
        probe.access.observe_power(Deadline.after(5))
    assert caught.value.__cause__ is primary
    assert probe.calls == [["DescribeInstances"]] and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"] and not probe.access.cleanup_incomplete


@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("primary_type", [None, KeyboardInterrupt, SystemExit])
def test_stock_sdk_uncertain_close_keeps_original(sdk_owned_read, monkeypatch, after_effect, close_type, primary_type):
    from botocore.httpsession import URLLib3Session

    probe = sdk_owned_read
    primary = primary_type("synthetic send control") if primary_type else None
    cleanup = close_type("synthetic close failure")
    probe.replies.append(primary or (200, SUCCESS, {}))
    close = URLLib3Session.close
    closed = []

    def fail(http_session):
        if after_effect:
            close(http_session)
            closed.append(True)
        raise cleanup

    monkeypatch.setattr(URLLib3Session, "close", fail)
    expected = primary or (cleanup if close_type is not OSError else None)
    if expected is None:
        assert probe.access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    else:
        with pytest.raises(type(expected)) as caught:
            probe.access.observe_power(Deadline.after(5))
        assert caught.value is expected
        del caught
    for error in (primary, cleanup):
        if error is not None:
            error.__traceback__ = None
    original = weakref.ref(probe.clients.pop())
    gc.collect()
    assert original() is not None and probe.access._read_client is original()
    assert probe.access.cleanup_incomplete and closed == ([True] if after_effect else [])
    with pytest.raises(StateError):
        probe.access.observe_locator(Deadline.after(5))
    assert probe.calls == [["DescribeInstances"]] and probe.sleeps == [] and probe.replies == []
    monkeypatch.setattr(URLLib3Session, "close", close)
    assert probe.access.close(Deadline.after(5)) and not probe.access.cleanup_incomplete
    assert probe.closes == ["close", "close"] and probe.calls == [["DescribeInstances"]]


@pytest.mark.parametrize("boundary", ["send", "close"])
def test_stock_sdk_late_result_refuses_and_keeps_cleanup_evidence(sdk_owned_read, boundary):
    probe = sdk_owned_read
    setattr(probe, f"{boundary}_hook", lambda: probe.clock.__setitem__(0, 106.0))
    probe.replies.append((200, SUCCESS, {}))
    with pytest.raises(LimitExceededError):
        probe.access.observe_power(Deadline.after(5))
    assert probe.calls == [["DescribeInstances"]] and probe.closes == ["close"]
    assert not probe.access.cleanup_incomplete


@pytest.mark.parametrize(
    "status,code",
    [
        (500, "InternalError"),
        (503, "ServiceUnavailable"),
        (429, "RequestLimitExceeded"),
    ],
)
def test_retry_enabled_stock_sdk_control_reaches_retry_pipeline(sdk_owned_read, status, code):
    from botocore.config import Config

    probe = sdk_owned_read
    probe.replies.extend([(status, error_response(code), {}), (200, SUCCESS, {})])
    with closing(
        probe.original_client(
            "ec2",
            region_name=REGION,
            endpoint_url=ENDPOINT,
            verify=True,
            config=Config(
                connect_timeout=0.25,
                read_timeout=0.5,
                proxies={},
                retries={"total_max_attempts": 2, "mode": "standard"},
            ),
        )
    ) as client:
        result = client.describe_instances(InstanceIds=[INSTANCE])
    assert result["ResponseMetadata"]["RetryAttempts"] == 1
    assert probe.calls == [["DescribeInstances"], ["DescribeInstances"]] and len(probe.sleeps) == 1
    assert probe.replies == [] and probe.clients == []
