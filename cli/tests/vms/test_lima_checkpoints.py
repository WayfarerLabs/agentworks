"""Lima's driver-aware managed-checkpoint lifecycle."""

from __future__ import annotations

import copy
import json
import posixpath
import shlex
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.errors import StateError

_CHECKPOINT_NAME = f"agw-{'1' * 32}"


def _vm() -> object:
    return SimpleNamespace(name="dev", platform_metadata={"instance_name": "agw-dev"})


class _LimaBackend:
    def __init__(
        self,
        *,
        vm_type: str = "qemu",
        status: str = "Stopped",
        snapshots: list[str] | None = None,
        additional_disks: list[dict[str, str]] | None = None,
    ) -> None:
        self.instances: dict[str, dict[str, Any]] = {
            "agw-dev": {
                "name": "agw-dev",
                "status": status,
                "vmType": vm_type,
                "additionalDisks": additional_disks or [],
                "protected": False,
                "param": {},
                "generation": "captured",
                "dir": "/fake-lima/agw-dev",
                "vzIdentifier": "original-vz-id",
            }
        }
        self.snapshots = list(snapshots or [])
        self.incomplete_instances: set[str] = set()
        self.commands: list[str] = []

    def run(self, command: str, **_kwargs: object) -> str:
        self.commands.append(command)
        argv = shlex.split(command)
        if argv[:3] == ["limactl", "list", "--quiet"]:
            return "\n".join(sorted(self.instances.keys() | self.incomplete_instances))
        if argv[:3] == ["limactl", "list", "--json"]:
            record = self.instances.get(argv[3])
            return json.dumps(record) if record is not None else ""
        if argv[0] == "/bin/mv":
            source = posixpath.basename(argv[1])
            target = posixpath.basename(argv[2])
            record = self.instances.pop(source)
            record.update(name=target, dir=argv[2])
            self.instances[target] = record
            return ""
        if argv[1:3] == ["snapshot", "list"]:
            return "\n".join(self.snapshots)
        if argv[1:3] == ["snapshot", "create"]:
            self.snapshots.append(argv[-1])
            return ""
        if argv[1:3] == ["snapshot", "delete"]:
            self.snapshots.remove(argv[-1])
            return ""
        if argv[1:3] == ["snapshot", "apply"]:
            return ""
        if argv[1] == "clone":
            source, target = argv[-2:]
            record = copy.deepcopy(self.instances[source])
            record.update(
                name=target,
                status="Stopped",
                protected=False,
                dir=f"/fake-lima/{target}",
                vzIdentifier="",
            )
            expression = argv[argv.index("--set") + 1]
            params = record.setdefault("param", {})
            self._apply_param_expression(params, expression)
            self.instances[target] = record
            return ""
        if argv[1] == "protect":
            self.instances[argv[2]]["protected"] = True
            return ""
        if argv[1] == "unprotect":
            if argv[2] in self.instances:
                self.instances[argv[2]]["protected"] = False
            return ""
        if argv[1] == "rename":
            source, target = argv[-2:]
            record = self.instances.pop(source)
            record.update(name=target, dir=f"/fake-lima/{target}")
            self.instances[target] = record
            return ""
        if argv[1] == "delete":
            self.instances.pop(argv[-1], None)
            return ""
        if argv[1] == "edit":
            target = argv[-1]
            expression = argv[argv.index("--set") + 1]
            self._apply_param_expression(self.instances[target]["param"], expression)
            return ""
        raise AssertionError(f"unexpected Lima command: {command}")

    @staticmethod
    def _apply_param_expression(params: dict[str, Any], expression: str) -> None:
        for raw_segment in expression.split("|"):
            segment = raw_segment.strip()
            if segment.startswith("del(.param.") and segment.endswith(")"):
                params.pop(segment.removeprefix("del(.param.").removesuffix(")"), None)
                continue
            left, raw_value = segment.split("=", 1)
            key = left.strip().removeprefix(".param.")
            params[key] = json.loads(raw_value.strip())


def _platform(monkeypatch: pytest.MonkeyPatch, backend: _LimaBackend) -> LimaPlatform:
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    monkeypatch.setattr(
        LimaPlatform,
        "_run_lima",
        lambda self, command, **kwargs: backend.run(command, **kwargs),
    )
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


def _restore_expression(descriptor: CheckpointDescriptor, operation_id: str) -> str:
    return (
        "del(.param.agentworksCheckpointName) | "
        "del(.param.agentworksCheckpointSource) | "
        "del(.param.agentworksCheckpointLastRestore) | "
        f".param.agentworksCheckpointInstalledRestore = {json.dumps(operation_id)} | "
        '.param.agentworksRecoveryRole = "stage" | '
        f".param.agentworksRecoveryCheckpoint = {json.dumps(descriptor.identifier)} | "
        '.param.agentworksRecoverySource = "agw-dev" | '
        f".param.agentworksRecoveryOperation = {json.dumps(operation_id)}"
    )


