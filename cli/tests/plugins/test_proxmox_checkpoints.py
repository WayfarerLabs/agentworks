"""Proxmox managed-checkpoint ownership and replay behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import CheckpointDescriptor
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

if TYPE_CHECKING:
    from agentworks.db import VMRow


class _FakeAPI:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.running = False
        self.calls: list[tuple[object, ...]] = []
        self.complete_snapshot_after_lists: int | None = None

    def list_snapshots(self, node: str, vmid: int) -> list[dict[str, Any]]:
        self.calls.append(("list_snapshots", node, vmid))
        if self.complete_snapshot_after_lists is not None:
            self.complete_snapshot_after_lists -= 1
            if self.complete_snapshot_after_lists == 0:
                for snapshot in self.snapshots:
                    snapshot.pop("snapstate", None)
                self.complete_snapshot_after_lists = None
        return list(self.snapshots)

    def create_snapshot(self, node: str, vmid: int, name: str, *, description: str) -> str:
        self.calls.append(("create_snapshot", node, vmid, name, description))
        self.snapshots.append({"name": name, "description": description})
        return "UPID:create"

    def rollback_snapshot(self, node: str, vmid: int, name: str) -> str:
        self.calls.append(("rollback_snapshot", node, vmid, name))
        return "UPID:rollback"

    def delete_snapshot(self, node: str, vmid: int, name: str) -> str:
        self.calls.append(("delete_snapshot", node, vmid, name))
        self.snapshots = [entry for entry in self.snapshots if entry.get("name") != name]
        return "UPID:delete"

    def wait_for_task(self, node: str, upid: str) -> None:
        self.calls.append(("wait_for_task", node, upid))

    def vm_status(self, node: str, vmid: int) -> dict[str, str]:
        self.calls.append(("vm_status", node, vmid))
        return {"status": "running" if self.running else "stopped"}


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> tuple[ProxmoxPlatform, _FakeAPI, VMRow]:
    fake = _FakeAPI()
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: fake)
    platform = ProxmoxPlatform(
        "pve-site",
        {
            "api_url": "https://pve.example.com:8006",
            "node": "pve1",
            "token_id": "agw@pam!agw",
            "template_vmids": {"trixie": 9001},
        },
    )
    vm = cast(
        "VMRow",
        SimpleNamespace(name="vm1", platform_metadata={"vmid": "100", "node": "pve1"}),
    )
    return platform, fake, vm


def _create_checkpoint(
    platform: ProxmoxPlatform,
    vm: VMRow,
    name: str,
    *,
    resume: bool = False,
) -> CheckpointDescriptor:
    return platform.create_checkpoint(
        vm,
        name,
        RunContext(),
        operation_id="create-1",
        resume=resume,
    )


def test_create_is_replay_safe_and_lists_only_owned_snapshots(
    harness: tuple[ProxmoxPlatform, _FakeAPI, VMRow],
) -> None:
    platform, fake, vm = harness
    fake.snapshots.append({"name": "operator", "description": "keep me"})

    first = _create_checkpoint(platform, vm, "agw-123")
    replay = _create_checkpoint(platform, vm, "agw-123", resume=True)

    assert first == replay == CheckpointDescriptor(name="agw-123", identifier="agw-123")
    assert platform.list_checkpoints(vm, RunContext()) == (first,)
    assert [call[0] for call in fake.calls].count("create_snapshot") == 1


def test_create_refuses_same_name_unmanaged_snapshot(
    harness: tuple[ProxmoxPlatform, _FakeAPI, VMRow],
) -> None:
    platform, fake, vm = harness
    fake.snapshots.append({"name": "agw-123", "description": "operator snapshot"})

    with pytest.raises(StateError):
        _create_checkpoint(platform, vm, "agw-123")


def test_create_waits_for_an_interrupted_snapshot_to_finish(
    harness: tuple[ProxmoxPlatform, _FakeAPI, VMRow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, fake, vm = harness
    fake.snapshots.append(
        {
            "name": "agw-123",
            "description": platform._checkpoint_description(vm),  # noqa: SLF001
            "snapstate": "prepare",
        }
    )
    fake.complete_snapshot_after_lists = 3
    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.sleep", lambda _seconds: None)

    checkpoint = _create_checkpoint(platform, vm, "agw-123", resume=True)

    assert checkpoint == CheckpointDescriptor("agw-123", "agw-123")
    assert [call[0] for call in fake.calls].count("create_snapshot") == 0
    assert [call[0] for call in fake.calls].count("list_snapshots") >= 3


def test_failed_incomplete_snapshot_cannot_be_restored_and_can_be_deleted(
    harness: tuple[ProxmoxPlatform, _FakeAPI, VMRow],
) -> None:
    platform, fake, vm = harness
    checkpoint = CheckpointDescriptor("agw-123", "agw-123")
    fake.snapshots.append(
        {
            "name": checkpoint.name,
            "description": platform._checkpoint_description(vm),  # noqa: SLF001
            "snapstate": "failed",
        }
    )

    with pytest.raises(StateError):
        _create_checkpoint(platform, vm, checkpoint.name, resume=True)
    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, checkpoint, RunContext(), operation_id="restore-1")

    platform.delete_checkpoint(vm, checkpoint, RunContext())

    assert platform.list_checkpoints(vm, RunContext()) == ()


def test_restore_requires_owned_checkpoint_and_stopped_vm(
    harness: tuple[ProxmoxPlatform, _FakeAPI, VMRow],
) -> None:
    platform, fake, vm = harness
    checkpoint = _create_checkpoint(platform, vm, "agw-123")

    fake.running = True
    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, checkpoint, RunContext(), operation_id="restore-1")

    fake.running = False
    platform.restore_checkpoint(vm, checkpoint, RunContext(), operation_id="restore-1")
    assert ("rollback_snapshot", "pve1", 100, "agw-123") in fake.calls
    assert platform.status(vm, RunContext()) is VMStatus.STOPPED


def test_delete_is_replay_safe_and_proves_absence(
    harness: tuple[ProxmoxPlatform, _FakeAPI, VMRow],
) -> None:
    platform, fake, vm = harness
    checkpoint = _create_checkpoint(platform, vm, "agw-123")

    platform.delete_checkpoint(vm, checkpoint, RunContext())
    platform.delete_checkpoint(vm, checkpoint, RunContext())

    assert platform.list_checkpoints(vm, RunContext()) == ()
    assert [call[0] for call in fake.calls].count("delete_snapshot") == 1
