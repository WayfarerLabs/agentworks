"""Owned agent and exact server packet observations for native authentication tests."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest


@dataclass(frozen=True)
class KeyOffer:
    phase: Literal["querying", "attempting"]
    algorithm: str
    blob: str


def key_offers(log: bytes) -> list[KeyOffer]:
    """Observe incoming unsigned queries and signed requests, never acceptance logs.

    OpenSSH logs each wire key before interpreting authorization, at DEBUG2:
    https://github.com/openssh/openssh-portable/blob/V_8_5_P1/auth2-pubkey.c#L107-L120
    Unknown packet-log syntax fails instead of treating it as no offer.
    """
    offers = []
    for line in log.decode("ascii", errors="replace").splitlines():
        if "userauth_pubkey:" not in line or " public key " not in line:
            continue
        match = re.search(
            r"userauth_pubkey: (?:valid|invalid) user \S+ (querying|attempting) public key "
            r"(\S+) ([A-Za-z0-9+/=]+)(?: \[preauth\])?$",
            line,
        )
        assert match is not None, "Unsupported OpenSSH packet observation syntax"
        phase, algorithm, blob = match.groups()
        assert phase in {"querying", "attempting"}
        offers.append(KeyOffer(phase, algorithm, blob))
    return offers


def public_key(path: Path) -> tuple[str, str]:
    """Read only the owned fixture public file, retaining algorithm and wire blob."""
    algorithm, blob, *_ = path.read_text().split()
    return algorithm, blob


@contextmanager
def owned_agent(keys: tuple[Path, ...]) -> Iterator[str]:
    """Own a foreground agent, explicit additions and one short private socket root."""
    agent_binary, add_binary = shutil.which("ssh-agent"), shutil.which("ssh-add")
    if agent_binary is None or add_binary is None:
        pytest.skip("Installed OpenSSH agent and explicit key-addition tool required")
    with tempfile.TemporaryDirectory(prefix="agw-ssh-auth-offer-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        socket = root / "agent"
        with (root / "agent.log").open("wb") as log:
            agent = subprocess.Popen(
                [agent_binary, "-D", "-a", str(socket)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
            try:
                until = time.monotonic() + 3
                while not socket.is_socket():
                    if agent.poll() is not None:
                        pytest.fail("Owned SSH agent exited before socket publication")
                    if time.monotonic() >= until:
                        pytest.fail("Owned SSH agent did not publish its socket")
                    time.sleep(0.01)
                for key in keys:
                    subprocess.run(
                        [add_binary, str(key)],
                        check=True,
                        timeout=10,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env={"SSH_AUTH_SOCK": str(socket), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                    )
                inventory = subprocess.run(
                    [add_binary, "-L"],
                    check=True,
                    timeout=10,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    env={"SSH_AUTH_SOCK": str(socket), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                )
                observed = {tuple(line.split()[:2]) for line in inventory.stdout.decode("ascii").splitlines()}
                assert observed == {public_key(key.with_suffix(".pub")) for key in keys}
                yield str(socket)
            finally:
                if agent.poll() is None:
                    agent.kill()
                agent.wait(timeout=3)
                assert agent.poll() is not None
                # The killed foreground agent may leave its owned socket inode.
                socket.unlink(missing_ok=True)
        assert not socket.exists()
    assert not root.exists()
