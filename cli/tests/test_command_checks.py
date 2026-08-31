"""Shared target-user command checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.command_checks import check_required_commands
from agentworks.ssh import SSHError, SSHResult

if TYPE_CHECKING:
    from agentworks.transports import Transport


class _LoginShellTarget:
    def __init__(self, tmp_path: Path, *, startup_status: int = 0) -> None:
        self.bin_dir = tmp_path / "login-bin"
        self.bin_dir.mkdir()
        self.shell = tmp_path / "login-shell"
        self.shell.write_text(
            "#!/bin/sh\n"
            "printf 'untrusted shell stdout\\n'\n"
            "printf 'untrusted shell stderr\\n' >&2\n"
            'test "$AGW_TEST_STARTUP_STATUS" -eq 0 || exit "$AGW_TEST_STARTUP_STATUS"\n'
            'test "$1" = -lic || exit 90\n'
            'PATH="$AGW_TEST_LOGIN_PATH" exec /bin/sh -c "$2"\n'
        )
        self.shell.chmod(0o755)
        self.results: list[SSHResult] = []
        self.startup_status = startup_status

    def run(self, command: str, **kwargs: object) -> SSHResult:
        assert kwargs["check"] is False
        assert kwargs["discard_output"] is True
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "SHELL": str(self.shell),
                "AGW_TEST_LOGIN_PATH": str(self.bin_dir),
                "AGW_TEST_STARTUP_STATUS": str(self.startup_status),
            },
        )
        ssh_result = SSHResult(result.returncode, result.stdout, result.stderr)
        self.results.append(ssh_result)
        return ssh_result


def test_required_command_uses_target_user_login_environment(tmp_path: Path) -> None:
    target = _LoginShellTarget(tmp_path)
    command = target.bin_dir / "login-only-command"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)

    assert check_required_commands((command.name,), cast("Transport", target)) == ()
    assert check_required_commands(("absent-command",), cast("Transport", target)) == ("absent-command",)
    assert all(not result.stdout and not result.stderr for result in target.results)


def test_required_command_check_fails_value_safely_when_indeterminate() -> None:
    class _IndeterminateTarget:
        def run(self, *_args: object, **kwargs: object) -> SSHResult:
            assert kwargs["discard_output"] is True
            return SSHResult(255, "secret", "hostile")

    target = cast("Transport", _IndeterminateTarget())

    with pytest.raises(SSHError) as exc_info:
        check_required_commands(("gh",), target)

    assert "secret" not in str(exc_info.value)
    assert "hostile" not in str(exc_info.value)


def test_required_command_does_not_confuse_shell_startup_status_with_absence(tmp_path: Path) -> None:
    target = cast("Transport", _LoginShellTarget(tmp_path, startup_status=20))

    with pytest.raises(SSHError):
        check_required_commands(("sh",), target)


def test_required_command_runs_in_real_zsh(tmp_path: Path) -> None:
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")

    class _ZshTarget:
        def run(self, command: str, **kwargs: object) -> SSHResult:
            assert kwargs["check"] is False
            assert kwargs["discard_output"] is True
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                check=False,
                capture_output=True,
                text=True,
                env={
                    "HOME": str(tmp_path),
                    "PATH": "/usr/bin:/bin",
                    "SHELL": zsh,
                    "ZDOTDIR": str(tmp_path),
                },
            )
            return SSHResult(result.returncode, result.stdout, result.stderr)

    assert check_required_commands(("sh",), cast("Transport", _ZshTarget())) == ()
