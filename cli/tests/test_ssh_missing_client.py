"""A missing ``ssh`` or ``scp`` is the legacy transport's typed failure, whichever path starts it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks import ssh
from agentworks.execution.trust import HOST_TRUST, ssh_options
from agentworks.ssh import SSHError, SSHTarget
from agentworks.transports.ssh import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _transport() -> SSHTransport:
    return SSHTransport("vm-host", trust=HOST_TRUST)


def _target() -> SSHTarget:
    return SSHTarget(host="vm-host", host_key_options=ssh_options(HOST_TRUST))


@pytest.mark.parametrize(
    "start",
    [
        lambda tmp: ssh.run(_target(), "true"),
        lambda tmp: ssh.run(_target(), "cat > /tmp/x", input_text="secret"),
        lambda tmp: ssh.copy_to(_target(), tmp / "file", "/tmp/file"),
        lambda tmp: _transport().run("true"),
        lambda tmp: _transport().copy_to(tmp / "file", "/tmp/file"),
        lambda tmp: _transport().copy_from("/tmp/file", tmp / "file"),
        lambda tmp: _transport().call_streaming("true"),
        lambda tmp: _transport().interactive("true"),
    ],
    ids=[
        "run",
        "run-with-input",
        "copy-to",
        "transport-run",
        "transport-copy-to",
        "transport-copy-from",
        "transport-streaming",
        "transport-interactive",
    ],
)
def test_a_missing_openssh_program_is_an_ssh_error(
    start: Callable[[Path], object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(SSHError):
        start(tmp_path)
