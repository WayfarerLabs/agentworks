"""tmux session management for agentworks sessions.

Each session gets a locked-down tmux session. Session names are globally
unique and used directly as the tmux session name. A restricted tmux config
disables all interactive session management (no splits, no new windows, no
prefix key) while keeping a large scrollback buffer.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.ssh import ExecTarget

RESTRICTED_CONFIG_PATH = "/opt/agentworks/tmux-session.conf"
DEFAULT_HISTORY_LIMIT = 50_000

# Agent tmux socket infrastructure
AGENT_SOCKET_ROOT = "/run/agentworks/agent-tmux-sockets"
AGENT_SOCKET_GROUP = "tmux-agent-access"

# Admin tmux socket infrastructure (mirrors the agent pattern; per-session
# sockets so each admin session creates a fresh tmux server that inherits the
# SetEnv-delivered env from the SSH connection, preventing the prior shared-
# server env-leak across admin sessions).
ADMIN_SOCKET_ROOT = "/run/agentworks/admin-tmux-sockets"


class RunCommand(Protocol):
    """Callable that runs a shell command on a target (e.g. partial of ssh.run).

    The ``env`` kwarg is materialized by the underlying SSH layer as
    ``-o SetEnv=KEY=VALUE`` arguments; see ``agentworks.ssh.run``.
    """

    def __call__(
        self,
        command: str,
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> object: ...


def agent_socket_path(linux_user: str, session_name: str) -> str:
    """Return the tmux socket path for an agent-mode session."""
    return f"{AGENT_SOCKET_ROOT}/{linux_user}/{session_name}.sock"


def admin_socket_path(admin_username: str, session_name: str) -> str:
    """Return the tmux socket path for an admin-mode session."""
    return f"{ADMIN_SOCKET_ROOT}/{admin_username}/{session_name}.sock"


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
        # Directory is correct, but still ensure admin is in the group.
        admin = shlex.quote(admin_username)
        result = target.run(f"usermod -aG {grp} {admin}", sudo=True, check=False)
        if not result.ok:
            from agentworks import output

            output.warn(f"Failed to add {admin_username} to {AGENT_SOCKET_GROUP}, tmux socket access may fail")
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

    Targets the per-user agent socket directory under ``AGENT_SOCKET_ROOT``.
    Use ``cleanup_stale_admin_sockets`` for admin sessions (different root).
    Uses sudo for both the tmux check and file removal -- this is an
    infrastructure maintenance context (vm reinit / agent create).

    Returns the number of stale sockets removed.
    """
    return _cleanup_stale_sockets_under(
        target, f"{AGENT_SOCKET_ROOT}/{linux_user}",
    )


def cleanup_stale_admin_sockets(target: ExecTarget, admin_username: str) -> int:
    """Remove admin-side socket files whose tmux server is no longer running.

    Mirrors ``cleanup_stale_sockets`` for the per-session admin socket
    directory introduced by the env-and-secrets SDD. Called from VM reinit
    to keep the directory from accumulating cruft over a long-lived VM's
    repeated session create/delete cycles.
    """
    return _cleanup_stale_sockets_under(
        target, f"{ADMIN_SOCKET_ROOT}/{admin_username}",
    )


def _cleanup_stale_sockets_under(target: ExecTarget, dir_path: str) -> int:
    """Shared implementation for cleanup_stale_{agent,admin}_sockets."""
    q_dir = shlex.quote(dir_path)
    result = target.run(f"find {q_dir} -name '*.sock' -type s 2>/dev/null", sudo=True, check=False)
    if not result.stdout.strip():
        return 0

    removed = 0
    for sock_path in result.stdout.strip().splitlines():
        sock_path = sock_path.strip()
        if not sock_path:
            continue
        q_sock = shlex.quote(sock_path)
        check = target.run(f"tmux -S {q_sock} list-sessions 2>/dev/null", sudo=True, check=False)
        if not check.ok:
            target.run(f"rm -f {q_sock}", sudo=True, check=False)
            removed += 1
    return removed


