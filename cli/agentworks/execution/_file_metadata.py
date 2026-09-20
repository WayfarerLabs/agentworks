"""Private Linux metadata convergence beneath a borrowed parent descriptor.

The caller owns validated numeric inputs, the protected parent descriptor, and
the cooperating-writer lock. Mutations target only a verified path-only handle
through procfs, never the mutable caller path.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import NoReturn

from ._file_objects import (
    FileKind,
    FileObjectError,
    FileObjectFailureKind,
    FileObjectPhase,
    _ObservedObject,
    _open_observed,
    _verify_observed,
)
from ._file_snapshot import _snapshot_stat
from ._file_stat import FileRevision

_ACCESS_ACL = "system.posix_acl_access"
_ACL_VERSION = 2
_ACL_USER_OBJ = 0x01
_ACL_USER = 0x02
_ACL_GROUP_OBJ = 0x04
_ACL_GROUP = 0x08
_ACL_MASK = 0x10
_ACL_OTHER = 0x20


class MetadataFailureKind(Enum):
    """Closed metadata failures that reveal no filesystem input."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    METADATA = "metadata"
    IO = "io"


class MetadataPhase(Enum):
    """The fixed metadata operation in which work stopped."""

    OBSERVATION = "observation"
    CREATION = "creation"
    OWNERSHIP = "ownership"
    MODE = "mode"
    VERIFICATION = "verification"


class MetadataEffect(Enum):
    """What is safely known about the target after a failed operation."""

    UNCHANGED = "unchanged"
    PARTIAL = "partial"
    UNCERTAIN = "uncertain"


class MetadataStep(StrEnum):
    """A target mutation confirmed complete by the helper."""

    CREATION = "creation"
    OWNERSHIP = "ownership"
    MODE = "mode"


class MetadataError(Exception):
    """A closed failure with bounded facts about in-place effects."""

    def __init__(
        self,
        kind: MetadataFailureKind,
        phase: MetadataPhase,
        *,
        completed_steps: tuple[MetadataStep, ...] = (),
        attempted_step: MetadataStep | None = None,
    ) -> None:
        self.kind = kind
        self.phase = phase
        self.completed_steps = completed_steps
        self.attempted_step = attempted_step
        if attempted_step is not None:
            self.effect = MetadataEffect.UNCERTAIN
        elif completed_steps:
            self.effect = MetadataEffect.PARTIAL
        else:
            self.effect = MetadataEffect.UNCHANGED
        super().__init__(
            kind.value,
            phase.value,
            self.effect.value,
            tuple(step.value for step in completed_steps),
            None if attempted_step is None else attempted_step.value,
        )


@dataclass(frozen=True, slots=True, repr=False)
class MetadataResult:
    """Confirmed convergence result and metadata-only revision evidence."""

    changed: bool
    revision: FileRevision


@dataclass(slots=True, repr=False)
class _MutationState:
    phase: MetadataPhase = MetadataPhase.OBSERVATION
    completed_steps: list[MetadataStep] = field(default_factory=list)
    attempted_step: MetadataStep | None = None


def set_metadata(
    parent_fd: int,
    leaf_name: str,
    *,
    uid: int,
    gid: int,
    mode: int,
    expires_at: float | None,
) -> MetadataResult:
    """Converge one existing regular file or directory on its held inode."""
    return _run_metadata(parent_fd, leaf_name, uid, gid, mode, expires_at, create=False)


def ensure_directory(
    parent_fd: int,
    leaf_name: str,
    *,
    uid: int,
    gid: int,
    mode: int,
    expires_at: float | None,
) -> MetadataResult:
    """Create at most the final directory component, then converge metadata."""
    return _run_metadata(parent_fd, leaf_name, uid, gid, mode, expires_at, create=True)


