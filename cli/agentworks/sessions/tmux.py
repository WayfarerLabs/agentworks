"""tmux session management for agentworks sessions.

Each session gets a locked-down tmux session. Session names are globally
unique and used directly as the tmux session name. A restricted tmux config
disables all interactive session management (no splits, no new windows, no
prefix key) while keeping a large scrollback buffer.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol

from agentworks.errors import ConnectivityError, ExternalError, StateError
from agentworks.naming import LINUX_USERNAME_MAX_LENGTH

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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


def _test_presence_from_result(result: object) -> ProbeStatus:
    """Classify a shell ``test`` result, whose 0/1 contract is unambiguous."""
    returncode = getattr(result, "returncode", None)
    if returncode is None:
        return ProbeStatus.PRESENT if getattr(result, "ok", False) else ProbeStatus.ABSENT
    if returncode == 0:
        return ProbeStatus.PRESENT
    if returncode == 1:
        return ProbeStatus.ABSENT
    return ProbeStatus.UNKNOWN


_TMUX_SERVER_ABSENCE_DIAGNOSTICS = (
    re.compile(r"no server running on .+", re.ASCII),
    re.compile(r"error connecting to .+ \((?:No such file or directory|Connection refused)\)", re.ASCII),
)
_TMUX_TARGET_ABSENCE_DIAGNOSTIC = re.compile(r"can't find session: .+", re.ASCII)


def _normalized_probe_streams(result: object) -> tuple[str, str]:
    """Return probe stdout and stderr with surrounding whitespace removed."""
    stdout = (getattr(result, "stdout", "") or "").strip()
    stderr = (getattr(result, "stderr", "") or "").strip()
    return stdout, stderr


def runtime_probe_command(command: str, fact: str) -> str:
    """Put this observation after a distinct line boundary in startup output."""
    return f"printf '\\nAGW_RUNTIME_{fact}='; printf '\\nAGW_RUNTIME_STDERR=' >&2; {command}"


def runtime_probe_payload(stdout: str, fact: str) -> str | None:
    """Accept one observation frame; duplicate or absent frames are unknown."""
    prefix = f"AGW_RUNTIME_{fact}="
    lines = stdout.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(starts) != 1:
        return None
    return "".join(lines[starts[0] :])[len(prefix) :]


def runtime_probe_stderr(stderr: str) -> str | None:
    """Separate command diagnostics from preceding SSH startup stderr."""
    if "AGW_RUNTIME_STDERR=" not in stderr:
        return stderr.strip()
    payload = runtime_probe_payload(stderr, "STDERR")
    return payload.strip() if payload is not None else None


def _tmux_presence_from_result(
    result: object, *, missing_target_is_absent: bool, stdout: str | None = None
) -> ProbeStatus:
    """Classify one tmux presence probe from its exit and canonical diagnostic."""
    returncode = getattr(result, "returncode", None)
    if returncode is None:
        return ProbeStatus.PRESENT if getattr(result, "ok", False) else ProbeStatus.UNKNOWN
    original_stdout, stderr = _normalized_probe_streams(result)
    native_stderr = runtime_probe_stderr(stderr)
    if native_stderr is None:
        return ProbeStatus.UNKNOWN
    if returncode == 0:
        return ProbeStatus.PRESENT
    if returncode != 1:
        return ProbeStatus.UNKNOWN

    stderr = native_stderr
    stdout = original_stdout if stdout is None else stdout.strip()
    streams = [value for value in (stdout, stderr) if value]
    # A canonical tmux diagnostic is a single line on exactly one stream. The
    # direct probe transports fold CR to LF on capture (SSH via ``text=True``,
    # Lima/WSL2 via ``decode_stream``), but the batch presence path hex-encodes
    # the diagnostic in-guest and reconstitutes it with ``bytes.fromhex`` (see
    # ``sessions/manager/_status.py``), so those inner bytes never pass through
    # that folding and a raw CR can survive. Reject either newline form, since
    # regex ``.`` matches ``\r`` and would otherwise misclassify ``a\rb`` as a
    # diagnostic; also reject more than one non-empty stream.
    if len(streams) != 1 or "\n" in streams[0] or "\r" in streams[0]:
        return ProbeStatus.UNKNOWN
    diagnostic = streams[0]
    if any(pattern.fullmatch(diagnostic) for pattern in _TMUX_SERVER_ABSENCE_DIAGNOSTICS):
        return ProbeStatus.ABSENT
    if missing_target_is_absent and _TMUX_TARGET_ABSENCE_DIAGNOSTIC.fullmatch(diagnostic):
        return ProbeStatus.ABSENT
    return ProbeStatus.UNKNOWN


_BOOT_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.ASCII,
)


def canonical_boot_id(value: object) -> str | None:
    """Return one canonical Linux boot UUID or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _BOOT_ID_PATTERN.fullmatch(candidate) else None


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
    """Remove only observed direct sockets whose native server is confirmed absent."""
    from agentworks.native_files import native_path

    dir_path = native_path(dir_path)
    q_dir = shlex.quote(dir_path)
    begin, end = "\nAGW_STALE_SOCKETS_BEGIN\0", "AGW_STALE_SOCKETS_END\0"
    result = target.run(
        "printf '\\nAGW_STALE_SOCKETS_BEGIN\\0'; "
        f"find {q_dir} -maxdepth 1 -name '*.sock' -type s -print0 2>/dev/null "
        "&& printf 'AGW_STALE_SOCKETS_END\\0'",
        sudo=True,
        check=False,
    )
    if not result.ok or result.stdout.count(begin) != 1 or result.stdout.count(end) != 1:
        return 0
    _, _, payload = result.stdout.partition(begin)
    payload, _, trailing = payload.partition(end)
    if trailing or (payload and not payload.endswith("\0")):
        return 0
    paths = payload.split("\0")[:-1] if payload else []
    if len(paths) != len(set(paths)):
        return 0
    try:
        if any(
            native_path(path) != path or path.rpartition("/")[0] != dir_path or not path.endswith(".sock")
            for path in paths
        ):
            return 0
    except StateError:
        return 0

    removed = 0
    for sock_path in paths:
        q_sock = shlex.quote(sock_path)
        socket_guard = f"test -S {q_sock} && ! test -L {q_sock}"

        def run(
            command: str, *, check: bool = True, env: dict[str, str] | None = None, guard: str = socket_guard
        ) -> object:
            return target.run(f"{guard} && {command}", sudo=True, check=check, env=env or {})

        if probe_tmux_server(run_command=run, socket_path=sock_path) is ProbeStatus.ABSENT:
            result = target.run(f"{socket_guard} && rm -f -- {q_sock}", sudo=True, check=False)
            if result.ok:
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

