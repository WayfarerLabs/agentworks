"""WSL2 transport implementation.

Reaches a WSL2 distro via ``wsl.exe --distribution <distro> --user
<user>``. Used as the provisioner transport for the WSL2 platform.
"""

from __future__ import annotations

import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports.base import Transport

if TYPE_CHECKING:
    from agentworks.ssh import SSHLogger


def _env_assignment_prefix(env: dict[str, str] | None) -> str:
    """``K1=v1 K2=v2 `` (trailing space) for ``env``, else empty."""
    if not env:
        return ""
    return "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())


class WSL2Transport(Transport):
    """WSL2 transport via ``wsl.exe``."""

    def __init__(
        self,
        distro_name: str,
        *,
        user: str = "agentworks",
        default_timeout: int | None = None,
        logger: SSHLogger | None = None,
    ) -> None:
        self.distro_name = distro_name
        self.user = user
        self.default_timeout = default_timeout
        self.logger = logger

    def _resolve_timeout(self, override: int | None) -> int | None:
        return override if override is not None else self.default_timeout

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
        """Run ``command`` inside the WSL2 distro."""
        del tty  # not meaningful for non-interactive wsl.exe
        if sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"
        env_prefix = _env_assignment_prefix(env)
        args = [
            "wsl",
            "--distribution",
            self.distro_name,
            "--user",
            self.user,
            "--",
            "bash",
            "-lc",
            f"{env_prefix}{command}",
        ]
        t = self._resolve_timeout(timeout)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=t,
            )
        except subprocess.TimeoutExpired as err:
            raise SSHError(f"WSL2 command timed out after {t}s: {command}") from err
        ssh_result = SSHResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if self.logger is not None:
            self.logger.log_command(command, ssh_result)
        if check and not ssh_result.ok:
            raise SSHError(
                f"WSL2 command failed (exit {result.returncode}): {command}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return ssh_result

    def interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Interactive shell via ``wsl --distribution <distro> --user <user>``."""
        del env  # documented gap: wsl.exe doesn't expose env injection
        args = ["wsl", "--distribution", self.distro_name, "--user", self.user]
        if command:
            args.extend(["--", "bash", "-lc", command])
        return subprocess.call(args)

    def copy_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy ``local_path`` into the distro via ``wsl ... cat > path``.

        We pipe stdin into ``cat`` rather than passing a Windows path on
        the wsl.exe command line to avoid the path translation pitfalls
        (forward/back-slash, drive letters).
        """
        del timeout  # wsl.exe doesn't honor a subprocess timeout here
        content = Path(local_path).read_bytes()
        result = subprocess.run(
            ["wsl", "--distribution", self.distro_name, "--user", "root", "--", "bash", "-c", f"cat > {remote_path}"],
            input=content,
            capture_output=True,
        )
        if result.returncode != 0:
            raise SSHError(f"WSL2 copy failed: {result.stderr.decode().strip()}")

    def copy_from(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Copy ``remote_path`` from the distro via ``wsl ... cat`` to stdout.

        Mirrors ``copy_to``: pipe through wsl.exe rather than relying on
        Windows path translation.
        """
        del timeout
        result = subprocess.run(
            ["wsl", "--distribution", self.distro_name, "--user", "root", "--", "bash", "-c", f"cat {remote_path}"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise SSHError(f"WSL2 copy failed: {result.stderr.decode().strip()}")
        Path(local_path).write_bytes(result.stdout)

    def copy_dir_to(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a directory via tar + ``copy_to`` + remote extract."""
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
        """Write ``content`` to ``remote_path`` via tempfile + ``copy_to``."""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".tmp", delete=False) as f:
            f.write(content.encode("utf-8"))
            tmp_path = f.name
        try:
            self.copy_to(tmp_path, remote_path)
            if self.logger is not None:
                self.logger.log_command(
                    f"(wsl) write {remote_path} ({len(content)} bytes)",
                    SSHResult(returncode=0, stdout="", stderr=""),
                )
        except SSHError:
            if self.logger is not None:
                self.logger.log_error(f"(wsl) failed to write {remote_path}")
            raise
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if mode:
            self.run(f"chmod {mode} {remote_path}")

    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run ``command`` inside the WSL2 distro with inherited stdio."""
        env_prefix = _env_assignment_prefix(env)
        args = [
            "wsl",
            "--distribution",
            self.distro_name,
            "--user",
            self.user,
            "--",
            "bash",
            "-lc",
            f"{env_prefix}{command}",
        ]
        return subprocess.call(args)
