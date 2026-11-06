"""The vectors for the request vocabulary slice 5 adds, F1 to F5 and F8 in the files LLD.

They join :mod:`vectors`' list, so the same lanes run them: finite stdin and
the operation log (F1, F2), core-owned Python programs and their startup (F3,
F4), an interactive script (F5), a startup file that closes low descriptors,
the run sinks, and the environment of an elevated request (F8). Each asserts
observable behavior only.
"""

from __future__ import annotations

import ast
import json
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from agentworks import ssh
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import (
    Bytes,
    CaptureOutput,
    Command,
    DiscardOutput,
    NoInput,
    PythonProgram,
    Script,
    Shell,
)
from agentworks.execution.results import Completed, Unknown, UnknownReason
from agentworks.ssh import SSHLogger
from tests.execution.conformance.lanes import LocalLane, needs_sudo

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result
    from tests.execution.conformance.lanes import Lane

REQUEST_DEADLINE = 60


def python(source: str, *, stdin: bytes | None = None, log_streams: bool = True) -> Command:
    return Command(
        ("python3", "-c", source),
        stdin=NoInput() if stdin is None else Bytes(stdin),
        log_streams=log_streams,
        deadline=REQUEST_DEADLINE,
    )


# -- F1: stdin never reaches a record or an error -----------------------------------------


def stdin_reaches_no_record_and_no_error(lane: Lane) -> None:
    secret = f"agw-f1-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as logs, mock.patch.object(ssh, "LOG_DIR", Path(logs)):
        # The operation log a command really keeps, in a directory of its own.
        logger = SSHLogger("conformance", "F1")
        records: list[None] = []

        def log(request: RunRequest, result: Result) -> None:
            records.append(None)
            logger.record(request, result)

        access = lane.access_with_log(log)
        reads = "import sys; sys.stdin.buffer.read()"
        for log_streams in (True, False):
            request = python(reads, stdin=secret.encode(), log_streams=log_streams)

            result = access.run(request)

            assert result.completion == Completed(0)
            assert secret not in repr(request) + repr(result)
        echoes = (
            "import sys; data = sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data); sys.exit(3)"
        )
        withheld = python(echoes, stdin=secret.encode(), log_streams=False)

        with pytest.raises(CommandFailed) as raised:
            access.run(withheld, check=True)

        error = raised.value
        assert error.result.stdout == error.result.stderr == secret.encode()  # the payload did echo it
        assert all(secret not in text for text in (str(error), repr(error), error.hint or ""))
        assert len(records) == 3
        assert secret not in logger.path.read_text()

        with pytest.raises(CommandFailed):
            access.run(python(echoes, stdin=secret.encode()), check=True)
        assert secret in logger.path.read_text()  # the streams are logged unless the request withholds them


# -- F2 and F3: a program's input and arguments arrive exactly ---------------------------


def program(source: str, **options: object) -> PythonProgram:
    return PythonProgram(source, **{"deadline": REQUEST_DEADLINE, **options})  # type: ignore[arg-type]


def every_input_byte_reaches_a_program_at_its_first_byte(lane: Lane) -> None:
    # A prefix that looks like a frame header: anything that read ahead would swallow or misplace it.
    data = b"0000000005hello" + bytes(range(256)) * 512
    copies = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"

    result = lane.run(program(copies, stdin=Bytes(data)))

    assert (result.completion, result.stdout) == (Completed(0), data)


def program_arguments_arrive_exactly_and_the_source_is_in_no_argument(lane: Lane) -> None:
    items = ("", " ", "a b", "$HOME", "`id`", "'", '"', "\\", "*", "-n", "--", "-c", "; echo injected", "-", "\t\n")
    source = (
        "# PROGRAM_TOKEN_3e7f\n"
        "import sys\n"
        "sys.stdout.write(repr((sys.argv[1:], open('/proc/self/cmdline', 'rb').read())))\n"
    )

    result = lane.run(program(source, args=items))

    assert result.completion == Completed(0)
    arguments, cmdline = ast.literal_eval(result.stdout.decode())
    assert tuple(arguments) == items
    assert b"/dev/fd/" in cmdline  # the interpreter was given a descriptor, not the text
    assert b"PROGRAM_TOKEN" not in cmdline


