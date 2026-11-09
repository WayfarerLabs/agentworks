"""The guest file program against a real local filesystem.

Each operation runs the program's ``main`` in its own interpreter, under each
interpreter the L lane uses, with its lock in a sticky, world-writable directory
of the test's own, so operations and the test can contend. Linux only: the
program walks with ``O_PATH``.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import file_program, files
from tests.execution.conformance.lanes import python_interpreters, sudo_granted, unavailable

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file program walks paths with Linux's O_PATH")

_MAIN = (
    "import sys; sys.path.insert(0, sys.argv[1]); import file_program\n"
    "{patch}\n"
    "sys.exit(file_program.main(sys.argv[4:], lock_path=sys.argv[2], lock_wait=float(sys.argv[3])))"
)


@dataclass
class Guest:
    """The program, its lock and a working tree under one test's directory."""

    root: Path
    python: str
    lock_wait: float = 5.0

    @property
    def lock(self) -> Path:
        return self.root / "lock" / "agentworks-files.lock"

    @property
    def tree(self) -> Path:
        return self.root / "tree"

    def start(
        self,
        operation: str,
        params: dict,
        *,
        stdin: bytes = b"",
        patch: str = "",
        sudo: bool = False,
        umask: int | None = None,
    ) -> subprocess.Popen[bytes]:
        argv = [
            self.python,
            "-I",
            "-S",
            "-c",
            _MAIN.format(patch=patch),
            str(self.root / "program"),
            str(self.lock),
            str(self.lock_wait),
            operation,
            json.dumps(params),
        ]
        process = subprocess.Popen(
            ["sudo", "-n", "--", *argv] if sudo else argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=None if umask is None else (lambda: os.umask(umask)),  # noqa: PLW1509
        )
        assert process.stdin is not None
        process.stdin.write(stdin)
        process.stdin.close()
        return process

    def run(self, operation: str, params: dict, **options: object) -> subprocess.CompletedProcess[bytes]:
        process = self.start(operation, params, **options)  # type: ignore[arg-type]
        assert process.stdout is not None and process.stderr is not None
        # The program's streams are small, so reading one and then the other cannot stall it.
        stdout, stderr = process.stdout.read(), process.stderr.read()
        process.wait(timeout=30)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def write_if(
        self, path: Path, data: bytes, *, expected: str | None = None, **overrides: object
    ) -> subprocess.CompletedProcess[bytes]:
        params = {
            "path": str(path),
            "expected": expected,
            "mode": 0o600,
            "group": None,
            "parent_mode": 0o700,
            "preserve": False,
            "size": len(data),
            "sha256": digest(data),
        }
        options = {key: overrides.pop(key) for key in ("patch", "sudo", "umask") if key in overrides}
        return self.run("write_if", {**params, **overrides}, stdin=data, **options)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def temporaries(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.startswith(".agentworks-tmp-"))


@pytest.fixture(params=sorted(python_interpreters()))
def guest(request: pytest.FixtureRequest, tmp_path: Path) -> Guest:
    program = tmp_path / "program"
    program.mkdir()
    (program / "file_program.py").write_text(files.program_source())
    (tmp_path / "lock").mkdir()
    (tmp_path / "lock").chmod(0o1777)
    (tmp_path / "tree").mkdir()
    return Guest(tmp_path, python_interpreters()[request.param])


@pytest.fixture
def as_root(guest: Guest) -> Iterator[None]:
    """The sudo lane: required where passwordless sudo is granted (CI), skipped elsewhere; root's files go after."""
    if not sudo_granted():
        unavailable("the sudo lane needs passwordless sudo")
    yield
    subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(guest.root)], check=False)


# -- Conditional writes ------------------------------------------------------------------


def test_write_if_creates_parents_with_the_requested_modes_and_reads_back(guest: Guest) -> None:
    destination = guest.tree / "a" / "b" / "file"

    written = guest.write_if(destination, b"content", mode=0o640, parent_mode=0o750)

    assert written.returncode == 0, written.stderr
    assert destination.read_bytes() == b"content"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert stat.S_IMODE((guest.tree / "a").stat().st_mode) == 0o750
    read = guest.run("read", {"path": str(destination)})
    assert (read.returncode, read.stdout) == (0, b"content")
    observed = guest.run("stat", {"path": str(destination)})
    assert json.loads(observed.stdout) == {"kind": "file", "mode": 0o640, "digest": digest(b"content")}


