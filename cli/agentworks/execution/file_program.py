"""The guest file program: every file operation core performs on a managed VM.

``files.Files`` sends this file's source as a ``PythonProgram`` whose arguments
are the operation's name and one JSON object of parameters. Content travels on
stdin, never as an argument, and so do content digests (``SEALED``): stdin
starts with one JSON line of them, then the content. ``write`` replaces a file
whatever it holds; every other mutation is conditional on what its caller
observed. The operation log records
arguments, so neither ever reaches it. The exit status is the answer (the
``EXIT_*`` constants). stdout carries ``read``'s bytes or ``stat``'s one JSON line and
nothing else. stderr carries a traceback for status 1.

Every path component is opened relative to its parent without following links,
so nothing is read or written through a symbolic link anywhere on the path. Every
mutating operation holds one VM-wide lock (``LOCK_PATH``) from its walk to its
last system call; ``read`` and ``stat`` take none, since a rename replaces a file
whole. Writers that bypass the lock are out of scope: the program is consistent
among Agentworks operations, not a defense against a hostile account.

Plain syntax and the standard library only, as for agw-launch: the guest's
python3 may be any release a supported Debian ships. It has no import-time effect,
and the modules that exist only on POSIX are imported inside ``main``, so the
workstation can import it anywhere.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

LOCK_PATH = "/run/lock/agentworks-files.lock"
"""The one lock file every mutating operation of every identity takes."""

LOCK_WAIT_SECONDS = 30.0
"""How long a mutating operation waits for the lock before ``EXIT_LOCK_TIMEOUT``."""

EXIT_CONFLICT = 2
"""The destination does not hold what ``expected`` says."""
EXIT_ROOT_RETAINED = 3
"""``prune_empty_parents`` kept a verified-empty root whose parent will not release it."""
EXIT_UNSUITABLE = 4
"""A link, or the wrong type, at the destination or a parent."""
EXIT_DENIED = 5
"""``EACCES``, ``EPERM`` or ``EROFS``."""
EXIT_CORRUPT_INPUT = 6
"""Content (``write``'s or ``write_if``'s) did not arrive as sent: its size or SHA-256 differs from the parameters."""
EXIT_LOCK_TIMEOUT = 7
"""The lock was not acquired within its wait."""
EXIT_ABSENT = 8
"""``read`` of a file that, or a parent of which, does not exist."""

if sys.platform != "win32":
    # The flags exist only on POSIX; the workstation imports this module for its
    # constants on any platform, and the program itself runs only on the guest.
    _TRAVERSE = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_TEMPORARY_PREFIX = ".agentworks-tmp-"


SEALED = {
    "write_if": ("expected", "sha256"),
    "write": ("sha256",),
    "remove_if": ("expected",),
}
"""The parameters each operation takes from stdin's first line rather than its arguments: content digests."""

Params = dict[str, Any]
"""One operation's decoded JSON parameters, its ``SEALED`` ones included (an operation that takes content
gets it as ``data``)."""


class _Exit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _classify(error: OSError) -> int:
    """The status for a refused path, or re-raise anything else as status 1."""
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return EXIT_UNSUITABLE
    if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return EXIT_DENIED
    raise error