def ensure_admin_socket_root(
    target: ExecTarget,
    admin_username: str,
) -> None:
    """Create the admin tmux socket root directory (idempotent).

    Simpler than ``ensure_agent_socket_root``: admin sockets only need to be
    accessible to the admin user, so no shared group is involved. The root
    directory is owned by the admin user and sits at mode 0700.
    """
    q_root = shlex.quote(ADMIN_SOCKET_ROOT)
    q_admin_dir = shlex.quote(f"{ADMIN_SOCKET_ROOT}/{admin_username}")
    q_admin = shlex.quote(admin_username)

    probe = target.run(
        f'if test -d {q_admin_dir}; then stat -c "%U %a" {q_admin_dir} 2>/dev/null || echo PROBE_FAILED; '
        f"else echo MISSING; fi",
        sudo=True,
        check=False,
    )
    if probe.stdout.strip() == f"{admin_username} 700":
        return

    target.run(f"mkdir -p {q_root}", sudo=True)
    target.run(f"chown root:root {q_root}", sudo=True)
    target.run(f"chmod 0755 {q_root}", sudo=True)
    target.run(f"mkdir -p {q_admin_dir}", sudo=True)
    target.run(f"chown {q_admin}:{q_admin} {q_admin_dir}", sudo=True)
    target.run(f"chmod 0700 {q_admin_dir}", sudo=True)


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

# When tmux spawns a default-shell pane (no explicit pane command, as in
# `tmux new-session -d` with no trailing command), source the operator's
# login dotfiles so profile fragments installed by VM init
# (/etc/profile.d/agentworks-identity.sh, ~/.agentworks-profile.sh) are
# loaded. Without this, tmux's default-shell runs non-login and the
# AGENTWORKS_VM identity vars and PATH additions from the fragments
# would not appear in no-command sessions.
set -g default-command "$SHELL -l"

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


def tmux_cmd(base: str, socket_path: str | None = None, *, sudo: bool = False) -> str:
    """Build a tmux command string, optionally with ``-S`` and ``sudo``.

    Session commands (has-session, kill-session, send-keys, capture-pane) do
    NOT use sudo -- socket access goes through group permissions, and failures
    surface as BROKEN status. ``sudo=True`` is only for infrastructure
    operations (e.g. cleanup_stale_sockets probing sockets during setup).
    """
    cmd = f"tmux -S {shlex.quote(socket_path)} {base}" if socket_path else f"tmux {base}"
    return f"sudo -n {cmd}" if sudo else cmd


def _grant_server_access(
    run_command: RunCommand,
    socket_path: str,
) -> None:
    """Grant tmux server-access to every member of the socket group.

    Called as the agent (the tmux server owner) post FRD R1. No inner
    sudo is needed: the agent runs ``tmux server-access`` against its
    own server.
    """
    q_sock = shlex.quote(socket_path)
    grp = shlex.quote(AGENT_SOCKET_GROUP)
    run_command(
        f"for u in $(getent group {grp} | cut -d: -f4 | tr ',' ' '); do "
        f'tmux -S {q_sock} server-access -a "$u"; '
        f"done",
    )


def _tmux_env_flags(env: dict[str, str] | None) -> str:
    """Return ``-e KEY=VAL -e KEY=VAL`` flags for ``tmux new-session`` /
    ``tmux split-window``.

    Tmux's session-environment table propagates these vars to every pane in
    the session (and to panes spawned later via ``split-window``). Belt-and-
    suspenders with the SSH SetEnv layer: SetEnv brings the vars into the
    SSH command's shell, which tmux then inherits when it spawns/starts the
    server; tmux's ``-e`` makes the propagation explicit per-session, which
    matters when the same operator opens multiple sessions on a long-lived
    tmux server (each session ends up with its own per-session env).

    Returns the empty string for empty / None input so call sites can
    string-concat without conditionals.
    """
    if not env:
        return ""
    parts = [f"-e {shlex.quote(f'{k}={v}')}" for k, v in env.items()]
    return " " + " ".join(parts)


