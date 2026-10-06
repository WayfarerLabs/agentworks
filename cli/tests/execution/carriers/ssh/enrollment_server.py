"""Owned enrollment server shared by installed-client trust workflow tests."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Event
from typing import Literal

import pytest

from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.enrollment import SSHCreationProvenance
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles, import_trust
from tests.execution.carriers.ssh.fixture_worker import FixtureWorker, fixture_call


@dataclass
class LocalSSH:
    connection: SSHConnection
    provenance: SSHCreationProvenance
    authorized: Path
    host_public_key: bytes
    policy_kind: str


@contextmanager
def enrollment_server(
    tmp_path: Path, scenario: str, *, log_path: Path | None = None, log_level: Literal["ERROR", "DEBUG2"] = "ERROR"
) -> Iterator[LocalSSH]:
    """Own one foreground loopback server and all keys.

    Native use requires a tester-owned account/home or independently demonstrated
    startup-free configured account shell. PermitUserRC does not fence shell hooks.
    """
    if sys.platform != "linux":
        pytest.skip("Local unprivileged sshd fixture requires Linux")
    import pwd

    tmp_path = tmp_path.resolve()
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).is_file() or shutil.which("ssh-keygen") is None or shutil.which("ssh") is None:
        pytest.skip("Installed OpenSSH client, key generator and sshd required")
    identity, host_key, other_key = (tmp_path / name for name in ("identity", "host-key", "other-key"))
    for key in (identity, host_key, other_key):
        fixture_call(
            partial(
                subprocess.run, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=10
            )
        )
    authorized = tmp_path / "authorized"
    authorized.write_bytes(b"" if scenario == "auth_failure" else identity.with_suffix(".pub").read_bytes())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    alias = "owned-fixture-alias" if scenario.endswith("_alias") else None
    lookup = alias or f"[127.0.0.1]:{port}"
    host_public = host_key.with_suffix(".pub").read_text()
    other_public = other_key.with_suffix(".pub").read_text()
    policy = tmp_path / "policy"
    policy.write_text(
        lookup + " " + host_public
        if scenario in {"matching", "hashed", "hashed_alias"}
        else lookup + " " + other_public
        if scenario == "mismatch"
        else "@revoked " + lookup + " " + host_public
        if scenario == "revoked_marker"
        else "@cert-authority " + lookup + " " + other_public
        if scenario in {"ca", "revoked_ca"}
        else ""
    )
    if scenario in {"hashed", "hashed_alias"}:
        fixture_call(
            lambda: subprocess.run(
                ["ssh-keygen", "-q", "-H", "-f", str(policy)],
                check=True,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    certificate_config = ""
    if scenario in {"ca", "revoked_ca"}:
        fixture_call(
            lambda: subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-s",
                    str(other_key),
                    "-I",
                    "fixture-host",
                    "-h",
                    "-n",
                    "127.0.0.1",
                    "-V",
                    "-1m:+5m",
                    str(host_key.with_suffix(".pub")),
                ],
                check=True,
                timeout=10,
            )
        )
        certificate_config = f'HostCertificate "{host_key}-cert.pub"\n'
    if scenario in {"workflow", "workflow_alias"}:
        policy.write_text(
            "# complete fixture policy\n@cert-authority *.unrelated.invalid "
            + other_public
            + "@revoked revoked.unrelated.invalid "
            + other_public
        )
    revoked = None
    if scenario in {"revoked", "revoked_ca", "workflow", "workflow_alias"}:
        revoked = tmp_path / "revocations"
        fixture_call(
            lambda: subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-k",
                    "-f",
                    str(revoked),
                    str(
                        (
                            other_key if scenario in {"revoked_ca", "workflow", "workflow_alias"} else host_key
                        ).with_suffix(".pub")
                    ),
                ],
                check=True,
                timeout=10,
            )
        )
    bundle = import_trust(tmp_path / "managed", sources=SSHTrustFiles((policy,), revoked), authority="fixture")
    config = tmp_path / "sshd_config"
    config.write_text(
        f'Port {port}\nListenAddress 127.0.0.1\nHostKey "{host_key}"\n'
        f'AuthorizedKeysFile "{authorized}"\nPidFile "{tmp_path / "pid"}"\n'
        "StrictModes no\nUsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        f"PermitRootLogin prohibit-password\nPermitUserRC no\nLogLevel {log_level}\n" + certificate_config
    )
    with ExitStack() as logs:
        # Always retain diagnostic evidence without a pipe that can fill at DEBUG2.
        diagnostics_path = log_path or tmp_path / "server.log"
        diagnostics = logs.enter_context(diagnostics_path.open("wb"))
        servers: list[subprocess.Popen[bytes]] = []

        ready = Event()

        def lifetime(stop: Event) -> None:
            try:
                servers.append(
                    subprocess.Popen(
                        [sshd, "-D", "-e", "-f", str(config)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=diagnostics,
                    )
                )
                ready.set()
                if not stop.wait(120):
                    raise TimeoutError("Owned sshd fixture lifetime exceeded its bound")
            finally:
                if servers:
                    server = servers[0]
                    if server.poll() is None:
                        server.kill()
                    server.wait(timeout=2)
                    assert server.returncode is not None

        try:
            with FixtureWorker(lifetime) as worker:
                worker.start()
                until = time.monotonic() + 3
                while not ready.wait(0.01):
                    if worker.done or time.monotonic() >= until:
                        pytest.fail(f"Owned sshd constructor did not publish; diagnostics: {diagnostics_path}")
                server = servers[0]
                until = time.monotonic() + 3
                while True:
                    if server.poll() is not None:
                        pytest.fail(
                            f"Owned sshd startup exited with status {server.returncode}; "
                            f"diagnostics: {diagnostics_path}"
                        )
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        if time.monotonic() >= until:
                            pytest.fail(f"Owned sshd did not become available; diagnostics: {diagnostics_path}")
                        time.sleep(0.01)
                yield LocalSSH(
                    SSHConnection(
                        "127.0.0.1",
                        pwd.getpwuid(os.getuid()).pw_name,
                        identity,
                        bundle,
                        port=port,
                        host_key_alias=alias,
                    ),
                    SSHCreationProvenance(
                        "fixture-provider/creation-123", "127.0.0.1", port=port, host_key_alias=alias
                    ),
                    authorized,
                    host_public.encode(),
                    scenario,
                )
        finally:
            # Worker completion and reaping precede socket/log custody release.
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