class _Walk:
    """The open directories of one path, from ``/`` down.

    ``opened`` holds, for each directory entered, the descriptor of the directory
    containing it and its name. ``parent`` is the deepest open directory, and
    ``name`` the last component. ``complete`` is false when a parent was missing
    and nothing created it; ``parent`` is then the last directory that exists.
    """

    def __init__(self, path: str, *, create_mode: int | None = None, gid: int = -1) -> None:
        self.opened: list[tuple[int, str]] = []
        self.complete = True
        components = path.split("/")[1:]
        self.name = components[-1]
        self.parent = os.open("/", _TRAVERSE)
        for component in components[:-1]:
            try:
                child = os.open(component, _TRAVERSE, dir_fd=self.parent)
            except FileNotFoundError:
                if create_mode is None:
                    self.complete = False
                    return
                child = self.open_or_make(component, create_mode, gid)
            self.opened.append((self.parent, component))
            self.parent = child

    def open_or_make(self, component: str, mode: int, gid: int) -> int:
        """The directory ``component`` of ``parent``, made with ``mode`` (and group ``gid``) when absent."""
        try:
            os.mkdir(component, 0o700, dir_fd=self.parent)
        except FileExistsError:
            return os.open(component, _TRAVERSE, dir_fd=self.parent)
        child = os.open(component, _OPEN_DIRECTORY, dir_fd=self.parent)
        if gid != -1:
            os.fchown(child, -1, gid)
        os.fchmod(child, mode)
        return child

    def close(self) -> None:
        os.close(self.parent)
        for descriptor, _ in self.opened:
            os.close(descriptor)


def _open_regular(parent: int, name: str) -> int | None:
    """A regular file opened without following a link, ``None`` if absent; anything else is unsuitable."""
    try:
        descriptor = os.open(name, _OPEN_FILE, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ENXIO:  # a socket, or a device with nothing behind it
            raise _Exit(EXIT_UNSUITABLE) from None
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise _Exit(EXIT_UNSUITABLE)
    return descriptor


def _digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 256 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _lock(path: str, wait: float) -> int | None:
    """The lock's descriptor, held, or ``None`` when ``wait`` passed first.

    A lock file that cannot be opened (a link, a denial) raises: only a writer
    outside Agentworks can make it so, which is out of scope, so it is status 1.
    """
    import fcntl

    try:
        descriptor = os.open(path, _OPEN_FILE)
    except FileNotFoundError:
        try:
            # protected_regular refuses an O_CREAT open of another identity's file in a sticky
            # directory, even for root; with O_EXCL the kernel reports EEXIST first.
            descriptor = os.open(path, _OPEN_FILE | os.O_CREAT | os.O_EXCL, 0o644)
            os.fchmod(descriptor, 0o644)
        except FileExistsError:
            descriptor = os.open(path, _OPEN_FILE)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return None
            time.sleep(0.05)


def _read(params: Params) -> int:
    walk = _Walk(params["path"])
    try:
        if not walk.complete:
            return EXIT_ABSENT
        descriptor = _open_regular(walk.parent, walk.name)
        if descriptor is None:
            return EXIT_ABSENT
        try:
            while chunk := os.read(descriptor, 256 * 1024):
                sys.stdout.buffer.write(chunk)
        finally:
            os.close(descriptor)
        sys.stdout.buffer.flush()
        return 0
    finally:
        walk.close()


def _stat(params: Params) -> int:
    walk = _Walk(params["path"])
    try:
        observed: Params = {"kind": "absent", "mode": None, "digest": None}
        if walk.complete:
            try:
                metadata = os.stat(walk.name, dir_fd=walk.parent, follow_symlinks=False)
            except FileNotFoundError:
                metadata = None
            if metadata is not None and stat.S_ISDIR(metadata.st_mode):
                observed["kind"] = "directory"
            elif metadata is not None:
                if not stat.S_ISREG(metadata.st_mode):
                    return EXIT_UNSUITABLE
                descriptor = _open_regular(walk.parent, walk.name)
                if descriptor is not None:
                    try:
                        mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
                        observed = {"kind": "file", "mode": mode, "digest": _digest(descriptor)}
                    finally:
                        os.close(descriptor)
        sys.stdout.write(json.dumps(observed) + "\n")
        sys.stdout.flush()
        return 0
    finally:
        walk.close()


def _destination_digest(parent: int, name: str) -> str | None:
    descriptor = _open_regular(parent, name)
    if descriptor is None:
        return None
    try:
        return _digest(descriptor)
    finally:
        os.close(descriptor)


def _temporary_name(name: str) -> str:
    """The one temporary name beside the destination ``name``, whatever its length.

    It is fixed, so the next write to the same destination replaces what a
    write killed before its rename left behind.
    """
    return _TEMPORARY_PREFIX + hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:24]


