"""Private staging and execution for WSL2's primary bootstrap script."""

from __future__ import annotations

import shlex
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.bootstrap_script import (
    generate_bootstrap_script,
    parse_bootstrap_output,
)
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.errors import ProvisioningError
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import BootstrapProgress
    from agentworks.transports import Transport


def _warn_bootstrap_file_residue(message: str) -> None:
    """Emit one fixed cleanup warning without replacing active control flow."""
    try:  # noqa: SIM105 - warning failures must not replace the active provisioning failure
        output.warn(message)
    except (Exception, KeyboardInterrupt, SystemExit, GeneratorExit):
        pass


def run_wsl2_bootstrap(
    exec_target: Transport,
    *,
    admin_username: str,
    ssh_public_key: str,
    tailscale_auth_key: str,
    hostname: str,
    swap_gib: int,
    progress: BootstrapProgress,
) -> str:
    """Run WSL2's generated bootstrap and return its Tailscale IP.

    The caller supplies resolved bootstrap inputs and owns the progress sink's
    lifecycle. This helper owns only script generation, private local and guest
    staging, execution, cleanup, output parsing, redaction, and progress
    reporting. It does not persist VM state.
    """
    from agentworks.secrets.line_safety import (
        LineOrientedSecretUse,
        require_line_safe_secret,
    )

    tailscale_auth_key = require_line_safe_secret(
        tailscale_auth_key,
        use=LineOrientedSecretUse.TAILSCALE,
    )
    output.info("Bootstrapping VM...")

    script = generate_bootstrap_script(
        admin_username=admin_username,
        ssh_public_key=ssh_public_key,
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=tailscale_auth_key,
        hostname=hostname,
        swap=swap_gib,
    )

    # Allocate both stages privately before the script content reaches them.
    # The random guest name also avoids following a pre-existing predictable
    # path. Neither path contains the key, and the key crosses no command argv.
    remote_script = f"/tmp/agentworks-bootstrap-{uuid.uuid4().hex}.sh"
    try:
        try:
            exec_target.run(
                f"install -m 600 /dev/null {shlex.quote(remote_script)}",
                sudo=True,
            )
        except SSHError:
            raise ProvisioningError("could not create the private guest bootstrap staging file") from None

        local_script: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="agentworks-bootstrap-",
                suffix=".sh",
                delete=False,
            ) as staged:
                local_script = Path(staged.name)
                staged.write(script.encode("utf-8"))
            copy_failed = False
            try:
                exec_target.copy_to(local_script, remote_script)
            except SSHError:
                copy_failed = True
            if copy_failed:
                # Raise outside the handling arm so the transport's raw error
                # cannot remain attached as __context__. WSL2 copy diagnostics
                # may reflect the key-bearing script through stderr.
                raise ProvisioningError("could not copy the private guest bootstrap staging file") from None
        finally:
            if local_script is not None:
                active_failure = sys.exc_info()[1]
                try:
                    local_script.unlink(missing_ok=True)
                    if local_script.exists():
                        raise OSError("bootstrap staging file still exists")
                except (OSError, KeyboardInterrupt, SystemExit, GeneratorExit) as cleanup_failure:
                    if active_failure is None:
                        if not isinstance(cleanup_failure, OSError):
                            raise
                        raise ProvisioningError(
                            "could not verify removal of the local bootstrap staging file"
                        ) from None
                    _warn_bootstrap_file_residue(
                        "could not verify removal of the local bootstrap staging file; "
                        "primary failure unchanged; plaintext may remain"
                    )

        # Run the bootstrap script synchronously over WSL2's local wsl.exe
        # transport. A detached poll would run in a separate systemd-logind
        # session, where KillUserProcesses can reap it when the first wsl.exe
        # invocation exits. The wrappers prevent terminal-related hangs:
        # setsid removes the controlling TTY, stdin is EOF, and stderr joins
        # captured stdout beside the structured markers.
        output.detail("Running bootstrap script...")
        result = exec_target.run(
            f"setsid sudo -n /bin/bash {shlex.quote(remote_script)} </dev/null 2>&1",
            check=False,
            timeout=900,
        )
    finally:
        active_failure = sys.exc_info()[1]
        quoted_remote_script = shlex.quote(remote_script)
        cleanup_command = f"rm -f -- {quoted_remote_script} && test ! -e {quoted_remote_script}"
        try:
            cleanup_result = exec_target.run(cleanup_command, sudo=True, check=False)
            if not cleanup_result.ok:
                raise SSHError("guest bootstrap staging file still exists")
        except (SSHError, OSError, KeyboardInterrupt, SystemExit, GeneratorExit) as cleanup_failure:
            if active_failure is None:
                if not isinstance(cleanup_failure, (SSHError, OSError)):
                    raise
                raise ProvisioningError("could not verify removal of the guest bootstrap staging file") from None
            _warn_bootstrap_file_residue(
                "could not verify removal of the guest bootstrap staging file; "
                "primary failure unchanged; plaintext may remain"
            )

    bootstrap = parse_bootstrap_output(result.stdout, result.returncode)

    def sanitize(value: str) -> str:
        if not tailscale_auth_key:
            return value
        return value.replace(tailscale_auth_key, "[REDACTED]")

    for step in bootstrap.steps:
        step_name = sanitize(step.name)
        progress.step(step_name)
        if step.success_msg:
            success_msg = sanitize(step.success_msg)
            output.detail(f"{step_name}: {success_msg}")
            progress.output(success_msg)
        for warning in step.warnings:
            safe_warning = sanitize(warning)
            output.warn(safe_warning)
            progress.warning(safe_warning)
        if step.error:
            safe_error = sanitize(step.error)
            output.warn(f"Error: {safe_error}")
            progress.log_error(safe_error)

    if result.stdout:
        progress.output(sanitize(result.stdout))

    if not bootstrap.ok:
        raise SSHError(f"Bootstrap script failed (exit {result.returncode})")

    assert bootstrap.tailscale_ip is not None
    return bootstrap.tailscale_ip
