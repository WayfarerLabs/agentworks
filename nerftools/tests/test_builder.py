"""Tests for shell script generation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nerftools.builder import build_script_text, build_scripts
from nerftools.manifest import NerfManifest, PackageMeta, ParamSpec, ToolSpec


def _tool(
    command: list[str],
    params: dict[str, ParamSpec] | None = None,
    env: dict[str, str] | None = None,
    description: str = "A test tool",
    example: str | None = None,
) -> ToolSpec:
    return ToolSpec(
        description=description,
        command=tuple(command),
        params=params or {},
        env=env or {},
        example=example,
    )


def _flag(
    flag: str,
    description: str = "A param",
    *,
    required: bool = False,
    pattern: str | None = None,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
    default: str | None = None,
) -> ParamSpec:
    return ParamSpec(
        flag=flag, description=description, required=required, pattern=pattern, allow=allow, deny=deny, default=default
    )


def _positional(description: str = "A param", *, required: bool = False) -> ParamSpec:
    return ParamSpec(positional=True, description=description, required=required)


# -- Script structure ----------------------------------------------------------


def test_simple_tool_has_shebang() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    assert script.startswith("#!/usr/bin/env bash\n")


def test_simple_tool_has_set_pipefail() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    assert "set -euo pipefail" in script


def test_simple_tool_exec_line() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    assert "exec echo hello" in script


def test_simple_tool_no_argument_parsing() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    assert "while [[ $#" not in script
    assert 'case "$1"' not in script


def test_generated_header_comment() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    assert "# my-tool -- A test tool" in script
    assert "# Generated from my-pkg manifest." in script


# -- Flag params ---------------------------------------------------------------


def test_flag_param_in_case_statement() -> None:
    params = {"remote": _flag("--remote", required=True)}
    script = build_script_text("t", "p", _tool(["git", "push", "{remote}"], params))
    assert "--remote) REMOTE=" in script


def test_flag_param_exec_substitution() -> None:
    params = {"remote": _flag("--remote", required=True)}
    script = build_script_text("t", "p", _tool(["git", "push", "{remote}", "HEAD"], params))
    assert 'exec git push "${REMOTE}" HEAD' in script


def test_required_flag_validation() -> None:
    params = {"remote": _flag("--remote", required=True)}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], params))
    assert '-z "${REMOTE}"' in script
    assert "--remote is required" in script


def test_optional_flag_no_required_check() -> None:
    params = {"remote": _flag("--remote", required=False)}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], params))
    assert "--remote is required" not in script


def test_pattern_validation() -> None:
    params = {"remote": _flag("--remote", pattern="^[a-z]+$")}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], params))
    assert "^[a-z]+$" in script
    assert "must match" in script


def test_deny_validation() -> None:
    params = {"remote": _flag("--remote", deny=("origin", "main"))}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], params))
    assert '"${REMOTE}" == "origin"' in script
    assert '"${REMOTE}" == "main"' in script


def test_allow_validation() -> None:
    params = {"env": _flag("--env", allow=("prod", "staging"))}
    script = build_script_text("t", "p", _tool(["echo", "{env}"], params))
    assert '"${ENV}" != "prod"' in script
    assert '"${ENV}" != "staging"' in script
    assert "must be one of" in script


def test_default_value_in_var_declaration() -> None:
    params = {"branch": _flag("--branch", default="main")}
    script = build_script_text("t", "p", _tool(["echo", "{branch}"], params))
    assert 'BRANCH="main"' in script


# -- Positional params ---------------------------------------------------------


def test_positional_param_collected() -> None:
    params = {"remote": _positional(required=True)}
    script = build_script_text("t", "p", _tool(["git", "fetch", "{remote}"], params))
    assert 'REMOTE="${1:-}"' in script


def test_positional_exec_substitution() -> None:
    params = {"remote": _positional(required=True)}
    script = build_script_text("t", "p", _tool(["git", "fetch", "{remote}"], params))
    assert 'exec git fetch "${REMOTE}"' in script


# -- Env vars ------------------------------------------------------------------


def test_env_exports_before_exec() -> None:
    script = build_script_text(
        "t",
        "p",
        _tool(
            ["az", "account", "show"],
            env={"AZURE_CONFIG_DIR": "/home/user/.azure"},
        ),
    )
    lines = script.splitlines()
    export_idx = next(i for i, line in enumerate(lines) if "AZURE_CONFIG_DIR" in line)
    exec_idx = next(i for i, line in enumerate(lines) if line.startswith("exec "))
    assert export_idx < exec_idx
    assert 'export AZURE_CONFIG_DIR="/home/user/.azure"' in script


# -- Usage / help --------------------------------------------------------------


def test_usage_contains_tool_name() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hi"]))
    assert "Usage: my-tool" in script


def test_usage_contains_flag() -> None:
    params = {"remote": _flag("--remote", required=True)}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], params))
    assert "--remote <remote>" in script


def test_usage_contains_example() -> None:
    script = build_script_text(
        "my-tool",
        "my-pkg",
        _tool(
            ["echo", "hi"],
            example="my-tool --remote upstream",
        ),
    )
    assert "Example: my-tool --remote upstream" in script


def test_usage_contains_description() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hi"], description="Does the thing"))
    assert "Does the thing." in script


# -- Bash syntax validation ----------------------------------------------------


def test_generated_script_is_valid_bash() -> None:
    """bash -n syntax-checks the generated script."""
    params = {
        "remote": _flag("--remote", required=True, pattern="^[a-z]+$", deny=("origin",)),
    }
    script = build_script_text("my-tool", "my-pkg", _tool(["git", "push", "{remote}", "HEAD"], params))
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_simple_tool_bash_syntax() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


# -- keep_existing / clean behavior -------------------------------------------


def _simple_manifest(name: str = "test-pkg") -> NerfManifest:
    return NerfManifest(
        package=PackageMeta(name=name, description="Test", skill_group=name, skill_intro=None),
        tools={"my-tool": _tool(["echo", "hello"])},
    )


def test_build_scripts_clears_stale_files_by_default(tmp_path: Path) -> None:
    stale = tmp_path / "stale-tool"
    stale.write_text("old")
    build_scripts([_simple_manifest()], tmp_path)
    assert not stale.exists()


def test_build_scripts_keep_existing_preserves_unmanaged_files(tmp_path: Path) -> None:
    extra = tmp_path / "custom-tool"
    extra.write_text("custom")
    build_scripts([_simple_manifest()], tmp_path, keep_existing=True)
    assert extra.exists()


def test_build_scripts_always_writes_generated_files(tmp_path: Path) -> None:
    build_scripts([_simple_manifest()], tmp_path)
    assert (tmp_path / "my-tool").exists()


@pytest.mark.parametrize("keep", [True, False])
def test_build_scripts_overwrites_existing_generated_file(tmp_path: Path, keep: bool) -> None:
    (tmp_path / "my-tool").write_text("old content")
    build_scripts([_simple_manifest()], tmp_path, keep_existing=keep)
    assert "old content" not in (tmp_path / "my-tool").read_text()
