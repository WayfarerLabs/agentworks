"""Read-only probes in the target user's own shell: presence, a command's status, and nothing else leaving the guest."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.command_checks import check_required_commands, presence_probe, run_user_shell_command
from agentworks.errors import ExternalError
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from tests.conftest import requires_posix_shell
from tests.vm_access_support import Admin, RecordingVMAccess, account_shell, command_status, local_vm_access

if TYPE_CHECKING:
    from agentworks.execution.access import VMAccess
    from agentworks.execution.requests import RunRequest

MARKER = "untrusted-output-6d2f"


def _account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str = "") -> tuple[VMAccess, Path, list[Result]]:
    """A local account whose login startup prints and puts its own ``bin`` on ``PATH``, and the results it returns."""
    home = tmp_path / "home"
    bin_dir = tmp_path / "login-bin"
    home.mkdir()
    bin_dir.mkdir()
    (home / ".profile").write_text(
        f"printf '{MARKER} startup stdout\\n'\nprintf '{MARKER} startup stderr\\n' >&2\n"
        f'export PATH="{bin_dir}:$PATH"\n{profile}'
    )
    results: list[Result] = []

    def log(request: RunRequest, result: Result) -> None:
        results.append(result)

    access = local_vm_access(monkeypatch, home=home, shell=account_shell(tmp_path), log=log)
    return access, bin_dir, results


@requires_posix_shell
def test_required_command_uses_the_login_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    access, bin_dir, _ = _account(tmp_path, monkeypatch)
    command = bin_dir / "login-only-command"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)

    assert check_required_commands((command.name,), access) == ()
    assert check_required_commands(("absent-command-agw",), access) == ("absent-command-agw",)


@requires_posix_shell
def test_a_probe_reports_its_commands_status_and_none_of_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access, _, results = _account(tmp_path, monkeypatch)

    status = run_user_shell_command(
        f"echo '{MARKER} command stdout'; echo '{MARKER} command stderr' >&2; exit 21", access
    )

    assert status == 21
    assert all(f"{MARKER} command".encode() not in result.stdout + result.stderr for result in results)
    assert results and all(not result.stderr for result in results)


@requires_posix_shell
def test_a_startup_file_that_exits_first_is_indeterminate_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access, _, _ = _account(tmp_path, monkeypatch, profile="exit 0\n")

    with pytest.raises(ExternalError):
        check_required_commands(("sh",), access)


@requires_posix_shell
def test_startup_output_cannot_forge_a_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    access, _, _ = _account(tmp_path, monkeypatch, profile="printf 'agentworks-command-status:0\\n'\n")

    assert run_user_shell_command("exit 20", access) == 20


def test_required_command_sees_interactive_startup_in_real_zsh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")
    bin_dir = tmp_path / "interactive-bin"
    bin_dir.mkdir()
    command = bin_dir / "interactive-only-command"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    # Only an interactive zsh reads .zshrc.
    (tmp_path / ".zshrc").write_text(f'export PATH="{bin_dir}:$PATH"\n')
    access = local_vm_access(monkeypatch, home=tmp_path, shell=Path(zsh))

    assert check_required_commands((command.name, "sh"), access) == ()
    assert check_required_commands(("absent-command-agw",), access) == ("absent-command-agw",)


@pytest.mark.parametrize(
    "outcome",
    [
        Result(Completed(0), b"secret-stdout\n", b""),
        Result(Completed(0), b"agentworks-command-status:256\n", b""),
        command_status(7),
    ],
)
def test_an_indeterminate_check_fails_without_quoting_what_it_read(outcome: Result) -> None:
    access = RecordingVMAccess([], Admin())
    access.respond(presence_probe("gh"), outcome)

    with pytest.raises(ExternalError) as raised:
        check_required_commands(("gh",), access)

    assert "secret" not in str(raised.value)


def test_a_check_that_may_not_have_run_raises_its_execution_error() -> None:
    access = RecordingVMAccess([], Admin())
    access.respond(presence_probe("gh"), Result(Unknown(UnknownReason.CONNECTION)))

    with pytest.raises(ExecutionIncomplete):
        check_required_commands(("gh",), access)
