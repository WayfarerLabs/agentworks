"""Independent AWS creation with the real ledger and scripted SDK boundary."""

from __future__ import annotations

import gzip
import json
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProvisionRequest
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.aws import _creation_access
from agentworks.plugins.aws._creation import KIND
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.platform import EC2Platform
from tests.plugins.test_aws_creation_support import (
    ACCOUNT,
    INSTANCE,
    JOIN_KEY,
    NIC,
    REGION,
    VOLUME,
    Session,
)
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY


@pytest.fixture
def owned(tmp_path, monkeypatch):
    with closing(Database(tmp_path / "creation.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        session = Session(owner)
        settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
        config = SimpleNamespace(operator=SimpleNamespace(ssh=settings, ssh_allow_cidrs=[]))
        platform = EC2Platform("offline", {"region": REGION, "auth": {"mode": "ambient"}})
        request = ProvisionRequest(
            vm_name="vm-one",
            debian_release=DebianRelease.TRIXIE,
            hostname="system-vm-one",
            system_slug="system",
            instance_marker="a" * 32,
            admin_username="operator",
            ssh_public_key=TEST_SSH_PUBLIC_KEY,
            ssh_private_key=None,
            tailscale_auth_key=JOIN_KEY,
            progress=SimpleNamespace(),
            cpus=2,
            memory_gib=2,
            disk_gib=20,
            swap_gib=0,
        )
        events = []

        class Trust:
            def __init__(self, selected_owner, **kwargs):
                assert selected_owner is owner and kwargs["endpoint"] == "192.0.2.7"
                events.append("trust-retained")

            def prepare(self):
                assert session.nonce is not None and session.rules
                resource = next(
                    row for row in owner.list_pending_lifecycle_obligations() if row.obligation_kind == KIND
                )
                recorded = json.loads(resource.payload)
                assert recorded["volumes"] == [VOLUME] and recorded["interfaces"] == [NIC]
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
            database=database,
            owner=owner,
            session=session,
            access=access,
            request=request,
            platform=platform,
            custody=custody,
            events=events,
            config=config,
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
    platform = EC2Platform(
        "offline",
        {
            "region": REGION,
            "auth": {
                "mode": "access-key",
                "access_key_id": "fake-key",
                "access_key_secret": "aws-secret",
                "assume_role_arn": "arn:aws:iam::123456789012:role/creation",
            },
        },
    )
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


@pytest.mark.parametrize("floor", [21, 0, None, True])
def test_invalid_or_undersized_image_disk_refuses_before_resources(owned, floor):
    owned.session.image_floor = floor
    with pytest.raises(ValidationError):
        owned.access.provision(Deadline.after(5))
    methods = [method for method, _ in owned.session.calls]
    assert "create_security_group" not in methods and "run_instances" not in methods
    assert not owned.owner.list_pending_lifecycle_obligations()


def test_image_floor_equal_request_preserves_exact_requested_disk(owned):
    owned.session.image_floor = owned.request.disk_gib
    owned.access.provision(Deadline.after(5))
    launch = next(arguments for method, arguments in owned.session.calls if method == "run_instances")
    assert launch["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == owned.request.disk_gib


def test_root_admin_creation_returns_direct_root_delivery(owned):
    request = replace(owned.request, admin_username="root")
    access = owned.platform.build_creation_access(
        request, RunContext(config=owned.config), owner=owned.owner, custody=owned.custody
    )
    result = access.provision(Deadline.after(5))
    assert result.binding.delivery_account == "root"
    assert result.binding._early_guest_facts_route.account == "root"
    assert result.binding._early_guest_facts_route.root_entry.mode is IdentityMode.DIRECT


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("volume", "vol-1123456789abcdef0"),
        ("interface", "eni-1123456789abcdef0"),
    ],
)
def test_replacement_attachment_is_not_adopted_for_rollback(owned, field, replacement):
    owned.access.provision(Deadline.after(5))
    assert owned.access.creation.intent.volumes == (VOLUME,)
    assert owned.access.creation.intent.interfaces == (NIC,)
    setattr(owned.session, field, replacement)
    owned.access.request_rollback()
    owned.owner.stop_admission()
    with pytest.raises(StateError):
        owned.access.settle(Deadline.after(5))
    assert "terminate_instances" not in [method for method, _ in owned.session.calls]
    assert owned.access.creation.intent.volumes == (VOLUME,)
    assert owned.access.creation.intent.interfaces == (NIC,)


@pytest.mark.parametrize("stage", ["provision", "rollback"])
@pytest.mark.parametrize("tags", [[], [{"Key": "agentworks:creation", "Value": "foreign-creation"}]])
def test_missing_or_foreign_volume_tags_refuse_guest_work_or_termination(owned, stage, tags):
    if stage == "rollback":
        owned.access.provision(Deadline.after(5))
    owned.session.volume_tags = tags
    if stage == "provision":
        with pytest.raises(StateError):
            owned.access.provision(Deadline.after(5))
        assert not owned.events
    owned.access.request_rollback()
    owned.owner.stop_admission()
    with pytest.raises(StateError):
        owned.access.settle(Deadline.after(5))
    assert "terminate_instances" not in [method for method, _ in owned.session.calls]


def test_running_launch_records_attachments_when_receipt_has_not_populated_them(owned, monkeypatch):
    reply = owned.session.reply

    def without_attachments(method, arguments):
        result = reply(method, arguments)
        if method == "run_instances":
            result["Instances"][0].pop("BlockDeviceMappings")
            result["Instances"][0].pop("NetworkInterfaces")
        return result

    monkeypatch.setattr(owned.session, "reply", without_attachments)
    owned.access.provision(Deadline.after(5))
    assert owned.access.creation.intent.volumes == (VOLUME,)
    assert owned.access.creation.intent.interfaces == (NIC,)
    assert "prerequisites" in owned.events


def test_one_settle_waits_for_shutdown_volume_nic_and_group_absence(owned, monkeypatch):
    from agentworks.plugins.aws import _creation_rollback

    clock = [100.0]
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(_creation_rollback, "time", SimpleNamespace(sleep=pause))
    owned.access.provision(Deadline.after(5))
    owned.session.termination_states = ["shutting-down", "shutting-down", "terminated"]
    owned.session.volume_linger, owned.session.nic_linger, owned.session.group_linger = 2, 3, 2
    owned.access.request_rollback()
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(5))
    assert pauses and all(0 < seconds <= 0.1 for seconds in pauses)
    assert not owned.owner.list_pending_lifecycle_obligations()
    methods = [method for method, _ in owned.session.calls]
    assert methods.count("terminate_instances") == methods.count("delete_security_group") == 1


@pytest.mark.parametrize("pending", ["termination", "volume", "interface", "group"])
def test_cleanup_deadline_retains_original_request_then_fresh_budget_observes(owned, monkeypatch, pending):
    from agentworks.plugins.aws import _creation_rollback

    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        _creation_rollback, "time", SimpleNamespace(sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    )
    owned.access.provision(Deadline.after(5))
    owned.session.termination_states = [] if pending == "termination" else ["terminated"]
    if pending == "volume":
        owned.session.volume_linger = 100
    elif pending == "interface":
        owned.session.nic_linger = 100
    elif pending == "group":
        owned.session.group_linger = 100
    owned.access.request_rollback()
    owned.owner.stop_admission()
    with pytest.raises(LimitExceededError):
        owned.access.settle(Deadline.after(0.25))
    assert owned.owner.list_pending_lifecycle_obligations()
    owned.session.termination_states = ["terminated"]
    owned.session.volume_linger = owned.session.nic_linger = owned.session.group_linger = 0
    assert owned.access.settle(Deadline.after(5))
    methods = [method for method, _ in owned.session.calls]
    assert methods.count("run_instances") == methods.count("terminate_instances") == 1
    assert methods.count("delete_security_group") == 1
