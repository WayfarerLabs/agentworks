"""tmux session management for agentworks sessions.

Each session gets a locked-down tmux session. Session names are globally
unique and used directly as the tmux session name. A restricted tmux config
disables all interactive session management (no splits, no new windows, no
prefix key) while keeping a large scrollback buffer.
"""

from __future__ import annotations

import enum
import shlex
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agentworks.ssh import ExecTarget

RESTRICTED_CONFIG_PATH = "/opt/agentworks/tmux-session.conf"
DEFAULT_HISTORY_LIMIT = 50_000

# Agent tmux socket infrastructure
AGENT_SOCKET_ROOT = "/run/agentworks/agent-tmux-sockets"
AGENT_SOCKET_GROUP = "tmux-agent-access"


class RunCommand(Protocol):
    """Callable that runs a shell command on a target (e.g. partial of ssh.run)."""

    def __call__(self, command: str, *, check: bool = True) -> object: ...


def agent_socket_path(linux_user: str, session_name: str) -> str:
    """Return the tmux socket path for an agent-mode session."""
    return f"{AGENT_SOCKET_ROOT}/{linux_user}/{session_name}.sock"


def ensure_agent_socket_root(
    target: ExecTarget,
    admin_username: str,
    *,
    warn_if_missing: bool = True,
) -> None:
    """Create the agent tmux socket root directory and group (idempotent).

    Fast-paths when the directory already exists with the correct group and
    permissions (probe + group membership check).

    Pass ``warn_if_missing=False`` when the caller already knows the directory
    won't exist (e.g. first-time VM init), to avoid a misleading warning.
    """
    grp = shlex.quote(AGENT_SOCKET_GROUP)
    q_root = shlex.quote(AGENT_SOCKET_ROOT)

    probe = target.run(
        f'if test -d {q_root}; then stat -c "%G %a" {q_root} 2>/dev/null || echo PROBE_FAILED; '
        f"else echo MISSING; fi",
        sudo=True,
        check=False,
    )
    stdout = probe.stdout.strip()
    if stdout == f"{AGENT_SOCKET_GROUP} 2771":
        # Directory is correct, but still ensure admin is in the group
        # (needed since we no longer use sudo for runtime tmux operations).
        admin = shlex.quote(admin_username)
        grp = shlex.quote(AGENT_SOCKET_GROUP)
        target.run(f"usermod -aG {grp} {admin}", sudo=True, check=False)
        return

    if stdout == "MISSING":
        should_warn, state = warn_if_missing, "missing"
    elif stdout == "PROBE_FAILED":
        should_warn, state = True, "probe failed"
    else:
        should_warn, state = True, "misconfigured"

    if should_warn:
        from agentworks import output

        output.warn(f"Socket root {AGENT_SOCKET_ROOT} {state}, recreating")

    admin = shlex.quote(admin_username)
    result = target.run(f"getent group {grp} >/dev/null 2>&1", check=False)
    if not result.ok:
        target.run(f"/usr/sbin/groupadd {grp}", sudo=True)
    target.run(f"usermod -aG {grp} {admin}", sudo=True)
    target.run(f"mkdir -p {AGENT_SOCKET_ROOT}", sudo=True)
    target.run(f"chown root:{grp} {AGENT_SOCKET_ROOT}", sudo=True)
    target.run(f"chmod 2771 {AGENT_SOCKET_ROOT}", sudo=True)


def ensure_agent_socket_dir(
    target: ExecTarget,
    linux_user: str,
    *,
    warn_if_missing: bool = True,
) -> None:
    """Create a per-agent tmux socket directory (idempotent).

    Fast-paths when the directory already exists with the correct owner/group
    and permissions (single SSH round-trip).
    """
    q_user = shlex.quote(linux_user)
    grp = shlex.quote(AGENT_SOCKET_GROUP)
    q_path = shlex.quote(f"{AGENT_SOCKET_ROOT}/{linux_user}")

    probe = target.run(
        f'if test -d {q_path}; then stat -c "%U %G %a" {q_path} 2>/dev/null || echo PROBE_FAILED; '
        f"else echo MISSING; fi",
        sudo=True,
        check=False,
    )
    stdout = probe.stdout.strip()
    if stdout == f"{linux_user} {AGENT_SOCKET_GROUP} 2770":
        return

    if stdout == "MISSING":
        should_warn, state = warn_if_missing, "missing"
    elif stdout == "PROBE_FAILED":
        should_warn, state = True, "probe failed"
    else:
        should_warn, state = True, "misconfigured"

    if should_warn:
        from agentworks import output

        output.warn(f"Socket directory for {linux_user} {state}, recreating")

    target.run(f"mkdir -p {q_path}", sudo=True)
    target.run(f"chown {q_user}:{grp} {q_path}", sudo=True)
    target.run(f"chmod 2770 {q_path}", sudo=True)


