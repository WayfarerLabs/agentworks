"""Machine discovery and instruction selection at actual native scope boundaries."""

import pytest

from tests.artifacts.test_native_probe import probe
from tests.conftest import requires_posix_shell

pytestmark = requires_posix_shell


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_machine_inventory_detects_native_name_collisions(tmp_path, tool):
    root = tmp_path / ("etc/claude-code/.claude" if tool == "claude" else "etc/codex")
    skill = root / "skills/other-path/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Machine skill\n---\nbody\n")
    assert "native-artifact-collision" in probe(tmp_path, tool=tool)["problems"]
    assert "native-artifact-collision" in probe(tmp_path, tool=tool, workspace_only=True)["problems"]
    assert "native-artifact-collision" in probe(tmp_path, tool=tool, vm_only=True)["problems"]
    assert probe(tmp_path, tool=tool, identities=("skill:other",))["problems"] == []


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_vm_probe_ignores_admin_home_policy_and_missing_native_command(tmp_path, tool):
    settings = {"skills": {"include_instructions": False}} if tool == "codex" else {"claudeMdExcludes": ["**"]}
    result = probe(
        tmp_path,
        tool=tool,
        settings=settings,
        vm_only=True,
        environment={"PATH": "", "CODEX_HOME": "relative", "CLAUDE_CONFIG_DIR": "relative"},
    )
    assert result["problems"] == []
    assert result["native_home"].startswith(str(tmp_path / "etc"))


def test_codex_disabled_agents_are_not_claimed_as_discovered(tmp_path):
    assert (
        "native-agent-policy"
        in probe(tmp_path, settings={"agents": {"enabled": False}}, identities=("agent:review",))["problems"]
    )
    assert probe(tmp_path, settings={"agents": {"enabled": False}}, identities=("skill:review",))["problems"] == []


@pytest.mark.parametrize("scope", ["home/.codex", "workspace"])
def test_codex_detects_new_override_shadowing_previously_applied_guidance(tmp_path, scope):
    directory = tmp_path / scope
    directory.mkdir(parents=True)
    ordinary = directory / "AGENTS.md"
    ordinary.write_text("Managed instructions")
    override = directory / "AGENTS.override.md"
    override.write_text("User override")
    assert "native-guidance-shadowed" in probe(tmp_path, paths=(str(ordinary),), identities=())["problems"]
    assert probe(tmp_path, paths=(str(override),), identities=())["problems"] == []
    override.write_text("")
    assert probe(tmp_path, paths=(str(ordinary),), identities=())["problems"] == []