# -- F4 and F5: startup under the account's default shell -------------------------------


def use_account_shell(lane: Lane) -> str:
    """Rewrite the L lane's launcher to make the lane's shell the account's default, and return that shell's name.

    On S and V nothing is rewritten: the name is the account's own default shell's.
    """
    if isinstance(lane, LocalLane):
        lane.use_lane_shell_as_account_shell()
        return lane.shell_name
    result = lane.run(Command(("printenv", "SHELL"), deadline=REQUEST_DEADLINE))
    assert result.completion == Completed(0)
    return result.stdout.decode().strip().rsplit("/", 1)[-1]


# Each startup file prints, exports its own marker and ends with a failing command substitution:
# zsh returns that stale status from an `exec` that only redirects, which `&&` would act on.
STARTUP_FILES = {".profile": "PROFILE", ".bashrc": "BASHRC", ".zprofile": "ZPROFILE", ".zshrc": "ZSHRC"}


def startup_content(marker: str) -> str:
    return f"echo startup-{marker}\necho startup-{marker} >&2\nexport AGW_F4_{marker}=1\nx=$(false)\n"


def startup_markers(shell: str, *, login: bool, interactive: bool) -> list[str]:
    """Which of the startup files ``shell`` reads with these flags (no ``.bash_profile``, no ``ENV``)."""
    if shell == "zsh":
        return [*(["ZPROFILE"] if login else []), *(["ZSHRC"] if interactive else [])]
    if shell == "bash":
        return ["PROFILE"] if login else ["BASHRC"]
    return ["PROFILE"] if login else []


def program_under_login_and_interactive_startup(lane: Lane) -> None:
    shell = use_account_shell(lane)
    home = lane.make_dir({name: startup_content(marker) for name, marker in STARTUP_FILES.items()})
    items = ("", "a b", "$HOME", "'", "-c", "--")
    source = (
        "import json, os, sys\n"
        "seen = sorted(name[7:] for name in os.environ if name.startswith('AGW_F4_'))\n"
        "print('AGW_F4=' + json.dumps({'argv': sys.argv[1:], 'seen': seen}))\n"
    )
    for login, interactive in ((True, False), (False, True), (True, True)):
        flags = (shell, login, interactive)

        result = lane.run(program(source, args=items, login=login, interactive=interactive, env={"HOME": home}))

        assert result.completion == Completed(0), (*flags, result.stderr)
        reports = [line for line in result.stdout.decode().splitlines() if line.startswith("AGW_F4=")]
        assert reports, (*flags, result.stdout, result.stderr)  # the program ran
        report = json.loads(reports[-1].removeprefix("AGW_F4="))
        assert tuple(report["argv"]) == items, flags
        assert report["seen"] == sorted(startup_markers(shell, login=login, interactive=interactive)), flags


def interactive_script_sees_its_startup_and_owns_its_status(lane: Lane) -> None:
    use_account_shell(lane)
    # A shell that read the script as its input would prompt for each line with these.
    function = "agw_f5() { echo f5-called; }\nPS1='agw-f5-prompt '\nPS2='agw-f5-prompt '\n"
    home = lane.make_dir(
        {
            ".shrc": function,  # sh and dash read $ENV when interactive
            ".bashrc": function,
            ".zshrc": function,
            ".profile": '[ -n "$BASH_VERSION" ] && . "$HOME/.bashrc"\n',  # as Debian's skeleton does
        }
    )
    for shell in (Shell.SH, Shell.BASH, Shell.USER_DEFAULT):
        for login in (False, True):
            request = Script(
                "agw_f5\nexit 7\n",
                shell,
                login=login,
                interactive=True,
                env={"HOME": home, "ENV": f"{home}/.shrc"},
                deadline=REQUEST_DEADLINE,
            )

            result = lane.run(request)

            assert result.completion == Completed(7), (shell, login, result.stderr)
            assert b"f5-called\n" in result.stdout, (shell, login, result.stdout, result.stderr)
            assert b"agw-f5-prompt" not in result.stdout + result.stderr, (shell, login, result.stderr)


