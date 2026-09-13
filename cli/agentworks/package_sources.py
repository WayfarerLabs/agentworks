"""Bounded workstation package capture without checkout or installation.

The source reference grammar and invoking workstation identity are shared with
``sources``. Git packages are read from committed objects, never a checkout.
"""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from agentworks.sources import SourceRef, SourceRefError, parse_source_ref


@dataclass(frozen=True)
class CaptureLimits:
    seconds: float = 120
    members: int = 4096
    member_bytes: int = 16 * 1024 * 1024
    total_bytes: int = 64 * 1024 * 1024
    storage_bytes: int = 128 * 1024 * 1024
    depth: int = 32


DEFAULT_CAPTURE_LIMITS = CaptureLimits()


@dataclass(frozen=True)
class PackageMember:
    path: str
    data: bytes
    executable: bool


@dataclass(frozen=True)
class CapturedPackage:
    members: tuple[PackageMember, ...]
    source: str
    selected_path: str = ""
    requested_ref: str = ""
    commit: str = ""


_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)


def validate_member_path(path: str, *, depth: int = 32) -> None:
    """Validate filesystem/Git/codec input against portable package containment."""
    parts = path.split("/")
    if not path or len(parts) > depth:
        raise SourceRefError("artifact member path is empty or exceeds traversal depth")
    for part in parts:
        if (
            not part
            or part in (".", "..")
            or part.casefold() == ".git"
            or part[-1:] in (".", " ")
            or _RESERVED.match(part)
            or len(part.encode("utf-8", errors="replace")) > 255
            or any(ord(char) < 32 or ord(char) == 127 or char in '\\:*?"<>|' for char in part)
            or unicodedata.normalize("NFC", part) != part
        ):
            raise SourceRefError("artifact member has an unsafe or nonportable path")
        try:
            part.encode("utf-8")
        except UnicodeError:
            raise SourceRefError("artifact member path is not UTF-8") from None


def validate_member_set(paths: list[str], *, depth: int = 32) -> None:
    """Reject ambiguous names, including directory aliases, at a package boundary."""
    spellings: dict[str, str] = {}
    files: set[str] = set()
    for path in paths:
        validate_member_path(path, depth=depth)
        if path in files:
            raise SourceRefError("artifact package contains duplicate member paths")
        files.add(path)
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            key = prefix.casefold()
            if key in spellings and spellings[key] != prefix:
                raise SourceRefError("artifact package contains portable-path collisions")
            spellings[key] = prefix
    for path in files:
        if any("/".join(path.split("/")[:index]) in files for index in range(1, len(path.split("/")))):
            raise SourceRefError("artifact package has a file/directory collision")


def validate_artifact_source(source: str) -> SourceRef:
    """Validate operator-authored references without exposing credentials in errors."""
    if source.startswith("git::") and "?" in source:
        query = source.rsplit("?", 1)[1]
        if not query.startswith("ref=") or "&" in query or "?" in query or "#" in query:
            raise SourceRefError("artifact Git sources accept only a single ref query parameter")
    try:
        result = parse_source_ref(source)
    except SourceRefError:
        raise SourceRefError("invalid artifact source reference") from None
    if result.kind == "git":
        if result.path.startswith("https://"):
            url = urlsplit(result.path)
            if not url.hostname or url.username or url.password or url.query or url.fragment:
                raise SourceRefError("artifact Git source must use a credential-free repository URL")
        elif not re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", result.path):
            raise SourceRefError("invalid credential-free artifact Git source")
        if result.subpath:
            validate_member_path(result.subpath)
    return result


