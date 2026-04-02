"""Tests for output format builders."""

from __future__ import annotations

import json
from pathlib import Path

from nerftools.formats import build_claude_plugin
from nerftools.manifest import ArgSpec, NerfManifest, PackageMeta, ToolSpec


def _manifest(
    name: str = "test-pkg",
    skill_group: str = "test-pkg",
    tools: dict[str, ToolSpec] | None = None,
) -> NerfManifest:
    return NerfManifest(
        package=PackageMeta(
            name=name,
            description="Test package",
            skill_group=skill_group,
            skill_intro="",
        ),
        tools=tools or {},
    )


def _tool(command: list[str], **kwargs: object) -> ToolSpec:
    return ToolSpec(description="A test tool", command=tuple(command), **kwargs)


# -- claude-plugin format ------------------------------------------------------


def test_claude_plugin_creates_plugin_json(tmp_path: Path) -> None:
    build_claude_plugin([_manifest()], tmp_path)
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    assert plugin_json.exists()
    data = json.loads(plugin_json.read_text())
    assert data["name"] == "nerftools"
    assert data["skills"] == "./skills/"


def test_claude_plugin_creates_marketplace_json(tmp_path: Path) -> None:
    build_claude_plugin([_manifest()], tmp_path)
    mp = tmp_path / ".claude-plugin" / "marketplace.json"
    assert mp.exists()
    data = json.loads(mp.read_text())
    assert data["name"] == "agentworks-nerf-local"
    assert data["plugins"][0]["source"] == "./"


def test_claude_plugin_creates_skills_with_scripts(tmp_path: Path) -> None:
    tools = {"git-add": _tool(["git", "add", "{{files}}"], args={"files": ArgSpec(description="files", variadic=True)})}
    build_claude_plugin([_manifest(skill_group="git", tools=tools)], tmp_path, prefix="nerf-")

    skill_md = tmp_path / "skills" / "nerf-git" / "SKILL.md"
    assert skill_md.exists()

    script = tmp_path / "skills" / "nerf-git" / "scripts" / "nerf-git-add"
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable


def test_claude_plugin_skill_uses_plugin_root(tmp_path: Path) -> None:
    tools = {"git-log": _tool(["git", "log"])}
    build_claude_plugin([_manifest(skill_group="git", tools=tools)], tmp_path, prefix="nerf-")

    skill_md = tmp_path / "skills" / "nerf-git" / "SKILL.md"
    content = skill_md.read_text()
    assert "${CLAUDE_PLUGIN_ROOT}" in content
    assert "nerf-git/scripts/nerf-git-log" in content


def test_claude_plugin_overview_skill(tmp_path: Path) -> None:
    tools = {"git-log": _tool(["git", "log"])}
    build_claude_plugin([_manifest(skill_group="git", tools=tools)], tmp_path, prefix="nerf-")

    overview = tmp_path / "skills" / "nerftools" / "SKILL.md"
    assert overview.exists()
    content = overview.read_text()
    assert "# Nerf Tools" in content
    assert "nerf-git" in content


def test_claude_plugin_nerfctl_scripts(tmp_path: Path) -> None:
    build_claude_plugin([_manifest()], tmp_path)

    scripts_dir = tmp_path / "scripts"
    assert scripts_dir.exists()
    assert (scripts_dir / "nerfctl-grant-allow").exists()
    assert (scripts_dir / "nerfctl-grant-deny").exists()
    assert (scripts_dir / "nerfctl-grant-reset").exists()
    assert (scripts_dir / "nerfctl-grant-list").exists()
    assert (scripts_dir / "nerfctl-install-plugin").exists()


def test_claude_plugin_nerfctl_skills(tmp_path: Path) -> None:
    build_claude_plugin([_manifest()], tmp_path)

    for name in ("nerfctl-grant-allow", "nerfctl-grant-deny", "nerfctl-grant-reset", "nerfctl-grant-list"):
        skill_md = tmp_path / "skills" / name / "SKILL.md"
        assert skill_md.exists(), f"missing {name}/SKILL.md"
        content = skill_md.read_text()
        assert "disable-model-invocation: true" in content
        assert "${CLAUDE_PLUGIN_ROOT}" in content


def test_claude_plugin_cleans_output_by_default(tmp_path: Path) -> None:
    stale = tmp_path / "old-stuff"
    stale.mkdir()
    (stale / "file.txt").write_text("stale")
    build_claude_plugin([_manifest()], tmp_path)
    assert not stale.exists()


def test_claude_plugin_maps_to_line(tmp_path: Path) -> None:
    tools = {"git-push": _tool(["git", "push", "{{remote}}", "HEAD"])}
    build_claude_plugin([_manifest(skill_group="git", tools=tools)], tmp_path, prefix="nerf-")

    content = (tmp_path / "skills" / "nerf-git" / "SKILL.md").read_text()
    assert "**Maps to:** `git push <remote> HEAD`" in content