def cleanup_stale_sockets(target: ExecTarget, linux_user: str) -> int:
    """Remove socket files whose tmux server is no longer running.

    Returns the number of stale sockets removed.
    """
    q_dir = shlex.quote(f"{AGENT_SOCKET_ROOT}/{linux_user}")
    result = target.run(f"find {q_dir} -name '*.sock' -type s 2>/dev/null", sudo=True, check=False)
    if not result.stdout.strip():
        return 0

    removed = 0
    for sock_path in result.stdout.strip().splitlines():
        sock_path = sock_path.strip()
        if not sock_path:
            continue
        q_sock = shlex.quote(sock_path)
        # Use sudo for the tmux check -- this is an infrastructure maintenance
        # context (vm reinit / agent create), so we need reliable results
        # regardless of admin's socket permissions or tmux ACLs.
        check = target.run(f"tmux -S {q_sock} list-sessions 2>/dev/null", sudo=True, check=False)
        if not check.ok:
            target.run(f"rm -f {q_sock}", sudo=True, check=False)
            removed += 1
    return removed


def generate_restricted_config(history_limit: int = DEFAULT_HISTORY_LIMIT) -> str:
    """Generate the locked-down tmux config for sessions.

    Loads the user's tmux.conf first so that familiar keybindings (prefix key,
    detach, copy mode, etc.) work for direct session attach. Then disables
    window/pane/session management on top to enforce one session per tmux server.
    When inside the console, the console's prefix eclipses the session's, so the
    session-level bindings are effectively invisible.
    """
    return f"""\
# Generated by agentworks. Do not edit.
# Locked-down config for agentworks sessions.
#
# Loads user tmux.conf for familiar keybindings (prefix, detach, copy mode),
# then disables window/pane/session creation to enforce one session per server.

# Load user config first
if-shell "test -f ~/.tmux.conf" "source-file ~/.tmux.conf"

# Large scrollback buffer (override user config)
set -g history-limit {history_limit}

# Size windows based on the most recently active client, not the smallest.
# Sessions are created detached (default geometry) then attached from
# within the console session. Without this, the inner session stays stuck
# at the small detached size.
set -g window-size latest
set -g aggressive-resize on

# Disable status bar -- the console provides this when nested;
# for direct attach, the session is the only thing on screen.
set -g status off

# Disable window/pane/session creation and management.
# The user's prefix key, detach, copy mode, and scroll bindings are preserved.
unbind c          # new-window
unbind %          # split-window -h
unbind '"'        # split-window -v
unbind &          # kill-window
unbind x          # kill-pane
unbind n          # next-window
unbind p          # previous-window
unbind w          # choose-window
unbind s          # choose-session
unbind $          # rename-session
unbind ,          # rename-window
unbind .          # move-window
unbind !          # break-pane
unbind :          # command-prompt (prevents arbitrary tmux commands)
"""


def deploy_restricted_config(
    run_command: RunCommand,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
) -> None:
    """Write the restricted tmux config to the VM."""
    config = generate_restricted_config(history_limit)
    # Ensure directory exists and write config
    run_command(f"sudo mkdir -p $(dirname {RESTRICTED_CONFIG_PATH})")
    run_command(f"sudo tee {RESTRICTED_CONFIG_PATH} > /dev/null << 'TMUX_CONF'\n{config}TMUX_CONF")


def tmux_cmd(base: str, socket_path: str | None = None) -> str:
    """Build a tmux command string, optionally with ``-S`` for a custom socket.

    Admin has group access to agent sockets via tmux-agent-access, so sudo
    is not needed. If access fails, that indicates a permissions problem
    (restart the session to reapply ACLs, or run vm reinit for filesystem
    permissions).
    """
    return f"tmux -S {shlex.quote(socket_path)} {base}" if socket_path else f"tmux {base}"


def _grant_server_access(
    run_command: RunCommand,
    linux_user: str,
    socket_path: str,
) -> None:
    """Grant tmux server-access to every member of the socket group."""
    q_user = shlex.quote(linux_user)
    q_sock = shlex.quote(socket_path)
    grp = shlex.quote(AGENT_SOCKET_GROUP)
    run_command(
        f"for u in $(getent group {grp} | cut -d: -f4 | tr ',' ' '); do "
        f"sudo -u {q_user} tmux -S {q_sock} server-access -a \"$u\"; "
        f"done",
    )


