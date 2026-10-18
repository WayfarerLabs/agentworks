"""Private observations and pure admission for workstation tree transfers."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.carrier import Deadline
    from agentworks.execution.files import FileKind


class LocalFileUnsupportedError(OSError):
    """The local object cannot be admitted by the transfer contract."""


class LocalFileLimitError(OSError):
    """The complete tree exceeds a caller-selected transfer bound."""


class LocalFileCleanupUncertainError(OSError):
    """An admitted close has no confirmed outcome and cannot be repeated."""


@dataclass(frozen=True, repr=False)
class LocalIdentity:
    """One local observation, usable only as a later source comparison."""

    platform: str
    device: int
    inode: int | bytes
    size: int
    times: tuple[int, ...]
    mode: int
    links: int
    kind: FileKind

    @property
    def object_key(self) -> tuple[str, int, int | bytes]:
        return (self.platform, self.device, self.inode)


@dataclass(frozen=True, repr=False)
class LocalTreeEntry:
    relative_path: PurePosixPath
    kind: FileKind
    size: int
    identity: LocalIdentity


def check_deadline(deadline: Deadline) -> None:
    if deadline.expired:
        raise TimeoutError("Local file deadline expired")


def _validate_windows_component(component: str) -> None:
    """Refuse ambiguous Windows tree names before workstation mutation."""
    stem = component.split(".", 1)[0].rstrip(" ").upper()
    if (
        not component
        or component in (".", "..")
        or component[-1] in (" ", ".")
        or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in component)
        or stem in ("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$")
        or stem in {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789\u00b9\u00b2\u00b3"}
    ):
        raise LocalFileUnsupportedError("Local Windows tree component is unsupported")


def _validate_windows_path(path: Path | PureWindowsPath) -> None:
    raw = str(path)
    parsed = PureWindowsPath(raw)
    try:
        units = len(raw.encode("utf-16-le")) // 2
    except UnicodeError:
        raise LocalFileUnsupportedError("Local Windows tree path encoding is unsupported") from None
    if (
        len(raw) < 3
        or raw[1:3] != ":\\"
        or not raw[0].isascii()
        or not raw[0].isalpha()
        or raw.startswith("\\\\")
        or "/" in raw
        or "\x00" in raw
        or units >= 260
    ):
        raise LocalFileUnsupportedError("Local Windows tree requires an ordinary drive path")
    for component in parsed.parts[1:]:
        _validate_windows_component(component)


def validate_local_tree_component(component: str) -> None:
    """Validate an externally supplied relative tree component for this host."""
    if not component or component in (".", "..") or "/" in component or "\x00" in component:
        raise LocalFileUnsupportedError("Local tree component is unsupported")
    try:
        component.encode("utf-8")
    except UnicodeError:
        raise LocalFileUnsupportedError("Local tree component is not UTF-8") from None
    if sys.platform == "win32":
        _validate_windows_component(component)


def validate_local_tree_destination(path: Path) -> None:
    """Validate a caller's local path without opening or creating any object."""
    if ".." in path.parts or "\x00" in str(path):
        raise LocalFileUnsupportedError("Local tree path must be normalized")
    if sys.platform == "win32":
        _validate_windows_path(path.absolute())
    elif sys.platform not in ("linux", "darwin"):
        raise LocalFileUnsupportedError("Local file transfers require a supported workstation")


def validate_limits(max_entries: int, max_depth: int) -> None:
    """Validate caller-supplied complete-tree limits before filesystem I/O."""
    if (
        type(max_entries) is not int
        or not 1 <= max_entries <= 4_096
        or type(max_depth) is not int
        or not 0 <= max_depth <= 8
    ):
        raise ValueError("Local inventory requires bounded entry and depth limits")
