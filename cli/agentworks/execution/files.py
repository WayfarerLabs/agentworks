"""Files on a managed VM, as one identity, through the guest file program.

``VMAccess.files()`` returns a :class:`Files`. Each method is one ``run`` of
``file_program.py`` as a ``PythonProgram``: the source travels on the
connection, the parameters as one JSON argument, content on stdin. Nothing is
read or written through a symbolic link, every mutation is conditional on what
the caller observed and holds the VM-wide file lock, and ``elevated=True`` runs
the operation as root, which an agent's access refuses before dispatch.

Modes and placement policy are the caller's: this module enforces only that a
path is absolute and normalized (:func:`guest_path`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.errors import ExternalError, StateError
from agentworks.execution import file_program
from agentworks.execution.errors import CommandFailed, FileConflict, FileLockTimeout, PathRefusal, PathRefused
from agentworks.execution.requests import CAPTURE_LIMIT_BYTES, Bytes, CaptureOutput, NoInput, PythonProgram

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.access import VMAccess
    from agentworks.execution.results import Result

_NO_INPUT = NoInput()
_CAPTURE = CaptureOutput()


def program_source() -> str:
    """The text of ``file_program.py``, which every file operation sends."""
    return resources.files("agentworks.execution").joinpath("file_program.py").read_text(encoding="utf-8")


def guest_path(path: str) -> str:
    """``path`` if it is absolute, printable and has no empty, ``.`` or ``..`` component.

    Boundary: paths come from plugin output and from recorded ownership, which
    persists across executions, so each is checked before the program sees it.
    """
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or not path.isprintable()
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
    ):
        raise StateError(f"guest file paths must be absolute and normalized: {path!r}")
    return str(PurePosixPath(path))


@dataclass(frozen=True)
class Absent:
    """Nothing is at the path, or a parent of it is missing."""


@dataclass(frozen=True)
class Directory:
    """A directory is at the path."""


@dataclass(frozen=True)
class RegularFile:
    """A regular file is at the path, with its SHA-256 and permission bits, both observed on the guest."""

    digest: str
    mode: int


PathState = Absent | Directory | RegularFile


class Pruned(Enum):
    """What :meth:`Files.prune_empty_parents` did."""

    COMPLETE = "complete"
    """Every empty parent up to the root is gone, or it stopped at the first one that is not empty."""
    ROOT_RETAINED = "root_retained"
    """The root is empty but stays, because its parent denies removing it."""


@dataclass(frozen=True)
class Files:
    """One identity's file operations on one VM; each method is one request bounded by ``deadline`` seconds."""

    access: VMAccess
    deadline: float | None = None

    def read(self, path: str, *, limit: int = CAPTURE_LIMIT_BYTES, elevated: bool = False) -> bytes | None:
        """The bytes of the regular file at ``path``, or ``None`` when it or a parent is absent.

        A file larger than ``limit`` is ``ExecutionIncomplete`` naming the limit.
        The content stays out of the operation log.
        """
        path = guest_path(path)
        status, result = self._run(
            "read", {"path": path}, elevated=elevated, stdout=CaptureOutput(limit), log_streams=False
        )
        if status == file_program.EXIT_ABSENT:
            return None
        self._require_done("read", path, status, result)
        return result.stdout

    def stat(self, path: str, *, elevated: bool = False) -> PathState:
        """What is at ``path``; a regular file's digest is computed on the guest, so no content travels."""
        path = guest_path(path)
        status, result = self._run("stat", {"path": path}, elevated=elevated, log_streams=False)
        self._require_done("stat", path, status, result)
        try:
            observed = json.loads(result.stdout)
            kind, mode, digest = observed["kind"], observed["mode"], observed["digest"]
            if kind == "absent":
                return Absent()
            if kind == "directory":
                return Directory()
            if (
                kind != "file"
                or type(mode) is not int
                or not 0 <= mode <= 0o7777
                or not isinstance(digest, str)
                or len(digest) != 64
                or digest.strip("0123456789abcdef")
            ):
                raise ValueError
            return RegularFile(digest, mode)
        except (ValueError, KeyError, TypeError):
            raise ExternalError(
                f"the file program's stat of '{path}' on VM '{self.access.vm_name}' was unreadable"
            ) from None

    def write_if(
        self,
        path: str,
        data: bytes,
        *,
        expected: str | None,
        mode: int,
        parent_mode: int,
        group: str | None = None,
        preserve_metadata: bool = False,
        elevated: bool = False,
    ) -> None:
        """Replace ``path`` with ``data`` only while it still has digest ``expected`` (``None``: still absent).

        The file is written beside the destination with ``mode`` (and ``group``,
        set first), then renamed over it. With ``expected=None`` missing parents
        are created with ``parent_mode``; otherwise a missing parent is a
        conflict. ``preserve_metadata`` gives the new file the existing
        one's owner, group, mode and extended attributes instead. A changed
        destination is ``FileConflict``, and nothing is written.
        """
        path = guest_path(path)
        params = {
            "path": path,
            "expected": expected,
            "mode": mode,
            "group": group,
            "parent_mode": parent_mode,
            "preserve": preserve_metadata,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        status, result = self._run("write_if", params, elevated=elevated, stdin=Bytes(data))
        self._require_done("write_if", path, status, result)

    def remove_if(self, path: str, *, expected: str, elevated: bool = False) -> None:
        """Remove the regular file at ``path`` only while it has digest ``expected``; absent is a conflict."""
        path = guest_path(path)
        status, result = self._run("remove_if", {"path": path, "expected": expected}, elevated=elevated)
        self._require_done("remove_if", path, status, result)

    def prune_empty_parents(self, path: str, *, root: str, elevated: bool = False) -> Pruned:
        """Remove the empty parents of ``path``, deepest first, up to and including ``root``.

        ``root`` must be a proper ancestor of ``path``. Pruning stops at the
        first parent that is not empty or is busy (a mount point).
        """
        path, root = guest_path(path), guest_path(root)
        if not path.startswith(root + "/"):
            raise StateError(f"'{path}' is not beneath the root '{root}' it is pruned to")
        status, result = self._run("prune_empty_parents", {"path": path, "root": root}, elevated=elevated)
        if status == file_program.EXIT_ROOT_RETAINED:
            return Pruned.ROOT_RETAINED
        self._require_done("prune_empty_parents", path, status, result)
        return Pruned.COMPLETE

    def _run(
        self,
        operation: str,
        params: Mapping[str, object],
        *,
        elevated: bool,
        stdin: NoInput | Bytes = _NO_INPUT,
        stdout: CaptureOutput = _CAPTURE,
        log_streams: bool = True,
    ) -> tuple[int, Result]:
        request = PythonProgram(
            program_source(),
            args=(operation, json.dumps(params, ensure_ascii=True, separators=(",", ":"))),
            elevated=elevated,
            stdin=stdin,
            deadline=self.deadline,
            log_streams=log_streams,
        )
        result = self.access.run(request, stdout=stdout)
        return result.require_completed(self.access.vm_name), result

    def _require_done(self, operation: str, path: str, status: int, result: Result) -> None:
        """Raise the error for any status but 0."""
        vm_name = self.access.vm_name
        if status == 0:
            return
        if status == file_program.EXIT_CONFLICT:
            raise FileConflict(vm_name, operation, path)
        if status in (file_program.EXIT_UNSUITABLE, file_program.EXIT_DENIED):
            reason = PathRefusal.UNSUITABLE if status == file_program.EXIT_UNSUITABLE else PathRefusal.DENIED
            raise PathRefused(vm_name, operation, path, reason)
        if status == file_program.EXIT_LOCK_TIMEOUT:
            raise FileLockTimeout(vm_name, operation, path)
        raise CommandFailed(vm_name, status, result)
