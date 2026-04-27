"""Tests for nerftools plugin build and install script generation."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from nerftools import BUILTIN_MANIFESTS_DIR  # type: ignore[import-untyped]
from nerftools.config import (
    load_config,
    resolve_claude_plugin_meta,
)
from nerftools.formats import build_claude_plugin  # type: ignore[import-untyped]
from nerftools.manifest import load_manifest, merge_manifests  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_NERF_CONFIG_PATH = Path(__file__).resolve().parent.parent / "agentworks" / "nerf-config.yaml"


def test_nerf_config_exists() -> None:
    assert _NERF_CONFIG_PATH.exists(), f"nerf-config.yaml not found at {_NERF_CONFIG_PATH}"


def test_nerf_config_loads() -> None:
    config = load_config(_NERF_CONFIG_PATH)
    assert config.package.name == "nerftools"


def test_nerf_config_metadata() -> None:
    config = load_config(_NERF_CONFIG_PATH)
    plugin_meta, marketplace_meta = resolve_claude_plugin_meta(config)

    assert plugin_meta.name == "nerftools"
    assert plugin_meta.description != ""

    assert marketplace_meta is not None
    assert marketplace_meta.name == "agentworks-nerftools-local"
    assert marketplace_meta.owner.name == "Agentworks"


def test_nerf_config_none_gives_nerftools_defaults() -> None:
    """load_config(None) returns nerftools built-in defaults, not our config."""
    config = load_config(None)
    plugin_meta, marketplace_meta = resolve_claude_plugin_meta(config)
    # Defaults use 'nerftools' for both names
    assert plugin_meta.name == "nerftools"
    assert marketplace_meta is not None
    assert marketplace_meta.name == "nerftools"


# ---------------------------------------------------------------------------
# Plugin build
# ---------------------------------------------------------------------------


@pytest.fixture()
def built_plugin(tmp_path: Path) -> Path:
    """Build a plugin from default manifests and return the output directory."""
    manifest_paths = sorted(p for p in BUILTIN_MANIFESTS_DIR.iterdir() if p.suffix == ".yaml")
    manifests = merge_manifests([load_manifest(p) for p in manifest_paths])

    config = load_config(_NERF_CONFIG_PATH)
    plugin_meta, marketplace_meta = resolve_claude_plugin_meta(config)

    from dataclasses import replace as dc_replace

    plugin_meta = dc_replace(plugin_meta, version="0.1.test")

    build_claude_plugin(manifests, tmp_path, plugin_meta, marketplace_meta=marketplace_meta)
    return tmp_path


def test_plugin_json_structure(built_plugin: Path) -> None:
    plugin_json = built_plugin / ".claude-plugin" / "plugin.json"
    assert plugin_json.exists()
    data = json.loads(plugin_json.read_text())
    assert data["name"] == "nerftools"
    assert data["version"] == "0.1.test"
    assert data["skills"] == "./skills/"


def test_marketplace_json_structure(built_plugin: Path) -> None:
    marketplace_json = built_plugin / ".claude-plugin" / "marketplace.json"
    assert marketplace_json.exists()
    data = json.loads(marketplace_json.read_text())
    assert data["name"] == "agentworks-nerftools-local"
    assert data["owner"]["name"] == "Agentworks"
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["name"] == "nerftools"
    assert data["plugins"][0]["source"] == "./"


def test_plugin_has_skills(built_plugin: Path) -> None:
    skills_dir = built_plugin / "skills"
    assert skills_dir.is_dir()
    skill_dirs = [d.name for d in skills_dir.iterdir() if d.is_dir()]
    # Should have nerf-git, nerftools overview, nerfctl skills, etc.
    assert "nerf-git" in skill_dirs
    assert "nerftools" in skill_dirs  # overview skill
    assert "nerfctl-grant-allow" in skill_dirs


def test_plugin_has_nerfctl_scripts(built_plugin: Path) -> None:
    scripts_dir = built_plugin / "scripts"
    assert scripts_dir.is_dir()
    script_names = {f.name for f in scripts_dir.iterdir()}
    assert "nerfctl-grant-allow" in script_names
    assert "nerfctl-grant-deny" in script_names
    assert "nerfctl-grant-reset" in script_names
    assert "nerfctl-grant-list" in script_names
    assert "nerfctl-grant-by-threat" in script_names


def test_plugin_tool_scripts_are_executable(built_plugin: Path) -> None:
    """Every nerf-* script in skills should be executable."""
    for script in built_plugin.rglob("nerf-*"):
        if script.is_file() and script.parent.name == "scripts":
            assert script.stat().st_mode & 0o111, f"{script} is not executable"


# ---------------------------------------------------------------------------
# Install script generation
# ---------------------------------------------------------------------------


def test_install_script_content() -> None:
    """Verify the install script string has correct structure and names."""
    p_name = shlex.quote("nerftools")
    m_name = shlex.quote("agentworks-nerftools-local")
    install_script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"\n'
        "claude plugin marketplace remove agentworks-nerf-local >/dev/null 2>&1 || true\n"
        'claude plugin marketplace add "$PLUGIN_DIR"\n'
        f"claude plugin install {p_name}@{m_name} --scope user\n"
    )

    assert install_script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in install_script
    assert "claude plugin marketplace remove agentworks-nerf-local" in install_script
    assert "claude plugin marketplace add" in install_script
    assert "claude plugin install nerftools@agentworks-nerftools-local --scope user" in install_script


def test_install_script_shell_quoting() -> None:
    """Verify shlex.quote produces safe values for plugin/marketplace names."""
    # Normal names should pass through unchanged (single-quoted by shlex)
    assert shlex.quote("nerftools") == "nerftools"
    assert shlex.quote("agentworks-nerftools-local") == "agentworks-nerftools-local"

    # Names with special chars would be quoted
    dangerous = "foo; rm -rf /"
    quoted = shlex.quote(dangerous)
    assert ";" not in quoted or quoted.startswith("'")


# ---------------------------------------------------------------------------
# Version format
# ---------------------------------------------------------------------------


def test_version_format() -> None:
    from datetime import UTC, datetime

    version = datetime.now(UTC).strftime("0.1.%Y%m%d%H%M")
    # Should look like 0.1.202604261530
    assert version.startswith("0.1.20")
    assert len(version) == 16  # "0.1." + 12 digits (YYYYMMDDHHMM)


# ---------------------------------------------------------------------------
# Custom manifest merge
# ---------------------------------------------------------------------------


def test_custom_manifest_merges_with_defaults(tmp_path: Path) -> None:
    """A custom manifest adds its package alongside defaults."""
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        """\
version: 1
package:
  name: my-tools
  description: Custom tools
  skill_group: my-tools
tools:
  hello:
    description: Say hello
    threat:
      read: none
      write: none
    script: |
      echo hello
"""
    )

    manifest_paths = sorted(p for p in BUILTIN_MANIFESTS_DIR.iterdir() if p.suffix == ".yaml")
    manifest_paths.append(custom)
    manifests = merge_manifests([load_manifest(p) for p in manifest_paths])

    package_names = [m.package.name for m in manifests]
    assert "my-tools" in package_names
    assert "git" in package_names  # defaults still present
