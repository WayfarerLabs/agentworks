"""The conformance vectors, as one list that three lanes run.

Each vector says which lanes it runs on: ``L`` (a local shell), ``S`` (a local
sshd) and ``V`` (a live VM). A vector asserts only observable behavior: the
completion, the bytes, the status. None restates a message.
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.errors import ExecutionIncomplete, RequestRefused, TrustMismatch
from agentworks.execution.requests import Command, LoginShell, Script, Shell, shell_command_line
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Unknown,
    UnknownReason,
)
from tests.execution.conformance.lanes import Lane, LocalFilesystem, LocalLane, SSHDLane, sudo_granted, unavailable

if TYPE_CHECKING:
    from collections.abc import Callable

KERNEL_STRING_LIMIT = 131_071
REQUEST_DEADLINE = 60


def cmd(*argv: str, **options: object) -> Command:
    return Command(tuple(argv), **{"deadline": REQUEST_DEADLINE, **options})  # type: ignore[arg-type]


def script(source: str, shell: Shell = Shell.SH, **options: object) -> Script:
    return Script(source, shell, **{"deadline": REQUEST_DEADLINE, **options})  # type: ignore[arg-type]


def refused(code: str) -> NotDelivered:
    return NotDelivered(NotDeliveredReason.REFUSED, code)


def local(lane: Lane) -> LocalLane:
    if not isinstance(lane, LocalLane):
        pytest.skip("local lane only")
    return lane


def needs_startup_files(lane: LocalLane) -> None:
    """sh and dash read no startup file under ``-c``, so a vector about startup files has nothing to test there."""
    if lane.shell_name not in ("bash", "zsh"):
        pytest.skip(f"{lane.shell_name} reads no startup file for a non-interactive command")


def startup_file(lane: LocalLane) -> str:
    return ".zshenv" if lane.shell_name == "zsh" else ".bashrc"


# -- Shared by L, S and V ---------------------------------------------------------------


def literal_argv(lane: Lane) -> None:
    items = ["", " ", "a b", "$HOME", "`id`", "'", '"', "\\", "*", "-n", "--", "-c", "; echo injected", "-", "\t\n"]

    result = lane.run(cmd("python3", "-c", "import sys; sys.stdout.write(repr(sys.argv[1:]))", *items))

    assert result.completion == Completed(0)
    assert result.stdout.decode() == repr(items)


def script_versus_stdin(lane: Lane) -> None:
    source = "# SCRIPT_TOKEN_5d1c\ntr '\\0' ' ' < /proc/$$/cmdline; echo; cat"

    streamed = lane.stream(script(source), stdin=b"from the caller")

    assert streamed.status == 0
    interpreter_line, _, caller_input = streamed.stdout.partition(b"\n")
    assert b"/dev/fd/" in interpreter_line  # the interpreter was given a descriptor, not the text
    assert b"SCRIPT_TOKEN" not in interpreter_line
    assert caller_input == b"from the caller"  # and its stdin stayed the caller's


def interpreter_and_login(lane: Lane) -> None:
    probe = '[ "$(readlink -f /proc/$$/exe)" = "$(readlink -f %s)" ] && echo same'
    cases = [(Shell.SH, "/bin/sh"), (Shell.BASH, "/bin/bash"), (Shell.USER_DEFAULT, '"$SHELL"')]
    for shell, expected in cases:
        for login in (False, True):
            result = lane.run(script(probe % expected, shell, login=login))
            assert result.completion == Completed(0), (shell, login)
            assert result.stdout.endswith(b"same\n"), (shell, login, result.stdout)


def environment_bytes(lane: Lane) -> None:
    def text(data: bytes) -> str:
        return data.decode("utf-8", "surrogateescape")

    expected = {f"AGWV{n}": b"<" + bytes([n]) + b">" for n in range(1, 256)}
    expected |= {
        "AGWV_PERCENT": b"100% %s %d %%",
        "AGWV_QUOTES": b"'single' \"double\"",
        "AGWV_EXPANSION": b"${HOME} $(id) `id`",
        "AGWV_NEWLINES": b"a\r\nb\n",
    }
    program = (
        "import os,sys; "
        "sys.stdout.write(repr({k.decode(): v for k, v in os.environb.items() if k.startswith(b'AGWV')}))"
    )

    result = lane.run(cmd("python3", "-c", program, env={name: text(value) for name, value in expected.items()}))

    assert result.completion == Completed(0)
    assert ast.literal_eval(result.stdout.decode()) == expected


def size_limits(lane: Lane) -> None:
    limit = KERNEL_STRING_LIMIT
    measure = "import sys; sys.stdout.write(str(len(sys.argv[1])))"
    assert lane.run(cmd("python3", "-c", measure, "a" * limit)).stdout == str(limit).encode()
    with pytest.raises(ValidationError):
        lane.run(cmd("python3", "-c", measure, "a" * (limit + 1)))

    env_length = limit - len("AGWBIG=")
    program = "import os,sys; sys.stdout.write(str(len(os.environ['AGWBIG'])))"
    assert lane.run(cmd("python3", "-c", program, env={"AGWBIG": "b" * env_length})).stdout == str(env_length).encode()
    with pytest.raises(ValidationError):
        lane.run(cmd("python3", "-c", program, env={"AGWBIG": "b" * (env_length + 1)}))

    # Each argument is within the limit; the total is over ARG_MAX, which only exec can know.
    total = lane.run(cmd("true", *["c" * limit] * 64))
    assert total.completion == refused("exec E2BIG")


def working_directory(lane: Lane) -> None:
    assert lane.run(cmd("pwd", cwd=lane.workdir)).stdout == lane.workdir.encode() + b"\n"

    missing = f"{lane.workdir}/no-such-directory-agw"
    assert lane.run(cmd("true", cwd=missing)).completion == refused("cwd")
    streamed = lane.stream(cmd("true", cwd=missing))
    assert streamed.status == 125
    assert streamed.stdout == b""
    assert len(streamed.stderr.splitlines()) == 1


def missing_command(lane: Lane) -> None:
    assert lane.run(cmd("no-such-command-agw")).completion == refused("exec ENOENT")
    streamed = lane.stream(cmd("no-such-command-agw"))
    assert streamed.status == 127
    assert len(streamed.stderr.splitlines()) == 1

    not_executable = lane.make_script("#!/bin/sh\necho hi\n", mode=0o644)
    assert lane.run(cmd(not_executable)).completion == refused("exec EACCES")
    assert lane.stream(cmd(not_executable)).status == 126

    no_shebang = lane.make_script("echo hi\n")
    assert lane.run(cmd(no_shebang)).completion == refused("exec ENOEXEC")
    assert lane.stream(cmd(no_shebang)).status == 126


def exit_statuses(lane: Lane) -> None:
    for status in (0, 1, 255):
        assert lane.run(cmd("sh", "-c", f"exit {status}")).completion == Completed(status)
        assert lane.stream(cmd("sh", "-c", f"exit {status}")).status == status
    assert lane.run(cmd("sh", "-c", "kill -9 $$")).completion == Completed(137)
    assert lane.run(cmd("sh", "-c", "kill -TERM $$")).completion == Completed(143)


def stdin_positioning(lane: Lane) -> None:
    # A prefix that looks like a frame header: a launcher that read ahead would swallow or misplace it.
    data = b"0000000005hello" + bytes(range(256)) * 1024

    streamed = lane.stream(cmd("cat"), stdin=data)

    assert (streamed.status, streamed.stdout) == (0, data)


def stdin_consumer_exits_early(lane: Lane) -> None:
    streamed = lane.stream(cmd("head", "-c", "3"), stdin=b"0123456789" * 100_000)

    assert (streamed.status, streamed.stdout) == (0, b"012")


class LeavingSink(io.BytesIO):
    """An operator stream whose reader leaves after a few bytes, as with ``| head -c 2``."""

    def __init__(self, after: int) -> None:
        super().__init__()
        self._after = after

    def write(self, data: object, /) -> int:  # type: ignore[override]
        super().write(data)  # type: ignore[arg-type]
        if self.tell() >= self._after:
            raise BrokenPipeError
        return len(data)  # type: ignore[arg-type]


def wait_until_gone(pid_file: str) -> None:
    pid = int(Path(pid_file).read_text())
    deadline_at = time.monotonic() + 10
    while time.monotonic() < deadline_at:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("the guest's writer is still running")


def consumer_leaves_an_endless_writer(lane: Lane, *, writes_to: str) -> None:
    """The writer takes SIGPIPE when the client closes its end, and the request ends as ssh's did, as 141."""
    pid_file = f"{lane.make_dir()}/pid"
    redirect = ">&2" if writes_to == "stderr" else ""
    endless = cmd("sh", "-c", f"echo $$ > {pid_file}; exec yes {redirect}", deadline=15)
    leaving, other = LeavingSink(2), io.BytesIO()
    started = time.monotonic()

    status = lane.access.stream(
        endless,
        stdin=None,
        stdout=leaving if writes_to == "stdout" else other,
        stderr=leaving if writes_to == "stderr" else other,
    )

    assert time.monotonic() - started < 10
    assert status == 141
    assert leaving.getvalue()[:2] == b"y\n"
    wait_until_gone(pid_file)


