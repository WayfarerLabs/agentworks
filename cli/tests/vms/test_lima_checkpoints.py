"""Lima's native managed-checkpoint lifecycle."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.ssh import SSHError


def _vm() -> object:
    return SimpleNamespace(name="dev", platform_metadata={"instance_name": "agw-dev"})


class _LimaSnapshots:
    def __init__(self, names: list[str] | None = None) -> None:
        self.names = list(names or [])
        self.commands: list[str] = []

    def run(self, command: str, **_kwargs: object) -> str:
        self.commands.append(command)
        if "snapshot list" in command:
            return "\n".join(self.names)
        if "snapshot create" in command:
            self.names.append(command.rsplit(" ", 1)[-1])
        elif "snapshot delete" in command:
            self.names.remove(command.rsplit(" ", 1)[-1])
        return ""


def _platform(monkeypatch: pytest.MonkeyPatch, backend: _LimaSnapshots) -> LimaPlatform:
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    monkeypatch.setattr(LimaPlatform, "_run_lima", lambda self, command, **kwargs: backend.run(command, **kwargs))
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm, ctx: VMStatus.STOPPED)
    return platform


def _create_checkpoint(
    platform: LimaPlatform,
    vm: object,
    name: str,
    ctx: RunContext,
    *,
    resume: bool = False,
) -> CheckpointDescriptor:
    return platform.create_checkpoint(  # type: ignore[arg-type]
        vm,
        name,
        ctx,
        operation_id="create-1",
        resume=resume,
    )


def test_lima_checkpoint_lifecycle_is_scoped_and_replay_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _LimaSnapshots(["operator-snapshot"])
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = CheckpointDescriptor(name="agw-checkpoint-1", identifier="agw-checkpoint-1")

    assert _create_checkpoint(platform, vm, descriptor.name, ctx) == descriptor
    assert _create_checkpoint(platform, vm, descriptor.name, ctx, resume=True) == descriptor
    assert platform.list_checkpoints(vm, ctx) == (descriptor,)  # type: ignore[arg-type]
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]
    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]

    assert sum("snapshot create" in command for command in backend.commands) == 1
    assert sum("snapshot apply" in command for command in backend.commands) == 1
    assert sum("snapshot delete" in command for command in backend.commands) == 1
    assert all("agw-dev" in command for command in backend.commands)


def test_lima_checkpoint_create_refuses_a_running_vm_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _LimaSnapshots(["agw-checkpoint-1"])
    platform = _platform(monkeypatch, backend)
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm, ctx: VMStatus.RUNNING)

    with pytest.raises(StateError):
        _create_checkpoint(platform, _vm(), "agw-checkpoint-1", RunContext())

    assert not any("snapshot create" in command for command in backend.commands)


def test_lima_checkpoint_inventory_maps_unsupported_driver_to_state_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})

    def _unsupported(self: LimaPlatform, command: str, **kwargs: object) -> str:
        raise SSHError("snapshotter is not implemented")

    monkeypatch.setattr(LimaPlatform, "_run_lima", _unsupported)

    with pytest.raises(StateError) as caught:
        platform.list_checkpoints(_vm(), RunContext())  # type: ignore[arg-type]

    assert caught.value.entity_kind == "vm"
    assert isinstance(caught.value.__cause__, SSHError)


def test_lima_restore_rejects_a_foreign_provider_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _LimaSnapshots(["agw-checkpoint-1"])
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError):
        platform.restore_checkpoint(  # type: ignore[arg-type]
            _vm(),
            CheckpointDescriptor(name="agw-checkpoint-1", identifier="another-tag"),
            RunContext(),
            operation_id="restore-1",
        )

    assert not any("snapshot apply" in command for command in backend.commands)


def test_lima_create_refuses_duplicate_managed_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _LimaSnapshots(["agw-checkpoint-1", "agw-checkpoint-1"])
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError):
        _create_checkpoint(platform, _vm(), "agw-checkpoint-1", RunContext())

    assert not any("snapshot create" in command for command in backend.commands)
