"""Tests for manifest loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nerftools.manifest import ManifestError, NerfManifest, load_manifest, merge_manifests

# -- Fixtures ------------------------------------------------------------------


def _write_manifest(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "manifest.yaml"
    p.write_text(yaml.dump(content))
    return p


def _minimal_manifest(tools: dict | None = None) -> dict:
    return {
        "package": {
            "name": "test-pkg",
            "description": "Test package",
            "skill_group": "test-pkg",
        },
        "tools": tools
        or {
            "test-tool": {
                "description": "A test tool",
                "command": ["echo", "hello"],
            },
        },
    }


# -- Package loading -----------------------------------------------------------


def test_load_minimal_manifest(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, _minimal_manifest())
    m = load_manifest(p)
    assert isinstance(m, NerfManifest)
    assert m.package.name == "test-pkg"
    assert m.package.skill_group == "test-pkg"
    assert "test-tool" in m.tools


def test_load_manifest_with_skill_intro(tmp_path: Path) -> None:
    raw = _minimal_manifest()
    raw["package"]["skill_intro"] = "Use these tools carefully."
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.package.skill_intro == "Use these tools carefully."


def test_missing_package_section_raises(tmp_path: Path) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text("tools:\n  foo:\n    description: x\n    command: [echo]\n")
    with pytest.raises(ManifestError, match="'package' section is required"):
        load_manifest(p)


def test_missing_tools_section_raises(tmp_path: Path) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text("package:\n  name: x\n  description: x\n  skill_group: x\n")
    with pytest.raises(ManifestError, match="'tools' section is required"):
        load_manifest(p)


def test_missing_required_package_field_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest()
    del raw["package"]["name"]
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'name' is required"):
        load_manifest(p)


# -- Tool loading --------------------------------------------------------------


def test_tool_with_flag_param(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with flag",
                "command": ["git", "push", "{remote}", "HEAD"],
                "params": {
                    "remote": {
                        "flag": "--remote",
                        "description": "Remote name",
                        "required": True,
                        "pattern": "^[a-z]+$",
                        "deny": ["origin"],
                    },
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    tool = m.tools["my-tool"]
    param = tool.params["remote"]
    assert param.flag == "--remote"
    assert param.required is True
    assert param.pattern == "^[a-z]+$"
    assert param.deny == ("origin",)


def test_tool_with_positional_param(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with positional",
                "command": ["git", "fetch", "{remote}"],
                "params": {
                    "remote": {
                        "positional": True,
                        "description": "Remote name",
                        "required": True,
                    },
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    param = m.tools["my-tool"].params["remote"]
    assert param.positional is True
    assert param.flag is None


def test_tool_with_env(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with env",
                "command": ["az", "account", "show"],
                "env": {"AZURE_CONFIG_DIR": "/home/user/.azure"},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["my-tool"].env == {"AZURE_CONFIG_DIR": "/home/user/.azure"}


def test_tool_with_example(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with example",
                "command": ["echo", "hi"],
                "example": "my-tool --foo bar",
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["my-tool"].example == "my-tool --foo bar"


# -- Validation errors ---------------------------------------------------------


def test_flag_and_positional_conflict(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "params": {"x": {"flag": "--x", "positional": True, "description": "x"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="mutually exclusive"):
        load_manifest(p)


def test_allow_and_deny_conflict(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "params": {"x": {"flag": "--x", "description": "x", "allow": ["a"], "deny": ["b"]}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot both be set"):
        load_manifest(p)


def test_default_with_required_conflict(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "params": {"x": {"flag": "--x", "description": "x", "required": True, "default": "val"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'default' cannot be set when 'required' is true"):
        load_manifest(p)


def test_invalid_pattern_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "params": {"x": {"flag": "--x", "description": "x", "pattern": "[invalid"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="invalid 'pattern' regex"):
        load_manifest(p)


def test_unreferenced_placeholder_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "params": {},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="no param 'x' is defined"):
        load_manifest(p)


def test_unused_param_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "hello"],
                "params": {"x": {"flag": "--x", "description": "x"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="not referenced in command"):
        load_manifest(p)


def test_neither_flag_nor_positional_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "params": {"x": {"description": "x"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="one of 'flag' or 'positional' must be set"):
        load_manifest(p)


# -- Merging -------------------------------------------------------------------


def test_merge_last_wins(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    first.write_text(
        yaml.dump(
            _minimal_manifest(
                tools={
                    "my-tool": {"description": "First version", "command": ["echo", "first"]},
                }
            )
        )
    )
    second = tmp_path / "second.yaml"
    second.write_text(
        yaml.dump(
            _minimal_manifest(
                tools={
                    "my-tool": {"description": "Second version", "command": ["echo", "second"]},
                }
            )
        )
    )

    merged = merge_manifests([load_manifest(first), load_manifest(second)])
    assert len(merged) == 1
    assert merged[0].tools["my-tool"].description == "Second version"


def test_merge_different_packages(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    a.write_text(
        yaml.dump(
            {
                "package": {"name": "pkg-a", "description": "A", "skill_group": "pkg-a"},
                "tools": {"tool-a": {"description": "Tool A", "command": ["echo", "a"]}},
            }
        )
    )
    b = tmp_path / "b.yaml"
    b.write_text(
        yaml.dump(
            {
                "package": {"name": "pkg-b", "description": "B", "skill_group": "pkg-b"},
                "tools": {"tool-b": {"description": "Tool B", "command": ["echo", "b"]}},
            }
        )
    )

    merged = merge_manifests([load_manifest(a), load_manifest(b)])
    assert len(merged) == 2
    names = {m.package.name for m in merged}
    assert names == {"pkg-a", "pkg-b"}


# -- Built-in manifest ---------------------------------------------------------


def test_builtin_nerf_git_loads() -> None:
    from nerftools.cli import _BUILTIN_MANIFESTS_DIR

    manifest_path = _BUILTIN_MANIFESTS_DIR / "nerf-git" / "manifest.yaml"
    assert manifest_path.exists(), f"Built-in manifest not found: {manifest_path}"
    m = load_manifest(manifest_path)
    assert m.package.name == "nerf-git"
    assert "nerf-git-push-origin" in m.tools
    assert "nerf-git-push-remote" in m.tools
    assert "nerf-git-fetch" in m.tools
    assert "nerf-git-log" in m.tools
