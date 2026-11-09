"""Guidance projection retains identity, scope and supported package members."""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

import pytest
import yaml

from agentworks.artifacts.application import ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.guidance import guidance_skills
from agentworks.artifacts.session import validate_session_application
from agentworks.errors import StateError
from agentworks.plugins.codex import artifacts as codex
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context


def metadata(file):
    return yaml.safe_load(file.data.decode().split("---", 2)[1])


def test_same_named_rules_and_hints_from_different_scopes_have_distinct_owned_packages():
    first = artifact(ArtifactType.RULE, owner="one")
    second = artifact(ArtifactType.RULE, owner="two")
    hints = [artifact(ArtifactType.HINT, owner=owner, name=name) for owner in ("one", "two") for name in ("a", "b")]
    files = guidance_skills("/skills", received(first, second, *hints))
    assert len(files) == 4
    assert len({file.native_identity for file in files}) == 4
    assert sorted(len(file.origins) for file in files) == [1, 1, 2, 2]
    assert {origin for file in files for origin in file.origins} == {
        item.origin_identity for item in (first, second, *hints)
    }
    for file in files:
        name = PurePosixPath(file.package_root).name
        assert metadata(file)["name"] == name and len(name) <= 64
        assert file.native_identity == "skill:" + name
    assert first.content.text.encode() in next(file.data for file in files if file.origins == (first.origin_identity,))


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
    if change == "outside-home":
        file = replace(
            file,
            path=file.path.replace(current.home, "/outside", 1),
            package_root=file.package_root.replace(current.home, "/outside", 1),
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
