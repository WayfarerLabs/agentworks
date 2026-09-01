"""tmux session management for agentworks sessions.

Each session gets a locked-down tmux session. Session names are globally
unique and used directly as the tmux session name. A restricted tmux config
disables all interactive session management (no splits, no new windows, no
prefix key) while keeping a large scrollback buffer.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol

from agentworks.errors import StateError
from agentworks.naming import LINUX_USERNAME_MAX_LENGTH

if TYPE_CHECKING:
    from agentworks.transports import Transport

RESTRICTED_CONFIG_PATH = "/opt/agentworks/tmux-session.conf"
DEFAULT_HISTORY_LIMIT = 50_000

# How much of a dead workload's output is folded into the create-time error:
# the last N nonempty lines (the tail is where a dying tool's own error
# message lands), each capped to a per-line character budget. Both caps keep
# the operator-visible message readable.
DEAD_WORKLOAD_OUTPUT_LINES = 15
DEAD_WORKLOAD_OUTPUT_LINE_CHARS = 500

# Agent tmux socket infrastructure
AGENT_SOCKET_ROOT = "/run/agentworks/agent-tmux-sockets"
AGENT_SOCKET_GROUP = "tmux-agent-access"

# AF_UNIX socket paths are capped by ``sizeof(sun_path)`` (Linux <sys/un.h>):
# 108 bytes. A pathname socket needs a trailing NUL, so the usable path length
# is 107. (Live-measured on Linux: a 107-char bind succeeds, 108 fails with
# "File name too long".)
SUN_PATH_MAX = 108

# Session names embed into the per-agent tmux socket path
# ``agent_socket_path`` builds: f"{AGENT_SOCKET_ROOT}/{linux_user}/{name}.sock".
# The tightest case is the longest possible agent username: ``agt-`` plus a
# max-length agent name is exactly LINUX_USERNAME_MAX_LENGTH (32), the Linux
# username ceiling. The fixed socket-path overhead under that username is the
# socket root, two separators, the username, and the ".sock" suffix; whatever
# remains under the usable path length (SUN_PATH_MAX - 1) is the session-name
# budget. Deriving the cap from ``AGENT_SOCKET_ROOT`` in this same module ties
# it to the real prefix: a socket-root change shifts the cap automatically
# rather than silently reintroducing an unbindable path (and a pinned test
# asserts the worst-case path is exactly 107 at the cap). Admin sessions embed
# into an equal-length root under an equal-capped username, so this cap is safe
# for them too. At 34 this stays well clear of the freeform 64 that console /
# vm-site names use, so it is NOT redundant with MAX_FREEFORM_NAME_LENGTH.
_SESSION_SOCKET_OVERHEAD = len(AGENT_SOCKET_ROOT) + len("/") + LINUX_USERNAME_MAX_LENGTH + len("/") + len(".sock")
MAX_SESSION_NAME_LENGTH = (SUN_PATH_MAX - 1) - _SESSION_SOCKET_OVERHEAD  # 107 - 73 = 34

# Admin tmux socket infrastructure (mirrors the agent pattern; per-session
# sockets so each admin session creates a fresh tmux server that inherits the
# SetEnv-delivered env from the SSH connection, preventing the prior shared-
# server env-leak across admin sessions).
ADMIN_SOCKET_ROOT = "/run/agentworks/admin-tmux-sockets"


class RunCommand(Protocol):
    """Callable that runs a shell command on a target (e.g. partial of ``Transport.run``).

    The ``env`` kwarg is materialized by the underlying SSH layer as
    ``-o SetEnv=KEY=VALUE`` arguments; see ``Transport.run`` /
    ``agentworks.transports.ssh.SSHTransport.run``.
    """

    def __call__(
        self,
        command: str,
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class TmuxServerFingerprint:
    """The persisted identity of one Linux tmux server process."""

    pid: int
    boot_id: str
    start_ticks: int


class ProbeStatus(Enum):
    """Tri-state result for a remote runtime-presence probe."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntegerProbe:
    """A tri-state remote probe carrying a positive integer when present."""

    status: ProbeStatus
    value: int | None = None


