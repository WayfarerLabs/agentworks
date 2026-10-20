"""Script external observations around actual Proxmox creation and core teardown.

HTTP replies, guest/account preparation, public guest configuration/join,
attestation, trust maintenance and SSH helper transcripts are synthetic. The
provider controllers, SQLite claims, route assembly, command access and aggregate
settlement remain real. No guest program or provider request executes.
"""

from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import OperationScope as ContextScope
from agentworks.capabilities.base import RunContext, ScopeLevel, TargetAbsence
from agentworks.capabilities.vm_platform.base import ProvisionRequest
from agentworks.capabilities.vm_platform.registry import VM_PLATFORM_REGISTRY
from agentworks.db import Database, InitStatus, LifecycleObligationState, ProvisioningStatus
from agentworks.debian import DebianRelease
from agentworks.errors import StateError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._target_identity import TargetIdentityPreparation, TargetIdentityStatus
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.carriers._proxmox_http import _Endpoint
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, _ProxmoxWire
from agentworks.execution.carriers.ssh import SSHCarrier, SSHSettings, SSHTrustFiles
from agentworks.execution.models import Command, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExitCode
from agentworks.execution.target import ExecutionRoute
from agentworks.operations import OperationOwner
from agentworks.plugins.proxmox._creation_access import ProxmoxOwnedCreationAccess
from agentworks.plugins.proxmox._rollback import RollbackPhase
from agentworks.vms import _creation_operation as creation
from agentworks.vms import _native_operation
from agentworks.vms._native_operation import VMOperationControlFact
from agentworks.vms.target_identity import compose_managed_vm_target_identity
from agentworks.vms.target_preparation import VMTargetPreparation, VMTargetPreparationStatus
from tests.execution.test_inline_observer import TranscriptCarrier, _complete_transcript
from tests.plugins.test_proxmox_creation_access import JOIN_KEY, MARKER, Harness, _Secrets, _UnusedProgress
from tests.plugins.test_proxmox_rollback import RollbackHarness
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

_ADDRESS = "100.64.0.7"
_ROLLBACK = (_Endpoint.VM_STOP, _Endpoint.VM_REMOVE_DISKS, _Endpoint.VM_DESTROY)


