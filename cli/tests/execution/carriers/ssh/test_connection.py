"""Literal inputs, local validation and the installed client's offline parser."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import PreparedInvocation
from agentworks.execution.carriers.ssh.connection import (
    SSHConnection,
    admit_connection,
    build_ssh_argv,
)
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, SSHTrustFiles


@pytest.fixture
def connection(tmp_path: Path) -> SSHConnection:
    tmp_path = tmp_path.resolve()
    return SSHConnection("host.example", "account", tmp_path / "identity", SSHTrustFiles((tmp_path / "known hosts",)))


def test_construction_and_serialization_are_passive(connection: SSHConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("Passive connection inspected the filesystem")

    monkeypatch.setattr(Path, "stat", fail)
    monkeypatch.setattr(Path, "open", fail)
    copy = replace(connection)
    build_ssh_argv(copy, PreparedInvocation(("true",)), trust=copy.trust)
    with pytest.raises(FrozenInstanceError):
        copy.port = 23  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "-oProxyCommand=bad"),
        ("host", "other@host"),
        ("host", "ssh://host:22"),
        ("host", "host\nother"),
        ("host", "host\0other"),
        ("host", "host name"),
        ("host", "[::1]"),
        ("host", "fe80::1%eth0"),
        ("host", "name:2222"),
        ("user", "-option"),
        ("user", "user\noption"),
        ("user", "user@host"),
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("port", "22"),
        ("keepalive_interval", -1),
        ("keepalive_interval", True),
        ("keepalive_count_max", 0),
        ("keepalive_count_max", 2_147_483_648),
        ("host_key_alias", "alias\nProxyCommand=bad"),
        ("agent_socket", "SSH_AUTH_SOCK"),
        ("agent_socket", "$SSH_AUTH_SOCK"),
        ("ssh_executable", ""),
        ("ssh_executable", "./ssh"),
        ("ssh_executable", "relative/ssh"),
        ("ssh_executable", "~/bin/ssh"),
        ("ssh_executable", "-ssh"),
        ("ssh_executable", "ssh\0bad"),
        ("identity_file", None),
        ("trust", "relative"),
    ],
)
def test_unsupported_or_injectable_input_is_refused(connection: SSHConnection, field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        replace(connection, **{field: value})


@pytest.mark.parametrize("name", ["relative", "~/identity", "/tmp/%d/key", "/tmp/${HOME}/key", '/tmp/a"b', "/tmp/a\nb"])
@pytest.mark.parametrize("field", ["identity_file", "known_hosts", "revoked_host_keys", "directory"])
def test_nonliteral_paths_are_refused(connection: SSHConnection, name: str, field: str) -> None:
    with pytest.raises(ValidationError):
        if field == "identity_file":
            replace(connection, identity_file=Path(name))
        elif field == "directory":
            replace(connection, trust=ManagedSSHTrust(Path(name)))
        else:
            trust = (
                SSHTrustFiles((Path(name),)) if field == "known_hosts" else SSHTrustFiles((Path("/known"),), Path(name))
            )
            replace(connection, trust=trust)


def test_ipv6_and_explicit_nondefault_port_are_preserved(connection: SSHConnection) -> None:
    argv = build_ssh_argv(
        replace(connection, host="::1", port=2200), PreparedInvocation(("true",)), trust=connection.trust
    )
    assert argv[-7:-1] == ["-p", "2200", "-l", "account", "--", "::1"]


@pytest.mark.windows
def test_files_checked_at_operation_time_without_mutation(connection: SSHConnection, tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()
    assert isinstance(connection.trust, SSHTrustFiles)
    with pytest.raises(ValidationError):
        admit_connection(connection)
    connection.identity_file.write_bytes(b"fixture identity")
    connection.trust.known_hosts[0].write_bytes(b"fixture trust")
    admit_connection(connection)
    revoked = tmp_path / "revoked"
    with pytest.raises(StateError):
        admit_connection(replace(connection, trust=SSHTrustFiles(connection.trust.known_hosts, revoked)))
    revoked.write_bytes(b"fixture revocations")
    admit_connection(replace(connection, trust=SSHTrustFiles(connection.trust.known_hosts, revoked)))
    connection.trust.known_hosts[0].unlink()
    connection.trust.known_hosts[0].mkdir()
    with pytest.raises(StateError):
        admit_connection(connection)
    assert connection.identity_file.read_bytes() == b"fixture identity"
    assert revoked.read_bytes() == b"fixture revocations"


@pytest.mark.skipif(os.name == "nt", reason="Explicit Unix-domain agent sockets")
def test_agent_endpoint_is_explicit_and_must_be_a_socket(connection: SSHConnection) -> None:
    assert isinstance(connection.trust, SSHTrustFiles)
    connection.identity_file.touch()
    connection.trust.known_hosts[0].touch()
    # macOS's default temporary path plus pytest nesting can exceed AF_UNIX limits.
    with tempfile.TemporaryDirectory(prefix="agw-ssh-agent-", dir="/tmp") as directory:
        endpoint = Path(directory) / "agent"
        selected = replace(connection, agent_socket=str(endpoint))
        endpoint.touch()
        with pytest.raises(ValidationError):
            admit_connection(selected)
        endpoint.unlink()
        with socket.socket(socket.AF_UNIX) as agent:
            agent.bind(str(endpoint))
            admit_connection(selected)


@pytest.mark.windows
@pytest.mark.parametrize("public_identity", [False, True])
def test_sibling_key_cannot_select_another_identity(
    connection: SSHConnection, tmp_path: Path, public_identity: bool
) -> None:
    assert isinstance(connection.trust, SSHTrustFiles)
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        pytest.skip("Identity selection check needs installed ssh-keygen")
    first, second = tmp_path / "first", tmp_path / "second"
    for key in (first, second):
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=10)
    connection.trust.known_hosts[0].touch()
    identity = Path(str(first) + ".pub") if public_identity else first
    selected = replace(connection, identity_file=identity)
    admit_connection(selected)
    Path(str(identity) + ".pub").write_bytes(Path(str(second) + ".pub").read_bytes())
    with pytest.raises(ValidationError):
        admit_connection(selected)


def test_truncated_public_identity_cannot_fall_back_to_sibling(connection: SSHConnection) -> None:
    assert isinstance(connection.trust, SSHTrustFiles)
    connection.trust.known_hosts[0].touch()
    connection.identity_file.write_bytes(b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5\n")
    Path(str(connection.identity_file) + ".pub").write_bytes(b"retained sibling")
    with pytest.raises(ValidationError):
        admit_connection(connection)


@pytest.mark.skipif(os.name == "nt", reason="POSIX account-shell serialization")
def test_remote_arguments_round_trip_through_real_shell(connection: SSHConnection) -> None:
    args = ("", "simple", "two words", "quote'and\"double", "a\nb", "$(exit 99)", "; exit 98", "*", "\\", "café")
    invocation = PreparedInvocation(("printf", "%s\\0", *args))
    command = build_ssh_argv(connection, invocation, trust=connection.trust)[-1]
    observed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, check=True)
    assert observed.stdout == b"".join(arg.encode() + b"\0" for arg in args)
    assert observed.stderr == b""


@pytest.mark.skipif(os.name == "nt", reason="POSIX account-shell command position")
@pytest.mark.parametrize("name", ["for", "!", "NAME=value"])
def test_command_position_is_literal(connection: SSHConnection, tmp_path: Path, name: str) -> None:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 17\n")
    executable.chmod(0o700)
    command = build_ssh_argv(connection, PreparedInvocation((name,)), trust=connection.trust)[-1]
    observed = subprocess.run(["/bin/sh", "-c", command], env={"PATH": str(tmp_path)}, capture_output=True)
    assert observed.returncode == 17


@pytest.mark.windows
def test_argv_explicitly_enforces_isolation_policy(connection: SSHConnection) -> None:
    argv = build_ssh_argv(connection, PreparedInvocation(("true",)), trust=connection.trust)
    assert argv[argv.index("-F") + 1] == "none"
    assert "-T" in argv
    assert "-n" not in argv
    options = {argv[index + 1] for index, arg in enumerate(argv) if arg == "-o"}
    # Missing controls must fail even when ssh -G omits their disabled values
    # or the installed client's defaults happen to match the required policy.
    assert {
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=none",
        "KnownHostsCommand=none",
        "UpdateHostKeys=no",
        "CheckHostIP=no",
        "VerifyHostKeyDNS=no",
        "CanonicalizeHostname=no",
        "IdentitiesOnly=yes",
        "PreferredAuthentications=publickey",
        "PKCS11Provider=none",
        "SecurityKeyProvider=none",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "HostbasedAuthentication=no",
        "IdentityAgent=none",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ClearAllForwardings=yes",
        "Tunnel=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "ControlMaster=no",
        "ControlPath=none",
        "ControlPersist=no",
        "PermitLocalCommand=no",
        "EscapeChar=none",
        "ConnectionAttempts=1",
    } <= options


@pytest.mark.windows
def test_installed_openssh_parses_isolated_policy(connection: SSHConnection, tmp_path: Path) -> None:
    assert isinstance(connection.trust, SSHTrustFiles)
    ssh = shutil.which("ssh")
    if ssh is None:
        pytest.skip("Offline parser check needs an installed OpenSSH client")
    selected = replace(
        connection,
        ssh_executable=ssh,
        identity_file=tmp_path / "identity with 'quote' and #hash",
        trust=SSHTrustFiles(
            (*connection.trust.known_hosts, tmp_path / "second policy"), tmp_path / "revocations with spaces"
        ),
        host_key_alias="owned-alias",
        keepalive_interval=7,
        keepalive_count_max=3,
    )
    assert isinstance(selected.trust, SSHTrustFiles)
    argv = build_ssh_argv(selected, PreparedInvocation(("true",)), trust=selected.trust)
    # -G exits after configuration processing; it never establishes a connection.
    observed = subprocess.run([argv[0], "-G", *argv[1:]], capture_output=True, text=True, timeout=10, check=True)
    settings = dict(line.split(" ", 1) for line in observed.stdout.splitlines())
    identity_files = [
        line.removeprefix("identityfile ") for line in observed.stdout.splitlines() if line.startswith("identityfile ")
    ]
    assert identity_files == [selected.identity_file.as_posix()]
    assert settings["userknownhostsfile"] == " ".join(path.as_posix() for path in selected.trust.known_hosts)
    assert selected.trust.revoked_host_keys is not None
    assert settings["revokedhostkeys"] == selected.trust.revoked_host_keys.as_posix()
    assert settings["certificatefile"] == (selected.identity_file / "disabled-certificate").as_posix()
    assert settings["identityagent"] == "none"
    assert settings["globalknownhostsfile"] == "none"
    assert settings["hostkeyalias"] == "owned-alias"
    assert settings["serveraliveinterval"] == "7"
    assert settings["serveralivecountmax"] == "3"
    assert settings["batchmode"] == "yes"
    assert settings["stricthostkeychecking"] in {"true", "yes"}
    assert settings["requesttty"] in {"false", "no"}
    assert settings["forwardagent"] in {"false", "no"}
    assert settings["clearallforwardings"] == "yes"
    assert settings["controlmaster"] in {"false", "no"}
    # OpenSSH omits some disabled settings from -G output. Explicit presence
    # is covered separately at the returned-argv boundary above.
    assert settings.get("controlpath", "none") == "none"
    assert settings.get("proxycommand", "none") == "none"
    assert settings.get("proxyjump", "none") == "none"
    assert settings.get("securitykeyprovider", "none") == "none"
    assert settings.get("pkcs11provider", "none") == "none"
    assert settings.get("knownhostscommand", "none") == "none"
    assert settings.get("sendenv") is None