def _write(params: Params) -> int:
    """Replace the file whatever it holds, with the requested mode and ownership; never through a link."""
    import pwd

    uid = -1 if params["owner"] is None else pwd.getpwnam(params["owner"]).pw_uid
    return _replace(params, uid=uid, expected=None, compare=False, preserve=False)


def _write_if(params: Params) -> int:
    """Replace the file only while it still has digest ``expected`` (``None``: still absent)."""
    return _replace(params, uid=-1, expected=params["expected"], compare=True, preserve=params["preserve"])


def _replace(params: Params, *, uid: int, expected: str | None, compare: bool, preserve: bool) -> int:
    """Publish ``data`` at the path through a temporary sibling renamed over it.

    The temporary file gets its owner, group and mode before the rename, so
    the destination never holds content without them. ``compare`` checks the
    destination's digest against ``expected``; without it, an existing
    destination need only be a regular file.
    """
    import grp

    gid = -1 if params["group"] is None else grp.getgrnam(params["group"]).gr_gid
    # A conditional write creates parents only for an absent destination: a missing one contradicts ``expected``.
    create_mode = params["parent_mode"] if not compare or expected is None else None
    walk = _Walk(params["path"], create_mode=create_mode, gid=gid)
    if not walk.complete:
        walk.close()
        return EXIT_CONFLICT
    parent = walk.parent
    temporary = _temporary_name(walk.name)
    try:
        # Under the lock no live write owns this name: anything there is a killed write's.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)
        output = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
        )
        try:
            view = memoryview(params["data"])
            while view:
                view = view[os.write(output, view) :]
            # chown first: it clears set-id bits that fchmod then sets.
            if uid != -1 or gid != -1:
                os.fchown(output, uid, gid)
            os.fchmod(output, params["mode"])
            # Opening the destination is also its type check: a link or anything but a regular file is unsuitable.
            existing = _open_regular(parent, walk.name)
            try:
                if compare and (None if existing is None else _digest(existing)) != expected:
                    return EXIT_CONFLICT
                if preserve and existing is not None:
                    _copy_metadata(existing, output)
            finally:
                if existing is not None:
                    os.close(existing)
            os.fsync(output)
        finally:
            os.close(output)
        os.rename(temporary, walk.name, src_dir_fd=parent, dst_dir_fd=parent)
        # The write is done once renamed; making the rename durable is best effort, not its outcome.
        with contextlib.suppress(OSError):
            directory = os.open(".", _OPEN_DIRECTORY, dir_fd=parent)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return 0
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)
        walk.close()


def _copy_metadata(source: int, target: int) -> None:
    """Give ``target`` the owner, group, mode and extended attributes (ACLs included) of ``source``.

    Attributes come after the mode, so an ACL's mask survives the ``fchmod``;
    one the new file inherited from a default directory ACL is removed.
    """
    metadata = os.fstat(source)
    os.fchown(target, metadata.st_uid, metadata.st_gid)
    os.fchmod(target, stat.S_IMODE(metadata.st_mode))
    wanted, present = _attributes(source), _attributes(target)
    for key in present.keys() - wanted.keys():
        os.removexattr(target, key)
    for key, value in wanted.items():
        if present.get(key) != value:
            os.setxattr(target, key, value)


def _attributes(descriptor: int) -> dict[str, bytes]:
    """The extended attributes of ``descriptor``: none on a filesystem that keeps none.

    One removed between listing and reading (``ENODATA``) is left out.
    """
    try:
        keys = os.listxattr(descriptor)
    except OSError as error:
        if error.errno == errno.ENOTSUP:
            return {}
        raise
    attributes: dict[str, bytes] = {}
    for key in keys:
        try:
            attributes[key] = os.getxattr(descriptor, key)
        except OSError as error:
            if error.errno != errno.ENODATA:
                raise
    return attributes


