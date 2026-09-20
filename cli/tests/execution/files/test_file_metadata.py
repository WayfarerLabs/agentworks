"""Linux held-object metadata convergence and directory creation."""

from __future__ import annotations

import errno
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

import agentworks.execution._file_metadata as metadata_module
from agentworks.execution._file_metadata import (
    MetadataEffect,
    MetadataError,
    MetadataFailureKind,
    MetadataPhase,
    MetadataResult,
    MetadataStep,
    ensure_directory,
    set_metadata,
)
from agentworks.execution._file_objects import FileObjectPhase, _ObservedObject, _open_observed

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file metadata")


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _set(parent_fd: int, name: str, mode: int) -> MetadataResult:
    return set_metadata(
        parent_fd,
        name,
        uid=os.getuid(),
        gid=os.getgid(),
        mode=mode,
        expires_at=None,
    )


def _ensure(parent_fd: int, name: str, mode: int) -> MetadataResult:
    return ensure_directory(
        parent_fd,
        name,
        uid=os.getuid(),
        gid=os.getgid(),
        mode=mode,
        expires_at=None,
    )


def _failure(function: object, *args: object, **kwargs: object) -> MetadataError:
    with pytest.raises(MetadataError) as raised:
        function(*args, **kwargs)  # type: ignore[operator]
    return raised.value


def _acl(path: Path, name: str = "system.posix_acl_access") -> bytes:
    return os.getxattr(path, name)


def _require_setfacl() -> str:
    command = shutil.which("setfacl")
    if command is None:
        pytest.skip("setfacl is required to provision the ACL fixture")
    return command


def _expire_during_final_acl_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    original_getxattr = os.getxattr
    clock = [0.0]
    reads = 0

    def advancing_getxattr(path: str, attribute: str) -> bytes:
        nonlocal reads
        try:
            return original_getxattr(path, attribute)
        finally:
            reads += 1
            if reads == 2:
                clock[0] = 2.0

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(os, "getxattr", advancing_getxattr)


def test_regular_mode_zero_converges_without_content_access(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"private")
    target.chmod(0)
    parent_fd = _open_parent(tmp_path)
    try:
        result = _set(parent_fd, target.name, 0o600)
        observed = target.stat()
        content = target.read_bytes()
    finally:
        os.close(parent_fd)
        target.chmod(0o600)

    assert result.changed
    assert result.revision.stat.inode == observed.st_ino
    assert stat.S_IMODE(observed.st_mode) == 0o600
    assert content == b"private"
    assert "changed" not in repr(result)
    assert "revision" not in repr(result)


def test_converged_file_and_directory_are_unchanged(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"content")
    regular.chmod(0o640)
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o750)
    parent_fd = _open_parent(tmp_path)
    try:
        regular_result = _set(parent_fd, regular.name, 0o640)
        directory_result = _ensure(parent_fd, directory.name, 0o750)
    finally:
        os.close(parent_fd)

    assert not regular_result.changed
    assert not directory_result.changed


def test_non_linux_refuses_before_parent_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    os.close(parent_fd)
    monkeypatch.setattr(sys, "platform", "win32")

    error = _failure(_set, parent_fd, "target", 0o600)

    assert error.kind is MetadataFailureKind.UNSUPPORTED
    assert error.effect is MetadataEffect.UNCHANGED


@pytest.mark.parametrize("mode", [0o2770, 0o2771, 0o3770])
def test_directory_creation_supports_required_setgid_and_sticky_modes(tmp_path: Path, mode: int) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        result = _ensure(parent_fd, "created", mode)
    finally:
        os.close(parent_fd)

    assert result.changed
    assert stat.S_IMODE((tmp_path / "created").stat().st_mode) == mode


