"""Tests for nerfctl-claude-* shell scripts.

Each test runs the shell script as a subprocess and asserts on stdout, exit
code, and the resulting JSON state of the settings file. Tests use --scope user
with HOME overridden to a temp directory so scripts write to a controlled path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_NERFCTL_DIR = Path(__file__).parent.parent / "nerftools" / "nerfctl" / "claude"


def _ensure_jq() -> str | None:
    """Ensure jq is available. Returns the bin directory containing jq, or None.

    If jq is a mise-managed tool, installs it if needed and returns the real
    binary path (not the shim) so tests can override HOME without breaking mise.
    """
    # Check if jq works directly (system install)
    check = subprocess.run(
        ["bash", "-c", "type -P jq && echo '{}' | jq ."],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        jq_path = check.stdout.strip().splitlines()[0]
        # If it's a mise shim, resolve the real path
        if "mise" not in jq_path:
            return str(Path(jq_path).parent)

    # Try mise
    mise_check = subprocess.run(["bash", "-c", "command -v mise"], capture_output=True)
    if mise_check.returncode != 0:
        return None
    subprocess.run(["mise", "install", "jq"], capture_output=True)
    where = subprocess.run(["mise", "where", "jq"], capture_output=True, text=True)
    if where.returncode != 0:
        return None
    return where.stdout.strip() + "/bin"


_jq_bin_dir = _ensure_jq()

pytestmark = pytest.mark.skipif(_jq_bin_dir is None, reason="jq not available")
_GRANT = _NERFCTL_DIR / "grant.sh"
_DENY = _NERFCTL_DIR / "deny.sh"
_RESET = _NERFCTL_DIR / "reset.sh"
_LIST = _NERFCTL_DIR / "list.sh"
_INSTALL_PLUGIN = _NERFCTL_DIR / "install-plugin.sh"


def _run(
    script: Path,
    *args: str,
    home: Path | None = None,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    # When HOME is overridden, mise shims break. Strip mise shims from PATH
    # and prepend the real jq binary directory resolved at setup time.
    if home is not None and "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if "mise/shims" not in p)
        if _jq_bin_dir:
            env["PATH"] = _jq_bin_dir + os.pathsep + env["PATH"]
    if home is not None:
        env["HOME"] = str(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "--norc", "--noprofile", str(script), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def _user_settings(tmp_path: Path, content: dict | None = None) -> Path:
    """Create a user settings file at the expected location and return its path."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    f = claude_dir / "settings.json"
    f.write_text(json.dumps(content or {}))
    return f


