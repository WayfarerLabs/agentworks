"""Guidance projection retains identity, scope and supported package members."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import PurePosixPath

import pytest
import yaml

from agentworks.artifacts.application import ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.guidance import guidance_skills
from agentworks.artifacts.publication import validate_application
from agentworks.artifacts.session import validate_session_application
from agentworks.errors import StateError
from agentworks.plugins.codex import artifacts as codex
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context


def metadata(file: ArtifactFile) -> dict:
    value = yaml.safe_load(file.data.decode().split("---", 2)[1])
    assert isinstance(value, dict)
    return value


def test_same_named_rules_and_hints_from_different_scopes_have_distinct_owned_packages():
    first = artifact(ArtifactType.RULE, owner="one")
    second = artifact(ArtifactType.RULE, owner="two")
    hints = [artifact(ArtifactType.HINT, owner=owner, name=name) for owner in ("one", "two") for name in ("a", "b")]
    files = guidance_skills("/skills", received(first, second, *hints))
    entries = [file for file in files if file.path.endswith("/SKILL.md")]
    assert len(entries) == 4
    assert len({file.native_identity for file in files}) == 4
    assert sorted(len(file.origins) for file in entries) == [1, 1, 2, 2]
    assert {origin for file in files for origin in file.origins} == {
        item.origin_identity for item in (first, second, *hints)
    }
    for file in entries:
        name = PurePosixPath(file.package_root).name
        assert metadata(file)["name"] == name and len(name) <= 64
        assert file.native_identity == "skill:" + name
    assert first.content.text.encode() == next(
        file.data for file in files if file.origins == (first.origin_identity,) and file.path.endswith("/guidance.md")
    )


@pytest.mark.parametrize(
    "integration,skills",
    [("claude-code", False), ("claude-code", True), ("codex", True), ("grok-build", False), ("grok-build", True)],
)
def test_long_rule_keys_with_the_same_readable_prefix_keep_distinct_native_outputs(integration, skills):
    from agentworks.plugins.claude import artifacts as claude
    from agentworks.plugins.grok import artifacts as grok

    items = tuple(artifact(ArtifactType.RULE, name="r" * 40 + suffix) for suffix in ("-one", "-two"))
    inputs = received(*items)
    if integration == "codex":
        plan = codex.outer_artifacts(inputs, skills_root="/native/skills", agents_root="/native/agents")
    elif integration == "claude-code":
        plan = claude.outer_artifacts(inputs, "/native", guidance_as_skills=skills)
    else:
        plan = grok.outer_artifacts(inputs, "/native", workspace=skills)
    validate_application(plan, inputs, "user", integration=integration)
    assert not plan.deferred and len(plan.files) == (4 if skills else 2)
    assert len({file.path for file in plan.files}) == len(plan.files)
    assert {file.origins for file in plan.files} == {(item.origin_identity,) for item in items}
    if skills:
        assert len({file.native_identity for file in plan.files}) == 2
        for file in plan.files:
            if not file.path.endswith("/SKILL.md"):
                continue
            name = metadata(file)["name"]
            assert name == PurePosixPath(file.package_root).name and len(name) <= 64
            assert file.native_identity == "skill:" + name
    else:
        assert all(len(PurePosixPath(file.path).stem) <= 64 for file in plan.files)


def test_owner_hint_aggregate_keeps_one_native_name_when_its_keys_change():
    first = artifact(ArtifactType.HINT, name="first")
    second = artifact(ArtifactType.HINT, name="second")
    before = guidance_skills("/skills", received(first))[0]
    after = guidance_skills("/skills", received(second))[0]
    assert before.path == after.path and before.native_identity == after.native_identity
    assert before.origins == (first.origin_identity,) and after.origins == (second.origin_identity,)


def test_session_and_run_qualification_prevents_package_overlap_and_preserves_assets():
    item = artifact(ArtifactType.SKILL)
    initial = context(item)
    other_session = replace(initial, session_uuid="other")
    next_run = replace(initial, run_id="next")
    paths = []
    for current in (initial, other_session, next_run):
        plan = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-home-skills"])
        validate_session_application(plan.application, current, integration="codex")
        paths.append({file.path for file in plan.application.files})
        entry = next(file for file in plan.application.files if file.path.endswith("/SKILL.md"))
        values = metadata(entry)
        assert values["name"] == PurePosixPath(entry.package_root).name
        assert len(values["name"]) <= 64
        assert current.session_uuid in values["description"] and current.run_id in values["description"]
        assert item.origin.resource_name in values["description"]
        for member in item.content.members:
            if member.path != "SKILL.md":
                actual = next(file for file in plan.application.files if file.path.endswith("/" + member.path))
                assert (actual.data, actual.executable) == (member.data, member.executable)
    assert all(not left.intersection(right) for index, left in enumerate(paths) for right in paths[index + 1 :])


@pytest.mark.parametrize("change", ["outside-home", "other-session", "other-run", "identity", "package", "section"])
def test_home_publication_refuses_unrelated_or_forged_packages(change):
    item = artifact(ArtifactType.RULE)
    current = context(item)
    plan = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-home-skills"])
    file = plan.application.files[0]
    package = file.package_root
    assert package is not None
    if change == "outside-home":
        file = replace(
            file,
            path=file.path.replace(current.home, "/outside", 1),
            package_root=package.replace(current.home, "/outside", 1),
        )
    elif change in ("other-session", "other-run"):
        foreign = (
            replace(current, session_uuid="foreign")
            if change == "other-session"
            else replace(current, run_id="foreign")
        )
        file = codex.session_artifacts(
            foreign, extra_args=[], enabled_workarounds=["session-home-skills"]
        ).application.files[0]
    elif change == "identity":
        file = replace(file, native_identity="skill:foreign")
    elif change == "package":
        file = replace(file, package_root=None)
    else:
        file = replace(file, generated_section=True)
    with pytest.raises(StateError):
        validate_session_application(replace(plan.application, files=(file,)), current, integration="codex")


def test_ordinary_home_file_without_a_native_skill_package_is_never_authorized():
    item = artifact(ArtifactType.RULE)
    current = context(item)
    plan = codex.session_artifacts(current, extra_args=[], enabled_workarounds=["session-home-skills"])
    arbitrary = ArtifactFile(current.home + "/.profile", b"changed", (item.origin_identity,))
    with pytest.raises(StateError):
        validate_session_application(replace(plan.application, files=(arbitrary,)), current, integration="codex")


@pytest.mark.parametrize("selection,delivered", [("review", True), ("review", False), (None, True)])
def test_explicit_claude_persona_selection_tracks_only_its_delivered_native_identity(selection, delivered):
    import shlex

    from agentworks.plugins.claude.artifacts import session_artifacts
    from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration

    item = artifact(ArtifactType.AGENT)
    current = context(item)
    integration = ClaudeCodeIntegration(
        "claude-code",
        {"agent": selection},
        session_name="s1",
        vm_name="vm",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=current,
    )
    integration._artifact_plan = session_artifacts(
        current, extra_args=[], enabled_workarounds=["session-agent-definitions"] if delivered else []
    )
    command = integration._resume_or_launch("11111111-2222-4333-8444-555555555555", resume=False)
    argv = shlex.split(shlex.split(command)[2])
    if selection is None:
        assert "--agent" not in argv
    elif delivered:
        identity = next(
            file.native_identity.removeprefix("agent:")
            for file in integration._artifact_plan.application.files
            if file.native_identity and file.native_identity.startswith("agent:")
        )
        assert argv[argv.index("--agent") + 1] == identity
    else:
        assert argv[argv.index("--agent") + 1] == selection


def test_generated_helper_collision_defers_both_origins_and_keeps_independent_guidance(monkeypatch):
    from agentworks.artifacts.native.common import prepare_session_delivery
    from agentworks.plugins.claude.artifacts import session_artifacts

    rule = artifact(ArtifactType.RULE)
    generated = guidance_skills("/skills", received(rule))[0]
    colliding = artifact(ArtifactType.SKILL, name=metadata(generated)["name"])
    hint = artifact(ArtifactType.HINT, name="independent")
    current = context(rule, colliding, hint)
    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *a, **k: "/home/alice/.claude")
    result, _ = prepare_session_delivery(
        object(),
        tool="claude",
        integration="claude-code",
        context=current,
        render=lambda prepared: session_artifacts(
            prepared, extra_args=[], enabled_workarounds=["session-skill-plugin"]
        ),
        workspace="/ws",
    )
    assert {deferred.input_id for deferred in result.application.deferred} == {rule.identity, colliding.identity}
    assert {origin for file in result.application.files for origin in file.origins} == {hint.origin_identity}


@pytest.mark.parametrize("artifact_type", [ArtifactType.RULE, ArtifactType.HINT])
@pytest.mark.parametrize("session", [False, True])
def test_generated_guidance_keeps_native_template_syntax_in_literal_supporting_data(artifact_type, session):
    import hashlib

    from agentworks.artifacts.capture import text_content

    text = (
        "Literal fixture.\n!`printf 'LITERAL_%s' DATA`\n"
        "```\n!`printf 'FENCED_%s' DATA`\n```\n"
        "$ARGUMENTS $1 ${CLAUDE_SESSION_ID} ${agw:session_name}\n"
    )
    item = replace(artifact(artifact_type), content=text_content(artifact_type, "review", text))
    current = context(item) if session else None
    files = guidance_skills("/skills", received(item), context=current)
    entry = next(file for file in files if file.path.endswith("/SKILL.md"))
    data = next(file for file in files if file.path.endswith("/guidance.md"))
    expected = text if artifact_type is ArtifactType.RULE else f"## {item.content.name}\n\n{text}"
    assert data.data == expected.encode()
    assert text.encode() not in entry.data
    assert hashlib.sha256(data.data).hexdigest().encode() in entry.data
    assert {file.package_root for file in files} == {entry.package_root}
    assert {file.origins for file in files} == {(item.origin_identity,)}
    assert {file.native_identity for file in files} == {entry.native_identity}
    assert metadata(entry).keys() == {"name", "description"}
    assert metadata(entry)["name"] == PurePosixPath(entry.package_root).name
    changed = replace(item, content=text_content(artifact_type, "review", text + "New revision.\n"))
    after = guidance_skills("/skills", received(changed), context=context(changed) if session else None)
    updated = next(file for file in after if file.path.endswith("/SKILL.md"))
    assert updated.path == entry.path and updated.native_identity == entry.native_identity
    assert updated.data != entry.data


def test_generated_guidance_cannot_interpolate_freeform_rule_description():
    from agentworks.artifacts.capture import text_content

    text = '---\ndescription: "!`printf data` $ARGUMENTS"\n---\nLiteral body.\n'
    item = replace(artifact(ArtifactType.RULE), content=text_content(ArtifactType.RULE, "review", text))
    files = guidance_skills("/skills", received(item))
    entry = next(file for file in files if file.path.endswith("/SKILL.md"))
    assert item.content.description not in metadata(entry)["description"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")
def test_generated_guidance_supporting_file_is_updated_and_retired_with_its_entrypoint(tmp_path):
    from pathlib import Path

    from agentworks.artifacts.publication import publish_artifacts
    from tests.native_setup_fixtures import LocalFixtureTransport

    target = LocalFixtureTransport(tmp_path / "guest")
    item = artifact(ArtifactType.RULE)
    initial = guidance_skills(str(target.home / "skills"), received(item))
    owned = publish_artifacts(target, initial, (), lambda files: None, roots=(str(target.home),)).files
    assert len(owned) == 2
    changed = replace(item, content=replace(item.content, text="Revised literal body.\n"))
    desired = guidance_skills(str(target.home / "skills"), received(changed))
    updated = publish_artifacts(target, desired, owned, lambda files: None, roots=(str(target.home),)).files
    assert {file.path for file in owned} == {file.path for file in updated}
    for file in desired:
        assert Path(file.path).read_bytes() == file.data
    assert all(before.sha256 != after.sha256 for before, after in zip(owned, updated, strict=True))
    retired = publish_artifacts(target, (), updated, lambda files: None, roots=(str(target.home),))
    assert not retired.files and not retired.skipped
    assert all(not Path(file.path).exists() for file in desired)
