"""Fixtures for the L and S lanes, shared by the vectors and the lane-specific tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.execution.conformance.lanes import (
    SHELLS,
    LocalLane,
    SSHDLane,
    SSHDServer,
    python_interpreters,
    rootless_sshd,
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
def local_lane(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalLane:
    shell, label = request.param
    return LocalLane(monkeypatch, tmp_path, shell, python_interpreters()[label])


@pytest.fixture
def sshd_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sshd_server: SSHDServer) -> SSHDLane:
    return SSHDLane(monkeypatch, tmp_path, sshd_server)
