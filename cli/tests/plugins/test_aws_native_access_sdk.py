"""Owned AWS composition through the actual offline EC2 service pipeline."""

from __future__ import annotations

import os
import socket
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import Database, LifecycleObligationState, VMStatus
from agentworks.db.operations import OperationResourceKind, OperationScope
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.aws import _native_access
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.config import AwsAccessKeyAuth
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform
from tests.plugins.test_aws_activation_sdk import ACCOUNT, INSTANCE, Raw

REGION = "us-east-1"
GROUP = "sg-0123456789abcdef0"
RULE = "sgr-0123456789abcdef0"
PREFIX = "192.0.2.1/32"


@pytest.fixture
def inherited_aws(monkeypatch, tmp_path):
    for name, value in {
        "AWS_PROFILE": "nonexistent-poison-profile",
        "AWS_DEFAULT_PROFILE": "nonexistent-poison-profile",
        "AWS_CONFIG_FILE": str(tmp_path / "missing-poison-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "missing-poison-credentials"),
        "AWS_ENDPOINT_URL": "https://poison.invalid",
        "AWS_ENDPOINT_URL_EC2": "https://poison-ec2.invalid",
        "AWS_ENDPOINT_URL_STS": "https://poison-sts.invalid",
        "AWS_CA_BUNDLE": str(tmp_path / "missing-poison-ca"),
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp_path / "missing-poison-token"),
        "AWS_ROLE_ARN": "arn:aws:iam::000000000000:role/poison",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def native_aws(monkeypatch, tmp_path, inherited_aws):
    import boto3
    from botocore.awsrequest import AWSResponse
    from botocore.client import BaseClient
    from botocore.httpsession import URLLib3Session

    for name in tuple(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        path = tmp_path / name
        path.touch(mode=0o600)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    clock = [100.0]
    wall = [datetime(2030, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
    config = SimpleNamespace(operator=SimpleNamespace(ssh=settings, ssh_allow_cidrs=[]))
    vm = SimpleNamespace(
        name="vm-one",
        site="offline",
        admin_username="root",
        platform_metadata={
            "instance_id": INSTANCE,
            "account_id": ACCOUNT,
            "region": REGION,
            "security_group_id": GROUP,
            "backend_name": "backend-one",
        },
    )
    platform = EC2Platform("offline", {"region": REGION, "auth": {"mode": "ambient"}})
    session = boto3.Session(
        aws_access_key_id="FAKEOFFLINE", aws_secret_access_key="fake-offline-secret", region_name=REGION
    )
    probe = SimpleNamespace(
        wall=wall,
        sts_error=None,
        sts_close_error=None,
        role_trigger=None,
        role_fault=None,
        api_error=None,
        clock=clock,
        calls=[],
        clients=[],
        closes=[],
        state="running",
        endpoint="192.0.2.7",
        account=ACCOUNT,
        group_account=ACCOUNT,
        tag="backend-one",
        rules=[],
        error=None,
        close_error=None,
        authorize_error=None,
        malformed_authorize=False,
        keep_rule=False,
        before_send=lambda action: None,
        after_send=lambda action: None,
        preparations=[],
        pauses=[],
        close_hook=lambda client: None,
    )

    def pause(seconds):
        assert 0 < seconds <= 0.25
        assert not probe.preparations and probe.access._route is None
        probe.pauses.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(_native_access, "time", SimpleNamespace(sleep=pause))
    monkeypatch.setattr(_native_access, "_build_ambient_session", lambda region: session)
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.ssh_exposure.operator_ssh_prefixes", lambda extras: [PREFIX]
    )

    def prepare(vm, platform, locator, binding, *, deadline, owner, observe_locator):
        assert observe_locator.__self__ is probe.access
        assert deadline is probe.deadline and owner is probe.owner
        assert binding is probe.access.binding
        assert observe_locator(deadline) == locator
        probe.preparations.append(binding)
        return SimpleNamespace(status="offline-guest-prepared")

    monkeypatch.setattr(_native_access, "prepare_managed_vm_target_from_platform", prepare)

    def deny(*args, **kwargs):
        pytest.fail("unexpected real network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr("botocore.endpoint.time.sleep", deny)
    original_close = BaseClient.close

    def close(client):
        probe.closes.append(client)
        probe.close_hook(client)
        if client.meta.service_model.service_name == "sts" and probe.sts_close_error is not None:
            raise probe.sts_close_error
        if probe.close_error is not None:
            raise probe.close_error
        original_close(client)

    monkeypatch.setattr(BaseClient, "close", close)
    original_client = session.client

    def construct(*args, **kwargs):
        client = original_client(*args, **kwargs)
        assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
        probe.clients.append(client)
        return client

    monkeypatch.setattr(session, "client", construct)

    def rule_xml(rule: dict[str, Any]) -> str:
        return (
            f"<item><securityGroupRuleId>{rule['id']}</securityGroupRuleId><groupId>{GROUP}</groupId>"
            f"<groupOwnerId>{ACCOUNT}</groupOwnerId><isEgress>false</isEgress><ipProtocol>tcp</ipProtocol>"
            f"<fromPort>22</fromPort><toPort>22</toPort><cidrIpv4>{PREFIX}</cidrIpv4>"
            f"<description>{rule['description']}</description></item>"
        )

    def send(http_session, request):
        data = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
        action = data["Action"][0]
        probe.before_send(action)
        probe.calls.append((action, data))
        service = "sts" if action == "AssumeRole" else "ec2"
        assert request.url.rstrip("/") == f"https://{service}.{REGION}.amazonaws.com"
        if probe.error is not None:
            raise probe.error
        if probe.api_error is not None and probe.api_error[0] == action:
            code = probe.api_error[1]
            error_body = (
                f"<Response><Errors><Error><Code>{code}</Code><Message>offline</Message>"
                "</Error></Errors><RequestID>offline-request</RequestID></Response>"
            ).encode()
            return AWSResponse(request.url, 400, {"content-type": "text/xml"}, Raw(error_body))
        status = 200
        body: Any
        if action == "AssumeRole":
            if probe.role_fault == "expired":
                probe.clock[0] = 106.0
            if probe.role_fault == "close":
                probe.sts_close_error = OSError("offline STS close")
            if probe.role_fault in ("expired", "close"):
                body = (
                    b"<ErrorResponse><Error><Code>AccessDenied</Code><Message>offline</Message></Error></ErrorResponse>"
                )
                return AWSResponse(request.url, 400, {"content-type": "text/xml"}, Raw(body))
            body = (
                f'<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                "<AssumeRoleResult><Credentials><AccessKeyId>FAKEROLE</AccessKeyId>"
                "<SecretAccessKey>fake-role-secret</SecretAccessKey><SessionToken>fake-token</SessionToken>"
                f"<Expiration>{(wall[0] + timedelta(hours=1)).isoformat()}</Expiration>"
                "</Credentials></AssumeRoleResult><ResponseMetadata><RequestId>offline</RequestId>"
                "</ResponseMetadata></AssumeRoleResponse>"
            ).encode()
            return AWSResponse(request.url, 200, {"content-type": "text/xml"}, Raw(body))
        elif action == "DescribeInstances":
            assert data["InstanceId.1"] == [INSTANCE] and "InstanceId.2" not in data
            body = (
                f"<reservationSet><item><ownerId>{probe.account}</ownerId><instancesSet><item>"
                f"<instanceId>{INSTANCE}</instanceId><instanceState><name>{probe.state}</name></instanceState>"
                f"<ipAddress>{probe.endpoint}</ipAddress><vpcId>vpc-offline</vpcId>"
                f"<groupSet><item><groupId>{GROUP}</groupId></item></groupSet>"
                f"<tagSet><item><key>agentworks:vm</key><value>{probe.tag}</value></item></tagSet>"
                "</item></instancesSet></item></reservationSet>"
            )
        elif action == "StartInstances":
            assert probe.access._activation is not None
            row = probe.owner.inspect_lifecycle_obligation(probe.access._activation.obligation_id)
            assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
            probe.state = "running"
            body = f"<instancesSet><item><instanceId>{INSTANCE}</instanceId></item></instancesSet>"
        elif action == "DescribeSecurityGroups":
            body = (
                f"<securityGroupInfo><item><groupId>{GROUP}</groupId><ownerId>{probe.group_account}</ownerId>"
                "<vpcId>vpc-offline</vpcId><tagSet><item><key>agentworks:vm</key>"
                "<value>backend-one</value></item></tagSet></item></securityGroupInfo>"
            )
        elif action == "DescribeSecurityGroupRules":
            assert data["Filter.1.Name"] == ["group-id"] and data["Filter.1.Value.1"] == [GROUP]
            body = "<securityGroupRuleSet>" + "".join(rule_xml(r) for r in probe.rules) + "</securityGroupRuleSet>"
        elif action == "AuthorizeSecurityGroupIngress":
            route = probe.access._route
            assert route is not None and route._rules[0].obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert data["GroupId"] == [GROUP]
            rule = {"id": RULE, "description": data["IpPermissions.1.IpRanges.1.Description"][0]}
            probe.rules.append(rule)
            if probe.authorize_error is not None:
                raise probe.authorize_error
            body = "<return>true</return><securityGroupRuleSet>" + rule_xml(rule) + "</securityGroupRuleSet>"
            if probe.malformed_authorize:
                body = "<return>true</return>"
        elif action == "RevokeSecurityGroupIngress":
            if data.get("DryRun") == ["true"]:
                status = 400
                body = "<Errors><Error><Code>DryRunOperation</Code><Message>offline</Message></Error></Errors>"
            else:
                assert data["SecurityGroupRuleId.1"] == [RULE] and "IpPermissions.1.IpProtocol" not in data
                if not probe.keep_rule:
                    probe.rules.clear()
                body = "<return>true</return>"
        else:
            pytest.fail(f"unexpected EC2 action {action}")
        probe.after_send(action)
        xml = (
            f'<{action}Response xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
            f"<requestId>offline-request</requestId>{body}</{action}Response>"
        ).encode()
        return AWSResponse(request.url, status, {"content-type": "text/xml"}, Raw(xml))

    monkeypatch.setattr(URLLib3Session, "send", send)
    with closing(Database(tmp_path / "native.db")) as database:
        probe.owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, vm.name), "native"
        )
        probe.repository = probe.owner._repository
        probe.deadline = Deadline.after(5)
        probe.vm = vm
        probe.platform = platform
        probe.access = platform.build_native_execution_access(
            vm,
            RunContext(config=config),
            owner=probe.owner,
            custody=LocalDeliveryCustody(),
        )
        assert not probe.calls and probe.access.binding is None and probe.access.preparation is None
        yield probe


@pytest.mark.parametrize("power", [VMStatus.RUNNING, VMStatus.STOPPED])
def test_sdk_prepares_and_removes_only_owned_rule(native_aws, power):
    p = native_aws
    p.state = power.value
    assert p.access.observe_power(p.deadline) is power
    p.access.prepare(power, p.deadline)
    assert len(p.preparations) == 1 and p.access.preparation is not None
    p.access.route_check(p.deadline)
    assert p.access.settle(p.deadline)
    actions = [a for a, _ in p.calls]
    assert actions.count("StartInstances") == (power is VMStatus.STOPPED)
    assert actions.count("AuthorizeSecurityGroupIngress") == 1
    assert actions.count("RevokeSecurityGroupIngress") == 2
    assert not p.rules and not p.access.cleanup_incomplete
    assert not p.owner.list_pending_lifecycle_obligations()


def test_sdk_duplicate_tuple_is_never_adopted_or_revoked(native_aws):
    p = native_aws
    p.rules.append({"id": RULE, "description": "somebody-else"})
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access.settle(p.deadline)
    assert len(p.rules) == 1
    assert not any(a in ("AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress") for a, _ in p.calls)


@pytest.mark.parametrize("fault", ["lost", "malformed", "control"])
def test_sdk_missing_authorize_receipt_stays_possible_effect(native_aws, fault):
    p = native_aws
    from botocore.exceptions import EndpointConnectionError

    p.authorize_error = {
        "lost": EndpointConnectionError(endpoint_url="https://offline.invalid"),
        "control": KeyboardInterrupt(),
    }.get(fault)
    p.malformed_authorize = fault == "malformed"
    expected = {"lost": EC2Error, "malformed": ValidationError, "control": KeyboardInterrupt}[fault]
    with pytest.raises(expected):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    p.rules.clear()
    assert not p.access.settle(p.deadline)
    row = p.owner.inspect_lifecycle_obligation(p.access._route._rules[0].obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert sum(a == "AuthorizeSecurityGroupIngress" for a, _ in p.calls) == 1
    assert not any(a == "RevokeSecurityGroupIngress" and "DryRun" not in d for a, d in p.calls)


@pytest.mark.parametrize("field", ["account", "group_account", "tag"])
def test_sdk_foreign_identity_refuses_route_mutation(native_aws, field):
    p = native_aws
    setattr(p, field, "foreign")
    with pytest.raises((StateError, EC2Error)):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert not any(a == "AuthorizeSecurityGroupIngress" for a, _ in p.calls)


def test_sdk_endpoint_change_does_not_rebind(native_aws):
    p = native_aws
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    original = p.access.binding
    p.endpoint = "192.0.2.8"
    with pytest.raises(StateError):
        p.access.route_check(p.deadline)
    assert p.access.binding is original
    assert p.access.settle(p.deadline)


def test_sdk_failed_removal_observation_retains_route_debt(native_aws):
    p = native_aws
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    p.keep_rule = True
    assert not p.access.settle(p.deadline)
    p.keep_rule = False
    assert p.access.settle(p.deadline)
    assert sum(a == "AuthorizeSecurityGroupIngress" for a, _ in p.calls) == 1


def test_sdk_original_client_debt_blocks_new_reads(native_aws):
    p = native_aws
    p.close_error = OSError("offline close")
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    original = p.access._read_client
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert not p.access.settle(p.deadline)
    assert p.access._read_client is original
    p.close_error = None
    assert p.access.settle(p.deadline)
    assert len(p.clients) == 1


def test_sdk_lost_start_resolution_reply_reconciles_exact_row(native_aws, monkeypatch):
    p = native_aws
    p.state = "stopped"
    original = p.repository.resolve_lifecycle_obligation
    lost = [False]

    def resolve(*args, **kwargs):
        row = original(*args, **kwargs)
        if row.obligation_kind == "aws-ec2-start" and not lost[0]:
            lost[0] = True
            raise ConnectionError("lost committed resolution reply")
        return row

    monkeypatch.setattr(p.repository, "resolve_lifecycle_obligation", resolve)
    with pytest.raises(ConnectionError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert p.access._running is not None
    monkeypatch.setattr(p.access._activation, "reconcile", lambda deadline: pytest.fail("producer after resolution"))
    assert p.access.settle(p.deadline)
    assert sum(a == "StartInstances" for a, _ in p.calls) == 1


@pytest.mark.parametrize("state", ["pending", "stopped"])
def test_sdk_nonrunning_cannot_resolve_acknowledged_start(native_aws, state):
    p = native_aws
    p.state = "stopped"
    p.after_send = lambda action: setattr(p, "state", state) if action == "StartInstances" else None
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    row = p.owner.inspect_lifecycle_obligation(p.access._activation.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert p.deadline.expired and p.pauses and p.access._running is None
    cleanup = Deadline.after(5)
    assert not p.access.settle(cleanup)
    p.state = "running"
    assert p.access.settle(cleanup)


def test_sdk_late_running_is_never_completion(native_aws):
    p = native_aws
    p.state = "stopped"
    p.after_send = lambda action: p.clock.__setitem__(0, 106.0) if action == "StartInstances" else None
    with pytest.raises(TimeoutError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert p.access._running is None
    p.after_send = lambda action: None
    assert p.access.settle(Deadline.after(5))


def enable_role(
    probe: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str | None = None,
    fault: str | None = None,
) -> _OwnedRoleSession:
    import botocore.credentials

    role = _OwnedRoleSession(probe.vm.name)
    probe.access._role_auth = role
    probe.platform = EC2Platform(
        "offline",
        {
            "region": REGION,
            "auth": {
                "mode": "access-key",
                "access_key_id": "FAKESOURCE",
                "access_key_secret": "selected",
                "assume_role_arn": f"arn:aws:iam::{ACCOUNT}:role/offline",
            },
        },
    )
    probe.access._platform = probe.platform
    role.begin(probe.deadline)
    try:
        session = role.build_session(
            AwsAccessKeyAuth(
                mode="access-key",
                access_key_id="FAKESOURCE",
                access_key_secret="selected",
                assume_role_arn=f"arn:aws:iam::{ACCOUNT}:role/offline",
            ),
            "fake-source-secret",
            "offline",
            REGION,
        )
        session.get_credentials()._time_fetcher = lambda: probe.wall[0]
        monkeypatch.setattr(botocore.credentials, "_local_now", lambda: probe.wall[0])
        probe.access._session = session
    finally:
        role.end()
    original = session.client
    triggered = [False]

    def construct(*args, **kwargs):
        client = original(*args, **kwargs)

        def advisory(**kwargs):
            if not triggered[0]:
                triggered[0] = True
                probe.wall[0] += timedelta(minutes=50)
                probe.role_fault = fault

        if target is not None:
            client.meta.events.register(f"before-sign.ec2.{target}", advisory)
        return client

    monkeypatch.setattr(session, "client", construct)
    return role


def test_sdk_same_role_helper_spans_reads_start_routes_and_cleanup(native_aws, monkeypatch):
    p = native_aws
    role = enable_role(p, monkeypatch)
    p.state = "stopped"
    assert p.access.observe_power(p.deadline) is VMStatus.STOPPED
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert p.access._activation._role_auth is role and p.access._route._role_auth is role
    p.access.route_check(p.deadline)
    assert p.access.settle(p.deadline)
    assert sum(a == "AssumeRole" for a, _ in p.calls) == 1
    assert not role.cleanup_incomplete


@pytest.mark.parametrize(
    "target",
    [
        "DescribeSecurityGroups",
        "DescribeSecurityGroupRules",
        "RevokeSecurityGroupIngress",
        "AuthorizeSecurityGroupIngress",
    ],
)
@pytest.mark.parametrize("fault", ["expired", "close"])
def test_actual_route_send_guard_after_suppressed_advisory(native_aws, monkeypatch, target, fault):
    p = native_aws
    role = enable_role(p, monkeypatch, target=target, fault=fault)
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    with pytest.raises(LimitExceededError if fault == "expired" else StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert not any(action == target for action, _ in p.calls)
    assert sum(action == "AssumeRole" for action, _ in p.calls) == 2
    p.sts_close_error = None
    role.close_client()


def test_missing_route_guard_counterfactual_reaches_forbidden_send(native_aws, monkeypatch):
    p = native_aws
    role = enable_role(p, monkeypatch, target="AuthorizeSecurityGroupIngress", fault="expired")
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    monkeypatch.setattr(role, "guard_ec2_send", lambda **kwargs: None)
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert sum(action == "AuthorizeSecurityGroupIngress" for action, _ in p.calls) == 1


def test_sdk_missing_start_ack_cannot_resolve_from_running(native_aws):
    from botocore.exceptions import EndpointConnectionError

    p = native_aws
    p.state = "stopped"

    def lose_start(action):
        if action == "StartInstances":
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    p.after_send = lose_start
    with pytest.raises(EC2Error):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    p.after_send = lambda action: None
    assert p.state == "running"
    assert not p.access.settle(p.deadline)
    assert p.access._running is None
    assert sum(action == "StartInstances" for action, _ in p.calls) == 1