def test_a_destination_changed_after_it_was_read_is_a_conflict_and_keeps_the_new_bytes(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"observed")
    destination.write_bytes(b"changed by another writer")

    replaced = guest.write_if(destination, b"replacement", expected=digest(b"observed"))
    removed = guest.run("remove_if", {"path": str(destination), "expected": digest(b"observed")})

    assert (replaced.returncode, removed.returncode) == (file_program.EXIT_CONFLICT, file_program.EXIT_CONFLICT)
    assert destination.read_bytes() == b"changed by another writer"
    assert temporaries(guest.tree) == []


def test_two_concurrent_writes_with_one_expectation_have_exactly_one_winner(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"start")
    params = {"expected": digest(b"start"), "mode": 0o600, "group": None, "parent_mode": 0o700, "preserve": False}
    contenders = [
        guest.start("write_if", {"path": str(destination), **params, "size": 1, "sha256": digest(data)}, stdin=data)
        for data in (b"a", b"b")
    ]

    statuses = sorted(process.wait(timeout=30) for process in contenders)

    assert statuses == [0, file_program.EXIT_CONFLICT]
    assert destination.read_bytes() in (b"a", b"b")


def test_remove_if_removes_only_the_expected_file_and_an_absent_one_is_a_conflict(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"owned")

    removed = guest.run("remove_if", {"path": str(destination), "expected": digest(b"owned")})
    again = guest.run("remove_if", {"path": str(destination), "expected": digest(b"owned")})

    assert (removed.returncode, again.returncode) == (0, file_program.EXIT_CONFLICT)
    assert not destination.exists()


@pytest.mark.parametrize("damage", ["short", "different"])
def test_content_that_did_not_arrive_as_sent_publishes_nothing(guest: Guest, damage: str) -> None:
    destination = guest.tree / "new" / "file"
    sent = b"the whole content"
    arrived = sent[:5] if damage == "short" else b"the whole c0ntent"

    result = guest.run(
        "write_if",
        {
            "path": str(destination),
            "expected": None,
            "mode": 0o600,
            "group": None,
            "parent_mode": 0o700,
            "preserve": False,
            "size": len(sent),
            "sha256": digest(sent),
        },
        stdin=arrived,
    )

    assert result.returncode == file_program.EXIT_CORRUPT_INPUT
    assert os.listdir(guest.tree) == []


# -- Links, types and permissions ----------------------------------------------------------


def _all_operations(path: Path) -> list[tuple[str, dict, bytes]]:
    data = b"replacement"
    write = {
        "path": str(path),
        "expected": None,
        "mode": 0o600,
        "group": None,
        "parent_mode": 0o700,
        "preserve": False,
        "size": len(data),
        "sha256": digest(data),
    }
    return [
        ("read", {"path": str(path)}, b""),
        ("stat", {"path": str(path)}, b""),
        ("write_if", write, data),
        ("remove_if", {"path": str(path), "expected": digest(b"target")}, b""),
        ("ensure_directory", {"path": str(path), "mode": 0o700}, b""),
        ("prune_empty_parents", {"path": str(path / "member"), "root": str(path)}, b""),
    ]


@pytest.mark.parametrize("where", ["destination", "parent"])
def test_a_link_at_the_destination_or_a_parent_is_refused_by_every_operation(guest: Guest, where: str) -> None:
    target = guest.root / "target"
    target.mkdir()
    (target / "file").write_bytes(b"target")
    if where == "destination":
        (guest.tree / "link").symlink_to(target / "file")
        path = guest.tree / "link"
    else:
        (guest.tree / "link").symlink_to(target, target_is_directory=True)
        path = guest.tree / "link" / "file"

    for operation, params, stdin in _all_operations(path):
        result = guest.run(operation, params, stdin=stdin)
        assert result.returncode == file_program.EXIT_UNSUITABLE, (operation, result.stderr)

    assert sorted(os.listdir(target)) == ["file"]
    assert (target / "file").read_bytes() == b"target"