def _recovery_role_expression(
    descriptor: CheckpointDescriptor,
    *,
    role: str,
    operation_id: str,
) -> str:
    return (
        f".param.agentworksRecoveryRole = {json.dumps(role)} | "
        f".param.agentworksRecoveryCheckpoint = {json.dumps(descriptor.identifier)} | "
        '.param.agentworksRecoverySource = "agw-dev" | '
        f".param.agentworksRecoveryOperation = {json.dumps(operation_id)}"
    )


def test_qemu_checkpoint_lifecycle_is_scoped_and_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(snapshots=["operator-snapshot"])
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


def test_checkpoint_create_refuses_a_running_vm_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(status="Running")
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError):
        _create_checkpoint(platform, _vm(), _CHECKPOINT_NAME, RunContext())

    assert not any("snapshot create" in command or " clone " in command for command in backend.commands)


def test_checkpoint_create_rejects_an_unsupported_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="krunkit")
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError) as caught:
        _create_checkpoint(platform, _vm(), _CHECKPOINT_NAME, RunContext())

    assert caught.value.entity_kind == "vm"
    assert not any("snapshot create" in command or " clone " in command for command in backend.commands)


def test_restore_rejects_a_foreign_provider_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(snapshots=[_CHECKPOINT_NAME])
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError):
        platform.restore_checkpoint(  # type: ignore[arg-type]
            _vm(),
            CheckpointDescriptor(name=_CHECKPOINT_NAME, identifier="another-tag"),
            RunContext(),
            operation_id="restore-1",
        )

    assert not any("snapshot apply" in command or " clone " in command for command in backend.commands)


def test_qemu_create_refuses_duplicate_managed_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _LimaBackend(snapshots=[_CHECKPOINT_NAME, _CHECKPOINT_NAME])
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError):
        _create_checkpoint(platform, _vm(), _CHECKPOINT_NAME, RunContext())

    assert not any("snapshot create" in command for command in backend.commands)


def test_vz_checkpoint_is_a_stopped_protected_recovery_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, RunContext())

    assert descriptor.name == _CHECKPOINT_NAME
    assert descriptor.identifier in backend.instances
    assert backend.instances[descriptor.identifier]["status"] == "Stopped"
    assert backend.instances[descriptor.identifier]["protected"] is True
    assert platform.list_checkpoints(vm, RunContext()) == (descriptor,)  # type: ignore[arg-type]
    assert not any("snapshot" in command for command in backend.commands)


def test_vz_checkpoint_rejects_additional_disks_before_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz", additional_disks=[{"name": "data"}])
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError):
        _create_checkpoint(platform, _vm(), _CHECKPOINT_NAME, RunContext())

    assert not any(" clone " in command for command in backend.commands)


def test_vz_inventory_rejects_a_different_source_incarnation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    _create_checkpoint(platform, vm, _CHECKPOINT_NAME, RunContext())
    backend.instances["agw-dev"]["param"]["agentworksVmIncarnation"] = "replacement"

    with pytest.raises(StateError):
        platform.list_checkpoints(vm, RunContext())  # type: ignore[arg-type]


def test_vz_inventory_fails_closed_for_an_incomplete_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, RunContext())
    backend.instances.pop(descriptor.identifier)
    backend.incomplete_instances.add(descriptor.identifier)

    with pytest.raises(StateError):
        platform.list_checkpoints(vm, RunContext())  # type: ignore[arg-type]


def test_vz_restore_retains_first_emergency_and_replays_by_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    backend.instances["agw-dev"]["generation"] = "upgraded"

    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]

    stem = descriptor.identifier.removeprefix("agwcp-")
    emergency = f"agwem-{stem}"
    assert backend.instances["agw-dev"]["generation"] == "captured"
    assert backend.instances[emergency]["generation"] == "upgraded"
    assert backend.instances[emergency]["protected"] is True
    assert backend.instances[emergency]["vzIdentifier"] == "original-vz-id"
    assert backend.instances["agw-dev"]["vzIdentifier"] == ""
    first_restore_clone_count = sum(" clone " in command for command in backend.commands)

    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    assert sum(" clone " in command for command in backend.commands) == first_restore_clone_count

    backend.instances["agw-dev"]["generation"] = "later"
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-2")  # type: ignore[arg-type]
    assert backend.instances["agw-dev"]["generation"] == "captured"
    assert backend.instances[emergency]["generation"] == "upgraded"
    assert not any(name.startswith("agwrd-") for name in backend.instances)
    assert not any(name.startswith("agwrs-") for name in backend.instances)