def _remove_if(params: Params) -> int:
    walk = _Walk(params["path"])
    try:
        if not walk.complete:
            return EXIT_CONFLICT
        if _destination_digest(walk.parent, walk.name) != params["expected"]:
            return EXIT_CONFLICT
        os.unlink(walk.name, dir_fd=walk.parent)
        return 0
    finally:
        walk.close()


def _ensure_directory(params: Params) -> int:
    walk = _Walk(params["path"], create_mode=params["mode"])
    try:
        os.close(walk.open_or_make(walk.name, params["mode"], -1))
        return 0
    finally:
        walk.close()


def _prune_empty_parents(params: Params) -> int:
    root_depth = len(params["root"].split("/")[1:])
    walk = _Walk(params["path"])
    try:
        # opened[i] is (the directory holding component i, component i), and
        # opened[i + 1][0], or walk.parent for the last, is component i itself.
        chain = walk.opened
        for index in range(len(chain) - 1, root_depth - 2, -1):
            parent, component = chain[index]
            try:
                os.rmdir(component, dir_fd=parent)
            except FileNotFoundError:
                continue
            except OSError as error:
                # Not empty, or busy as a mount point: pruning stops here.
                if error.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY):
                    return 0
                if index == root_depth - 1 and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    walked = chain[index + 1][0] if index + 1 < len(chain) else walk.parent
                    if _is_same_empty_directory(parent, component, walked):
                        return EXIT_ROOT_RETAINED
                raise
        return 0
    finally:
        walk.close()


def _is_same_empty_directory(parent: int, component: str, walked: int) -> bool:
    """Whether ``component`` is still the directory the walk opened, and empty."""
    check = os.open(component, _OPEN_DIRECTORY, dir_fd=parent)
    try:
        observed, held = os.fstat(check), os.fstat(walked)
        if (observed.st_dev, observed.st_ino) != (held.st_dev, held.st_ino):
            return False
        with os.scandir(check) as entries:
            return next(entries, None) is None
    finally:
        os.close(check)


_READERS = {"read": _read, "stat": _stat}
_MUTATORS = {
    "write": _write,
    "write_if": _write_if,
    "remove_if": _remove_if,
    "ensure_directory": _ensure_directory,
    "prune_empty_parents": _prune_empty_parents,
}


def main(argv: list[str], *, lock_path: str = LOCK_PATH, lock_wait: float = LOCK_WAIT_SECONDS) -> int:
    """Run one operation, ``argv`` being its name and its JSON parameters, and return the exit status."""
    import signal

    def end(number: int, frame: object) -> None:
        raise SystemExit(128 + number)

    # Without a terminal no hangup arrives when the connection drops; a signal that does
    # arrive unwinds through each operation's cleanup and exits nonzero.
    signal.signal(signal.SIGHUP, end)
    signal.signal(signal.SIGTERM, end)
    operation, params = argv[0], json.loads(argv[1])
    if operation in _READERS:
        return _status(_READERS[operation], params)
    mutate = _MUTATORS[operation]
    if operation in SEALED:
        # The whole input arrives before anything is created, so a dropped connection
        # (a short stdin) publishes nothing.
        header, _, data = sys.stdin.buffer.read().partition(b"\n")
        sealed = json.loads(header)
        params.update((name, sealed[name]) for name in SEALED[operation])
        if "sha256" in SEALED[operation]:
            if len(data) != params["size"] or hashlib.sha256(data).hexdigest() != params["sha256"]:
                return EXIT_CORRUPT_INPUT
            params["data"] = data
    lock = _lock(lock_path, lock_wait)
    if lock is None:
        return EXIT_LOCK_TIMEOUT
    try:
        return _status(mutate, params)
    finally:
        os.close(lock)


def _status(operation: Callable[[Params], int], params: Params) -> int:
    """Run one operation on its path, classifying a refusal of that path."""
    try:
        return operation(params)
    except _Exit as done:
        return done.status
    except OSError as error:
        return _classify(error)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
