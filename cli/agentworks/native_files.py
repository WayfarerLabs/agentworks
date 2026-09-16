"""Private snapshots and atomic publication for owned native files."""

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
import errno, grp, hashlib, json, os, secrets, stat, sys
op, destination, staging, expected, group, executable = sys.argv[1:7]
public = len(sys.argv) > 7 and sys.argv[7] == '1'
preserve = len(sys.argv) > 8 and sys.argv[8] == '1'
parts = destination.split('/')[1:]
if not destination.startswith('/') or any(p in ('', '.', '..') for p in parts):
    sys.exit(1)
if op in ('directory', 'mkdir'):
    parts.append('unused')
traverse = getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
def classify(error):
    # Report a link, a non-directory component, and a denial as distinct
    # statuses so the caller can name the cause instead of failing generically.
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        sys.exit(4)
    if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        sys.exit(5)
    raise error
if op == 'prune':
    root_parts = staging.split('/')[1:]
    if parts[:len(root_parts)] != root_parts or len(parts) <= len(root_parts):
        sys.exit(1)
    parents = []
    fd = os.open('/', traverse)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, traverse, dir_fd=fd)
            except FileNotFoundError:
                break
            parents.append((fd, component))
            fd = child
        for index in range(len(parents) - 1, len(root_parts) - 2, -1):
            parent, component = parents[index]
            try:
                os.rmdir(component, dir_fd=parent)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    break
                if index == len(root_parts) - 1 and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    check = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        original = parents[index + 1][0] if index + 1 < len(parents) else fd
                        observed, held = os.fstat(check), os.fstat(original)
                        if (observed.st_dev, observed.st_ino) != (held.st_dev, held.st_ino):
                            raise OSError(errno.ESTALE, 'package root changed')
                        with os.scandir(check) as entries:
                            if next(entries, None) is None:
                                sys.exit(3)  # Only the final, verified-empty package root may remain.
                    finally:
                        os.close(check)
                raise
    finally:
        os.close(fd)
        for parent, _ in parents:
            os.close(parent)
    sys.exit(0)
fd = os.open('/', traverse)
try:
    for component in parts[:-1]:
        try:
            next_fd = os.open(component, traverse, dir_fd=fd)
        except FileNotFoundError:
            if op in ('read', 'fingerprint', 'delete', 'directory'):
                print(json.dumps({'exists': False}))
                sys.exit(0)
            os.mkdir(component, 0o755 if public else (0o700 if not group else 0o2770), dir_fd=fd)
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if public:
                os.fchmod(next_fd, 0o755)
            if group:
                os.fchown(next_fd, -1, grp.getgrnam(group).gr_gid)
                os.fchmod(next_fd, 0o2770)
        os.close(fd)
        fd = next_fd
    if op in ('directory', 'mkdir'):
        print(json.dumps({'exists': True}))
        sys.exit(0)
    content = None
    actual = '-'
    mode = 0
    metadata = None
    attributes = {}
    try:
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(source_fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                sys.exit(4)
            metadata = os.fstat(source.fileno())
            mode = stat.S_IMODE(metadata.st_mode)
            if op == 'read':
                content = source.read()
            else:
                digest = hashlib.sha256()
                while chunk := source.read(256 * 1024):
                    digest.update(chunk)
                actual = digest.hexdigest()
            if preserve:
                attributes = {key: os.getxattr(source.fileno(), key) for key in os.listxattr(source.fileno())}
    if op == 'fingerprint':
        print(json.dumps({'exists': actual != '-', 'sha256': actual, 'mode': mode}))
        sys.exit(0)
    if op == 'read':
        if content is not None:
            output_fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output_fd, 'wb') as output:
                if public:
                    staging_owner = os.stat(os.path.dirname(staging), follow_symlinks=False)
                    os.fchown(output.fileno(), staging_owner.st_uid, staging_owner.st_gid)
                output.write(content)
        print(json.dumps({'exists': content is not None}))
    else:
        if actual != expected:
            sys.exit(2)
        if op == 'delete':
            if actual != '-':
                os.unlink(parts[-1], dir_fd=fd)
            sys.exit(0)
        gid = -1 if not group else grp.getgrnam(group).gr_gid
        with open(staging, 'rb') as source:
            replacement = source.read()
        name = '.agentworks-settings-' + secrets.token_hex(12)
        try:
            output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(output_fd, 'wb') as output:
                output.write(replacement)
                output.flush()
                uid = metadata.st_uid if preserve and metadata is not None else -1
                gid = metadata.st_gid if preserve and metadata is not None else gid
                os.fchown(output.fileno(), uid, gid)
                if not preserve or metadata is None:
                    mode = (0o755 if executable == '1' else 0o644) if public else (
                        (0o700 if executable == '1' else 0o600) if not group
                        else (0o770 if executable == '1' else 0o660)
                    )
                os.fchmod(output.fileno(), mode)
                if preserve and metadata is not None:
                    current_attributes = {
                        key: os.getxattr(output.fileno(), key) for key in os.listxattr(output.fileno())
                    }
                    for key in current_attributes.keys() - attributes.keys():
                        os.removexattr(output.fileno(), key)
                    for key, value in attributes.items():
                        if current_attributes.get(key) != value:
                            os.setxattr(output.fileno(), key, value)
                os.fsync(output.fileno())
            os.replace(name, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            sync_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)
        finally:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
