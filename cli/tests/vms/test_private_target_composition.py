"""Private WSL2-shaped composition with local command and file helper evidence.

The guest identity and account lookup replies are synthetic. Account lookup is
not proved here; the response uses the process identity so sandbox group
restrictions cannot turn an otherwise valid local helper into a refusal.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope, VMRow
from agentworks.errors import StateError
from agentworks.execution._account_protocol import (
    AccountRequest,
    AccountRequestError,
    decode_account_lookup_request,
    encode_account_identity,
)
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._target_identity import TargetIdentityStatus, prepare_target_identity
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, encode_vm_guest_identity_success
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    EndOfInput,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.carriers.wsl2 import WSL2Carrier
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExitCode
from agentworks.operations import OperationOwner
from agentworks.vms.target_preparation import VMTargetPreparationStatus, prepare_managed_vm_target_from_platform
from tests.execution.files._file_snapshot_support import install_fixture_bundle

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.execution.binding import NativeExecutionBinding

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local fixed helpers require Linux")

_MARKER = "0123456789abcdef0123456789abcdef"
_GUEST = VMGuestIdentity(_MARKER, "00000000-0000-4000-8000-000000000001", 1234)
_LOCATOR = ProviderLocator("wsl2:observed-registration")


def _vm(account: str) -> VMRow:
    return VMRow(
        name="box",
        site="wsl2",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="ready",
        init_status="ready",
        tailscale_host=None,
        cpus=None,
        memory_gib=None,
        disk_gib=None,
        swap_gib=None,
        admin_username=account,
        hostname="box",
        created_at="2026-09-21T00:00:00Z",
        last_seen_at=None,
        platform_metadata={"distro_name": "recorded-distro"},
        instance_marker=_MARKER,
    )


class _LocalWSL2Dispatch:
    """Exercise a real WSL2 binding without launching a Windows client."""

    def __init__(self, database: Database, owner: OperationOwner) -> None:
        self.local_delivery = LocalDeliveryCustody()
        self.inline_uncertain = False
        self.calls = 0
        self.guest_calls = 0
        self.account_calls = 0
        self.local_calls = 0
        self.database = database
        self.owner = owner
        self.binding: NativeExecutionBinding | None = None

    def execute(
        self,
        carrier: WSL2Carrier,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        carrier.validate(invocation, io=io)
        self.calls += 1
        binding = self.binding
        assert binding is not None
        early = binding._early_guest_facts_route
        assert early is not None
        claim = self.database.operations.inspect(self.owner.ownership.scope)
        assert claim is not None and claim.ownership == self.owner.ownership
        assert claim.state is OperationClaimState.POSSIBLE_DISPATCH
        with pytest.raises(StateError):
            self.owner.borrow()
        if carrier is early.carrier:
            self.guest_calls += 1
            assert self.calls == self.guest_calls == 1
            assert carrier.connection.user == "root" and early.account == binding.delivery_account
            assert carrier.connection.distribution == "recorded-distro" and carrier.connection.wsl_executable == "wsl"
            assert isinstance(io.input, EndOfInput)
            assert isinstance(io.output, SinkOutput)
            nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
            payload = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode("ascii")
            io.output.stdout.try_write(memoryview(payload + encode_vm_guest_identity_success(nonce, _GUEST)))
            complete = CapturedOutput(complete=True, retention=Retention.DELIVERED)
            return CarrierReport(Dispatch.SENT, ExitStatus(code=0), stdout=complete, stderr=complete)
        assert carrier is binding.carrier and carrier.connection.user == binding.delivery_account
        assert carrier.connection.distribution == "recorded-distro" and carrier.connection.wsl_executable == "wsl"
        assert self.guest_calls == 1
        request = None
        if isinstance(io.input, FiniteInput):
            with suppress(AccountRequestError):
                request = decode_account_lookup_request(io.input.data)
        if isinstance(request, AccountRequest):
            self.account_calls += 1
            assert isinstance(io.output, SinkOutput) and request.account == binding.delivery_account
            nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
            assert request.nonce == nonce
            groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
            identity = IdentityExpectation(os.geteuid(), os.getegid(), groups)
            payload = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode("ascii")
            io.output.stdout.try_write(memoryview(payload + encode_account_identity(nonce, identity)))
            complete = CapturedOutput(complete=True, retention=Retention.DELIVERED)
            return CarrierReport(Dispatch.SENT, ExitStatus(code=0), stdout=complete, stderr=complete)
        if self.inline_uncertain:
            return CarrierReport(Dispatch.UNKNOWN)
        self.local_calls += 1
        result = run_process(
            list(invocation.argv),
            io=io,
            deadline=deadline,
            custody=custody if custody is not None else self.local_delivery,
        )
        completion = None if result.exit_status is None else ExitStatus(code=result.exit_status)
        return CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )


@pytest.fixture
def composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Database, OperationOwner, VMRow, WSL2Platform, _LocalWSL2Dispatch]]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "box"), "composition")
    import pwd

    vm = _vm(pwd.getpwuid(os.geteuid()).pw_name)
    platform = WSL2Platform("wsl2", {})

    def observe(_vm: VMRow, _ctx: RunContext, *, deadline: Deadline) -> ProviderLocator:
        assert not deadline.expired
        return _LOCATOR

    monkeypatch.setattr(platform, "observe_provider_locator", observe)
    dispatch = _LocalWSL2Dispatch(database, owner)
    monkeypatch.setattr(
        WSL2Carrier,
        "execute",
        lambda carrier, invocation, *, io, deadline: dispatch.execute(carrier, invocation, io=io, deadline=deadline),
    )
    install_fixture_bundle(monkeypatch, tmp_path / "scratch")
    try:
        yield database, owner, vm, platform, dispatch
    finally:
        database.close()


def _prepared_accesses(
    owner: OperationOwner, vm: VMRow, platform: WSL2Platform, root: Path, dispatch: _LocalWSL2Dispatch
) -> tuple[ExecutionOperation, ExecutionAccess, FileAccess]:
    """Prepare the private target and account plan under one existing owner."""
    binding = platform.resolve_native_execution_binding(vm, RunContext(), deadline=Deadline.after(30))
    dispatch.binding = binding
    prepared = prepare_managed_vm_target_from_platform(
        vm, platform, RunContext(), _LOCATOR, binding, deadline=Deadline.after(30), owner=owner
    )
    assert prepared.status is VMTargetPreparationStatus.PREPARED
    assert prepared.target is not None
    assert isinstance(binding.carrier, WSL2Carrier)
    assert binding.carrier._connection.distribution == "recorded-distro"
    assert binding.delivery_account == vm.admin_username

    accounts = prepare_target_identity(
        binding.carrier,
        delivery_account=binding.delivery_account,
        workload_account=vm.admin_username,
        include_elevated=False,
        runtime_selection=binding.runtime_selection,
        deadline=Deadline.after(30),
        owner=owner,
    )
    assert accounts.status is TargetIdentityStatus.PREPARED
    assert accounts.ordinary_plan is not None
    assert accounts.ordinary_plan.expected.euid == os.geteuid()
    operation = ExecutionOperation(owner, prepared.target)
    execution = ExecutionAccess(
        operation,
        binding.carrier,
        runtime_selection=binding.runtime_selection,
        ordinary_plan=accounts.ordinary_plan,
        elevated_plan=accounts.elevated_plan,
        entity_kind="vm",
        entity_name=vm.name,
        deadline=lambda: Deadline.after(30),
    )
    files = FileAccess(
        FileOperation(owner, prepared.target),
        binding.carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=binding.runtime_selection,
        ordinary_plan=accounts.ordinary_plan,
        elevated_plan=accounts.elevated_plan,
        entity_kind="vm",
        entity_name=vm.name,
        deadline=lambda: Deadline.after(30),
    )
    return operation, execution, files


def test_prepared_wsl2_binding_drives_command_and_file_under_one_owner(
    composition: tuple[Database, OperationOwner, VMRow, WSL2Platform, _LocalWSL2Dispatch], tmp_path: Path
) -> None:
    database, owner, vm, platform, dispatch = composition
    operation, execution, files = _prepared_accesses(owner, vm, platform, tmp_path, dispatch)

    target = tmp_path / "observed.txt"
    target.write_bytes(b"observed")
    result = execution.run(Command(("/bin/sh", "-c", "printf composed")), profile=Protection.DIRECT)
    assert result.status == ExitCode(0) and result.stdout.data == b"composed"
    observed = files.stat(PurePosixPath(target))
    assert observed is not None and observed.size == len(b"observed")
    assert dispatch.calls == 4  # Guest identity, account, command, file metadata.
    assert dispatch.guest_calls == dispatch.account_calls == 1 and dispatch.local_calls == 2
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    operation.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_uncertain_execution_prevents_following_file_custody(
    composition: tuple[Database, OperationOwner, VMRow, WSL2Platform, _LocalWSL2Dispatch], tmp_path: Path
) -> None:
    _, owner, vm, platform, dispatch = composition
    operation, execution, files = _prepared_accesses(owner, vm, platform, tmp_path, dispatch)

    dispatch.inline_uncertain = True
    result = execution.run(Command(("/bin/true",)), profile=Protection.DIRECT)
    assert result.dispatch is Dispatch.UNKNOWN
    calls = dispatch.calls
    with pytest.raises(StateError):
        files.stat(PurePosixPath(tmp_path / "observed.txt"))
    assert dispatch.calls == calls
    with pytest.raises(StateError):
        operation.finish()
    with pytest.raises(StateError):
        owner.close()