def _run_metadata(
    parent_fd: int,
    leaf_name: str,
    uid: int,
    gid: int,
    mode: int,
    expires_at: float | None,
    *,
    create: bool,
) -> MetadataResult:
    state = _MutationState()
    observed: _ObservedObject | None = None
    created_revision: FileRevision | None = None
    failure: MetadataError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    result: MetadataResult | None = None
    try:
        try:
            _require_linux(state)
            _check_deadline(expires_at, state)
            if create:
                _require_metadata_kind(FileKind.DIRECTORY, mode, state)
            observed = _open_observed(parent_fd, leaf_name, FileObjectPhase.CONDITION, expires_at)
            if observed is None:
                if not create:
                    raise _error(MetadataFailureKind.CONFLICT, state)
                created_revision = _create_directory(parent_fd, leaf_name, state)
                observed = _open_observed(parent_fd, leaf_name, FileObjectPhase.CONDITION, expires_at)
                if observed is None or observed.revision.stat != created_revision.stat:
                    raise _error(MetadataFailureKind.CONFLICT, state)
            elif create and observed.kind is not FileKind.DIRECTORY:
                kind = (
                    MetadataFailureKind.UNSUPPORTED
                    if observed.kind is FileKind.SOCKET
                    else MetadataFailureKind.CONFLICT
                )
                raise _error(kind, state)
            if not create:
                _require_metadata_kind(observed.kind, mode, state)
            result = _converge(
                parent_fd,
                leaf_name,
                observed,
                uid,
                gid,
                mode,
                expires_at,
                state,
            )
        except MetadataError as error:
            failure = error
        except FileObjectError as error:
            failure = _object_error(error, state)
        except BaseException as error:
            control = error
    finally:
        if observed is not None:
            try:
                os.close(observed.descriptor)
            except OSError:
                pass
            except BaseException as error:
                close_control = error

    if close_control is not None:
        control = close_control
    if control is not None:
        _raise_control(control, failure or _error(MetadataFailureKind.IO, state))
    if failure is not None:
        raise failure
    assert result is not None
    return result


def _create_directory(
    parent_fd: int,
    leaf_name: str,
    state: _MutationState,
) -> FileRevision:
    state.phase = MetadataPhase.CREATION
    state.attempted_step = MetadataStep.CREATION
    try:
        os.mkdir(leaf_name, 0o700, dir_fd=parent_fd)
    except OSError as error:
        if error.errno == errno.EEXIST:
            state.attempted_step = None
            raise _error(MetadataFailureKind.CONFLICT, state) from None
        raise _error(MetadataFailureKind.IO, state) from None
    state.completed_steps.append(MetadataStep.CREATION)
    state.attempted_step = None
    try:
        created = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        parent = os.fstat(parent_fd)
    except OSError:
        raise _error(MetadataFailureKind.IO, state) from None
    if not stat.S_ISDIR(created.st_mode) or created.st_dev != parent.st_dev:
        raise _error(MetadataFailureKind.CONFLICT, state)
    return FileRevision(_snapshot_stat(created))


def _converge(
    parent_fd: int,
    leaf_name: str,
    observed: _ObservedObject,
    uid: int,
    gid: int,
    mode: int,
    expires_at: float | None,
    state: _MutationState,
) -> MetadataResult:
    current, bridge = _verify_target(parent_fd, leaf_name, observed, expires_at, state)
    _verify_access_acl(bridge, current, state)

    if (current.stat.uid, current.stat.gid) != (uid, gid):
        state.phase = MetadataPhase.OWNERSHIP
        current, bridge = _verify_target(parent_fd, leaf_name, observed, expires_at, state)
        _check_deadline(expires_at, state)
        state.attempted_step = MetadataStep.OWNERSHIP
        try:
            os.chown(bridge, uid, gid)
        except OSError:
            raise _error(MetadataFailureKind.METADATA, state) from None
        state.completed_steps.append(MetadataStep.OWNERSHIP)
        state.attempted_step = None
        current, bridge = _verify_target(parent_fd, leaf_name, observed, expires_at, state)
        _verify_access_acl(bridge, current, state)
        if (current.stat.uid, current.stat.gid) != (uid, gid):
            raise _error(MetadataFailureKind.METADATA, state)

    if stat.S_IMODE(current.stat.mode) != mode:
        state.phase = MetadataPhase.MODE
        current, bridge = _verify_target(parent_fd, leaf_name, observed, expires_at, state)
        _check_deadline(expires_at, state)
        state.attempted_step = MetadataStep.MODE
        try:
            os.chmod(bridge, mode)
        except OSError:
            raise _error(MetadataFailureKind.METADATA, state) from None
        state.completed_steps.append(MetadataStep.MODE)
        state.attempted_step = None

    state.phase = MetadataPhase.VERIFICATION
    _check_deadline(expires_at, state)
    current, bridge = _verify_target(parent_fd, leaf_name, observed, expires_at, state)
    _verify_access_acl(bridge, current, state)
    if (current.stat.uid, current.stat.gid) != (uid, gid) or stat.S_IMODE(current.stat.mode) != mode:
        raise _error(MetadataFailureKind.METADATA, state)
    return MetadataResult(bool(state.completed_steps), current)