def _pane_command(command: str, q_path: str) -> str:
    """Return the pane command for ``tmux new-session``.

    Shape:

    - command non-empty: ``$SHELL -lic 'cd <path> && exec <command>'``
    - command empty: ``""`` (let tmux fall back to its default-shell login)

    Defensive against a caller pre-prepending ``exec``: this function is the
    sole owner of the exec wrapping, so a leading ``exec`` on the input is
    stripped before re-applying. (A prior Phase 3 pass had both
    ``_build_session_command`` and ``_pane_command`` emitting their own
    ``exec``, producing ``cd ... && exec exec <cmd>``; that's harmless at
    runtime but visible in scrollback and confusing.)

    Env injection is NOT part of this string. Env reaches the pane via
    ``tmux new-session -e KEY=VAL`` (which seeds the session-environment
    table) and via SSH SetEnv (which seeds the tmux server's process env
    when the server starts).
    """
    if not command:
        return ""
    stripped = command.removeprefix("exec ").lstrip()
    inner = shlex.quote(f"cd {q_path} && exec {stripped}")
    return f"$SHELL -lic {inner}"


def create_session(
    session_name: str,
    workspace_path: str,
    command: str,
    linux_user: str | None,
    *,
    run_command: RunCommand,
    target: ExecTarget | None = None,
    admin_username: str | None = None,
    is_admin: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[str, int | None]:
    """Create a locked-down tmux session.

    Both admin and agent modes use per-session sockets under
    ``/run/agentworks/<mode>-tmux-sockets/<user>/<name>.sock`` (per the env-
    and-secrets SDD: each session gets a fresh tmux server, so the SetEnv-
    delivered env from the SSH connection reaches the pane via the server
    inheriting its own creation env).

    For admin mode, ``run_command`` is admin's SSH connection. ``target`` is
    used for sudo socket-root setup (writing under ``/run/agentworks/``).
    For agent mode, ``run_command`` is the AGENT's SSH connection (FRD R1),
    and ``target`` must be admin's ExecTarget (for AGENT_SOCKET_ROOT setup).

    ``env`` flows to the pane via two channels (belt and suspenders):
    1. ``run_command`` materializes ``-o SetEnv=K=V`` args on the SSH
       command line so sshd injects the vars into the user's shell that
       runs ``tmux new-session``; tmux inherits.
    2. ``-e KEY=VAL`` on ``tmux new-session`` seeds the session-environment
       table so per-session env stays scoped to this session.

    Returns (socket_path, tmux_server_pid).
    """
    assert linux_user is not None
    assert admin_username is not None, "admin_username required for create_session"
    assert target is not None, "target (admin's ExecTarget) required for create_session"

    q_session = shlex.quote(session_name)
    q_path = shlex.quote(workspace_path)
    pane_cmd = _pane_command(command, q_path)
    env_flags = _tmux_env_flags(env)

    # Per-session socket setup. The admin path uses the simpler shape (no
    # cross-user group magic); the agent path keeps the existing group-shared
    # plumbing because admin needs attach access to the agent's tmux server
    # (per the 2026-04-10 agent-tmux-sockets SDD).
    if is_admin:
        ensure_admin_socket_root(target, admin_username)
        sock = admin_socket_path(admin_username, session_name)
    else:
        ensure_agent_socket_root(target, admin_username)
        ensure_agent_socket_dir(target, linux_user)
        sock = agent_socket_path(linux_user, session_name)
    q_sock = shlex.quote(sock)

    # Stale-socket handling: if a socket file exists but no server is alive
    # behind it, remove it before creating the new session. An active server
    # is a conflict (something else owns this name).
    sock_exists = run_command(f"test -e {q_sock}", check=False)
    if getattr(sock_exists, "ok", False):
        server_alive = run_command(
            f"tmux -S {q_sock} list-sessions 2>/dev/null", check=False
        )
        if getattr(server_alive, "ok", False):
            raise RuntimeError(
                f"Socket {sock} already has an active tmux server. "
                f"Kill it first or choose a different session name."
            )
        from agentworks import output as _output

        _output.detail(f"Removing stale socket: {sock}")
        run_command(f"rm -f {q_sock}", check=False)

    # Create the session. SetEnv vars travel with run_command; tmux's -e
    # flags add them to the session-environment table.
    cmd = (
        f"tmux -S {q_sock} new-session -d -s {q_session} "
        f"-c {q_path} -f {RESTRICTED_CONFIG_PATH}{env_flags}"
    )
    if pane_cmd:
        cmd += f" {shlex.quote(pane_cmd)}"
    run_command(cmd, env=env)

    # Socket permissions + cross-user access only matter for agent sessions
    # (admin has direct access to its own per-session socket).
    if not is_admin:
        run_command(f"chmod g+rwx {q_sock}")
        _grant_server_access(run_command, sock)

    try:
        pid_out = run_command(tmux_cmd("display-message -p '#{pid}'", sock), check=False)
        pid: int | None = _parse_pid(getattr(pid_out, "stdout", ""), context="after session create")
    except (RuntimeError, ValueError):
        pid = None  # best-effort; auto-repair will recover on next access
    return (sock, pid)


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


def _parse_pid(raw: str, context: str) -> int:
    """Parse a PID from tmux display-message output. Raises RuntimeError on failure."""
    pid_str = raw.strip()
    if not pid_str:
        raise RuntimeError(f"tmux returned empty PID output ({context})")
    try:
        pid = int(pid_str)
    except ValueError:
        raise RuntimeError(f"tmux returned non-numeric PID: {pid_str!r} ({context})") from None
    if pid <= 0:
        raise RuntimeError(f"tmux returned invalid PID: {pid} ({context})")
    return pid


# -- PID-based liveness helpers --------------------------------------------


def get_tmux_server_pid(
    *,
    target: ExecTarget,
    socket_path: str | None = None,
) -> int | None:
    """Retrieve the PID of a running tmux server.

    Returns None if the server is not running or unreachable.
    """
    cmd = tmux_cmd("display-message -p '#{pid}'", socket_path) + " 2>/dev/null"
    result = target.run(cmd, check=False)
    if not result.ok:
        return None
    pid_str = result.stdout.strip()
    if not pid_str:
        return None
    try:
        pid = int(pid_str)
    except ValueError:
        return None
    return pid if pid > 0 else None


def force_kill_tmux_server(
    pid: int,
    *,
    target: ExecTarget,
    socket_path: str | None = None,
    log: Callable[[str], None] | None = None,
    use_sudo: bool = True,
) -> bool:
    """Kill a tmux server by PID with SIGTERM -> SIGKILL escalation.

    Cleans up socket file if present. Returns True if the process is dead.

    ``use_sudo`` defaults True for the admin path (cross-uid kill of an
    agent's tmux pid; admin's NOPASSWD sudo). Pass False when ``target`` is
    the agent's own ExecTarget: the agent can kill its own pid and remove
    its own socket without sudo. FRD R1's carve-out for admin force-kill
    applies to batch operations; single-session agent ops go through agent
    SSH directly (no sudo).
    """
    if pid <= 1:
        raise ValueError(f"refusing to kill PID {pid} (dangerous special value)")
    import time

    def _log(msg: str) -> None:
        if log:
            log(msg)

    # SIGTERM
    _log(f"Sending SIGTERM to PID {pid}")
    target.run(f"kill {pid}", sudo=use_sudo, check=False)
    time.sleep(2)

    # Check if still alive
    if target.run(f"test -d /proc/{pid}", check=False).ok:
        _log(f"PID {pid} survived SIGTERM, escalating to SIGKILL")
        target.run(f"kill -9 {pid}", sudo=use_sudo, check=False)
        time.sleep(1)

    # Final check
    if target.run(f"test -d /proc/{pid}", check=False).ok:
        _log(f"PID {pid} survived SIGKILL")
        return False  # process survived

    _log(f"PID {pid} is dead")

    # Clean up stale socket (validate path is under expected root)
    if socket_path and socket_path.startswith(AGENT_SOCKET_ROOT + "/"):
        _log(f"Removing stale socket {socket_path}")
        target.run(f"rm -f {shlex.quote(socket_path)}", sudo=use_sudo, check=False)

    return True
