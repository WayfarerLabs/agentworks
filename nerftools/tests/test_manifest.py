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


def test_tool_with_flag(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with flag",
                "command": ["git", "push", "{remote}", "HEAD"],
                "flags": {
                    "remote": {
                        "description": "Remote name",
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
    flag = tool.flags["remote"]
    assert flag.flag == "--remote"
    assert flag.required is True  # optional=False by default
    assert flag.pattern == "^[a-z]+$"
    assert flag.deny == ("origin",)


def test_flag_auto_derived_name(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool",
                "command": ["git", "push", "{my_remote}"],
                "flags": {
                    "my_remote": {"description": "Remote"},
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["my-tool"].flags["my_remote"].flag == "--my-remote"


def test_flag_explicit_name(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool",
                "command": ["git", "push", "{remote}"],
                "flags": {
                    "remote": {"flag": "-r", "description": "Remote"},
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["my-tool"].flags["remote"].flag == "-r"


def test_flag_with_short(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool",
                "command": ["git", "push", "{remote}"],
                "flags": {
                    "remote": {"description": "Remote", "short": "-r"},
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["my-tool"].flags["remote"].short == "-r"


def test_invalid_short_flag_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool",
                "command": ["git", "push", "{remote}"],
                "flags": {
                    "remote": {"description": "Remote", "short": "--r"},
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="single-character flag"):
        load_manifest(p)


def test_optional_flag(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool",
                "command": ["git", "fetch", "{remote}"],
                "flags": {
                    "remote": {"description": "Remote", "optional": True},
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    flag = m.tools["my-tool"].flags["remote"]
    assert flag.optional is True
    assert flag.required is False


def test_tool_with_positional_arg(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with positional",
                "command": ["git", "fetch", "{remote}"],
                "args": {
                    "remote": {
                        "description": "Remote name",
                        "required": True,
                    },
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    arg = m.tools["my-tool"].args["remote"]
    assert arg.required is True
    assert arg.variadic is False


def test_variadic_arg(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with variadic",
                "command": ["git", "add", "{files}"],
                "args": {
                    "files": {
                        "description": "Files to add",
                        "variadic": True,
                    },
                },
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    arg = m.tools["my-tool"].args["files"]
    assert arg.variadic is True
    assert arg.required is False


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


def test_tool_with_guard(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Tool with guard",
                "command": ["git", "push", "{remote}", "HEAD"],
                "flags": {
                    "remote": {"description": "Remote"},
                },
                "guards": [
                    {
                        "command": ["git", "remote", "get-url", "{remote}"],
                        "fail_message": "Remote does not exist",
                    }
                ],
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    tool = m.tools["my-tool"]
    assert len(tool.guards) == 1
    assert tool.guards[0].command == ("git", "remote", "get-url", "{remote}")
    assert tool.guards[0].fail_message == "Remote does not exist"


# -- Validation errors ---------------------------------------------------------


def test_flag_and_arg_name_collision(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "flags": {"x": {"description": "x"}},
                "args": {"x": {"description": "x"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="names defined in both flags and args"):
        load_manifest(p)


def test_allow_and_deny_conflict_in_flag(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "flags": {"x": {"description": "x", "allow": ["a"], "deny": ["b"]}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot both be set"):
        load_manifest(p)


def test_allow_and_deny_conflict_in_arg(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "args": {"x": {"description": "x", "allow": ["a"], "deny": ["b"]}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot both be set"):
        load_manifest(p)


def test_invalid_pattern_in_flag_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "flags": {"x": {"description": "x", "pattern": "[invalid"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="invalid 'pattern' regex"):
        load_manifest(p)


def test_invalid_pattern_in_arg_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "args": {"x": {"description": "x", "pattern": "[invalid"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="invalid 'pattern' regex"):
        load_manifest(p)


def test_undefined_placeholder_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'x' is not defined in flags or args"):
        load_manifest(p)


def test_unused_flag_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "hello"],
                "flags": {"x": {"description": "x"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="not referenced in command"):
        load_manifest(p)


def test_unused_arg_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "hello"],
                "args": {"x": {"description": "x"}},
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="not referenced in command"):
        load_manifest(p)


def test_variadic_not_last_raises(tmp_path: Path) -> None:
    # Write YAML directly to control key ordering (yaml.dump sorts keys)
    p = tmp_path / "manifest.yaml"
    p.write_text(
        "package:\n"
        "  name: test-pkg\n"
        "  description: Test package\n"
        "  skill_group: test-pkg\n"
        "tools:\n"
        "  my-tool:\n"
        "    description: Bad tool\n"
        "    command: [echo, '{files}', '{extra}']\n"
        "    args:\n"
        "      files:\n"
        "        description: Files\n"
        "        variadic: true\n"
        "      extra:\n"
        "        description: Extra\n"
    )
    with pytest.raises(ManifestError, match="variadic but is not the last arg"):
        load_manifest(p)


def test_guard_undefined_placeholder_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(
        tools={
            "my-tool": {
                "description": "Bad tool",
                "command": ["echo", "{x}"],
                "flags": {"x": {"description": "x"}},
                "guards": [{"command": ["check", "{y}"], "fail_message": "fail"}],
            },
        }
    )
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'y' is not defined in flags or args"):
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