except OSError as error:
    classify(error)
finally:
    os.close(fd)
"""


# Shared by every integration. These are directory boundaries, never file targets.
ROOT_FILE_DIRECTORIES = ("/etc/claude-code", "/etc/codex", "/opt/agentworks/artifacts")

_UNSUITABLE_PATH = 4
_DENIED_PATH = 5


def _path_failure(subject: str, destination: str, returncode: int) -> StateError:
    """Name why a guarded path could not be used, from the guest program's status."""
    if returncode == _UNSUITABLE_PATH:
        return StateError(
            f"{subject} '{destination}' is a symlink or is not a regular file",
            hint=(
                "Agentworks owns this file and never reads or writes through a link. Remove the link on the VM, "
                "stop whatever installs it (commonly a dotfiles install script) from claiming this path, and "
                "express those values through the integration's settings mapping instead."
            ),
        )
    if returncode == _DENIED_PATH:
        return StateError(
            f"{subject} '{destination}' cannot be accessed",
            hint="Check the ownership and mode of the file and each of its parent directories, then retry setup.",
        )
    return StateError(f"{subject} '{destination}' could not be inspected safely")


def native_path(path: str) -> str:
    """Validate an external guest path before interpreting its components."""
    if (
        not path.startswith("/")
        or not path.isprintable()
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
    ):
        raise StateError("native settings require an absolute, normalized guest path")
    return str(PurePosixPath(path))


def root_native_path(path: str, *, directory: bool = False) -> str:
    """Constrain elevated plugin paths and persisted cleanup paths before I/O.

    Boundary directories may be inspected or created, but file operations and
    removable package roots must stay strictly beneath them.
    """
    path = native_path(path)
    if not any(path.startswith(root + "/") or (directory and path == root) for root in ROOT_FILE_DIRECTORIES):
        raise StateError(f"Elevated native file access is outside the allowed directories: '{path}'")
    return path


def require_python3(runner: Transport) -> None:
    """Check the guest prerequisite before native setup, including older VMs."""
    if not runner.run("command -v python3 >/dev/null", check=False, discard_output=True).ok:
        raise StateError(
            "native harness setup requires python3 on the VM",
            hint="Run agw vm reinit <vm-name> to install required system packages before retrying setup.",
        )


