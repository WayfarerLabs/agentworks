"""A stable native entry discovers the current run's ordinary private index."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentworks.artifacts.application import ArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import NativeSessionArtifacts
from agentworks.artifacts.session import validate_session_application
from agentworks.capabilities.harness_integration import HarnessStart, ShellIntegration
from agentworks.plugins.grok.artifacts import session_artifacts
from tests.artifacts.test_native_delivery import artifact, context
from tests.artifacts.test_session_lifecycle import lifecycle as lifecycle


def render(current: SessionArtifactContext) -> NativeSessionArtifacts:
    return session_artifacts(current, extra_args=[], enabled_workarounds=["session-home-skills"])


def entry(plan: NativeSessionArtifacts) -> ArtifactFile:
    return next(file for file in plan.application.files if file.native_identity is not None)


def test_entry_is_static_across_runs_and_current_private_index_preserves_type_maps():
    first = context(
        artifact(ArtifactType.RULE),
        artifact(ArtifactType.HINT, name="first"),
        artifact(ArtifactType.HINT, name="second"),
        artifact(ArtifactType.SKILL),
    )
    second = replace(first, run_id="next-run", directory=first.directory + "-next")
    old, new = render(first), render(second)
    assert (entry(old).path, entry(old).data, entry(old).native_identity) == (
        entry(new).path,
        entry(new).data,
        entry(new).native_identity,
    )
    assert old.application.artifacts_dir == first.directory and new.application.artifacts_dir == second.directory
    assert first.directory.encode() not in entry(old).data and second.directory.encode() not in entry(new).data
    for current, plan in ((first, old), (second, new)):
        validate_session_application(plan.application, current, integration="grok-build")
        index = json.loads(next(file.data for file in plan.application.files if file.path.endswith("/index.json")))
        group = index["groups"][0]
        assert (
            set(group["rules"]) == {"review"}
            and set(group["skills"]) == {"review"}
            and set(group["hints"]) == {"first", "second"}
        )
        assert group["skills"]["review"]["description"] == artifact(ArtifactType.SKILL).content.description
        assert group["hints"]["first"]["files"] == group["hints"]["second"]["files"]
        canonical = next(
            file
            for file in plan.application.files
            if file.path.startswith(current.directory + "/") and file.path.endswith("/SKILL.md")
        )
        assert canonical.data == artifact(ArtifactType.SKILL).content.members[0].data
        assert canonical.native_identity is None
        assert len([file for file in plan.application.files if file.native_identity]) == 1


@pytest.mark.parametrize(
    "field,value", [("paths", ["*.py"]), ("disable-model-invocation", True), ("user-invocable", False)]
)
def test_indexed_skill_with_native_activation_controls_is_unhandled(field, value):
    skill = artifact(ArtifactType.SKILL)
    skill = replace(skill, content=replace(skill.content, metadata_json=json.dumps({field: value})))
    rule = artifact(ArtifactType.RULE)
    plan = render(context(skill, rule))
    assert {item.input_id for item in plan.application.deferred} == {skill.identity}
    assert all(skill.origin_identity not in file.origins for file in plan.application.files)
    assert any(rule.origin_identity in file.origins for file in plan.application.files)


def test_changed_private_artifacts_reuse_exactly_one_owned_entry_through_restart(lifecycle, monkeypatch):
    def start(integration, *args, **kwargs):
        return HarnessStart("", artifacts=render(integration._session_binding.artifact_context).application)

    monkeypatch.setattr(ShellIntegration, "start", start)
    lifecycle.restart()
    files = lifecycle.files()
    helper = next(path for path in files if "/.grok/skills/" in str(path))
    before = helper.read_bytes()
    old_private = tuple(path for path in files if path != helper)
    lifecycle.restart()
    after = lifecycle.files()
    assert helper in after and helper.read_bytes() == before
    assert sum(path == helper for path in after) == 1
    assert all(not path.exists() for path in old_private)
    assert all(path.exists() for path in after)
