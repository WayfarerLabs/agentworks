"""Explicit connection admission and isolated OpenSSH policy for every delivery mode."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentworks.errors import ValidationError
from agentworks.execution.carriers.ssh.settings import validate_literal_path, validate_ssh_executable
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, SSHTrustFiles, resolve_trust

if TYPE_CHECKING:
    from agentworks.execution.carrier import PreparedInvocation


@dataclass(frozen=True)
class SSHConnection:
    """Resolved input from an adapter author; construction does no filesystem I/O.

    Connections accept explicit trust, literal DNS/IP hosts, POSIX account
    names, and explicit filesystem paths. An absent agent disables agent use.
    Unsupported path expansion and account policies fail before dispatch.
    """

    host: str
    user: str
    identity_file: Path
    trust: SSHTrustFiles | ManagedSSHTrust
    port: int = 22
    host_key_alias: str | None = None
    agent_socket: str | None = None
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
        if not isinstance(self.identity_file, Path):
            raise ValidationError("SSH identity must be a native Path value")
        validate_literal_path(self.identity_file)
        if isinstance(self.trust, SSHTrustFiles):
            _validate_trust_paths(self.trust)
        elif isinstance(self.trust, ManagedSSHTrust):
            validate_literal_path(self.trust.directory)
        else:
            raise ValidationError("SSH trust must be explicit files or an owned managed bundle")
        if self.agent_socket is not None:
            if not isinstance(self.agent_socket, str):
                raise ValidationError("SSH agent socket must be an absolute native path")
            validate_literal_path(Path(self.agent_socket))
        validate_ssh_executable(self.ssh_executable)


def _validate_trust_paths(trust: SSHTrustFiles) -> None:
    for path in trust.known_hosts:
        validate_literal_path(path)
    if trust.revoked_host_keys is not None:
        validate_literal_path(trust.revoked_host_keys)


def admit_connection(connection: SSHConnection) -> SSHTrustFiles:
    """Validate identity and freshly admit one immutable trust selection.

    Managed policy is resolved on every operation. OpenSSH parses authentication
    and trust data and enforces permissions when it opens the admitted files.
    Local filesystem calls are synchronous; callers recheck their deadline
    after admission and before dispatch. Trust is never enrolled here.
    """
    if not connection.identity_file.is_file() or not os.access(connection.identity_file, os.R_OK):
        raise ValidationError("SSH requires an existing readable identity file")
    if connection.agent_socket is not None and not Path(connection.agent_socket).is_socket():
        raise ValidationError("SSH requires an existing explicit Unix-domain agent socket")
    _validate_identity_sidecar(connection.identity_file)
    trust = resolve_trust(connection.trust)
    _validate_trust_paths(trust)
    return trust


def _validate_identity_sidecar(identity_file: Path) -> None:
    """Refuse a sibling public key that could select another identity from an agent.

    OpenSSH tries IdentityFile.pub before extracting a private key's public part.
    IdentitiesOnly does not fix a stale or substituted sibling public key.
    """
    from agentworks.ssh_identity import (
        SSHIdentityReadError,
        VerifiedSSHIdentity,
        read_private_ssh_identity,
        read_public_ssh_identity,
    )

    sidecar = Path(str(identity_file) + ".pub")
    if not sidecar.exists():
        return
    try:
        # A public file cannot prove OpenSSH will accept its entire encoding.
        # Refuse an ambiguous public-file/sibling pair instead of allowing a
        # client parse failure to select another key through suffix lookup.
        identity = read_private_ssh_identity(identity_file)
        companion = read_public_ssh_identity(sidecar)
    except SSHIdentityReadError:
        raise ValidationError("SSH identity and sibling public key must be independently verifiable") from None
    if not isinstance(identity, VerifiedSSHIdentity) or identity.fingerprint != companion.fingerprint:
        raise ValidationError("SSH sibling public key does not verify against the configured identity")


def build_ssh_argv(
    connection: SSHConnection,
    invocation: PreparedInvocation,
    *,
    trust: SSHTrustFiles,
    terminal: bool = False,
    local_forwards: tuple[str, ...] = (),
) -> list[str]:
    """Build strict delivery argv using this operation's admitted trust selection."""
    return _build_argv(
        connection, invocation, trust=trust, host_key_checking="yes", terminal=terminal, local_forwards=local_forwards
    )


def _build_enrollment_argv(
    connection: SSHConnection, invocation: PreparedInvocation, *, trust: SSHTrustFiles
) -> list[str]:
    """Build first enrollment argv after the caller validates an exclusive candidate."""
    return _build_argv(connection, invocation, trust=trust, host_key_checking="accept-new")


def _build_argv(
    connection: SSHConnection,
    invocation: PreparedInvocation,
    *,
    trust: SSHTrustFiles,
    host_key_checking: Literal["yes", "accept-new"],
    terminal: bool = False,
    local_forwards: tuple[str, ...] = (),
) -> list[str]:
    """Serialize literal argv through a POSIX account shell, without doing I/O.

    Account-shell compatibility and startup behavior are delivery preconditions.
    The caller owns stdin (pipe or DEVNULL), deadline and executable version
    checks. Forward specifications come from the owned forwarding operation's
    validated values; they are -L operands, never arbitrary client options.
    No option here steals input or introduces application shell policy.
    """
    _validate_trust_paths(trust)
    known_hosts = " ".join(f'"{path.as_posix()}"' for path in trust.known_hosts)
    options = [
        # Authentication, trust and input overrides.
        "BatchMode=yes",
        f"StrictHostKeyChecking={host_key_checking}",
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
        f"UserKnownHostsFile={known_hosts}",
        (
            "IdentityAgent=none"
            if connection.agent_socket is None
            else f'IdentityAgent="{Path(connection.agent_socket).as_posix()}"'
        ),
    ]
    if trust.revoked_host_keys is not None:
        options.append(f'RevokedHostKeys="{trust.revoked_host_keys.as_posix()}"')
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
