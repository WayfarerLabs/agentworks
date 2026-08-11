"""Shell-level behavior for install-command executable predicates."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agentworks.agents.initializer import _build_agent_test_command
from agentworks.install_commands import UserInstallCommandEntry
from agentworks.vms.initializer.packages import _build_test_command

PredicateBuilder = Callable[[UserInstallCommandEntry, str], str | None]


def _vm_predicate(entry: UserInstallCommandEntry, shell: str) -> str | None:
    return _build_test_command(entry, shell, "/home/agentworks")


def _agent_predicate(entry: UserInstallCommandEntry, shell: str) -> str | None:
    return _build_agent_test_command(entry, "/home/agent", shell)


@pytest.mark.parametrize("builder", [_vm_predicate, _agent_predicate], ids=("vm", "agent"))
@pytest.mark.parametrize("shell", ["bash", "zsh", "dash"])
@pytest.mark.parametrize("executable", [True, False], ids=("executable", "non-executable"))
def test_slash_test_exec_uses_shell_independent_executable_predicate(
    tmp_path: Path,
    builder: PredicateBuilder,
    shell: str,
    executable: bool,
) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"test shell {shell!r} is unavailable")
    executable_path = tmp_path / "tool with spaces"
    executable_path.write_text("#!/bin/sh\nexit 0\n")
    executable_path.chmod(0o755 if executable else 0o644)
    entry = UserInstallCommandEntry(
        name="path-tool",
        command="install-path-tool",
        test_exec=str(executable_path),
    )

    predicate = builder(entry, shell)

    assert predicate == f"test -x {shlex.quote(str(executable_path))}"
    result = subprocess.run([shell_path, "-c", predicate], check=False)
    assert (result.returncode == 0) is executable


@pytest.mark.skipif(not (sys.platform.startswith("linux") and bool(os.environ.get("CI"))), reason="Linux CI only")
def test_linux_ci_installs_and_exercises_all_predicate_shells(tmp_path: Path) -> None:
    executable_path = tmp_path / "tool"
    executable_path.write_text("#!/bin/sh\nexit 0\n")
    executable_path.chmod(0o755)

    for shell in ("bash", "zsh", "dash"):
        shell_path = shutil.which(shell)
        assert shell_path is not None, f"Linux CI must provide {shell}"
        result = subprocess.run([shell_path, "-c", f"test -x {shlex.quote(str(executable_path))}"], check=False)
        assert result.returncode == 0


def test_bare_test_exec_keeps_each_runners_login_shell_shape() -> None:
    entry = UserInstallCommandEntry(name="path-tool", command="install-path-tool", test_exec="path-tool")

    assert _vm_predicate(entry, "zsh") == "zsh -lic 'command -v path-tool' > /dev/null 2>&1"
    assert _agent_predicate(entry, "bash") == "bash -lc 'command -v path-tool > /dev/null 2>&1'"
