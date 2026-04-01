"""Tests for nerfctl-claude-* shell scripts.

Each test runs the shell script as a subprocess and asserts on stdout, exit
code, and the resulting JSON state of the settings file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_NERFCTL_DIR = Path(__file__).parent.parent / "nerftools" / "nerfctl" / "claude"
_GRANT = _NERFCTL_DIR / "grant.sh"
_DENY = _NERFCTL_DIR / "deny.sh"
_RESET = _NERFCTL_DIR / "reset.sh"
_LIST = _NERFCTL_DIR / "list.sh"


def _run(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _settings(tmp_path: Path, content: dict | None = None) -> Path:
    """Create a settings file and return its path."""
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(content or {}))
    return f


def _read(path: Path) -> dict:  # type: ignore[type-arg]
    return json.loads(path.read_text())  # type: ignore[no-any-return]


# -- grant --------------------------------------------------------------------


def test_grant_adds_to_allow(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    result = _run(_GRANT, "nerf-git-push-origin", "--settings", str(s))
    assert result.returncode == 0
    assert _read(s)["permissions"]["allow"] == ["Bash(nerf-git-push-origin)"]


def test_grant_does_not_duplicate(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": ["Bash(nerf-git-push-origin)"], "deny": []}})
    _run(_GRANT, "nerf-git-push-origin", "--settings", str(s))
    assert _read(s)["permissions"]["allow"] == ["Bash(nerf-git-push-origin)"]


def test_grant_removes_from_deny(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": [], "deny": ["Bash(nerf-git-push-origin)"]}})
    _run(_GRANT, "nerf-git-push-origin", "--settings", str(s))
    data = _read(s)
    assert "Bash(nerf-git-push-origin)" in data["permissions"]["allow"]
    assert "Bash(nerf-git-push-origin)" not in data["permissions"]["deny"]


def test_grant_creates_settings_file_if_missing(tmp_path: Path) -> None:
    s = tmp_path / "new-settings.json"
    result = _run(_GRANT, "nerf-git-log", "--settings", str(s))
    assert result.returncode == 0
    assert s.exists()
    assert "Bash(nerf-git-log)" in _read(s)["permissions"]["allow"]


def test_grant_requires_tool_arg(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    result = _run(_GRANT, "--settings", str(s))
    assert result.returncode != 0
    assert "required" in result.stderr


def test_grant_no_claude_dir_without_settings_flag(tmp_path: Path) -> None:
    result = _run(_GRANT, "nerf-git-log", cwd=tmp_path)
    assert result.returncode != 0
    assert ".claude" in result.stderr


def test_grant_uses_claude_dir_by_default(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    result = _run(_GRANT, "nerf-git-log", cwd=tmp_path)
    assert result.returncode == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()


# -- deny ---------------------------------------------------------------------


def test_deny_adds_to_deny(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    result = _run(_DENY, "nerf-git-push-origin", "--settings", str(s))
    assert result.returncode == 0
    assert _read(s)["permissions"]["deny"] == ["Bash(nerf-git-push-origin)"]


def test_deny_does_not_duplicate(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": [], "deny": ["Bash(nerf-git-push-origin)"]}})
    _run(_DENY, "nerf-git-push-origin", "--settings", str(s))
    assert _read(s)["permissions"]["deny"] == ["Bash(nerf-git-push-origin)"]


def test_deny_removes_from_allow(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": ["Bash(nerf-git-push-origin)"], "deny": []}})
    _run(_DENY, "nerf-git-push-origin", "--settings", str(s))
    data = _read(s)
    assert "Bash(nerf-git-push-origin)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-push-origin)" in data["permissions"]["deny"]


def test_deny_requires_tool_arg(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    result = _run(_DENY, "--settings", str(s))
    assert result.returncode != 0
    assert "required" in result.stderr


# -- reset --------------------------------------------------------------------


def test_reset_removes_from_allow(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": ["Bash(nerf-git-log)"], "deny": []}})
    result = _run(_RESET, "nerf-git-log", "--settings", str(s))
    assert result.returncode == 0
    assert _read(s)["permissions"]["allow"] == []


def test_reset_removes_from_deny(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": [], "deny": ["Bash(nerf-git-log)"]}})
    _run(_RESET, "nerf-git-log", "--settings", str(s))
    assert _read(s)["permissions"]["deny"] == []


def test_reset_removes_from_both(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(nerf-git-log)", "Bash(nerf-git-fetch)"],
                "deny": ["Bash(nerf-git-log)"],
            }
        },
    )
    _run(_RESET, "nerf-git-log", "--settings", str(s))
    data = _read(s)
    assert "Bash(nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" not in data["permissions"]["deny"]
    assert "Bash(nerf-git-fetch)" in data["permissions"]["allow"]


def test_reset_noop_when_file_missing(tmp_path: Path) -> None:
    s = tmp_path / "missing.json"
    result = _run(_RESET, "nerf-git-log", "--settings", str(s))
    assert result.returncode == 0
    assert not s.exists()


def test_reset_requires_tool_arg(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    result = _run(_RESET, "--settings", str(s))
    assert result.returncode != 0
    assert "required" in result.stderr


# -- list ---------------------------------------------------------------------


def test_list_shows_allowed_nerf_entries(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(nerf-git-log)", "Bash(some-other-tool)"],
                "deny": [],
            }
        },
    )
    result = _run(_LIST, "--settings", str(s))
    assert result.returncode == 0
    assert "Bash(nerf-git-log)" in result.stdout
    assert "some-other-tool" not in result.stdout


def test_list_shows_denied_nerf_entries(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        {
            "permissions": {
                "allow": [],
                "deny": ["Bash(nerf-git-push-origin)"],
            }
        },
    )
    result = _run(_LIST, "--settings", str(s))
    assert result.returncode == 0
    assert "Bash(nerf-git-push-origin)" in result.stdout
    assert "Denied" in result.stdout


def test_list_includes_nerfctl_entries(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(nerfctl-claude-grant)"],
                "deny": [],
            }
        },
    )
    result = _run(_LIST, "--settings", str(s))
    assert "Bash(nerfctl-claude-grant)" in result.stdout


def test_list_filters_out_non_nerf_entries(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(nerf-git-log)", "Bash(unrelated-tool)"],
                "deny": [],
            }
        },
    )
    result = _run(_LIST, "--settings", str(s))
    assert "unrelated-tool" not in result.stdout


def test_list_no_settings_file(tmp_path: Path) -> None:
    s = tmp_path / "missing.json"
    result = _run(_LIST, "--settings", str(s))
    assert result.returncode == 0
    assert "No settings file" in result.stdout


def test_list_no_nerf_entries(tmp_path: Path) -> None:
    s = _settings(tmp_path, {"permissions": {"allow": ["Bash(unrelated)"], "deny": []}})
    result = _run(_LIST, "--settings", str(s))
    assert result.returncode == 0
    assert "No nerf tool permissions" in result.stdout


# -- grant/deny round-trips ---------------------------------------------------


def test_grant_then_deny_moves_entry(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _run(_GRANT, "nerf-git-log", "--settings", str(s))
    _run(_DENY, "nerf-git-log", "--settings", str(s))
    data = _read(s)
    assert "Bash(nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" in data["permissions"]["deny"]


def test_deny_then_grant_moves_entry(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _run(_DENY, "nerf-git-log", "--settings", str(s))
    _run(_GRANT, "nerf-git-log", "--settings", str(s))
    data = _read(s)
    assert "Bash(nerf-git-log)" in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" not in data["permissions"]["deny"]


def test_grant_then_reset_clears_entry(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _run(_GRANT, "nerf-git-log", "--settings", str(s))
    _run(_RESET, "nerf-git-log", "--settings", str(s))
    data = _read(s)
    assert "Bash(nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" not in data["permissions"]["deny"]


@pytest.mark.parametrize("script", [_GRANT, _DENY, _RESET, _LIST])
def test_help_flag_exits_nonzero_with_usage(script: Path) -> None:
    result = _run(script, "--help")
    assert result.returncode != 0
    assert "Usage:" in result.stderr
