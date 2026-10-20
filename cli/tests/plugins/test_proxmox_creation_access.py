"""Real creation controllers and ledger with scripted HTTP, never a host bootstrap."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NoReturn

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProvisionRequest
from agentworks.db import Database, VMStatus
from agentworks.db.operations import (
    LifecycleObligation,
    LifecycleObligationState,
    OperationOwnership,
    OperationResourceKind,
    OperationScope,
)
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _Endpoint
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, _ProxmoxWire, _WireFailure
from agentworks.operations import OperationOwner
from agentworks.plugins.proxmox._creation_access import RESOURCE_KIND, ProxmoxOwnedCreationAccess
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

if TYPE_CHECKING:
    from agentworks.execution._process import LocalProcessOwner

MARKER = "a" * 32
SOURCE_UUID = "12345678-1234-1234-1234-123456789abc"
SMBIOS = "87654321-4321-4321-4321-cba987654321"
TOKEN = "api-secret-sentinel"
JOIN_KEY = "tskey-join-sentinel"


class _UnusedProgress:
    def step(self, name: str) -> NoReturn:
        raise AssertionError(name)

    def output(self, text: str) -> NoReturn:
        raise AssertionError(text)

    def warning(self, msg: str) -> NoReturn:
        raise AssertionError(msg)

    def log_error(self, msg: str) -> NoReturn:
        raise AssertionError(msg)


@dataclass
class _Secrets:
    calls: list[str] = field(default_factory=list)

    def get(self, name: str) -> str:
        self.calls.append(name)
        assert name == "pve-token"
        return TOKEN


@dataclass
class Harness:
    database: Database
    owner: OperationOwner
    custody: LocalDeliveryCustody
    platform: ProxmoxPlatform
    request: ProvisionRequest
    secrets: _Secrets
    access: ProxmoxOwnedCreationAccess
    clock: list[float] = field(default_factory=lambda: [100.0])
    guest_info: list[dict[str, object] | BaseException] = field(default_factory=list)
    default_guest_info: dict[str, object] | BaseException = field(
        default_factory=lambda: {"result": {"version": "9.0", "supported_commands": []}}
    )
    calls: list[tuple[_Endpoint, str, int]] = field(default_factory=list)
    bodies: list[tuple[_Endpoint, bytes]] = field(default_factory=list)
    power: str = "stopped"
    current: dict[str, object] = field(
        default_factory=lambda: {
            "description": f"agentworks-instance:{MARKER}",
            "smbios1": f"uuid={SMBIOS}",
            "digest": "a" * 40,
            "scsi0": "local-lvm:vm-123-disk-0,size=20G",
        }
    )
    fault: tuple[_Endpoint, str] | None = None
    primary: BaseException = field(default_factory=lambda: KeyboardInterrupt("wire reply interrupted"))
    bootstrap_stage: str = "complete"

    def row(self) -> LifecycleObligation | None:
        return self.owner.inspect_lifecycle_obligation(self.access._resource_id)

    def exchange(
        self,
        wire: _ProxmoxWire,
        endpoint: _Endpoint,
        method: str,
        suffix: str | None,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None,
        timeout: float | None,
    ) -> object:
        connection = wire._connection
        assert custody is self.custody and custody.settled
        assert timeout is not None and 0 < timeout <= 5
        assert connection.api_url == "https://pve.example:8006" and connection.node == "node1"
        assert connection.token_id == "worker@pve!creation" and connection.token_secret == TOKEN
        assert self.database.operations.inspect(self.owner.ownership.scope) is not None
        self.calls.append((endpoint, method, connection.vmid))
        if body is not None:
            assert TOKEN.encode() not in body and JOIN_KEY.encode() not in body
            self.bodies.append((endpoint, body))
        if method != "GET":
            resource = self.row()
            assert resource is not None and resource.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert any(row.obligation_kind != RESOURCE_KIND for row in self.owner.list_pending_lifecycle_obligations())
        result = self.reply(endpoint, method, suffix, body, connection.vmid)
        if self.fault == (endpoint, method):
            raise self.primary
        return result

    def reply(self, endpoint: _Endpoint, method: str, suffix: str | None, body: bytes | None, vmid: int) -> object:
        if endpoint is _Endpoint.NEXT_ID:
            assert method == "GET" and vmid == 9000
            return 123
        if endpoint is _Endpoint.CURRENT_CONFIG:
            assert method == "GET"
            return {"smbios1": f"uuid={SOURCE_UUID}"} if vmid == 9000 else self.current.copy()
        if endpoint is _Endpoint.POWER:
            return {"status": self.power}
        if endpoint is _Endpoint.TASK_STATUS:
            assert suffix is not None
            parts = suffix.split(":")
            return {
                "upid": suffix,
                "node": "node1",
                "pid": 171,
                "pstart": 0x100000001,
                "starttime": 205,
                "type": parts[5],
                "id": parts[6],
                "user": "worker@pve",
                "tokenid": "creation",
                "status": "stopped",
                "exitstatus": "OK",
            }
        if endpoint is _Endpoint.VM_CLONE:
            assert method == "POST" and vmid == 9000 and body is not None
            assert json.loads(body) == {
                "newid": 123,
                "name": f"project-{self.request.vm_name}",
                "description": f"agentworks-instance:{MARKER}",
                "storage": "local-lvm",
                "pool": "agents",
                "full": 1,
            }
            return "UPID:node1:000000AB:100000001:000000CD:qmclone:9000:worker@pve!creation:"
        if endpoint is _Endpoint.VM_CONFIGURE:
            assert method == "PUT" and body is not None and vmid == 123
            self.current.update(json.loads(body))
            self.current["digest"] = "b" * 40
            return None
        if endpoint is _Endpoint.VM_RESIZE:
            assert method == "PUT" and vmid == 123 and body is not None
            assert json.loads(body) == {"disk": "scsi0", "size": "40G", "digest": "b" * 40}
            self.current.update(scsi0="local-lvm:vm-123-disk-0,size=40G", digest="c" * 40)
            return "UPID:node1:000000AB:100000001:000000CD:resize:123:worker@pve!creation:"
        if endpoint is _Endpoint.VM_START:
            assert method == "POST" and vmid == 123
            self.power = "running"
            return "UPID:node1:000000AB:100000001:000000CD:qmstart:123:worker@pve!creation:"
        if endpoint is _Endpoint.GUEST_INFO:
            info = self.guest_info.pop(0) if self.guest_info else self.default_guest_info
            if isinstance(info, BaseException):
                raise info
            return info
        assert endpoint is _Endpoint.GUEST_AGENT and vmid == 123
        bootstrap = self.access.bootstrap
        assert bootstrap is not None and bootstrap.intent is not None
        if method == "POST":
            assert suffix == "exec" and body is not None
            payload = json.loads(body)
            argv = payload["command"]
            assert argv[:9] == [
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "LANG=C",
                "LC_ALL=C",
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
            ]
            assert argv[-3:] == [bootstrap.intent.nonce, "operator", "5"]
            assert payload["input-data"] == ""
            assert hashlib.sha256(argv[9].encode("ascii")).hexdigest() == bootstrap.intent.source_sha256
            # This request is inspected only. Nothing dispatches its Bash on the host.
            return {"pid": 321}
        assert method == "GET" and suffix == "exec-status?pid=321"
        return {
            "exited": True,
            "exitcode": 0,
            "out-data": f"AGW_CREATION_BOOTSTRAP_1:{bootstrap.intent.nonce}:{self.bootstrap_stage}:0\n",
        }


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Database]:
    with closing(Database(tmp_path_factory.mktemp("creation-access-ledger") / "state.db")) as value:
        yield value


@pytest.fixture
def owned(database: Database, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def advance(seconds: float) -> None:
        assert 0 < seconds <= 0.1
        clock[0] = round(clock[0] + seconds, 6)

    monkeypatch.setattr(time, "sleep", advance)
    name = "vm-" + hashlib.sha256(request.node.name.encode()).hexdigest()[:16]
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, name), "proof")
    custody, secrets = LocalDeliveryCustody(), _Secrets()
    platform = ProxmoxPlatform(
        "site-one",
        {
            "name": "proxmox",
            "api_url": "https://pve.example:8006",
            "node": "node1",
            "token_id": "worker@pve!creation",
            "token_secret": "pve-token",
            "template_vmids": {"bookworm": 9000},
            "storage": "local-lvm",
            "pool": "agents",
        },
    )
    provision = ProvisionRequest(
        vm_name=name,
        debian_release=DebianRelease.BOOKWORM,
        hostname=f"project-{name}",
        system_slug="project",
        instance_marker=MARKER,
        admin_username="operator",
        ssh_public_key=TEST_SSH_PUBLIC_KEY,
        ssh_private_key=None,
        tailscale_auth_key=JOIN_KEY,
        progress=_UnusedProgress(),
        cpus=4,
        memory_gib=8,
        disk_gib=40,
        swap_gib=4,
    )
    value: Harness | None = None

    def legacy_unavailable(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("legacy creation and connection discovery are unavailable")

    for method in (
        "_api",
        "create",
        "native_transport",
        "build_native_execution_access",
        "resolve_native_execution_binding",
    ):
        monkeypatch.setattr(ProxmoxPlatform, method, legacy_unavailable)

    def exchange(
        wire: _ProxmoxWire,
        endpoint: _Endpoint,
        method: str,
        suffix: str | None,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None,
        timeout: float | None,
    ) -> object:
        assert value is not None
        return value.exchange(wire, endpoint, method, suffix, custody=custody, body=body, timeout=timeout)

    monkeypatch.setattr(_ProxmoxWire, "_exchange", exchange)
    access = platform.build_creation_access(provision, RunContext(secrets=secrets), owner=owner, custody=custody)
    assert isinstance(access, ProxmoxOwnedCreationAccess)
    value = Harness(database, owner, custody, platform, provision, secrets, access, clock=clock)
    yield value
    assert custody.close(Deadline.after(1))


def test_passive_factory_then_public_bootstrap_retains_exact_owner_and_connection(owned: Harness) -> None:
    access = owned.access
    assert owned.calls == owned.secrets.calls == [] and access.platform_metadata == {} and owned.row() is None
    assert JOIN_KEY not in repr(access) and TOKEN not in repr(access)
    result = access.provision(Deadline.after(5))
    assert owned.secrets.calls == ["pve-token"]
    assert result.platform_metadata == access.platform_metadata == {"vmid": "123", "node": "node1"}
    assert result.binding is access.binding and result.binding.delivery_account == "root"
    assert isinstance(result.binding.carrier, ProxmoxCarrier) and access._wire is not None
    assert result.binding.carrier._wire._connection is access._wire._connection
    assert access.independent_availability is _IndependentJobAvailability.NO_IDLE_STOP and access.guest_check is None
    assert access.bootstrap is not None and access.bootstrap.observation.bootstrap_succeeded
    framed = json.dumps(
        ("agentworks.proxmox.provider-locator.v1", "https://pve.example:8006", 123, owned.current["vmgenid"]),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert result.locator.token == "proxmox:v1:" + hashlib.sha256(framed).hexdigest()
    mutations = [(endpoint, method, vmid) for endpoint, method, vmid in owned.calls if method != "GET"]
    assert mutations == [
        (_Endpoint.VM_CLONE, "POST", 9000),
        (_Endpoint.VM_CONFIGURE, "PUT", 123),
        (_Endpoint.VM_RESIZE, "PUT", 123),
        (_Endpoint.VM_START, "POST", 123),
        (_Endpoint.GUEST_AGENT, "POST", 123),
    ]
    settings = next(json.loads(body) for endpoint, body in owned.bodies if endpoint is _Endpoint.VM_CONFIGURE)
    assert settings["cores"] == 4 and settings["memory"] == "8192" and settings["ciuser"] == "operator"
    row = owned.row()
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert len(owned.owner.list_pending_lifecycle_obligations()) == 1
    assert owned.database.operations.inspect(owned.owner.ownership.scope) is not None
    assert access.observe_power(Deadline.after(1)) is VMStatus.RUNNING
    assert access.route_check is not None
    access.route_check(Deadline.after(1))
    with pytest.raises(StateError):
        access.provision(Deadline.after(5))


def test_successful_transfer_resolves_resource_but_never_releases_owner(owned: Harness) -> None:
    owned.access.provision(Deadline.after(5))
    before = owned.calls.copy()
    owned.access.complete_bootstrap(Deadline.after(1))
    row = owned.row()
    assert row is not None and row.state is LifecycleObligationState.RESOLVED
    assert json.loads(row.payload)["accepted"] is True
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(1))
    assert owned.calls == before
    assert owned.database.operations.inspect(owned.owner.ownership.scope) is not None
    with pytest.raises(StateError):
        owned.access.request_rollback()
    owned.owner.seal_lifecycle_obligations()
    owned.owner.record_effects_resolved()
    owned.owner.close()
    assert owned.database.operations.inspect(owned.owner.ownership.scope) is None


def test_aborted_unaccepted_resource_is_retained_without_dummy_rollback(owned: Harness) -> None:
    owned.access.provision(Deadline.after(5))
    owned.access.request_rollback()
    before = owned.calls.copy()
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(1))
    assert owned.calls == before
    with pytest.raises(StateError):
        owned.access.complete_bootstrap(Deadline.after(1))
    with pytest.raises(StateError):
        owned.owner.close()
    assert owned.database.operations.inspect(owned.owner.ownership.scope) is not None


@pytest.mark.parametrize(
    "endpoint,method",
    [
        (_Endpoint.VM_CLONE, "POST"),
        (_Endpoint.VM_CONFIGURE, "PUT"),
        (_Endpoint.VM_START, "POST"),
        (_Endpoint.GUEST_AGENT, "POST"),
    ],
)
def test_lost_wire_reply_keeps_actual_controller_and_original_control_without_replay(
    owned: Harness, endpoint: _Endpoint, method: str
) -> None:
    cause = RuntimeError("original cause")
    owned.primary.__cause__ = cause
    owned.fault = endpoint, method
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.access.provision(Deadline.after(5))
    assert caught.value is owned.primary and caught.value.__cause__ is cause
    controllers = owned.access.clone, owned.access.configuration, owned.access.activation, owned.access.bootstrap
    assert owned.access.clone is not None and owned.access.clone.wire_effect_possible
    assert owned.access.platform_metadata == (
        {} if endpoint is _Endpoint.VM_CLONE else {"vmid": "123", "node": "node1"}
    )
    owned.access.request_rollback()
    owned.owner.stop_admission()
    before = owned.calls.copy()
    assert not owned.access.settle(Deadline.after(1))
    assert owned.calls == before
    assert controllers == (
        owned.access.clone,
        owned.access.configuration,
        owned.access.activation,
        owned.access.bootstrap,
    )
    row = owned.row()
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("committed", [False, True])
def test_clone_ack_bookkeeping_retains_same_controller_and_observes_without_replay(
    owned: Harness, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    repository = owned.database.operations
    original = repository.publish_lifecycle_obligation_payload
    primary = KeyboardInterrupt("clone acknowledgment bookkeeping")

    def lost(
        ownership: OperationOwnership,
        obligation_id: str,
        *,
        expected_revision: int,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        clone = owned.access.clone
        if clone is not None and obligation_id == clone.obligation_id:
            if committed:
                original(
                    ownership,
                    obligation_id,
                    expected_revision=expected_revision,
                    payload_version=payload_version,
                    payload=payload,
                )
            raise primary
        return original(
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.access.provision(Deadline.after(5))
    clone = owned.access.clone
    assert caught.value is primary and clone is not None and clone.intent.upid is not None
    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", original)
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(1))
    assert owned.access.clone is clone and clone.association is not None
    assert owned.access.platform_metadata == {"vmid": "123", "node": "node1"}
    assert sum(endpoint is _Endpoint.VM_CLONE for endpoint, _, _ in owned.calls) == 1
    assert owned.access.configuration is None


@pytest.mark.parametrize("committed", [False, True])
def test_acceptance_reply_loss_cannot_reopen_rollback(
    owned: Harness, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    owned.access.provision(Deadline.after(5))
    original = owned.database.operations.resolve_lifecycle_obligation
    primary = KeyboardInterrupt("transfer resolve reply")

    def lost(ownership: OperationOwnership, obligation_id: str) -> LifecycleObligation:
        if obligation_id == owned.access._resource_id:
            if committed:
                original(ownership, obligation_id)
            raise primary
        return original(ownership, obligation_id)

    monkeypatch.setattr(owned.database.operations, "resolve_lifecycle_obligation", lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.access.complete_bootstrap(Deadline.after(1))
    assert caught.value is primary
    with pytest.raises(StateError):
        owned.access.request_rollback()
    monkeypatch.setattr(owned.database.operations, "resolve_lifecycle_obligation", original)
    before = owned.calls.copy()
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(1)) and owned.calls == before


def test_incomplete_public_bootstrap_does_not_produce_result_or_transfer(owned: Harness) -> None:
    owned.bootstrap_stage = "packages"
    with pytest.raises(StateError):
        owned.access.provision(Deadline.after(5))
    assert owned.access.bootstrap is not None and not owned.access.bootstrap.observation.bootstrap_succeeded
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(1))
    with pytest.raises(StateError):
        owned.access.complete_bootstrap(Deadline.after(1))
    assert sum(endpoint is _Endpoint.GUEST_AGENT and method == "POST" for endpoint, method, _ in owned.calls) == 1


def test_inputs_are_snapshotted_without_retaining_join_key(owned: Harness) -> None:
    owned.request.cpus, owned.request.memory_gib, owned.request.disk_gib = 99, 99, 99
    owned.request.admin_username = "changed"
    owned.request.tailscale_auth_key = "changed-key"
    owned.access.provision(Deadline.after(5))
    assert owned.current["cores"] == 4 and owned.current["memory"] == "8192" and owned.current["ciuser"] == "operator"
    assert owned.current["scsi0"] == "local-lvm:vm-123-disk-0,size=40G"
    access = owned.access
    assert access.clone is not None and access.configuration is not None
    assert access.activation is not None and access.bootstrap is not None
    for identifier in (
        access._resource_id,
        access.clone.obligation_id,
        access.configuration.configuration_obligation_id,
        access.configuration.resize_obligation_id,
        access.activation.obligation_id,
        access.bootstrap.obligation_id,
    ):
        row = owned.owner.inspect_lifecycle_obligation(identifier)
        assert row is not None
        assert JOIN_KEY.encode() not in row.payload and TOKEN.encode() not in row.payload


@pytest.mark.parametrize("invalid", ["tls", "template", "marker", "key"])
def test_pure_input_refusal_precedes_secret_delivery(owned: Harness, invalid: str) -> None:
    config = owned.platform.config.model_dump()
    if invalid == "tls":
        config["verify_ssl"] = False
    if invalid == "template":
        config["template_vmids"] = {}
    platform = ProxmoxPlatform("site-one", config)
    provision = replace(owned.request, instance_marker="invalid") if invalid == "marker" else owned.request
    if invalid == "key":
        provision = replace(provision, ssh_public_key="not-a-key")
    with pytest.raises((ConfigError, ValidationError)):
        platform.build_creation_access(
            provision, RunContext(secrets=owned.secrets), owner=owned.owner, custody=owned.custody
        )
    assert owned.calls == owned.secrets.calls == []


def test_unused_rollback_is_passive_and_settles_without_resource_adoption(owned: Harness) -> None:
    owned.access.request_rollback()
    with pytest.raises(StateError):
        owned.access.provision(Deadline.after(5))
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(1))
    assert owned.access.platform_metadata == {} and owned.row() is None and owned.calls == owned.secrets.calls == []


@pytest.mark.parametrize("committed", [False, True])
def test_resource_arm_interruption_before_clone_wire_can_discharge_unused_intent(
    owned: Harness, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    original = owned.database.operations.mark_lifecycle_obligation_possible_effect
    primary = KeyboardInterrupt("resource arm reply")

    def lost(ownership: OperationOwnership, obligation_id: str) -> LifecycleObligation:
        assert obligation_id == owned.access._resource_id
        if committed:
            original(ownership, obligation_id)
        raise primary

    monkeypatch.setattr(owned.database.operations, "mark_lifecycle_obligation_possible_effect", lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.access.provision(Deadline.after(5))
    assert caught.value is primary
    clone = owned.access.clone
    assert clone is not None and not clone.wire_effect_possible and clone.association is None
    assert owned.access.platform_metadata == {}
    monkeypatch.setattr(owned.database.operations, "mark_lifecycle_obligation_possible_effect", original)
    owned.access.request_rollback()
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(1))
    assert owned.calls == [(_Endpoint.NEXT_ID, "GET", 9000)]
    row = owned.row()
    assert row is not None and row.state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("committed", [False, True])
def test_acceptance_publication_loss_preserves_closed_window_and_exact_transfer(
    owned: Harness, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    owned.access.provision(Deadline.after(5))
    original = owned.database.operations.publish_lifecycle_obligation_payload
    primary = KeyboardInterrupt("transfer publication reply")

    def lost(
        ownership: OperationOwnership,
        obligation_id: str,
        *,
        expected_revision: int,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        assert obligation_id == owned.access._resource_id and json.loads(payload)["accepted"] is True
        if committed:
            original(
                ownership,
                obligation_id,
                expected_revision=expected_revision,
                payload_version=payload_version,
                payload=payload,
            )
        raise primary

    monkeypatch.setattr(owned.database.operations, "publish_lifecycle_obligation_payload", lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.access.complete_bootstrap(Deadline.after(1))
    assert caught.value is primary
    with pytest.raises(StateError):
        owned.access.request_rollback()
    monkeypatch.setattr(owned.database.operations, "publish_lifecycle_obligation_payload", original)
    before = owned.calls.copy()
    owned.owner.stop_admission()
    assert owned.access.settle(Deadline.after(1)) and owned.calls == before
    row = owned.row()
    assert row is not None and row.state is LifecycleObligationState.RESOLVED
    assert json.loads(row.payload)["accepted"] is True


@pytest.mark.parametrize("first", [_WireFailure("QGA not ready"), {"result": {"version": "9.0"}}])
def test_readiness_retries_unavailable_or_malformed_information_under_original_budget(
    owned: Harness, first: dict[str, object] | BaseException
) -> None:
    owned.guest_info.append(first)
    owned.access.provision(Deadline.after(5))
    assert sum(endpoint is _Endpoint.GUEST_INFO for endpoint, _, _ in owned.calls) == 2
    assert owned.clock[0] == 100.1
    assert owned.access.bootstrap is not None and owned.access.bootstrap.observation.bootstrap_succeeded
    assert sum(endpoint is _Endpoint.VM_START for endpoint, _, _ in owned.calls) == 1


@pytest.mark.parametrize("primary", [KeyboardInterrupt("readiness"), SystemExit(7), StateError("settlement refused")])
def test_readiness_does_not_swallow_control_or_settlement_refusal(owned: Harness, primary: BaseException) -> None:
    cause = RuntimeError("original readiness cause")
    primary.__cause__ = cause
    owned.guest_info.append(primary)
    with pytest.raises(type(primary)) as caught:
        owned.access.provision(Deadline.after(5))
    assert caught.value is primary and caught.value.__cause__ is cause
    assert sum(endpoint is _Endpoint.GUEST_INFO for endpoint, _, _ in owned.calls) == 1
    assert owned.clock[0] == 100.0 and owned.access.bootstrap is None


def test_readiness_unavailability_expires_original_budget_without_start_replay(owned: Harness) -> None:
    owned.default_guest_info = _WireFailure("QGA unavailable")
    deadline = Deadline.after(5)
    with pytest.raises(TimeoutError):
        owned.access.provision(deadline)
    assert deadline.expired and owned.clock[0] == 105.0
    assert 1 <= sum(endpoint is _Endpoint.GUEST_INFO for endpoint, _, _ in owned.calls) <= 51
    assert sum(endpoint is _Endpoint.VM_START for endpoint, _, _ in owned.calls) == 1
    assert owned.access.bootstrap is None
    owned.owner.stop_admission()
    assert not owned.access.settle(Deadline.after(1))


def test_unavailable_readiness_with_unsettled_actual_delivery_cannot_retry(
    owned: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[LocalProcessOwner] = []

    def unavailable(wire: _ProxmoxWire, *, custody: LocalDeliveryCustody, timeout: float) -> NoReturn:
        assert custody is owned.custody and wire is owned.access._wire and timeout == 5
        attempts.append(custody.begin_process())
        raise _WireFailure("HTTP delivery unavailable")

    monkeypatch.setattr(_ProxmoxWire, "request_guest_info", unavailable)
    with pytest.raises(StateError):
        owned.access.provision(Deadline.after(5))
    assert len(attempts) == 1 and not owned.custody.settled
    assert owned.clock[0] == 100.0 and owned.access.bootstrap is None
