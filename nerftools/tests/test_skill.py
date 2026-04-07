"""Tests for rulesync skill generation (v1)."""

from __future__ import annotations

from pathlib import Path

from nerftools.manifest import (
    ArgSpec,
    NerfManifest,
    OptionSpec,
    PackageMeta,
    PassthroughSpec,
    SwitchSpec,
    TemplateSpec,
    ThreatLevel,
    ThreatSpec,
    ToolSpec,
)
from nerftools.skill import build_skill_text, build_skills

_THREAT_NONE = ThreatSpec(read=ThreatLevel.NONE, write=ThreatLevel.NONE)


def _manifest(
    name: str = "test-pkg",
    skill_group: str = "test-pkg",
    skill_intro: str = "",
    tools: dict[str, ToolSpec] | None = None,
) -> NerfManifest:
    return NerfManifest(
        version=1,
        package=PackageMeta(
            name=name,
            description="Test package",
            skill_group=skill_group,
            skill_intro=skill_intro,
        ),
        tools=tools or {},
    )


def _template_tool(
    command: list[str],
    switches: dict[str, SwitchSpec] | None = None,
    options: dict[str, OptionSpec] | None = None,
    arguments: dict[str, ArgSpec] | None = None,
    description: str = "A test tool",
) -> ToolSpec:
    return ToolSpec(
        description=description,
        threat=_THREAT_NONE,
        template=TemplateSpec(command=tuple(command)),
        switches=switches or {},
        options=options or {},
        arguments=arguments or {},
    )


def _option(
    flag: str,
    description: str = "A param",
    *,
    required: bool = True,
    pattern: str | None = None,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
) -> OptionSpec:
    return OptionSpec(
        flag=flag, description=description, required=required,
        pattern=pattern, allow=allow, deny=deny,
    )


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


def test_tool_has_h2_section() -> None:
    m = _manifest(tools={"my-tool": _template_tool(["echo"])})
    skill = build_skill_text(m)
    assert "## my-tool" in skill


def test_tool_description_in_skill() -> None:
    m = _manifest(tools={"my-tool": _template_tool(["echo"], description="Does the thing")})
    skill = build_skill_text(m)
    assert "Does the thing." in skill


def test_no_args_tool_shows_no_arguments() -> None:
    m = _manifest(tools={"my-tool": _template_tool(["echo"])})
    skill = build_skill_text(m)
    assert "No arguments." in skill


def test_tool_separated_by_horizontal_rule() -> None:
    m = _manifest(tools={"my-tool": _template_tool(["echo"])})
    skill = build_skill_text(m)
    assert "---" in skill


# -- Usage line ----------------------------------------------------------------


def test_usage_line_simple_tool() -> None:
    m = _manifest(tools={"my-tool": _template_tool(["echo"])})
    skill = build_skill_text(m)
    assert "**Usage:** `<nerf-bin>/my-tool`" in skill
    assert "**Maps to:** `echo`" in skill


def test_maps_to_shows_placeholders() -> None:
    m = _manifest(tools={"t": _template_tool(["git", "push", "{{arguments.remote}}", "{{arguments.branch}}"])})
    skill = build_skill_text(m)
    assert "**Maps to:** `git push <remote> <branch>`" in skill


def test_maps_to_npm_pkgrun_shows_runner() -> None:
    tool = ToolSpec(
        description="Run cspell",
        threat=_THREAT_NONE,
        template=TemplateSpec(command=("cspell@8.19.4", "{{arguments.args}}"), npm_pkgrun=True),
        arguments={"args": ArgSpec(description="args", variadic=True)},
    )
    m = _manifest(tools={"pkgrun-cspell": tool})
    skill = build_skill_text(m)
    assert "**Maps to:** `<runner> cspell@8.19.4 <args>`" in skill


def test_usage_line_required_option() -> None:
    m = _manifest(tools={"t": _template_tool(["echo", "{{options.remote}}"], options={"remote": _option("--remote")})})
    skill = build_skill_text(m)
    assert "--remote <remote>" in skill


def test_usage_line_option_with_short() -> None:
    options = {"remote": OptionSpec(flag="--remote", description="Remote", short="-r", required=True)}
    m = _manifest(tools={"t": _template_tool(["echo", "{{options.remote}}"], options=options)})
    skill = build_skill_text(m)
    assert "--remote|-r <remote>" in skill


def test_usage_line_optional_option_bracketed() -> None:
    m = _manifest(tools={"t": _template_tool(
        ["echo", "{{options.branch}}"],
        options={"branch": _option("--branch", required=False)},
    )})
    skill = build_skill_text(m)
    assert "[--branch <branch>]" in skill


def test_usage_line_positional_required() -> None:
    m = _manifest(tools={"t": _template_tool(
        ["git", "fetch", "{{arguments.remote}}"],
        arguments={"remote": _arg(required=True)},
    )})
    skill = build_skill_text(m)
    assert "<remote>" in skill


def test_usage_line_variadic_arg() -> None:
    m = _manifest(tools={"t": _template_tool(["git", "add", "{{arguments.files}}"], arguments={"files": _arg(variadic=True)})})
    skill = build_skill_text(m)
    assert "<files...>" in skill


