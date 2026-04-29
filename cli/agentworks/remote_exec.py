"""Detached remote command execution with nohup + poll.

Runs long-running commands on remote hosts in a way that survives SSH
disconnects. The command runs under nohup with output redirected to a file.
The workstation polls for completion by checking the process status and
tailing new output.

If the workstation reconnects after a drop, it detects the still-running
process and resumes polling instead of starting a new one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer

from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import ExecTarget


@dataclass
class DetachedResult:
    """Result of a detached remote command."""

    exit_code: int
    output: str


# Shell wrapper that writes PID, runs the command, then writes exit status.
_WRAPPER_TEMPLATE = """\
#!/bin/bash
echo $$ > {pid_file}
{command} > {output_file} 2>&1
echo $? > {status_file}
"""


def run_detached(
    target: ExecTarget,
    command: str,
    *,
    label: str = "Remote command",
    base_path: str = "/tmp/agentworks-detached",
    poll_interval: int = 3,
    quiet_timeout: int = 300,
    timeout: int | None = None,
    as_root: bool = False,
    quiet: bool = False,
) -> DetachedResult:
    """Run a command detached on a remote host, polling for completion.

    If a previous run is still in progress (PID file exists, process alive),
    resumes polling instead of starting a new one.

    Running as root: prefer ``as_root=True`` to embedding ``sudo -n`` in the
    command. With ``as_root=True``, the wrapper script itself runs as root so
    the command, its output, and cleanup all happen uniformly with root
    privileges. Inline ``sudo -n`` is only appropriate when parts of a
    multi-step command need different privilege levels (e.g., a pipeline that
    mixes root and non-root stages).

    Args:
        target: Remote execution target.
        command: Shell command to run.
        label: Human-readable label for progress messages.
        base_path: Base path for output/pid/status files (unique per operation).
        poll_interval: Seconds between polls.
        quiet_timeout: Warn if no new output for this many seconds.
        timeout: Hard timeout in seconds. The remote process is killed and
            exit code 1 is returned. Partial output is still captured.
        as_root: Run the wrapper script as root. Prefer this over embedding
            ``sudo -n`` in the command.
        quiet: Suppress progress output (still captured in the result).

    Returns:
        DetachedResult with exit code and full output.
    """
    output_file = f"{base_path}.out"
    pid_file = f"{base_path}.pid"
    status_file = f"{base_path}.status"
    wrapper_file = f"{base_path}.sh"

    # Check for a completed previous run (reconnect after process finished)
    if _status_file_exists(target, status_file):
        if not quiet:
            typer.echo(f"  {label}: found completed result from previous run")
    # Check for an existing running process (resume scenario)
    elif _is_running(target, pid_file):
        if not quiet:
            typer.echo(f"  {label}: resuming in-progress operation...")
    else:
        # Write and start the wrapper script
        wrapper = _WRAPPER_TEMPLATE.format(
            command=command,
            output_file=output_file,
            pid_file=pid_file,
            status_file=status_file,
        )
        target.write_file(wrapper_file, wrapper)

        # Clear any stale files from a previous run
        target.run(f"rm -f {output_file} {pid_file} {status_file}", sudo=as_root, check=False)

        # Launch detached. nohup must be OUTSIDE sudo so that SIGHUP (from
        # SSH PTY teardown) hits nohup first, not sudo. tty=False is the
        # primary protection (no PTY = no SIGHUP), but the nohup ordering
        # provides defense-in-depth. We don't use sudo=True here because
        # that wraps in bash -c, putting nohup inside the sudo'd shell.
        if as_root:
            nohup_cmd = f"nohup sudo -n /bin/bash {wrapper_file} </dev/null >/dev/null 2>&1 &"
        else:
            nohup_cmd = f"nohup /bin/bash {wrapper_file} </dev/null >/dev/null 2>&1 &"
        target.run(nohup_cmd, tty=False, check=False)

        # Brief pause for PID file to be written
        time.sleep(0.5)

        if not quiet:
            typer.echo(f"  {label}: started (detached)")

    # Poll for completion
    output = _poll_until_done(
        target,
        output_file,
        pid_file,
        status_file,
        label=label,
        poll_interval=poll_interval,
        quiet_timeout=quiet_timeout,
        timeout=timeout,
        quiet=quiet,
    )

    # Read exit code
    exit_code = _read_exit_code(target, status_file)

    # Cleanup remote files (best-effort, may fail if SSH is still recovering)
    import contextlib

    with contextlib.suppress(SSHError):
        target.run(f"rm -f {wrapper_file} {pid_file} {status_file} {output_file}", sudo=as_root, check=False)

    return DetachedResult(exit_code=exit_code, output=output)


def _is_running(target: ExecTarget, pid_file: str) -> bool:
    """Check if a detached process is still running."""
    # Check PID file exists
    result = target.run(f"test -f {pid_file}", check=False)
    if result.returncode != 0:
        return False
    # Read PID and check if process is alive (ps -p works regardless of user)
    result = target.run(f"ps -p $(cat {pid_file}) > /dev/null 2>&1", check=False)
    return result.returncode == 0


def _status_file_exists(target: ExecTarget, status_file: str) -> bool:
    """Check if a status file exists (process completed)."""
    result = target.run(f"test -f {status_file}", check=False)
    return result.returncode == 0


def _poll_until_done(
    target: ExecTarget,
    output_file: str,
    pid_file: str,
    status_file: str,
    *,
    label: str,
    poll_interval: int,
    quiet_timeout: int,
    timeout: int | None = None,
    quiet: bool = False,
) -> str:
    """Poll the remote process until it completes, streaming new output."""
    last_size = 0
    last_output_time = time.monotonic()
    start_time = time.monotonic()
    warned_quiet = False

    ssh_failures = 0

    while True:
        time.sleep(poll_interval)

        # Hard timeout -- kill the remote process to avoid orphans
        if timeout is not None and (time.monotonic() - start_time) > timeout:
            typer.echo(
                f"  {label}: timed out after {timeout}s, killing remote process",
                err=True,
            )
            target.run(
                f"test -f {pid_file} && kill $(cat {pid_file}) 2>/dev/null",
                check=False,
            )
            break

        # All polling commands go through SSH which may be temporarily
        # down (e.g., tailscale logout disrupts Azure networking). Catch
        # SSHError and retry -- the wrapper script on the VM keeps running.
        try:
            # Read new output since last poll
            new_output = _read_new_output(target, output_file, last_size)
            if new_output:
                last_output_time = time.monotonic()
                warned_quiet = False
                if not quiet:
                    for line in new_output.splitlines():
                        typer.echo(f"  {line}")
                last_size += len(new_output.encode("utf-8"))

            # Check if process finished (status file exists)
            status_check = target.run(f"test -f {status_file}", check=False)
            if status_check.returncode == 0:
                # Process done -- read any remaining output
                final_output = _read_new_output(target, output_file, last_size)
                if final_output and not quiet:
                    for line in final_output.splitlines():
                        typer.echo(f"  {line}")
                break

            # Check if process is still alive (PID check)
            if not _is_running(target, pid_file):
                # Process gone but no status file -- unexpected termination
                break

            # Reset SSH failure counter on success
            if ssh_failures > 0 and not quiet:
                typer.echo(f"  {label}: connection restored")
            ssh_failures = 0

        except SSHError:
            ssh_failures += 1
            if not quiet:
                if ssh_failures == 1:
                    typer.echo(f"  {label}: connection lost, waiting for recovery...")
                elif ssh_failures % 6 == 0:
                    typer.echo(f"  {label}: still waiting... ({ssh_failures * poll_interval}s)")
            # Don't break -- the wrapper script is still running on the VM
            continue

        # Warn if no output for a while
        quiet_secs = time.monotonic() - last_output_time
        if quiet_secs > quiet_timeout and not warned_quiet:
            typer.echo(
                f"  {label}: no output for {int(quiet_secs)}s (still running)...",
                err=True,
            )
            warned_quiet = True

    # Read the full output for the caller. Retry on SSH failure since the
    # connection may still be recovering after a transient disruption.
    for _read_attempt in range(6):
        try:
            result = target.run(f"cat {output_file} 2>/dev/null", check=False)
            return result.stdout
        except SSHError:
            time.sleep(5)
    return ""  # give up after retries


def _read_new_output(target: ExecTarget, output_file: str, offset: int) -> str:
    """Read new bytes from the output file since the given offset."""
    result = target.run(
        f"tail -c +{offset + 1} {output_file} 2>/dev/null",
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _read_exit_code(target: ExecTarget, status_file: str) -> int:
    """Read the exit code from the status file."""
    result = target.run(f"cat {status_file} 2>/dev/null", check=False)
    try:
        return int(result.stdout.strip())
    except (ValueError, AttributeError):
        return 1  # assume failure if we can't read it