def stdout_consumer_leaves_an_endless_writer(lane: Lane) -> None:
    consumer_leaves_an_endless_writer(lane, writes_to="stdout")


def stderr_consumer_leaves_an_endless_writer(lane: Lane) -> None:
    consumer_leaves_an_endless_writer(lane, writes_to="stderr")


def stdout_consumer_leaves_a_quiet_payload_that_then_ends(lane: Lane) -> None:
    marker = f"{lane.make_dir()}/finished"
    quiet = cmd("sh", "-c", f"echo hello; sleep 1; echo done > {marker}; exit 4", deadline=15)
    started = time.monotonic()

    status = lane.access.stream(quiet, stdin=None, stdout=LeavingSink(1), stderr=io.BytesIO())

    assert status == 4  # the guest's own status, reported once it ended
    assert time.monotonic() - started >= 1
    assert Path(marker).read_text() == "done\n"


def several_arguments_run_as_a_shell_command_line(lane: Lane) -> None:
    # vm exec shell-joins the arguments and sends the line under the account shell: builtins, a leading
    # assignment and keywords still work, and a word starting with = is never expanded (zsh would).
    builtin = script(shell_command_line(["command", "-v", "sh"]), Shell.USER_DEFAULT)
    assignment = script(shell_command_line(["FOO=bar", "printenv", "FOO"]), Shell.USER_DEFAULT)
    words = ["a b", "$HOME", "=ls", "-x", "it's", "*"]
    echo = script(shell_command_line(["printf", "%s|", *words]), Shell.USER_DEFAULT)

    found, assigned, printed = lane.run(builtin), lane.run(assignment), lane.run(echo)

    assert found.completion == Completed(0)
    assert found.stdout.strip() != b""
    assert (assigned.completion, assigned.stdout) == (Completed(0), b"bar\n")
    assert printed.completion == Completed(0)
    assert printed.stdout.decode() == "".join(f"{word}|" for word in words)


