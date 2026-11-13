"""Managed package paths must survive actual Bash startup precedence."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _write_agent_profile
from agentworks.ssh import SSHResult
from agentworks.vms.initializer.shell_env import _write_agentworks_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux guest Bash startup")


@pytest.mark.parametrize("role", ["admin", "agent"])
@pytest.mark.parametrize(
    "startup,trailing_newline",
    [
        (None, True),
        (".profile", False),
        (".bash_profile", True),
        (".bash_profile", False),
        (".bash_login", True),
        (".bash_login", False),
    ],
)
def test_actual_bash_login_finds_package_tools_and_preserves_dotfiles(
    tmp_path: Path, role: str, startup: str | None, trailing_newline: bool
) -> None:
    binary_dir = tmp_path / ".local/bin"
    binary_dir.mkdir(parents=True)
    probe = binary_dir / "npm-probe"
    probe.write_text("#!/bin/sh\nprintf '%s' ready\n")
    probe.chmod(0o755)
    if startup is not None:
        content = "# managed shell setup\nexport DOTFILE_MARKER=kept"
        (tmp_path / startup).write_text(content + ("\n" if trailing_newline else ""))
    child_env = {**os.environ, "HOME": str(tmp_path)}
    target = MagicMock()

    def write_file(path: str, content: str, **kwargs) -> None:
        Path(path.replace("~", str(tmp_path), 1)).write_text(content)

    def run(command: str, **kwargs) -> SSHResult:
        result = subprocess.run(["/bin/sh", "-c", command], env=child_env, capture_output=True, text=True, check=True)
        return SSHResult(result.returncode, result.stdout, result.stderr)

    target.write_file.side_effect = write_file
    target.run.side_effect = run
    for _ in range(2):
        if role == "admin":
            _write_agentworks_profile(target, [str(binary_dir)], MagicMock())
        else:
            _write_agent_profile(
                target, home=str(tmp_path), shell="bash", identity_env={}, path_additions=[str(binary_dir)]
            )
        result = subprocess.run(
            ["bash", "-lc", 'npm-probe; printf ":%s\\n%s" "${DOTFILE_MARKER:-unset}" "$PATH"'],
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
        )
        state, path = result.stdout.splitlines()
        assert state == ("ready:kept" if startup is not None else "ready:unset")
        assert path.split(":").count(str(binary_dir)) == 1
    for optional in (".bash_profile", ".bash_login"):
        assert (tmp_path / optional).exists() is (optional == startup)