def test_regular_file_supports_all_ordinary_permission_bits(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    try:
        _set(parent_fd, target.name, 0o777)
    finally:
        os.close(parent_fd)

    assert stat.S_IMODE(target.stat().st_mode) == 0o777


def test_creation_uses_exact_final_component_and_restrictive_initial_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    original_mkdir = os.mkdir
    calls: list[tuple[str, int, int | None]] = []

    def recording_mkdir(path: str, mode: int, *, dir_fd: int | None = None) -> None:
        calls.append((path, mode, dir_fd))
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", recording_mkdir)
    try:
        _ensure(parent_fd, "final", 0o755)
    finally:
        os.close(parent_fd)

    assert calls == [("final", 0o700, parent_fd)]
    assert {path.name for path in tmp_path.iterdir()} == {"final"}


def test_metadata_does_not_recurse_into_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    child = target / "child"
    child.write_bytes(b"content")
    child.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    try:
        _set(parent_fd, target.name, 0o755)
    finally:
        os.close(parent_fd)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert stat.S_IMODE(child.stat().st_mode) == 0o600


def test_ensure_existing_regular_file_conflicts_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(_ensure, parent_fd, target.name, 0o755)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.CONFLICT
    assert error.effect is MetadataEffect.UNCHANGED
    assert target.read_bytes() == b"content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_proc_bridge_mutates_retained_inode_after_named_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target.chmod(0o600)
    old_inode = target.stat().st_ino
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    replacement.chmod(0o644)
    parent_fd = _open_parent(tmp_path)
    original_chmod = os.chmod

    def replacing_chmod(path: str, mode: int) -> None:
        target.rename(tmp_path / "old-target")
        replacement.rename(target)
        original_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", replacing_chmod)
    try:
        error = _failure(_set, parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    retained = tmp_path / "old-target"
    assert retained.stat().st_ino == old_inode
    assert stat.S_IMODE(retained.stat().st_mode) == 0o640
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert error.kind is MetadataFailureKind.CONFLICT
    assert error.effect is MetadataEffect.PARTIAL
    assert error.completed_steps == (MetadataStep.MODE,)


def test_replacement_seen_before_mutation_refuses_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    target.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    parent_fd = _open_parent(tmp_path)
    original_verify = metadata_module._verify_target
    calls = 0

    def replacing_verify(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.replace(replacement, target)
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(metadata_module, "_verify_target", replacing_verify)
    try:
        error = _failure(_set, parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.CONFLICT
    assert error.effect is MetadataEffect.UNCHANGED
    assert error.completed_steps == ()
    assert target.read_bytes() == b"new"


def test_expired_deadline_refuses_before_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(metadata_module, "_open_observed", lambda *_args: pytest.fail("object was observed"))
    try:
        error = _failure(
            set_metadata,
            parent_fd,
            "target",
            uid=os.getuid(),
            gid=os.getgid(),
            mode=0o600,
            expires_at=0.0,
        )
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.DEADLINE
    assert error.effect is MetadataEffect.UNCHANGED


def test_deadline_after_creation_reports_known_partial_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)

    def expired_convergence(*args: object) -> MetadataResult:
        state = args[-1]
        assert isinstance(state, metadata_module._MutationState)
        metadata_module._check_deadline(0.0, state)
        pytest.fail("deadline check returned")

    monkeypatch.setattr(metadata_module, "_converge", expired_convergence)
    try:
        error = _failure(_ensure, parent_fd, "created", 0o755)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.DEADLINE
    assert error.effect is MetadataEffect.PARTIAL
    assert error.completed_steps == (MetadataStep.CREATION,)
    assert (tmp_path / "created").is_dir()


def test_deadline_during_final_acl_observation_reports_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    _expire_during_final_acl_observation(monkeypatch)
    try:
        error = _failure(
            set_metadata,
            parent_fd,
            target.name,
            uid=os.getuid(),
            gid=os.getgid(),
            mode=0o600,
            expires_at=1.0,
        )
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.DEADLINE
    assert error.phase is MetadataPhase.VERIFICATION
    assert error.effect is MetadataEffect.UNCHANGED
    assert error.completed_steps == ()
    assert error.attempted_step is None


def test_deadline_during_final_acl_observation_reports_completed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    _expire_during_final_acl_observation(monkeypatch)
    try:
        error = _failure(
            set_metadata,
            parent_fd,
            target.name,
            uid=os.getuid(),
            gid=os.getgid(),
            mode=0o640,
            expires_at=1.0,
        )
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.DEADLINE
    assert error.phase is MetadataPhase.VERIFICATION
    assert error.effect is MetadataEffect.PARTIAL
    assert error.completed_steps == (MetadataStep.MODE,)
    assert error.attempted_step is None
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_failed_mode_syscall_reports_uncertain_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)

    def fail_chmod(_path: str, _mode: int) -> None:
        raise OSError(errno.EIO, "fixture")

    monkeypatch.setattr(os, "chmod", fail_chmod)
    try:
        error = _failure(_set, parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.METADATA
    assert error.phase is MetadataPhase.MODE
    assert error.effect is MetadataEffect.UNCERTAIN
    assert error.attempted_step is MetadataStep.MODE
    assert error.completed_steps == ()


def test_failed_mode_after_creation_records_completed_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(os, "chmod", lambda *_args: (_ for _ in ()).throw(OSError(errno.EIO, "fixture")))
    try:
        error = _failure(_ensure, parent_fd, "created", 0o755)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.METADATA
    assert error.effect is MetadataEffect.UNCERTAIN
    assert error.completed_steps == (MetadataStep.CREATION,)
    assert error.attempted_step is MetadataStep.MODE
    assert (tmp_path / "created").is_dir()


def test_control_interruption_preserves_uncertain_safe_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(os, "chmod", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt) as raised:
        _set(parent_fd, target.name, 0o640)
    os.close(parent_fd)

    assert isinstance(raised.value.__cause__, MetadataError)
    assert raised.value.__cause__.effect is MetadataEffect.UNCERTAIN
    assert raised.value.__cause__.attempted_step is MetadataStep.MODE


def test_successful_syscall_that_cannot_establish_mode_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(os, "chmod", lambda *_args: None)
    try:
        error = _failure(_set, parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.METADATA
    assert error.effect is MetadataEffect.PARTIAL
    assert error.completed_steps == (MetadataStep.MODE,)
    assert error.attempted_step is None


def test_unchanged_ownership_is_not_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(os, "chown", lambda *_args: pytest.fail("unchanged ownership was applied"))
    try:
        _set(parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_ownership_precedes_mode_reapplication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o2770)
    parent_fd = _open_parent(tmp_path)
    original_verify = metadata_module._verify_target
    original_chown = os.chown
    original_chmod = os.chmod
    verify_calls = 0
    events: list[MetadataStep] = []

    def mismatched_first_owner(*args: object, **kwargs: object) -> tuple[object, str]:
        nonlocal verify_calls
        verify_calls += 1
        revision, bridge = original_verify(*args, **kwargs)  # type: ignore[arg-type]
        if verify_calls == 1:
            revision = replace(revision, stat=replace(revision.stat, uid=os.getuid() + 1))
        return revision, bridge

    def clearing_chown(path: str, uid: int, gid: int) -> None:
        events.append(MetadataStep.OWNERSHIP)
        original_chown(path, uid, gid)
        original_chmod(path, 0o770)

    def recording_chmod(path: str, mode: int) -> None:
        events.append(MetadataStep.MODE)
        original_chmod(path, mode)

    monkeypatch.setattr(metadata_module, "_verify_target", mismatched_first_owner)
    monkeypatch.setattr(os, "chown", clearing_chown)
    monkeypatch.setattr(os, "chmod", recording_chmod)
    try:
        result = _set(parent_fd, target.name, 0o2770)
    finally:
        os.close(parent_fd)

    assert result.changed
    assert events == [MetadataStep.OWNERSHIP, MetadataStep.MODE]
    assert stat.S_IMODE(target.stat().st_mode) == 0o2770


def test_missing_proc_bridge_refuses_without_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    original_stat = os.stat

    def missing_proc(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "fixture")
        return original_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", missing_proc)
    try:
        error = _failure(_set, parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    assert error.kind is MetadataFailureKind.UNSUPPORTED
    assert error.effect is MetadataEffect.UNCHANGED
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_existing_access_acl_matches_direct_chmod_semantics(tmp_path: Path) -> None:
    setfacl = _require_setfacl()
    target = tmp_path / "target"
    control = tmp_path / "control"
    target.write_bytes(b"content")
    control.write_bytes(b"content")
    configured = subprocess.run(
        [setfacl, "-m", f"g:{os.getgid()}:r--", str(target), str(control)],
        check=False,
        capture_output=True,
    )
    assert configured.returncode == 0, configured.stderr.decode(errors="replace")
    os.chown(control, os.getuid(), os.getgid())
    os.chmod(control, 0o660)
    parent_fd = _open_parent(tmp_path)
    try:
        _set(parent_fd, target.name, 0o660)
    finally:
        os.close(parent_fd)

    assert _acl(target) == _acl(control)
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(control.stat().st_mode) == 0o660


def test_created_directory_acl_matches_direct_operations(tmp_path: Path) -> None:
    setfacl = _require_setfacl()
    direct_parent = tmp_path / "direct-parent"
    helper_parent = tmp_path / "helper-parent"
    direct_parent.mkdir()
    helper_parent.mkdir()
    acl_spec = f"u::rwx,g::r-x,g:{os.getgid()}:rw-,m::rwx,o::---"
    for parent in (direct_parent, helper_parent):
        configured = subprocess.run(
            [setfacl, "-d", "-m", acl_spec, str(parent)],
            check=False,
            capture_output=True,
        )
        assert configured.returncode == 0, configured.stderr.decode(errors="replace")

    os.mkdir(direct_parent / "created", 0o700)
    os.chown(direct_parent / "created", os.getuid(), os.getgid())
    os.chmod(direct_parent / "created", 0o2770)
    parent_fd = _open_parent(helper_parent)
    try:
        _ensure(parent_fd, "created", 0o2770)
    finally:
        os.close(parent_fd)

    direct = direct_parent / "created"
    created = helper_parent / "created"
    assert _acl(created) == _acl(direct)
    assert _acl(created, "system.posix_acl_default") == _acl(direct, "system.posix_acl_default")
    assert stat.S_IMODE(created.stat().st_mode) == stat.S_IMODE(direct.stat().st_mode) == 0o2770


def test_ordinary_extended_attribute_is_preserved(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    try:
        os.setxattr(target, "user.agentworks-metadata-test", b"keep")
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("the test filesystem does not support user xattrs")
        raise
    parent_fd = _open_parent(tmp_path)
    try:
        _set(parent_fd, target.name, 0o640)
    finally:
        os.close(parent_fd)

    assert os.getxattr(target, "user.agentworks-metadata-test") == b"keep"


@pytest.mark.parametrize("object_kind", ["symlink", "hardlink", "fifo", "socket"])
def test_metadata_refuses_links_and_special_objects(tmp_path: Path, object_kind: str) -> None:
    target = tmp_path / "target"
    listener: socket.socket | None = None
    parent_fd = _open_parent(tmp_path)
    if object_kind == "symlink":
        other = tmp_path / "other"
        other.write_bytes(b"content")
        target.symlink_to(other)
    elif object_kind == "hardlink":
        target.write_bytes(b"content")
        os.link(target, tmp_path / "other-link")
    elif object_kind == "fifo":
        os.mkfifo(target)
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(f"/proc/self/fd/{parent_fd}/{target.name}")
    try:
        error = _failure(_set, parent_fd, target.name, 0o600)
    finally:
        os.close(parent_fd)
        if listener is not None:
            listener.close()

    assert error.kind is MetadataFailureKind.UNSUPPORTED
    assert error.effect is MetadataEffect.UNCHANGED


def test_mode_authority_refuses_setuid_and_regular_special_bits(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"content")
    directory = tmp_path / "directory"
    directory.mkdir()
    parent_fd = _open_parent(tmp_path)
    try:
        regular_error = _failure(_set, parent_fd, regular.name, 0o1777)
        directory_error = _failure(_set, parent_fd, directory.name, 0o4770)
    finally:
        os.close(parent_fd)

    assert regular_error.kind is MetadataFailureKind.UNSUPPORTED
    assert directory_error.kind is MetadataFailureKind.UNSUPPORTED
    assert regular_error.effect is directory_error.effect is MetadataEffect.UNCHANGED


def test_invalid_directory_mode_refuses_before_observation_or_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _open_parent(tmp_path)
    os.close(parent_fd)
    monkeypatch.setattr(metadata_module, "_open_observed", lambda *_args: pytest.fail("object was observed"))

    error = _failure(_ensure, parent_fd, "missing", 0o4770)

    assert error.kind is MetadataFailureKind.UNSUPPORTED
    assert error.effect is MetadataEffect.UNCHANGED
    assert error.completed_steps == ()
    assert error.attempted_step is None
    assert not (tmp_path / "missing").exists()


def test_observation_descriptor_is_closed_on_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    descriptors: list[int] = []

    def recording_open(
        object_parent_fd: int,
        leaf_name: str,
        phase: FileObjectPhase,
        expires_at: float | None,
    ) -> _ObservedObject | None:
        observed = _open_observed(object_parent_fd, leaf_name, phase, expires_at)
        if observed is not None:
            descriptors.append(observed.descriptor)
        return observed

    monkeypatch.setattr(metadata_module, "_open_observed", recording_open)
    _set(parent_fd, target.name, 0o640)
    _failure(_set, parent_fd, "missing", 0o600)
    os.close(parent_fd)

    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError) as raised:
            os.fstat(descriptor)
        assert raised.value.errno == errno.EBADF


def test_close_interruption_preserves_completed_effect_as_safe_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    original_close = os.close

    def close_then_interrupt(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor != parent_fd:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "close", close_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        _set(parent_fd, target.name, 0o640)
    monkeypatch.setattr(os, "close", original_close)
    os.close(parent_fd)

    assert isinstance(raised.value.__cause__, MetadataError)
    assert raised.value.__cause__.effect is MetadataEffect.PARTIAL
    assert raised.value.__cause__.completed_steps == (MetadataStep.MODE,)


def test_completed_ownership_step_is_reported_on_later_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    parent_fd = _open_parent(tmp_path)
    original_verify = metadata_module._verify_target
    original_acl = metadata_module._verify_access_acl
    verify_calls = 0
    acl_calls = 0

    def mismatched_first_owner(*args: object, **kwargs: object) -> tuple[object, str]:
        nonlocal verify_calls
        verify_calls += 1
        revision, bridge = original_verify(*args, **kwargs)  # type: ignore[arg-type]
        if verify_calls == 1:
            revision = replace(revision, stat=replace(revision.stat, uid=os.getuid() + 1))
        return revision, bridge

    def refuse_after_owner(*args: object) -> None:
        nonlocal acl_calls
        acl_calls += 1
        if acl_calls == 2:
            state = args[-1]
            assert isinstance(state, metadata_module._MutationState)
            raise metadata_module._error(MetadataFailureKind.METADATA, state)
        original_acl(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(metadata_module, "_verify_target", mismatched_first_owner)
    monkeypatch.setattr(metadata_module, "_verify_access_acl", refuse_after_owner)
    try:
        error = _failure(_set, parent_fd, target.name, 0o600)
    finally:
        os.close(parent_fd)

    assert error.effect is MetadataEffect.PARTIAL
    assert error.completed_steps == (MetadataStep.OWNERSHIP,)
