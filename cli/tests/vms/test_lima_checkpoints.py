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
from agentworks.capabilities.vm_platform.lima import (
    LimaPlatform,
    _rename_directory_exclusive,
    _rename_remote_directory_exclusive,
)
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
        self.inventory_failure = False
        self.symlink_paths: set[str] = set()
        self.interrupt_after_move: int | None = None
        self.move_count = 0
        self.commands: list[str] = []

    def run(self, command: str, **_kwargs: object) -> str:
        self.commands.append(command)
        argv = shlex.split(command)
        if argv[:3] == ["limactl", "list", "--quiet"]:
            if self.inventory_failure:
                raise OSError("Lima inventory contains an unreadable instance")
            return "\n".join(sorted(self.instances.keys() | self.incomplete_instances))
        if argv[:3] == ["limactl", "list", "--json"]:
            record = self.instances.get(argv[3])
            return json.dumps(record) if record is not None else ""
        if argv[0] == "/bin/test":
            negated = argv[1] == "!"
            predicate_index = 2 if negated else 1
            predicate = argv[predicate_index]
            path = argv[predicate_index + 1]
            existing_dirs = {str(record["dir"]) for record in self.instances.values()}
            result = {
                "-d": path in existing_dirs,
                "-e": path in existing_dirs,
                "-L": path in self.symlink_paths,
            }[predicate]
            if negated:
                result = not result
            if not result:
                raise OSError(f"test failed: {command}")
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

    def rename(self, source_path: str, target_path: str) -> None:
        source = posixpath.basename(source_path)
        target = posixpath.basename(target_path)
        record = self.instances.pop(source)
        record.update(name=target, dir=target_path)
        self.instances[target] = record
        self.move_count += 1
        if self.move_count == self.interrupt_after_move:
            raise KeyboardInterrupt

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
    monkeypatch.setattr(
        LimaPlatform, "_rename_lima_instance_directory", lambda self, source, target: backend.rename(source, target)
    )
    return platform


def test_exclusive_directory_rename_does_not_follow_a_target_symlink(tmp_path: Any) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    target = tmp_path / "target"
    source.mkdir()
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileExistsError):
        _rename_directory_exclusive(str(source), str(target))

    assert source.is_dir()
    assert target.is_symlink()
    assert list(outside.iterdir()) == []


def test_exclusive_directory_rename_moves_the_exact_directory(tmp_path: Any) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()

    _rename_directory_exclusive(str(source), str(target))

    assert not source.exists()
    assert target.is_dir()


def test_remote_directory_rename_forces_standard_sftp_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        invocation.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima.subprocess.run", fake_run)

    _rename_remote_directory_exclusive("operator@host", "/lima/source", "/lima/target")

    assert invocation["args"][-2:] == ["--", "operator@host"]
    assert invocation["input"] == b'rename -l "/lima/source" "/lima/target"\n'


def test_remote_lima_readiness_requires_sftp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda command: None if command == "sftp" else f"/bin/{command}")

    readiness = LimaPlatform.not_ready({"placement": {"mode": "ssh", "host": "operator@host"}})

    assert not readiness.is_ready
    assert readiness.reason is not None


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


def test_vz_create_reports_repair_for_an_unreadable_partial_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    backend.inventory_failure = True
    platform = _platform(monkeypatch, backend)

    with pytest.raises(StateError) as caught:
        _create_checkpoint(platform, _vm(), _CHECKPOINT_NAME, RunContext(), resume=True)

    assert caught.value.hint is not None
    assert backend.move_count == 0


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
    backend.interrupt_after_move = 1

    with pytest.raises(KeyboardInterrupt):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    backend.interrupt_after_move = None
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
    backend.interrupt_after_move = 2

    with pytest.raises(KeyboardInterrupt):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    backend.interrupt_after_move = None
    move_count = backend.move_count
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    assert backend.instances[descriptor.identifier]["param"]["agentworksCheckpointLastRestore"] == operation_id
    assert backend.move_count == move_count
    assert backend.instances["agw-dev"]["param"]["agentworksRecoveryOperation"] == operation_id


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

    operation_id = "restore-2"
    discard = f"agwrd-{descriptor.identifier.removeprefix('agwcp-')}"
    backend.interrupt_after_move = backend.move_count + 1

    with pytest.raises(KeyboardInterrupt):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    backend.interrupt_after_move = None
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

    assert backend.move_count == 0


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

    assert backend.move_count == 0


def test_vz_restore_refuses_a_hidden_target_symlink_before_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, RunContext())
    emergency = f"agwem-{descriptor.identifier.removeprefix('agwcp-')}"
    backend.symlink_paths.add(f"/fake-lima/{emergency}")

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, RunContext(), operation_id="restore-1")  # type: ignore[arg-type]

    assert "agw-dev" in backend.instances
    assert backend.move_count == 0


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
    move_count = backend.move_count

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-2")  # type: ignore[arg-type]

    assert not any(command.startswith("limactl clone ") for command in backend.commands[command_count:])
    assert backend.move_count == move_count


def test_vz_completed_restore_refuses_a_broken_source_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    operation_id = "restore-1"
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]
    stem = descriptor.identifier.removeprefix("agwcp-")
    discard = f"agwrd-{stem}"
    discard_record = copy.deepcopy(backend.instances["agw-dev"])
    discard_record.update(name=discard, status="Stopped", protected=False, dir=f"/fake-lima/{discard}")
    discard_record["param"].update(
        agentworksRecoveryRole="discard",
        agentworksRecoveryCheckpoint=descriptor.identifier,
        agentworksRecoverySource="agw-dev",
        agentworksRecoveryOperation=operation_id,
    )
    backend.instances[discard] = discard_record
    backend.instances["agw-dev"]["status"] = "Broken"
    command_count = len(backend.commands)

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id=operation_id)  # type: ignore[arg-type]

    assert discard in backend.instances
    assert not any("limactl delete" in command for command in backend.commands[command_count:])


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


def test_vz_delete_removes_an_owned_unprotected_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LimaBackend(vm_type="vz")
    platform = _platform(monkeypatch, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, _CHECKPOINT_NAME, ctx)
    backend.instances[descriptor.identifier]["protected"] = False

    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]

    assert descriptor.identifier not in backend.instances


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