def _install(db: Database, work: Path, scenario: str, patches: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = [100.0]
    patches.setattr(time, "monotonic", lambda: clock[0])

    def advance(seconds):
        assert 0 < seconds <= 0.1
        clock[0] = round(clock[0] + seconds, 6)

    patches.setattr(time, "sleep", advance)
    settings = SSHSettings(work / "trust", work / "identity")
    request = ProvisionRequest(
        vm_name="created",
        debian_release=DebianRelease.BOOKWORM,
        hostname="project-created",
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
    platform = VM_PLATFORM_REGISTRY["proxmox"](
        "site-one",
        {
            "api_url": "https://pve.example:8006",
            "node": "node1",
            "token_id": "worker@pve!creation",
            "token_secret": "pve-token",
            "template_vmids": {"bookworm": 9000},
            "storage": "local-lvm",
            "pool": "agents",
        },
    )
    secrets = _Secrets()
    ctx = RunContext(
        config=SimpleNamespace(operator=SimpleNamespace(ssh=settings)),
        operation_scope=ContextScope(ScopeLevel.VM, vm=request.vm_name),
        secrets=secrets,
    )
    h = SimpleNamespace(
        workflow=None,
        provider=None,
        rollback=None,
        primary=KeyboardInterrupt("controlled observation failure"),
        deadline=Deadline.after(5),
        clock=clock,
        guest_routes=[],
        deliveries=[],
        events=[],
    )
    prepare_context = creation._prepare_created_context

    def observe_workflow(workflow, *args, **kwargs):
        assert h.workflow is None
        h.workflow = workflow
        h.ownership = workflow.owner.ownership
        h.custody = workflow.local_delivery
        claim = db.operations.inspect(h.ownership.scope)
        assert claim is not None and claim.ownership == h.ownership
        return prepare_context(workflow, *args, **kwargs)

    patches.setattr(creation, "_prepare_created_context", observe_workflow)

    def exchange(wire, endpoint, method, suffix, *, custody, body, timeout):
        workflow = h.workflow
        claim = db.operations.inspect(h.ownership.scope)
        assert claim is not None and claim.ownership == h.ownership
        assert custody is h.custody and custody.settled
        if h.provider is None:
            assert isinstance(workflow.creation, ProxmoxOwnedCreationAccess)
            row = db.get_vm(request.vm_name)
            assert row is not None
            assert row.provisioning_status == ProvisioningStatus.IN_PROGRESS.value
            assert row.init_status == InitStatus.PENDING.value
            h.provider = Harness(
                db, workflow.owner, custody, platform, request, secrets, workflow.creation, clock=clock
            )
            h.provider.bootstrap_stage = "packages" if scenario == "prebootstrap" else "complete"
            h.rollback = RollbackHarness(h.provider, free_error=scenario == "unknown-rollback")
        reply = h.rollback.exchange(wire, endpoint, method, suffix, custody=custody, body=body, timeout=timeout)
        if scenario == "prebootstrap" and endpoint is _Endpoint.GUEST_AGENT and method == "GET":
            nonce = h.provider.access.bootstrap.intent.nonce
            return {**reply, "exitcode": 1, "out-data": f"AGW_CREATION_BOOTSTRAP_1:{nonce}:packages:1\n"}
        return reply

    patches.setattr(_ProxmoxWire, "_exchange", exchange)

    def unavailable(*args, **kwargs):
        raise AssertionError("unexpected legacy operation or unscripted native delivery")

    for name in (
        "_api",
        "create",
        "native_transport",
        "build_native_execution_access",
        "resolve_native_execution_binding",
    ):
        patches.setattr(platform, name, unavailable)
    patches.setattr(ProxmoxCarrier, "execute", unavailable)
    patches.setattr(creation, "admit_connection", lambda connection: SSHTrustFiles((work / "known-hosts",)))

    def prepare_guest(vm, selected_platform, locator, binding, *, deadline, owner, observe_locator):
        assert selected_platform is platform and owner is h.workflow.owner and deadline is h.deadline
        assert observe_locator.__self__ is h.provider.access
        canonical = isinstance(binding.carrier, SSHCarrier)
        h.guest_routes.append(canonical)
        if scenario == "unknown-rollback" or canonical and scenario == "canonical-failure":
            raise h.primary
        guest = VMGuestIdentity(vm.instance_marker, "12345678-1234-1234-1234-123456789abc", 1)
        target = compose_managed_vm_target_identity(vm, locator, guest)
        return VMTargetPreparation(
            VMTargetPreparationStatus.PREPARED, target, SimpleNamespace(observation=SimpleNamespace(identity=guest))
        )

    def prepare_account(carrier, *, deadline, owner, **kwargs):
        assert owner is h.workflow.owner and deadline is h.deadline
        ordinary = IdentityPlan(IdentityExpectation(1000, 1000, (1000,)), IdentityMode.DIRECT)
        elevated = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
        return TargetIdentityPreparation(TargetIdentityStatus.PREPARED, ordinary, elevated, None, None, None)

    patches.setattr(_native_operation, "prepare_managed_vm_target_from_platform", prepare_guest)
    patches.setattr(_native_operation, "prepare_target_identity", prepare_account)

    def bootstrap(operation, selected_request, deadline):
        assert operation is h.workflow.routes[0].views and selected_request is request and deadline is h.deadline
        h.events.append("bootstrap")
        return _ADDRESS

    def attest(operation, release, deadline):
        assert operation is h.workflow.routes[0].views and deadline is h.deadline
        h.events.append("attest")
        return release

    patches.setattr(creation, "bootstrap_created_guest", bootstrap)
    patches.setattr(creation, "attest_created_guest", attest)

    class Trust:
        def __init__(self, owner, *, vm_name, admin_username, instance_marker, endpoint, settings, deadline):
            assert owner is h.workflow.owner and deadline is h.deadline
            assert (vm_name, admin_username, instance_marker) == (request.vm_name, request.admin_username, MARKER)
            row = db.get_vm(vm_name)
            config = ctx.config
            assert row is not None and config is not None
            assert row.tailscale_host == endpoint == _ADDRESS and settings is config.operator.ssh

        def prepare(self):
            h.events.append("trust")

        def close(self, deadline):
            assert deadline is h.deadline
            h.resource = h.provider.row()
            h.events.append("trust-close")
            return True

    patches.setattr(creation, "CreationSSHTrust", Trust)

    def ssh_delivery(carrier, invocation, *, io, deadline, custody):
        assert carrier._connection.host == _ADDRESS and carrier._connection.user == request.admin_username
        assert deadline is h.deadline and custody.settled
        claim = db.operations.inspect(h.ownership.scope)
        assert claim is not None and claim.ownership == h.ownership
        active = h.workflow.routes[1].views.execution_operation.active_inline_calls
        assert len(active) == 1 and active[0].operation.outstanding_attempt.local_delivery is custody
        h.deliveries.append(custody)
        transcript = TranscriptCarrier(_complete_transcript(io.output.stdout.nonce, stdout=b"canonical-result\x00\xff"))
        return transcript.execute(invocation, io=io, deadline=deadline)

    patches.setattr(SSHCarrier, "execute", ssh_delivery)
    h.operation = lambda: creation.vm_creation_context(
        db, request, platform, ctx, deadline=h.deadline, trusted_root=PurePosixPath("/trusted")
    )
    h.context, h.secrets = ctx, secrets
    return h


def _assert_canonical(h: SimpleNamespace, ctx: RunContext, db: Database) -> None:
    workflow, access = h.workflow, h.provider.access
    native, canonical = (route.views for route in workflow.routes)
    assert workflow.creation is workflow.access is access
    assert access._owner is workflow.owner and access._custody is workflow.local_delivery is h.custody
    assert native.owner is canonical.owner is workflow.owner
    assert workflow.owner.ownership == h.ownership and workflow.deadline is h.deadline
    assert native.execution_operation._native_binding.carrier is access.binding.carrier
    assert isinstance(access.binding.carrier, ProxmoxCarrier)
    target = ctx.admin_execution_target()
    assert target is not None and target is canonical.target and target.route is ExecutionRoute.CANONICAL
    assert ctx.config is h.context.config and ctx.operation_scope is h.context.operation_scope
    assert ctx.secret("pve-token") == h.context.secret("pve-token")
    assert ctx.agent_execution_target() is None and ctx.agent_execution_target_absence() is TargetAbsence.UNAVAILABLE
    assert workflow.routes[0].body_admission_closed and not workflow.body_admission_closed
    with pytest.raises(StateError):
        native.execution.run(Command(("true",)), profile=Protection.DIRECT)
    row = db.get_vm("created")
    assert row is not None
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value and row.init_status == InitStatus.PENDING.value
    assert row.tailscale_host == _ADDRESS and row.platform_metadata == {"vmid": "123", "node": "node1"}
    resource = h.provider.row()
    assert resource.state is LifecycleObligationState.RESOLVED and json.loads(resource.payload)["accepted"] is True
    execution = target.execution()
    assert execution is not None
    result = execution.run(
        Command(("/bin/fixture-command", "argument with spaces")),
        profile=Protection.DIRECT,
        output=Output.capture(max_bytes=64),
    )
    assert result.dispatch is Dispatch.SENT and result.status == ExitCode(23)
    assert result.stdout.data == b"canonical-result\x00\xff" and result.owned_cleanup_confirmed
    assert len(h.deliveries) == 1 and h.deliveries[0].settled
    claim = db.operations.inspect(h.ownership.scope)
    assert claim is not None and claim.ownership == h.ownership


def exercise_creation(work: Path, scenario: str) -> None:
    with closing(Database(work / "state.db")) as db, pytest.MonkeyPatch.context() as patches:
        h = _install(db, work, scenario, patches)
        if scenario in {"success", "body-failure"}:
            if scenario == "success":
                with h.operation() as ctx:
                    _assert_canonical(h, ctx, db)
            else:
                with pytest.raises(KeyboardInterrupt) as body_failure, h.operation() as ctx:
                    _assert_canonical(h, ctx, db)
                    raise h.primary
                assert body_failure.value is h.primary and h.primary.__cause__ is None
            assert h.guest_routes == [False, True] and h.events == ["bootstrap", "attest", "trust", "trust-close"]
            assert h.rollback.alive and not h.rollback.mutations()
            row = db.get_vm("created")
            assert row is not None
            assert row.provisioning_status == ProvisioningStatus.COMPLETE.value
            assert row.init_status == InitStatus.PENDING.value
            before = h.provider.calls.copy()
            target = ctx.admin_execution_target()
            assert target is not None
            execution = target.execution()
            assert execution is not None
            with pytest.raises(StateError):
                execution.run(Command(("true",)), profile=Protection.DIRECT)
            assert h.provider.calls == before and len(h.deliveries) == 1
        else:
            expected = StateError if scenario == "prebootstrap" else KeyboardInterrupt
            with pytest.raises(expected) as preparation_failure, h.operation():
                raise AssertionError("failed preparation yielded a context")
            if scenario != "prebootstrap":
                assert preparation_failure.value is h.primary
            row = db.get_vm("created")
            assert row is not None
            assert row.provisioning_status == ProvisioningStatus.FAILED.value
            assert row.init_status == InitStatus.PENDING.value
            assert row.platform_metadata == {"vmid": "123", "node": "node1"}
            assert not h.deliveries
            if scenario == "canonical-failure":
                assert row.tailscale_host == _ADDRESS and h.rollback.alive and not h.rollback.mutations()
                assert h.guest_routes == [False, True] and len(h.workflow.routes) == 2
                assert h.primary.__cause__ is None
                assert h.workflow.routes[0].body_admission_closed
                resource = h.resource
                assert resource.state is LifecycleObligationState.RESOLVED and json.loads(resource.payload)["accepted"]
                with pytest.raises(StateError):
                    h.provider.access.request_rollback()
            else:
                assert row.tailscale_host is None and not h.rollback.alive and not h.rollback.images
                assert not h.events
                assert h.rollback.mutations() == list(_ROLLBACK)
                if scenario == "unknown-rollback":
                    assert h.guest_routes == [False]
                    _retry_unknown_rollback(h, db)
                else:
                    assert not h.guest_routes and not h.workflow.routes and preparation_failure.value.__cause__ is None
                    assert h.provider.access.bootstrap.observation.completion is not None
                    assert not h.provider.access.bootstrap.observation.bootstrap_succeeded
                    with pytest.raises(StateError):
                        h.provider.access.complete_bootstrap(Deadline.after(1))
                assert h.provider.access.rollback.state.phase is RollbackPhase.COMPLETE
        row = db.get_vm("created")
        assert row is not None
        expected_status = (
            ProvisioningStatus.COMPLETE if scenario in {"success", "body-failure"} else ProvisioningStatus.FAILED
        )
        assert row.provisioning_status == expected_status.value and row.init_status == InitStatus.PENDING.value
        assert row.instance_marker == MARKER and row.platform_metadata == {"vmid": "123", "node": "node1"}
        assert h.workflow.owner.ownership == h.ownership and h.workflow.local_delivery is h.custody
        assert h.custody.settled and h.workflow._components_settled() and h.workflow.finalizing
        assert db.operations.inspect(h.ownership.scope) is None


def _retry_unknown_rollback(h: SimpleNamespace, db: Database) -> None:
    fact = h.primary.__cause__
    assert type(fact) is VMOperationControlFact and fact._workflow is h.workflow
    claim = db.operations.inspect(h.ownership.scope)
    assert claim is not None and claim.ownership == h.ownership
    access = h.provider.access
    controllers = (access.clone, access.configuration, access.activation, access.bootstrap, access.rollback)
    resource = h.provider.row()
    assert resource.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert access.rollback.state.phase is RollbackPhase.DESTROY_REQUESTED
    assert h.workflow.body_admission_closed and not h.workflow.finalizing and h.custody.settled
    with pytest.raises(StateError):
        h.workflow.owner.close()
    with pytest.raises(StateError):
        OperationOwner.acquire(db.operations, h.ownership.scope, "competing-creation")
    before = h.provider.calls.copy()
    h.rollback.free_error = False
    h.clock[0] += 6
    assert h.deadline.expired
    fresh = Deadline.after(5)
    assert fresh.expires_at > h.deadline.expires_at
    fact.retry_cleanup(fresh)
    assert controllers == (access.clone, access.configuration, access.activation, access.bootstrap, access.rollback)
    assert h.rollback.mutations() == list(_ROLLBACK)
    assert all(method == "GET" for _, method, _ in h.provider.calls[len(before) :])
    assert sum(endpoint is _Endpoint.VMID_FREE for endpoint, _, _ in h.provider.calls) == 2
    assert h.primary.__cause__ is fact
