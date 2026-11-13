"""The guest program for detached work: launch, observe, interrupt and discard one launch.

``detached.Detached`` sends this file's source as an elevated ``PythonProgram``
whose arguments are the operation and the launch id, 32 lowercase hex digits.
``launch`` reads its spec, one JSON object of ``argv`` and ``env``, from
stdin, so the environment reaches the payload and no file or argument. The
exit status is the answer (the ``EXIT_*`` constants); ``observe`` and
``interrupt`` write one JSON line to stdout, then the output tail's raw bytes.

Every launch lives in ``BASE/<id>/``: ``process`` (the payload's pid, locked
while the supervisor or the payload's process tree lives), ``output`` (both of
the payload's streams), ``status`` (its exit status, written by rename) and
``work/`` (its working directory). The lock is the process identity, so no pid
is ever compared. A held lock is ``running``; a free one with ``status`` is
``exited``, without it ``lost``; a directory with no ``process`` is
``starting``. The lock is tested before ``status`` is read: the supervisor
writes ``status`` before it exits and releases its hold, so that order never
reads a finished launch as ``lost``.

Plain syntax and the standard library only, as for agw-launch: the guest's
python3 may be any release a supported Debian ships. It has no import-time
effect, and the modules that exist only on POSIX are imported where they are
used, so the workstation can import it anywhere for its constants.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE = "/var/lib/agentworks/detached"
"""Where every launch lives: root-owned, mode 0700, under a root-owned 0755 parent."""

OUTPUT_TAIL_BYTES = 65_536
"""How much of the end of ``output`` an observation of a launch that is not running carries."""

INTERRUPT_GRACE_SECONDS = 10.0
"""How long ``interrupt`` waits after ``SIGTERM`` before it sends ``SIGKILL``."""

INTERRUPT_KILL_WAIT_SECONDS = 5.0
"""How long ``interrupt`` waits after ``SIGKILL``; with the grace, inside the request's 30-second deadline."""

PRUNE_AGE_SECONDS = 7 * 24 * 3600
"""How old a sibling launch directory that is not running must be before a new launch removes it."""

EXIT_INVALID = 2
"""A malformed launch id or spec."""
EXIT_EXISTS = 3
"""``launch`` of an id whose directory already exists; nothing was changed."""
EXIT_RUNNING = 4
"""``discard`` of a running launch; nothing was removed."""

ABSENT, STARTING, RUNNING, EXITED, LOST = "absent", "starting", "running", "exited", "lost"

_ID = re.compile(r"[0-9a-f]{32}")
_POLL_SECONDS = 0.05


def main(
    argv: list[str],
    *,
    base: str = BASE,
    interrupt_grace: float = INTERRUPT_GRACE_SECONDS,
    kill_wait: float = INTERRUPT_KILL_WAIT_SECONDS,
) -> int:
    """Run one operation, ``argv`` being its name and the launch id, and return the exit status."""
    import signal

    def end(number: int, frame: object) -> None:
        raise SystemExit(128 + number)

    signal.signal(signal.SIGHUP, end)
    signal.signal(signal.SIGTERM, end)
    operation, launch_id = argv
    if not _ID.fullmatch(launch_id):
        sys.stderr.write("the launch id is not 32 lowercase hex digits\n")
        return EXIT_INVALID
    directory = os.path.join(base, launch_id)
    if operation == "launch":
        return _launch(base, directory)
    if operation == "observe":
        _report(directory, _state(directory))
        return 0
    if operation == "interrupt":
        _report(directory, _interrupt(directory, interrupt_grace, kill_wait))
        return 0
    if operation == "discard":
        return _discard(directory)
    raise ValueError(f"unknown operation {operation!r}")


# -- State -------------------------------------------------------------------------------


def _lock_held(directory: str) -> bool | None:
    """Whether ``process`` is locked; ``None`` when there is no ``process`` yet."""
    import fcntl

    try:
        descriptor = os.open(os.path.join(directory, "process"), os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)  # closing releases the shared lock this test may have taken
    return False


def _read_status(directory: str) -> int | None:
    try:
        with open(os.path.join(directory, "status")) as recorded:
            return int(recorded.read())
    except FileNotFoundError:
        return None


def _state(directory: str) -> tuple[str, int | None]:
    """The launch's state, and its status when it has exited. The lock is tested before ``status`` is read."""
    if not os.path.isdir(directory):
        return ABSENT, None
    held = _lock_held(directory)
    if held is None:
        return STARTING, None
    if held:
        return RUNNING, None
    status = _read_status(directory)
    return (LOST, None) if status is None else (EXITED, status)


