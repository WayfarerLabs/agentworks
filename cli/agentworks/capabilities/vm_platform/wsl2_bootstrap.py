"""Running WSL2's primary bootstrap script through the distro's root channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.bootstrap_script import (
    generate_bootstrap_script,
    parse_bootstrap_output,
)
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.errors import ProvisioningError
from agentworks.execution import root_script

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform.base import BootstrapProgress
    from agentworks.execution.root_script import RawRun

BOOTSTRAP_DEADLINE_SECONDS = 900


def run_wsl2_bootstrap(
    run_as_root: Callable[[bytes, float], RawRun],
    *,
    admin_username: str,
    ssh_public_key: str,
    hostname: str,
    swap_gib: int,
    progress: BootstrapProgress,
) -> None:
    """Run WSL2's generated bootstrap once and report its steps, raising unless it exits 0.

    The script holds no secret, so it travels on the channel's stdin like any
    root script, and runs synchronously: a detached run would be in its own
    systemd-logind session, where ``KillUserProcesses`` can reap it when the
    ``wsl.exe`` that started it exits. The caller owns the progress sink and
    joins Tailscale afterwards. Nothing here persists VM state.
    """
    output.info("Bootstrapping VM...")
    script = generate_bootstrap_script(
        admin_username=admin_username,
        ssh_public_key=ssh_public_key,
        provisioning_packages=PROVISIONING_PACKAGES,
        hostname=hostname,
        swap=swap_gib,
    )

    output.detail("Running bootstrap script...")
    result = root_script.run(run_as_root, script, deadline=BOOTSTRAP_DEADLINE_SECONDS)
    stdout = result.stdout.decode("utf-8", errors="replace")
    bootstrap = parse_bootstrap_output(stdout, -1 if result.status is None else result.status)

    for step in bootstrap.steps:
        progress.step(step.name)
        if step.success_msg:
            output.detail(f"{step.name}: {step.success_msg}")
            progress.output(step.success_msg)
        for warning in step.warnings:
            output.warn(warning)
            progress.warning(warning)
        if step.error:
            output.warn(f"Error: {step.error}")
            progress.log_error(step.error)

    for text in (stdout, result.stderr.decode("utf-8", errors="replace"), result.channel_tail):
        if text:
            progress.output(text)

    if result.status is None:
        raise ProvisioningError("the outcome of the WSL2 bootstrap script is unknown")
    if not bootstrap.ok:
        raise ProvisioningError(f"the WSL2 bootstrap script failed (exit {result.status})")
