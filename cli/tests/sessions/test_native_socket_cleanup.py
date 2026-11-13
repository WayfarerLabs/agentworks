"""Stale-socket maintenance never treats shell output as a removal authority.

The cleanup runs over the fixture's recording access, whose requests run on
this machine (``LocalFixtureTransport.answer``), elevation ignored. A
``runuser`` shim on the fixture's ``PATH`` records the account each server
probe asks for and runs the probe as the test's own user.
"""

from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import ExternalError, StateError
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, Result
from agentworks.sessions.manager._teardown import (
    DedicatedTeardownPlan,
    _remove_stale_socket,
    execute_dedicated_teardown,
)
from agentworks.sessions.tmux import _cleanup_stale_sockets_under
from tests.native_setup_fixtures import LocalFixtureTransport

if TYPE_CHECKING:
    from agentworks.transports import Transport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest socket and tmux commands")

OWNER = "agt-sockets"


@pytest.fixture
def target() -> Iterator[LocalFixtureTransport]:
    with tempfile.TemporaryDirectory(prefix="agw-r8-sessions-sockets-") as location:
        fixture = LocalFixtureTransport(Path(location))
        shim = fixture.root / "bin" / "runuser"
        shim.parent.mkdir()
        shim.write_text(
            '#!/bin/sh\n[ "$1" = -u ] && [ "$3" = -- ] || exit 64\n'
            f'printf "%s\\n" "$2" >> {shlex.quote(str(fixture.root / "runuser.log"))}\nshift 3\nexec "$@"\n'
        )
        shim.chmod(0o755)
        yield fixture


def cleanup(target: LocalFixtureTransport, directory: str) -> int:
    return _cleanup_stale_sockets_under(target.files(), directory, owner=OWNER)


def probed_as(target: LocalFixtureTransport) -> list[str]:
    log = target.root / "runuser.log"
    return log.read_text().split() if log.exists() else []


def stale(path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))


@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("phase", ["worker-entry", "socket-removal"])
def test_dedicated_worker_preserves_non_socket_paths(
    target: LocalFixtureTransport,
    symlink: bool,
    phase: str,
) -> None:
    candidate = target.root / "selected.sock"
    outside = target.root / "outside.sock"
    stale(outside)
    if symlink:
        candidate.symlink_to(outside)
    else:
        candidate.write_bytes(b"operator content")

    if phase == "worker-entry":
        plan = DedicatedTeardownPlan(
            session_name="selected",
            session_uuid="11111111-1111-4111-8111-111111111111",
            run_id=None,
            vm_name="vm",
            socket_path=str(candidate),
            stored_pid=101,
            stored_boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            stored_start_ticks=201,
            target=cast("Transport", target),
            sudo=False,
            force=True,
        )
        with pytest.raises(StateError):
            execute_dedicated_teardown(plan)
        assert not any("tmux " in command for command in target.commands)
    else:
        with pytest.raises(ExternalError):
            _remove_stale_socket(
                session_name="selected", socket_path=str(candidate), target=cast("Transport", target), sudo=False
            )

    assert outside.is_socket()
    if symlink:
        assert candidate.is_symlink()
    else:
        assert candidate.read_bytes() == b"operator content"


def inventory(*paths: str) -> str:
    return "\nAGW_STALE_SOCKETS_BEGIN\0" + "".join(path + "\0" for path in paths) + "AGW_STALE_SOCKETS_END\0"


def source_of(request: object) -> str:
    if isinstance(request, Script):
        return request.source
    if isinstance(request, Command):
        return shlex.join(request.argv)
    return ""


def intercept(
    target: LocalFixtureTransport,
    monkeypatch: pytest.MonkeyPatch,
    replace: Callable[[str, Callable[[], Result]], Result],
) -> None:
    """Answer each command or script through ``replace(source, run_it)``, which may run it, change it or not."""
    original = target._spawn

    def spawn(argv: list[str], request) -> Result:  # noqa: ANN001
        return replace(source_of(request), lambda: original(argv, request))

    monkeypatch.setattr(target, "_spawn", spawn)


def observe(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, stdout: str, *, returncode: int = 0
) -> None:
    def replace(source: str, run: Callable[[], Result]) -> Result:
        if "find " in source:
            return Result(Completed(returncode), stdout.encode())
        return run()

    intercept(target, monkeypatch, replace)


