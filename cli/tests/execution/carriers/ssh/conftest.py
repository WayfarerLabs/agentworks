"""Owned installed-OpenSSH fixtures for SSH carrier tests."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def local_sshd(tmp_path: Path) -> Iterator[SSHConnection]:
    """Own fresh keys and one foreground loopback server for a single test."""
    tmp_path = tmp_path.resolve()
    if sys.platform != "linux":
        pytest.skip("Local unprivileged sshd fixture requires Linux")
    import pwd

    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).is_file() or shutil.which("ssh-keygen") is None or shutil.which("ssh") is None:
        pytest.skip("Installed OpenSSH client, key generator and sshd required")
    identity, host_key = tmp_path / "identity", tmp_path / "host_key"
    for key in (identity, host_key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=10)
    authorized = tmp_path / "authorized"
    authorized.write_bytes(identity.with_suffix(".pub").read_bytes())
    port = _unused_port()
    trust = tmp_path / "trust"
    trust.write_text(f"[127.0.0.1]:{port} " + host_key.with_suffix(".pub").read_text())
    config = tmp_path / "sshd_config"
    config.write_text(
        f'Port {port}\nListenAddress 127.0.0.1\nHostKey "{host_key}"\n'
        f'AuthorizedKeysFile "{authorized}"\nPidFile "{tmp_path / "pid"}"\n'
        "StrictModes no\nUsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "AllowTcpForwarding local\nPermitRootLogin prohibit-password\nPermitUserRC no\nLogLevel ERROR\n"
    )
    server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 3
        while True:
            if server.poll() is not None:
                pytest.skip("Local unprivileged sshd unavailable")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("Owned sshd did not become available")
                time.sleep(0.01)
        yield SSHConnection(
            "127.0.0.1", pwd.getpwuid(os.getuid()).pw_name, identity, SSHTrustFiles((trust,)), port=port
        )
    finally:
        server.kill()
        server.wait(timeout=2)
        assert server.stderr is not None
        server.stderr.close()
