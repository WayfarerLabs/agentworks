"""Independent AWS creation with the real ledger and scripted SDK boundary."""

from __future__ import annotations

import gzip
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
import yaml

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProvisionRequest
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.aws import _creation_access
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.platform import EC2Platform
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY
from tests.plugins.test_aws_creation_support import (
    ACCOUNT, INSTANCE, JOIN_KEY, REGION, Session,
)

@pytest.fixture
def owned(tmp_path, monkeypatch):
    with closing(Database(tmp_path / "creation.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        session = Session(owner)
        settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
        config = SimpleNamespace(operator=SimpleNamespace(ssh=settings, ssh_allow_cidrs=[]))
        platform = EC2Platform("offline", {"region": REGION, "auth": {"mode": "ambient"}})
        request = ProvisionRequest(
            vm_name="vm-one", debian_release=DebianRelease.TRIXIE, hostname="system-vm-one", system_slug="system",
            instance_marker="a" * 32, admin_username="operator", ssh_public_key=TEST_SSH_PUBLIC_KEY,
            ssh_private_key=None, tailscale_auth_key=JOIN_KEY, progress=SimpleNamespace(),
            cpus=2, memory_gib=2, disk_gib=20, swap_gib=0,
        )
        events = []

        class Trust:
            def __init__(self, selected_owner, **kwargs):
                assert selected_owner is owner and kwargs["endpoint"] == "192.0.2.7"
                events.append("trust-retained")

            def prepare(self):
                assert session.nonce is not None and session.rules
                events.append("trust-prepared")

            def close(self, deadline):
                assert not session.rules
                events.append("trust-closed")
                return True

        monkeypatch.setattr(_creation_access, "_build_ambient_session", lambda region: session)
        monkeypatch.setattr(_creation_access, "CreationSSHTrust", Trust)
        monkeypatch.setattr(_creation_access.SSHReadiness, "wait", lambda *args: events.append("listening"))
        monkeypatch.setattr(_creation_access.OwnedSSHExposure, "resolve", lambda *args: ("192.0.2.1/32",))
        monkeypatch.setattr(_creation_access, "observe_early_guest", lambda *args: events.append("prerequisites"))
        custody = LocalDeliveryCustody()
        access = platform.build_creation_access(request, RunContext(config=config), owner=owner, custody=custody)
        yield SimpleNamespace(
            database=database, owner=owner, session=session, access=access, request=request, platform=platform,
            custody=custody, events=events, config=config,
        )


def test_passive_factory_then_independent_result_and_acceptance(owned):
    assert not owned.session.calls and not owned.session.clients and not owned.events
    assert not owned.access.platform_metadata and owned.access.binding is None
    deadline = Deadline.after(5)
    result = owned.access.provision(deadline)
    assert result.binding is owned.access.binding
    assert result.locator.token == f"aws-ec2:{ACCOUNT}:{REGION}:{INSTANCE}"
    assert dict(result.platform_metadata) == dict(owned.access.platform_metadata)
    assert owned.access.observe_power(deadline) is VMStatus.RUNNING
    assert owned.events == ["listening", "trust-retained", "trust-prepared", "prerequisites"]
    owned.access.complete_bootstrap(deadline)
    with pytest.raises(StateError):
        owned.access.request_rollback()
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(5))
    assert not owned.owner.list_pending_lifecycle_obligations()
    assert "terminate_instances" not in [method for method, _ in owned.session.calls]
    assert "delete_security_group" not in [method for method, _ in owned.session.calls]
    assert len(owned.session.clients) == len(owned.session.closes)
    assert owned.database.operations.inspect(owned.owner.ownership.scope) is not None


def test_rollback_is_passive_then_exact_and_uses_existing_obligations(owned):
    owned.access.provision(Deadline.after(5))
    before = list(owned.session.calls)
    original_ids = {row.obligation_id for row in owned.owner.list_pending_lifecycle_obligations()}
    owned.access.request_rollback()
    assert owned.session.calls == before
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(5))
    assert owned.access.settle(Deadline.after(5))
    assert not owned.owner.list_pending_lifecycle_obligations()
    assert owned.access.creation.obligation_id in original_ids
    mutations = [method for method, _ in owned.session.calls]
    assert mutations.count("create_security_group") == mutations.count("run_instances") == 1
    assert mutations.count("terminate_instances") == mutations.count("delete_security_group") == 1
    assert mutations.index("describe_volumes") < mutations.index("terminate_instances")