def bash_child_reads_no_startup_file(lane: Lane) -> None:
    home = lane.make_dir({".bashrc": "export AGW_STARTUP=read\n", ".bash_profile": "export AGW_STARTUP=read\n"})
    child = "echo ${AGW_STARTUP:-none}"
    with_pipe = script(f"bash -c '{child}'", env={"HOME": home})
    with_socket = cmd(
        "python3",
        "-c",
        "import socket, subprocess\n"
        "a, b = socket.socketpair()\n"
        f"print(subprocess.run(['bash', '-c', {child!r}], stdin=a, capture_output=True, text=True).stdout, end='')",
        env={"HOME": home},
    )

    assert lane.run(with_pipe).stdout == b"none\n"
    assert lane.run(with_socket).stdout == b"none\n"


def background_child_holds_both_streams(lane: Lane) -> None:
    started = time.monotonic()

    result = lane.run(script("sleep 5 &\necho done"))

    assert time.monotonic() - started < 4
    assert result.completion == Completed(0)
    assert result.stdout == b"done\n"


def output_without_a_trailing_newline_and_a_refusal(lane: Lane) -> None:
    result = lane.run(cmd("printf", "no newline"))
    assert (result.completion, result.stdout) == (Completed(0), b"no newline")

    result = lane.run(cmd("true", cwd=f"{lane.workdir}/no-such-directory-agw"))
    assert (result.completion, result.stdout, result.stderr) == (refused("cwd"), b"", b"")


