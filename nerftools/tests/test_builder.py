"""Tests for shell script generation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nerftools.builder import build_script_text, build_scripts
from nerftools.manifest import ArgSpec, FlagSpec, GuardSpec, NerfManifest, PackageMeta, ToolSpec


def _flag(
    flag: str,
    description: str = "A param",
    *,
    optional: bool = False,
    pattern: str | None = None,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
) -> FlagSpec:
    return FlagSpec(flag=flag, description=description, optional=optional, pattern=pattern, allow=allow, deny=deny)


def _arg(
    description: str = "A param",
    *,
    required: bool = False,
    variadic: bool = False,
    pattern: str | None = None,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
) -> ArgSpec:
    return ArgSpec(description=description, required=required, variadic=variadic, pattern=pattern, allow=allow, deny=deny)


def _tool(
    command: list[str],
    flags: dict[str, FlagSpec] | None = None,
    args: dict[str, ArgSpec] | None = None,
    env: dict[str, str] | None = None,
    description: str = "A test tool",
    guards: tuple[GuardSpec, ...] = (),
) -> ToolSpec:
    return ToolSpec(
        description=description,
        command=tuple(command),
        flags=flags or {},
        args=args or {},
        env=env or {},
        guards=guards,
    )


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


def test_flag_in_case_statement() -> None:
    flags = {"remote": _flag("--remote")}
    script = build_script_text("t", "p", _tool(["git", "push", "{remote}"], flags=flags))
    assert "--remote) REMOTE=" in script


def test_flag_exec_substitution() -> None:
    flags = {"remote": _flag("--remote")}
    script = build_script_text("t", "p", _tool(["git", "push", "{remote}", "HEAD"], flags=flags))
    assert 'exec git push "${REMOTE}" HEAD' in script


def test_required_flag_validation() -> None:
    # Flags are required by default (optional=False)
    flags = {"remote": _flag("--remote")}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], flags=flags))
    assert '-z "${REMOTE}"' in script
    assert "--remote is required" in script


def test_optional_flag_no_required_check() -> None:
    flags = {"remote": _flag("--remote", optional=True)}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], flags=flags))
    assert "--remote is required" not in script


def test_optional_flag_uses_conditional_expansion() -> None:
    flags = {"remote": _flag("--remote", optional=True)}
    script = build_script_text("t", "p", _tool(["git", "fetch", "{remote}"], flags=flags))
    assert '${REMOTE:+"$REMOTE"}' in script


def test_pattern_validation() -> None:
    flags = {"remote": _flag("--remote", pattern="^[a-z]+$")}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], flags=flags))
    assert "^[a-z]+$" in script
    assert "must match" in script


def test_deny_validation() -> None:
    flags = {"remote": _flag("--remote", deny=("origin", "main"))}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], flags=flags))
    assert '"${REMOTE}" == "origin"' in script
    assert '"${REMOTE}" == "main"' in script


def test_allow_validation() -> None:
    flags = {"env": _flag("--env", allow=("prod", "staging"))}
    script = build_script_text("t", "p", _tool(["echo", "{env}"], flags=flags))
    assert '"${ENV}" != "prod"' in script
    assert '"${ENV}" != "staging"' in script
    assert "must be one of" in script


def test_flag_parser_break_when_positional_args_present() -> None:
    flags = {"verbose": _flag("--verbose", optional=True)}
    args = {"target": _arg(required=True)}
    script = build_script_text("t", "p", _tool(["cmd", "{verbose}", "{target}"], flags=flags, args=args))
    assert "*) break ;;" in script


def test_flag_parser_error_when_no_positional_args() -> None:
    flags = {"remote": _flag("--remote")}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], flags=flags))
    assert "unknown argument" in script
    assert "*) break ;;" not in script


# -- Positional args -----------------------------------------------------------


def test_positional_arg_collected() -> None:
    args = {"remote": _arg(required=True)}
    script = build_script_text("t", "p", _tool(["git", "fetch", "{remote}"], args=args))
    assert 'REMOTE="${1:-}"' in script


def test_positional_exec_substitution() -> None:
    args = {"remote": _arg(required=True)}
    script = build_script_text("t", "p", _tool(["git", "fetch", "{remote}"], args=args))
    assert 'exec git fetch "${REMOTE}"' in script


def test_required_arg_validation() -> None:
    args = {"target": _arg(required=True)}
    script = build_script_text("t", "p", _tool(["cmd", "{target}"], args=args))
    assert '-z "${TARGET}"' in script
    assert "<target> is required" in script


def test_optional_arg_no_required_check() -> None:
    args = {"target": _arg(required=False)}
    script = build_script_text("t", "p", _tool(["cmd", "{target}"], args=args))
    assert "<target> is required" not in script


def test_variadic_arg_collected() -> None:
    args = {"files": _arg(variadic=True)}
    script = build_script_text("t", "p", _tool(["git", "add", "{files}"], args=args))
    assert 'FILES=("$@")' in script


def test_variadic_arg_exec_substitution() -> None:
    args = {"files": _arg(required=True, variadic=True)}
    script = build_script_text("t", "p", _tool(["git", "add", "{files}"], args=args))
    assert '"${FILES[@]}"' in script


def test_optional_variadic_uses_conditional_expansion() -> None:
    args = {"files": _arg(variadic=True)}
    script = build_script_text("t", "p", _tool(["git", "add", "{files}"], args=args))
    assert '${FILES[@]+"${FILES[@]}"}' in script


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


# -- Guards --------------------------------------------------------------------


def test_guard_check_before_exec() -> None:
    guards = (GuardSpec(command=("git", "remote", "get-url", "{remote}"), fail_message="Remote not found"),)
    flags = {"remote": _flag("--remote")}
    script = build_script_text(
        "t", "p", _tool(["git", "push", "{remote}", "HEAD"], flags=flags, guards=guards)
    )
    lines = script.splitlines()
    guard_idx = next(i for i, line in enumerate(lines) if "get-url" in line)
    exec_idx = next(i for i, line in enumerate(lines) if line.startswith("exec "))
    assert guard_idx < exec_idx
    assert "Remote not found" in script


# -- Usage / help --------------------------------------------------------------


def test_usage_contains_tool_name() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hi"]))
    assert "Usage: my-tool" in script


def test_usage_contains_flag() -> None:
    flags = {"remote": _flag("--remote")}
    script = build_script_text("t", "p", _tool(["echo", "{remote}"], flags=flags))
    assert "--remote <remote>" in script


def test_usage_contains_description() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hi"], description="Does the thing"))
    assert "Does the thing." in script


# -- Bash syntax validation ----------------------------------------------------


def test_generated_script_is_valid_bash() -> None:
    """bash -n syntax-checks the generated script."""
    flags = {"remote": _flag("--remote", pattern="^[a-z]+$", deny=("origin",))}
    script = build_script_text("my-tool", "my-pkg", _tool(["git", "push", "{remote}", "HEAD"], flags=flags))
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_simple_tool_bash_syntax() -> None:
    script = build_script_text("my-tool", "my-pkg", _tool(["echo", "hello"]))
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_tool_with_flags_and_args_bash_syntax() -> None:
    flags = {"verbose": _flag("--verbose", optional=True)}
    args = {"target": _arg(required=True)}
    script = build_script_text(
        "my-tool", "my-pkg", _tool(["cmd", "{verbose}", "{target}"], flags=flags, args=args)
    )
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_variadic_tool_bash_syntax() -> None:
    args = {"files": _arg(required=True, variadic=True)}
    script = build_script_text("my-tool", "my-pkg", _tool(["git", "add", "{files}"], args=args))
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


# -- keep_existing / clean behavior -------------------------------------------


def _simple_manifest(name: str = "test-pkg") -> NerfManifest:
    return NerfManifest(
        package=PackageMeta(name=name, description="Test", skill_group=name, skill_intro=""),
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
