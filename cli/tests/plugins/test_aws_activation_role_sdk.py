"""Retained StartInstances role signing and original-only public close, offline."""

from __future__ import annotations

import os
import socket
from contextlib import closing
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.aws._activation import EC2Activation
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.config import AwsAccessKeyAuth
from tests.plugins.test_aws_activation_sdk import ACCOUNT, INSTANCE, REQUEST, SUCCESS, Raw

REGION = "us-east-1"
SITE_REGION = "eu-west-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/offline"


@pytest.fixture
def role_activation(monkeypatch, tmp_path):
    import botocore.credentials
    from botocore.awsrequest import AWSResponse
    from botocore.client import BaseClient
    from botocore.httpsession import URLLib3Session

    assert version("boto3") == "1.43.92" and version("botocore") == "1.43.93"
    for variable in tuple(os.environ):
        if variable.startswith("AWS_"):
            monkeypatch.delenv(variable)
    for variable in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        path = tmp_path / variable
        path.touch(mode=0o600)
        monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "POISONAMBIENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "poison-never-valid")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    def deny(*args, **kwargs):
        pytest.fail("unexpected ambient resolver or real network")

    monkeypatch.setattr(botocore.credentials.CredentialResolver, "load_credentials", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr("botocore.endpoint.time.sleep", deny)
    clock = [100.0]
    wall = [datetime(2030, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(botocore.credentials, "_local_now", lambda: wall[0])
    role = _OwnedRoleSession("vm-one")
    probe = SimpleNamespace(
        role=role,
        clock=clock,
        wall=wall,
        calls=[],
        closes=[],
        signed=[],
        adapters=[],
        active=None,
        sts_error=None,
        sts_close_error=None,
        ec2_error=None,
        ec2_close_error=None,
        token=0,
        send_hook=lambda service: None,
    )
    original_close = BaseClient.close

    def original(service: str) -> Any:
        return role._client if service == "sts" else probe.active._client

    def observed_close(client):
        service = client.meta.service_model.service_name
        assert original(service) is client
        probe.closes.append((service, client))
        error = probe.sts_close_error if service == "sts" else probe.ec2_close_error
        if error is not None:
            raise error
        original_close(client)

    def send(http_session, request):
        data = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
        service = "sts" if data["Action"] == ["AssumeRole"] else "ec2"
        client = original(service)
        assert client is not None
        assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
        region = SITE_REGION if service == "sts" else REGION
        assert client.meta.region_name == region
        assert request.url.rstrip("/") == f"https://{service}.{region}.amazonaws.com"
        assert 0 < client.meta.config.connect_timeout <= 5
        assert client.meta.config.read_timeout == client.meta.config.connect_timeout
        probe.calls.append((service, client))
        authorization = request.headers.get("Authorization", b"")
        if isinstance(authorization, bytes):
            authorization = authorization.decode()
        token = request.headers.get("X-Amz-Security-Token")
        expected_key = "FAKESOURCE" if service == "sts" else "FAKEROLE"
        expected_token = None if service == "sts" else f"fake-token-{probe.token}".encode()
        # Retain only a boolean, never credential headers.
        probe.signed.append(expected_key in authorization and token == expected_token)
        probe.send_hook(service)
        if service == "ec2":
            assert data["Action"] == ["StartInstances"]
            assert data["InstanceId.1"] == [INSTANCE] and "InstanceId.2" not in data
            if probe.ec2_error is not None:
                raise probe.ec2_error
            body = SUCCESS
        else:
            assert data["RoleArn"] == [ROLE] and data["RoleSessionName"] == ["agentworks"]
            if isinstance(probe.sts_error, BaseException):
                raise probe.sts_error
            if probe.sts_error:
                body = (
                    f"<ErrorResponse><Error><Code>{probe.sts_error}</Code>"
                    "<Message>offline</Message></Error><RequestId>offline</RequestId></ErrorResponse>"
                ).encode()
                return AWSResponse(request.url, 400, {"content-type": "text/xml"}, Raw(body))
            probe.token += 1
            expiry = (wall[0] + timedelta(hours=1)).isoformat()
            body = (
                '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                "<AssumeRoleResult><Credentials><AccessKeyId>FAKEROLE</AccessKeyId>"
                "<SecretAccessKey>fake-role-secret</SecretAccessKey>"
                f"<SessionToken>fake-token-{probe.token}</SessionToken><Expiration>{expiry}</Expiration>"
                f"</Credentials><AssumedRoleUser><Arn>{ROLE}</Arn><AssumedRoleId>offline</AssumedRoleId>"
                "</AssumedRoleUser></AssumeRoleResult><ResponseMetadata><RequestId>offline</RequestId>"
                "</ResponseMetadata></AssumeRoleResponse>"
            ).encode()
        return AWSResponse(request.url, 200, {"content-type": "text/xml"}, Raw(body))

    monkeypatch.setattr(BaseClient, "close", observed_close)
    monkeypatch.setattr(URLLib3Session, "send", send)
    with closing(Database(tmp_path / "activation-role.db")) as database:
        probe.owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "sdk-role-proof"
        )
        role.begin(Deadline.after(5))
        try:
            auth = AwsAccessKeyAuth(
                mode="access-key", access_key_id="FAKESOURCE", access_key_secret="selected", assume_role_arn=ROLE
            )
            probe.session = role.build_session(auth, "fake-source-secret", "offline-role", SITE_REGION)
            probe.session.get_credentials()._time_fetcher = lambda: wall[0]
        finally:
            role.end()
        try:
            yield probe
        finally:
            clock[0] = 100.0
            probe.sts_close_error = probe.ec2_close_error = None
            role.end()
            for adapter in probe.adapters:
                probe.active = adapter
                assert adapter.close(Deadline.after(5))
            role.close_client()
            assert not role.cleanup_incomplete


def construct(probe: SimpleNamespace) -> EC2Activation:
    adapter = EC2Activation(
        probe.owner,
        "vm-one",
        probe.session,
        ACCOUNT,
        REGION,
        INSTANCE,
        ProviderLocator(f"aws-ec2:{ACCOUNT}:{REGION}:{INSTANCE}"),
        role_auth=probe.role,
    )
    probe.adapters.append(adapter)
    probe.active = adapter
    return adapter


def start(probe: SimpleNamespace) -> str:
    adapter = construct(probe)
    deadline = Deadline.after(5)
    probe.role.begin(deadline)
    try:
        return adapter.start(deadline)
    finally:
        probe.role.end()


def advisory(probe: SimpleNamespace) -> None:
    assert start(probe) == REQUEST
    probe.wall[0] += timedelta(minutes=50)


def services(probe: SimpleNamespace) -> list[str]:
    return [service for service, client in probe.calls]


def test_real_sdk_first_cached_and_advisory_refresh(role_activation):
    probe = role_activation
    assert probe.calls == []
    assert start(probe) == REQUEST
    assert start(probe) == REQUEST
    probe.wall[0] += timedelta(minutes=50)
    assert start(probe) == REQUEST
    assert services(probe) == ["sts", "ec2", "ec2", "sts", "ec2"]
    assert all(probe.signed) and probe.token == 2
    assert probe.closes == probe.calls


def test_normally_closed_advisory_failure_preserves_valid_sdk_credentials(role_activation):
    advisory(role_activation)
    role_activation.sts_error = "AccessDenied"
    assert start(role_activation) == REQUEST
    assert services(role_activation) == ["sts", "ec2", "sts", "ec2"]
    assert all(role_activation.signed)


def test_mandatory_rejection_never_selects_ambient_credentials(role_activation):
    from botocore.exceptions import ClientError

    probe = role_activation
    probe.sts_error = "InvalidClientTokenId"
    with pytest.raises(ClientError) as caught:
        start(probe)
    assert caught.value.response["Error"]["Code"] == "InvalidClientTokenId"
    assert services(probe) == ["sts"]
    assert not probe.role.cleanup_incomplete and not probe.active.cleanup_incomplete
    row = probe.owner.inspect_lifecycle_obligation(probe.active.obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT


def test_cached_credentials_require_caller_admitted_role_interval(role_activation):
    probe = role_activation
    assert start(probe) == REQUEST
    adapter = construct(probe)
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert services(probe) == ["sts", "ec2"]


@pytest.mark.parametrize("failed_advisory", [False, True])
def test_actual_start_post_signing_deadline_guard(role_activation, failed_advisory):
    probe = role_activation
    if failed_advisory:
        advisory(probe)
        probe.sts_error = "AccessDenied"
    before = services(probe).count("ec2")
    probe.send_hook = lambda service: probe.clock.__setitem__(0, 106.0) if service == "sts" else None
    with pytest.raises(LimitExceededError):
        start(probe)
    assert services(probe).count("ec2") == before


@pytest.mark.parametrize("advisory_error", [None, "AccessDenied"])
def test_actual_start_guard_refuses_uncertain_sts_close(role_activation, advisory_error):
    probe = role_activation
    advisory(probe)
    probe.sts_error, probe.sts_close_error = advisory_error, OSError("close")
    with pytest.raises(StateError):
        start(probe)
    original = probe.role._client
    assert original is not None and services(probe).count("ec2") == 1
    calls = list(probe.calls)
    assert probe.active.close(Deadline.after(5))
    assert probe.role._client is original and probe.calls == calls
    probe.sts_close_error = None
    probe.role.close_client()
    assert probe.closes[-1] == ("sts", original) and probe.calls == calls


def test_missing_guard_counterfactual_detects_suppressed_advisory_send(role_activation, monkeypatch):
    probe = role_activation
    advisory(probe)
    probe.sts_error = "AccessDenied"
    monkeypatch.setattr(probe.role, "guard_ec2_send", lambda **kwargs: None)
    probe.send_hook = lambda service: probe.clock.__setitem__(0, 106.0) if service == "sts" else None
    with pytest.raises(TimeoutError):
        start(probe)
    assert services(probe) == ["sts", "ec2", "sts", "ec2"]
    assert probe.active.payload.request_id == REQUEST


def test_passive_construction_and_terminal_close_before_start(role_activation):
    probe = role_activation
    adapter = construct(probe)
    assert probe.calls == probe.closes == [] and adapter.obligation is None
    assert adapter.close(Deadline.after(5))
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert probe.owner.inspect_lifecycle_obligation(adapter.obligation_id) is None
    assert probe.calls == probe.closes == []


@pytest.mark.parametrize("deadline", [Deadline(None), Deadline(99.0)])
def test_close_requires_finite_timely_admission(role_activation, deadline):
    probe = role_activation
    adapter = construct(probe)
    with pytest.raises(ValidationError if deadline.expires_at is None else TimeoutError):
        adapter.close(deadline)
    assert probe.calls == probe.closes == [] and adapter.obligation is None
    admitted = Deadline.after(5)
    probe.role.begin(admitted)
    try:
        assert adapter.start(admitted) == REQUEST
    finally:
        probe.role.end()


@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("primary_type", [None, KeyboardInterrupt, SystemExit])
def test_close_retries_same_ec2_original_without_provider_or_ledger_replay(
    role_activation, monkeypatch, close_type, primary_type
):
    probe = role_activation
    primary = primary_type("send") if primary_type else None
    close_error = close_type("close")
    probe.ec2_error, probe.ec2_close_error = primary, close_error
    expected = primary or (close_error if close_type is not OSError else None)
    if expected is None:
        assert start(probe) == REQUEST
    else:
        with pytest.raises(type(expected)) as caught:
            start(probe)
        assert caught.value is expected
    adapter = probe.active
    original = adapter._client
    assert original is not None and adapter.cleanup_incomplete
    row = probe.owner.inspect_lifecycle_obligation(adapter.obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row.payload_revision == int(primary is None)
    calls = list(probe.calls)

    def deny(*args, **kwargs):
        pytest.fail("close attempted ledger or session work")

    monkeypatch.setattr(probe.session, "client", deny)
    monkeypatch.setattr(probe.owner, "inspect_lifecycle_obligation", deny)
    if close_type is OSError:
        assert not adapter.close(Deadline.after(5))
    else:
        with pytest.raises(close_type) as caught:
            adapter.close(Deadline.after(5))
        assert caught.value is close_error
    assert adapter._client is original
    probe.ec2_close_error = None
    assert adapter.close(Deadline.after(5))
    assert probe.closes[-1] == ("ec2", original) and probe.calls == calls
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))


@pytest.mark.parametrize("primary", [KeyboardInterrupt(), SystemExit(23)])
def test_role_control_identity_survives_cleanup_control(role_activation, primary):
    probe = role_activation
    probe.sts_error = primary
    probe.sts_close_error = SystemExit("sts close")
    probe.ec2_close_error = KeyboardInterrupt("ec2 close")
    with pytest.raises(type(primary)) as caught:
        start(probe)
    assert caught.value is primary
    assert services(probe) == ["sts"]
    assert probe.role.cleanup_incomplete and probe.active.cleanup_incomplete