def deadline(lane: Lane) -> None:
    spawned: list[subprocess.Popen[bytes]] = []

    class Recording(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[call-overload]
            spawned.append(self)  # type: ignore[arg-type]

    lane.monkeypatch.setattr(subprocess, "Popen", Recording)
    started = time.monotonic()

    result = lane.run(cmd("sleep", "3", deadline=0.5))

    assert time.monotonic() - started < 3
    assert result.completion == Unknown(UnknownReason.DEADLINE)
    assert spawned
    assert all(process.poll() is not None for process in spawned)


class SlowSink(io.BytesIO):
    """An operator stream that is slow to take output, so a pipe stays full while the guest finishes."""

    def write(self, data: object, /) -> int:  # type: ignore[override]
        time.sleep(0.2)
        return super().write(data)  # type: ignore[arg-type]


def payload_leaves_the_shared_streams_non_blocking_and_full(lane: Lane) -> None:
    # L is the regression guard: its pump is in this process and the slow sink keeps the pipes full.
    # S and V drain too fast through sshd and the network to catch a lost closing marker.
    # The payload shares descriptors 1 and 2 with the launcher, so what it does to them applies to the
    # launcher's closing markers. Both pipes are full when it exits, and the reader is slow.
    program = (
        "import os\n"
        "for fd in (1, 2):\n"
        "    os.set_blocking(fd, False)\n"
        "    for chunk in (b'x' * 4096, b'x'):\n"
        "        try:\n"
        "            while True:\n"
        "                os.write(fd, chunk)\n"
        "        except BlockingIOError:\n"
        "            pass\n"
    )
    out, err = SlowSink(), SlowSink()

    status = lane.access.stream(cmd("python3", "-c", program), stdin=None, stdout=out, stderr=err)

    assert status == 0
    assert out.getvalue() == b"x" * len(out.getvalue()) != b""
    assert err.getvalue() == b"x" * len(err.getvalue()) != b""


# -- Handover ---------------------------------------------------------------------------


def handover_hands_the_terminal_a_login_shell(lane: Lane) -> None:
    token = lane.handover_token()
    with lane.terminal(b"pwd\nexit 7\n") as terminal:
        code = lane.access.attach(LoginShell(cwd=lane.workdir))

    assert code == 7
    assert terminal.stdout.splitlines()[-1] == lane.workdir.encode()
    assert lane.wait_gone(f"/tmp/agw-handover-{token}")


def handover_refuses_before_the_terminal_is_touched(lane: Lane) -> None:
    lane.handover_token()
    with lane.terminal() as terminal, pytest.raises(RequestRefused) as raised:
        lane.access.attach(LoginShell(cwd=f"{lane.workdir}/no-such-directory-agw"))

    assert raised.value.code == "cwd"
    assert terminal.calls == 0


def handover_never_reuses_an_existing_name(lane: Lane) -> None:
    token = lane.handover_token()
    planted = f"/tmp/agw-handover-{token}"
    lane.mkdir(planted)
    try:
        with lane.terminal() as terminal, pytest.raises(RequestRefused) as raised:
            lane.access.attach(LoginShell())

        assert raised.value.code == "handover"
        assert terminal.calls == 0
        assert lane.exists(planted)  # the launcher removed nothing it did not create
    finally:
        lane.remove_dir(planted)


def handover_whose_second_connection_never_arrives_leaves_nothing(lane: Lane) -> None:
    token = lane.handover_token()
    with lane.terminal(behavior=lambda argv, half: 0) as terminal:
        assert lane.access.attach(LoginShell()) == 0

    assert terminal.calls == 1
    assert lane.wait_gone(f"/tmp/agw-handover-{token}")


def handover_gives_up_after_its_wait(lane: Lane) -> None:
    lane.shorten_handover_wait(1)
    token = lane.handover_token()

    def never_connects(argv: list[str], half: object) -> int:
        time.sleep(2.5)  # past the launcher's wait, with the parking connection still open
        return 0

    with lane.terminal(behavior=never_connects):
        started = time.monotonic()
        assert lane.access.attach(LoginShell()) == 0

    assert time.monotonic() - started < 15
    assert lane.wait_gone(f"/tmp/agw-handover-{token}")


def parking_that_never_reports_is_bounded_and_leaves_the_terminal_alone(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), "cat >/dev/null\n")  # holds the request and the connection open
    account.shorten_parking_wait(2)
    caught: list[BaseException] = []

    def attach() -> None:
        try:
            account.access.attach(LoginShell(cwd=account.workdir))
        except BaseException as error:  # noqa: BLE001 - the thread hands every outcome to the vector
            caught.append(error)

    # A regression in the bound would hang the attach forever, so the vector bounds itself.
    with account.terminal() as terminal:
        thread = threading.Thread(target=attach, daemon=True)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "the parking wait did not end"

    assert len(caught) == 1
    assert isinstance(caught[0], ExecutionIncomplete)
    assert caught[0].result.completion == Unknown(UnknownReason.DEADLINE)
    assert terminal.calls == 0


