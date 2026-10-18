"""Passive local owners retained by an ordinary file call before filesystem I/O.

Close only releases original resources. A close with an unknown outcome is never
retried, even when another original can still be released independently. Inventory
observations are comparisons for later admission, not an atomic tree snapshot.
"""

from __future__ import annotations

import sys
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.execution._local_file_io_platform import PosixHandles, WindowsHandles
from agentworks.execution._local_file_io_types import (
    LocalFileCleanupUncertainError as LocalFileCleanupUncertainError,
)
from agentworks.execution._local_file_io_types import LocalFileLimitError as LocalFileLimitError
from agentworks.execution._local_file_io_types import LocalFileUnsupportedError as LocalFileUnsupportedError
from agentworks.execution._local_file_io_types import LocalIdentity as LocalIdentity
from agentworks.execution._local_file_io_types import LocalTreeEntry as LocalTreeEntry
from agentworks.execution._local_file_io_types import (
    check_deadline,
    validate_limits,
    validate_local_tree_component,
    validate_local_tree_destination,
)
from agentworks.execution.files import Change, FileKind

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._local_file_io_platform import _HeldHandle
    from agentworks.execution.carrier import Deadline

__all__ = [
    "LocalDestination",
    "LocalFileSource",
    "LocalTreeInventory",
    "LocalTreeEntry",
    "LocalIdentity",
    "LocalFileUnsupportedError",
    "LocalFileLimitError",
    "LocalFileCleanupUncertainError",
    "validate_local_tree_component",
    "validate_local_tree_destination",
]


def _handles() -> PosixHandles:
    return WindowsHandles() if sys.platform == "win32" else PosixHandles()


class LocalFileSource:
    """Owned read-only source whose EOF confirms its original observation."""

    def __init__(self, path: Path, *, expected: LocalIdentity | None = None) -> None:
        validate_local_tree_destination(path)
        self._path = path
        self._expected = expected
        self._handles = _handles()
        self._file: _HeldHandle | None = None
        self._original: LocalIdentity | None = None
        self._deadline: Deadline | None = None
        self._read_size = 0
        self._started = False
        self._closed = False
        self._eof = False

    @property
    def settled(self) -> bool:
        return self._handles.settled

    def open(self, deadline: Deadline) -> int:
        if self._started or self._closed:
            raise ValueError("Local source cannot be reopened")
        self._started = True
        self._deadline = deadline
        metadata_only = sys.platform != "win32"
        self._file = self._handles.open_path(
            self._path.absolute(),
            directory=None if metadata_only else False,
            deadline=deadline,
        )
        original = self._handles.observe(self._file)
        if original.kind is not FileKind.REGULAR:
            raise LocalFileUnsupportedError("Local source is not an ordinary single-link file")
        if original.size < 0 or original.size > 2**63 - 1:
            raise LocalFileLimitError("Local source exceeds the supported byte bound")
        if self._expected is not None and original != self._expected:
            raise FileExistsError("Local source changed since inventory")
        if self._handles.observe_named(self._file) != original:
            raise FileExistsError("Local source changed during admission")
        if metadata_only:
            parent = self._file.parent
            assert parent is not None
            self._file = self._handles.child(parent, self._file.path.name, directory=False, deadline=deadline)
            if self._handles.observe(self._file) != original or self._handles.observe_named(self._file) != original:
                raise FileExistsError("Local source changed while establishing read access")
        self._original = original
        check_deadline(deadline)
        return original.size

    def read(self, maximum: int, /) -> bytes | None:
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("Local source read requires a positive byte bound")
        held, original, deadline = self._file, self._original, self._deadline
        if held is None or original is None or deadline is None or self._closed:
            raise ValueError("Local source is not open")
        check_deadline(deadline)
        if self._eof:
            return b""
        remaining = original.size - self._read_size
        data = self._handles.read(held, min(maximum, remaining, 1024 * 1024) if remaining else 1)
        check_deadline(deadline)
        if remaining:
            if not data:
                raise FileExistsError("Local source became shorter during transfer")
            self._read_size += len(data)
            return data
        if data or self._handles.observe(held) != original or self._handles.observe_named(held) != original:
            raise FileExistsError("Local source changed during transfer")
        check_deadline(deadline)
        self._eof = True
        return b""

    def close(self, deadline: Deadline) -> None:
        # Cleanup remains necessary after deadline expiry and never reopens a source.
        self._closed = True
        self._handles.close()