class NativeFiles(AbstractContextManager["NativeFiles"]):
    """Private staging with optional elevation for guarded VM destinations.

    Transfers and staging stay owned by the transport user. Only the guarded
    guest helper runs as root for VM publication; new VM files are readable
    by all users, while generated-section updates preserve existing metadata.
    """

    def __init__(self, runner: Transport, *, root: bool = False) -> None:
        self.runner = runner
        self.root = root
        self._local: tempfile.TemporaryDirectory[str] | None = None
        self.remote = ""
        self._counter = 0

    def __enter__(self) -> NativeFiles:
        require_python3(self.runner)
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

    def _command(self, arguments: list[str], *, preserve_metadata: bool = False) -> str:
        prefix = ["sudo", "-n", "--"] if self.root else []
        return shlex.join([*prefix, *arguments, "1" if self.root else "0", "1" if preserve_metadata else "0"])

    def _path(self, destination: str, *, directory: bool = False) -> str:
        return root_native_path(destination, directory=directory) if self.root else native_path(destination)

    def directory(self, destination: str, *, create: bool = False) -> bool:
        """Check or create a native directory through guarded parent descriptors."""
        destination = self._path(destination, directory=True)
        command = self._command(
            [
                "python3",
                "-c",
                _FILE_PROGRAM,
                "mkdir" if create else "directory",
                destination,
                "-",
                "-",
                "",
                "0",
            ]
        )
        result = self.runner.run(command, check=False)
        if not result.ok:
            raise _path_failure("native config directory", destination, result.returncode)
        try:
            exists = json.loads(result.stdout)["exists"]
            if not isinstance(exists, bool):
                raise ValueError
            return exists
        except (ValueError, KeyError, TypeError):
            raise ExternalError("invalid native directory observation") from None

    def read(self, destination: str) -> bytes | None:
        """Read a regular native file without emitting its contents into logs."""
        destination = self._path(destination)
        remote, local = self.slot()
        command = self._command(["python3", "-c", _FILE_PROGRAM, "read", destination, remote, "-", "", "0"])
        result = self.runner.run(command, check=False, discard_output=False)
        if not result.ok:
            raise _path_failure("native settings file", destination, result.returncode)
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

    def publish(
        self,
        destination: str,
        content: bytes,
        *,
        expected: str | None,
        group: str = "",
        executable: bool = False,
        preserve_metadata: bool = False,
    ) -> None:
        """Atomically replace a guarded file only while its observed bytes match."""
        destination = self._path(destination)
        remote, local = self.slot()
        local.write_bytes(content)
        try:
            self.runner.copy_to(local, remote)
            command = self._command(
                [
                    "python3",
                    "-c",
                    _FILE_PROGRAM,
                    "write",
                    destination,
                    remote,
                    expected or "-",
                    group,
                    "1" if executable else "0",
                ],
                preserve_metadata=preserve_metadata,
            )
            result = self.runner.run(command, check=False, discard_output=True)
        except Exception:
            raise ExternalError("could not publish native settings file") from None
        if result.returncode == 2:
            raise StateError("native settings changed during setup; retry against the current file")
        if result.returncode in (_UNSUITABLE_PATH, _DENIED_PATH):
            raise _path_failure("native settings file", destination, result.returncode)
        if not result.ok:
            raise StateError("native settings publication could not establish the destination ownership or mode")

    def remove(self, destination: str, *, expected: str) -> None:
        """Remove only a regular file that still matches its recorded ownership."""
        destination = self._path(destination)
        command = self._command(["python3", "-c", _FILE_PROGRAM, "delete", destination, "-", expected, "", "0"])
        result = self.runner.run(command, check=False, discard_output=True)
        if result.returncode == 2:
            raise StateError("owned native file changed; retaining it for operator inspection")
        if result.returncode in (_UNSUITABLE_PATH, _DENIED_PATH):
            raise _path_failure("owned native file", destination, result.returncode)
        if not result.ok:
            raise StateError("owned native file could not be removed safely")

    def prune_empty_parents(self, destination: str, *, root: str) -> None:
        """Prune only empty parents of a retired file, through its package root."""
        destination, root = self._path(destination), self._path(root)
        if not destination.startswith(root + "/"):
            raise StateError("retired native file is outside its package root")
        command = self._command(["python3", "-c", _FILE_PROGRAM, "prune", destination, root, "-", "", "0"])
        result = self.runner.run(command, check=False, discard_output=True)
        if result.returncode == 3:
            output.warn(
                f"Empty retired package root '{root}' was retained because its parent denies directory removal."
            )
        elif not result.ok:
            raise StateError(
                f"Retired members beneath package root '{root}' could not be pruned safely",
                hint="Check permissions and retry setup. Cleanup evidence remains; files may already be absent.",
            )

    def fingerprint(self, destination: str) -> tuple[str, int] | None:
        """Stream a guarded file's hash and mode without copying or logging its body."""
        destination = self._path(destination)
        command = self._command(["python3", "-c", _FILE_PROGRAM, "fingerprint", destination, "-", "-", "", "0"])
        result = self.runner.run(command, check=False)
        if not result.ok:
            raise _path_failure("artifact destination", destination, result.returncode)
        try:
            observed = json.loads(result.stdout)
            if observed["exists"] is False:
                return None
            digest, mode = observed["sha256"], observed["mode"]
            if (
                observed["exists"] is not True
                or not isinstance(digest, str)
                or len(digest) != 64
                or type(mode) is not int
            ):
                raise ValueError
            return digest, mode
        except (ValueError, KeyError, TypeError):
            raise ExternalError("invalid artifact destination observation") from None