# -- Parameter sections --------------------------------------------------------


def test_switch_listed_in_switches() -> None:
    switches = {"verbose": SwitchSpec(flag="--verbose", description="Enable verbose")}
    m = _manifest(tools={"t": _template_tool(["cmd", "{{switches.verbose}}"], switches=switches)})
    skill = build_skill_text(m)
    assert "**Switches:**" in skill
    assert "--verbose" in skill
    assert "Enable verbose" in skill


def test_option_listed_in_options() -> None:
    options = {"remote": _option("--remote", "Remote name")}
    m = _manifest(tools={"t": _template_tool(["echo", "{{options.remote}}"], options=options)})
    skill = build_skill_text(m)
    assert "**Options:**" in skill
    assert "--remote" in skill
    assert "Remote name" in skill


def test_required_option_labeled() -> None:
    m = _manifest(tools={"t": _template_tool(["echo", "{{options.x}}"], options={"x": _option("--x")})})
    skill = build_skill_text(m)
    assert "(required)" in skill


def test_optional_option_labeled() -> None:
    m = _manifest(tools={"t": _template_tool(["echo", "{{options.x}}"], options={"x": _option("--x", required=False)})})
    skill = build_skill_text(m)
    assert "(optional)" in skill


def test_pattern_constraint_shown() -> None:
    m = _manifest(tools={"t": _template_tool(["echo", "{{x}}"], options={"x": _option("--x", pattern="^[a-z]+$")})})
    skill = build_skill_text(m)
    assert "^[a-z]+$" in skill


def test_arg_listed_in_arguments() -> None:
    m = _manifest(tools={"t": _template_tool(
        ["cmd", "{{target}}"],
        arguments={"target": _arg("The target", required=True)},
    )})
    skill = build_skill_text(m)
    assert "**Arguments:**" in skill
    assert "<target>" in skill
    assert "The target" in skill


# -- Passthrough skill ---------------------------------------------------------


def test_passthrough_maps_to_shows_dollar_at() -> None:
    tool = ToolSpec(
        description="Safe find",
        threat=_THREAT_NONE,
        passthrough=PassthroughSpec(command="find", prefix=(".",)),
    )
    m = _manifest(tools={"safe-find": tool})
    skill = build_skill_text(m)
    assert '**Maps to:** `find . "$@"`' in skill


def test_passthrough_shows_denied_patterns() -> None:
    tool = ToolSpec(
        description="Safe find",
        threat=_THREAT_NONE,
        passthrough=PassthroughSpec(command="find", deny=("-exec", "-delete"), prefix=(".",)),
    )
    m = _manifest(tools={"safe-find": tool})
    skill = build_skill_text(m)
    assert "**Denied patterns:**" in skill
    assert "`-exec`" in skill
    assert "`-delete`" in skill


def test_passthrough_usage_shows_tokens() -> None:
    tool = ToolSpec(
        description="Safe find",
        threat=_THREAT_NONE,
        passthrough=PassthroughSpec(command="find"),
    )
    m = _manifest(tools={"safe-find": tool})
    skill = build_skill_text(m)
    assert "[tokens...]" in skill


# -- Script mode skill ---------------------------------------------------------


def test_script_mode_no_maps_to() -> None:
    tool = ToolSpec(description="Check", threat=_THREAT_NONE, script="echo done")
    m = _manifest(tools={"check": tool})
    skill = build_skill_text(m)
    assert "Maps to:" not in skill


# -- Switch usage in skill -----------------------------------------------------


def test_switch_usage_shows_bracketed_flag() -> None:
    switches = {"draft": SwitchSpec(flag="--draft", description="Draft PR")}
    m = _manifest(tools={"t": _template_tool(["gh", "pr", "create", "{{draft}}"], switches=switches)})
    skill = build_skill_text(m)
    assert "[--draft]" in skill


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


def test_build_skills_prefix_applied_to_dir(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="git")], tmp_path, prefix="nerf-")
    assert (tmp_path / "nerf-git" / "SKILL.md").exists()


def test_build_skills_prefix_in_skill_content(tmp_path: Path) -> None:
    build_skills([_manifest(skill_group="git", name="git")], tmp_path, prefix="nerf-")
    content = (tmp_path / "nerf-git" / "SKILL.md").read_text()
    assert "name: nerf-git" in content
    assert "# nerf-git" in content


def test_build_skill_text_prefix_applied_to_tool_names(tmp_path: Path) -> None:
    m = _manifest(skill_group="git", tools={"git-fetch": _template_tool(["git", "fetch"])})
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
        _manifest(skill_group="git", tools={"git-add": _template_tool(["git", "add"])}),
        _manifest(skill_group="az-repos", tools={"az-pr-create": _template_tool(["az", "repos", "pr", "create"])}),
    ]
    text = build_overview_text(manifests, prefix="nerf-")
    assert "# Nerf Tools" in text
    assert "**nerf-git**" in text
    assert "**nerf-az-repos**" in text


def test_nerftools_skill_has_frontmatter() -> None:
    from nerftools.skill import build_overview_text

    text = build_overview_text([_manifest(skill_group="git")], prefix="nerf-")
    assert text.startswith("---\n")
    assert "name: nerftools" in text