def test_vz_restore_resumes_between_atomic_instance_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    operation_id = "restore-1"
    stem = descriptor.identifier.removeprefix("agwcp-")
    stage = f"agwrs-{stem}"
    emergency = f"agwem-{stem}"

    expression = _restore_expression(descriptor, operation_id)
    backend.run(f"limactl clone --tty=false --set {shlex.quote(expression)} {descriptor.identifier} {stage}")
    role_expression = _recovery_role_expression(
        descriptor,
        role="emergency",
        operation_id=operation_id,
    )
    backend.run(f"limactl edit --tty=false --set {shlex.quote(role_expression)} agw-dev")
    backend.run("limactl protect agw-dev")
    backend.run(f"/bin/mv /fake-lima/agw-dev /fake-lima/{emergency}")

    platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    assert "agw-dev" in backend.instances
    assert emergency in backend.instances
    assert backend.instances[emergency]["protected"] is True
    assert stage not in backend.instances


def test_vz_restore_resumes_after_recovered_instance_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    operation_id = "restore-1"
    stem = descriptor.identifier.removeprefix("agwcp-")
    stage = f"agwrs-{stem}"
    emergency = f"agwem-{stem}"
    expression = _restore_expression(descriptor, operation_id)
    backend.run(f"limactl clone --tty=false --set {shlex.quote(expression)} {descriptor.identifier} {stage}")
    role_expression = _recovery_role_expression(
        descriptor,
        role="emergency",
        operation_id=operation_id,
    )
    backend.run(f"limactl edit --tty=false --set {shlex.quote(role_expression)} agw-dev")
    backend.run("limactl protect agw-dev")
    backend.run(f"/bin/mv /fake-lima/agw-dev /fake-lima/{emergency}")
    backend.run(f"/bin/mv /fake-lima/{stage} /fake-lima/agw-dev")
    move_count = sum(command.startswith("/bin/mv ") for command in backend.commands)

    platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    assert backend.instances[descriptor.identifier]["param"]["agentworksCheckpointLastRestore"] == operation_id
    assert sum(command.startswith("/bin/mv ") for command in backend.commands) == move_count
    assert backend.instances["agw-dev"]["param"]["agentworksCheckpointInstalledRestore"] == operation_id


def test_vz_restore_resumes_after_later_source_is_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    backend.instances["agw-dev"]["generation"] = "later"

    stem = descriptor.identifier.removeprefix("agwcp-")
    stage = f"agwrs-{stem}"
    discard = f"agwrd-{stem}"
    operation_id = "restore-2"
    expression = _restore_expression(descriptor, operation_id)
    backend.run(f"limactl clone --tty=false --set {shlex.quote(expression)} {descriptor.identifier} {stage}")
    role_expression = _recovery_role_expression(
        descriptor,
        role="discard",
        operation_id=operation_id,
    )
    backend.run(f"limactl edit --tty=false --set {shlex.quote(role_expression)} agw-dev")
    backend.run(f"/bin/mv /fake-lima/agw-dev /fake-lima/{discard}")

    platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    assert backend.instances["agw-dev"]["generation"] == "captured"
    assert discard not in backend.instances


def test_vz_restore_refuses_a_running_instance_before_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, RunContext())
    backend.instances["agw-dev"]["status"] = "Running"

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, RunContext(), operation_id="restore-1")  # type: ignore[arg-type]

    assert not any(command.startswith("/bin/mv ") for command in backend.commands)


def test_vz_restore_refuses_an_unsafe_lima_instance_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, RunContext())
    backend.instances["agw-dev"]["dir"] = "/fake-lima/not-the-instance"

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, RunContext(), operation_id="restore-1")  # type: ignore[arg-type]

    assert not any(command.startswith("/bin/mv ") for command in backend.commands)


def test_vz_restore_refuses_a_running_emergency_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    emergency = f"agwem-{descriptor.identifier.removeprefix('agwcp-')}"
    backend.instances[emergency]["status"] = "Running"
    command_count = len(backend.commands)

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-2")  # type: ignore[arg-type]

    assert not any(command.startswith(("limactl clone ", "/bin/mv ")) for command in backend.commands[command_count:])


def test_vz_delete_removes_checkpoint_and_restore_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]

    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]
    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]

    assert platform.list_checkpoints(vm, ctx) == ()  # type: ignore[arg-type]
    assert not any(name.startswith(("agwcp-", "agwem-", "agwrs-", "agwrd-")) for name in backend.instances)


def test_vz_delete_refuses_foreign_recovery_metadata_before_deleting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    emergency = f"agwem-{descriptor.identifier.removeprefix('agwcp-')}"
    backend.instances[emergency]["param"]["agentworksRecoveryCheckpoint"] = "foreign"

    with pytest.raises(StateError):
        platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]

    assert descriptor.identifier in backend.instances
    assert emergency in backend.instances
