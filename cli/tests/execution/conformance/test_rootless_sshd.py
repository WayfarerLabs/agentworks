"""The S lane's own sshd: a start on a given port is ready only once that sshd holds the port."""

from __future__ import annotations

import socket
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.execution.conformance import lanes

if TYPE_CHECKING:
    from pathlib import Path


class Unavailable(Exception):
    """What the lane's missing prerequisite raised, in place of a skip or a CI failure."""


def test_a_port_another_process_holds_is_retried_and_then_reported_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sshd = lanes.find_sshd()
    if sshd is None:
        pytest.skip("the S lane's sshd is not installed")
    starts: list[Any] = []
    real_popen = subprocess.Popen

    def counting_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if argv[0] == sshd:
            starts.append(argv)
        return real_popen(argv, *args, **kwargs)

    def unavailable(message: str) -> None:
        raise Unavailable(message)

    monkeypatch.setattr(lanes, "unavailable", unavailable)
    monkeypatch.setattr(lanes, "RESTART_ATTEMPTS", 2)
    monkeypatch.setattr(lanes.subprocess, "Popen", counting_popen)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]

        with pytest.raises(Unavailable), lanes.rootless_sshd(tmp_path / "sshd", port=port):
            pytest.fail("a plain listener on the port was taken for the lane's sshd")

    assert len(starts) == 2