def memfd_refusing_the_noexec_seal_still_runs_the_script(lane: Lane) -> None:
    # Kernels before 6.3 reject MFD_NOEXEC_SEAL with EINVAL, and so does any kernel for a flag it does not know.
    account = local(lane)
    account.rewrite_launcher(b"os.MFD_CLOEXEC | 0x8)", b"os.MFD_CLOEXEC | 0x40000000)")

    result = account.run(script("echo ran"))

    assert (result.completion, result.stdout) == (Completed(0), b"ran\n")


def memfd_that_cannot_be_created_is_not_delivered(lane: Lane) -> None:
    # No descriptor is free, so memfd_create fails with EMFILE, which is not the seal's EINVAL.
    account = local(lane)
    account.rewrite_launcher(
        b"    script_fd = -1\n",
        b"    script_fd = -1\n    import resource\n    resource.setrlimit(resource.RLIMIT_NOFILE, (3, 3))\n",
    )
    marker = f"{account.make_dir()}/ran"

    result = account.run(script(f"touch {marker}"))

    assert result.completion == NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)
    assert not account.exists(marker)


def wrong_identity_is_an_authentication_failure(lane: Lane) -> None:
    from tests.execution.conformance.lanes import generate_key

    if isinstance(lane, LocalLane):
        pytest.skip("the local lane has no sshd to refuse a key")
    with tempfile.TemporaryDirectory(prefix="agw-conformance-key-") as directory:
        stranger = lane.access_with_identity(generate_key(Path(directory) / "unauthorized"))

        result = stranger.run(cmd("true"))

    assert result.completion == Unknown(UnknownReason.AUTH_FAILED)  # A1


# -- S only -----------------------------------------------------------------------------


def host_key_mismatch_is_trust_mismatch(lane: Lane) -> None:
    from agentworks.execution import trust

    if not isinstance(lane, SSHDLane):
        pytest.skip("S lane only")
    other = trust.HostKey("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIOU3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv0")
    trust.write_records(lane.vm_name, [other])

    result = lane.run(cmd("true"))

    assert result.completion == Unknown(UnknownReason.HOST_KEY)
    with pytest.raises(TrustMismatch):
        lane.stream(cmd("true"), stdin=None)


# -- L only: the account's startup files, and what the launcher resets ------------------

NOISE = (
    "echo startup noise\n"
    "echo startup noise on stderr >&2\n"
    "printf 'noise with no newline'\n"
    "printf 'stderr with no newline' >&2\n"
    "export STARTUP_EXPORT=leaked\n"
)


def stage0_survives_a_noisy_startup_file(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), NOISE)

    result = account.run(cmd("sh", "-c", 'echo "payload ${STARTUP_EXPORT-clean}"; echo e >&2'))

    assert result.completion == Completed(0)
    assert (result.stdout, result.stderr) == (b"payload clean\n", b"e\n")


