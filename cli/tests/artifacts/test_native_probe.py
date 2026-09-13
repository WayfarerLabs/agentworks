"""Exercise the target-side native preflight against controlled local files/tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import MAX_CODEX_PERSONA_BYTES
from agentworks.artifacts.native.probe import _PROBE, _inventory_problems
from agentworks.package_sources import CaptureLimits
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
    inventory_bytes: int = CaptureLimits().total_bytes,
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
        "codex_persona_bytes": MAX_CODEX_PERSONA_BYTES,
        "inventory_bytes": inventory_bytes,
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
    observed["problems"].extend(
        problem.code for problem in _inventory_problems(observed["inventory"], identities, entries or {})
    )
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


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_inventory_uses_native_metadata_name_instead_of_unmanaged_filename(tmp_path, tool, kind):
    native = ".claude" if tool == "claude" else ".codex" if tool == "codex" else ".grok"
    if kind == "skill":
        path = (
            tmp_path
            / "workspace"
            / (".agents" if tool == "codex" else native)
            / "skills"
            / "other-directory"
            / "SKILL.md"
        )
    else:
        path = tmp_path / "workspace" / native / "agents" / ("different.toml" if tool == "codex" else "different.md")
    path.parent.mkdir(parents=True)
    path.write_text(
        'name = "review"\ndeveloper_instructions = "unmanaged"\n'
        if tool == "codex" and kind == "agent"
        else "---\nname: 'review'\ndescription: Unmanaged\n---\nbody\n"
    )
    result = probe(tmp_path, tool=tool, identities=(f"{kind}:review",))
    assert "native-artifact-collision" in result["problems"]
    assert "body" not in json.dumps(result["inventory"])
    unrelated = probe(tmp_path, tool=tool, identities=(f"{kind}:unrelated",))
    assert unrelated["problems"] == []


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_owned_entry_is_exempt_only_when_its_current_native_name_matches(tmp_path, tool):
    native = ".claude" if tool == "claude" else ".codex" if tool == "codex" else ".grok"
    path = tmp_path / "home" / native / "agents" / ("review.toml" if tool == "codex" else "review.md")
    path.parent.mkdir(parents=True)
    content = 'name = "review"\n' if tool == "codex" else "---\nname: review\n---\nbody\n"
    path.write_text(content)
    entries = {str(path): "agent:review"}
    assert (
        probe(tmp_path, tool=tool, identities=("agent:review",), paths=(str(path),), entries=entries)["problems"] == []
    )
    path.write_text(content.replace("review", "renamed"))
    assert (
        "native-artifact-identity-mismatch"
        in probe(tmp_path, tool=tool, identities=("agent:review",), paths=(str(path),), entries=entries)["problems"]
    )


def test_config_registered_codex_role_cannot_override_selected_persona(tmp_path):
    configured = {"agents": {"review": {"config_file": "/unmanaged/review.toml"}}}
    assert "native-artifact-collision" in probe(tmp_path, settings=configured, identities=("agent:review",))["problems"]
    assert (
        probe(
            tmp_path,
            settings=configured,
            identities=("agent:review",),
            entries={"/unmanaged/review.toml": "agent:review"},
        )["problems"]
        == []
    )


@pytest.mark.parametrize("layout", ["nested", "symlink", "bad-yaml"])
def test_ambiguous_native_inventory_refuses_instead_of_skipping_candidates(tmp_path, layout):
    root = tmp_path / "workspace/.claude/agents"
    root.mkdir(parents=True)
    if layout == "nested":
        (root / "nested").mkdir()
        (root / "nested/agent.md").write_text("---\nname: review\n---\nbody\n")
    elif layout == "symlink":
        outside = tmp_path / "other.md"
        outside.write_text("---\nname: review\n---\nbody\n")
        (root / "agent.md").symlink_to(outside)
    else:
        (root / "agent.md").write_text("---\nname: [invalid\n---\nbody\n")
    assert "unreadable-native-inventory" in probe(tmp_path, tool="claude", identities=("agent:review",))["problems"]
    # An unselected artifact type does not widen preflight's inventory.
    assert probe(tmp_path, tool="claude", identities=("skill:unrelated",))["problems"] == []


@pytest.mark.parametrize("kind", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_launch_checks_unmanaged_workspace_collision_with_only_handled_ancestor(tmp_path, monkeypatch, kind):
    import shlex
    from types import SimpleNamespace

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInputs
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.errors import StateError
    from agentworks.plugins.claude.artifacts import outer_artifacts
    from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
    from tests.artifacts._fixtures import received
    from tests.artifacts.test_native_delivery import owned

    home, workspace = tmp_path / "home", tmp_path / "workspace"
    item = artifact(kind)
    application = outer_artifacts(received(item), str(home / ".claude"))
    for file in application.files:
        path = Path(file.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.data)
    collision = (
        workspace / ".claude" / ("skills/other-name/SKILL.md" if kind is ArtifactType.SKILL else "agents/other-name.md")
    )
    collision.parent.mkdir(parents=True)
    collision.write_text("---\nname: review\ndescription: Other definition\n---\nbody\n")
    probe(tmp_path, tool="claude", identities=())  # Provision only the fake native executable.
    environment = {
        "HOME": str(home),
        "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    }
    observed_requests = []

    def run(command, *, env, **kwargs):
        argv = shlex.split(shlex.split(command)[-1])
        observed_requests.append(json.loads(argv[-1]))
        return subprocess.run(argv, env={**os.environ, **env}, capture_output=True, text=True, check=False)

    prepared = SessionArtifactContext(
        inputs=ArtifactInputs(),
        home=str(home),
        directory=str(home / "private"),
        session_uuid="u",
        run_id="r",
        ancestor_files=tuple(owned(file) for file in application.files),
        environment=environment,
    )
    integration = ClaudeCodeIntegration(
        "claude",
        {},
        session_name="s1",
        vm_name="vm",
        workspace_name="ws",
        workspace_path=str(workspace),
        target=None,
        admin=True,
        state={},
        artifact_context=prepared,
    )
    monkeypatch.setattr(
        integration, "_resume_or_launch", lambda *args, **kwargs: pytest.fail("launch reached after collision")
    )
    with pytest.raises(StateError):
        integration.start(RunContext(admin_target=SimpleNamespace(run=run)), intent=HarnessLaunchIntent.CREATE)
    assert len(observed_requests) == 1
    assert not prepared.inputs
    assert set(observed_requests[0]["entries"]) == {file.path for file in application.files if file.native_identity}
    assert collision.exists()


def test_codex_registered_role_layer_without_name_is_valid_inventory(tmp_path):
    path = tmp_path / "home/.codex/agents/role-layer.toml"
    path.parent.mkdir(parents=True)
    path.write_text('developer_instructions = "Role layer without auto-discovery metadata"\n')
    settings = {"agents": {"review": {"config_file": "agents/role-layer.toml"}}}
    assert probe(tmp_path, settings=settings, identities=("agent:unrelated",))["problems"] == []
    assert (
        probe(tmp_path, settings=settings, identities=("agent:review",), entries={str(path): "agent:review"})[
            "problems"
        ]
        == []
    )
    assert "native-artifact-collision" in probe(tmp_path, settings=settings, identities=("agent:review",))["problems"]


def test_codex_generated_outer_persona_above_header_limit_remains_discoverable(tmp_path):
    from dataclasses import replace

    from agentworks.plugins.codex.artifacts import outer_artifacts
    from tests.artifacts._fixtures import received

    item = artifact(ArtifactType.AGENT)
    item = replace(item, content=replace(item.content, text="Long instructions.\n" * 4000))
    application = outer_artifacts(
        received(item),
        skills_root=str(tmp_path / "home/.agents/skills"),
        agents_root=str(tmp_path / "home/.codex/agents"),
    )
    file = application.files[0]
    assert len(file.data) > 32768
    path = Path(file.path)
    path.parent.mkdir(parents=True)
    path.write_bytes(file.data)
    assert probe(tmp_path, identities=("agent:review",), entries={file.path: "agent:review"})["problems"] == []


def test_inventory_collision_retains_native_identity_and_competing_path():
    path = "/workspace/.claude/agents/alternate.md"
    problems = _inventory_problems(
        [{"kind": "agent", "path": path, "frontmatter": "name: review"}],
        ("agent:review",),
        {"/home/alice/.claude/agents/review.md": "agent:review"},
    )
    assert len(problems) == 1
    assert (problems[0].identity, problems[0].path) == ("agent:review", path)


def test_codex_inventory_bounds_combined_toml_bytes(tmp_path):
    root = tmp_path / "home/.codex/agents"
    root.mkdir(parents=True)
    body = 'name = "unrelated"\ndeveloper_instructions = "instructions"\n'
    for name in ("first", "second"):
        (root / f"{name}.toml").write_text(body)
    total = 2 * len(body.encode())
    assert probe(tmp_path, identities=("agent:review",), inventory_bytes=total)["problems"] == []
    assert (
        "unreadable-native-inventory"
        in probe(tmp_path, identities=("agent:review",), inventory_bytes=total - 1)["problems"]
    )


def test_removed_skill_support_directories_do_not_block_remaining_native_skill(tmp_path):
    from agentworks.native_files import NativeFiles
    from tests.native_setup_fixtures import LocalFixtureTransport

    transport = LocalFixtureTransport(tmp_path)
    root = tmp_path / "home/.claude/skills"
    removed = root / "removed"
    removed.mkdir(parents=True)
    (removed / "SKILL.md").write_text("---\nname: removed\n---\nbody\n")
    (removed / "scripts/nested").mkdir(parents=True)
    (removed / "scripts/nested/check.sh").write_text("exit 0\n")
    remaining = root / "review/SKILL.md"
    remaining.parent.mkdir()
    remaining.write_text("---\nname: review\n---\nbody\n")
    # NativeFiles owns whole files, so successful cleanup preserves support directories.
    native = NativeFiles(transport)
    for path in (removed / "SKILL.md", removed / "scripts/nested/check.sh"):
        import hashlib

        native.remove(str(path), expected=hashlib.sha256(path.read_bytes()).hexdigest())
    assert (removed / "scripts/nested").is_dir()
    assert probe(tmp_path, tool="claude", entries={str(remaining): "skill:review"})["problems"] == []


@pytest.mark.parametrize("tool,native", [("claude", ".claude"), ("codex", ".codex"), ("grok", ".grok")])
def test_workspace_native_parent_symlink_is_refused_before_reading_metadata(tmp_path, tool, native):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    (outside / "agents").mkdir(parents=True)
    marker = outside / "agents" / ("other.toml" if tool == "codex" else "other.md")
    marker.write_text('name = "review"\n' if tool == "codex" else "---\nname: review\n---\n")
    (workspace / native).symlink_to(outside, target_is_directory=True)
    result = probe(tmp_path, tool=tool, identities=("agent:review",))
    assert "unreadable-native-inventory" in result["problems"]
    assert result["inventory"] == []


@pytest.mark.parametrize("scope", ["home", "workspace"])
def test_codex_skills_parent_symlink_is_refused_before_reading_metadata(tmp_path, scope):
    anchor = tmp_path / scope
    anchor.mkdir()
    outside = tmp_path / "outside"
    (outside / "skills/review").mkdir(parents=True)
    (outside / "skills/review/SKILL.md").write_text("---\nname: review\n---\n")
    (anchor / ".agents").symlink_to(outside, target_is_directory=True)
    result = probe(tmp_path)
    assert "unreadable-native-inventory" in result["problems"]
    assert result["inventory"] == []


@pytest.mark.parametrize(
    "tool,variable", [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("grok", "GROK_HOME")]
)
def test_session_probe_accepts_explicit_external_native_home(tmp_path, tool, variable):
    external = tmp_path / "external-native"
    external.mkdir()
    result = probe(
        tmp_path,
        tool=tool,
        identities=(),
        paths=(str(tmp_path / "home/private-run/context.md"),),
        environment={variable: str(external)},
    )
    assert result["native_home"] == str(external)
    assert result["problems"] == []
