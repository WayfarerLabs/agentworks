"""Private WSL2-shaped target composition under one operation owner."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope, VMRow
from agentworks.errors import StateError
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._target_identity import TargetIdentityStatus, prepare_target_identity
from agentworks.execution._vm_guest_identity_bundle import FIXED_SOURCE
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, encode_vm_guest_identity_success
from agentworks.execution.access import ExecutionAccess, FileAccess
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
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

    def __init__(self) -> None:
        self.guest_uncertain = False
        self.inline_uncertain = False
        self.calls = 0

    def execute(
        self, carrier: WSL2Carrier, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline
    ) -> CarrierReport:
        carrier.validate(invocation, io=io)
        self.calls += 1
        if FIXED_SOURCE in invocation.argv:
            if self.guest_uncertain:
                return CarrierReport(Dispatch.UNKNOWN)
            assert isinstance(io.output, SinkOutput)
            nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
            payload = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode("ascii")
            io.output.stdout.try_write(memoryview(payload + encode_vm_guest_identity_success(nonce, _GUEST)))
            complete = CapturedOutput(complete=True, retention=Retention.DELIVERED)
            return CarrierReport(Dispatch.SENT, ExitStatus(code=0), stdout=complete, stderr=complete)
        if self.inline_uncertain:
            return CarrierReport(Dispatch.UNKNOWN)
        result = run_process(list(invocation.argv), io=io, deadline=deadline)
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
    dispatch = _LocalWSL2Dispatch()
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


def test_prepared_wsl2_binding_drives_command_and_file_under_one_owner(
    composition: tuple[Database, OperationOwner, VMRow, WSL2Platform, _LocalWSL2Dispatch], tmp_path: Path
) -> None:
    database, owner, vm, platform, dispatch = composition
    prepared = prepare_managed_vm_target_from_platform(
        vm, platform, RunContext(), deadline=Deadline.after(30), owner=owner
    )
    assert prepared.preparation.status is VMTargetPreparationStatus.PREPARED
    assert prepared.preparation.target is not None and prepared.binding is not None
    assert isinstance(prepared.binding.carrier, WSL2Carrier)
    assert prepared.binding.carrier._connection.distribution == "recorded-distro"
    assert prepared.binding.delivery_account == vm.admin_username

    accounts = prepare_target_identity(
        prepared.binding.carrier,
        delivery_account=prepared.binding.delivery_account,
        workload_account=vm.admin_username,
        include_elevated=False,
        runtime_selection=prepared.binding.runtime_selection,
        deadline=Deadline.after(30),
        owner=owner,
    )
    assert accounts.status is TargetIdentityStatus.PREPARED
    assert accounts.ordinary_plan is not None
    execution = ExecutionAccess(
        ExecutionOperation(owner),
        prepared.binding.carrier,
        runtime_selection=prepared.binding.runtime_selection,
        ordinary_plan=accounts.ordinary_plan,
        elevated_plan=accounts.elevated_plan,
        entity_kind="vm",
        entity_name=vm.name,
        deadline=lambda: Deadline.after(30),
    )
    files = FileAccess(
        FileOperation(owner, prepared.preparation.target),
        prepared.binding.carrier,
        trusted_root=PurePosixPath(tmp_path),
        runtime_selection=prepared.binding.runtime_selection,
        ordinary_plan=accounts.ordinary_plan,
        elevated_plan=accounts.elevated_plan,
        entity_kind="vm",
        entity_name=vm.name,
        deadline=lambda: Deadline.after(30),
    )

    target = tmp_path / "observed.txt"
    target.write_bytes(b"observed")
    result = execution.run(Command(("/bin/sh", "-c", "printf composed")), profile=Protection.DIRECT)
    assert result.status == ExitCode(0) and result.stdout.data == b"composed"
    observed = files.stat(PurePosixPath(target))
    assert observed is not None and observed.size == len(b"observed")
    assert dispatch.calls == 4  # Guest identity, account, command, file metadata.
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_uncertain_guest_preparation_withholds_binding_and_later_custody(
    composition: tuple[Database, OperationOwner, VMRow, WSL2Platform, _LocalWSL2Dispatch],
) -> None:
    _, owner, vm, platform, dispatch = composition
    dispatch.guest_uncertain = True
    prepared = prepare_managed_vm_target_from_platform(
        vm, platform, RunContext(), deadline=Deadline.after(30), owner=owner
    )

    assert prepared.preparation.status is VMTargetPreparationStatus.UNCERTAIN
    assert prepared.preparation.target is None and prepared.binding is None
    assert prepared.preparation.requires_owner_retention
    with pytest.raises(StateError):
        owner.borrow()
    assert dispatch.calls == 1


def test_uncertain_execution_prevents_following_file_custody(
    composition: tuple[Database, OperationOwner, VMRow, WSL2Platform, _LocalWSL2Dispatch], tmp_path: Path
) -> None:
    _, owner, vm, platform, dispatch = composition
    prepared = prepare_managed_vm_target_from_platform(
        vm, platform, RunContext(), deadline=Deadline.after(30), owner=owner
    )
    assert prepared.binding is not None and prepared.preparation.target is not None
    accounts = prepare_target_identity(
        prepared.binding.carrier,
        delivery_account=prepared.binding.delivery_account,
        workload_account=vm.admin_username,
        include_elevated=False,
        runtime_selection=prepared.binding.runtime_selection,
        deadline=Deadline.after(30),
        owner=owner,
    )
    assert accounts.ordinary_plan is not None
    execution = ExecutionAccess(
        ExecutionOperation(owner),
        prepared.binding.carrier,
        runtime_selection=prepared.binding.runtime_selection,
        ordinary_plan=accounts.ordinary_plan,
        elevated_plan=None,
        entity_kind="vm",
        entity_name=vm.name,
        deadline=lambda: Deadline.after(30),
    )
    files = FileAccess(
        FileOperation(owner, prepared.preparation.target),
        prepared.binding.carrier,
        trusted_root=PurePosixPath(tmp_path),
        runtime_selection=prepared.binding.runtime_selection,
        ordinary_plan=accounts.ordinary_plan,
        elevated_plan=None,
        entity_kind="vm",
        entity_name=vm.name,
        deadline=lambda: Deadline.after(30),
    )

    dispatch.inline_uncertain = True
    result = execution.run(Command(("/bin/true",)), profile=Protection.DIRECT)
    assert result.dispatch is Dispatch.UNKNOWN
    calls = dispatch.calls
    with pytest.raises(StateError):
        files.stat(PurePosixPath(tmp_path / "observed.txt"))
    assert dispatch.calls == calls
    with pytest.raises(StateError):
        owner.close()
