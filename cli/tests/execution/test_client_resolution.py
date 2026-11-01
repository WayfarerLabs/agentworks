"""Resolving the OpenSSH client against temporary executables."""

from __future__ import annotations

import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import client
from agentworks.execution.errors import ClientUnsupported

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX shell scripts")


def fake_ssh(path: Path, banner: str, *, name: str = "ssh") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    executable = path / name
    executable.write_text(f"#!/bin/sh\necho '{banner}' >&2\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def posix(path_value: str) -> client.SSHClient:
    return client.resolve(windows=False, environ={"PATH": path_value})


def test_the_first_ssh_along_path_is_resolved_to_an_absolute_path_with_its_version(tmp_path: Path) -> None:
    found = fake_ssh(tmp_path / "bin", "OpenSSH_9.2p1 Debian-2, OpenSSL 3.0.15 3 Sep 2024")

    resolved = posix(str(tmp_path / "bin"))

    assert resolved == client.SSHClient(str(found), (9, 2))
    assert os.path.isabs(resolved.path)


def test_only_the_first_candidate_is_judged(tmp_path: Path) -> None:
    fake_ssh(tmp_path / "old", "OpenSSH_8.4p1")
    fake_ssh(tmp_path / "new", "OpenSSH_9.9p1")

    with pytest.raises(ClientUnsupported) as raised:
        posix(os.pathsep.join([str(tmp_path / "old"), str(tmp_path / "new")]))

    assert str(tmp_path / "old") in str(raised.value)
    assert str(tmp_path / "new") not in str(raised.value)


def test_empty_and_relative_path_elements_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A planted ./ssh must never be found, whatever the working directory.
    fake_ssh(tmp_path / "cwd", "OpenSSH_9.9p1")
    monkeypatch.chdir(tmp_path / "cwd")
    real = fake_ssh(tmp_path / "real", "OpenSSH_9.2p1")

    resolved = posix(os.pathsep.join(["", ".", "cwd", "relative/bin", str(tmp_path / "real")]))

    assert resolved.path == str(real)
    with pytest.raises(ClientUnsupported):
        posix(os.pathsep.join(["", ".", "relative/bin"]))


@pytest.mark.parametrize("banner", ["OpenSSH_8.4p1", "OpenSSH_7.9p1", "Dropbear v2025.88", "garbage", ""])
def test_an_old_or_foreign_client_is_refused_with_the_path_and_the_reason(tmp_path: Path, banner: str) -> None:
    found = fake_ssh(tmp_path, banner)

    with pytest.raises(ClientUnsupported) as raised:
        posix(str(tmp_path))

    assert str(found) in str(raised.value)


@pytest.mark.parametrize("banner", ["OpenSSH_8.5p1", "OpenSSH_10.1", "OpenSSH_9.9p1, LibreSSL 3.3.6"])
def test_supported_banners(tmp_path: Path, banner: str) -> None:
    fake_ssh(tmp_path, banner)

    assert posix(str(tmp_path)).version >= (8, 5)


def test_the_windows_build_is_refused_on_posix_and_required_on_windows(tmp_path: Path) -> None:
    fake_ssh(tmp_path, "OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2")
    with pytest.raises(ClientUnsupported):
        posix(str(tmp_path))

    system = tmp_path / "sys"
    fake_ssh(system / "System32" / "OpenSSH", "OpenSSH_9.2p1", name="ssh.exe")
    with pytest.raises(ClientUnsupported):
        client.resolve(windows=True, environ={"SystemRoot": str(system)})


def test_windows_never_searches_path_and_judges_every_candidate_in_order(tmp_path: Path) -> None:
    git_for_windows = fake_ssh(tmp_path / "git" / "bin", "OpenSSH_9.9p1")
    root = tmp_path / "windows"
    program_files = tmp_path / "programs"
    sysnative = fake_ssh(root / "Sysnative" / "OpenSSH", "OpenSSH_for_Windows_9.5p1", name="ssh.exe")
    inbox = fake_ssh(root / "System32" / "OpenSSH", "OpenSSH_for_Windows_8.1p1", name="ssh.exe")
    msi = fake_ssh(program_files / "OpenSSH", "OpenSSH_for_Windows_9.5p1", name="ssh.exe")
    environ = {"SystemRoot": str(root), "ProgramFiles": str(program_files), "PATH": str(git_for_windows.parent)}

    assert client.candidates(windows=True, environ=environ) == [sysnative, inbox, msi]
    assert client.resolve(windows=True, environ=environ).path == str(sysnative)

    sysnative.unlink()
    # The inbox 8.1 client is judged and refused, so a Windows 10 workstation with the MSI install gets the MSI.
    assert client.candidates(windows=True, environ=environ) == [inbox, msi]
    assert client.resolve(windows=True, environ=environ).path == str(msi)


def test_windows_skips_unset_variables_and_missing_files(tmp_path: Path) -> None:
    assert client.candidates(windows=True, environ={}) == []
    assert client.candidates(windows=True, environ={"SystemRoot": str(tmp_path)}) == []
    with pytest.raises(ClientUnsupported):
        client.resolve(windows=True, environ={"SystemRoot": str(tmp_path)})


def test_an_unsupported_client_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client.resolve_ssh.cache_clear()
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ClientUnsupported):
        client.resolve_ssh()
    found = fake_ssh(tmp_path, "OpenSSH_9.2p1")

    assert client.resolve_ssh().path == str(found)
    client.resolve_ssh.cache_clear()