def a_startup_file_that_closes_low_descriptors_leaves_the_source(lane: Lane) -> None:
    use_account_shell(lane)
    closes = "exec 3<&- 4<&- 5<&- 6<&- 7<&- 8<&- 9<&-\n"
    home = lane.make_dir({name: closes for name in (*STARTUP_FILES, ".shrc")})
    env = {"HOME": home, "ENV": f"{home}/.shrc"}

    ran = lane.run(program("print('agw-source-ran')", login=True, interactive=True, env=env))
    sourced = lane.run(
        Script(
            "echo agw-source-ran\nexit 4\n",
            Shell.USER_DEFAULT,
            login=True,
            interactive=True,
            env=env,
            deadline=REQUEST_DEADLINE,
        )
    )

    assert ran.completion == Completed(0), ran.stderr
    assert b"agw-source-ran\n" in ran.stdout
    assert sourced.completion == Completed(4), sourced.stderr
    assert b"agw-source-ran\n" in sourced.stdout


# -- F8: an elevated request's environment ---------------------------------------------


def elevated_request_keeps_its_environment(lane: Lane) -> None:
    needs_sudo(lane)

    result = lane.run(Command(("env",), elevated=True, env={"AGW_F8": "a b"}, deadline=REQUEST_DEADLINE))

    assert result.completion == Completed(0)
    assert "AGW_F8=a b" in result.stdout.decode().splitlines()


# -- The run sinks ----------------------------------------------------------------------


def run_sinks_capture_to_their_limit_or_discard(lane: Lane) -> None:
    writes = "import sys; sys.stdout.write('out' * 4); sys.stderr.write('err')"
    request = python(writes)

    discarded = lane.access.run(request, stdout=DiscardOutput(), stderr=DiscardOutput())
    kept = lane.access.run(request, stdout=CaptureOutput(12), stderr=DiscardOutput())
    over = lane.access.run(request, stdout=CaptureOutput(11))

    assert (discarded.completion, discarded.stdout, discarded.stderr) == (Completed(0), b"", b"")
    assert (kept.completion, kept.stdout, kept.stderr) == (Completed(0), b"out" * 4, b"")
    assert over.completion == Unknown(UnknownReason.OUTPUT_LIMIT)


# (name, lanes, check), which ``vectors.build`` turns into its ``Vector`` entries.
ENTRIES: list[tuple[str, str, Callable[..., None]]] = [
    ("F1-stdin-reaches-no-record-or-error", "LSV", stdin_reaches_no_record_and_no_error),
    ("F2-every-input-byte-reaches-a-program", "LSV", every_input_byte_reaches_a_program_at_its_first_byte),
    (
        "F3-program-arguments-exact-source-hidden",
        "LSV",
        program_arguments_arrive_exactly_and_the_source_is_in_no_argument,
    ),
    ("F4-program-under-login-and-interactive-startup", "LV", program_under_login_and_interactive_startup),
    ("F5-interactive-script", "LV", interactive_script_sees_its_startup_and_owns_its_status),
    (
        "startup-closes-low-descriptors",
        "LV",
        a_startup_file_that_closes_low_descriptors_leaves_the_source,
    ),
    ("F8-elevated-request-keeps-its-environment", "LV", elevated_request_keeps_its_environment),
    ("run-sinks", "LS", run_sinks_capture_to_their_limit_or_discard),
]
