"""Explicit connection data and isolated OpenSSH policy for buffered delivery."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from agentworks.execution.carrier import PreparedInvocation


def _literal_path(path: Path) -> str:
    # OpenSSH expands tokens after parsing quotes. Refuse that policy rather
    # than turning a configured filename into another file or environment lookup.
    value = path.as_posix()
    if (
        not path.is_absolute()
        or any(char in value for char in '%$"\\')
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValidationError("SSH paths must be absolute native paths without expansion tokens or control characters")
    return value


@dataclass(frozen=True)
class SSHConnection:
    """Resolved input from an adapter author; construction does no filesystem I/O.

    The proof accepts pre-provisioned trust, literal DNS/IP hosts, POSIX account
    names, and explicit filesystem paths. An absent agent disables agent use.
    Unsupported path expansion and account policies fail before dispatch.
    """

    host: str
    user: str
    identity_file: Path
    known_hosts_file: Path
    port: int = 22
    host_key_alias: str | None = None
    agent_socket: str | None = None
    revoked_host_keys: Path | None = None
    ssh_executable: str = "ssh"
    keepalive_interval: int = 15
    keepalive_count_max: int = 4

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9_:][A-Za-z0-9_.:-]*", self.host):
            raise ValidationError("SSH host must be a literal DNS name or unbracketed IP address")
        if ":" in self.host:
            try:
                IPv6Address(self.host)
            except ValueError:
                raise ValidationError("SSH IPv6 host must be an unbracketed address without a scope") from None
        if not isinstance(self.user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\$?", self.user):
            raise ValidationError("SSH user must be a literal POSIX account name")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValidationError("SSH port must be an integer from 1 through 65535")
        if type(self.keepalive_interval) is not int or not 0 <= self.keepalive_interval <= 2_147_483_647:
            raise ValidationError("SSH keepalive interval must be a nonnegative client integer")
        if type(self.keepalive_count_max) is not int or not 1 <= self.keepalive_count_max <= 2_147_483_647:
            raise ValidationError("SSH keepalive count must be a positive client integer")
        if self.host_key_alias is not None and (
            not isinstance(self.host_key_alias, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", self.host_key_alias)
        ):
            raise ValidationError("SSH host key alias must be a literal lookup name")
        for path in (self.identity_file, self.known_hosts_file):
            if not isinstance(path, Path):
                raise ValidationError("SSH files must be native Path values")
            _literal_path(path)
        if self.revoked_host_keys is not None:
            if not isinstance(self.revoked_host_keys, Path):
                raise ValidationError("SSH revocation file must be a native Path value")
            _literal_path(self.revoked_host_keys)
        if self.agent_socket is not None:
            if not isinstance(self.agent_socket, str):
                raise ValidationError("SSH agent socket must be an absolute native path")
            _literal_path(Path(self.agent_socket))
        if (
            not isinstance(self.ssh_executable, str)
            or not self.ssh_executable
            or self.ssh_executable.startswith("-")
            or any(ord(c) < 32 or ord(c) == 127 for c in self.ssh_executable)
        ):
            raise ValidationError("SSH executable must be an explicit command name or native path")


def validate_connection_files(connection: SSHConnection) -> None:
    """Check caller-provided files at operation time without reading key material.

    OpenSSH remains responsible for parsing identity, trust and revocation data
    and enforcing permissions. Its subsequent opens can still fail after this
    availability check. Trust is never created, rewritten or enrolled here.
    """
    for path in (connection.identity_file, connection.known_hosts_file, connection.revoked_host_keys):
        if path is not None and (not path.is_file() or not os.access(path, os.R_OK)):
            raise ValidationError("SSH requires existing identity, known-host and optional revocation files")
    if connection.agent_socket is not None and not Path(connection.agent_socket).is_socket():
        raise ValidationError("SSH requires an existing explicit Unix-domain agent socket")


def build_ssh_argv(
    connection: SSHConnection,
    invocation: PreparedInvocation,
    *,
    terminal: bool = False,
    local_forwards: tuple[str, ...] = (),
) -> list[str]:
    """Serialize literal argv through a POSIX account shell, without doing I/O.

    Account-shell compatibility and startup behavior are proof preconditions.
    The caller owns stdin (pipe or DEVNULL), deadline and executable version
    checks. Forward specifications come from the owned forwarding operation's
    validated values; they are -L operands, never arbitrary client options.
    No option here steals input or introduces application shell policy.
    """
    options = [
        # Authentication, trust and input overrides.
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=none",
        "UpdateHostKeys=no",
        "CheckHostIP=no",
        "IdentitiesOnly=yes",
        # OpenSSH 8.5 treats CertificateFile=none as a relative filename:
        # https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c#L2312-L2320
        # An explicit child of the validated regular identity file cannot load
        # a certificate, including the loader's automatic .pub probe.
        f'CertificateFile="{(connection.identity_file / "disabled-certificate").as_posix()}"',
        "PreferredAuthentications=publickey",
        "SecurityKeyProvider=none",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "ClearAllForwardings=no" if local_forwards else "ClearAllForwardings=yes",
        "ExitOnForwardFailure=yes",
        "EscapeChar=none",
        # Pin disabled features even when they match a client's defaults:
        # isolation must hold across supported OpenSSH versions (8.5 and newer).
        "KnownHostsCommand=none",
        "VerifyHostKeyDNS=no",
        "CanonicalizeHostname=no",
        "PKCS11Provider=none",
        "HostbasedAuthentication=no",
        "ForwardAgent=no",
        "ForwardX11=no",
        "Tunnel=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "ControlMaster=no",
        "ControlPath=none",
        "ControlPersist=no",
        "PermitLocalCommand=no",
        "ConnectionAttempts=1",
        f"ServerAliveInterval={connection.keepalive_interval}",
        f"ServerAliveCountMax={connection.keepalive_count_max}",
        # Explicit caller selections.
        f'IdentityFile="{connection.identity_file.as_posix()}"',
        f'UserKnownHostsFile="{connection.known_hosts_file.as_posix()}"',
        (
            "IdentityAgent=none"
            if connection.agent_socket is None
            else f'IdentityAgent="{Path(connection.agent_socket).as_posix()}"'
        ),
    ]
    if connection.revoked_host_keys is not None:
        options.append(f'RevokedHostKeys="{connection.revoked_host_keys.as_posix()}"')
    if connection.host_key_alias is not None:
        options.append(f"HostKeyAlias={connection.host_key_alias}")
    argv = [connection.ssh_executable, "-F", "none", "-tt" if terminal else "-T"]
    for option in options:
        argv.extend(("-o", option))
    for forward in local_forwards:
        argv.extend(("-L", forward))
    # Quote even shell-safe words: a command name can be a reserved word or an
    # assignment, which shlex.quote intentionally leaves unquoted.
    command = " ".join("'" + arg.replace("'", "'\"'\"'") + "'" for arg in invocation.argv)
    argv.extend(("-p", str(connection.port), "-l", connection.user, "--", connection.host, command))
    return argv
