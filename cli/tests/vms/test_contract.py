"""The VM contract: the script protocol parser, the requests and their mapping, enforcement, and the fragments."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.debian import DebianRelease
from agentworks.errors import StateError
from agentworks.execution.errors import ExecutionIncomplete, VMNotReady
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Result,
    Unknown,
    UnknownReason,
)
from agentworks.vms import contract
from agentworks.vms.contract import (
    BASE_SCRIPT_INCOMPLETE,
    BASE_TOOLS,
    DETAIL_NOT_SHOWN,
    LOGIN_SHELLS,
    PAM_ENV,
    SUDO,
    SYSTEMD,
    BaseReport,
    Finding,
    parse_script_output,
)
from tests.vm_access_support import Admin, Agent, RecordingVMAccess

if TYPE_CHECKING:
    from pathlib import Path

    from tests.vm_access_support import Call

TRIXIE = b'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'
BOOKWORM = b'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
END = b"AGW-CHECK-END\n"
ACCOUNTS = ("agentworks", "agt-worker")
WORKER = Agent("worker", "agt-worker")


def parse(stdout: bytes, *, status: int = 0, accounts: tuple[str, ...] = ACCOUNTS) -> tuple[Finding, ...]:
    return parse_script_output(stdout, status=status, accounts=accounts)


# -- The parser: the guest's output is input ----------------------------------------------


def test_a_clean_run_has_no_findings() -> None:
    assert parse(END) == ()


@pytest.mark.parametrize(
    ("line", "finding"),
    [
        (
            b"AGW-CHECK systemd the system was not booted with systemd",
            Finding(SYSTEMD, ("the system was not booted with systemd",)),
        ),
        (b"AGW-CHECK base-tools flock setsid", Finding(BASE_TOOLS, ("flock setsid",))),
        (b"AGW-CHECK login-shells agt-worker", Finding(LOGIN_SHELLS, ("agt-worker",))),
        (
            b"AGW-CHECK pam-env /etc/environment ~/.pam_environment",
            Finding(PAM_ENV, ("/etc/environment ~/.pam_environment",)),
        ),
    ],
    ids=["systemd", "base-tools", "login-shells", "pam-env"],
)
def test_a_problem_line_reports_its_row(line: bytes, finding: Finding) -> None:
    assert parse(line + b"\r\n" + END) == (finding,)


def test_an_unknown_id_and_a_line_that_is_not_a_problem_are_dropped() -> None:
    stdout = b"AGW-CHECK sudo root\nAGW-CHECK release trixie\nAGW-CHECK nonsense x\nAGW-CHECKsystemd x\nnoise\n" + END

    assert parse(stdout) == ()


@pytest.mark.parametrize(
    "detail",
    [b"x" * 81, b"a,b", b"\x1b[31mred", b"$(id)", b"tab\there", b"\xff\xfe"],
    ids=["too-long", "comma", "escape", "substitution", "tab", "undecodable"],
)
def test_a_hostile_or_malformed_detail_still_reports_its_row(detail: bytes) -> None:
    assert parse(b"AGW-CHECK systemd " + detail + b"\n" + END) == (Finding(SYSTEMD, (DETAIL_NOT_SHOWN,)),)


def test_repeated_lines_for_a_row_aggregate_into_one_finding() -> None:
    stdout = (
        b"AGW-CHECK login-shells agentworks\nAGW-CHECK login-shells agt-worker\nAGW-CHECK login-shells agt-worker\n"
    )

    assert parse(stdout + END) == (Finding(LOGIN_SHELLS, ("agentworks", "agt-worker")),)


def test_an_account_the_guest_invents_is_dropped() -> None:
    stdout = b"AGW-CHECK login-shells root\nAGW-CHECK login-shells agt-worker\n" + END

    assert parse(stdout) == (Finding(LOGIN_SHELLS, ("agt-worker",)),)


def test_a_missing_end_is_one_failure_naming_the_status_and_no_stream_text() -> None:
    stdout = b"AGW-CHECK systemd the system was not booted with systemd\nAGW-CHECK pam-env /etc/environment\n"

    assert parse(stdout, status=2) == (Finding(BASE_SCRIPT_INCOMPLETE, ("exit status 2",)),)


def test_findings_come_in_table_order() -> None:
    stdout = b"AGW-CHECK pam-env /etc/environment\nAGW-CHECK systemd x\n" + END

    assert [finding.check for finding in parse(stdout)] == [SYSTEMD, PAM_ENV]


# -- The requests --------------------------------------------------------------------------


def access(*outcomes: Result | BaseException) -> RecordingVMAccess:
    calls: list[Call] = []
    recording = RecordingVMAccess(calls, Admin())
    recording.run_outcomes = list(outcomes)
    return recording


def check(recording: RecordingVMAccess, *, expected: DebianRelease | None = None) -> BaseReport:
    return contract.check_base(recording, expected_release=expected, admin="agentworks", agents=[WORKER])


def test_check_base_asks_for_the_release_first_then_the_script_then_sudo() -> None:
    recording = access(Result(Completed(0), TRIXIE), Result(Completed(0), END), Result(Completed(0), b"0\n"))

    report = check(recording, expected=DebianRelease.TRIXIE)

    release, script, sudo = [call.request for call in recording.calls]
    assert isinstance(release, Command)
    assert release.argv == ("cat", "/etc/os-release")
    assert isinstance(script, Script)
    assert not script.elevated
    assert script.env == {contract.ACCOUNTS_VARIABLE: "agentworks agt-worker"}
    assert sudo == SUDO.probe
    assert report.release is DebianRelease.TRIXIE
    assert report.findings == ()


@pytest.mark.parametrize(
    ("os_release", "expected"),
    [(BOOKWORM, DebianRelease.TRIXIE), (b"ID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=noble\n", None)],
    ids=["mismatch", "unrecognized"],
)
def test_a_release_that_fails_raises_before_anything_else_is_asked(
    os_release: bytes, expected: DebianRelease | None
) -> None:
    recording = access(Result(Completed(0), os_release))

    with pytest.raises(StateError):
        check(recording, expected=expected)
    assert len(recording.calls) == 1


@pytest.mark.parametrize(
    ("os_release", "release"), [(TRIXIE, DebianRelease.TRIXIE), (BOOKWORM, DebianRelease.BOOKWORM)]
)
def test_observe_release_accepts_both_recognized_releases(os_release: bytes, release: DebianRelease) -> None:
    assert contract.observe_release(access(Result(Completed(0), os_release)), expected=None) is release


@pytest.mark.parametrize(
    ("outcome", "passes"),
    [
        (Result(Completed(0), b"0\n"), True),
        (Result(Completed(0), b"1000\n"), False),
        (Result(Completed(1)), False),
        (Result(NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)), False),
        (Result(NotDelivered(NotDeliveredReason.PYTHON_MISSING)), False),
    ],
    ids=["root", "not-root", "nonzero", "no-passwordless-sudo", "no-sudo"],
)
def test_the_elevated_outcome_is_the_sudo_rows_own(outcome: Result, passes: bool) -> None:
    report = check(access(Result(Completed(0), TRIXIE), Result(Completed(0), END), outcome))

    assert report.findings == (() if passes else (Finding(SUDO, ("sudo -n did not run as root",)),))


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (Result(Unknown(UnknownReason.CONNECTION)), ExecutionIncomplete),
        (Result(Unknown(UnknownReason.DEADLINE)), ExecutionIncomplete),
        (Result(NotDelivered(NotDeliveredReason.STDLIB_MISSING)), VMNotReady),
    ],
)
def test_any_other_elevated_outcome_raises(outcome: Result, error: type[Exception]) -> None:
    with pytest.raises(error):
        check(access(Result(Completed(0), TRIXIE), Result(Completed(0), END), outcome))


def test_a_vm_without_python_raises_before_any_row_is_read() -> None:
    with pytest.raises(VMNotReady):
        check(access(Result(NotDelivered(NotDeliveredReason.PYTHON_MISSING))))


# -- Enforcement ---------------------------------------------------------------------------


def report(*findings: Finding) -> BaseReport:
    return BaseReport(DebianRelease.TRIXIE, findings, {"agentworks": Admin(), "agt-worker": WORKER})


def test_a_failure_raises() -> None:
    with pytest.raises(StateError):
        contract.enforce(report(Finding(BASE_TOOLS, ("flock",))), "box")


def test_a_probe_that_did_not_complete_raises() -> None:
    with pytest.raises(StateError):
        contract.enforce(report(Finding(BASE_SCRIPT_INCOMPLETE, ("exit status 2",))), "box")


def test_a_warning_only_report_warns_and_does_not_raise(captured_output: Any) -> None:
    contract.enforce(
        report(Finding(LOGIN_SHELLS, ("agentworks", "agt-worker")), Finding(PAM_ENV, ("/etc/environment",))), "box"
    )

    assert len(captured_output.warnings) == 3


def test_an_agents_shell_warning_names_agent_reinit_and_the_admins_names_vm_reinit(captured_output: Any) -> None:
    contract.enforce(report(Finding(LOGIN_SHELLS, ("agentworks", "agt-worker"))), "box")

    admin, agent = captured_output.warnings
    assert "agw vm reinit box" in admin
    assert "agw agent reinit worker" in agent


# -- The fragments, run for real by a local shell ------------------------------------------
#
# The guest runs the script under /bin/sh, which is dash on Debian.

SHELLS = [name for name in ("sh", "dash", "bash") if shutil.which(name)]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the script is for a POSIX guest")


def run_script(
    shell: str, *, home: Path, accounts: str = "", path: str | None = None, script: str = contract.BASE_SCRIPT
) -> tuple[Finding, ...]:
    env = {"HOME": str(home), "PATH": path or os.environ["PATH"], contract.ACCOUNTS_VARIABLE: accounts}
    completed = subprocess.run(
        [shutil.which(shell) or shell, "-c", script], env=env, cwd=home, capture_output=True, check=False, timeout=30
    )
    assert completed.returncode == 0
    return parse_script_output(completed.stdout, status=completed.returncode, accounts=accounts.split())


def finding(findings: tuple[Finding, ...], row: contract.Check) -> Finding | None:
    return next((found for found in findings if found.check is row), None)


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_base_tools_names_each_missing_tool(shell: str, tmp_path: Path) -> None:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for tool in ("install", "nohup", "tar"):
        (stubs / tool).write_text("#!/bin/sh\n")
        (stubs / tool).chmod(0o755)

    found = finding(run_script(shell, home=tmp_path, path=str(stubs)), BASE_TOOLS)

    assert found == Finding(BASE_TOOLS, ("flock setsid",))


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_login_shells_reports_an_unsupported_shell_and_an_account_with_no_entry(shell: str, tmp_path: Path) -> None:
    import pwd

    if shutil.which("getent") is None:
        pytest.skip("the guest's getent is not on this machine")
    root_supported = os.path.basename(pwd.getpwuid(0).pw_shell or "/bin/sh") in ("sh", "dash", "bash", "zsh")

    found = finding(run_script(shell, home=tmp_path, accounts="root agw-no-such-account"), LOGIN_SHELLS)

    expected = ("agw-no-such-account",) if root_supported else ("root", "agw-no-such-account")
    assert found == Finding(LOGIN_SHELLS, expected)


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_an_account_name_is_never_globbed(shell: str, tmp_path: Path) -> None:
    # Globbed, the word would become this file's name, which is not an account passed in and so dropped.
    (tmp_path / "agw-glob-match").write_text("")

    found = finding(run_script(shell, home=tmp_path, accounts="agw-*"), LOGIN_SHELLS)

    assert found == Finding(LOGIN_SHELLS, (DETAIL_NOT_SHOWN,))


# pam-env: only the home entry is asserted, since the two system files are the host's own
# /etc. The sshd PAM stack the fragment consults is a temporary file, like Debian 12's by default.

HOME_ENTRY = "~/.pam_environment"
SSHD_PAM_PATH = "/etc/pam.d/sshd"
DEBIAN_12_SSHD_PAM = b"session    required     pam_env.so user_readenv=1 envfile=/etc/default/locale\n"


def pam_env_files(shell: str, home: Path, sshd_pam: bytes | None = DEBIAN_12_SSHD_PAM) -> list[str]:
    pam_file = home / "sshd-pam"
    if sshd_pam is not None:
        pam_file.write_bytes(sshd_pam)
    script = contract.BASE_SCRIPT.replace(SSHD_PAM_PATH, str(pam_file))
    assert script != contract.BASE_SCRIPT
    found = finding(run_script(shell, home=home, script=script), PAM_ENV)
    return [] if found is None else [file for detail in found.details for file in detail.split()]


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("contents", "reported"),
    [
        (None, False),
        (b"", False),
        (b"\n\n", False),
        (b"# a comment\n\n   \n  # indented comment\n", False),
        (b"# a comment\r\n\r\n   \r\n  # indented\r\n", False),
        (b"FOO=1\n", True),
        (b"# a comment\n\nFOO DEFAULT=1", True),
        (b"  FOO=1\r\n", True),
    ],
    ids=[
        "missing",
        "empty",
        "blank-lines",
        "comments-and-blanks",
        "comments-and-blanks-crlf",
        "setting",
        "setting-without-trailing-newline",
        "indented-setting-crlf",
    ],
)
def test_pam_env_reports_a_file_only_when_it_holds_a_setting(
    shell: str, contents: bytes | None, reported: bool, tmp_path: Path
) -> None:
    if contents is not None:
        (tmp_path / ".pam_environment").write_bytes(contents)

    assert (HOME_ENTRY in pam_env_files(shell, tmp_path)) is reported


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("sshd_pam", "reported"),
    [
        (DEBIAN_12_SSHD_PAM, True),
        (b"session required pam_env.so envfile=/etc/default/locale\n", False),
        (b"# session required pam_env.so user_readenv=1\n", False),
        (b"   #session required pam_env.so user_readenv=1\n", False),
        (b"session required pam_unix.so user_readenv=1\n", False),
        (b"session required pam_env.so # user_readenv=1\n", False),
        (b"", False),
        (None, False),
    ],
    ids=[
        "user_readenv-present",
        "user_readenv-absent",
        "line-commented-out",
        "indented-line-commented-out",
        "other-module",
        "option-in-trailing-comment",
        "empty-stack",
        "stack-missing",
    ],
)
def test_pam_env_counts_the_home_file_only_where_sshd_applies_it(
    shell: str, sshd_pam: bytes | None, reported: bool, tmp_path: Path
) -> None:
    (tmp_path / ".pam_environment").write_bytes(b"FOO=1\n")

    assert (HOME_ENTRY in pam_env_files(shell, tmp_path, sshd_pam)) is reported


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("shell", SHELLS)
def test_pam_env_does_not_report_an_unreadable_file(shell: str, tmp_path: Path) -> None:
    unreadable = tmp_path / ".pam_environment"
    unreadable.write_bytes(b"FOO=1\n")
    unreadable.chmod(0)

    assert HOME_ENTRY not in pam_env_files(shell, tmp_path)