@dataclass(frozen=True)
class FingerprintProbe:
    """A tri-state stable tmux-server fingerprint probe."""

    status: ProbeStatus
    fingerprint: TmuxServerFingerprint | None = None


def _presence_from_result(result: object) -> ProbeStatus:
    """Classify the command convention used by tmux and ``test`` probes."""
    returncode = getattr(result, "returncode", None)
    if returncode is None:
        return ProbeStatus.PRESENT if getattr(result, "ok", False) else ProbeStatus.ABSENT
    if returncode == 0:
        return ProbeStatus.PRESENT
    if returncode == 1:
        return ProbeStatus.ABSENT
    return ProbeStatus.UNKNOWN


def agent_socket_dir(linux_user: str) -> str:
    """Return the per-agent tmux socket directory under ``AGENT_SOCKET_ROOT``.

    Single source of truth for the per-agent socket path so create
    (``ensure_agent_socket_dir``) and teardown agree on the location.
    """
    return f"{AGENT_SOCKET_ROOT}/{linux_user}"


def agent_socket_path(linux_user: str, session_name: str) -> str:
    """Return the tmux socket path for an agent-mode session."""
    return f"{agent_socket_dir(linux_user)}/{session_name}.sock"


def admin_socket_path(admin_username: str, session_name: str) -> str:
    """Return the tmux socket path for an admin-mode session."""
    return f"{ADMIN_SOCKET_ROOT}/{admin_username}/{session_name}.sock"