@pytest.mark.parametrize("banner", ["Welcome\n", "Welcome"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_native_cleanup_retains_banner_file_and_live_server_but_removes_stale_socket(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, banner: str, stream: str
) -> None:
    binary = shutil.which("tmux")
    assert binary is not None, "tmux must be installed for native runtime safety coverage"
    directory = target.root / "s"
    directory.mkdir()
    dead = directory / "dead.sock"
    stale(dead)
    live = directory / "live.sock"
    valuable = target.home / "Welcome"
    valuable.write_bytes(b"operator file")
    native_environment = {**target.environment, "SHELL": "/bin/sh"}
    subprocess.run(
        [binary, "-f", "/dev/null", "-S", str(live), "new-session", "-d", "-s", "agw-r7-native-sockets", "sleep 60"],
        env=native_environment,
        check=True,
        capture_output=True,
    )
    subprocess.run([binary, "-S", str(live), "list-sessions"], env=native_environment, check=True, capture_output=True)

    def replace(source: str, run: Callable[[], Result]) -> Result:
        result = run()
        return Result(
            result.completion,
            (banner.encode() if stream == "stdout" else b"") + result.stdout,
            (banner.encode() if stream == "stderr" else b"") + result.stderr,
        )

    intercept(target, monkeypatch, replace)
    try:
        removed = cleanup(target, str(directory))
        assert valuable.read_bytes() == b"operator file"
        assert removed == 1
        assert not dead.exists()
        assert live.is_socket()
        assert probed_as(target) == [OWNER, OWNER]
        alive = subprocess.run([binary, "-S", str(live), "list-sessions"], env=native_environment, check=False)
        assert alive.returncode == 0
    finally:
        subprocess.run(
            [binary, "-S", str(live), "kill-server"], env=target.environment, check=False, capture_output=True
        )
    assert not list((target.root / "tmp").iterdir())


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "truncated",
        "duplicate-frame",
        "duplicate-path",
        "relative",
        "parent",
        "dot",
        "outside",
        "failed",
        "nested",
        "control",
    ],
)
def test_untrusted_inventory_cannot_authorize_any_removal(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    directory = target.root / "s"
    directory.mkdir()
    dead = directory / "dead.sock"
    stale(dead)
    outside = target.root / "outside.sock"
    stale(outside)
    valuable = target.home / "Welcome"
    valuable.write_bytes(b"operator file")
    payload = {
        "missing": "Welcome\n",
        "truncated": "\nAGW_STALE_SOCKETS_BEGIN\0" + str(dead) + "\0",
        "duplicate-frame": inventory(str(dead)) + inventory(str(dead)),
        "duplicate-path": inventory(str(dead), str(dead)),
        "relative": inventory(str(dead), "Welcome"),
        "parent": inventory(str(dead), str(directory / ".." / "outside.sock")),
        "dot": inventory(str(dead), str(directory) + "/./dead.sock"),
        "outside": inventory(str(dead), str(outside)),
        "failed": inventory(str(dead)),
        "nested": inventory(str(dead), str(directory / "nested" / "dead.sock")),
        "control": inventory(str(dead), str(directory / "bad\nname.sock")),
    }[case]
    observe(target, monkeypatch, payload, returncode=1 if case == "failed" else 0)
    assert cleanup(target, str(directory)) == 0
    assert dead.is_socket() and outside.is_socket()
    assert valuable.read_bytes() == b"operator file"


@pytest.mark.parametrize("symlink", [False, True])
def test_native_type_is_checked_before_tmux_and_before_unlink(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    directory = target.root / "s"
    directory.mkdir()
    candidate = directory / "operator.sock"
    outside = target.root / "outside.sock"
    stale(outside)
    if symlink:
        candidate.symlink_to(outside)
    else:
        candidate.write_bytes(b"operator content")
    observe(target, monkeypatch, inventory(str(candidate)))
    assert cleanup(target, str(directory)) == 0
    assert probed_as(target) == []
    assert outside.is_socket()
    if symlink:
        assert candidate.is_symlink()
    else:
        assert candidate.read_bytes() == b"operator content"


@pytest.mark.parametrize("symlink", [False, True])
def test_native_type_is_rechecked_after_the_tmux_probe(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    directory = target.root / "s"
    directory.mkdir()
    candidate = directory / "dead.sock"
    stale(candidate)
    outside = target.root / "outside.sock"
    stale(outside)

    def replace(source: str, run: Callable[[], Result]) -> Result:
        result = run()
        if "list-sessions" in source:
            candidate.unlink(missing_ok=True)
            if symlink:
                candidate.symlink_to(outside)
            else:
                candidate.write_bytes(b"concurrent operator content")
        return result

    intercept(target, monkeypatch, replace)
    assert cleanup(target, str(directory)) == 0
    assert outside.is_socket()
    if symlink:
        assert candidate.is_symlink()
    else:
        assert candidate.read_bytes() == b"concurrent operator content"


@pytest.mark.parametrize("failure", ["unknown-tmux", "unlink-failed"])
def test_unknown_native_presence_or_failed_unlink_does_not_claim_removal(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    directory = target.root / "s"
    directory.mkdir()
    candidate = directory / "dead.sock"
    stale(candidate)

    def replace(source: str, run: Callable[[], Result]) -> Result:
        if failure == "unknown-tmux" and "list-sessions" in source:
            return Result(Completed(1), b"", b"unclassified native refusal")
        if failure == "unlink-failed" and "rm -f" in source:
            return Result(Completed(1), b"", b"fixture removal denied")
        return run()

    intercept(target, monkeypatch, replace)
    assert cleanup(target, str(directory)) == 0
    assert candidate.is_socket()


@pytest.mark.parametrize("directory", ["relative", "/", "/run/../tmp", "/run//sockets", "/run/./sockets"])
def test_invalid_expected_directory_is_rejected_before_dispatch(target: LocalFixtureTransport, directory: str) -> None:
    files = target.files()
    with pytest.raises(StateError):
        _cleanup_stale_sockets_under(files, directory, owner=OWNER)
    assert files.access.requests() == []  # type: ignore[attr-defined]
