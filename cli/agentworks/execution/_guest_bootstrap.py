"""Fixed Linux root entry that drops credentials before loading a helper."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from typing import Any

_INIT_PATH = "/proc/1/stat"
_STATUS_PATH = "/proc/self/status"
_INIT_LIMIT = 8192
_STATUS_LIMIT = 16384
_REFUSAL = 125


class _BootstrapRefusal(Exception):
    """A fixed admission step failed before helper source was loaded."""


def _read_init(descriptor: int) -> bytes:
    """Read a fresh, bounded PID 1 stat snapshot from the held descriptor."""
    return os.pread(descriptor, _INIT_LIMIT + 1, 0)


def _capabilities_zero(*, non_root: bool) -> bool:
    names = ("CapInh", "CapPrm", "CapEff", "CapAmb") if non_root else ("CapInh", "CapAmb")
    descriptor = os.open(_STATUS_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        content = os.read(descriptor, _STATUS_LIMIT + 1)
    finally:
        os.close(descriptor)
    if len(content) > _STATUS_LIMIT:
        return False
    observed: dict[str, int] = {}
    for line in content.splitlines():
        name, separator, value = line.partition(b":")
        key = name.decode("ascii", "ignore")
        if separator and key in names:
            stripped = value.strip()
            if key in observed or not stripped or any(byte not in b"0123456789abcdefABCDEF" for byte in stripped):
                return False
            observed[key] = int(stripped, 16)
    return set(observed) == set(names) and all(value == 0 for value in observed.values())


def _drop_and_verify(uid: int, gid: int, groups: tuple[int, ...]) -> None:
    os.setgroups(list(groups))
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    if (
        os.getresuid() != (uid, uid, uid)
        or os.getresgid() != (gid, gid, gid)
        or tuple(sorted(set(os.getgroups()) | {os.getegid()})) != groups
        or not _capabilities_zero(non_root=uid != 0)
    ):
        raise ValueError("credential verification failed")


def _run(
    uid: int,
    gid: int,
    groups: tuple[int, ...],
    loader_source: str,
    expected_guest: tuple[str, str, int],
) -> int:
    """Verify root, open the sole privileged descriptor, then drop credentials."""
    if os.getresuid() != (0, 0, 0):
        raise _BootstrapRefusal
    try:
        descriptor = os.open(_INIT_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise _BootstrapRefusal from None
    try:
        try:
            # The descriptor stays with this trusted helper; command/script
            # exec closes it. Helper-internal forks are not a security boundary.
            os.set_inheritable(descriptor, False)
        except OSError:
            raise _BootstrapRefusal from None

        def read_init() -> bytes:
            return _read_init(descriptor)

        try:
            _drop_and_verify(uid, gid, groups)
        except (OSError, ValueError, RuntimeError):
            raise _BootstrapRefusal from None
        scope: dict[str, Any] = {
            "__name__": "__main__",
        }
        try:
            exec(compile(loader_source, "<agentworks-fixed-loader>", "exec"), scope)
            scope["_agw_load_identity"]()
            guest = sys.modules[scope["_agw_guest_module"]]
            guest._bind_init_reader(read_init)
            identity = guest._identity()
            if (identity.instance_marker, identity.boot_id, identity.init_start_ticks) != expected_guest:
                raise _BootstrapRefusal
        except BaseException:
            raise _BootstrapRefusal from None
        scope["_agw_load_remaining"]()
        return scope["_agw_enter_body"]()
    finally:
        with suppress(OSError):
            os.close(descriptor)


def main(
    uid: int,
    gid: int,
    groups: tuple[int, ...],
    loader_source: str,
    expected_guest: tuple[str, str, int],
) -> int:
    """Refuse closed on privileged admission failures without echoing input."""
    if sys.platform != "linux":
        return _REFUSAL
    try:
        return _run(uid, gid, groups, loader_source, expected_guest)
    except _BootstrapRefusal:
        return _REFUSAL