# Preserve the launch pane until its caller records the runtime identity.
set -g remain-on-exit on

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

    Owner-targeted session commands do not need sudo because socket access goes
    through group permissions. Admin-targeted batch operations use
    ``sudo=True`` when inspecting agent-owned runtimes.
    """
    cmd = f"tmux -S {shlex.quote(socket_path)} {base}" if socket_path else f"tmux {base}"
    return f"sudo -n {cmd}" if sudo else cmd


def exact_tmux_target(name: str) -> str:
    """Return an always-quoted exact tmux target for every supported shell.

    Tmux's leading ``=`` requests exact matching, but zsh interprets an
    unquoted ``=name`` as command-path expansion. ``shlex.quote`` deliberately
    leaves ``=`` unquoted, so exact targets need this explicit shell boundary.
    """
    return "'" + ("=" + name).replace("'", "'\"'\"'") + "'"


def _launch_pane_command(command: str, session_name: str, socket_path: str | None) -> str:
    """Target the pane returned by new-session, independent of the active window."""
    if socket_path is None:
        return tmux_cmd(command.replace("AGW_LAUNCH_PANE", exact_tmux_target(session_name + ":")), None)
    observe = tmux_cmd("show-option -gqv @agentworks-launch-pane", socket_path)
    legacy = tmux_cmd(f"list-panes -s -t {exact_tmux_target(session_name)} -F '#{{pane_id}}'", socket_path)
    pane_session = tmux_cmd("display-message -p -t \"$agw_launch_pane\" '#{session_name}'", socket_path)
    return (
        f"agw_launch_pane=$({observe}) || exit $?; "
        f'if [ -z "$agw_launch_pane" ]; then agw_launch_pane=$({legacy}) || exit $?; fi; '
        'case "$agw_launch_pane" in %*) ;; *) exit 1;; esac; '
        'case "${agw_launch_pane#%}" in ""|*[!0-9]*) exit 1;; esac; '
        f'test "$({pane_session})" = {shlex.quote(session_name)} || exit 1; '
        + tmux_cmd(command.replace("AGW_LAUNCH_PANE", '"$agw_launch_pane"'), socket_path)
    )


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
    *,
    redactions: Iterable[str] = (),
) -> str:
    """Trim captured pane output for embedding in an error message.

    Drops blank lines (a fresh pane's scrollback is mostly trailing blanks),
    keeps only the last ``max_lines`` nonempty lines (where a dying
    workload's own error message lands), and caps each line to
    ``max_line_chars`` characters.
    """
    from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
    from agentworks.secrets.redaction import secret_redaction_forms

    values = tuple(redactions)
    for pattern in sorted({form for value in values for form in secret_redaction_forms(value)}, key=len, reverse=True):
        text = text.replace(pattern, "[REDACTED]")
    nonempty = [
        sanitize_setup_diagnostic(line, redactions=values)[:max_line_chars]
        for line in text.splitlines()
        if line.strip()
    ]
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
    - ``("alive", "")``: tmux explicitly reported a live pane.
    - ``("unverified", "")``: neither attempt produced a valid observation.
      This is NOT proof of death: a transient transport failure against a healthy
      workload lands here too, so callers must neither kill nor diagnose on
      this answer (the same unreachable-is-not-dead stance as
      ``stop_session``'s BrokenStateError path).

    Both facts ride one round trip: ``#{pane_dead}`` and
    ``#{pane_dead_status}`` in a single display-message.
    """
    cmd = (
        _launch_pane_command(
            "display-message -p -t AGW_LAUNCH_PANE '#{pane_dead} #{pane_dead_status}'", session_name, socket_path
        )
        + " 2>/dev/null"
    )
    for _attempt in range(2):  # retry once: a single blip must not condemn the create
        result = run_command(runtime_probe_command(cmd, "PANE"), check=False)
        if getattr(result, "ok", False):
            payload = runtime_probe_payload(getattr(result, "stdout", "") or "", "PANE")
            if payload is None:
                continue
            observed = re.fullmatch(r"([01])(?: ([0-9]+))?", payload.strip())
            if observed is None:
                continue
            flag, status = observed.groups()
            if flag == "1":
                return ("dead", status or "")
            return ("alive", "")
    return ("unverified", "")


def _raise_workload_died(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str,
    exit_status: str = "",
    redactions: Iterable[str] = (),
) -> NoReturn:
    """Capture a dead pane's output, kill its server, and raise a typed error.

    Capture attempts precede the kill because teardown discards scrollback.
    Expected diagnostic and cleanup I/O failures cannot replace the observed
    workload failure. A surviving server either self-removes its socket or
    surfaces as the active-server conflict when the name is reused.
    """
    captures = []
    with suppress(ConnectivityError, ExternalError, OSError, subprocess.SubprocessError):
        captures.append(
            capture_output(session_name, run_command=run_command, socket_path=socket_path, join_wrapped=True)
        )
    # Normal capture is the active screen; -a reads its saved counterpart.
    # Either may contain useful output even when the active pane has tmux's
    # generated dead-pane overlay, so neither capture substitutes for the other.
    with suppress(ConnectivityError, ExternalError, OSError, subprocess.SubprocessError):
        alt = run_command(
            _launch_pane_command("capture-pane -t AGW_LAUNCH_PANE -p -J -a", session_name, socket_path)
            + " 2>/dev/null",
            check=False,
        )
        if getattr(alt, "ok", False):
            captures.append(getattr(alt, "stdout", "") or "")
    captured = _trim_captured_output("\n".join(dict.fromkeys(captures)), redactions=redactions)
    with suppress(ConnectivityError, ExternalError, OSError, subprocess.SubprocessError):
        run_command(tmux_cmd("kill-server", socket_path) + " 2>/dev/null", check=False)
    status_note = f" (status {exit_status})" if exit_status else ""
    if captured:
        indented = "\n".join(f"  {line}" for line in captured.splitlines())
        detail = f"; captured terminal output:\n{indented}"
    else:
        detail = "; no output could be captured"
    raise StateError(
        f"session workload exited immediately after launch{status_note}{detail}",
        entity_kind="session",
        entity_name=session_name,
        hint="Check the session template's command and flags.",
    )


def _raise_workload_unverified(session_name: str, *, socket_path: str) -> NoReturn:
    """Raise when liveness probes produced no valid pane observation.

    Deliberately kills nothing and captures nothing: a failed or ambiguous
    probe is not a dead workload. Killing on suspicion could turn a temporary
    observation failure into a real outage. The message must not blame the template.
    """
    raise StateError(
        "could not verify the session workload's state after launch; the session may still be running",
        entity_kind="session",
        entity_name=session_name,
        hint=(
            f"The managed row and startup protection were retained for socket {socket_path}. "
            "Restore reliable runtime observation, then retry session stop or restart."
        ),
    )


def _ensure_workload_alive(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str,
    prior_error: Exception | None = None,
    redactions: Iterable[str] = (),
) -> None:
    """Liveness gate: return only if the pane is verifiably alive.

    Dead panes raise the capture-and-kill error. Without confirmed death,
    preserve any earlier sharing error; otherwise an unverifiable observation
    raises without touching the server. Cancellation and internal failures propagate.
    """
    try:
        liveness, exit_status = _probe_pane_liveness(session_name, run_command=run_command, socket_path=socket_path)
    except (ConnectivityError, ExternalError, OSError, subprocess.SubprocessError):
        if prior_error is not None:
            raise prior_error from None
        raise
    if liveness == "dead":
        _raise_workload_died(
            session_name,
            run_command=run_command,
            socket_path=socket_path,
            exit_status=exit_status,
            redactions=redactions,
        )
    if prior_error is not None:
        raise prior_error
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

    The new server holds a dead pane with ``remain-on-exit`` so its diagnostics
    survive the caller's stable-identity checkpoint. This returns the acknowledged
    socket and optional PID before any further remote work. The caller must
    persist its fingerprint and call :func:`finalize_session_start` before
    reporting success. An unacknowledged transport failure is not evidence that
    startup completed.

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
    socket_presence = _test_presence_from_result(sock_exists)
    if socket_presence is ProbeStatus.UNKNOWN:
        raise StateError(
            f"could not determine whether managed tmux socket {sock} exists",
            entity_kind="session",
            entity_name=session_name,
        )
    if socket_presence is ProbeStatus.PRESENT:
        socket_type = run_command(f"test -S {q_sock} && ! test -L {q_sock}", check=False)
        if _test_presence_from_result(socket_type) is not ProbeStatus.PRESENT:
            raise StateError(
                f"managed tmux socket path {sock} contains a non-socket or symlink",
                entity_kind="session",
                entity_name=session_name,
            )
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
        run_command(f"test -S {q_sock} && ! test -L {q_sock} && rm -f -- {q_sock}")

    # The generated config protects new panes before their workloads execute.
    # Keep the pane ID returned by new-session, since user config and the
    # workload itself can create or select other windows on this server.
    cmd = (
        f"tmux -f {shlex.quote(RESTRICTED_CONFIG_PATH)} -S {q_sock}"
        f" new-session -d -P -F '#{{pane_id}}' -s {q_session} -c {q_path}{env_flags}"
    )
    if pane_cmd:
        cmd += f" {shlex.quote(pane_cmd)}"
    cmd = (
        f"agw_launch_pane=$({cmd}) || exit $?; "
        'case "$agw_launch_pane" in %*) ;; *) exit 1;; esac; '
        'case "${agw_launch_pane#%}" in ""|*[!0-9]*) exit 1;; esac; '
        + tmux_cmd('set-option -g @agentworks-launch-pane "$agw_launch_pane"', sock)
        + " \\; set-option -t \"$agw_launch_pane\" remain-on-exit on \\; display-message -p '#{pid}'"
    )
    result = run_command(cmd, env=env)
    try:
        pid = _parse_pid(getattr(result, "stdout", "") or "", context="after session create")
    except RuntimeError:
        pid = None
    # The acknowledgement is returned before any further remote work. The
    # caller now retains this runtime, records its identity, and only then
    # finalizes startup. remain-on-exit protects diagnostics throughout.
    return (sock, pid)


def finalize_session_start(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str,
    is_admin: bool,
    redactions: Iterable[str] = (),
) -> None:
    """Finish startup only after the caller persisted the server fingerprint."""
    grant_error: Exception | None = None
    if not is_admin:
        try:
            run_command(f"chmod g+rwx {shlex.quote(socket_path)}")
        except (ConnectivityError, ExternalError, OSError, subprocess.SubprocessError) as error:
            grant_error = error
    # A confirmed workload failure outranks auxiliary socket-sharing failure.
    redactions = tuple(redactions)
    _ensure_workload_alive(
        session_name, run_command=run_command, socket_path=socket_path, prior_error=grant_error, redactions=redactions
    )
    if not is_admin:
        try:
            _grant_server_access(run_command, socket_path)
        except (ConnectivityError, ExternalError, OSError, subprocess.SubprocessError) as error:
            grant_error = error
    _ensure_workload_alive(
        session_name, run_command=run_command, socket_path=socket_path, prior_error=grant_error, redactions=redactions
    )
    try:
        run_command(_launch_pane_command("set-option -t AGW_LAUNCH_PANE remain-on-exit off", session_name, socket_path))
    except (ConnectivityError, ExternalError, OSError, subprocess.SubprocessError) as error:
        raise ExternalError(
            f"could not finish startup for session '{session_name}'",
            entity_kind="session",
            entity_name=session_name,
            hint=f"Its managed runtime was retained. Run 'agw session start {session_name}' to retry completion.",
        ) from error
    _ensure_workload_alive(session_name, run_command=run_command, socket_path=socket_path, redactions=redactions)


def startup_protection_enabled(session_name: str, *, run_command: RunCommand, socket_path: str) -> bool | None:
    """Observe whether the launch pane still needs startup completion."""
    command = _launch_pane_command("show-option -wAqv -t AGW_LAUNCH_PANE remain-on-exit", session_name, socket_path)
    result = run_command(runtime_probe_command(command, "STARTUP"), check=False)
    payload = runtime_probe_payload(getattr(result, "stdout", "") or "", "STARTUP")
    if getattr(result, "returncode", None) != 0 or runtime_probe_stderr(getattr(result, "stderr", "") or "") != "":
        return None
    return {"on": True, "off": False}.get(payload.strip()) if payload is not None else None


def kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> bool:
    """Kill a session's tmux session. Returns True if the session existed."""
    q_session = exact_tmux_target(session_name)
    result = run_command(
        tmux_cmd(f"kill-session -t {q_session}", socket_path),
        check=False,
    )
    return getattr(result, "ok", True)


def kill_server(*, run_command: RunCommand, socket_path: str) -> bool:
    """Destroy the complete dedicated tmux server behind ``socket_path``."""
    result = run_command(tmux_cmd("kill-server", socket_path), check=False)
    return getattr(result, "ok", True)


def probe_tmux_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> ProbeStatus:
    """Probe an exact tmux session without conflating failure with absence."""
    q_session = exact_tmux_target(session_name)
    result = run_command(
        runtime_probe_command(tmux_cmd(f"has-session -t {q_session}", socket_path), "SESSION"),
        check=False,
    )
    payload = runtime_probe_payload(getattr(result, "stdout", "") or "", "SESSION")
    if payload is None or (getattr(result, "ok", False) and payload.strip()):
        return ProbeStatus.UNKNOWN
    return _tmux_presence_from_result(result, missing_target_is_absent=True, stdout=payload)


def probe_tmux_server(*, run_command: RunCommand, socket_path: str) -> ProbeStatus:
    """Probe a dedicated tmux server without conflating failure with absence."""
    result = run_command(
        runtime_probe_command(tmux_cmd("list-sessions", socket_path) + " >/dev/null", "SERVER"), check=False
    )
    payload = runtime_probe_payload(getattr(result, "stdout", "") or "", "SERVER")
    if payload is None or (getattr(result, "ok", False) and payload.strip()):
        return ProbeStatus.UNKNOWN
    return _tmux_presence_from_result(result, missing_target_is_absent=False, stdout=payload)


def _stable_absence_probe(probe: Callable[[], ProbeStatus]) -> ProbeStatus:
    """Re-probe one indeterminate post-teardown transition without waiting."""
    status = probe()
    return probe() if status is ProbeStatus.UNKNOWN else status


def probe_tmux_session_after_teardown(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None = None,
) -> ProbeStatus:
    """Probe a removed session twice only across an indeterminate transition."""
    return _stable_absence_probe(
        lambda: probe_tmux_session(session_name, run_command=run_command, socket_path=socket_path)
    )


def probe_tmux_server_after_teardown(*, run_command: RunCommand, socket_path: str) -> ProbeStatus:
    """Probe a stopped server twice only across an indeterminate transition."""
    return _stable_absence_probe(lambda: probe_tmux_server(run_command=run_command, socket_path=socket_path))


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
    if lines < 1:
        from agentworks.errors import ValidationError

        raise ValidationError("session log line count must be positive")
    flags = "-p -J" if join_wrapped else "-p"
    result = run_command(
        _launch_pane_command(f"capture-pane -t AGW_LAUNCH_PANE {flags} -S -{lines}", session_name, socket_path),
        check=False,
    )
    if getattr(result, "returncode", None) != 0:
        raise ExternalError(
            f"Could not capture output for session '{session_name}'.",
            hint="Check that the session's tmux server and pane are reachable, then retry session logs.",
        )
    captured = (getattr(result, "stdout", "") or "").splitlines()
    while captured and not captured[-1].strip():
        captured.pop()
    return "\n".join(captured[-lines:]) + ("\n" if captured else "")


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
    cmd = runtime_probe_command(tmux_cmd("display-message -p '#{pid}'", socket_path), "PID")
    result = target.run(cmd, sudo=True, check=False) if sudo else target.run(cmd, check=False)
    payload = runtime_probe_payload(result.stdout, "PID")
    if payload is None:
        return IntegerProbe(ProbeStatus.UNKNOWN)
    presence = _tmux_presence_from_result(result, missing_target_is_absent=False, stdout=payload)
    if presence is not ProbeStatus.PRESENT:
        return IntegerProbe(presence)
    pid_str = payload.strip()
    if not re.fullmatch(r"[0-9]+", pid_str, re.ASCII) or runtime_probe_stderr(result.stderr) != "":
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


def probe_process_start_ticks(
    pid: int,
    *,
    target: Transport,
    sudo: bool = False,
) -> IntegerProbe:
    """Probe one process start time without conflating failure with absence."""
    result = target.run(runtime_probe_command(f"cat /proc/{pid}/stat", "TICKS"), sudo=sudo, check=False)
    if getattr(result, "returncode", 0 if getattr(result, "ok", False) else 1) != 0:
        return IntegerProbe(ProbeStatus.UNKNOWN)
    payload = runtime_probe_payload(result.stdout, "TICKS")
    if payload is None:
        return IntegerProbe(ProbeStatus.UNKNOWN)
    payload = payload.strip()
    if "\n" in payload or "\r" in payload or runtime_probe_stderr(result.stderr) != "":
        return IntegerProbe(ProbeStatus.UNKNOWN)
    try:
        return IntegerProbe(ProbeStatus.PRESENT, parse_process_start_ticks(payload))
    except ValueError:
        return IntegerProbe(ProbeStatus.UNKNOWN)


def read_boot_id(target: Transport) -> str | None:
    """Read a canonical boot UUID from this command's own observation frame."""
    result = target.run(runtime_probe_command("cat /proc/sys/kernel/random/boot_id", "BOOT"), check=False)
    if getattr(result, "returncode", 0 if getattr(result, "ok", False) else 1) != 0:
        return None
    if runtime_probe_stderr(result.stderr) != "":
        return None
    return canonical_boot_id(runtime_probe_payload(getattr(result, "stdout", "") or "", "BOOT"))


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
    first_ticks = probe_process_start_ticks(first_pid.value, target=target, sudo=sudo)
    boot_id = read_boot_id(target)
    second_pid = probe_tmux_server_pid(target=target, socket_path=socket_path, sudo=sudo)
    second_ticks = probe_process_start_ticks(first_pid.value, target=target, sudo=sudo)
    if second_pid.status is ProbeStatus.ABSENT:
        return FingerprintProbe(ProbeStatus.ABSENT)
    if (
        first_ticks.status is not ProbeStatus.PRESENT
        or boot_id is None
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
