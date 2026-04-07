"""Tests for manifest loading and validation (v1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nerftools.manifest import (
    ManifestError,
    NerfManifest,
    ThreatLevel,
    load_manifest,
    merge_manifests,
)

# -- Fixtures ------------------------------------------------------------------


def _write_manifest(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "manifest.yaml"
    p.write_text(yaml.dump(content))
    return p


def _minimal_manifest(tools: dict | None = None) -> dict:
    return {
        "version": 1,
        "package": {
            "name": "test-pkg",
            "description": "Test package",
            "skill_group": "test-pkg",
        },
        "tools": tools
        or {
            "test-tool": {
                "description": "A test tool",
                "threat": {"read": "workspace", "write": "none"},
                "template": {"command": ["echo", "hello"]},
            },
        },
    }


# -- Version -------------------------------------------------------------------


def test_missing_version_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest()
    del raw["version"]
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'version' is required"):
        load_manifest(p)


def test_unsupported_version_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest()
    raw["version"] = 99
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="unsupported manifest version"):
        load_manifest(p)


# -- Package loading -----------------------------------------------------------


def test_load_minimal_manifest(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, _minimal_manifest())
    m = load_manifest(p)
    assert isinstance(m, NerfManifest)
    assert m.version == 1
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
    p.write_text(
        "version: 1\ntools:\n  foo:\n    description: x\n"
        "    threat:\n      read: none\n      write: none\n"
        "    template:\n      command: [echo]\n"
    )
    with pytest.raises(ManifestError, match="'package' section is required"):
        load_manifest(p)


def test_missing_tools_section_raises(tmp_path: Path) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text("version: 1\npackage:\n  name: x\n  description: x\n  skill_group: x\n")
    with pytest.raises(ManifestError, match="'tools' section is required"):
        load_manifest(p)


def test_missing_required_package_field_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest()
    del raw["package"]["name"]
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'name' is required"):
        load_manifest(p)


# -- Threat model --------------------------------------------------------------


def test_threat_level_ordering() -> None:
    assert ThreatLevel.NONE <= ThreatLevel.WORKSPACE
    assert ThreatLevel.WORKSPACE <= ThreatLevel.MACHINE
    assert ThreatLevel.MACHINE <= ThreatLevel.REMOTE
    assert ThreatLevel.REMOTE <= ThreatLevel.ADMIN
    assert not ThreatLevel.ADMIN <= ThreatLevel.NONE


def test_threat_level_comparison() -> None:
    assert ThreatLevel.WORKSPACE < ThreatLevel.REMOTE
    assert ThreatLevel.REMOTE > ThreatLevel.WORKSPACE
    assert ThreatLevel.NONE >= ThreatLevel.NONE


def test_tool_threat_loaded(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, _minimal_manifest())
    m = load_manifest(p)
    tool = m.tools["test-tool"]
    assert tool.threat.read == ThreatLevel.WORKSPACE
    assert tool.threat.write == ThreatLevel.NONE


def test_missing_threat_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {"description": "x", "template": {"command": ["echo"]}},
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="'threat' is required"):
        load_manifest(p)


def test_invalid_threat_level_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "galaxy", "write": "none"},
            "template": {"command": ["echo"]},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="invalid read level"):
        load_manifest(p)


# -- Execution modes -----------------------------------------------------------


def test_template_mode(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "hello"]},
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["t"].template is not None
    assert m.tools["t"].template.command == ("echo", "hello")
    assert m.tools["t"].mode == "template"


def test_passthrough_mode(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "workspace", "write": "none"},
            "passthrough": {"command": "find", "deny": ["-exec"], "prefix": ["."]},
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["t"].passthrough is not None
    assert m.tools["t"].passthrough.command == "find"
    assert m.tools["t"].passthrough.deny == ("-exec",)
    assert m.tools["t"].passthrough.prefix == (".",)
    assert m.tools["t"].mode == "passthrough"


def test_script_mode(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "script": "echo hello",
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["t"].script == "echo hello"
    assert m.tools["t"].mode == "script"


def test_no_mode_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="exactly one of"):
        load_manifest(p)


def test_multiple_modes_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo"]},
            "script": "echo hello",
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="only one of"):
        load_manifest(p)


def test_params_in_passthrough_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "passthrough": {"command": "find"},
            "switches": {"verbose": {"description": "Verbose"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="not allowed in passthrough"):
        load_manifest(p)


# -- Switches ------------------------------------------------------------------


def test_switch_loaded(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{switches.verbose}}"]},
            "switches": {"verbose": {"description": "Enable verbose"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    sw = m.tools["t"].switches["verbose"]
    assert sw.flag == "--verbose"
    assert sw.description == "Enable verbose"


def test_switch_short(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{switches.verbose}}"]},
            "switches": {"verbose": {"description": "Verbose", "short": "-v"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["t"].switches["verbose"].short == "-v"


def test_invalid_switch_short_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{switches.verbose}}"]},
            "switches": {"verbose": {"description": "Verbose", "short": "--vv"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="single-character flag"):
        load_manifest(p)


# -- Options -------------------------------------------------------------------


def test_option_loaded(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{options.remote}}"]},
            "options": {
                "remote": {
                    "description": "Remote name",
                    "required": True,
                    "pattern": "^[a-z]+$",
                    "deny": ["origin"],
                },
            },
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    opt = m.tools["t"].options["remote"]
    assert opt.flag == "--remote"
    assert opt.required is True
    assert opt.pattern == "^[a-z]+$"
    assert opt.deny == ("origin",)


def test_option_allow_deny_conflict_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{options.x}}"]},
            "options": {"x": {"description": "x", "allow": ["a"], "deny": ["b"]}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot both be set"):
        load_manifest(p)


def test_invalid_option_pattern_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{options.x}}"]},
            "options": {"x": {"description": "x", "pattern": "[invalid"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="invalid 'pattern' regex"):
        load_manifest(p)


# -- Arguments -----------------------------------------------------------------


def test_argument_loaded(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["git", "fetch", "{{arguments.remote}}"]},
            "arguments": {
                "remote": {"description": "Remote name", "required": True},
            },
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    arg = m.tools["t"].arguments["remote"]
    assert arg.required is True
    assert arg.variadic is False


def test_variadic_argument(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["git", "add", "{{arguments.files}}"]},
            "arguments": {"files": {"description": "Files", "variadic": True}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["t"].arguments["files"].variadic is True


def test_argument_allow_deny_conflict_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{arguments.x}}"]},
            "arguments": {"x": {"description": "x", "allow": ["a"], "deny": ["b"]}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot both be set"):
        load_manifest(p)


# -- Guards and pre hooks ------------------------------------------------------


def test_guard_loaded(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["git", "push", "{{arguments.remote}}", "HEAD"]},
            "arguments": {"remote": {"description": "Remote", "required": True}},
            "guards": [
                {"command": ["git", "remote", "get-url", "{{arguments.remote}}"], "fail_message": "Remote not found"},
            ],
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert len(m.tools["t"].guards) == 1
    assert m.tools["t"].guards[0].command == ("git", "remote", "get-url", "{{arguments.remote}}")


def test_pre_hook_loaded(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo"]},
            "pre": "echo setup",
        },
    })
    p = _write_manifest(tmp_path, raw)
    m = load_manifest(p)
    assert m.tools["t"].pre == "echo setup"


# -- Cross-reference validation ------------------------------------------------


def test_undefined_placeholder_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "{{arguments.x}}"]},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot be resolved"):
        load_manifest(p)


def test_unreferenced_param_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "hello"]},
            "arguments": {"x": {"description": "x"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="not referenced in template command"):
        load_manifest(p)


def test_name_overlap_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["cmd", "{{arguments.x}}"]},
            "switches": {"x": {"description": "x"}},
            "arguments": {"x": {"description": "x"}},
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="names overlap"):
        load_manifest(p)


def test_variadic_not_last_raises(tmp_path: Path) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        "version: 1\n"
        "package:\n"
        "  name: test-pkg\n"
        "  description: Test package\n"
        "  skill_group: test-pkg\n"
        "tools:\n"
        "  t:\n"
        "    description: x\n"
        "    threat:\n"
        "      read: none\n"
        "      write: none\n"
        "    template:\n"
        "      command: [echo, '{{arguments.files}}', '{{arguments.extra}}']\n"
        "    arguments:\n"
        "      files:\n"
        "        description: Files\n"
        "        variadic: true\n"
        "      extra:\n"
        "        description: Extra\n"
    )
    with pytest.raises(ManifestError, match="variadic but is not the last"):
        load_manifest(p)


def test_guard_undefined_placeholder_raises(tmp_path: Path) -> None:
    raw = _minimal_manifest(tools={
        "t": {
            "description": "x",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "{{arguments.x}}"]},
            "arguments": {"x": {"description": "x"}},
            "guards": [{"command": ["check", "{{arguments.y}}"], "fail_message": "fail"}],
        },
    })
    p = _write_manifest(tmp_path, raw)
    with pytest.raises(ManifestError, match="cannot be resolved"):
        load_manifest(p)


# -- Merging -------------------------------------------------------------------


def test_merge_last_wins(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    first.write_text(yaml.dump(_minimal_manifest(tools={
        "t": {
            "description": "First version",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "first"]},
        },
    })))
    second = tmp_path / "second.yaml"
    second.write_text(yaml.dump(_minimal_manifest(tools={
        "t": {
            "description": "Second version",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "second"]},
        },
    })))

    merged = merge_manifests([load_manifest(first), load_manifest(second)])
    assert len(merged) == 1
    assert merged[0].tools["t"].description == "Second version"


def test_merge_different_packages(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    a.write_text(yaml.dump({
        "version": 1,
        "package": {"name": "pkg-a", "description": "A", "skill_group": "pkg-a"},
        "tools": {"t": {
            "description": "A",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "a"]},
        }},
    }))
    b = tmp_path / "b.yaml"
    b.write_text(yaml.dump({
        "version": 1,
        "package": {"name": "pkg-b", "description": "B", "skill_group": "pkg-b"},
        "tools": {"t": {
            "description": "B",
            "threat": {"read": "none", "write": "none"},
            "template": {"command": ["echo", "b"]},
        }},
    }))

    merged = merge_manifests([load_manifest(a), load_manifest(b)])
    assert len(merged) == 2
    names = {m.package.name for m in merged}
    assert names == {"pkg-a", "pkg-b"}


# -- Built-in manifest ---------------------------------------------------------


def test_builtin_git_loads() -> None:
    from nerftools.cli import _DEFAULT_MANIFESTS_DIR

    manifest_path = _DEFAULT_MANIFESTS_DIR / "git.yaml"
    assert manifest_path.exists(), f"Built-in manifest not found: {manifest_path}"
    m = load_manifest(manifest_path)
    assert m.version == 1
    assert m.package.name == "git"
    assert "git-add" in m.tools
    assert "git-commit" in m.tools
    assert "git-fetch" in m.tools
    assert "git-push-main" in m.tools
    assert "git-push-branch" in m.tools
    assert "git-tag" in m.tools
    # Verify threat profiles
    assert m.tools["git-log"].threat.read == ThreatLevel.WORKSPACE
    assert m.tools["git-log"].threat.write == ThreatLevel.NONE
    assert m.tools["git-push-branch"].threat.write == ThreatLevel.REMOTE
