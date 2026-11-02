"""Resolve the OpenSSH client once, to an absolute path and a version.

Every spawn takes the resolved :class:`SSHClient`, so no caller can launch a
bare ``ssh``. Two hazards guard the resolution: on Windows a bare ``ssh`` is
looked up in the current directory first and ``PATH`` commonly finds the MSYS
build bundled with Git for Windows, whose argument handling corrupts every
invocation; and an empty ``PATH`` element means the current directory on POSIX.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import ssh_identity
from agentworks.execution.errors import ClientUnsupported

if TYPE_CHECKING:
    from collections.abc import Mapping

MINIMUM_VERSION = (8, 5)
"""OpenSSH 8.5 is the oldest client the VM policy's option set is known to work with."""

_BANNER = re.compile(rb"\AOpenSSH_(for_Windows_)?(\d+)\.(\d+)(?:p\d+)?(?:[,\s]|$)")
_VERSION_PROBE_SECONDS = 10
_SSH_ADD_PROBE_SECONDS = 3

_INSTALL_GUIDANCE = {
    True: (
        "Install the Win32-OpenSSH MSI from https://github.com/PowerShell/Win32-OpenSSH/releases. "
        "Windows 10's inbox client is OpenSSH 8.1, which is too old. Git for Windows' bundled ssh is not "
        "supported and PATH is never searched."
    ),
    False: "Install OpenSSH 8.5 or newer so that 'ssh' is on an absolute directory of PATH.",
}


@dataclass(frozen=True)
class SSHClient:
    """An OpenSSH client that passed resolution."""

    path: str
    version: tuple[int, int]


def candidates(*, windows: bool, environ: Mapping[str, str]) -> list[Path]:
    """The client paths worth judging, in order.

    POSIX has one: the first ``ssh`` along ``PATH``, skipping empty and
    relative elements. Windows never searches ``PATH``; each fixed location is
    skipped when its variable is unset or its file is missing. ``Sysnative`` and
    ``ProgramW6432`` exist only for a 32-bit Python on 64-bit Windows, where
    ``System32`` and ``ProgramFiles`` are redirected away from the real client.
    """
    if windows:
        locations = (
            ("SystemRoot", ("Sysnative", "OpenSSH")),
            ("SystemRoot", ("System32", "OpenSSH")),
            ("ProgramW6432", ("OpenSSH",)),
            ("ProgramFiles", ("OpenSSH",)),
        )
        found = []
        for variable, parts in locations:
            if base := environ.get(variable):
                path = Path(base, *parts, "ssh.exe")
                if path.is_file():
                    found.append(path)
        return found
    for element in environ.get("PATH", "").split(os.pathsep):
        if element and os.path.isabs(element):
            path = Path(element, "ssh")
            if path.is_file() and os.access(path, os.X_OK):
                return [path]
    return []


def judge(path: Path, *, windows: bool) -> SSHClient | str:
    """Run ``ssh -V`` once and return the client, or the reason it is not acceptable."""
    try:
        probe = subprocess.run(
            [str(path), "-V"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_VERSION_PROBE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "could not be started"
    match = _BANNER.match(probe.stderr)
    if match is None or bool(match[1]) != windows:
        return "not an OpenSSH build for this platform"
    version = (int(match[2]), int(match[3]))
    if version < MINIMUM_VERSION:
        return f"OpenSSH {version[0]}.{version[1]} is too old (8.5 or newer is required)"
    return SSHClient(str(path), version)


def resolve(*, windows: bool, environ: Mapping[str, str]) -> SSHClient:
    """Resolve the client for a platform and environment, without caching."""
    refusals: list[str] = []
    for path in candidates(windows=windows, environ=environ):
        verdict = judge(path, windows=windows)
        if isinstance(verdict, SSHClient):
            return verdict
        refusals.append(f"  {path}: {verdict}")
    tried = "\n".join(refusals) if refusals else "  no OpenSSH client was found"
    raise ClientUnsupported(f"no supported OpenSSH client:\n{tried}\n{install_guidance(windows=windows)}")


def install_guidance(*, windows: bool) -> str:
    """How to install a supported client on the platform, for a :class:`ClientUnsupported` message."""
    return _INSTALL_GUIDANCE[windows]


def tool_beside_ssh(ssh: SSHClient, name: str) -> Path:
    """The OpenSSH program ``name`` (``ssh-add``, ``ssh-keygen``) from the resolved client's directory.

    It is never looked up on ``PATH``, for the reasons the client itself is
    not: ``PATH`` can name a relative or current-directory program, and on
    Windows it commonly finds Git for Windows' MSYS build. Raises
    :class:`ClientUnsupported` naming the expected path, and how to install a
    supported client, when no executable file is there.
    """
    windows = sys.platform == "win32"
    tool = Path(ssh.path).with_name(f"{name}.exe" if windows else name)
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        raise ClientUnsupported(
            f"no executable {name} beside the OpenSSH client: expected {tool}\n{install_guidance(windows=windows)}"
        )
    return tool


def identity_is_loaded(ssh: SSHClient, identity_file: Path) -> bool:
    """Whether the SSH agent lists the key whose public half is ``<identity_file>.pub``.

    ``ssh-add -L`` lists the agent's public keys without signing, so it never
    prompts a confirm-required or per-signature agent, and it does not contact
    the VM. It exits 1 for an agent with no keys and 2 for no agent; both mean
    not loaded. The key counts as loaded when a listed line's type and base64
    blob equal the ``.pub`` file's. Only the ``ssh-add`` beside the resolved
    client runs (:func:`tool_beside_ssh`). ``False`` when it cannot be asked
    (no ``.pub`` file or one without a type and blob, no ``ssh-add``, it did
    not run): the caller then keeps its conditional hint.
    """
    public_key = Path(f"{identity_file}.pub")
    try:
        ssh_add = tool_beside_ssh(ssh, "ssh-add")
        if not public_key.is_file():  # a FIFO there would block the read
            return False
        wanted = public_key.read_text(encoding="utf-8", errors="replace").split()[:2]
    except (ClientUnsupported, OSError):
        return False
    if len(wanted) != 2:
        return False
    try:
        probe = subprocess.run(
            [str(ssh_add), "-L"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_SSH_ADD_PROBE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if probe.returncode != 0:
        return False
    listed = probe.stdout.decode("utf-8", errors="replace").splitlines()
    return any(line.split()[:2] == wanted for line in listed)


def agent_load_hint(identity_file: Path) -> str | None:
    """How to load ``identity_file`` into the SSH agent, or ``None`` unless that is the likely fix.

    ssh runs in batch mode, so an encrypted key the agent does not hold is
    refused like any other. There is no hint for an unencrypted key or one the
    agent already lists. This reads the key file and may run ``ssh-add``, so
    callers ask only after a connection or authentication failure.
    """
    hint = ssh_identity.passphrase_hint(identity_file)
    if hint is None or identity_is_loaded(resolve_ssh(), identity_file):
        return None
    return hint


@functools.cache
def resolve_ssh() -> SSHClient:
    """The process's OpenSSH client. A refusal is not cached, so installing one takes effect on the next call."""
    return resolve(windows=sys.platform == "win32", environ=os.environ)
