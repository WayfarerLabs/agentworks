"""Core's scripts run as root through a platform's own channel.

Every platform implements one primitive, ``run_as_root``: run
``/bin/bash -s`` as root through its channel with the script on stdin, and
return both streams as a :class:`RawRun`. Every helper that takes it names
the parameter ``run_as_root``. No channel's exit status is trusted, so
:func:`run` frames the payload with the execution markers and reads the
payload's status from them alone. The one secret a script may need travels
inside the script text, so it is in no argument, on the workstation or the
guest.

:func:`run_process` is the channel for a platform whose root channel is a
local process (``limactl``, ``wsl.exe``, ``ssh``), and :func:`read_host_keys`
is the fixed script that reads a VM's public host keys.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError, StateError
from agentworks.execution import envelope, pump, ssh_process, trust
from agentworks.execution.requests import Script, Shell
from agentworks.execution.results import Completed, Result

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.execution.requests import RunRequest

CHANNEL_TAIL_LINES = 20
REDACTED = "[REDACTED]"
HOST_KEYS_DEADLINE_SECONDS = 30
_HOST_KEYS_PAYLOAD = "cat /etc/ssh/ssh_host_*_key.pub"


@dataclass(frozen=True)
class RawRun:
    """Everything a root channel printed on each stream, uninterpreted. There is no status: none is trusted."""

    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


@dataclass(frozen=True)
class RootScriptResult:
    """What a root script reported, with its secret redacted from every field.

    ``status`` is the payload's exit status, or ``None`` when the markers
    cannot establish it. ``stdout`` and ``stderr`` are the payload's bytes
    between the markers. ``channel_tail`` is the last lines the channel
    printed before the payload started (its own errors), safe to show.
    """

    status: int | None
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    channel_tail: str


def run(
    run_as_root: Callable[[bytes, float], RawRun],
    payload: str,
    *,
    deadline: float,
    secret: str | None = None,
    log: Callable[[RunRequest, Result], None] | None = None,
) -> RootScriptResult:
    """Run ``payload`` as root once and read its status from the markers.

    ``secret`` (today only a Tailscale auth key) is assigned to the
    unexported shell variable ``AGW_SECRET``, which the payload sees and its
    children's environment does not. ``log`` records the request, as a Bash
    ``Script`` whose source is never written, and the redacted result. A raise
    from ``run_as_root`` propagates and is never followed by a second
    dispatch: the script may have run.
    """
    if secret is not None:
        from agentworks.secrets.line_safety import LineOrientedSecretUse, require_line_safe_secret

        secret = require_line_safe_secret(secret, use=LineOrientedSecretUse.TAILSCALE)
    nonce = envelope.new_nonce()
    raw = run_as_root(wrap(payload, nonce=nonce, secret=secret), deadline)
    redact = _redactor(secret)

    stdout_filter, stderr_filter = envelope.StreamFilter(nonce), envelope.StreamFilter(nonce)
    stdout = stdout_filter.feed(raw.stdout) + stdout_filter.finish()
    stderr = stderr_filter.feed(raw.stderr) + stderr_filter.finish()
    completion = envelope.classify(stdout_filter, stderr_filter, None)
    start = envelope.marker_prefix(nonce) + b"start\n"
    before = _before(raw.stderr, start)
    if not (stdout_filter.started or stderr_filter.started):
        # wsl.exe prints its own errors on stdout.
        before += b"\n" + raw.stdout
    channel_tail = ssh_process.tail_lines(
        redact(before.replace(b"\0", b"")).decode("utf-8", errors="replace"), CHANNEL_TAIL_LINES
    )
    result = RootScriptResult(
        status=completion.status if isinstance(completion, Completed) else None,
        stdout=redact(stdout),
        stderr=redact(stderr),
        channel_tail=channel_tail,
    )
    if log is not None:
        log(
            Script(payload, Shell.BASH, deadline=deadline),
            Result(completion, result.stdout, result.stderr, result.channel_tail),
        )
    return result


def wrap(payload: str, *, nonce: str, secret: str | None) -> bytes:
    """The script a channel runs: one brace group, so Bash parses all of it before running any.

    ``exec </dev/null`` gives a payload command that reads stdin end of file
    rather than script bytes, and a script cut short is a syntax error that
    runs nothing, not even the start marker. The markers are the execution
    package's own grammar, so :class:`envelope.StreamFilter` reads them.
    """
    marker = f"\\036{nonce}"
    lines = ["{", "exec </dev/null"]
    if secret is not None:
        lines.append(f"AGW_SECRET={shlex.quote(secret)}")
    lines += [
        f"printf '{marker} start\\n'; printf '{marker} start\\n' >&2",
        "(",
        payload.rstrip("\n"),
        ")",
        "s=$?",
        f"""printf '{marker} end\\n'; printf '{marker} exit %d\\n' "$s" >&2""",
        "}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _redactor(secret: str | None) -> Callable[[bytes], bytes]:
    forms = [] if not secret else sorted({secret, shlex.quote(secret)}, key=len, reverse=True)

    def redact(data: bytes) -> bytes:
        for form in forms:
            data = data.replace(form.encode("utf-8"), REDACTED.encode("ascii"))
        return data

    return redact


def _before(stream: bytes, start: bytes) -> bytes:
    found = stream.find(start)
    return stream if found < 0 else stream[:found]


def run_process(argv: Sequence[str], script: bytes, deadline: float) -> RawRun:
    """``run_as_root`` for a channel that is a local process: run ``argv`` with ``script`` on its stdin.

    The pump drives it with no markers, so it returns when the process exits
    and its pipes go quiet, at end of file, at ``deadline`` or when a stream
    passes the pump's 1 MiB limit; the last two kill the process. A
    descendant that keeps a pipe open never stalls it. A program that cannot
    start raises ``ConnectivityError`` naming it (``ClientUnsupported`` for
    the OpenSSH client).
    """
    stdout, stderr = pump.Capture(), pump.Capture()
    with pump.PipeSession(
        argv,
        request=script,
        nonce=None,
        on_stdout=stdout,
        on_stderr=stderr,
        deadline=pump.Deadline.after(deadline),
    ) as session:
        session.pump()
        session.end()
    return RawRun(bytes(stdout.data), bytes(stderr.data))


def read_host_keys(run_as_root: Callable[[bytes, float], RawRun]) -> tuple[trust.HostKey, ...]:
    """The VM's public host keys, read by a fixed script through its root channel.

    The output is guest text, so only lines :func:`trust.is_valid_key`
    accepts count. An unknown or failed outcome, or no valid key, raises.
    """
    result = run(run_as_root, _HOST_KEYS_PAYLOAD, deadline=HOST_KEYS_DEADLINE_SECONDS)
    if result.status is None:
        raise ConnectivityError(
            "the VM's host keys could not be read: the outcome of the read over its platform channel is unknown"
        )
    if result.status != 0:
        raise StateError(f"the VM's host keys could not be read: the read exited with status {result.status}")
    keys: list[trust.HostKey] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2 and trust.is_valid_key(fields[0], fields[1]):
            key = trust.HostKey(fields[0], fields[1])
            if key not in keys:
                keys.append(key)
    if not keys:
        raise StateError("the VM reported no valid public host key in /etc/ssh")
    return tuple(keys)