def create_session(
    session_name: str,
    workspace_path: str,
    command: str,
    linux_user: str | None,
    *,
    run_command: RunCommand,
    target: ExecTarget,
    run_as_root: RunCommand | None = None,
    admin_username: str | None = None,
    is_admin: bool = True,
) -> str | None:
    """Create a locked-down tmux session.

    For admin mode, the command runs directly on the admin's default tmux
    server.  For agent mode, the session is created as the agent Linux user
    with a per-session socket so the agent's tmux server (and shell) run under
    the agent's uid.  The admin gains access via group permissions on the
    socket and the tmux ``server-access`` ACL.

    Returns the socket path for agent-mode sessions, None for admin-mode.
    """
    q_session = shlex.quote(session_name)
    q_path = shlex.quote(workspace_path)

    if is_admin:
        if command:
            inner = shlex.quote(f"cd {q_path} && {command}")
            shell_cmd = f"$SHELL -lic {inner}"
        else:
            shell_cmd = ""

        cmd = f"tmux new-session -d -s {q_session} -c {q_path} -f {RESTRICTED_CONFIG_PATH}"
        if shell_cmd:
            cmd += f" {shlex.quote(shell_cmd)}"
        run_command(cmd)
        return None
    else:
        assert linux_user is not None
        assert run_as_root is not None, "run_as_root required for agent sessions"
        assert admin_username is not None, "admin_username required for agent sessions"
        q_user = shlex.quote(linux_user)
        sock = agent_socket_path(linux_user, session_name)
        q_sock = shlex.quote(sock)

        # Ensure the tmpfs socket directories exist (wiped on VM reboot).
        ensure_agent_socket_root(target, admin_username)
        ensure_agent_socket_dir(target, linux_user)

        # Check for an existing socket file before creating the session.
        # A stale socket (no server) is removed to start clean. An active
        # socket (server running) is an error -- something else is using it.
        sock_exists = run_command(f"test -e {q_sock}", check=False)
        if getattr(sock_exists, "ok", False):
            server_alive = run_as_root(
                f"tmux -S {q_sock} list-sessions 2>/dev/null",
                check=False,
            )
            if getattr(server_alive, "ok", False):
                raise RuntimeError(
                    f"Socket {sock} already has an active tmux server. "
                    f"Kill it first or choose a different session name."
                )
            # Stale socket -- remove it
            from agentworks import output as _output

            _output.detail(f"Removing stale socket: {sock}")
            run_as_root(f"rm -f {q_sock}", check=False)

        # Build the pane command.  sudo --login gives the agent a proper
        # login environment; tmux then starts the pane shell as that user.
        if command:
            inner = shlex.quote(f"cd {q_path} && {command}")
            shell_cmd = f"$SHELL -lic {inner}"
        else:
            shell_cmd = ""

        cmd = (
            f"sudo --login -u {q_user} "
            f"tmux -S {q_sock} new-session -d -s {q_session} "
            f"-c {q_path} -f {RESTRICTED_CONFIG_PATH}"
        )
        if shell_cmd:
            cmd += f" {shlex.quote(shell_cmd)}"
        run_command(cmd)

        # Fix socket permissions (tmux creates sockets mode 0700).
        # Socket is owned by the agent user, so sudo is needed.
        run_as_root(f"chmod g+rwx {q_sock}")

        # Grant tmux server-access to all socket-group members
        _grant_server_access(run_command, linux_user, sock)

        return sock


def kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> bool:
    """Kill a session's tmux session. Returns True if the session existed."""
    q_session = shlex.quote(session_name)
    result = run_command(
        tmux_cmd(f"kill-session -t {q_session}", socket_path),
        check=False,
    )
    return getattr(result, "ok", True)