def ensure_agent_socket_root(
    target: Transport,
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
        f'if test -d {q_root}; then stat -c "%G %a" {q_root} 2>/dev/null || echo PROBE_FAILED; else echo MISSING; fi',
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
    target: Transport,
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
    q_path = shlex.quote(agent_socket_dir(linux_user))

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


def cleanup_stale_sockets(target: Transport, linux_user: str) -> int:
    """Remove socket files whose tmux server is no longer running.

    Targets the per-user agent socket directory under ``AGENT_SOCKET_ROOT``.
    Use ``cleanup_stale_admin_sockets`` for admin sessions (different root).
    Uses sudo for both the tmux check and file removal -- this is an
    infrastructure maintenance context (vm reinit / agent create).

    Returns the number of stale sockets removed.
    """
    return _cleanup_stale_sockets_under(
        target,
        agent_socket_dir(linux_user),
    )


def cleanup_stale_admin_sockets(target: Transport, admin_username: str) -> int:
    """Remove admin-side socket files whose tmux server is no longer running.

    Mirrors ``cleanup_stale_sockets`` for the per-session admin socket
    directory introduced by the env-and-secrets SDD. Called from VM reinit
    to keep the directory from accumulating cruft over a long-lived VM's
    repeated session create/delete cycles.
    """
    return _cleanup_stale_sockets_under(
        target,
        f"{ADMIN_SOCKET_ROOT}/{admin_username}",
    )


def _cleanup_stale_sockets_under(target: Transport, dir_path: str) -> int:
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
    target: Transport,
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

    Called as the agent (the tmux server owner). No inner
    sudo is needed: the agent runs ``tmux server-access`` against its
    own server.
    """
    q_sock = shlex.quote(socket_path)
    grp = shlex.quote(AGENT_SOCKET_GROUP)
    run_command(
        f"for u in $(getent group {grp} | cut -d: -f4 | tr ',' ' '); do tmux -S {q_sock} server-access -a \"$u\"; done",
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
    stripped before re-applying. (A prior version had the command source
    and ``_pane_command`` both emitting their own ``exec``, producing
    ``cd ... && exec exec <cmd>``; that's harmless at runtime but visible
    in scrollback and confusing.)

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


def _trim_captured_output(
    text: str,
    max_lines: int = DEAD_WORKLOAD_OUTPUT_LINES,
    max_line_chars: int = DEAD_WORKLOAD_OUTPUT_LINE_CHARS,
) -> str:
    """Trim captured pane output for embedding in an error message.

    Drops blank lines (a fresh pane's scrollback is mostly trailing blanks),
    keeps only the last ``max_lines`` nonempty lines (where a dying
    workload's own error message lands), and caps each line to
    ``max_line_chars`` characters.
    """
    nonempty = [line.rstrip()[:max_line_chars] for line in text.splitlines() if line.strip()]
    return "\n".join(nonempty[-max_lines:])


_PaneLiveness = Literal["alive", "dead", "unverified"]


def _probe_pane_liveness(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str,
) -> tuple[_PaneLiveness, str]:
    """Probe whether the session's pane has died (``remain-on-exit`` kept it).

    Returns ``(liveness, exit_status)``:

    - ``("dead", status)``: the probe ran and tmux reported a dead pane;
      ``status`` carries ``#{pane_dead_status}`` (may be empty, e.g. for a
      signal death).
    - ``("alive", "")``: the probe ran and tmux reported anything else.
    - ``("unverified", "")``: the probe itself failed twice. This is NOT
      proof of death: a transient transport failure against a healthy
      workload lands here too, so callers must neither kill nor diagnose on
      this answer (the same unreachable-is-not-dead stance as
      ``stop_session``'s BrokenStateError path).

    Both facts ride one round trip: ``#{pane_dead}`` and
    ``#{pane_dead_status}`` in a single display-message.
    """
    q_session = shlex.quote(f"={session_name}")
    cmd = (
        tmux_cmd(f"display-message -p -t {q_session} '#{{pane_dead}} #{{pane_dead_status}}'", socket_path)
        + " 2>/dev/null"
    )
    for _attempt in range(2):  # retry once: a single blip must not condemn the create
        result = run_command(cmd, check=False)
        if getattr(result, "ok", False):
            flag, _sep, status = (getattr(result, "stdout", "") or "").strip().partition(" ")
            if flag == "1":
                return ("dead", status.strip())
            return ("alive", "")
    return ("unverified", "")


def _raise_workload_died(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str,
    exit_status: str = "",
) -> NoReturn:
    """Capture a dead pane's output, kill its server, and raise a typed error.

    The capture must run before the kill (the scrollback dies with the
    server). The kill is best-effort: the raise is the load-bearing part,
    and a surviving server either self-removes its socket (server dead) or
    surfaces as the active-server conflict when the name is reused.
    """
    captured = _trim_captured_output(
        capture_output(session_name, run_command=run_command, socket_path=socket_path, join_wrapped=True),
    )
    if not captured:
        # A TUI that crashed after entering the alternate screen leaves its
        # message there, invisible to the main-screen capture. One
        # best-effort attempt; tmux errors out when no alternate screen
        # exists, hence check=False.
        q_session = shlex.quote(f"={session_name}")
        alt = run_command(
            tmux_cmd(f"capture-pane -t {q_session} -p -J -a", socket_path) + " 2>/dev/null",
            check=False,
        )
        captured = _trim_captured_output(getattr(alt, "stdout", "") or "")
    run_command(tmux_cmd("kill-server", socket_path) + " 2>/dev/null", check=False)
    status_note = f" (status {exit_status})" if exit_status else ""
    if captured:
        indented = "\n".join(f"  {line}" for line in captured.splitlines())
        detail = f"; its output:\n{indented}"
    else:
        detail = " and produced no output"
    raise StateError(
        f"session workload exited immediately after launch{status_note}{detail}",
        entity_kind="session",
        entity_name=session_name,
        hint="Check the session template's command and flags.",
    )


def _raise_workload_unverified(session_name: str, *, socket_path: str) -> NoReturn:
    """Raise for a liveness probe that could not run at all.

    Deliberately kills nothing and captures nothing: an unreachable server
    is not a dead workload (a transport blip against a healthy session
    lands here), and killing on suspicion would turn a transient failure
    into a real outage. The message must not blame the template.
    """
    raise StateError(
        "could not verify the session workload's state after launch "
        "(transport failure probing the new tmux session); the session may still be running",
        entity_kind="session",
        entity_name=session_name,
        hint=(
            f"Inspect (attach) or kill the session's tmux server at {socket_path}; "
            "note remain-on-exit may still be set on it."
        ),
    )


def _ensure_workload_alive(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str,
) -> None:
    """Liveness gate: return only if the pane is verifiably alive.

    Dead panes raise the capture-and-kill error; an unverifiable probe
    raises the honest could-not-verify error without touching the server.
    """
    liveness, exit_status = _probe_pane_liveness(session_name, run_command=run_command, socket_path=socket_path)
    if liveness == "dead":
        _raise_workload_died(session_name, run_command=run_command, socket_path=socket_path, exit_status=exit_status)
    if liveness == "unverified":
        _raise_workload_unverified(session_name, socket_path=socket_path)


def create_session(
    session_name: str,
    workspace_path: str,
    command: str,
    linux_user: str | None,
    *,
    run_command: RunCommand,
    target: Transport | None = None,
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
    For agent mode, ``run_command`` is the AGENT's SSH connection,
    and ``target`` must be admin's ``Transport`` (for AGENT_SOCKET_ROOT setup).

    ``env`` flows to the pane via two channels (belt and suspenders):
    1. ``run_command`` materializes ``-o SetEnv=K=V`` args on the SSH
       command line so sshd injects the vars into the user's shell that
       runs ``tmux new-session``; tmux inherits.
    2. ``-e KEY=VAL`` on ``tmux new-session`` seeds the session-environment
       table so per-session env stays scoped to this session.

    A workload that exits immediately (e.g. its CLI rejects a flag value in
    milliseconds) would take the fresh per-session tmux server down with it,
    losing its error output and leaving the grant machinery to fail opaquely
    against a dead socket. The session is therefore created with
    ``remain-on-exit`` chained into the same tmux invocation, liveness-checked
    before any socket grant work (and re-checked after the healthy path
    unsets the option), and a dead pane's captured output and exit status are
    raised as a typed ``StateError``. A transport failure while probing
    raises a distinct could-not-verify ``StateError`` without killing
    anything. Deliberate semantic change (2026-08-03): a workload that
    legitimately exits (status 0 included) within the create window now fails
    the create with its captured output, where it previously soft-succeeded
    into a stopped session.

    Returns (socket_path, tmux_server_pid).
    """
    assert linux_user is not None
    assert admin_username is not None, "admin_username required for create_session"
    assert target is not None, "target (admin's Transport) required for create_session"

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
    socket_presence = _presence_from_result(sock_exists)
    if socket_presence is ProbeStatus.UNKNOWN:
        raise StateError(
            f"could not determine whether managed tmux socket {sock} exists",
            entity_kind="session",
            entity_name=session_name,
        )
    if socket_presence is ProbeStatus.PRESENT:
        server_presence = probe_tmux_server(run_command=run_command, socket_path=sock)
        if server_presence is ProbeStatus.PRESENT:
            raise RuntimeError(
                f"Socket {sock} already has an active tmux server. Kill it first or choose a different session name."
            )
        if server_presence is ProbeStatus.UNKNOWN:
            raise StateError(
                f"could not determine whether managed tmux socket {sock} has an active server",
                entity_kind="session",
                entity_name=session_name,
                hint="Retry after transport access is reliable; the socket was left unchanged.",
            )
        from agentworks import output as _output

        _output.detail(f"Removing stale socket: {sock}")
        run_command(f"rm -f {q_sock}", check=False)

    # Create the session. SetEnv vars travel with run_command; tmux's -e
    # flags add them to the session-environment table. remain-on-exit is
    # chained into the SAME tmux client invocation so it lands microseconds
    # after the pane spawns: a workload that dies instantly (e.g. a CLI
    # rejecting an invalid flag value in milliseconds) then leaves a dead
    # pane holding the server and its scrollback alive, instead of taking
    # both down before anything can read the error. The healthy path
    # unsets it again below. Accepted residual window: a death in the
    # microseconds BEFORE the chained set-option lands fails this whole
    # check=True invocation as a raw transport error, which is correct: a
    # genuine new-session failure must not be misread as workload death.
    cmd = f"tmux -S {q_sock} new-session -d -s {q_session} -c {q_path} -f {RESTRICTED_CONFIG_PATH}{env_flags}"
    if pane_cmd:
        cmd += f" {shlex.quote(pane_cmd)}"
    cmd += f" \\; set-option -t {q_session} remain-on-exit on"
    run_command(cmd, env=env)

    # Liveness check BEFORE the socket grant machinery: a dead workload
    # must surface as its own captured error, not as the grant loop's
    # opaque SSH failure against a dead socket.
    _ensure_workload_alive(session_name, run_command=run_command, socket_path=sock)

    # Socket permissions + cross-user access only matter for agent sessions
    # (admin has direct access to its own per-session socket).
    if not is_admin:
        run_command(f"chmod g+rwx {q_sock}")
        _grant_server_access(run_command, sock)

    # PID fetch while remain-on-exit still holds the server up: a death in
    # this window cannot take the server down, so a failed fetch here is a
    # transport hiccup (best-effort; auto-repair recovers on next access),
    # not the original lost-output bug reborn.
    try:
        pid_out = run_command(tmux_cmd("display-message -p '#{pid}'", sock), check=False)
        pid: int | None = _parse_pid(getattr(pid_out, "stdout", ""), context="after session create")
    except (RuntimeError, ValueError):
        pid = None

    # Healthy path: restore normal pane-exit semantics (pane exit ends
    # session ends server), then re-check once as the LAST act before
    # return. A workload that died since the first check left a
    # dead-but-persisted pane; the re-check converts that into the same
    # capture-and-raise instead of a zombie session. check=False on the
    # unset: if the server itself vanished, the re-check is what reports it.
    run_command(tmux_cmd(f"set-option -u -t {q_session} remain-on-exit", sock), check=False)
    _ensure_workload_alive(session_name, run_command=run_command, socket_path=sock)
    return (sock, pid)


def kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> bool:
    """Kill a session's tmux session. Returns True if the session existed."""
    q_session = shlex.quote(f"={session_name}")
    result = run_command(
        tmux_cmd(f"kill-session -t {q_session}", socket_path),
        check=False,
    )
    return getattr(result, "ok", True)


def kill_server(*, run_command: RunCommand, socket_path: str) -> bool:
    """Destroy the complete dedicated tmux server behind ``socket_path``."""
    result = run_command(tmux_cmd("kill-server", socket_path), check=False)
    return getattr(result, "ok", True)


def kill_server_and_probe(*, run_command: RunCommand, socket_path: str) -> ProbeStatus:
    """Request dedicated-server teardown and return its verified presence."""
    kill_server(run_command=run_command, socket_path=socket_path)
    return probe_tmux_server(run_command=run_command, socket_path=socket_path)


def probe_tmux_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> ProbeStatus:
    """Probe an exact tmux session without conflating failure with absence."""
    q_session = shlex.quote(f"={session_name}")
    result = run_command(
        tmux_cmd(f"has-session -t {q_session}", socket_path) + " 2>/dev/null",
        check=False,
    )
    return _presence_from_result(result)


def probe_tmux_server(*, run_command: RunCommand, socket_path: str) -> ProbeStatus:
    """Probe a dedicated tmux server without conflating failure with absence."""
    result = run_command(tmux_cmd("list-sessions", socket_path) + " 2>/dev/null", check=False)
    return _presence_from_result(result)


def capture_output(
    session_name: str,
    *,
    run_command: RunCommand,
    lines: int = DEFAULT_HISTORY_LIMIT,
    socket_path: str | None = None,
    join_wrapped: bool = False,
) -> str:
    """Capture the scrollback buffer from a session.

    ``join_wrapped`` adds tmux's ``-J`` (join wrapped lines) so a message
    longer than the pane width survives later per-line trimming as one
    line; off by default to keep other callers' output shape unchanged.
    """
    q_session = shlex.quote(f"={session_name}")
    flags = "-p -J" if join_wrapped else "-p"
    result = run_command(
        tmux_cmd(f"capture-pane -t {q_session} {flags} -S -{lines}", socket_path),
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


def probe_tmux_server_pid(
    *,
    target: Transport,
    socket_path: str | None = None,
    sudo: bool = False,
) -> IntegerProbe:
    """Probe the positive PID of a running tmux server."""
    cmd = tmux_cmd("display-message -p '#{pid}'", socket_path) + " 2>/dev/null"
    result = target.run(cmd, sudo=True, check=False) if sudo else target.run(cmd, check=False)
    presence = _presence_from_result(result)
    if presence is not ProbeStatus.PRESENT:
        return IntegerProbe(presence)
    pid_str = result.stdout.strip()
    if not pid_str:
        return IntegerProbe(ProbeStatus.UNKNOWN)
    try:
        pid = int(pid_str)
    except ValueError:
        return IntegerProbe(ProbeStatus.UNKNOWN)
    if pid <= 0:
        return IntegerProbe(ProbeStatus.UNKNOWN)
    return IntegerProbe(ProbeStatus.PRESENT, pid)


def parse_process_start_ticks(stat_line: str) -> int:
    """Parse Linux ``/proc/PID/stat`` field 22 without splitting ``comm``."""
    close = stat_line.rfind(")")
    if close < 0:
        raise ValueError("process stat has no closing command delimiter")
    fields = stat_line[close + 1 :].split()
    if len(fields) < 20:
        raise ValueError("process stat is missing field 22")
    try:
        ticks = int(fields[19])
    except ValueError:
        raise ValueError("process stat field 22 is not an integer") from None
    if ticks <= 0:
        raise ValueError("process stat field 22 is not positive")
    return ticks


def probe_process_start_ticks(pid: int, *, target: Transport) -> IntegerProbe:
    """Probe one process start time without conflating failure with absence."""
    result = target.run(f"cat /proc/{pid}/stat", check=False)
    presence = _presence_from_result(result)
    if presence is not ProbeStatus.PRESENT:
        return IntegerProbe(presence)
    try:
        return IntegerProbe(ProbeStatus.PRESENT, parse_process_start_ticks(result.stdout.strip()))
    except ValueError:
        return IntegerProbe(ProbeStatus.UNKNOWN)


def capture_tmux_server_fingerprint(
    *,
    target: Transport,
    socket_path: str,
    sudo: bool = False,
) -> FingerprintProbe:
    """Capture a stable tmux PID, boot ID, and process start time."""
    first_pid = probe_tmux_server_pid(target=target, socket_path=socket_path, sudo=sudo)
    if first_pid.status is not ProbeStatus.PRESENT:
        return FingerprintProbe(first_pid.status)
    assert first_pid.value is not None
    first_ticks = probe_process_start_ticks(first_pid.value, target=target)
    boot = target.run("cat /proc/sys/kernel/random/boot_id", check=False)
    boot_id = boot.stdout.strip() if _presence_from_result(boot) is ProbeStatus.PRESENT else ""
    second_pid = probe_tmux_server_pid(target=target, socket_path=socket_path, sudo=sudo)
    second_ticks = probe_process_start_ticks(first_pid.value, target=target)
    if second_pid.status is ProbeStatus.ABSENT:
        return FingerprintProbe(ProbeStatus.ABSENT)
    if (
        first_ticks.status is not ProbeStatus.PRESENT
        or not boot_id
        or second_pid.status is not ProbeStatus.PRESENT
        or second_ticks.status is not ProbeStatus.PRESENT
        or second_pid.value != first_pid.value
        or second_ticks.value != first_ticks.value
    ):
        return FingerprintProbe(ProbeStatus.UNKNOWN)
    assert first_ticks.value is not None
    return FingerprintProbe(
        ProbeStatus.PRESENT,
        TmuxServerFingerprint(first_pid.value, boot_id, first_ticks.value),
    )
