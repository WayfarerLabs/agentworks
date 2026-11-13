"""Managed package paths must survive actual Bash startup precedence."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _write_agent_profile
from agentworks.ssh import SSHResult
from agentworks.vms.initializer.shell_env import _ensure_agentworks_files_sourced, _write_agentworks_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux guest Bash startup")


@pytest.fixture
def local_target(tmp_path: Path) -> MagicMock:
    target = MagicMock()

    def write_file(path: str, content: str, **kwargs) -> None:
        Path(path.replace("~", str(tmp_path), 1)).write_text(content)

    def run(command: str, **kwargs) -> SSHResult:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=True,
        )
        return SSHResult(result.returncode, result.stdout, result.stderr)

    target.write_file.side_effect = write_file
    target.run.side_effect = run
    return target


def _write_profile(target: MagicMock, home: Path, role: str, paths: list[str]) -> None:
    if role == "admin":
        _write_agentworks_profile(target, paths, MagicMock())
    else:
        _write_agent_profile(target, home=str(home), shell="bash", identity_env={}, path_additions=paths)


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
    tmp_path: Path, local_target: MagicMock, role: str, startup: str | None, trailing_newline: bool
) -> None:
    binary_dir = tmp_path / ".local/bin"
    binary_dir.mkdir(parents=True)
    probe = binary_dir / "npm-probe"
    probe.write_text("#!/bin/sh\nprintf '%s' ready\n")
    probe.chmod(0o755)
    if startup is not None:
        content = "# .agentworks-profile.sh is managed separately.\nexport DOTFILE_MARKER=kept"
        (tmp_path / startup).write_text(content + ("\n" if trailing_newline else ""))
    child_env = {**os.environ, "HOME": str(tmp_path)}
    for _ in range(2):
        _write_profile(local_target, tmp_path, role, [str(binary_dir)])
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


@pytest.mark.parametrize("role", ["admin", "agent"])
@pytest.mark.parametrize("legacy", [None, "variable", "absolute"])
@pytest.mark.parametrize("chained", [False, True])
def test_upgrade_and_chained_startup_apply_profile_once(
    tmp_path: Path, local_target: MagicMock, role: str, legacy: str | None, chained: bool
) -> None:
    if legacy is not None:
        source = "$HOME/.agentworks-profile.sh" if legacy == "variable" else str(tmp_path / ".agentworks-profile.sh")
        (tmp_path / ".profile").write_text(f". {source}\n")
    if chained:
        (tmp_path / ".bash_profile").write_text('. "$HOME/.profile"\n')
    (tmp_path / ".agentworks-rc.sh").write_text(":\n")
    binary_dir = str(tmp_path / ".local/bin")
    _write_profile(local_target, tmp_path, role, [binary_dir])
    previous = None
    for _ in range(2):
        _ensure_agentworks_files_sourced(local_target, home=str(tmp_path), shell="zsh", logger=MagicMock())
        state = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
        if previous is not None:
            assert state == previous
        previous = state
        for shell, command in (
            ("bash", 'printf "%s" "$PATH"'),
            ("/bin/sh", '. "$HOME/.profile"; . "$HOME/.profile"; printf "%s" "$PATH"'),
        ):
            result = subprocess.run(
                [shell, "-lc" if shell == "bash" else "-c", command],
                env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.split(":").count(binary_dir) == 1


@pytest.mark.parametrize("role", ["admin", "agent"])
def test_child_login_and_new_process_load_current_profile(tmp_path: Path, local_target: MagicMock, role: str) -> None:
    for directory in ("first", "second"):
        path = str(tmp_path / directory)
        _write_profile(local_target, tmp_path, role, [path])
        nested = "PATH=/usr/bin:/bin bash -lc " + shlex.quote('printf "%s" "$PATH"')
        result = subprocess.run(
            ["bash", "-lc", nested],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.split(":").count(path) == 1