def session_exists(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> bool:
    """Check if a session's tmux session is alive."""
    q_session = shlex.quote(session_name)
    result = run_command(
        tmux_cmd(f"has-session -t {q_session}", socket_path) + " 2>/dev/null",
        check=False,
    )
    return getattr(result, "ok", False)


class SessionState(enum.Enum):
    """Result of a session liveness check."""

    DEAD = "dead"
    ALIVE = "alive"
    INACCESSIBLE = "inaccessible"  # running but admin can't reach it


def check_session_alive(
    target: ExecTarget,
    session_name: str,
    socket_path: str | None = None,
) -> SessionState:
    """Check if a session is alive, with sudo fallback for socket-based sessions.

    Returns SessionState.ALIVE if the admin can reach the session directly,
    SessionState.INACCESSIBLE if only reachable via sudo (ACL or permission
    issue), or SessionState.DEAD if the session is not running.
    """
    q_session = shlex.quote(session_name)
    cmd = tmux_cmd(f"has-session -t {q_session}", socket_path) + " 2>/dev/null"
    result = target.run(cmd, check=False)
    if result.ok:
        return SessionState.ALIVE

    # For socket-based sessions, try sudo to detect running-but-inaccessible
    if socket_path:
        q_sock = shlex.quote(socket_path)
        sudo_cmd = f"sudo -n tmux -S {q_sock} has-session -t {q_session} 2>/dev/null"
        sudo_result = target.run(sudo_cmd, check=False)
        if sudo_result.ok:
            return SessionState.INACCESSIBLE

    return SessionState.DEAD


class BatchCheckError(Exception):
    """Raised when the batch session check command itself failed (SSH error, etc.)."""


def batch_check_sessions(
    target: ExecTarget,
    checks: list[tuple[str, str | None]],
) -> dict[str, bool]:
    """Check multiple sessions in a single SSH call.

    Args:
        target: ExecTarget for the VM (admin user).
        checks: list of (session_name, socket_path) pairs. socket_path=None
            means check the default tmux server.

    Returns:
        dict mapping session_name to alive (True/False).

    Raises:
        BatchCheckError: if the SSH command itself failed (connection error,
            permission denied, etc.). Callers should warn and skip
            reconciliation rather than marking sessions as stopped.

    Runs as admin, who has group access to all agent sockets via
    tmux-agent-access. Falls back to sudo for socket-based sessions
    when the non-sudo check fails (ACL or permission issue).
    """
    if not checks:
        return {}

    # Preflight: verify tmux is available. Individual has-session calls
    # suppress stderr (2>/dev/null) so a missing tmux would silently
    # return all sessions as dead.
    parts: list[str] = ["command -v tmux >/dev/null || { echo ERROR:TMUX_NOT_FOUND; exit 1; }"]
    for name, sock in checks:
        q_name = shlex.quote(name)
        q_alive = shlex.quote(f"ALIVE:{name}")
        q_error = shlex.quote(f"ERROR:{name}")
        if sock:
            q_sock = shlex.quote(sock)
            # Missing socket = dead (normal, session was stopped and cleaned up).
            # Try without sudo first (admin has group access in the normal case).
            # Fall back to sudo to distinguish "actually dead" from "running but
            # inaccessible" (filesystem permissions or tmux server-access ACL).
            # If sudo succeeds, emit both ALIVE and ERROR so callers can warn.
            parts.append(
                f"if [ ! -e {q_sock} ]; then :; "
                f"elif tmux -S {q_sock} has-session -t {q_name} 2>/dev/null; then echo {q_alive}; "
                f"elif sudo -n tmux -S {q_sock} has-session -t {q_name} 2>/dev/null; "
                f"then echo {q_alive}; echo {q_error}; fi"
            )
        else:
            parts.append(
                f"tmux has-session -t {q_name} 2>/dev/null && echo {q_alive} || true"
            )
    cmd = "; ".join(parts)
    from agentworks.ssh import SSHError

    try:
        result = target.run(cmd, check=False)
    except SSHError as e:
        raise BatchCheckError(f"SSH failed: {e}") from e

    stdout = result.stdout

    # Detect missing tmux (preflight check prints ERROR:TMUX_NOT_FOUND and exits)
    if "ERROR:TMUX_NOT_FOUND" in stdout:
        raise BatchCheckError("tmux is not installed on this VM")

    # Non-zero exit without the TMUX_NOT_FOUND sentinel means something
    # unexpected failed (syntax error, shell issue, etc.). Raise so callers
    # skip reconciliation rather than marking everything as stopped.
    if not result.ok:
        stderr = (result.stderr or "").strip()
        raise BatchCheckError(f"batch check exited {result.returncode}: {stderr}")

    alive_names: set[str] = set()
    error_names: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("ALIVE:"):
            alive_names.add(line[6:])
        elif line.startswith("ERROR:"):
            error_names.add(line[6:])

    result_map: dict[str, bool] = {}
    for name, _ in checks:
        if name in error_names:
            from agentworks import output

            output.warn(
                f"session '{name}': running but socket not accessible by admin "
                f"(use 'session restart {name} --force' to fix)"
            )
        result_map[name] = name in alive_names

    return result_map


def send_keys(
    session_name: str,
    keys: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> None:
    """Send keys to a session's tmux session."""
    q_session = shlex.quote(session_name)
    run_command(
        tmux_cmd(f"send-keys -t {q_session} {keys}", socket_path),
        check=False,
    )


def capture_output(
    session_name: str,
    *,
    run_command: RunCommand,
    lines: int = DEFAULT_HISTORY_LIMIT,
    socket_path: str | None = None,
) -> str:
    """Capture the scrollback buffer from a session."""
    q_session = shlex.quote(session_name)
    result = run_command(
        tmux_cmd(f"capture-pane -t {q_session} -p -S -{lines}", socket_path),
        check=False,
    )
    return getattr(result, "stdout", "") or ""