class PackageCapture:
    """One bounded acquisition operation, caching each repository/ref resolution.

    Entering the context creates private temporary storage. Exiting always removes
    it. Callers retain only immutable captured bytes and credential-free provenance.
    """

    def __init__(self, limits: CaptureLimits = DEFAULT_CAPTURE_LIMITS) -> None:
        self.limits = limits
        self._deadline = time.monotonic() + limits.seconds
        self._temporary = tempfile.TemporaryDirectory(prefix="agentworks-artifacts-")
        self.root = Path(self._temporary.name)
        self._repositories: dict[tuple[str, str], tuple[Path, str]] = {}
        self._members = 0
        self._bytes = 0

    def __enter__(self) -> PackageCapture:
        return self

    def __exit__(self, *args: object) -> None:
        self._temporary.cleanup()

    def check(self) -> None:
        if time.monotonic() >= self._deadline:
            raise SourceRefError("artifact capture exceeded its time limit")

    def account(self, size: int) -> None:
        self.check()
        self._members += 1
        self._bytes += size
        if size > self.limits.member_bytes or self._bytes > self.limits.total_bytes:
            raise SourceRefError("artifact capture exceeded its content size limit")
        if self._members > self.limits.members:
            raise SourceRefError("artifact capture exceeded its member count limit")

    def capture(self, source: str) -> CapturedPackage:
        """Capture operator-authored source and untrusted package members."""
        reference = validate_artifact_source(source)
        try:
            package = self._git(reference) if reference.kind == "git" else self._local(reference)
            validate_member_set([member.path for member in package.members], depth=self.limits.depth)
            return package
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            raise SourceRefError("could not capture artifact source") from None

    def _local(self, reference: SourceRef) -> CapturedPackage:
        path = Path(os.path.abspath(Path(reference.path).expanduser()))
        if sys.platform == "win32":
            return self._local_windows(path)
        result: list[PackageMember] = []
        before: dict[str, tuple[int, int, int, int, int, int]] = {}
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

        def visit(parent: int, name: str, relative: str, *, verify: bool = False) -> None:
            self.check()
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise SourceRefError("artifact packages require regular files and directories")
            if relative:
                validate_member_path(relative, depth=self.limits.depth)
            stamp = _stamp(info)
            if verify:
                if before.get(relative) != stamp:
                    raise SourceRefError("artifact source changed during capture")
            else:
                before[relative] = stamp
                if len(before) > self.limits.members * 2 + 1:
                    raise SourceRefError("artifact capture exceeded its directory entry limit")
            descriptor = os.open(name, flags, dir_fd=parent)
            try:
                if _stamp(os.fstat(descriptor)) != stamp:
                    raise SourceRefError("artifact source changed during capture")
                if stat.S_ISDIR(info.st_mode):
                    children: list[str] = []
                    with os.scandir(descriptor) as entries:
                        for entry in entries:
                            self.check()
                            children.append(entry.name)
                            if len(children) > self.limits.members * 2:
                                raise SourceRefError("artifact capture exceeded its directory entry limit")
                    for child in sorted(children):
                        visit(descriptor, child, f"{relative}/{child}" if relative else child, verify=verify)
                elif not verify:
                    self.account(info.st_size)
                    with os.fdopen(os.dup(descriptor), "rb") as stream:
                        data = stream.read(self.limits.member_bytes + 1)
                    if len(data) != info.st_size:
                        raise SourceRefError("artifact source changed during capture")
                    result.append(PackageMember(relative, data, bool(info.st_mode & 0o111)))
                if (
                    _stamp(os.fstat(descriptor)) != stamp
                    or _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != stamp
                ):
                    raise SourceRefError("artifact source changed during capture")
            finally:
                os.close(descriptor)

        # Keep every ancestor anchored until the complete package has been rechecked.
        # O_NOFOLLOW on a full pathname only protects its final component.
        ancestor_flags = getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY)) | os.O_NOFOLLOW | os.O_DIRECTORY
        with ExitStack() as handles:
            parent = os.open(path.anchor, ancestor_flags)
            handles.callback(os.close, parent)
            ancestors: list[tuple[int, str, tuple[int, int, int]]] = []
            for name in path.parts[1:-1]:
                self.check()
                descriptor = os.open(name, ancestor_flags, dir_fd=parent)
                handles.callback(os.close, descriptor)
                ancestors.append((parent, name, _stamp(os.fstat(descriptor))[:3]))
                parent = descriptor
            name = path.name or "."
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            relative = "" if stat.S_ISDIR(info.st_mode) else name
            visit(parent, name, relative)
            visit(parent, name, relative, verify=True)
            for ancestor, name, identity in ancestors:
                self.check()
                if _stamp(os.stat(name, dir_fd=ancestor, follow_symlinks=False))[:3] != identity:
                    raise SourceRefError("artifact source changed during capture")
        return CapturedPackage(tuple(result), str(path))

    def _local_windows(self, path: Path) -> CapturedPackage:
        from agentworks._package_source_windows import locked_file

        before: dict[str, tuple[int, int, int, int, int, int]] = {}
        result: list[PackageMember] = []

        def visit(current: Path, relative: str, *, verify: bool = False) -> None:
            self.check()
            # All parent components remain locked while opening this final component.
            with locked_file(current) as descriptor:
                info = os.fstat(descriptor)
                stamp = _stamp(info)
                if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                    raise SourceRefError("artifact packages require regular files and directories")
                if relative:
                    validate_member_path(relative, depth=self.limits.depth)
                if verify:
                    if before.get(relative) != stamp:
                        raise SourceRefError("artifact source changed during capture")
                else:
                    before[relative] = stamp
                    if len(before) > self.limits.members * 2 + 1:
                        raise SourceRefError("artifact capture exceeded its directory entry limit")
                if stat.S_ISDIR(info.st_mode):
                    children: list[str] = []
                    with os.scandir(current) as entries:
                        for entry in entries:
                            self.check()
                            children.append(entry.name)
                            if len(children) > self.limits.members * 2:
                                raise SourceRefError("artifact capture exceeded its directory entry limit")
                    for child in sorted(children):
                        visit(current / child, f"{relative}/{child}" if relative else child, verify=verify)
                elif not verify:
                    self.account(info.st_size)
                    with os.fdopen(os.dup(descriptor), "rb") as stream:
                        data = stream.read(self.limits.member_bytes + 1)
                    if len(data) != info.st_size:
                        raise SourceRefError("artifact source changed during capture")
                    result.append(PackageMember(relative, data, bool(info.st_mode & 0o111)))
                if _stamp(os.fstat(descriptor)) != stamp:
                    raise SourceRefError("artifact source changed during capture")

        with ExitStack() as handles:
            current = Path(path.anchor)
            handles.enter_context(locked_file(current))
            for name in path.parts[1:-1]:
                self.check()
                current /= name
                descriptor = handles.enter_context(locked_file(current))
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise SourceRefError("artifact source ancestor is not a directory")
            with locked_file(path) as descriptor:
                relative = "" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else path.name
                visit(path, relative)
                visit(path, relative, verify=True)
        return CapturedPackage(tuple(result), str(path))

    def _run(self, repository: Path, *args: str, output_limit: int | None = None) -> bytes:
        self.check()
        maximum = self.limits.member_bytes if output_limit is None else output_limit
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "fetch.recurseSubmodules=false",
            "-C",
            str(repository),
            *args,
        ]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_REPLACE_REF_BASE",
            )
        }
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        with tempfile.TemporaryFile(dir=self.root) as stdout, tempfile.TemporaryFile(dir=self.root) as stderr:
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, env=environment, start_new_session=os.name == "posix"
            )
            try:
                while process.poll() is None:
                    self.check()
                    size = sum(item.stat().st_size for item in self.root.rglob("*") if item.is_file())
                    if (
                        size > self.limits.storage_bytes
                        or os.fstat(stdout.fileno()).st_size > maximum
                        or os.fstat(stderr.fileno()).st_size > self.limits.member_bytes
                    ):
                        raise SourceRefError("artifact Git acquisition exceeded its storage limit")
                    time.sleep(0.01)
                self.check()
                size = sum(item.stat().st_size for item in self.root.rglob("*") if item.is_file())
                if size > self.limits.storage_bytes:
                    raise SourceRefError("artifact Git acquisition exceeded its storage limit")
                if process.returncode:
                    raise SourceRefError("artifact Git acquisition failed; check workstation access and revision")
                stdout.seek(0)
                data = stdout.read(maximum + 1)
                if len(data) > maximum:
                    raise SourceRefError("artifact Git output exceeded its size limit")
                return data
            finally:
                if process.poll() is None:
                    if sys.platform != "win32":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=5,
                                check=False,
                            )
                        except (OSError, subprocess.TimeoutExpired):
                            process.kill()
                process.wait()

    def _git(self, reference: SourceRef) -> CapturedPackage:
        key = (reference.path, reference.ref)
        if key not in self._repositories:
            repository = self.root / str(len(self._repositories))
            repository.mkdir()
            self._run(repository, "init", "--bare", "--template=")
            self._run(
                repository,
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                reference.path,
                reference.ref or "HEAD",
            )
            commit = self._run(repository, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
            self._repositories[key] = repository, commit
        repository, commit = self._repositories[key]
        selected = f"{commit}:{reference.subpath}" if reference.subpath else f"{commit}^{{tree}}"
        object_type = self._run(repository, "cat-file", "-t", selected).strip()
        result: list[PackageMember] = []
        if object_type == b"blob":
            # Inspect the committed mode as well as the blob, so selecting a link
            # directly cannot bypass the same rejection applied within a tree.
            listing = self._run(repository, "ls-tree", "-z", commit, "--", reference.subpath)
            rows = listing.split(b"\0")[:-1]
            if len(rows) != 1:
                raise SourceRefError("artifact source must select one package")
            entries = [(PurePosixPath(reference.subpath).name, rows[0].split(b"\t", 1)[0])]
        elif object_type == b"tree":
            listing = self._run(repository, "ls-tree", "-r", "-z", selected, output_limit=self.limits.members * 1024)
            entries = [
                (row.split(b"\t", 1)[1].decode("utf-8"), row.split(b"\t", 1)[0]) for row in listing.split(b"\0") if row
            ]
        else:
            raise SourceRefError("artifact source must select a file or package tree")
        if len(entries) > self.limits.members:
            raise SourceRefError("artifact capture exceeded its member count limit")
        for path, header in entries:
            mode, kind, object_id = header.split(b" ")
            validate_member_path(path, depth=self.limits.depth)
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise SourceRefError("artifact Git packages cannot contain links or submodules")
            object_name = object_id.decode("ascii")
            size = int(self._run(repository, "cat-file", "-s", object_name))
            self.account(size)
            data = self._run(repository, "cat-file", "blob", object_name)
            if data.startswith(b"version https://git-lfs.github.com/spec/v1\n") or data.startswith(
                b"version https://git-lfs.github.com/spec/v1\r\n"
            ):
                raise SourceRefError("artifact Git package contains an unresolved Git LFS pointer")
            result.append(PackageMember(path, data, mode == b"100755"))
        return CapturedPackage(tuple(result), reference.path, reference.subpath, reference.ref, commit)


def _stamp(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns
