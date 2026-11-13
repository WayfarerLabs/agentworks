"""Focused tests for user install commands run over an agent transport."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _run_agent_install_commands
from agentworks.install_commands import UserInstallCommandEntry
from agentworks.interpolation import context_values
from agentworks.ssh import SSHError
from tests.conftest import CapturedOutput, requires_posix_shell


def _run_commands(
    *entries: UserInstallCommandEntry,
    test_returncode: int,
    target: MagicMock | None = None,
    home: str = "/home/agt-dev",
    shell: str = "bash",
) -> MagicMock:
    if target is None:
        target = MagicMock()

    def run_side_effect(command: str, **kwargs: object) -> MagicMock:
        is_test = "command -v" in command or "test -f" in command or "test -d" in command
        returncode = test_returncode if is_test else 0
        return MagicMock(returncode=returncode, ok=returncode == 0, stdout="", stderr="")

    if target.run.side_effect is None:
        target.run.side_effect = run_side_effect
    registry = MagicMock()
    registry.iter_kind_items.return_value = [(entry.name, entry) for entry in entries]
    template = SimpleNamespace(shell=shell, user_install_commands=[entry.name for entry in entries])
    _run_agent_install_commands(
        agent_target=target,
        registry=registry,
        agent_tmpl=template,
        home=home,
        identity_env={"AGENTWORKS_AGENT": "dev"},
        context=context_values(vm_name="box", user="agt-dev", agent_name="dev"),
    )
    return target


def test_agent_install_skips_when_all_declared_tests_pass() -> None:
    entry = UserInstallCommandEntry(
        name="my-tool",
        command="run-agent-install",
        test_exec="my-tool",
        test_file="~/.my-tool/installed",
        test_dir="~/.my-tool",
    )

    target = _run_commands(entry, test_returncode=0)

    commands = [call.args[0] for call in target.run.call_args_list]
    test_command = next(command for command in commands if "command -v" in command)
    assert "test -f /home/agt-dev/.my-tool/installed" in test_command
    assert "test -d /home/agt-dev/.my-tool" in test_command
    assert " && " in test_command
    assert not any("run-agent-install" in command for command in commands)


def test_agent_install_runs_when_any_declared_test_fails() -> None:
    entry = UserInstallCommandEntry(
        name="my-tool",
        command="run-agent-install",
        test_exec="my-tool",
        test_file="~/.my-tool/installed",
    )

    target = _run_commands(entry, test_returncode=1)

    commands = [call.args[0] for call in target.run.call_args_list]
    assert any("command -v" in command and "test -f" in command for command in commands)
    assert any("run-agent-install" in command for command in commands)


@requires_posix_shell
@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
@pytest.mark.parametrize("test_returncode", [0, 1])
def test_agent_profile_deduplicates_without_changing_path_precedence(
    tmp_path: Path,
    captured_output: CapturedOutput,
    shell: str,
    test_returncode: int,
) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is unavailable")
    entries = [
        UserInstallCommandEntry(
            name="first",
            command="install-first",
            test_exec="first",
            path=["~/.local/bin", "~/.local/bin", "/opt/tools"],
        ),
        UserInstallCommandEntry(
            name="second",
            command="install-second",
            test_exec="second",
            path=["$HOME/.local/bin"],
        ),
    ]
    target = MagicMock()

    def write_file(path: str, content: str, **kwargs: object) -> None:
        Path(path).write_text(content)

    def run(command: str, **kwargs: object) -> MagicMock:
        if "grep -q" in command:
            subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True)
        return MagicMock(returncode=test_returncode)

    target.write_file.side_effect = write_file
    target.run.side_effect = run
    profile = tmp_path / ".agentworks-profile.sh"
    original_content = ""
    for _ in range(2):
        _run_commands(*entries, test_returncode=test_returncode, target=target, home=str(tmp_path), shell=shell)
        content = profile.read_text()
        if original_content:
            assert content == original_content
        original_content = content
        # Only the numeric observation is a contract; surrounding prose is free to change.
        assert re.findall(r"\d+", captured_output.info[-1]) == ["2"]
        result = subprocess.run(
            [shell_path, "-c", '. "$HOME/.agentworks-profile.sh"; printf "%s\\n" "$PATH" "$AGENTWORKS_AGENT"'],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.splitlines() == [f"{tmp_path}/.local/bin:/opt/tools:/usr/bin:/bin", "dev"]
    rc_files = [".profile", ".bashrc"] + ([".zprofile"] if shell == "zsh" else [])
    for name in rc_files:
        assert (tmp_path / name).read_text().splitlines() == [f". {profile}"]
    installs = [call.args[0] for call in target.run.call_args_list if "install-" in call.args[0]]
    assert len(installs) == (0 if test_returncode == 0 else 4)

    # Removing the install commands on reinit also removes their managed paths.
    _run_commands(test_returncode=test_returncode, target=target, home=str(tmp_path), shell=shell)
    result = subprocess.run(
        [shell_path, "-c", '. "$HOME/.agentworks-profile.sh"; printf "%s" "$PATH"'],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "/usr/bin:/bin"


def test_agent_install_runs_with_zero_non_empty_tests() -> None:
    entry = UserInstallCommandEntry(
        name="my-tool",
        command="run-agent-install",
        test_exec="",
        test_file="",
        test_dir="",
    )

    target = _run_commands(entry, test_returncode=0)

    commands = [call.args[0] for call in target.run.call_args_list]
    assert not any("command -v" in command or "test -f" in command or "test -d" in command for command in commands)
    assert any("run-agent-install" in command for command in commands)


@requires_posix_shell
@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_agent_failed_install_clears_its_path_on_reinit_and_continues(
    tmp_path: Path, captured_output: CapturedOutput, shell: str
) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is unavailable")
    entries = [
        UserInstallCommandEntry(name="first", command="install-first", path=["~/.first/bin"]),
        UserInstallCommandEntry(name="broken", command="install-broken", path=["~/.broken/bin"]),
        UserInstallCommandEntry(
            name="present", command="install-present", test_file="~/.present", path=["~/.present/bin"]
        ),
        UserInstallCommandEntry(name="last", command="install-last", path=["~/.last/bin"]),
    ]
    target = MagicMock()
    fail_install = False

    def write_file(path: str, content: str, **kwargs: object) -> None:
        Path(path).write_text(content)

    def run(command: str, **kwargs: object) -> MagicMock:
        if "install-broken" in command and fail_install:
            raise SSHError("installer exited 1")
        if "grep -q" in command:
            subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True)
        return MagicMock(returncode=0)

    target.write_file.side_effect = write_file
    target.run.side_effect = run
    for fail_install in (False, True):
        target.run.reset_mock()
        _run_commands(*entries, test_returncode=0, target=target, home=str(tmp_path), shell=shell)
        result = subprocess.run(
            [shell_path, "-c", '. "$HOME/.agentworks-profile.sh"; printf "%s\\n" "$PATH" "$AGENTWORKS_AGENT"'],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            check=True,
            capture_output=True,
            text=True,
        )
        expected = ["last", "present"] + ([] if fail_install else ["broken"]) + ["first"]
        assert result.stdout.splitlines() == [
            ":".join([*(f"{tmp_path}/.{name}/bin" for name in expected), "/usr/bin", "/bin"]),
            "dev",
        ]
        installs = [call.args[0] for call in target.run.call_args_list if "install-" in call.args[0]]
        assert installs == [f"{shell} -lc install-{name}" for name in ["first", "broken", "last"]]
    assert len(captured_output.warnings) == 1