def _require_metadata_kind(kind: FileKind, mode: int, state: _MutationState) -> None:
    if kind is FileKind.SOCKET:
        raise _error(MetadataFailureKind.UNSUPPORTED, state)
    invalid = bool(mode & ~0o777) if kind is FileKind.REGULAR else bool(mode & ~0o3777 or mode & stat.S_ISUID)
    if invalid:
        raise _error(MetadataFailureKind.UNSUPPORTED, state)


def _verify_target(
    parent_fd: int,
    leaf_name: str,
    observed: _ObservedObject,
    expires_at: float | None,
    state: _MutationState,
) -> tuple[FileRevision, str]:
    revision = _verify_observed(
        parent_fd,
        leaf_name,
        observed,
        FileObjectPhase.CONDITION,
        expires_at,
    )
    bridge = f"/proc/self/fd/{observed.descriptor}"
    try:
        bridged = os.stat(bridge)
    except OSError as error:
        kind = (
            MetadataFailureKind.UNSUPPORTED
            if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}
            else MetadataFailureKind.IO
        )
        raise _error(kind, state) from None
    if _snapshot_stat(bridged) != revision.stat:
        raise _error(MetadataFailureKind.CONFLICT, state)
    return revision, bridge


def _verify_access_acl(
    bridge: str,
    revision: FileRevision,
    state: _MutationState,
) -> None:
    try:
        access_acl = os.getxattr(bridge, _ACCESS_ACL)
    except OSError as error:
        if error.errno in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}:
            return
        kind = (
            MetadataFailureKind.UNSUPPORTED
            if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
            else MetadataFailureKind.IO
        )
        raise _error(kind, state) from None
    mode = stat.S_IMODE(revision.stat.mode)
    if _acl_mode(access_acl) != mode & 0o777:
        raise _error(MetadataFailureKind.METADATA, state)


def _acl_mode(access_acl: bytes) -> int | None:
    if len(access_acl) < 4 or (len(access_acl) - 4) % 8 or int.from_bytes(access_acl[:4], "little") != _ACL_VERSION:
        return None
    permissions: dict[int, int] = {}
    named = False
    for offset in range(4, len(access_acl), 8):
        tag = int.from_bytes(access_acl[offset : offset + 2], "little")
        permission = int.from_bytes(access_acl[offset + 2 : offset + 4], "little")
        if permission & ~0o7:
            return None
        if tag in {_ACL_USER, _ACL_GROUP}:
            named = True
        elif tag in permissions or tag not in {_ACL_USER_OBJ, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER}:
            return None
        else:
            permissions[tag] = permission
    if not {_ACL_USER_OBJ, _ACL_GROUP_OBJ, _ACL_OTHER} <= permissions.keys():
        return None
    if named and _ACL_MASK not in permissions:
        return None
    group = permissions.get(_ACL_MASK, permissions[_ACL_GROUP_OBJ])
    return permissions[_ACL_USER_OBJ] << 6 | group << 3 | permissions[_ACL_OTHER]


def _object_error(error: FileObjectError, state: _MutationState) -> MetadataError:
    if error.kind is FileObjectFailureKind.UNSUPPORTED:
        kind = MetadataFailureKind.UNSUPPORTED
    elif error.kind is FileObjectFailureKind.CONFLICT:
        kind = MetadataFailureKind.CONFLICT
    elif error.kind is FileObjectFailureKind.DEADLINE:
        kind = MetadataFailureKind.DEADLINE
    else:
        kind = MetadataFailureKind.IO
    return _error(kind, state)


def _error(kind: MetadataFailureKind, state: _MutationState) -> MetadataError:
    return MetadataError(
        kind,
        state.phase,
        completed_steps=tuple(state.completed_steps),
        attempted_step=state.attempted_step,
    )


def _require_linux(state: _MutationState) -> None:
    if sys.platform != "linux":
        raise _error(MetadataFailureKind.UNSUPPORTED, state)


def _check_deadline(expires_at: float | None, state: _MutationState) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise _error(MetadataFailureKind.DEADLINE, state)


def _raise_control(control: BaseException, fact: MetadataError) -> NoReturn:
    raise control from fact
