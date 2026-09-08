"""Private snapshots and atomic publication for fixed native settings roles."""

from __future__ import annotations

import json
import shlex
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ExternalError, StateError

if TYPE_CHECKING:
    from types import TracebackType

    from agentworks.transports import Transport

# Open each directory relative to its already-open parent. A concurrent symlink
# replacement cannot redirect reads or publication into another directory tree.
_FILE_PROGRAM = r"""
import grp, hashlib, json, os, secrets, stat, sys
op, destination, staging, expected, group = sys.argv[1:]
parts = destination.split('/')[1:]
if not destination.startswith('/') or any(p in ('', '.', '..') for p in parts):
    sys.exit(1)
if op in ('directory', 'mkdir'):
    parts.append('unused')
fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
try:
    for component in parts[:-1]:
        try:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            if op in ('read', 'directory'):
                print(json.dumps({'exists': False}))
                sys.exit(0)
            os.mkdir(component, 0o700 if not group else 0o2770, dir_fd=fd)
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if group:
                os.fchown(next_fd, -1, grp.getgrnam(group).gr_gid)
                os.fchmod(next_fd, 0o2770)
        os.close(fd)
        fd = next_fd
    if op in ('directory', 'mkdir'):
        print(json.dumps({'exists': True}))
        sys.exit(0)
    content = None
    try:
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(source_fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                sys.exit(1)
            content = source.read()
    if op == 'read':
        if content is not None:
            output_fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output_fd, 'wb') as output:
                output.write(content)
        print(json.dumps({'exists': content is not None}))
    else:
        actual = '-' if content is None else hashlib.sha256(content).hexdigest()
        if actual != expected:
            sys.exit(2)
        gid = -1 if not group else grp.getgrnam(group).gr_gid
        with open(staging, 'rb') as source:
            replacement = source.read()
        name = '.agentworks-settings-' + secrets.token_hex(12)
        try:
            output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(output_fd, 'wb') as output:
                os.fchown(output.fileno(), -1, gid)
                os.fchmod(output.fileno(), 0o600 if not group else 0o660)
                output.write(replacement)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
finally:
    os.close(fd)
"""


def native_path(path: str) -> str:
    """Validate an external guest path before interpreting its components."""
    if not path.startswith("/") or "\x00" in path or any(part in ("", ".", "..") for part in path.split("/")[1:]):
        raise StateError("native settings require an absolute, normalized guest path")
    return str(PurePosixPath(path))


class NativeFiles(AbstractContextManager["NativeFiles"]):
    """One operation's private remote/local staging, removed on every handled exit."""

    def __init__(self, runner: Transport) -> None:
        self.runner = runner
        self._local: tempfile.TemporaryDirectory[str] | None = None
        self.remote = ""
        self._counter = 0

    def __enter__(self) -> NativeFiles:
        result = self.runner.run("mktemp -d -t agentworks-native-XXXXXXXXXX", check=False)
        if not result.ok or not PurePosixPath(result.stdout.strip()).name.startswith("agentworks-native-"):
            raise ExternalError("could not create native setup staging directory")
        self.remote = native_path(result.stdout.strip())
        try:
            self._local = tempfile.TemporaryDirectory(prefix="agentworks-native-")
        except Exception:
            self.runner.run(f"rm -rf -- {shlex.quote(self.remote)}", check=False, discard_output=True)
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            result = self.runner.run(f"rm -rf -- {shlex.quote(self.remote)}", check=False, discard_output=True)
            if not result.ok:
                raise ExternalError("could not remove native setup staging directory")
        except Exception:
            if exc is None:
                raise ExternalError("could not remove native setup staging directory") from None
            output.warn("Native setup staging cleanup failed; the original setup error is retained.")
        finally:
            if self._local is not None:
                self._local.cleanup()

    def slot(self) -> tuple[str, Path]:
        """Allocate names in staging already owned by this operation."""
        if self._local is None:
            raise StateError("native setup staging is not open")
        self._counter += 1
        return f"{self.remote}/{self._counter}", Path(self._local.name) / str(self._counter)

    def directory(self, destination: str, *, create: bool = False) -> bool:
        """Check or create a native directory through guarded parent descriptors."""
        command = shlex.join(
            [
                "python3",
                "-c",
                _FILE_PROGRAM,
                "mkdir" if create else "directory",
                native_path(destination),
                "-",
                "-",
                "",
            ]
        )
        result = self.runner.run(command, check=False)
        if not result.ok:
            raise StateError("native config directory is inaccessible or contains a link")
        try:
            exists = json.loads(result.stdout)["exists"]
            if not isinstance(exists, bool):
                raise ValueError
            return exists
        except (ValueError, KeyError, TypeError):
            raise ExternalError("invalid native directory observation") from None

    def read(self, destination: str) -> bytes | None:
        """Read a regular native file without emitting its contents into logs."""
        destination = native_path(destination)
        remote, local = self.slot()
        command = shlex.join(["python3", "-c", _FILE_PROGRAM, "read", destination, remote, "-", ""])
        result = self.runner.run(command, check=False, discard_output=False)
        if not result.ok:
            raise StateError("native settings path is unreadable or contains an unsuitable file or link")
        try:
            exists = json.loads(result.stdout)["exists"]
            if not isinstance(exists, bool):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ExternalError("invalid native settings observation") from None
        if not exists:
            return None
        try:
            self.runner.copy_from(remote, local)
            return local.read_bytes()
        except Exception:
            raise ExternalError("could not capture native settings file") from None

    def publish(self, destination: str, content: bytes, *, expected: str | None, group: str = "") -> None:
        """Atomically replace a guarded file only while its observed bytes match."""
        destination = native_path(destination)
        remote, local = self.slot()
        local.write_bytes(content)
        try:
            self.runner.copy_to(local, remote)
            command = shlex.join(["python3", "-c", _FILE_PROGRAM, "write", destination, remote, expected or "-", group])
            result = self.runner.run(command, check=False, discard_output=True)
        except Exception:
            raise ExternalError("could not publish native settings file") from None
        if result.returncode == 2:
            raise StateError("native settings changed during setup; retry against the current file")
        if not result.ok:
            raise StateError("native settings publication could not establish the destination ownership or mode")