@pytest.mark.parametrize("method", ["create_security_group", "run_instances"])
def test_lost_creation_reply_remains_unresolved_without_name_adoption(owned, method):
    owned.session.fault = method
    with pytest.raises(KeyboardInterrupt):
        owned.access.provision(Deadline.after(5))
    owned.access.request_rollback()
    owned.owner.stop_admission()
    owned.session.fault = None
    for _ in range(2):
        assert not owned.access.settle(Deadline.after(5))
    methods = [name for name, _ in owned.session.calls]
    assert methods.count(method) == 1
    assert "terminate_instances" not in methods and "delete_security_group" not in methods
    assert owned.owner.list_pending_lifecycle_obligations()
    with pytest.raises(StateError):
        owned.access.provision(Deadline.after(5))


@pytest.mark.parametrize("committed", [False, True])
def test_lost_launch_receipt_publication_reconciles_without_replay(owned, monkeypatch, committed):
    repository = owned.database.operations
    publish = repository.publish_lifecycle_obligation_payload
    failed = False

    def interrupt(*args, **kwargs):
        nonlocal failed
        if not failed and json.loads(kwargs["payload"]).get("launch_receipt"):
            failed = True
            if committed:
                publish(*args, **kwargs)
            raise KeyboardInterrupt("publication reply interrupted")
        return publish(*args, **kwargs)

    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        owned.access.provision(Deadline.after(5))
    assert owned.access.platform_metadata["instance_id"] == INSTANCE
    owned.access.request_rollback()
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(5))
    assert owned.access.settle(Deadline.after(5))
    assert [method for method, _ in owned.session.calls].count("run_instances") == 1


def test_original_client_close_failure_blocks_following_requests(owned):
    def fail_after_group(method):
        if method == "create_security_group":
            owned.session.close_failure = OSError("close interrupted")

    owned.session.after = fail_after_group
    with pytest.raises(StateError):
        owned.access.provision(Deadline.after(5))
    original = owned.access.creation.client
    assert original is not None
    owned.access.request_rollback()
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(5))
    assert owned.access.creation.client is original and owned.session.closes[-1] is original
    owned.session.close_failure = None
    assert owned.access.settle(Deadline.after(5))
    assert "run_instances" not in [method for method, _ in owned.session.calls]


def test_unknown_termination_reply_observes_original_outcome(owned):
    owned.access.provision(Deadline.after(5))
    owned.access.request_rollback()
    owned.owner.stop_admission()
    owned.session.fault = "terminate_instances"
    with pytest.raises(KeyboardInterrupt):
        owned.access.settle(Deadline.after(5))
    owned.session.fault = None
    assert owned.access.settle(Deadline.after(5))
    assert [method for method, _ in owned.session.calls].count("terminate_instances") == 1


def test_observation_account_mismatch_prevents_destructive_cleanup(owned):
    owned.access.provision(Deadline.after(5))
    owned.access.request_rollback()
    owned.owner.stop_admission()
    owned.session.account = "999999999999"
    with pytest.raises(StateError):
        owned.access.settle(Deadline.after(5))
    assert "terminate_instances" not in [method for method, _ in owned.session.calls]


