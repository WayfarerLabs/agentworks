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

    Args:
        target: Remote execution target.
        command: Shell command to run.
        label: Human-readable label for progress messages.
        base_path: Base path for output/pid/status files (unique per operation).
        poll_interval: Seconds between polls.
        quiet_timeout: Warn if no new output for this many seconds.
        timeout: Hard timeout in seconds. Returns exit code 1 if exceeded.
        as_root: Run the wrapper script as root.

    Returns:
        DetachedResult with exit code and full output.
    """
    output_file = f"{base_path}.out"
    pid_file = f"{base_path}.pid"
    status_file = f"{base_path}.status"
    wrapper_file = f"{base_path}.sh"

    run_fn = target.run_as_root if as_root else target.run

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
        run_fn(f"rm -f {output_file} {pid_file} {status_file}", check=False)

        # Launch detached. We need nohup on the OUTSIDE of sudo so sudo
        # itself is protected from the SIGHUP sent when the SSH shell exits.
        # Redirect all fds so SSH returns immediately.
        #
        # Critical: this command must NOT run over an SSH PTY (-tt). When SSH
        # allocates a PTY (as on Windows where force_tty=True), closing it
        # sends SIGHUP to the entire foreground process group before nohup
        # can intercept. We temporarily disable force_tty for this one call.
        from dataclasses import is_dataclass, replace

        no_tty_target = target
        if (
            target.ssh is not None
            and is_dataclass(target.ssh)
            and getattr(target.ssh, "force_tty", False)
        ):
            no_tty_ssh = replace(target.ssh, force_tty=False)
            no_tty_target = replace(target, ssh=no_tty_ssh)

        if as_root:
            nohup_cmd = f"nohup sudo -n /bin/bash {wrapper_file} </dev/null >/dev/null 2>&1 &"
            no_tty_target.run(nohup_cmd, check=False)
        else:
            nohup_cmd = f"nohup /bin/bash {wrapper_file} </dev/null >/dev/null 2>&1 &"
            no_tty_target.run(nohup_cmd, check=False)

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

    # Cleanup remote files
    run_fn(f"rm -f {wrapper_file} {pid_file} {status_file} {output_file}", check=False)

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

        # Warn if no output for a while
        quiet_secs = time.monotonic() - last_output_time
        if quiet_secs > quiet_timeout and not warned_quiet:
            typer.echo(
                f"  {label}: no output for {int(quiet_secs)}s (still running)...",
                err=True,
            )
            warned_quiet = True

    # Read the full output for the caller
    result = target.run(f"cat {output_file} 2>/dev/null", check=False)
    return result.stdout


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