def _local_settings(tmp_path: Path, content: dict | None = None) -> Path:
    """Create a local settings file at the expected location and return its path."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    f = claude_dir / "settings.local.json"
    f.write_text(json.dumps(content or {}))
    return f


def _read(path: Path) -> dict:  # type: ignore[type-arg]
    return json.loads(path.read_text())  # type: ignore[no-any-return]


# -- grant --------------------------------------------------------------------


def test_grant_adds_to_allow(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    result = _run(_GRANT, "nerf-git-push-origin", home=tmp_path)
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-push-origin)" in data["permissions"]["allow"]
    assert "Bash($AGENTWORKS_NERF_BIN/nerf-git-push-origin)" in data["permissions"]["allow"]


def test_grant_does_not_duplicate(tmp_path: Path) -> None:
    _user_settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(nerf-git-push-origin)", "Bash($AGENTWORKS_NERF_BIN/nerf-git-push-origin)"],
                "deny": [],
            }
        },
    )
    _run(_GRANT, "nerf-git-push-origin", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    # Should not duplicate
    assert data["permissions"]["allow"].count("Bash(nerf-git-push-origin)") == 1


def test_grant_removes_from_deny(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": [], "deny": ["Bash(nerf-git-push-origin)"]}})
    _run(_GRANT, "nerf-git-push-origin", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-push-origin)" in data["permissions"]["allow"]
    assert "Bash(nerf-git-push-origin)" not in data["permissions"]["deny"]


def test_grant_creates_settings_file_if_missing(tmp_path: Path) -> None:
    result = _run(_GRANT, "nerf-git-log", home=tmp_path)
    assert result.returncode == 0
    s = tmp_path / ".claude" / "settings.json"
    assert s.exists()
    assert "Bash(nerf-git-log)" in _read(s)["permissions"]["allow"]


def test_grant_requires_tool_arg(tmp_path: Path) -> None:
    result = _run(_GRANT, home=tmp_path)
    assert result.returncode != 0
    assert "required" in result.stderr


def test_grant_local_scope(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    result = _run(_GRANT, "nerf-git-log", "--scope", "local", cwd=tmp_path)
    assert result.returncode == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()


def test_grant_local_scope_no_claude_dir(tmp_path: Path) -> None:
    result = _run(_GRANT, "nerf-git-log", "--scope", "local", cwd=tmp_path)
    assert result.returncode != 0
    assert ".claude" in result.stderr


# -- deny ---------------------------------------------------------------------


def test_deny_adds_to_deny(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    result = _run(_DENY, "nerf-git-push-origin", home=tmp_path)
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-push-origin)" in data["permissions"]["deny"]
    assert "Bash($AGENTWORKS_NERF_BIN/nerf-git-push-origin)" in data["permissions"]["deny"]


def test_deny_does_not_duplicate(tmp_path: Path) -> None:
    _user_settings(
        tmp_path,
        {
            "permissions": {
                "allow": [],
                "deny": ["Bash(nerf-git-push-origin)", "Bash($AGENTWORKS_NERF_BIN/nerf-git-push-origin)"],
            }
        },
    )
    _run(_DENY, "nerf-git-push-origin", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert data["permissions"]["deny"].count("Bash(nerf-git-push-origin)") == 1


def test_deny_removes_from_allow(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash(nerf-git-push-origin)"], "deny": []}})
    _run(_DENY, "nerf-git-push-origin", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-push-origin)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-push-origin)" in data["permissions"]["deny"]


def test_deny_requires_tool_arg(tmp_path: Path) -> None:
    result = _run(_DENY, home=tmp_path)
    assert result.returncode != 0
    assert "required" in result.stderr


# -- reset --------------------------------------------------------------------


def test_reset_removes_from_allow(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash(nerf-git-log)"], "deny": []}})
    result = _run(_RESET, "nerf-git-log", home=tmp_path)
    assert result.returncode == 0
    assert _read(tmp_path / ".claude" / "settings.json")["permissions"]["allow"] == []


def test_reset_removes_from_deny(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": [], "deny": ["Bash(nerf-git-log)"]}})
    _run(_RESET, "nerf-git-log", home=tmp_path)
    assert _read(tmp_path / ".claude" / "settings.json")["permissions"]["deny"] == []


def test_reset_removes_both_entries(tmp_path: Path) -> None:
    _user_settings(
        tmp_path,
        {
            "permissions": {
                "allow": [
                    "Bash(nerf-git-log)",
                    "Bash($AGENTWORKS_NERF_BIN/nerf-git-log)",
                    "Bash(nerf-git-fetch)",
                ],
                "deny": ["Bash(nerf-git-log)", "Bash($AGENTWORKS_NERF_BIN/nerf-git-log)"],
            }
        },
    )
    _run(_RESET, "nerf-git-log", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash($AGENTWORKS_NERF_BIN/nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-fetch)" in data["permissions"]["allow"]
    assert data["permissions"]["deny"] == []


def test_reset_noop_when_file_missing(tmp_path: Path) -> None:
    result = _run(_RESET, "nerf-git-log", home=tmp_path)
    assert result.returncode == 0


def test_reset_requires_tool_arg(tmp_path: Path) -> None:
    result = _run(_RESET, home=tmp_path)
    assert result.returncode != 0
    assert "required" in result.stderr


# -- list ---------------------------------------------------------------------


def test_list_shows_allowed_nerf_entries(tmp_path: Path) -> None:
    _user_settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(nerf-git-log)", "Bash(some-other-tool)"],
                "deny": [],
            }
        },
    )
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "Bash(nerf-git-log)" in result.stdout
    assert "some-other-tool" not in result.stdout


def test_list_shows_denied_nerf_entries(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": [], "deny": ["Bash(nerf-git-push-origin)"]}})
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "Bash(nerf-git-push-origin)" in result.stdout
    assert "Denied" in result.stdout


def test_list_includes_nerfctl_entries(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash(nerfctl-claude-grant)"], "deny": []}})
    result = _run(_LIST, home=tmp_path)
    assert "Bash(nerfctl-claude-grant)" in result.stdout


def test_list_includes_abs_path_entries(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash($AGENTWORKS_NERF_BIN/nerf-git-log)"], "deny": []}})
    result = _run(_LIST, home=tmp_path)
    assert "AGENTWORKS_NERF_BIN" in result.stdout


def test_list_filters_out_non_nerf_entries(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash(nerf-git-log)", "Bash(unrelated-tool)"], "deny": []}})
    result = _run(_LIST, home=tmp_path)
    assert "unrelated-tool" not in result.stdout


def test_list_no_settings_file(tmp_path: Path) -> None:
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "No settings file" in result.stdout


def test_list_no_nerf_entries(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash(unrelated)"], "deny": []}})
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "No nerf tool permissions" in result.stdout


# -- grant/deny round-trips ---------------------------------------------------


def test_grant_then_deny_moves_entry(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    _run(_GRANT, "nerf-git-log", home=tmp_path)
    _run(_DENY, "nerf-git-log", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" in data["permissions"]["deny"]


def test_deny_then_grant_moves_entry(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    _run(_DENY, "nerf-git-log", home=tmp_path)
    _run(_GRANT, "nerf-git-log", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-log)" in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" not in data["permissions"]["deny"]


def test_grant_then_reset_clears_entry(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    _run(_GRANT, "nerf-git-log", home=tmp_path)
    _run(_RESET, "nerf-git-log", home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert "Bash(nerf-git-log)" not in data["permissions"]["allow"]
    assert "Bash(nerf-git-log)" not in data["permissions"]["deny"]


# -- install-plugin (pre-flight checks only; actual install needs claude CLI) -


def test_install_plugin_requires_env_var(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "AGENTWORKS_NERF_HOME"}
    result = subprocess.run(
        ["bash", "--norc", "--noprofile", str(_INSTALL_PLUGIN)],
        capture_output=True,
        text=True,
        env={**env, "HOME": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "AGENTWORKS_NERF_HOME" in result.stderr


def test_install_plugin_requires_marketplace(tmp_path: Path) -> None:
    nerf_home = tmp_path / "nerf"
    nerf_home.mkdir()
    result = _run(
        _INSTALL_PLUGIN,
        home=tmp_path,
        env_extra={"AGENTWORKS_NERF_HOME": str(nerf_home)},
    )
    assert result.returncode != 0
    assert "marketplace" in result.stderr


# -- help flags ---------------------------------------------------------------


@pytest.mark.parametrize("script", [_GRANT, _DENY, _RESET, _LIST, _INSTALL_PLUGIN])
def test_help_flag_exits_nonzero_with_usage(script: Path) -> None:
    result = _run(script, "--help")
    assert result.returncode != 0
    assert "Usage:" in result.stderr
