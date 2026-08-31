"""WSL2's host-export managed-checkpoint lifecycle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import wsl2 as wsl2_mod
from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import StateError


def _vm() -> object:
    return SimpleNamespace(
        name="dev",
        admin_username="operator",
        platform_metadata={"distro_name": "agw-dev"},
    )


class _WslBackend:
    def __init__(self) -> None:
        self.registered = True
        self.running = False
        self.contents = b"initial-rootfs"
        self.exports: list[Path] = []
        self.imports: list[list[str]] = []
        self.fail_import = False
        self.fail_unregister = False

    def run(self, args: list[str], *, check: bool = True, timeout: int = 300) -> str:
        del check, timeout
        if args[:2] == ["--list", "--quiet"]:
            return "agw-dev\n" if self.registered else ""
        if args[:2] == ["--list", "--verbose"]:
            if not self.registered:
                return ""
            state = "Running" if self.running else "Stopped"
            return f"  NAME STATE VERSION\n  agw-dev {state} 2\n"
        if args[0] == "--export":
            destination = Path(args[2])
            destination.write_bytes(self.contents)
            self.exports.append(destination)
            return ""
        if args[0] == "--unregister":
            if not self.fail_unregister:
                self.registered = False
                self.running = False
            return ""
        if args[0] == "--import":
            if self.fail_import:
                raise RuntimeError("provider import failed")
            self.imports.append(args)
            self.contents = Path(args[3]).read_bytes()
            self.registered = True
            self.running = False
            return ""
        if args[0] == "--terminate":
            self.running = False
            return ""
        raise AssertionError(f"unexpected wsl arguments: {args}")


def _platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: _WslBackend,
) -> WSL2Platform:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(wsl2_mod, "_wsl", backend.run)
    monkeypatch.setattr(wsl2_mod, "_powershell", lambda *args, **kwargs: "")
    return WSL2Platform("wsl2", {})


def _create_checkpoint(
    platform: WSL2Platform,
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


def test_wsl_checkpoint_create_list_and_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    expected = CheckpointDescriptor(name="agw-checkpoint-1", identifier="wsl2:agw-dev:agw-checkpoint-1")

    assert _create_checkpoint(platform, vm, expected.name, ctx) == expected
    assert _create_checkpoint(platform, vm, expected.name, ctx, resume=True) == expected
    assert platform.list_checkpoints(vm, ctx) == (expected,)  # type: ignore[arg-type]
    assert len(backend.exports) == 1
    assert backend.exports[0].name.endswith(".partial")


def test_wsl_restore_reuses_retained_emergency_and_reimports_same_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, "agw-checkpoint-1", ctx)
    backend.contents = b"state-before-first-restore"

    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    backend.contents = b"state-before-later-restore"
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-2")  # type: ignore[arg-type]

    assert backend.contents == b"initial-rootfs"
    assert len(backend.exports) == 2
    emergency = wsl2_mod._checkpoint_root() / "agw-dev" / "agw-checkpoint-1.pre-restore.tar"
    assert emergency.read_bytes() == b"state-before-first-restore"
    assert len(backend.imports) == 2
    assert all(args[1] == "agw-dev" for args in backend.imports)
    assert all(Path(args[2]) == wsl2_mod._wsl_base_path() / "agw-dev" for args in backend.imports)


def test_wsl_failed_restore_retains_both_recovery_artifacts_for_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, "agw-checkpoint-1", ctx)
    backend.contents = b"pre-restore-state"
    backend.fail_import = True

    with pytest.raises(RuntimeError):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]

    checkpoint = wsl2_mod._checkpoint_root() / "agw-dev" / "agw-checkpoint-1.tar"
    emergency = wsl2_mod._checkpoint_root() / "agw-dev" / "agw-checkpoint-1.pre-restore.tar"
    assert checkpoint.read_bytes() == b"initial-rootfs"
    assert emergency.read_bytes() == b"pre-restore-state"

    backend.fail_import = False
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]
    assert backend.contents == b"initial-rootfs"
    assert len(backend.exports) == 2


def test_wsl_delete_removes_checkpoint_and_emergency_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, "agw-checkpoint-1", ctx)
    backend.contents = b"pre-restore-state"
    platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]

    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]
    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]

    assert platform.list_checkpoints(vm, ctx) == ()  # type: ignore[arg-type]
    assert not (wsl2_mod._checkpoint_root() / "agw-dev" / "agw-checkpoint-1.pre-restore.tar").exists()


def test_wsl_incomplete_create_remains_listed_until_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = CheckpointDescriptor(name="agw-checkpoint-1", identifier="wsl2:agw-dev:agw-checkpoint-1")
    checkpoint, _ = platform._checkpoint_paths(  # noqa: SLF001
        vm,  # type: ignore[arg-type]
        descriptor.name,
        create_dir=True,
    )
    partial = checkpoint.with_name(checkpoint.name + ".partial")
    partial.write_bytes(b"interrupted-export")

    assert platform.list_checkpoints(vm, ctx) == (descriptor,)  # type: ignore[arg-type]

    platform.delete_checkpoint(vm, descriptor, ctx)  # type: ignore[arg-type]

    assert platform.list_checkpoints(vm, ctx) == ()  # type: ignore[arg-type]
    assert not partial.exists()


def test_wsl_checkpoint_create_refuses_running_distro_before_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    _create_checkpoint(platform, _vm(), "agw-checkpoint-1", RunContext())
    backend.running = True

    with pytest.raises(StateError):
        _create_checkpoint(platform, _vm(), "agw-checkpoint-1", RunContext(), resume=True)

    assert len(backend.exports) == 1


def test_wsl_restore_rejects_foreign_identifier_before_destructive_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    _create_checkpoint(platform, vm, "agw-checkpoint-1", ctx)

    with pytest.raises(StateError):
        platform.restore_checkpoint(  # type: ignore[arg-type]
            vm,
            CheckpointDescriptor(name="agw-checkpoint-1", identifier="wsl2:other:agw-checkpoint-1"),
            ctx,
            operation_id="restore-1",
        )

    assert backend.registered
    assert backend.imports == []


def test_wsl_restore_proves_unregister_before_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    vm = _vm()
    ctx = RunContext()
    descriptor = _create_checkpoint(platform, vm, "agw-checkpoint-1", ctx)
    backend.fail_unregister = True

    with pytest.raises(StateError):
        platform.restore_checkpoint(vm, descriptor, ctx, operation_id="restore-1")  # type: ignore[arg-type]

    assert backend.registered
    assert backend.imports == []
    assert (wsl2_mod._checkpoint_root() / "agw-dev" / "agw-checkpoint-1.pre-restore.tar").exists()


def test_wsl_checkpoint_storage_rejects_symlinked_owned_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _WslBackend()
    platform = _platform(monkeypatch, tmp_path, backend)
    agentworks_dir = tmp_path / "agentworks"
    agentworks_dir.mkdir()
    (agentworks_dir / "checkpoints").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(StateError):
        platform.list_checkpoints(_vm(), RunContext())  # type: ignore[arg-type]