def test_stat_reports_a_directory_and_a_fifo_is_refused_without_blocking(guest: Guest) -> None:
    (guest.tree / "directory").mkdir()
    os.mkfifo(guest.tree / "fifo")

    directory = guest.run("stat", {"path": str(guest.tree / "directory")})
    fifo_stat = guest.run("stat", {"path": str(guest.tree / "fifo")})
    fifo_read = guest.run("read", {"path": str(guest.tree / "fifo")})

    assert json.loads(directory.stdout) == {"kind": "directory", "mode": None, "digest": None}
    assert (fifo_stat.returncode, fifo_read.returncode) == (file_program.EXIT_UNSUITABLE, file_program.EXIT_UNSUITABLE)


def test_reads_of_absent_paths_and_parents(guest: Guest) -> None:
    for path in (guest.tree / "absent", guest.tree / "missing" / "absent"):
        assert guest.run("read", {"path": str(path)}).returncode == file_program.EXIT_ABSENT
        observed = guest.run("stat", {"path": str(path)})
        assert json.loads(observed.stdout)["kind"] == "absent"


def test_a_search_only_ancestor_does_not_block_a_write(guest: Guest) -> None:
    ancestor = guest.tree / "search-only"
    (ancestor / "inner").mkdir(parents=True)
    ancestor.chmod(0o100)
    try:
        written = guest.write_if(ancestor / "inner" / "file", b"content")
        read = guest.run("read", {"path": str(ancestor / "inner" / "file")})
    finally:
        ancestor.chmod(0o700)

    assert written.returncode == 0, written.stderr
    assert read.stdout == b"content"


# -- The lock ---------------------------------------------------------------------------------


def test_the_lock_file_is_created_world_readable_whatever_the_umask(guest: Guest) -> None:
    result = guest.run("ensure_directory", {"path": str(guest.tree / "made"), "mode": 0o700}, umask=0o077)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(guest.lock.stat().st_mode) == 0o644


def test_a_link_at_the_lock_path_is_refused_and_not_followed(guest: Guest) -> None:
    target = guest.root / "lock-target"
    guest.lock.symlink_to(target)

    result = guest.run("ensure_directory", {"path": str(guest.tree / "made"), "mode": 0o700})

    assert result.returncode == file_program.EXIT_UNSUITABLE
    assert result.stderr.strip() == file_program.LOCK_REFUSED.encode()
    assert not target.exists() and not (guest.tree / "made").exists()


def test_a_held_lock_times_out_creating_nothing_while_reads_proceed(guest: Guest) -> None:
    import fcntl

    existing = guest.tree / "existing"
    existing.write_bytes(b"content")
    guest.lock.touch()
    guest.lock_wait = 0.2
    with guest.lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        written = guest.write_if(guest.tree / "new" / "file", b"content")
        read = guest.run("read", {"path": str(existing)})
        observed = guest.run("stat", {"path": str(existing)})

    assert written.returncode == file_program.EXIT_LOCK_TIMEOUT
    assert sorted(os.listdir(guest.tree)) == ["existing"]
    assert (read.returncode, read.stdout) == (0, b"content")
    assert observed.returncode == 0


def test_a_lock_another_identity_created_is_opened_not_recreated(guest: Guest, as_root: None) -> None:
    created = guest.run("ensure_directory", {"path": str(guest.root / "by-root"), "mode": 0o755}, sudo=True)
    assert created.returncode == 0, created.stderr
    assert guest.lock.stat().st_uid == 0

    result = guest.write_if(guest.tree / "file", b"content")

    assert result.returncode == 0, result.stderr
    assert guest.lock.stat().st_uid == 0


# -- Metadata -----------------------------------------------------------------------------------


