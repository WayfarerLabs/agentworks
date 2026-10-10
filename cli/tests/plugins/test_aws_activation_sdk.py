"""Actual locked SDK serialization, signing, parsing and retry pipeline, offline."""

from __future__ import annotations

import ast
import inspect
import os
import socket
import subprocess
import sys
import textwrap
import weakref
from collections.abc import Iterator
from contextlib import closing
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.aws._activation import EC2Activation, decode_activation_payload

INSTANCE = "i-0123456789abcdef0"
ACCOUNT = "123456789012"
REQUEST = "offline-provider-request-id"
ENDPOINT = "https://ec2.us-east-1.amazonaws.com"
SUCCESS = f"""<StartInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
<requestId>{REQUEST}</requestId><instancesSet><item><instanceId>{INSTANCE}</instanceId>
<currentState><code>0</code><name>pending</name></currentState>
<previousState><code>80</code><name>stopped</name></previousState>
</item></instancesSet></StartInstancesResponse>""".encode()


class Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def stream(self, amt: int | None = None, decode_content: bool = False) -> Iterator[bytes]:
        yield self.body


@pytest.fixture
def sdk_owned(tmp_path, monkeypatch):
    import boto3
    import botocore.session
    from botocore.awsrequest import AWSResponse
    from botocore.config import Config
    from botocore.httpsession import URLLib3Session

    calls: list[list[str]] = []
    sleeps: list[float] = []
    replies: list[Any] = []
    clients: list[Any] = []
    closes: list[str] = []

    def deny(*args, **kwargs):
        pytest.fail("unexpected real socket connection")

    def send(http_session, request):
        # Do not retain signing or credential headers.
        data = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
        assert request.method == "POST" and request.url.startswith(ENDPOINT)
        assert data["Action"] == ["StartInstances"] and data["InstanceId.1"] == [INSTANCE]
        assert "InstanceId.2" not in data
        calls.append(data["Action"])
        assert replies
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, body, headers = reply
        return AWSResponse(request.url, status, {"content-type": "text/xml", **headers}, Raw(body))

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(URLLib3Session, "send", send)
    monkeypatch.setattr("botocore.endpoint.time.sleep", sleeps.append)
    config_path, credentials_path = tmp_path / "empty-config", tmp_path / "empty-credentials"
    config_path.write_bytes(b"")
    credentials_path.write_bytes(b"")
    core = botocore.session.Session(session_vars={"profile": (None, None, None, None)})
    core.set_config_variable("config_file", str(config_path))
    core.set_config_variable("credentials_file", str(credentials_path))
    core.set_config_variable("region", "us-east-1")
    session = boto3.Session(
        botocore_session=core,
        aws_access_key_id="FAKEOFFLINEACCESSKEY",
        aws_secret_access_key="fake-offline-secret-never-valid",
        region_name="us-east-1",
    )
    assert core.get_config_variable("profile") is None
    assert core.full_config == {"profiles": {}, "services": {}, "sso_sessions": {}}
    original = session.client

    def construct(service, **kwargs):
        config = kwargs["config"]
        # Explicit route/trust/proxy selection isolates this offline fixture.
        client = original(
            service,
            endpoint_url=ENDPOINT,
            verify=True,
            config=config.merge(Config(proxies={})),
            region_name=kwargs["region_name"],
        )
        assert 0 < client.meta.config.connect_timeout <= 5
        assert client.meta.config.read_timeout == client.meta.config.connect_timeout
        assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
        assert client._endpoint.http_session._verify is True
        close = client.close

        def observed_close():
            closes.append("close")
            close()

        monkeypatch.setattr(client, "close", observed_close)
        clients.append(client)
        return client

    monkeypatch.setattr(session, "client", construct)
    with closing(Database(tmp_path / "sdk.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm-one"), "sdk-proof")
        adapter = EC2Activation(
            owner,
            "vm-one",
            session,
            ACCOUNT,
            "us-east-1",
            INSTANCE,
            ProviderLocator(f"aws-ec2:{ACCOUNT}:us-east-1:{INSTANCE}"),
        )
        yield SimpleNamespace(
            adapter=adapter,
            owner=owner,
            calls=calls,
            sleeps=sleeps,
            replies=replies,
            clients=clients,
            closes=closes,
            original_client=original,
        )


def test_stock_sdk_success_is_one_acknowledgment(sdk_owned):
    probe = sdk_owned
    probe.replies.append((200, SUCCESS, {}))
    assert probe.adapter.start(Deadline.after(5)) == REQUEST
    probe.adapter.reconcile(Deadline.after(5))
    assert probe.calls == [["StartInstances"]] and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"] and len(probe.clients) == 1
    assert not probe.adapter.cleanup_incomplete
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row.payload_revision == 1 and decode_activation_payload(row.payload).request_id == REQUEST


@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("original_type", [None, KeyboardInterrupt, SystemExit])
def test_stock_sdk_uncertain_close_keeps_original_client(
    sdk_owned, monkeypatch, after_effect, close_type, original_type
):
    probe = sdk_owned
    original = original_type("original send interruption") if original_type else None
    close_error = close_type("client close interruption")
    probe.replies.append(original if original else (200, SUCCESS, {}))
    from botocore.httpsession import URLLib3Session

    close = URLLib3Session.close
    closed = []

    def fail(http_session):
        if after_effect:
            close(http_session)
            closed.append(True)
        raise close_error

    monkeypatch.setattr(URLLib3Session, "close", fail)
    expected = original or (close_error if close_type is not OSError else None)
    if expected is None:
        assert probe.adapter.start(Deadline.after(5)) == REQUEST
    else:
        with pytest.raises(type(expected)) as caught:
            probe.adapter.start(Deadline.after(5))
        assert caught.value is expected
        del caught
    close_error.__traceback__ = None
    if original:
        original.__traceback__ = None
    identity = weakref.ref(probe.clients.pop())
    assert identity() is not None and probe.adapter._client is identity()
    assert probe.adapter.cleanup_incomplete and closed == ([True] if after_effect else [])
    probe.adapter.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        probe.adapter.start(Deadline.after(5))
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row.payload_revision == int(original is None)
    assert probe.adapter.payload.request_id == (REQUEST if original is None else None)
    assert probe.calls == [["StartInstances"]] and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"]


@pytest.mark.parametrize("boundary", ["entry", "bookkeeping"])
@pytest.mark.parametrize("original_type", [None, KeyboardInterrupt, SystemExit])
def test_stock_sdk_cleanup_python_boundary_keeps_custody_and_original_control(sdk_owned, boundary, original_type):
    probe = sdk_owned
    original = original_type("original send interruption") if original_type else None
    injected = SystemExit("cleanup boundary interruption")
    probe.replies.append(original if original else (200, SUCCESS, {}))
    method = probe.adapter.start.__func__
    lines, offset = inspect.getsourcelines(method)
    syntax = ast.parse(textwrap.dedent("".join(lines)))
    cleanup = next(
        node
        for node in ast.walk(syntax)
        if isinstance(node, ast.Try) and node.finalbody and isinstance(node.finalbody[0], ast.If)
    )
    entry = offset + cleanup.finalbody[0].lineno - 1
    fired: list[bool] = []

    def interrupt(frame, event, arg):
        if event == "line" and frame.f_code is method.__code__ and not fired:
            ready = frame.f_lineno == entry if boundary == "entry" else probe.closes == ["close"]
            if ready:
                fired.append(True)
                raise injected
        return interrupt

    prior = sys.gettrace()
    sys.settrace(interrupt)
    try:
        with pytest.raises(type(original or injected)) as caught:
            probe.adapter.start(Deadline.after(5))
        assert caught.value is (original or injected)
        del caught
    finally:
        sys.settrace(prior)
    injected.__traceback__ = None
    if original:
        original.__traceback__ = None
    identity = weakref.ref(probe.clients.pop())
    assert fired == [True] and probe.adapter._client is identity() and identity() is not None
    assert probe.adapter.cleanup_incomplete
    probe.adapter.reconcile(Deadline.after(5))
    assert probe.calls == [["StartInstances"]] and probe.sleeps == []
    assert probe.closes == ([] if boundary == "entry" else ["close"])
    assert probe.adapter.payload.request_id == (REQUEST if original is None else None)


@pytest.mark.parametrize(
    "status,code",
    [
        (500, "InternalError"),
        (503, "ServiceUnavailable"),
        (429, "RequestLimitExceeded"),
        (301, "MovedPermanently"),
        (307, "TemporaryRedirect"),
        (308, "PermanentRedirect"),
        (401, "AuthFailure"),
        (403, "UnauthorizedOperation"),
    ],
)
def test_stock_sdk_http_failure_sends_once_and_retains(sdk_owned, status, code):
    from botocore.exceptions import ClientError

    probe = sdk_owned
    body = f"""<Response><Errors><Error><Code>{code}</Code><Message>offline failure</Message>
    </Error></Errors><RequestID>{REQUEST}</RequestID></Response>""".encode()
    probe.replies.append((status, body, {"location": "https://unexpected.invalid/redirect"}))
    with pytest.raises(ClientError) as caught:
        probe.adapter.start(Deadline.after(5))
    assert caught.value.response["ResponseMetadata"]["RetryAttempts"] == 0
    assert caught.value.response["Error"]["Code"] == code
    probe.adapter.reconcile(Deadline.after(5))
    row = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 0
    assert probe.calls == [["StartInstances"]] and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"]


@pytest.mark.parametrize("exception_name", ["EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"])
def test_stock_sdk_transport_failure_sends_once(sdk_owned, exception_name):
    import botocore.exceptions

    exception_type = getattr(botocore.exceptions, exception_name)
    probe = sdk_owned
    primary = exception_type(endpoint_url=ENDPOINT)
    probe.replies.append(primary)
    with pytest.raises(exception_type) as caught:
        probe.adapter.start(Deadline.after(5))
    assert caught.value is primary
    probe.adapter.reconcile(Deadline.after(5))
    assert probe.calls == [["StartInstances"]] and probe.sleeps == [] and probe.replies == []
    assert probe.closes == ["close"] and probe.adapter.payload.request_id is None


@pytest.mark.parametrize(
    "status,code", [(500, "InternalError"), (503, "ServiceUnavailable"), (429, "RequestLimitExceeded")]
)
def test_retry_enabled_stock_sdk_control_reaches_real_retry_pipeline(sdk_owned, status, code):
    from botocore.config import Config

    probe = sdk_owned
    body = f"""<Response><Errors><Error><Code>{code}</Code><Message>offline failure</Message>
    </Error></Errors><RequestID>{REQUEST}</RequestID></Response>""".encode()
    probe.replies.extend([(status, body, {}), (200, SUCCESS, {})])
    # This control deliberately uses a separate ordinary SDK client, not the adapter.
    with closing(
        probe.original_client(
            "ec2",
            region_name="us-east-1",
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
        result = client.start_instances(InstanceIds=[INSTANCE])
    assert result["ResponseMetadata"]["RetryAttempts"] == 1
    assert probe.calls == [["StartInstances"], ["StartInstances"]] and len(probe.sleeps) == 1
    assert probe.replies == [] and probe.clients == []


def test_fresh_normal_import_is_sdk_lazy_without_additional_retired_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import sys
import agentworks.plugins.aws
def legacy():
    return sorted(name for name in sys.modules if name == 'agentworks.ssh'
                  or name.startswith(('agentworks.transports', 'agentworks.exec_protocol')))
baseline = legacy()
adapter = importlib.import_module('agentworks.plugins.aws._activation')
assert legacy() == baseline
assert not any(name == 'boto3' or name.startswith('boto3.') or name == 'botocore'
               or name.startswith('botocore.') for name in sys.modules)
print('parent registration legacy baseline:', baseline)
print('adapter source:', adapter.__file__)
""",
        ],
        env={**os.environ, "PYTHONPATH": os.environ["PYTHONPATH"]},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.returncode == 0
    print(result.stdout)
