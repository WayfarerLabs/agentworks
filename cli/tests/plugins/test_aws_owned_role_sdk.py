"""Actual locked SDK AssumeRole refresh/signing/send controls, without network."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import LimitExceededError, StateError, TokenRejectedError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws import _owned_auth
from agentworks.plugins.aws._native_access import EC2OwnedAccess
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform
from tests.plugins.test_aws_owned_read_sdk import ACCOUNT, INSTANCE, SUCCESS, Raw

REGION = "us-east-1"
SITE_REGION = "eu-west-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/offline"


@pytest.fixture
def sdk_role(monkeypatch, tmp_path):
    import socket
    from importlib.metadata import version

    import botocore.credentials
    from botocore.awsrequest import AWSResponse
    from botocore.client import BaseClient
    from botocore.httpsession import URLLib3Session

    # Discard inherited SDK configuration before selecting this offline setup.
    for variable in tuple(os.environ):
        if variable.startswith("AWS_"):
            monkeypatch.delenv(variable)
    assert version("boto3") == "1.43.92" and version("botocore") == "1.43.93"
    clock = [100.0]
    wall = [datetime(2030, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(botocore.credentials, "_local_now", lambda: wall[0])
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

    platform = EC2Platform(
        "offline-role",
        {
            "region": SITE_REGION,
            "auth": {
                "mode": "access-key",
                "access_key_id": "FAKESOURCE",
                "access_key_secret": "selected",
                "assume_role_arn": ROLE,
            },
        },
    )
    vm = SimpleNamespace(
        name="vm-one", platform_metadata={"instance_id": INSTANCE, "region": REGION, "account_id": ACCOUNT}
    )
    access = EC2OwnedAccess(vm, platform, RunContext(secrets=SimpleNamespace(get=lambda name: "fake-source-secret")))
    probe = SimpleNamespace(
        access=access,
        clock=clock,
        wall=wall,
        calls=[],
        closes=[],
        signed=[],
        sts_error=None,
        close_error=None,
        send_hook=lambda service: None,
        token=0,
        send_visits=[],
        missing_custody=False,
        status=400,
        malformed=False,
    )
    close = BaseClient.close

    def observed_close(client):
        service = client.meta.service_model.service_name
        owner = access._role_auth
        assert owner is not None
        original = owner._client if service == "sts" else access._read_client
        assert original is client
        probe.closes.append((service, client))
        if service == "sts" and probe.close_error is not None:
            raise probe.close_error
        close(client)

    def send(http_session, request):
        data = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
        service = "sts" if data["Action"] == ["AssumeRole"] else "ec2"
        owner = access._role_auth
        assert owner is not None
        original = owner._client if service == "sts" else access._read_client
        probe.send_visits.append(service)
        if original is None:
            probe.missing_custody = True
        assert original is not None
        assert original.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
        selected_region = SITE_REGION if service == "sts" else REGION
        assert original.meta.region_name == selected_region
        assert request.url.rstrip("/") == f"https://{service}.{selected_region}.amazonaws.com"
        assert 0 < original.meta.config.connect_timeout <= 5
        assert original.meta.config.read_timeout == original.meta.config.connect_timeout
        probe.calls.append(service)
        # Credential headers stay only in memory; never retain or display them.
        expected = "FAKESOURCE" if service == "sts" else "FAKEROLE"
        authorization = request.headers.get("Authorization", b"")
        if isinstance(authorization, bytes):
            authorization = authorization.decode()
        token = request.headers.get("X-Amz-Security-Token")
        expected_token = f"fake-token-{probe.token}".encode() if service == "ec2" else None
        probe.signed.append(expected in authorization and token == expected_token)
        probe.send_hook(service)
        if service == "sts":
            assert data["RoleArn"] == [ROLE] and data["RoleSessionName"] == ["agentworks"]
            if isinstance(probe.sts_error, BaseException):
                raise probe.sts_error
            if probe.sts_error:
                body = (
                    f"<ErrorResponse><Error><Code>{probe.sts_error}</Code>"
                    "<Message>offline</Message></Error><RequestId>offline</RequestId></ErrorResponse>"
                ).encode()
                return AWSResponse(request.url, probe.status, {"content-type": "text/xml"}, Raw(body))
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
            if probe.malformed:
                body = b"<AssumeRoleResponse><AssumeRoleResult/></AssumeRoleResponse>"
        else:
            assert data["Action"] == ["DescribeInstances"] and data["InstanceId.1"] == [INSTANCE]
            body = SUCCESS
        return AWSResponse(request.url, 200, {"content-type": "text/xml"}, Raw(body))

    monkeypatch.setattr(BaseClient, "close", observed_close)
    monkeypatch.setattr(URLLib3Session, "send", send)
    monkeypatch.setattr("botocore.endpoint.time.sleep", lambda value: pytest.fail("unexpected SDK retry sleep"))
    # Force credential wall time without replacing the SDK refresh implementation.
    build = _owned_auth._OwnedRoleSession.build_session

    def build_session(owner, *args):
        session = build(owner, *args)
        session.get_credentials()._time_fetcher = lambda: wall[0]
        probe.session = session
        return session

    monkeypatch.setattr(_owned_auth._OwnedRoleSession, "build_session", build_session)
    try:
        yield probe
    finally:
        probe.close_error = None
        clock[0] = 100.0
        assert access.close(Deadline.after(5))


def observe(probe: SimpleNamespace) -> VMStatus:
    probe.last_deadline = Deadline.after(5)
    access: EC2OwnedAccess = probe.access
    return access.observe_power(probe.last_deadline)


def advisory(probe: SimpleNamespace) -> None:
    assert observe(probe) is VMStatus.RUNNING
    probe.wall[0] += timedelta(minutes=50)


def test_inherited_profiles_and_endpoints_cannot_change_offline_sdk_setup(monkeypatch, request):
    for variable in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_EC2",
        "AWS_ENDPOINT_URL_STS",
    ):
        monkeypatch.setenv(variable, "poison-inherited-setting")
    probe = request.getfixturevalue("sdk_role")
    assert observe(probe) is VMStatus.RUNNING
    assert probe.calls == ["sts", "ec2"] and all(probe.signed)


def test_real_sdk_initial_cached_and_rotating_credentials(sdk_role):
    probe = sdk_role
    assert probe.calls == [] and probe.access._role_auth is None
    assert observe(probe) is VMStatus.RUNNING
    assert observe(probe) is VMStatus.RUNNING
    probe.wall[0] += timedelta(minutes=50)
    assert observe(probe) is VMStatus.RUNNING
    assert probe.calls == ["sts", "ec2", "ec2", "sts", "ec2"]
    assert all(probe.signed) and probe.token == 2
    assert [service for service, original in probe.closes] == ["sts", "ec2", "ec2", "sts", "ec2"]
    assert not probe.access.cleanup_incomplete


def test_actual_advisory_provider_failure_keeps_valid_credentials(sdk_role):
    advisory(sdk_role)
    sdk_role.sts_error = "AccessDenied"
    assert observe(sdk_role) is VMStatus.RUNNING
    assert sdk_role.calls == ["sts", "ec2", "sts", "ec2"]
    assert not sdk_role.access.cleanup_incomplete


def test_mandatory_explicit_rejection_never_falls_back(sdk_role):
    sdk_role.sts_error = "InvalidClientTokenId"
    with pytest.raises(TokenRejectedError):
        observe(sdk_role)
    assert sdk_role.calls == ["sts"] and not sdk_role.access.cleanup_incomplete


@pytest.mark.parametrize("status", [500, 503, 429, 301, 307, 308])
def test_actual_sts_service_pipeline_never_retries(sdk_role, status):
    sdk_role.sts_error, sdk_role.status = "InternalFailure", status
    with pytest.raises(EC2Error):
        observe(sdk_role)
    assert sdk_role.calls == ["sts"] and not sdk_role.access.cleanup_incomplete


def test_expired_mandatory_failure_never_uses_old_role(sdk_role):
    assert observe(sdk_role) is VMStatus.RUNNING
    sdk_role.wall[0] += timedelta(minutes=61)
    sdk_role.sts_error = "InvalidClientTokenId"
    with pytest.raises(TokenRejectedError):
        observe(sdk_role)
    assert sdk_role.calls == ["sts", "ec2", "sts"]


def test_malformed_sts_response_still_closes_returned_original(sdk_role):
    sdk_role.malformed = True
    with pytest.raises(EC2Error):
        observe(sdk_role)
    assert sdk_role.calls == ["sts"] and not sdk_role.access.cleanup_incomplete


@pytest.mark.parametrize("failed_advisory", [False, True])
def test_after_signing_deadline_guard_blocks_actual_send(sdk_role, failed_advisory):
    if failed_advisory:
        advisory(sdk_role)
        sdk_role.sts_error = "AccessDenied"
    before = sdk_role.calls.count("ec2")
    sdk_role.send_hook = lambda service: sdk_role.clock.__setitem__(0, 106.0) if service == "sts" else None
    with pytest.raises(LimitExceededError):
        observe(sdk_role)
    assert sdk_role.calls.count("ec2") == before


@pytest.mark.parametrize("advisory_error", [None, "AccessDenied"])
def test_failed_sts_close_blocks_send_and_teardown_retries_same_original(sdk_role, advisory_error):
    advisory(sdk_role)
    sdk_role.sts_error = advisory_error
    sdk_role.close_error = RuntimeError("close")
    with pytest.raises(StateError):
        observe(sdk_role)
    original = sdk_role.access._role_auth._client
    assert original is not None and sdk_role.calls.count("ec2") == 1
    calls = list(sdk_role.calls)
    with pytest.raises(StateError):
        observe(sdk_role)
    assert sdk_role.calls == calls
    sdk_role.close_error = None
    assert sdk_role.access.close(Deadline.after(5))
    assert sdk_role.closes[-1] == ("sts", original) and sdk_role.calls == calls


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(23)])
def test_real_sdk_control_identity_survives_cleanup_control(sdk_role, failure):
    sdk_role.sts_error = failure
    sdk_role.close_error = KeyboardInterrupt()
    with pytest.raises(type(failure)) as caught:
        observe(sdk_role)
    assert caught.value is failure and sdk_role.access.cleanup_incomplete
    assert sdk_role.calls == ["sts"]


def test_constructor_callback_refresh_has_retained_owner_and_deadline(sdk_role, monkeypatch):
    build = _owned_auth._OwnedRoleSession.build_session

    def construct(owner, *args):
        session = build(owner, *args)

        def refresh(**kwargs):
            assert sdk_role.access._role_auth is owner and owner._deadline is sdk_role.last_deadline
            assert sdk_role.access._read_client is None
            session.get_credentials().get_frozen_credentials()

        session.events.register("creating-client-class.ec2", refresh)
        return session

    monkeypatch.setattr(_owned_auth._OwnedRoleSession, "build_session", construct)
    assert observe(sdk_role) is VMStatus.RUNNING
    assert sdk_role.calls == ["sts", "ec2"]


def test_negative_control_missing_guard_fails_zero_send_oracle(sdk_role, monkeypatch):
    advisory(sdk_role)
    sdk_role.sts_error = "AccessDenied"
    sdk_role.send_hook = lambda service: sdk_role.clock.__setitem__(0, 106.0) if service == "sts" else None
    monkeypatch.setattr(sdk_role.access._role_auth, "guard_ec2_send", lambda **kwargs: None)
    with pytest.raises(LimitExceededError):
        observe(sdk_role)
    with pytest.raises(AssertionError):
        assert sdk_role.calls.count("ec2") == 1


def test_negative_control_delayed_retention_fails_actual_send_custody(sdk_role, monkeypatch):
    create = _owned_auth._OwnedRoleSession._create_sts_client

    def delayed(owner, *args, **kwargs):
        original = create(owner, *args, **kwargs)
        owner._client = None
        # Remove only this counterfactual's STS admission check so the actual
        # send oracle observes the missing custody instead of an earlier refusal.
        original.meta.events.unregister("before-send.sts.AssumeRole", owner._guard_sts_send)
        sdk_role.delayed_original = original
        return original

    monkeypatch.setattr(_owned_auth._OwnedRoleSession, "_create_sts_client", delayed)
    try:
        with pytest.raises(EC2Error):
            observe(sdk_role)
        assert sdk_role.missing_custody and sdk_role.send_visits == ["sts"]
        assert sdk_role.calls == []
    finally:
        sdk_role.access._role_auth._client = sdk_role.delayed_original
        assert sdk_role.access.close(Deadline.after(5))
