"""Fake server startup failures retain owned evidence and never become skips."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust
from tests.execution.carriers.ssh import enrollment_server as fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux enrollment fixture composition")


def test_unexpected_startup_exit_fails_reaps_then_releases_logs_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    retained: list[Any] = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def bind(self, _address):
            if retained:
                assert retained[0].reaped
                assert not retained[0].log.closed
                order.append("port-verified")

        def getsockname(self):
            return "127.0.0.1", 12345

        def setsockopt(self, *_args):
            pass

    class FakeServer:
        returncode = 17
        reaped = False

        def __init__(self, _argv, **options):
            self.log = options["stderr"]
            self.log.write(b"synthetic rejected server configuration\n")
            self.log.flush()
            retained.append(self)
            order.append("constructed")

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            assert timeout == 2 and not self.log.closed
            self.reaped = True
            order.append("reaped")
            return self.returncode

        def kill(self):
            pytest.fail("Already exited fake server was killed")

    def keys(argv, **_kwargs):
        path = Path(argv[-1])
        path.write_bytes(b"synthetic identity")
        path.with_suffix(".pub").write_text("ssh-ed25519 AAAAsynthetic fixture\n")

    monkeypatch.setattr(shutil, "which", lambda binary: sys.executable)
    monkeypatch.setattr(subprocess, "run", keys)
    monkeypatch.setattr(subprocess, "Popen", FakeServer)
    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(fixture, "import_trust", lambda directory, **_kwargs: ManagedSSHTrust(directory))
    with pytest.raises(BaseExceptionGroup) as caught, fixture.enrollment_server(tmp_path, "matching"):
        pytest.fail("An exited fake server became available")
    assert any(isinstance(error, pytest.fail.Exception) for error in caught.value.exceptions)
    assert not any(isinstance(error, pytest.skip.Exception) for error in caught.value.exceptions)
    assert order == ["constructed", "reaped", "port-verified"]
    assert retained[0].log.closed
    assert (tmp_path / "server.log").read_bytes() == b"synthetic rejected server configuration\n"
