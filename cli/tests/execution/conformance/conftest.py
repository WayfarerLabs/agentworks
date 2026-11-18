"""Fixtures for the L and S lanes, shared by the vectors and the lane-specific tests."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import detached, detached_program
from tests.execution.conformance.lanes import (
    SHELLS,
    LocalLane,
    SSHDLane,
    SSHDServer,
    python_interpreters,
    require_linux_guest,
    rootless_sshd,
    sudo_granted,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

L_CASES = [(shell, label) for shell in SHELLS for label in python_interpreters()]


@pytest.fixture(scope="session")
def sshd_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SSHDServer]:
    with rootless_sshd(tmp_path_factory.mktemp("sshd")) as server:
        yield server


@pytest.fixture(params=L_CASES, ids=[f"{shell}-{label}" for shell, label in L_CASES])
def local_lane(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_detached_base: None
) -> LocalLane:
    require_linux_guest()
    shell, label = request.param
    return LocalLane(monkeypatch, tmp_path, shell, python_interpreters()[label])


@pytest.fixture
def sshd_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sshd_server: SSHDServer, private_detached_base: None
) -> SSHDLane:
    return SSHDLane(monkeypatch, tmp_path, sshd_server)


@pytest.fixture(scope="session")
def detached_base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The detached program's base for launches the L and S lanes make on this machine, removed afterwards.

    The program creates the base and its parent as root, so removing them
    needs the same passwordless sudo the vectors that made them needed.
    """
    guest = tmp_path_factory.mktemp("detached-guest")
    yield guest / "agentworks" / "detached"
    shutil.rmtree(guest, ignore_errors=True)
    if guest.exists() and sudo_granted():
        subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(guest)], check=False)


@pytest.fixture
def private_detached_base(detached_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the detached program with its base at :func:`detached_base`, for a lane that runs it on this machine.

    The L and S lanes run the program here as root, where the VM's base would
    be the runner's own ``/var/lib``. The V lane does not request it, so it
    keeps the VM's real base.
    """
    source = detached.program_source()
    declaration = f'BASE = "{detached_program.BASE}"'
    if source.count(declaration) != 1:
        raise RuntimeError(f"the detached program no longer declares {declaration}; update this fixture")
    monkeypatch.setattr(detached, "program_source", lambda: source.replace(declaration, f'BASE = "{detached_base}"'))
    monkeypatch.setattr(detached_program, "BASE", str(detached_base))
