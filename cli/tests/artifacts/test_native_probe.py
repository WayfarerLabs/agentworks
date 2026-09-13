"""Exercise the target-side native preflight against controlled local files/tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.probe import _PROBE, _inventory_problems
from agentworks.plugins.claude.artifacts import session_artifacts
from tests.artifacts.test_native_delivery import artifact, context
from tests.conftest import requires_posix_shell

pytestmark = requires_posix_shell


def probe(
    tmp_path: Path,
    *,
    tool: str = "codex",
    settings: dict | None = None,
    identities: tuple[str, ...] = ("skill:review",),
    paths: tuple[str, ...] = (),
    entries: dict[str, str] | None = None,
    workspace_only: bool = False,
    flags: tuple[str, ...] = (),
    session_plugin: bool = False,
    version: str = "99.1.0",
    help_text: str = "--rules --agents --config",
    environment: dict[str, str] | None = None,
) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    native = home / (".claude" if tool == "claude" else ".codex" if tool == "codex" else ".grok")
    native.mkdir(exist_ok=True)
    if settings is not None:
        if tool == "claude":
            (native / "settings.json").write_text(json.dumps(settings))
        else:
            import tomli_w

            (native / "config.toml").write_text(tomli_w.dumps(settings))
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    executable = binary / tool
    executable.write_text(
        f'#!{sys.executable}\nimport sys\nprint({version!r} if "--version" in sys.argv else {help_text!r})\n'
    )
    executable.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    request = {
        "tool": tool,
        "home": str(home),
        "workspace": str(workspace),
        "paths": paths,
        "entries": entries or {},
        "flags": flags,
        "session_plugin": session_plugin,
        "workspace_only": workspace_only,
        "identities": identities,
        "check_policy": True,
    }
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "CODEX_HOME": str(native),
        "CLAUDE_CONFIG_DIR": str(native),
        "GROK_HOME": str(native),
        **(environment or {}),
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(request)], env=env, capture_output=True, text=True, check=True
    )
    observed = json.loads(result.stdout.removeprefix("AGW_ARTIFACT_PROBE="))
    assert isinstance(observed, dict)
    observed["problems"].extend(_inventory_problems(observed["inventory"], identities, entries or {}))
    return observed


@pytest.mark.parametrize(
    "tool,settings",
    [
        ("codex", {"skills": {"config": [{"name": "unrelated", "enabled": False}]}}),
        ("grok", {"skills": {"disabled": ["unrelated"]}, "subagents": {"toggle": {"unrelated": False}}}),
        ("claude", {"enabledPlugins": {"unrelated@market": False}}),
    ],
)
def test_unrelated_disabled_artifacts_do_not_block_requested_delivery(tmp_path, tool, settings):
    result = probe(tmp_path, tool=tool, settings=settings, session_plugin=True)
    assert result["problems"] == []


@pytest.mark.parametrize(
    "tool,settings",
    [
        ("codex", {"skills": {"config": [{"name": "review", "enabled": False}]}}),
        ("grok", {"skills": {"disabled": ["review"]}}),
    ],
)
def test_selected_disabled_skill_is_not_claimed_as_discovered(tmp_path, tool, settings):
    assert "native-skill-policy" in probe(tmp_path, tool=tool, settings=settings)["problems"]


def test_rule_exclusions_are_checked_against_actual_paths(tmp_path):
    path = str(tmp_path / "home/.claude/rules/agentworks-rule-review.md")
    assert (
        probe(tmp_path, tool="claude", settings={"claudeMdExcludes": ["**/other-project/**"]}, paths=(path,))[
            "problems"
        ]
        == []
    )
    assert (
        "native-discovery-exclusions"
        in probe(tmp_path, tool="claude", settings={"claudeMdExcludes": ["**/.claude/rules/**"]}, paths=(path,))[
            "problems"
        ]
    )


def test_workspace_preflight_does_not_consult_an_admin_users_disabled_config(tmp_path):
    assert (
        probe(tmp_path, tool="codex", settings={"skills": {"include_instructions": False}}, workspace_only=True)[
            "problems"
        ]
        == []
    )


def test_native_home_from_effective_environment_is_observed(tmp_path):
    expected = str(tmp_path / "custom-native")
    result = probe(tmp_path, environment={"CODEX_HOME": expected})
    assert result["native_home"] == expected


def test_old_or_incompatible_native_binary_is_rejected_before_publication(tmp_path):
    assert "unsupported-native-version" in probe(tmp_path, version="0.100.0")["problems"]
    assert "unsupported-native-cli" in probe(tmp_path, flags=("--unsupported-carrier",))["problems"]


def test_grok_gitignore_check_only_inspects_workspace_owned_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / ".gitignore").write_text(".grok/\n")
    outside = str(tmp_path / "home/.grok/skills/review/SKILL.md")
    assert probe(tmp_path, tool="grok", paths=(outside,))["problems"] == []
    inside = str(workspace / ".grok/skills/review/SKILL.md")
    assert "native-discovery-exclusions" in probe(tmp_path, tool="grok", paths=(inside,))["problems"]


# Carrier spellings from Claude Code 2.1.265 --help, including its compact file suffix.
CLAUDE_CARRIER_HELP = """
  --append-system-prompt <prompt>       Append a system prompt to the default
                                        via: --system-prompt[-file],
                                        --append-system-prompt[-file], --add-dir
                                        (CLAUDE.md dirs), --mcp-config,
                                        --settings, --agents, --plugin-dir.
  --system-prompt-snapshot <on|off>     Record the system prompt once per
"""


@pytest.mark.parametrize("type", [ArtifactType.RULE, ArtifactType.HINT])
@pytest.mark.parametrize("help_text", [CLAUDE_CARRIER_HELP, "--append-system-prompt-file --system-prompt-snapshot"])
def test_claude_help_accepts_generated_text_carriers(tmp_path, type, help_text):
    plan = session_artifacts(context(artifact(type)), configured=None, extra_args=[])
    result = probe(tmp_path, tool="claude", version="2.1.265", help_text=help_text, flags=plan.required_flags)
    assert result["problems"] == []


@pytest.mark.parametrize(
    "help_text,flag",
    [
        ("--append-system-prompt <prompt>", "--append-system-prompt-file"),
        (CLAUDE_CARRIER_HELP, "--unsupported-carrier"),
        (CLAUDE_CARRIER_HELP, "--append-system-prompt-file-unknown"),
        ("--system-prompt-snapshot-extra", "--system-prompt-snapshot"),
    ],
)
def test_claude_help_refuses_absent_or_partial_carrier(tmp_path, help_text, flag):
    assert "unsupported-native-cli" in probe(tmp_path, tool="claude", help_text=help_text, flags=(flag,))["problems"]


def test_claude_supported_carriers_do_not_bypass_plugin_policy(tmp_path):
    result = probe(
        tmp_path,
        tool="claude",
        help_text=CLAUDE_CARRIER_HELP,
        flags=("--append-system-prompt-file", "--system-prompt-snapshot", "--plugin-dir"),
        session_plugin=True,
        settings={"enabledPlugins": {"agentworks-artifacts@fixture": False}},
    )
    assert result["problems"] == ["native-plugin-policy"]