def _report(directory: str, observed: tuple[str, int | None]) -> None:
    """Write the observation's JSON line, then the output tail once the launch has ended."""
    state, status = observed
    work: dict[str, int] = {}
    if state not in (ABSENT, STARTING):
        try:
            with os.scandir(os.path.join(directory, "work")) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        work[entry.name] = entry.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            pass  # discarded since the state was read
    tail = _output_tail(directory) if state in (EXITED, LOST) else b""
    line = json.dumps({"state": state, "status": status, "work": work}, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.buffer.write(line.encode("ascii") + b"\n" + tail)
    sys.stdout.buffer.flush()


def _output_tail(directory: str) -> bytes:
    try:
        with open(os.path.join(directory, "output"), "rb") as output:
            output.seek(max(0, os.fstat(output.fileno()).st_size - OUTPUT_TAIL_BYTES))
            return output.read(OUTPUT_TAIL_BYTES)
    except FileNotFoundError:
        return b""


# -- Launch ------------------------------------------------------------------------------


def _read_spec() -> tuple[list[str], dict[str, str]] | None:
    """The spec from stdin, or ``None`` when it is not an argv of strings and an environment of strings."""
    try:
        spec = json.loads(sys.stdin.buffer.read())
        argv, env = spec["argv"], spec["env"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(argv, list) or not argv or not isinstance(env, dict):
        return None
    strings = [*argv, *env.values(), *env]
    if not all(isinstance(value, str) and "\0" not in value for value in strings):
        return None
    if any(not name or "=" in name for name in env):
        return None
    return argv, env


def _make_base(base: str) -> None:
    """Create the base and its parent with explicit modes; an existing one is root's own, so it is kept."""
    for path, mode in ((os.path.dirname(base), 0o755), (base, 0o700)):
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, mode)


def _prune(base: str, keep: str) -> None:
    """Remove every launch directory but ``keep`` that is not running and is older than the prune age."""
    now = time.time()
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if path == keep or not _ID.fullmatch(name):
            continue
        try:
            if not os.path.isdir(path) or os.path.islink(path) or now - os.stat(path).st_mtime < PRUNE_AGE_SECONDS:
                continue
            if _state(path)[0] != RUNNING:
                shutil.rmtree(path)
        except OSError:
            continue  # best effort: the next launch looks again


def _launch(base: str, directory: str) -> int:
    import fcntl

    spec = _read_spec()
    if spec is None:
        sys.stderr.write("the launch spec is not an argv and an environment of strings\n")
        return EXIT_INVALID
    _make_base(base)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        return EXIT_EXISTS
    os.mkdir(os.path.join(directory, "work"), 0o700)
    _prune(base, directory)
    # Locked before the payload exists and renamed to ``process`` only once it runs, so until
    # then the launch reads as starting, never as an unlocked ``process`` with no status.
    lock = os.open(os.path.join(directory, "process.new"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    report_read, report_write = os.pipe()
    if os.fork() == 0:
        status = 1
        try:
            os.close(report_read)
            _supervise(directory, spec, lock, report_write)
            status = 0
        finally:
            os._exit(status)  # never return into the launching process's code
    os.close(report_write)
    report = b""
    while chunk := os.read(report_read, 256):
        report += chunk
    if report == b"started":
        return 0
    code = report.decode("ascii", "replace")
    sys.stderr.write(code + "\n" if code else "the supervisor ended before the payload started\n")
    if code == "exec ENOENT":
        return 127
    return 126 if code.startswith("exec ") else 1


def _supervise(directory: str, spec: tuple[list[str], dict[str, str]], lock: int, report: int) -> None:
    """The supervisor: its own session, nothing of the connection held; starts the payload and records its status."""
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(null, descriptor)
    low = 3
    for kept in sorted((lock, report)):
        os.closerange(low, kept)
        low = kept + 1
    os.closerange(low, 1 << 20)
    argv, env = spec
    output = os.open(os.path.join(directory, "output"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        payload = subprocess.Popen(
            argv,
            cwd=os.path.join(directory, "work"),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            env={**os.environ, **env},
            start_new_session=True,
            restore_signals=True,
            pass_fds=(lock,),
        )
    except OSError as error:
        # The status first and then ``process``, so a recorded status always has a ``process``.
        _write_status(directory, 127 if error.errno == errno.ENOENT else 126)
        os.rename(os.path.join(directory, "process.new"), os.path.join(directory, "process"))
        os.write(report, f"exec {errno.errorcode.get(error.errno or 0, 'EIO')}".encode("ascii"))
        return
    os.close(output)
    os.write(lock, f"{payload.pid}\n".encode("ascii"))
    os.rename(os.path.join(directory, "process.new"), os.path.join(directory, "process"))
    os.write(report, b"started")
    os.close(report)
    returncode = payload.wait()
    _write_status(directory, 128 - returncode if returncode < 0 else returncode)


def _write_status(directory: str, status: int) -> None:
    partial = os.path.join(directory, "status.new")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        os.write(descriptor, f"{status}\n".encode("ascii"))
    finally:
        os.close(descriptor)
    os.rename(partial, os.path.join(directory, "status"))


# -- Interrupt and discard ---------------------------------------------------------------


def _interrupt(directory: str, grace: float, kill_wait: float) -> tuple[str, int | None]:
    """Stop a running payload's process group, ``SIGTERM`` and then ``SIGKILL``, and return the state after."""
    import signal

    observed = _state(directory)
    if observed[0] != RUNNING:
        return observed
    # Read only while the lock shows the payload or its supervisor alive.
    # An empty one is a payload that could not be executed, whose supervisor is about to exit.
    with open(os.path.join(directory, "process")) as process:
        recorded = process.read().strip()
    for number, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, kill_wait)):
        if recorded:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(recorded), number)
        deadline = time.monotonic() + wait
        while _lock_held(directory) and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
        if not _lock_held(directory):
            break
    return _state(directory)


def _discard(directory: str) -> int:
    state, _status = _state(directory)
    if state == RUNNING:
        return EXIT_RUNNING
    if state != ABSENT:
        shutil.rmtree(directory)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