def test_preserve_keeps_the_existing_mode_and_extended_attributes(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    destination.chmod(0o640)
    try:
        os.setxattr(destination, "user.agentworks-test", b"kept")
    except OSError:
        pytest.skip("this filesystem keeps no user extended attributes")

    result = guest.write_if(destination, b"after", expected=digest(b"before"), mode=0o600, preserve=True)

    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == b"after"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert os.getxattr(destination, "user.agentworks-test") == b"kept"


def test_preserve_keeps_the_existing_owner_and_group(guest: Guest, as_root: None) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    owner = destination.stat()

    result = guest.write_if(destination, b"after", expected=digest(b"before"), preserve=True, sudo=True)

    assert result.returncode == 0, result.stderr
    replaced = destination.stat()
    assert (replaced.st_uid, replaced.st_gid) == (owner.st_uid, owner.st_gid)


def test_an_elevated_write_without_preserve_creates_a_root_owned_file(guest: Guest, as_root: None) -> None:

    result = guest.write_if(guest.tree / "root" / "file", b"content", mode=0o644, parent_mode=0o755, sudo=True)

    assert result.returncode == 0, result.stderr
    assert (guest.tree / "root" / "file").stat().st_uid == 0
    assert (guest.tree / "root").stat().st_uid == 0


# -- Cleanup --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        "conflict",
        "rename",
        "attribute copy",
    ],
)
def test_a_failed_write_leaves_no_temporary_file_but_keeps_created_parents_and_others(
    guest: Guest, failure: str
) -> None:
    parent = guest.tree / "created"
    destination = parent / "file"
    unrelated = guest.tree / ".agentworks-tmp-unrelated"
    unrelated.write_bytes(b"not ours")
    patch = ""
    expected: str | None = None
    preserve = False
    if failure == "conflict":
        expected = digest(b"something else")
    elif failure == "rename":
        patch = "import os\ndef rename(*a, **k): raise PermissionError(13, 'denied')\nos.rename = rename"
    else:
        parent.mkdir()
        destination.write_bytes(b"before")
        expected, preserve = digest(b"before"), True
        patch = "import os\ndef listxattr(*a, **k): raise OSError(95, 'unsupported')\nos.listxattr = listxattr"

    result = guest.write_if(destination, b"after", expected=expected, preserve=preserve, patch=patch)

    assert result.returncode != 0
    assert parent.is_dir()
    assert temporaries(parent) == []
    assert unrelated.read_bytes() == b"not ours"
    if failure == "attribute copy":
        assert destination.read_bytes() == b"before"


def test_a_termination_during_a_write_exits_nonzero_and_leaves_no_temporary_file(guest: Guest) -> None:
    destination = guest.tree / "file"
    marker = guest.root / "writing"
    patch = (
        f"import os, time\ndef fsync(fd):\n    open({str(marker)!r}, 'w').close()\n    time.sleep(30)\nos.fsync = fsync"
    )
    process = guest.start(
        "write_if",
        {
            "path": str(destination),
            "expected": None,
            "mode": 0o600,
            "group": None,
            "parent_mode": 0o700,
            "preserve": False,
            "size": 5,
            "sha256": digest(b"after"),
        },
        stdin=b"after",
        patch=patch,
    )
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=20) == 128 + signal.SIGTERM
    assert not destination.exists()
    assert temporaries(guest.tree) == []


# -- Pruning ---------------------------------------------------------------------------------------


def test_prune_removes_empty_parents_up_to_the_root_and_stops_at_a_non_empty_one(guest: Guest) -> None:
    root = guest.tree / "package"
    (root / "a" / "b").mkdir(parents=True)
    (root / "keep").write_bytes(b"other member")

    result = guest.run("prune_empty_parents", {"path": str(root / "a" / "b" / "file"), "root": str(root)})

    assert result.returncode == 0, result.stderr
    assert sorted(os.listdir(root)) == ["keep"]


def test_prune_keeps_a_verified_empty_root_whose_parent_denies_removal(guest: Guest) -> None:
    holder = guest.tree / "holder"
    root = holder / "package"
    (root / "a").mkdir(parents=True)
    holder.chmod(0o500)
    try:
        result = guest.run("prune_empty_parents", {"path": str(root / "a" / "file"), "root": str(root)})
    finally:
        holder.chmod(0o700)

    assert result.returncode == file_program.EXIT_ROOT_RETAINED, result.stderr
    assert root.is_dir() and not (root / "a").exists()


def test_prune_does_not_report_a_root_that_was_replaced_after_the_walk(guest: Guest) -> None:
    root = guest.tree / "package"
    (root / "a").mkdir(parents=True)
    # The root's own removal finds it replaced by another empty directory and is denied.
    patch = (
        "import os, errno\n"
        "real = os.rmdir\n"
        "def rmdir(name, dir_fd=None):\n"
        "    if name == 'package':\n"
        "        os.rename('package', 'moved', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)\n"
        "        os.mkdir('package', dir_fd=dir_fd)\n"
        "        raise PermissionError(errno.EACCES, 'denied')\n"
        "    return real(name, dir_fd=dir_fd)\n"
        "os.rmdir = rmdir"
    )

    result = guest.run("prune_empty_parents", {"path": str(root / "a" / "file"), "root": str(root)}, patch=patch)

    assert result.returncode not in (0, file_program.EXIT_ROOT_RETAINED)
