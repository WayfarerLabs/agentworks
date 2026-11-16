"""The socket guard of ``as_socket_owner_line``, run by a real ``/bin/sh``.

Without ``switch_user`` the line needs no root: the guard runs as this test's
user and, when it passes, runs the command. A ``stat`` stand-in on ``PATH``
reports an owner name with a quote and a space, so the owner comparison is
checked through the shell's quoting as well as against a real socket.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.errors import ExternalError
from agentworks.sessions.manager._teardown import _remove_stale_socket
from agentworks.sessions.tmux import SOCKET_GUARD_REFUSED, as_socket_owner_line
from tests.conftest import current_user_name

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="runs the guard under a Linux /bin/sh")

ODD_OWNER = "o'b c"


@pytest.fixture
def root() -> Iterator[Path]:
    # Socket paths must fit sun_path, which pytest's nested tmp_path can exceed.
    directory = Path(tempfile.mkdtemp(prefix="agw-guard-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def run(root: Path, path: Path, owner: str, *, path_dirs: str | None = None) -> tuple[int, bool]:
    """Run the guarded line for ``path`` and ``owner``; its status, and whether the command ran."""
    marker = root / "ran"
    marker.unlink(missing_ok=True)
    line = as_socket_owner_line(f": > {marker}", socket_path=str(path), owner=owner, switch_user=False)
    environment = {"PATH": path_dirs or os.defpath}
    completed = subprocess.run(["/bin/sh", "-c", line], env=environment, capture_output=True, check=False)
    return completed.returncode, marker.exists()


def bound_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    return listener


def test_a_missing_path_runs_the_command(root: Path) -> None:
    assert run(root, root / "missing.sock", current_user_name()) == (0, True)


def test_a_socket_the_owner_owns_runs_the_command(root: Path) -> None:
    with bound_socket(root / "s.sock"):
        assert run(root, root / "s.sock", current_user_name()) == (0, True)


def test_a_socket_another_account_owns_is_refused(root: Path) -> None:
    with bound_socket(root / "s.sock"):
        assert run(root, root / "s.sock", "someone-else") == (SOCKET_GUARD_REFUSED, False)


def test_a_link_to_the_owners_socket_is_refused(root: Path) -> None:
    with bound_socket(root / "s.sock"):
        (root / "link.sock").symlink_to(root / "s.sock")
        assert run(root, root / "link.sock", current_user_name()) == (SOCKET_GUARD_REFUSED, False)


def test_a_regular_file_is_refused(root: Path) -> None:
    (root / "file.sock").write_text("")
    assert run(root, root / "file.sock", current_user_name()) == (SOCKET_GUARD_REFUSED, False)


def test_an_owner_name_with_a_quote_and_a_space_is_compared_whole(root: Path) -> None:
    """Through a ``stat`` that reports that name, the owner matches only itself, never by word or by injection."""
    stand_in = root / "bin"
    stand_in.mkdir()
    (stand_in / "stat").write_text(f"#!/bin/sh\nprintf '%s\\n' \"{ODD_OWNER}\"\n")
    (stand_in / "stat").chmod(0o755)
    path_dirs = f"{stand_in}:{os.defpath}"
    with bound_socket(root / "s.sock"):
        assert run(root, root / "s.sock", ODD_OWNER, path_dirs=path_dirs) == (0, True)
        assert run(root, root / "s.sock", "o'b", path_dirs=path_dirs) == (SOCKET_GUARD_REFUSED, False)
        assert run(root, root / "s.sock", current_user_name(), path_dirs=path_dirs) == (SOCKET_GUARD_REFUSED, False)


def test_a_refusal_with_a_reason_says_so_on_stderr_and_one_without_stays_silent(root: Path) -> None:
    (root / "file.sock").write_text("")
    for refusal, says in ((None, False), ("not attaching", True)):
        line = as_socket_owner_line(
            "true", socket_path=str(root / "file.sock"), owner=current_user_name(), switch_user=False, refusal=refusal
        )
        completed = subprocess.run(["/bin/sh", "-c", line], capture_output=True, check=False)
        assert (completed.returncode, bool(completed.stderr.strip())) == (SOCKET_GUARD_REFUSED, says)


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def test_a_refused_check_after_removal_is_never_read_as_removed(root: Path) -> None:
    """A link planted where the removed socket was is a refusal, not the absence removal must prove."""
    path = root / "s.sock"
    calls: list[str] = []

    def run_command(command: str, *, check: bool = True, env: dict[str, str] | None = None) -> _Result:
        if calls:
            path.symlink_to(root / "elsewhere")
        calls.append(command)
        line = as_socket_owner_line(command, socket_path=str(path), owner=current_user_name(), switch_user=False)
        done = subprocess.run(["/bin/sh", "-c", line], capture_output=True, text=True, check=False)
        return _Result(done.stdout, done.stderr, done.returncode)

    with bound_socket(path), pytest.raises(ExternalError, match="could not verify"):
        _remove_stale_socket(session_name="s", socket_path=str(path), run_command=run_command)
    assert len(calls) == 2
