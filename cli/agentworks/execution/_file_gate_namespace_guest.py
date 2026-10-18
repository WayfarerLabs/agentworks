"""Prepare one managed UID namespace with direct effects under the root gate."""

from __future__ import annotations

import errno
import os
import stat
import time

from . import _file_effect_gate as gate
from ._file_effect_gate_protocol import GateControlProtocolError
from ._file_gate_namespace_protocol import (
    MAX_REQUEST_BYTES,
    NamespaceRequest,
    NamespaceStatus,
    decode_namespace_request,
    encode_namespace_status,
)
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._vm_guest_identity_guest import _GuestRefusal, _identity

_RULE_DIRECTORY = "/etc/tmpfiles.d"
_RULE_GID = 0


def _no_acl(path: str | int) -> None:
    for name in (gate._ACL_ACCESS, gate._ACL_DEFAULT):
        try:
            os.getxattr(path, name)
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.EOPNOTSUPP):
                raise
        else:
            raise ValueError("namespace ACL is unsafe")


def _open_rule_directory() -> int:
    ancestor = ""
    for component in _RULE_DIRECTORY.split("/")[1:]:
        ancestor += "/" + component
        metadata = os.lstat(ancestor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != gate._ROOT_UID or metadata.st_mode & 0o022:
            raise ValueError("namespace rule anchor is unsafe")
        _no_acl(ancestor)
    return os.open(_RULE_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | gate._open_flags())


def _check_uid_directory(descriptor: int, uid: int, gid: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("namespace UID directory is unsafe")
    _no_acl(descriptor)


def _check_rule(parent: int, name: str, content: bytes) -> None:
    descriptor = os.open(name, os.O_RDONLY | gate._open_flags(), dir_fd=parent)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != gate._ROOT_UID
            or metadata.st_gid != _RULE_GID
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_size != len(content)
        ):
            raise ValueError("namespace rule is unsafe")
        _no_acl(descriptor)
        if os.read(descriptor, len(content) + 1) != content:
            raise ValueError("namespace rule content differs")
    finally:
        os.close(descriptor)


def _prepare_locked(request: NamespaceRequest, expires_at: float) -> NamespaceStatus:
    """All descriptors and owned temporary cleanup stay inside the caller's fence."""
    changed = False
    temporary_owned = False
    namespace = rules = uid_directory = temporary = None
    temporary_name = f".agentworks-file-gates-{request.uid}-{request.nonce}.tmp"
    rule_name = f"agentworks-file-gates-uid-{request.uid}.conf"
    content = f"d {gate._GATE_NAMESPACE}/{request.uid} 0700 {request.uid} {request.gid} - -\n".encode("ascii")
    status = NamespaceStatus.REFUSED
    try:
        gate._require_before_deadline(expires_at)
        rules = _open_rule_directory()
        namespace = os.open(gate._GATE_NAMESPACE, os.O_RDONLY | os.O_DIRECTORY | gate._open_flags())
        name = str(request.uid)
        try:
            uid_directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | gate._open_flags(), dir_fd=namespace)
        except FileNotFoundError:
            # Arm uncertainty before a syscall whose reply can be interrupted.
            changed = True
            os.mkdir(name, 0o700, dir_fd=namespace)
            uid_directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | gate._open_flags(), dir_fd=namespace)
            os.fchmod(uid_directory, 0o700)
            os.fchown(uid_directory, request.uid, request.gid)
        _check_uid_directory(uid_directory, request.uid, request.gid)
        gate._require_before_deadline(expires_at)
        try:
            _check_rule(rules, rule_name, content)
        except FileNotFoundError:
            changed = True
            temporary = os.open(
                temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | gate._open_flags(), 0o600, dir_fd=rules
            )
            temporary_owned = True
            offset = 0
            while offset < len(content):
                gate._require_before_deadline(expires_at)
                written = os.write(temporary, content[offset:])
                if written <= 0:
                    raise OSError("namespace rule write stalled") from None
                offset += written
            os.fchown(temporary, gate._ROOT_UID, _RULE_GID)
            os.fchmod(temporary, 0o644)
            _no_acl(temporary)
            os.fsync(temporary)
            gate._require_before_deadline(expires_at)
            os.link(temporary_name, rule_name, src_dir_fd=rules, dst_dir_fd=rules, follow_symlinks=False)
            os.unlink(temporary_name, dir_fd=rules)
            temporary_owned = False
            os.fsync(rules)
            _check_rule(rules, rule_name, content)
        gate._require_before_deadline(expires_at)
        status = NamespaceStatus.PREPARED
    except (OSError, ValueError):
        status = NamespaceStatus.UNCERTAIN if changed else NamespaceStatus.REFUSED
    finally:
        try:
            if temporary_owned:
                assert rules is not None and temporary is not None
                held = os.fstat(temporary)
                named = os.stat(temporary_name, dir_fd=rules, follow_symlinks=False)
                if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                    raise ValueError("namespace temporary inode changed")
                os.unlink(temporary_name, dir_fd=rules)
        except (OSError, ValueError):
            status = NamespaceStatus.UNCERTAIN
        finally:
            for descriptor in (temporary, uid_directory, namespace, rules):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        status = NamespaceStatus.UNCERTAIN
    return status


def _operate(request: NamespaceRequest, expires_at: float) -> NamespaceStatus:
    try:
        # Existing root anchors and the exact generation must be admitted before
        # opening a namespace or rule descriptor. This never bootstraps its fence.
        with gate.hold_file_effect_gate(request.binding, _identity, expires_at=expires_at):
            return _prepare_locked(request, expires_at)
    except (gate.FileEffectGateError, _GuestRefusal):
        return NamespaceStatus.REFUSED


def main(nonce: str) -> int:
    writer = FileRecordWriter(nonce)
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    status = NamespaceStatus.REFUSED
    try:
        request = decode_namespace_request(bytes(data))
        if request.nonce == nonce and matches_current_identity(request.identity):
            status = _operate(request, time.monotonic() + request.remaining_seconds)
    except GateControlProtocolError:
        pass
    # The gate has been released. Reporting performs no filesystem effects.
    writer.write(FileRecordKind.RESULT, encode_namespace_status(status))
    writer.write(FileRecordKind.FINISHED, b"{}")
    return 0