def output_before_the_start_marker_is_dropped(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    for noise in ("echo noise", "printf noise", "printf noise; printf noise >&2"):
        account.startup(startup_file(account), noise + "\n")

        result = account.run(cmd("printf", "payload"))

        assert (result.completion, result.stdout, result.stderr) == (Completed(0), b"payload", b""), noise


def python_missing_is_not_delivered(lane: Lane) -> None:
    account = local(lane)
    account.python = "/nonexistent/python3"

    result = account.run(cmd("true"))

    assert result.completion == NotDelivered(NotDeliveredReason.PYTHON_MISSING)


def incomplete_standard_library_is_not_delivered(lane: Lane) -> None:
    account = local(lane)
    wrapper = account.make_script(
        "#!/bin/sh\n"
        "# Stands in for a python3 whose standard library lacks json.\n"
        'program="$4"; shift 4\n'
        f"exec {account.python} -I -S -c "
        '\'import sys; sys.modules["json"] = None; program = sys.argv[1]; sys.argv = ["-c"] + sys.argv[2:]; '
        'exec(program)\' "$program" "$@"\n'
    )
    account.python = wrapper

    result = account.run(cmd("true"))

    assert result.completion == NotDelivered(NotDeliveredReason.STDLIB_MISSING)


def startup_that_exits_decides_by_status(lane: Lane, startup: str, expected: object) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), startup + "\n")

    assert account.run(cmd("true")).completion == expected


def killed_launcher_is_unknown(lane: Lane) -> None:
    result = lane.run(script("kill -9 $PPID"))

    assert result.completion == Unknown(UnknownReason.TRUNCATED)


def startup_that_reads_stdin_leaves_the_request_not_delivered(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    secret = "sekrit-env-value-77"
    # The startup file consumes the request and echoes it where a naive client would show it.
    account.startup(startup_file(account), "cat 1>&2\n")

    result = account.run(cmd("true", env={"SECRET": secret}))

    assert result.completion == NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)
    assert secret.encode() not in result.stdout + result.stderr
    assert secret not in repr(result)


def startup_that_redirects_a_standard_stream_leaves_the_request_not_delivered(lane: Lane, redirect: str) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), redirect + "\n")

    result = account.run(cmd("echo", "payload"))

    assert result.completion == NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)
    assert b"payload" not in result.stdout + result.stderr


