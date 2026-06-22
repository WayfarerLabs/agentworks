"""The ``Transport`` abstract base class.

Each concrete subclass implements the full operator I/O surface for one
delivery mechanism (SSH, ``limactl shell``, ``wsl.exe``, etc.). Callers
obtain a ``Transport`` via the factory functions in this package's
``__init__.py``: ``transport(vm, config)`` for the canonical admin
path, ``agent_transport(vm, config, agent)`` for the canonical agent
path, ``provisioner_transport(vm, config, *, stack)`` for the
platform-native opt-in.

The ABC surface covers both command exec and file movement because
every transport in practice supports both, sharing one delivery
mechanism per platform (SSH carries scp; ``limactl shell`` pairs with
``limactl copy``; ``wsl.exe`` carries both).
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.ssh import SSHResult


class Transport(abc.ABC):
    """Operator I/O channel to a VM: command exec and file movement."""

    @abc.abstractmethod
    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        tty: bool | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SSHResult:
        """Run ``command`` and return its result.

        ``sudo=True`` wraps the command in ``sudo -n bash -c '...'`` so
        compound commands run wholly as root. ``tty=None`` is transport
        default; ``tty=True``/``False`` overrides. ``check=True`` raises
        on non-zero exit. ``env`` is the per-call env dict; SSH carries
        it via ``-o SetEnv``, non-SSH transports prepend it as scoped
        assignments to the bash payload.
        """

    @abc.abstractmethod
    def interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run an interactive session with a TTY.

        Empty ``command`` opens a login shell on the VM. ``env`` is
        best-effort: SSH carries it via SetEnv; the non-SSH transports
        drop it (``limactl shell`` and ``wsl.exe`` don't expose env
        injection on their interactive APIs). Returns the process exit
        code; does not raise on remote-command failure.
        """

    @abc.abstractmethod
    def copy_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy a local file to the remote path on the VM.

        ``timeout`` is best-effort: SSH honors it; transports whose
        underlying CLI (limactl copy, wsl.exe) doesn't accept a timeout
        silently drop it.
        """

    @abc.abstractmethod
    def copy_from(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy a remote file from the VM to a local path.

        SSH uses scp; Lima uses ``limactl copy`` (reverse); WSL2 uses
        ``wsl ... cat`` to stdout; RemoteLima two-hops through the VM
        host. ``backup.py`` is the canonical consumer. ``timeout`` is
        best-effort: SSH honors it; transports whose underlying CLI
        (limactl copy, wsl.exe) doesn't accept a timeout silently drop
        it.
        """

    @abc.abstractmethod
    def copy_dir_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a local directory tree to the VM.

        Default tarball-based implementation: tar the local tree to a
        temp file (Python ``tarfile``, no client tar binary required),
        ``copy_to`` it, then ``run`` ``tar -xzf`` on the VM. With
        ``delete=True`` (default) the destination is cleared before
        extraction.
        """

    @abc.abstractmethod
    def write_file(
        self,
        remote_path: str,
        content: str,
        *,
        mode: str | None = None,
    ) -> None:
        """Write a small string atomically to ``remote_path``.

        Writes via a local tempfile + ``copy_to`` to avoid embedding
        multi-line content in command argv (which breaks on Windows
        due to CRLF conversion). If ``mode`` is set, chmods after the
        write. ~15 call sites today; load-bearing for init.
        """

    @abc.abstractmethod
    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a command with inherited stdio (no buffering).

        Used by ``vm exec`` and ``agent exec`` so the operator sees
        output stream in real time. Non-interactive (no TTY). Returns
        the remote exit code.
        """
