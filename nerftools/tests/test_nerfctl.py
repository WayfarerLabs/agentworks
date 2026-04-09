"""Tests for nerfctl shell scripts.

Each test runs the shell script as a subprocess and asserts on stdout, exit
code, and the resulting JSON state of the settings file.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_NERFCTL_DIR = Path(__file__).parent.parent / "nerftools" / "nerfctl" / "claude"
_GRANT = _NERFCTL_DIR / "grant-allow.sh"
_DENY = _NERFCTL_DIR / "grant-deny.sh"
_RESET = _NERFCTL_DIR / "grant-reset.sh"
_BY_THREAT = _NERFCTL_DIR / "grant-by-threat.sh"
_LIST = _NERFCTL_DIR / "grant-list.sh"
_INSTALL_PLUGIN = _NERFCTL_DIR / "install-plugin.sh"


def _ensure_jq() -> str | None:
    """Ensure jq is available. Returns the bin directory containing jq, or None."""
    check = subprocess.run(
        ["bash", "-c", "type -P jq && echo '{}' | jq ."],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        jq_path = check.stdout.strip().splitlines()[0]
        if "mise" not in jq_path:
            return str(Path(jq_path).parent)
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


def _run(
    script: Path,
    *args: str,
    home: Path | None = None,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
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


def _mock_plugin(tmp_path: Path, tools: list[str] | None = None) -> Path:
    """Create a mock plugin directory with tool scripts. Returns the plugin root."""
    plugin_root = tmp_path / "plugin"
    scripts_dir = plugin_root / "skills" / "nerf-test" / "scripts"
    scripts_dir.mkdir(parents=True)
    for tool in tools or ["nerf-test-tool"]:
        script = scripts_dir / tool
        script.write_text("#!/bin/bash\necho ok\n")
        script.chmod(0o755)
    return plugin_root


def _mock_plugin_with_threats(
    tmp_path: Path,
    tools: dict[str, tuple[str, str]],
) -> Path:
    """Create a mock plugin with threat metadata in scripts.

    tools: dict of tool_name -> (read_level, write_level)
    """
    plugin_root = tmp_path / "plugin"
    scripts_dir = plugin_root / "skills" / "nerf-test" / "scripts"
    scripts_dir.mkdir(parents=True)
    for tool_name, (read, write) in tools.items():
        script = scripts_dir / tool_name
        script.write_text(
            f"#!/bin/bash\n"
            f"# {tool_name} -- test tool\n"
            f"# nerf:threat:read={read}\n"
            f"# nerf:threat:write={write}\n"
            f"set -euo pipefail\n"
            f"echo ok\n"
        )
        script.chmod(0o755)
    return plugin_root


def _user_settings(tmp_path: Path, content: dict | None = None) -> Path:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    f = claude_dir / "settings.json"
    f.write_text(json.dumps(content or {}))
    return f


def _read(path: Path) -> dict:  # type: ignore[type-arg]
    return json.loads(path.read_text())  # type: ignore[no-any-return]


# -- grant --------------------------------------------------------------------


def test_grant_adds_to_allow(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    result = _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    entries = data["permissions"]["allow"]
    assert any("nerf-test-tool" in e for e in entries)
    assert any(str(plugin) in e for e in entries)


def test_grant_glob_matches_multiple(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-a", "nerf-test-b", "nerf-test-c"])
    result = _run(_GRANT, "nerf-test-*", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    assert len(data["permissions"]["allow"]) == 3


def test_grant_no_matches_fails(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    result = _run(_GRANT, "nerf-nonexistent-*", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode != 0
    assert "no tools matching" in result.stderr


def test_grant_does_not_duplicate(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert len(data["permissions"]["allow"]) == 1


def test_grant_removes_from_deny(tmp_path: Path) -> None:
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    script_path = str(plugin / "skills" / "nerf-test" / "scripts" / "nerf-test-tool")
    # Seed with stale (no :*) deny entry
    _user_settings(tmp_path, {"permissions": {"allow": [], "deny": [f"Bash({script_path})"]}})
    _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    # New :* entry in allow, stale entry removed from deny
    assert f"Bash({script_path}:*)" in data["permissions"]["allow"]
    assert f"Bash({script_path})" not in data["permissions"]["allow"]
    assert f"Bash({script_path})" not in data["permissions"]["deny"]
    assert f"Bash({script_path}:*)" not in data["permissions"]["deny"]


def test_grant_requires_args(tmp_path: Path) -> None:
    result = _run(_GRANT, home=tmp_path)
    assert result.returncode != 0
    assert "required" in result.stderr


def test_grant_local_scope(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir(parents=True)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    result = _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), "--scope", "local", cwd=tmp_path)
    assert result.returncode == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()


# -- deny ---------------------------------------------------------------------


def test_deny_adds_to_deny(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    result = _run(_DENY, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    assert any("nerf-test-tool" in e for e in data["permissions"]["deny"])


def test_deny_removes_from_allow(tmp_path: Path) -> None:
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    script_path = str(plugin / "skills" / "nerf-test" / "scripts" / "nerf-test-tool")
    # Seed with stale (no :*) allow entry
    _user_settings(tmp_path, {"permissions": {"allow": [f"Bash({script_path})"], "deny": []}})
    _run(_DENY, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    # Stale entry removed from allow, new :* entry in deny
    assert f"Bash({script_path})" not in data["permissions"]["allow"]
    assert f"Bash({script_path}:*)" not in data["permissions"]["allow"]
    assert f"Bash({script_path}:*)" in data["permissions"]["deny"]


def test_deny_requires_args(tmp_path: Path) -> None:
    result = _run(_DENY, home=tmp_path)
    assert result.returncode != 0
    assert "required" in result.stderr


# -- reset --------------------------------------------------------------------


def test_reset_removes_entries(tmp_path: Path) -> None:
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    script_path = str(plugin / "skills" / "nerf-test" / "scripts" / "nerf-test-tool")
    _user_settings(
        tmp_path,
        {
            "permissions": {
                "allow": [f"Bash({script_path})", "Bash(other-tool)"],
                "deny": [f"Bash({script_path})"],
            }
        },
    )
    result = _run(_RESET, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    assert f"Bash({script_path})" not in data["permissions"]["allow"]
    assert f"Bash({script_path})" not in data["permissions"]["deny"]
    assert "Bash(other-tool)" in data["permissions"]["allow"]


def test_reset_noop_when_file_missing(tmp_path: Path) -> None:
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    result = _run(_RESET, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode == 0


def test_reset_requires_args(tmp_path: Path) -> None:
    result = _run(_RESET, home=tmp_path)
    assert result.returncode != 0
    assert "required" in result.stderr


# -- list ---------------------------------------------------------------------


def test_list_shows_nerf_entries(tmp_path: Path) -> None:
    _user_settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Bash(/some/path/nerf-git-commit)", "Bash(unrelated)"],
                "deny": [],
            }
        },
    )
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "nerf-git-commit" in result.stdout
    assert "unrelated" not in result.stdout


def test_list_no_settings_file(tmp_path: Path) -> None:
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "No nerf tool permissions" in result.stdout


def test_list_no_nerf_entries(tmp_path: Path) -> None:
    _user_settings(tmp_path, {"permissions": {"allow": ["Bash(unrelated)"], "deny": []}})
    result = _run(_LIST, home=tmp_path)
    assert result.returncode == 0
    assert "No nerf tool permissions" in result.stdout


# -- round-trips ---------------------------------------------------------------


def test_grant_then_deny_moves_entry(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    _run(_DENY, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert not any("nerf-test-tool" in e for e in data["permissions"]["allow"])
    assert any("nerf-test-tool" in e for e in data["permissions"]["deny"])


def test_grant_then_reset_clears_entry(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin(tmp_path, ["nerf-test-tool"])
    _run(_GRANT, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    _run(_RESET, "nerf-test-tool", "--plugin-root", str(plugin), home=tmp_path)
    data = _read(tmp_path / ".claude" / "settings.json")
    assert not any("nerf-test-tool" in e for e in data["permissions"]["allow"])
    assert not any("nerf-test-tool" in e for e in data["permissions"]["deny"])


# -- install-plugin (pre-flight checks only) -----------------------------------


def test_install_plugin_requires_marketplace() -> None:
    result = subprocess.run(
        ["bash", "--norc", "--noprofile", str(_INSTALL_PLUGIN)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "marketplace" in result.stderr


# -- help flags ---------------------------------------------------------------


# -- grant-by-threat -----------------------------------------------------------


def test_by_threat_allows_inside_denies_outside(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin_with_threats(tmp_path, {
        "nerf-git-log": ("workspace", "none"),
        "nerf-git-add": ("workspace", "workspace"),
        "nerf-git-push": ("workspace", "remote"),
    })
    result = _run(
        _BY_THREAT,
        "--read", "workspace", "--write", "workspace",
        "--plugin-root", str(plugin),
        home=tmp_path,
    )
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    allow_str = " ".join(data["permissions"]["allow"])
    deny_str = " ".join(data["permissions"]["deny"])
    assert "nerf-git-log" in allow_str
    assert "nerf-git-add" in allow_str
    assert "nerf-git-push" in deny_str


def test_by_threat_outside_reset(tmp_path: Path) -> None:
    plugin = _mock_plugin_with_threats(tmp_path, {
        "nerf-git-log": ("workspace", "none"),
        "nerf-git-push": ("workspace", "remote"),
    })
    # Pre-seed push as allowed
    script_path = str(plugin / "skills" / "nerf-test" / "scripts" / "nerf-git-push")
    _user_settings(tmp_path, {"permissions": {"allow": [f"Bash({script_path})"], "deny": []}})
    result = _run(
        _BY_THREAT,
        "--read", "workspace", "--write", "workspace",
        "--outside", "reset",
        "--plugin-root", str(plugin),
        home=tmp_path,
    )
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    # Push should be removed from allow (reset), not in deny
    deny_str = " ".join(data["permissions"].get("deny", []))
    allow_str = " ".join(data["permissions"].get("allow", []))
    assert "nerf-git-push" not in deny_str
    assert "nerf-git-log" in allow_str


def test_by_threat_filter(tmp_path: Path) -> None:
    _user_settings(tmp_path)
    plugin = _mock_plugin_with_threats(tmp_path, {
        "nerf-git-log": ("workspace", "none"),
        "nerf-az-list": ("remote", "none"),
    })
    result = _run(
        _BY_THREAT,
        "--read", "workspace", "--write", "workspace",
        "--filter", "nerf-git-*",
        "--plugin-root", str(plugin),
        home=tmp_path,
    )
    assert result.returncode == 0
    data = _read(tmp_path / ".claude" / "settings.json")
    # Only git tool affected
    all_entries = data["permissions"].get("allow", []) + data["permissions"].get("deny", [])
    assert any("nerf-git-log" in e for e in all_entries)
    assert not any("nerf-az-list" in e for e in all_entries)


def test_by_threat_requires_read_write(tmp_path: Path) -> None:
    plugin = _mock_plugin_with_threats(tmp_path, {"nerf-t": ("none", "none")})
    result = _run(_BY_THREAT, "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode != 0
    assert "--read is required" in result.stderr


def test_by_threat_invalid_level(tmp_path: Path) -> None:
    plugin = _mock_plugin_with_threats(tmp_path, {"nerf-t": ("none", "none")})
    result = _run(_BY_THREAT, "--read", "galaxy", "--write", "none", "--plugin-root", str(plugin), home=tmp_path)
    assert result.returncode != 0
    assert "invalid read level" in result.stderr


def test_by_threat_annotations(tmp_path: Path) -> None:
    plugin = _mock_plugin_with_threats(tmp_path, {
        "nerf-git-push": ("workspace", "remote"),
    })
    script_path = str(plugin / "skills" / "nerf-test" / "scripts" / "nerf-git-push")
    _user_settings(tmp_path, {"permissions": {"allow": [f"Bash({script_path})"], "deny": []}})
    result = _run(
        _BY_THREAT,
        "--read", "workspace", "--write", "workspace",
        "--plugin-root", str(plugin),
        home=tmp_path,
    )
    assert result.returncode == 0
    assert "(was: allowed)" in result.stdout


# -- help flags ---------------------------------------------------------------


@pytest.mark.parametrize("script", [_GRANT, _DENY, _RESET, _BY_THREAT, _LIST, _INSTALL_PLUGIN])
def test_help_flag_exits_nonzero_with_usage(script: Path) -> None:
    result = _run(script, "--help")
    assert result.returncode != 0
    assert "Usage:" in result.stderr
