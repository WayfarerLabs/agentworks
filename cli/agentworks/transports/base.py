"""The ``Transport`` abstract base class.

Each concrete subclass implements the full operator I/O surface for one
delivery mechanism (SSH, ``limactl shell``, ``wsl.exe``, etc.). Callers
obtain a ``Transport`` via the factory functions in this package's
``__init__.py``: ``transport(vm, config)`` for the canonical admin
path, ``agent_transport(vm, config, agent)`` for the canonical agent
path, ``native_transport(vm, platform, config, *, stack)`` for the
platform-native opt-in.

The ABC surface covers both command exec and file movement because
every transport in practice supports both, sharing one delivery
mechanism per platform (SSH carries scp; ``limactl shell`` pairs with
``limactl copy``; ``wsl.exe`` carries both).
"""

from __future__ import annotations

import abc
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.ssh import SSHError, SSHResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.ssh import SSHLogger


class Transport(abc.ABC):
    """Operator I/O channel to a VM: command exec and file movement.

    Concrete subclasses populate ``self.default_timeout`` and
    ``self.logger`` in their constructors; the ABC declares the
    attributes here so polymorphic callers can read or set them
    without isinstance narrowing.
    """

    default_timeout: int | None = None
    logger: SSHLogger | None = None

    def _resolve_timeout(self, override: int | None) -> int | None:
        """Resolve a per-call timeout override against ``default_timeout``."""
        return override if override is not None else self.default_timeout

    @abc.abstractmethod
    def describe(self) -> str:
        """Return a short ``<scheme>:<endpoint>`` label for log lines.

        Examples: ``ssh:100.64.0.1``, ``lima:myvm``,
        ``remote_lima:myvm@host``, ``wsl2:Debian``. Used by
        :mod:`agentworks.vms.initializer` to label transports in init
        events without isinstance branching.
        """

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
        retries: int | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> SSHResult:
        """Run ``command`` and return its result.

        ``sudo=True`` wraps the command in ``sudo -n bash -c '...'`` so
        compound commands run wholly as root. ``tty=None`` is transport
        default; ``tty=True``/``False`` overrides. ``check=True`` raises
        on non-zero exit. ``env`` is the per-call env dict; SSH carries
        it via ``-o SetEnv``, non-SSH transports prepend it as scoped
        assignments to the bash payload.

        ``retries`` and ``on_retry`` are best-effort across transports:
        SSH retries on connection-level timeouts (default 1 attempt);
        non-SSH transports treat ``retries`` as a no-op (the underlying
        CLI doesn't surface a retryable timeout). Callers can rely on
        the kwargs being accepted without isinstance narrowing.
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

    # -- Concrete defaults --------------------------------------------------
    # ``copy_dir_to`` and ``write_file`` are concrete here because every
    # subclass historically implemented the same body. The default
    # composes ``copy_to`` + ``run`` (both abstract); a subclass with a
    # cheaper native option (e.g. a future rsync transport) can still
    # override.

    def copy_dir_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a local directory tree via tar + ``copy_to`` + remote extract.

        Uses Python's stdlib ``tarfile`` so no client tar binary is
        required (works on Windows). With ``delete=True`` (default) the
        destination is cleared before extraction.
        """
        local_path = Path(local_path)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(local_path, arcname=".")
            remote_tmp = f"/tmp/agentworks-copy-{tmp_path.name}"
            self.copy_to(tmp_path, remote_tmp, timeout=timeout)
        finally:
            tmp_path.unlink(missing_ok=True)

        if delete:
            self.run(f"rm -rf {remote_path} && mkdir -p {remote_path}", timeout=timeout)
        else:
            self.run(f"mkdir -p {remote_path}", timeout=timeout)
        self.run(f"tar -xzf {remote_tmp} -C {remote_path} && rm -f {remote_tmp}", timeout=timeout)

    def write_file(
        self,
        remote_path: str,
        content: str,
        *,
        mode: str | None = None,
    ) -> None:
        """Write ``content`` to ``remote_path`` atomically via tempfile + ``copy_to``.

        Avoids embedding multi-line content in command argv (which
        breaks on Windows due to CRLF conversion). If ``mode`` is set,
        chmods after the write.
        """
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".tmp", delete=False) as f:
            f.write(content.encode("utf-8"))
            tmp_path = f.name
        try:
            self.copy_to(tmp_path, remote_path)
            if self.logger is not None:
                self.logger.log_command(
                    f"({self.describe()}) write {remote_path} ({len(content)} bytes)",
                    SSHResult(returncode=0, stdout="", stderr=""),
                )
        except SSHError:
            if self.logger is not None:
                self.logger.log_error(f"({self.describe()}) failed to write {remote_path}")
            raise
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if mode:
            self.run(f"chmod {mode} {remote_path}")
