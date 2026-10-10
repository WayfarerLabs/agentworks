"""Executable admission excludes implicit cwd lookup and pins subsequent launch."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, PreparedInvocation
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.ssh import client
from agentworks.execution.carriers.ssh._io import run_process
from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles

pytestmark = pytest.mark.windows


def selectable_client(directory: Path, name: str = "ssh") -> Path:
    """Make a lookup candidate; synthetic launchers do not execute this file."""
    directory.mkdir()
    selected = directory / (name + ".exe" if os.name == "nt" else name)
    selected.write_bytes(b"synthetic executable")
    selected.chmod(0o700)
    return selected


@pytest.fixture
def connection(tmp_path: Path) -> SSHConnection:
    identity, trust = tmp_path / "identity", tmp_path / "trust"
    identity.write_bytes(b"synthetic identity")
    trust.write_bytes(b"synthetic trust")
    return SSHConnection("fixture.invalid", "fixture", identity, SSHTrustFiles((trust,)))


def test_lookup_uses_only_explicit_path_entries(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = selectable_client(tmp_path / "cwd")
    selected = selectable_client(tmp_path / "selected")
    monkeypatch.chdir(decoy.parent)
    monkeypatch.setenv("PATH", str(selected.parent))
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    assert os.path.samefile(client.resolve_client_executable(connection), selected)
    selected.unlink()
    with pytest.raises(ValidationError):
        client.resolve_client_executable(connection)
    monkeypatch.setenv("PATH", ".")
    assert os.path.samefile(client.resolve_client_executable(connection), decoy)


@pytest.mark.parametrize("path", [None, ""])
def test_absent_search_path_does_not_discover_cwd(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str | None
) -> None:
    selected = selectable_client(tmp_path / "cwd")
    monkeypatch.chdir(selected.parent)
    if path is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", path)
    with pytest.raises(ValidationError):
        client.resolve_client_executable(connection)
    assert client.resolve_client_executable(replace(connection, ssh_executable=str(selected))) == str(selected)


def test_relative_path_entry_is_pinned_before_cwd_changes(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = selectable_client(tmp_path / "selected")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "selected")
    pinned = client.resolve_client_executable(connection)
    assert os.path.samefile(pinned, selected)
    assert Path(pinned).is_absolute()


def test_probe_and_delivery_share_one_selection(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    selected = selectable_client(tmp_path / "selected%$ directory")
    replacement = selectable_client(tmp_path / "replacement")
    monkeypatch.setenv("PATH", str(selected.parent))
    calls: list[list[str]] = []
    original = run_process

    def launch(argv: list[str], *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody) -> ProcessResult:
        calls.append(argv)
        if argv[-1] == "-V":
            monkeypatch.setenv("PATH", str(replacement.parent))
            # Run a real owned child while substituting only the fixture executable.
            return original(
                [sys.executable, "-c", "import sys; sys.stderr.write('OpenSSH_9.9p1')"],
                io=io,
                deadline=deadline,
                custody=custody,
            )
        return original([sys.executable, "-c", "pass"], io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(client, "run_process", launch)
    report = client.SSHCarrier(connection).execute(
        PreparedInvocation(("true",)), io=CarrierIO(), deadline=Deadline.after(5), custody=custody
    )
    assert report.dispatch is Dispatch.SENT
    assert len(calls) == 2
    assert os.path.samefile(calls[0][0], selected)
    assert Path(calls[0][0]).is_absolute()
    assert calls[0][0] == calls[1][0]
    assert os.path.samefile(client.resolve_client_executable(connection), replacement)


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows PATH semantics")
@pytest.mark.parametrize("entries", [";{selected}", "{missing};;{selected}", "{selected};", "{missing};"])
def test_windows_empty_path_components_never_select_cwd(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: str
) -> None:
    decoy = selectable_client(tmp_path / "cwd")
    selected = selectable_client(tmp_path / "selected")
    monkeypatch.chdir(decoy.parent)
    monkeypatch.setenv("PATH", entries.format(selected=selected.parent, missing=tmp_path / "missing"))
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    if "{selected}" in entries:
        assert os.path.samefile(client.resolve_client_executable(connection), selected)
    else:
        with pytest.raises(ValidationError):
            client.resolve_client_executable(connection)


@pytest.mark.skipif(os.name == "nt", reason="POSIX empty PATH components select cwd")
def test_posix_empty_path_component_preserves_explicit_search_semantics(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = selectable_client(tmp_path / "cwd")
    monkeypatch.chdir(selected.parent)
    monkeypatch.setenv("PATH", os.pathsep + str(tmp_path / "missing"))
    assert os.path.samefile(client.resolve_client_executable(connection), selected)


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows executable search and OpenSSH")
def test_windows_native_version_probe_never_executes_cwd_decoy(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custody: LocalDeliveryCustody
) -> None:
    selected = client.resolve_client_executable(connection)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    decoy = cwd / "ssh.exe"
    shutil.copyfile(sys.executable, decoy)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(Path(selected).parent))
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    pinned = client.resolve_client_executable(connection)
    assert pinned == selected and pinned != str(decoy)
    assert client.check_client_version(pinned, deadline=Deadline.after(5), custody=custody) is None


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows executable suffix selection")
def test_windows_explicit_path_ignores_pathext(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = selectable_client(tmp_path / "selected")
    monkeypatch.setenv("PATHEXT", ".COM")
    assert client.resolve_client_executable(replace(connection, ssh_executable=str(selected))) == str(selected)
    extensionless = selected.with_suffix("")
    with pytest.raises(ValidationError):
        client.resolve_client_executable(replace(connection, ssh_executable=str(extensionless)))
    extensionless.write_bytes(b"explicit extensionless executable")
    assert client.resolve_client_executable(replace(connection, ssh_executable=str(extensionless))) == str(
        extensionless
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink parent traversal; Windows requires separate native evidence")
def test_path_parent_keeps_symlink_traversal_semantics(
    connection: SSHConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = selectable_client(tmp_path / "selected")
    (selected.parent / "child").mkdir()
    link = tmp_path / "link"
    link.symlink_to(selected.parent / "child", target_is_directory=True)
    monkeypatch.setenv("PATH", str(link / ".."))
    pinned = client.resolve_client_executable(connection)
    assert os.path.samefile(pinned, selected)
    assert Path(pinned).is_absolute()
