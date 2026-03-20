"""Tests for rulesync skill generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from nerftools.manifest import NerfManifest, PackageMeta, ParamSpec, ToolSpec
from nerftools.skill import build_skill_text, build_skills


def _manifest(
    name: str = "test-pkg",
    skill_group: str = "test-pkg",
    skill_intro: str | None = None,
    tools: dict[str, ToolSpec] | None = None,
) -> NerfManifest:
    return NerfManifest(
        package=PackageMeta(
            name=name,
            description="Test package",
            skill_group=skill_group,
            skill_intro=skill_intro,
        ),
        tools=tools or {},
    )


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


# -- Skill structure -----------------------------------------------------------


def test_skill_has_frontmatter() -> None:
    m = _manifest(skill_group="nerf-git", name="nerf-git")
    skill = build_skill_text(m)
    assert skill.startswith("---\n")
    assert "name: nerf-git" in skill
    assert 'targets: ["*"]' in skill


def test_skill_frontmatter_uses_package_description() -> None:
    m = _manifest(name="nerf-git", skill_group="nerf-git")
    skill = build_skill_text(m)
    assert 'description: "Test package"' in skill


def test_skill_has_h1_header() -> None:
    m = _manifest(skill_group="nerf-git")
    skill = build_skill_text(m)
    assert "# nerf-git\n" in skill


def test_skill_includes_intro() -> None:
    m = _manifest(skill_intro="Use these tools carefully.")
    skill = build_skill_text(m)
    assert "Use these tools carefully." in skill


def test_skill_no_intro_section_when_absent() -> None:
    m = _manifest(skill_intro=None, tools={"t": _tool(["echo"])})
    skill = build_skill_text(m)
    # Should have h1, then immediately tool h2
    lines = skill.splitlines()
    h1_idx = next(i for i, line in enumerate(lines) if line.startswith("# "))
    h2_idx = next(i for i, line in enumerate(lines) if line.startswith("## "))
    assert h2_idx > h1_idx


def test_tool_has_h2_section() -> None:
    m = _manifest(tools={"my-tool": _tool(["echo"])})
    skill = build_skill_text(m)
    assert "## my-tool" in skill


def test_tool_description_in_skill() -> None:
    m = _manifest(tools={"my-tool": _tool(["echo"], description="Does the thing")})
    skill = build_skill_text(m)
    assert "Does the thing." in skill


def test_no_args_tool_shows_no_arguments() -> None:
    m = _manifest(tools={"my-tool": _tool(["echo"])})
    skill = build_skill_text(m)
    assert "No arguments." in skill


def test_tool_separated_by_horizontal_rule() -> None:
    m = _manifest(tools={"my-tool": _tool(["echo"])})
    skill = build_skill_text(m)
    assert "---" in skill


# -- Usage line ----------------------------------------------------------------


def test_usage_line_simple_tool() -> None:
    m = _manifest(tools={"my-tool": _tool(["echo"])})
    skill = build_skill_text(m)
    assert "**Usage:** `my-tool`" in skill


def test_usage_line_required_flag() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{remote}"], {"remote": _flag("--remote", required=True)})})
    skill = build_skill_text(m)
    assert "--remote <remote>" in skill


def test_usage_line_optional_flag_bracketed() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{branch}"], {"branch": _flag("--branch")})})
    skill = build_skill_text(m)
    assert "[--branch <branch>]" in skill


def test_usage_line_positional_required() -> None:
    m = _manifest(tools={"t": _tool(["git", "fetch", "{remote}"], {"remote": _positional(required=True)})})
    skill = build_skill_text(m)
    assert "<remote>" in skill


# -- Argument section ----------------------------------------------------------


def test_flag_param_listed_in_arguments() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", "The x value")})})
    skill = build_skill_text(m)
    assert "**Arguments:**" in skill
    assert "--x" in skill
    assert "The x value" in skill


def test_required_param_labeled() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", required=True)})})
    skill = build_skill_text(m)
    assert "(required)" in skill


def test_optional_param_labeled() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", required=False)})})
    skill = build_skill_text(m)
    assert "(optional)" in skill


def test_pattern_constraint_shown() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", pattern="^[a-z]+$")})})
    skill = build_skill_text(m)
    assert "^[a-z]+$" in skill


def test_deny_constraint_shown() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", deny=("origin",))})})
    skill = build_skill_text(m)
    assert "origin" in skill


def test_allow_constraint_shown() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", allow=("prod", "staging"))})})
    skill = build_skill_text(m)
    assert "prod" in skill
    assert "staging" in skill


def test_default_shown_in_constraints() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{x}"], {"x": _flag("--x", default="main")})})
    skill = build_skill_text(m)
    assert "main" in skill


# -- Example -------------------------------------------------------------------


def test_example_shown_when_present() -> None:
    m = _manifest(tools={"t": _tool(["echo"], example="my-tool --remote upstream")})
    skill = build_skill_text(m)
    assert "**Example:** `my-tool --remote upstream`" in skill


def test_no_example_section_when_absent() -> None:
    m = _manifest(tools={"t": _tool(["echo"])})
    skill = build_skill_text(m)
    assert "**Example:**" not in skill


# -- keep_existing / clean behavior -------------------------------------------


def test_build_skills_clears_stale_dirs_by_default(tmp_path: Path) -> None:
    stale = tmp_path / "old-group"
    stale.mkdir()
    (stale / "SKILL.md").write_text("old")
    build_skills([_manifest(skill_group="new-group")], tmp_path)
    assert not stale.exists()


def test_build_skills_keep_existing_preserves_unmanaged_dirs(tmp_path: Path) -> None:
    extra = tmp_path / "custom-group"
    extra.mkdir()
    (extra / "SKILL.md").write_text("custom")
    build_skills([_manifest(skill_group="new-group")], tmp_path, keep_existing=True)
    assert extra.exists()


def test_build_skills_always_writes_generated_files(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="my-group")], tmp_path)
    assert (tmp_path / "my-group" / "SKILL.md").exists()


@pytest.mark.parametrize("keep", [True, False])
def test_build_skills_overwrites_existing_skill(tmp_path: Path, keep: bool) -> None:
    skill_dir = tmp_path / "my-group"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("old content")
    build_skills([_manifest(skill_group="my-group")], tmp_path, keep_existing=keep)
    assert "old content" not in (skill_dir / "SKILL.md").read_text()