def signals_descriptors_and_session_variables_are_reset(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(
        startup_file(account),
        "trap '' HUP INT TERM USR1 USR2\nexec 9</etc/passwd\nexport TERM=startup-term XDG_RUNTIME_DIR=/startup-xdg\n",
    )
    # The shell reads its own signal state with builtins. A child (grep) reading the shell's
    # status can catch the shell between blocking signals for a fork and restoring them, which
    # shows a blocked mask under load that the payload never had.
    inspect = (
        'while read key value; do case $key in SigBlk:|SigIgn:) echo "$key $value";; esac; done < /proc/$$/status; '
        "ls /proc/$$/fd; "
        'echo "TERM=${TERM-unset}"; echo "XDG=${XDG_RUNTIME_DIR-unset}"'
    )

    result = account.run(cmd("sh", "-c", inspect))

    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    owned = os.path.isdir(runtime) and os.stat(runtime).st_uid == uid
    assert result.completion == Completed(0)
    assert result.stdout.decode().splitlines() == [
        "SigBlk: 0000000000000000",
        "SigIgn: 0000000000000000",
        "0",
        "1",
        "2",
        "TERM=unset",
        f"XDG={runtime if owned else 'unset'}",
    ]


def login_flag_runs_the_account_login_startup(lane: Lane) -> None:
    account = local(lane)
    home = account.make_dir({".profile": "export AGW_LOGIN=read\n", ".bash_profile": "export AGW_LOGIN=read\n"})
    probe = 'echo "${AGW_LOGIN:-unset}"'

    for shell in (Shell.SH, Shell.BASH):
        assert account.run(script(probe, shell, login=True, env={"HOME": home})).stdout == b"read\n", shell
        assert account.run(script(probe, shell, login=False, env={"HOME": home})).stdout == b"unset\n", shell


def handover_shell_starts_with_clean_dispositions(lane: Lane) -> None:
    account = local(lane)
    needs_startup_files(account)
    account.startup(startup_file(account), "trap '' HUP INT TERM USR1 USR2\n")
    home = account.make_dir()

    with account.terminal(b"grep -E 'SigIgn|SigBlk' /proc/self/status\nexit 0\n") as terminal:
        code = account.access.attach(LoginShell(cwd=account.workdir, env={"HOME": home}))

    assert code == 0
    assert terminal.stdout.decode().splitlines() == ["SigBlk:\t0000000000000000", "SigIgn:\t0000000000000000"]


# -- Elevation (E1, E2, E4, E6) ----------------------------------------------------------


def needs_sudo(lane: Lane) -> None:
    """A local guest elevates only where the runner grants passwordless sudo: CI does, so it fails there without it."""
    if isinstance(lane, LocalFilesystem) and not sudo_granted():
        unavailable("the elevation vectors need passwordless sudo")


def elevated_runs_as_root_in_the_rebuilt_environment(lane: Lane) -> None:
    needs_sudo(lane)
    if isinstance(lane, LocalLane) and lane.shell_name in ("bash", "zsh"):
        lane.startup(startup_file(lane), "export AGW_FROM_STARTUP=1\n")

    identity = lane.run(cmd("id", "-u", elevated=True))
    environment = lane.run(cmd("env", elevated=True))

    assert (identity.completion, identity.stdout) == (Completed(0), b"0\n")
    assert environment.completion == Completed(0)
    names = dict(line.split("=", 1) for line in environment.stdout.decode().splitlines() if "=" in line)
    # A local guest is this machine, whose root may live elsewhere; a VM is Debian. Only a local
    # lane reads the passwd database, so a Windows workstation's V lane needs no pwd.
    if isinstance(lane, LocalFilesystem):
        import pwd

        root_home = pwd.getpwuid(0).pw_dir
    else:
        root_home = "/root"
    assert (names.get("HOME"), names.get("USER")) == (root_home, "root")
    assert not [name for name in names if name.startswith("SUDO_")]
    assert "AGW_FROM_STARTUP" not in names


def elevated_stdin_positioning(lane: Lane) -> None:
    needs_sudo(lane)
    data = b"0000000005hello" + bytes(range(256)) * 1024

    streamed = lane.stream(cmd("cat", elevated=True), stdin=data)

    assert (streamed.status, streamed.stdout) == (0, data)


def elevated_under_the_default_sudoers(lane: Lane) -> None:
    # On a fresh VM: use_pty, as Debian ships it, must leave the launcher's stream check passing.
    assert lane.run(cmd("true", elevated=True)).completion == Completed(0)
    version = lane.run(cmd("sudo", "-V", elevated=True))
    assert version.completion == Completed(0)
    print(version.stdout.decode(errors="replace"))  # the evidence the cell records


def elevated_background_child_holds_both_streams(lane: Lane) -> None:
    needs_sudo(lane)
    started = time.monotonic()

    result = lane.run(script("sleep 5 &\necho done", elevated=True))

    assert time.monotonic() - started < 4
    assert result.completion == Completed(0)
    assert result.stdout == b"done\n"


# -- The list ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vector:
    name: str
    lanes: str
    check: Callable[..., None]


def build() -> list[Vector]:
    shared = [
        Vector("literal-argv", "LSV", literal_argv),
        Vector("script-versus-stdin", "LSV", script_versus_stdin),
        Vector("interpreter-and-login", "LV", interpreter_and_login),
        Vector("environment-bytes", "LSV", environment_bytes),
        Vector("size-limits", "LV", size_limits),
        Vector("working-directory", "LV", working_directory),
        Vector("missing-command", "LSV", missing_command),
        Vector("exit-statuses", "LSV", exit_statuses),
        Vector("stdin-positioning", "LSV", stdin_positioning),
        Vector("stdin-consumer-exits-early", "LS", stdin_consumer_exits_early),
        Vector("stdout-consumer-leaves-endless-writer", "LS", stdout_consumer_leaves_an_endless_writer),
        Vector("stderr-consumer-leaves-endless-writer", "LS", stderr_consumer_leaves_an_endless_writer),
        Vector("stdout-consumer-leaves-quiet-payload", "LS", stdout_consumer_leaves_a_quiet_payload_that_then_ends),
        Vector("several-arguments-run-under-the-shell", "LSV", several_arguments_run_as_a_shell_command_line),
        Vector("bash-child-reads-no-startup-file", "LSV", bash_child_reads_no_startup_file),
        Vector("background-child-holds-both-streams", "LSV", background_child_holds_both_streams),
        Vector("no-trailing-newline-and-refusal", "LS", output_without_a_trailing_newline_and_a_refusal),
        Vector("deadline", "LS", deadline),
        Vector("handover-success", "LSV", handover_hands_the_terminal_a_login_shell),
        Vector("handover-refused-before-terminal", "LSV", handover_refuses_before_the_terminal_is_touched),
        Vector("handover-existing-name", "LSV", handover_never_reuses_an_existing_name),
        Vector(
            "handover-second-connection-never-arrives",
            "LSV",
            handover_whose_second_connection_never_arrives_leaves_nothing,
        ),
        Vector("handover-wait-expires", "LSV", handover_gives_up_after_its_wait),
        Vector("streams-left-non-blocking", "LSV", payload_leaves_the_shared_streams_non_blocking_and_full),
        Vector("host-key-mismatch", "S", host_key_mismatch_is_trust_mismatch),
        Vector("wrong-identity-is-auth-failed", "SV", wrong_identity_is_an_authentication_failure),
        Vector("E1-elevated-rebuilt-environment", "LV", elevated_runs_as_root_in_the_rebuilt_environment),
        Vector("E2-elevated-stdin-positioning", "LV", elevated_stdin_positioning),
        Vector("E4-elevated-default-sudoers", "V", elevated_under_the_default_sudoers),
        Vector("E6-elevated-background-child", "LV", elevated_background_child_holds_both_streams),
    ]
    startup_exits = [
        Vector(
            f"startup-{startup.replace(' ', '-')}",
            "L",
            partial(startup_that_exits_decides_by_status, startup=startup, expected=expected),
        )
        for startup, expected in [
            ("exit 0", NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)),
            ("exit 1", NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)),
            ("exit 255", Unknown(UnknownReason.CONNECTION)),
        ]
    ]
    startup_redirects = [
        Vector(
            f"startup-{redirect.replace(' ', '-').replace('>', 'to').replace('&', 'and').replace('/', '_')}",
            "L",
            partial(startup_that_redirects_a_standard_stream_leaves_the_request_not_delivered, redirect=redirect),
        )
        for redirect in ("exec >/dev/null", "exec 2>&1", "exec 2>/dev/null", "exec >&2")
    ]
    local_only = [
        Vector("stage0-noisy-startup", "L", stage0_survives_a_noisy_startup_file),
        Vector("output-before-marker-dropped", "L", output_before_the_start_marker_is_dropped),
        Vector("python-missing", "L", python_missing_is_not_delivered),
        Vector("standard-library-missing", "L", incomplete_standard_library_is_not_delivered),
        Vector("killed-launcher", "L", killed_launcher_is_unknown),
        Vector("startup-reads-stdin", "L", startup_that_reads_stdin_leaves_the_request_not_delivered),
        Vector("login-flag-runs-login-startup", "L", login_flag_runs_the_account_login_startup),
        Vector(
            "resets-signals-descriptors-session-variables", "L", signals_descriptors_and_session_variables_are_reset
        ),
        Vector("handover-clean-dispositions", "L", handover_shell_starts_with_clean_dispositions),
        Vector("parking-never-reports", "L", parking_that_never_reports_is_bounded_and_leaves_the_terminal_alone),
        Vector("memfd-noexec-seal-refused", "L", memfd_refusing_the_noexec_seal_still_runs_the_script),
        Vector("memfd-cannot-be-created", "L", memfd_that_cannot_be_created_is_not_delivered),
    ]
    return [*shared, *startup_exits, *startup_redirects, *local_only]


VECTORS = build()


def vectors_for(lane: str) -> list[Vector]:
    return [vector for vector in VECTORS if lane in vector.lanes]