class LocalTreeInventory:
    """Bounded complete traversal, retaining original cleanup through failure."""

    def __init__(self, root: Path, *, max_entries: int, max_depth: int) -> None:
        validate_local_tree_destination(root)
        validate_limits(max_entries, max_depth)
        self._root = root
        self._max_entries = max_entries
        self._max_depth = max_depth
        self._handles = _handles()
        self._started = False
        self._closed = False
        self._entries: list[LocalTreeEntry] = []

    @property
    def settled(self) -> bool:
        return self._handles.settled

    def inventory(self, deadline: Deadline) -> tuple[LocalTreeEntry, ...]:
        if self._started or self._closed:
            raise ValueError("Local inventory cannot be repeated")
        self._started = True
        root = self._handles.open_path(self._root.absolute(), directory=True, deadline=deadline)
        identity = self._handles.observe(root)
        self._entries.append(LocalTreeEntry(PurePosixPath("."), FileKind.DIRECTORY, 0, identity))
        self._walk(root, PurePosixPath("."), 0, identity.device, deadline)
        check_deadline(deadline)
        return tuple(sorted(self._entries, key=lambda entry: entry.relative_path.parts))

    def _walk(self, held: _HeldHandle, relative: PurePosixPath, depth: int, device: int, deadline: Deadline) -> None:
        check_deadline(deadline)
        before = self._handles.observe(held)
        scan = self._handles.scan(held)
        names: list[str] = []
        for entry in scan.iterator:
            check_deadline(deadline)
            if depth >= self._max_depth or len(self._entries) + len(names) >= self._max_entries:
                raise LocalFileLimitError("Local tree exceeds the depth or entry bound")
            validate_local_tree_component(entry.name)
            names.append(entry.name)
        self._handles.close_scan(scan)
        for name in sorted(names):
            check_deadline(deadline)
            child_relative = relative / name
            if len(str(child_relative).encode("utf-8")) > 4_096:
                raise LocalFileLimitError("Local tree path exceeds its byte bound")
            if len(self._entries) >= self._max_entries:
                raise LocalFileLimitError("Local tree exceeds its entry bound")
            observed = self._handles.stat_child(held, name, deadline)
            if observed.device != device:
                raise LocalFileUnsupportedError("Local tree crosses an unsupported mount")
            child = self._handles.child(
                held,
                name,
                directory=True if observed.kind is FileKind.DIRECTORY else None,
                deadline=deadline,
            )
            if self._handles.observe(child) != observed:
                raise FileExistsError("Local tree entry changed during admission")
            size = observed.size if observed.kind is FileKind.REGULAR else 0
            if not 0 <= size <= 2**63 - 1:
                raise LocalFileLimitError("Local tree file exceeds the supported byte bound")
            self._entries.append(LocalTreeEntry(child_relative, observed.kind, size, observed))
            if observed.kind is FileKind.DIRECTORY:
                self._walk(child, child_relative, depth + 1, device, deadline)
            elif self._handles.observe_named(child) != observed:
                raise FileExistsError("Local tree file changed during inventory")
            self._handles.close_handle(child)
        if self._handles.observe(held) != before or self._handles.observe_named(held) != before:
            raise FileExistsError("Local tree directory changed during inventory")

    def close(self, deadline: Deadline) -> None:
        self._closed = True
        self._handles.close()


class LocalDestination:
    """Observe one ordinary destination and optionally create a missing directory."""

    def __init__(self, path: Path) -> None:
        validate_local_tree_destination(path)
        self._path = path
        self._handles = _handles()
        self._started = False
        self._closed = False
        self._observed = False
        self._entry: LocalTreeEntry | None = None
        self._parent: _HeldHandle | None = None
        self.changed = False
        self.mutation_uncertain = False

    @property
    def settled(self) -> bool:
        return self._handles.settled

    @property
    def identity(self) -> LocalIdentity | None:
        return self._entry.identity if self._entry is not None else None

    def observe(self, deadline: Deadline) -> LocalTreeEntry | None:
        if self._observed or self._started or self._closed:
            raise ValueError("Local destination observation cannot be repeated")
        self._observed = True
        path = self._path.absolute()
        self._parent = self._handles.open_path(path.parent, directory=None, deadline=deadline)
        if self._handles.observe(self._parent).kind is not FileKind.DIRECTORY:
            raise LocalFileUnsupportedError("Local destination parent is not an ordinary directory")
        try:
            held = (
                self._parent
                if path == path.parent
                else self._handles.child(
                    self._parent,
                    path.name,
                    directory=None,
                    deadline=deadline,
                )
            )
        except FileNotFoundError:
            check_deadline(deadline)
            return None
        observed = self._handles.observe(held)
        if self._handles.observe_named(held) != observed:
            raise FileExistsError("Local destination changed during observation")
        self._entry = LocalTreeEntry(
            PurePosixPath("."),
            observed.kind,
            observed.size if observed.kind is FileKind.REGULAR else 0,
            observed,
        )
        check_deadline(deadline)
        return self._entry

    def ensure_directory(self, deadline: Deadline) -> Change:
        if self._started or self._closed:
            raise ValueError("Local directory admission cannot be repeated")
        if not self._observed:
            self.observe(deadline)
        self._started = True
        path = self._path.absolute()
        parent = self._parent
        assert parent is not None
        if self._entry is not None:
            observed = self._entry.identity
            if observed.kind is not FileKind.DIRECTORY:
                raise LocalFileUnsupportedError("Local destination is not an ordinary directory")
            current = (
                self._handles.observe(parent)
                if path == path.parent
                else self._handles.stat_child(
                    parent,
                    path.name,
                    deadline,
                )
            )
            if current != observed:
                raise FileExistsError("Local destination changed during admission")
            check_deadline(deadline)
            return Change.UNCHANGED
        if self._handles.observe_named(parent) != self._handles.observe(parent):
            raise FileExistsError("Local destination parent changed before creation")
        check_deadline(deadline)
        self.mutation_uncertain = True
        try:
            self._handles.mkdir(parent, path.name)
        except FileExistsError:
            self.mutation_uncertain = False
            raise
        self.changed = True
        self.mutation_uncertain = False
        child = self._handles.child(parent, path.name, directory=True, deadline=deadline)
        self._handles.restrict_directory(child)
        observed = self._handles.observe(child)
        self._entry = LocalTreeEntry(PurePosixPath("."), FileKind.DIRECTORY, 0, observed)
        check_deadline(deadline)
        return Change.CHANGED

    def close(self, deadline: Deadline) -> None:
        self._closed = True
        self._handles.close()
