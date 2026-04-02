"""Tests for rulesync skill generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from nerftools.manifest import ArgSpec, FlagSpec, NerfManifest, PackageMeta, ToolSpec
from nerftools.skill import build_skill_text, build_skills


def _manifest(
    name: str = "test-pkg",
    skill_group: str = "test-pkg",
    skill_intro: str = "",
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
    flags: dict[str, FlagSpec] | None = None,
    args: dict[str, ArgSpec] | None = None,
    description: str = "A test tool",
) -> ToolSpec:
    return ToolSpec(
        description=description,
        command=tuple(command),
        flags=flags or {},
        args=args or {},
    )


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
) -> ArgSpec:
    return ArgSpec(description=description, required=required, variadic=variadic)


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


def test_skill_has_env_var_preamble() -> None:
    m = _manifest()
    skill = build_skill_text(m)
    assert "AGENTWORKS_NERF_BIN" in skill
    assert "absolute path" in skill


def test_skill_includes_intro() -> None:
    m = _manifest(skill_intro="Use these tools carefully.")
    skill = build_skill_text(m)
    assert "Use these tools carefully." in skill


def test_skill_no_intro_section_when_absent() -> None:
    m = _manifest(skill_intro="", tools={"t": _tool(["echo"])})
    skill = build_skill_text(m)
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
    assert "**Usage:** `<nerf-bin>/my-tool`" in skill
    assert "**Maps to:** `echo`" in skill


def test_maps_to_shows_placeholders() -> None:
    m = _manifest(tools={"t": _tool(["git", "push", "{{remote}}", "{{branch}}"])})
    skill = build_skill_text(m)
    assert "**Maps to:** `git push <remote> <branch>`" in skill


def test_maps_to_npm_pkgrun_shows_runner() -> None:
    tool = ToolSpec(
        description="Run cspell",
        command=("cspell@8.19.4", "{{args}}"),
        args={"args": ArgSpec(description="args", variadic=True)},
        npm_pkgrun=True,
    )
    m = _manifest(tools={"pkgrun-cspell": tool})
    skill = build_skill_text(m)
    assert "**Maps to:** `<runner> cspell@8.19.4 <args>`" in skill


def test_usage_line_required_flag() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{remote}}"], flags={"remote": _flag("--remote")})})
    skill = build_skill_text(m)
    assert "--remote <remote>" in skill


def test_usage_line_flag_with_short() -> None:
    flags = {"remote": FlagSpec(flag="--remote", description="Remote", short="-r")}
    m = _manifest(tools={"t": _tool(["echo", "{{remote}}"], flags=flags)})
    skill = build_skill_text(m)
    assert "--remote|-r <remote>" in skill


def test_arg_section_flag_with_short() -> None:
    flags = {"remote": FlagSpec(flag="--remote", description="Remote name", short="-r")}
    m = _manifest(tools={"t": _tool(["echo", "{{remote}}"], flags=flags)})
    skill = build_skill_text(m)
    assert "`--remote|-r`" in skill


def test_usage_line_optional_flag_bracketed() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{branch}}"], flags={"branch": _flag("--branch", optional=True)})})
    skill = build_skill_text(m)
    assert "[--branch <branch>]" in skill


def test_usage_line_positional_required() -> None:
    m = _manifest(tools={"t": _tool(["git", "fetch", "{{remote}}"], args={"remote": _arg(required=True)})})
    skill = build_skill_text(m)
    assert "<remote>" in skill


def test_usage_line_variadic_arg() -> None:
    m = _manifest(tools={"t": _tool(["git", "add", "{{files}}"], args={"files": _arg(variadic=True)})})
    skill = build_skill_text(m)
    assert "<files...>" in skill


# -- Argument section ----------------------------------------------------------


def test_flag_listed_in_arguments() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{x}}"], flags={"x": _flag("--x", "The x value")})})
    skill = build_skill_text(m)
    assert "**Arguments:**" in skill
    assert "--x" in skill
    assert "The x value" in skill


def test_required_flag_labeled() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{x}}"], flags={"x": _flag("--x")})})
    skill = build_skill_text(m)
    assert "(required)" in skill


def test_optional_flag_labeled() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{x}}"], flags={"x": _flag("--x", optional=True)})})
    skill = build_skill_text(m)
    assert "(optional)" in skill


def test_pattern_constraint_shown() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{x}}"], flags={"x": _flag("--x", pattern="^[a-z]+$")})})
    skill = build_skill_text(m)
    assert "^[a-z]+$" in skill


def test_deny_constraint_shown() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{x}}"], flags={"x": _flag("--x", deny=("origin",))})})
    skill = build_skill_text(m)
    assert "origin" in skill


def test_allow_constraint_shown() -> None:
    m = _manifest(tools={"t": _tool(["echo", "{{x}}"], flags={"x": _flag("--x", allow=("prod", "staging"))})})
    skill = build_skill_text(m)
    assert "prod" in skill
    assert "staging" in skill


def test_arg_listed_in_arguments() -> None:
    m = _manifest(tools={"t": _tool(["cmd", "{{target}}"], args={"target": _arg("The target", required=True)})})
    skill = build_skill_text(m)
    assert "**Arguments:**" in skill
    assert "<target>" in skill
    assert "The target" in skill


# -- Boolean flags -------------------------------------------------------------


def test_boolean_flag_usage_shows_bracketed_flag() -> None:
    flags = {"draft": FlagSpec(flag="--draft", description="Draft PR", boolean=True)}
    m = _manifest(tools={"t": _tool(["gh", "pr", "create", "{{draft}}"], flags=flags)})
    skill = build_skill_text(m)
    assert "[--draft]" in skill


def test_boolean_flag_no_angle_brackets_in_usage() -> None:
    flags = {"draft": FlagSpec(flag="--draft", description="Draft PR", boolean=True)}
    m = _manifest(tools={"t": _tool(["gh", "pr", "create", "{{draft}}"], flags=flags)})
    skill = build_skill_text(m)
    usage_line = next(line for line in skill.splitlines() if line.startswith("**Usage:**"))
    assert "<draft>" not in usage_line


def test_boolean_flag_labeled_boolean_in_arguments() -> None:
    flags = {"draft": FlagSpec(flag="--draft", description="Draft PR", boolean=True)}
    m = _manifest(tools={"t": _tool(["gh", "pr", "create", "{{draft}}"], flags=flags)})
    skill = build_skill_text(m)
    assert "(boolean)" in skill


# -- keep_existing / clean behavior -------------------------------------------


def test_build_skills_clears_stale_dirs_by_default(tmp_path: Path) -> None:
    stale = tmp_path / "old-group"
    stale.mkdir()
    (stale / "SKILL.md").write_text("old")
    build_skills([_manifest(skill_group="new-group")], tmp_path, prefix="")
    assert not stale.exists()


def test_build_skills_keep_existing_preserves_unmanaged_dirs(tmp_path: Path) -> None:
    extra = tmp_path / "custom-group"
    extra.mkdir()
    (extra / "SKILL.md").write_text("custom")
    build_skills([_manifest(skill_group="new-group")], tmp_path, keep_existing=True, prefix="")
    assert extra.exists()


def test_build_skills_always_writes_generated_files(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="my-group")], tmp_path, prefix="")
    assert (tmp_path / "my-group" / "SKILL.md").exists()


@pytest.mark.parametrize("keep", [True, False])
def test_build_skills_overwrites_existing_skill(tmp_path: Path, keep: bool) -> None:
    skill_dir = tmp_path / "my-group"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("old content")
    build_skills([_manifest(skill_group="my-group")], tmp_path, keep_existing=keep, prefix="")
    assert "old content" not in (skill_dir / "SKILL.md").read_text()


def test_build_skills_prefix_applied_to_dir(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="git")], tmp_path, prefix="nerf-")
    assert (tmp_path / "nerf-git" / "SKILL.md").exists()
    assert not (tmp_path / "git").exists()


def test_build_skills_prefix_in_skill_content(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="git", name="git")], tmp_path, prefix="nerf-")
    content = (tmp_path / "nerf-git" / "SKILL.md").read_text()
    assert "name: nerf-git" in content
    assert "# nerf-git" in content


def test_plugin_manifest_generated(tmp_path: Path) -> None:
    from nerftools.skill import build_plugin_manifest

    out = build_plugin_manifest(tmp_path)
    assert out.exists()
    assert out.parent.name == ".claude-plugin"
    import json

    plugin = json.loads(out.read_text())
    assert plugin["name"] == "nerftools"
    assert plugin["skills"] == "./skills/"

    marketplace_path = out.parent / "marketplace.json"
    assert marketplace_path.exists()
    marketplace = json.loads(marketplace_path.read_text())
    assert marketplace["name"] == "agentworks-nerf-local"
    assert marketplace["plugins"][0]["name"] == "nerftools"
    assert marketplace["plugins"][0]["source"] == "./"


def test_build_skill_text_prefix_applied_to_tool_names(tmp_path: Path) -> None:
    m = _manifest(skill_group="git", tools={"git-fetch": _tool(["git", "fetch"])})
    skill = build_skill_text(m, prefix="nerf-")
    assert "## nerf-git-fetch" in skill
    assert "**Usage:** `<nerf-bin>/nerf-git-fetch`" in skill


# -- Overview skill ------------------------------------------------------------


def test_nerftools_skill_generated(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="git")], tmp_path, prefix="nerf-")
    assert (tmp_path / "nerftools" / "SKILL.md").exists()


def test_nerftools_skill_not_generated_when_no_manifests(tmp_path: Path) -> None:
    build_skills([], tmp_path, prefix="nerf-")
    assert not (tmp_path / "nerftools").exists()


def test_nerftools_skill_lists_tool_families() -> None:
    from nerftools.skill import build_overview_text

    manifests = [
        _manifest(skill_group="git", tools={"git-add": _tool(["git", "add"])}),
        _manifest(skill_group="az-repos", tools={"az-pr-create": _tool(["az", "repos", "pr", "create"])}),
    ]
    text = build_overview_text(manifests, prefix="nerf-")
    assert "# Nerf Tools" in text
    assert "**nerf-git**" in text
    assert "**nerf-az-repos**" in text
    assert "Test package" in text


def test_nerftools_skill_has_usage_guidance() -> None:
    from nerftools.skill import build_overview_text

    text = build_overview_text([_manifest(skill_group="git")], prefix="nerf-")
    assert "prefer it over invoking the underlying tool directly" in text
    assert "AGENTWORKS_NERF_BIN" in text
    assert "absolute path" in text


def test_nerftools_skill_has_frontmatter() -> None:
    from nerftools.skill import build_overview_text

    text = build_overview_text([_manifest(skill_group="git")], prefix="nerf-")
    assert text.startswith("---\n")
    assert "name: nerftools" in text