def test_late_launch_receipt_is_retained_for_fresh_cleanup_only(owned, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))

    def late(method):
        if method == "run_instances":
            clock[0] = 106.0

    owned.session.after = late
    with pytest.raises(LimitExceededError):
        owned.access.provision(Deadline.after(5))
    assert owned.access.platform_metadata["instance_id"] == INSTANCE
    owned.access.request_rollback()
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(5))
    assert owned.access.settle(Deadline.after(5))
    assert [method for method, _ in owned.session.calls].count("run_instances") == 1


def test_unknown_group_delete_reply_uses_independent_absence(owned):
    def stop_before_launch(method):
        if method == "describe_security_groups":
            raise StateError("stop before launch")

    owned.session.after = stop_before_launch
    with pytest.raises(StateError):
        owned.access.provision(Deadline.after(5))
    owned.session.after = lambda method: None
    owned.access.request_rollback()
    owned.owner.stop_admission()
    owned.session.fault = "delete_security_group"
    with pytest.raises(KeyboardInterrupt):
        owned.access.settle(Deadline.after(5))
    owned.session.fault = None
    assert owned.access.settle(Deadline.after(5))
    assert [method for method, _ in owned.session.calls].count("delete_security_group") == 1


def test_changed_attachment_prevents_termination(owned, monkeypatch):
    owned.access.provision(Deadline.after(5))
    original = owned.session.instance

    def detached():
        instance = original()
        instance["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"] = False
        return instance

    monkeypatch.setattr(owned.session, "instance", detached)
    owned.access.request_rollback()
    owned.owner.stop_admission()
    with pytest.raises(StateError):
        owned.access.settle(Deadline.after(5))
    assert "terminate_instances" not in [method for method, _ in owned.session.calls]


def test_public_startup_only_prepares_helpers_and_has_no_join_credential(owned):
    payload = yaml.safe_load(gzip.decompress(owned.access._user_data))
    assert payload["users"][0]["ssh_authorized_keys"] == [owned.request.ssh_public_key]
    assert "python3" in payload["packages"]
    assert JOIN_KEY.encode() not in gzip.decompress(owned.access._user_data)
    assert owned.access._marker == owned.request.instance_marker


def test_configured_role_is_retained_and_never_falls_back_to_ambient(owned, monkeypatch):
    calls = []

    def session(helper, auth, secret, site, region):
        assert auth.assume_role_arn == "arn:aws:iam::123456789012:role/creation"
        assert secret == "access-key-secret" and site == "offline" and region == REGION
        assert helper._deadline is not None
        calls.append(helper)
        return owned.session

    def ambient(region):
        pytest.fail("configured credentials must not fall back")

    monkeypatch.setattr(_OwnedRoleSession, "build_session", session)
    monkeypatch.setattr(_creation_access, "_build_ambient_session", ambient)
    platform = EC2Platform("offline", {"region": REGION, "auth": {
        "mode": "access-key", "access_key_id": "fake-key", "access_key_secret": "aws-secret",
        "assume_role_arn": "arn:aws:iam::123456789012:role/creation",
    }})
    ctx = RunContext(config=owned.config, secrets=SimpleNamespace(get=lambda name: "access-key-secret"))
    access = platform.build_creation_access(owned.request, ctx, owner=owned.owner, custody=owned.custody)
    access.provision(Deadline.after(5))
    assert calls == [access._role_auth] and access.creation.role_auth is access._role_auth
    access.complete_bootstrap(Deadline.after(5))
    owned.owner.stop_admission()
    assert access.settle(Deadline.after(5))
    assert access._role_auth._deadline is None and not access._role_auth.cleanup_incomplete
    resource = owned.owner.inspect_lifecycle_obligation(access.creation.obligation_id)
    assert resource is not None and b"access-key-secret" not in resource.payload


def test_foreign_owner_refused_before_effects(owned):
    owner = OperationOwner.acquire(
        owned.database.operations, OperationScope(OperationResourceKind.VM, "other-vm"), "proof"
    )
    with pytest.raises(ValidationError):
        owned.platform.build_creation_access(
            owned.request, RunContext(config=owned.config), owner=owner, custody=owned.custody
        )
    assert not owned.session.calls
