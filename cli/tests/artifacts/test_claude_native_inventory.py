"""Claude-owned skill storage is opaque to native artifact setup."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native import probe as native_probe
from agentworks.artifacts.native.probe import NativeArtifactConflictError, probe_native
from agentworks.artifacts.publication import publish_artifacts
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.harness_setup.model import SetupRecord
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact
from tests.artifacts.test_native_probe import probe
from tests.conftest import requires_posix_shell
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = requires_posix_shell


@pytest.mark.parametrize("scope", ["user", "workspace", "vm"])
@pytest.mark.parametrize("container", ["synced", "SyNcEd", ".trash", "anthropic-skills", "anthropic-skills:review"])
def test_claude_reserved_skill_containers_are_opaque(tmp_path, scope, container):
    root = {
        "user": tmp_path / "home/.claude",
        "workspace": tmp_path / "workspace/.claude",
        "vm": tmp_path / "etc/claude-code/.claude",
    }[scope]
    reserved = root / "skills" / container
    nested = reserved / "account/organization/morning/SKILL.md"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"---\nname: [invalid native-owned metadata\n")
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (reserved / "external").symlink_to(outside, target_is_directory=True)
    for index in range(513):
        (reserved / f"member-{index}").touch()
    valid = root / "skills/ordinary/SKILL.md"
    valid.parent.mkdir(parents=True)
    valid.write_text("---\nname: ordinary\ndescription: Existing skill\n---\nbody\n")

    result = probe(tmp_path, tool="claude", identities=("skill:new",), vm_only=scope == "vm")

    assert result["problems"] == []
    assert [row["path"] for row in result["inventory"]] == [str(valid)]
    assert nested.read_bytes() == b"---\nname: [invalid native-owned metadata\n"
    assert (reserved / "external").is_symlink()
    assert len(tuple(reserved.glob("member-*"))) == 513


@pytest.mark.parametrize("container", ["synced", "SyNcEd", ".trash", "anthropic-skills", "anthropic-skills:review"])
def test_claude_opaque_skill_container_links_are_not_followed(tmp_path, container):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"invalid native-owned contents")
    root = tmp_path / "home/.claude/skills"
    root.mkdir(parents=True)
    (root / container).symlink_to(outside, target_is_directory=True)

    result = probe(tmp_path, tool="claude")

    assert result["problems"] == []
    assert result["inventory"] == []
    assert (root / container).is_symlink()


@pytest.mark.parametrize("container", ["synced", ".trash"])
def test_claude_reserved_containers_do_not_consume_inventory_capacity(tmp_path, container):
    root = tmp_path / "home/.claude/skills"
    for index in range(512):
        marker = root / f"ordinary-{index}/SKILL.md"
        marker.parent.mkdir(parents=True)
        marker.write_text(f"---\nname: ordinary-{index}\ndescription: Existing skill\n---\nbody\n")
    (root / container).mkdir()
    selected = root / "ordinary-0/SKILL.md"

    result = probe(
        tmp_path,
        tool="claude",
        identities=("skill:ordinary-0",),
        entries={str(selected): "skill:ordinary-0"},
    )

    assert result["problems"] == []
    assert len(result["inventory"]) == 512


def test_claude_ordinary_nested_skill_inventory_remains_invalid(tmp_path):
    marker = tmp_path / "home/.claude/skills/ordinary/nested/SKILL.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("---\nname: review\ndescription: Existing skill\n---\nbody\n")

    result = probe(tmp_path, tool="claude")

    assert result["problems"] == ["unreadable-native-inventory"]


@pytest.mark.parametrize("tool,native", [("codex", ".agents"), ("grok", ".grok")])
@pytest.mark.parametrize("name", ["synced", "anthropic-skills"])
def test_claude_reserved_names_remain_ordinary_skills_for_other_harnesses(tmp_path, tool, native, name):
    marker = tmp_path / "home" / native / "skills" / name / "SKILL.md"
    marker.parent.mkdir(parents=True)
    marker.write_text(f"---\nname: {name}\ndescription: Existing skill\n---\nbody\n")

    result = probe(tmp_path, tool=tool, identities=(f"skill:{name}",))

    assert result["problems"] == ["native-artifact-collision"]
    assert [row["path"] for row in result["inventory"]] == [str(marker)]


@pytest.mark.parametrize("name", ["synced", "anthropic-skills"])
def test_claude_reserved_direct_skill_names_refuse_without_native_inspection(name):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            ok=True,
            stdout=(
                "AGW_ARTIFACT_PROBE="
                + json.dumps({"native_home": "/home/alice/.native", "problems": [], "inventory": []})
                if "input_data" in kwargs
                else "AGW_PYTHON3=available\n"
            ),
            stderr="",
        )

    runner = SimpleNamespace(run=run, logger=None)
    file = ArtifactFile(
        f"/home/alice/.claude/skills/{name}/SKILL.md",
        f"---\nname: {name}\ndescription: Supplied skill\n---\nbody\n".encode(),
        ("a" * 64,),
        native_identity=f"skill:{name}",
    )

    with pytest.raises(NativeArtifactConflictError) as raised:
        probe_native(runner, tool="claude", environment={}, files=(file,), redactions=(name,))

    assert raised.value.identities == (f"skill:{name}",)
    assert name not in str(raised.value)
    assert calls == []


@pytest.mark.parametrize("tool,namespace", [("codex", ""), ("grok", ""), ("claude", "agentworks-artifacts:")])
@pytest.mark.parametrize("name", ["synced", "anthropic-skills"])
def test_reserved_claude_names_are_usable_in_other_native_or_plugin_namespaces(tool, namespace, name):
    def run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            ok=True,
            stdout=(
                "AGW_ARTIFACT_PROBE="
                + json.dumps({"native_home": "/home/alice/.native", "problems": [], "inventory": []})
                if "input_data" in kwargs
                else "AGW_PYTHON3=available\n"
            ),
            stderr="",
        )

    file = ArtifactFile(
        f"/home/alice/.native/skills/{name}/SKILL.md",
        f"---\nname: {name}\ndescription: Supplied skill\n---\nbody\n".encode(),
        ("a" * 64,),
        native_identity=f"skill:{namespace}{name}",
    )

    assert probe_native(SimpleNamespace(run=run, logger=None), tool=tool, environment={}, files=(file,))


@pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest file publication helpers locally")
@pytest.mark.parametrize("reserved_name", ["synced", "anthropic-skills"])
def test_claude_user_reinit_preserves_reserved_storage_and_applies_independent_artifacts(
    tmp_path, db, monkeypatch, reserved_name
):
    target = LocalFixtureTransport(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    executable = bindir / "claude"
    executable.write_text("#!/bin/sh\nexit 91\n")
    executable.chmod(0o700)
    program = native_probe._PROBE
    for guest_path in ("/etc/claude-code/.claude", "/etc/claude-code/managed-settings.json", "/etc/codex"):
        program = program.replace(repr(guest_path), repr(str(tmp_path / guest_path.removeprefix("/"))))
    monkeypatch.setattr(native_probe, "_PROBE", program)
    native = target.home / ".claude"
    synced = native / "skills/synced/account/organization/morning/SKILL.md"
    retired = native / "skills/.trash/retired/SKILL.md"
    for path in (synced, retired):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"---\nname: [native-owned metadata\n")
    originals = {path: path.read_bytes() for path in (synced, retired)}
    supplied = artifact(ArtifactType.SKILL, name=reserved_name)
    supplied = replace(
        supplied,
        content=replace(
            supplied.content,
            members=(
                replace(
                    supplied.content.members[0],
                    data=f"---\nname: {reserved_name}\ndescription: Reserved skill\n---\nbody\n".encode(),
                ),
                *supplied.content.members[1:],
            ),
        ),
    )
    ordinary = artifact(ArtifactType.SKILL)
    rule = artifact(ArtifactType.RULE)
    incomplete: list[str] = []
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "site", "host"),
        runner=target,
        prior=None,
        checkpoint=lambda claims: None,
        report_incomplete=incomplete.append,
        artifacts=received(supplied, ordinary, rule),
        username="fixture",
        home=str(target.home),
    )
    integration = ClaudeCodeIntegration.for_setup(owner_kind="agent", owner_name="fixture", facet="user", config={})
    current: tuple[OwnedArtifactFile, ...] = ()
    for _ in range(2):
        plan = integration.user_init(invocation)
        assert incomplete
        assert [(item.input_id, item.destination) for item in plan.deferred] == [(supplied.identity, "session")]
        assert not any(supplied.origin_identity in file.origins for file in plan.files)
        published = publish_artifacts(target, plan.files, current, lambda files: None, roots=(str(target.home),))
        current = published.files
        assert (native / "skills/review/SKILL.md").read_bytes() == ordinary.content.members[0].data
        assert any(rule.origin_identity in file.origins for file in current)
        assert all(Path(file.path).is_file() for file in current)
        assert {path: path.read_bytes() for path in originals} == originals
        assert not (native / f"skills/{reserved_name}/SKILL.md").exists()
        invocation = replace(
            invocation,
            prior=SetupRecord(
                component="agent",
                integration="claude-code",
                destination_id="a" * 64,
                declaration={},
                artifact_files=current,
            ),
        )
