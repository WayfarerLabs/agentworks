"""Stale-socket maintenance never treats shell output as a removal authority."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import StateError
from agentworks.sessions.tmux import _cleanup_stale_sockets_under
from agentworks.ssh import SSHResult
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest socket and tmux commands")


@pytest.fixture
def target() -> Iterator[LocalFixtureTransport]:
    with tempfile.TemporaryDirectory(prefix="agw-r8-sessions-sockets-") as location:
        fixture = LocalFixtureTransport(Path(location))
        yield fixture


def stale(path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))


def inventory(*paths: str) -> str:
    return "\nAGW_STALE_SOCKETS_BEGIN\0" + "".join(path + "\0" for path in paths) + "AGW_STALE_SOCKETS_END\0"


def observe(
    target: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, stdout: str, *, returncode: int = 0
) -> None:
    original = target.run

    def run(command: str, **kwargs: Any) -> SSHResult:
        if "find " in command:
            return SSHResult(returncode, stdout, "")
        return original(command, **kwargs)

    monkeypatch.setattr(target, "run", run)


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
    original = target.run

    def run(command: str, **kwargs: Any) -> SSHResult:
        result = original(command, **kwargs)
        return SSHResult(
            result.returncode,
            (banner if stream == "stdout" else "") + result.stdout,
            (banner if stream == "stderr" else "") + result.stderr,
        )

    monkeypatch.setattr(target, "run", run)
    try:
        removed = _cleanup_stale_sockets_under(target, str(directory))
        assert valuable.read_bytes() == b"operator file"
        assert removed == 1
        assert not dead.exists()
        assert live.is_socket()
        assert original(f"tmux -S {live} list-sessions", check=False).ok
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
    assert _cleanup_stale_sockets_under(target, str(directory)) == 0
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
    assert _cleanup_stale_sockets_under(target, str(directory)) == 0
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
    original = target.run

    def run(command: str, **kwargs: Any) -> SSHResult:
        result = original(command, **kwargs)
        if "list-sessions" in command:
            candidate.unlink(missing_ok=True)
            if symlink:
                candidate.symlink_to(outside)
            else:
                candidate.write_bytes(b"concurrent operator content")
        return result

    monkeypatch.setattr(target, "run", run)
    assert _cleanup_stale_sockets_under(target, str(directory)) == 0
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
    original = target.run

    def run(command: str, **kwargs: Any) -> SSHResult:
        if failure == "unknown-tmux" and "list-sessions" in command:
            return SSHResult(1, "", "unclassified native refusal")
        if failure == "unlink-failed" and "rm -f" in command:
            return SSHResult(1, "", "fixture removal denied")
        return original(command, **kwargs)

    monkeypatch.setattr(target, "run", run)
    assert _cleanup_stale_sockets_under(target, str(directory)) == 0
    assert candidate.is_socket()


@pytest.mark.parametrize("directory", ["relative", "/", "/run/../tmp", "/run//sockets", "/run/./sockets"])
def test_invalid_expected_directory_is_rejected_before_dispatch(target: LocalFixtureTransport, directory: str) -> None:
    with pytest.raises(StateError):
        _cleanup_stale_sockets_under(target, directory)
    assert target.commands == []
