"""Artifact progress counts logical inputs and bounds displayed names."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentworks import output
from agentworks.artifacts.application import ArtifactDeferral, ArtifactSkip, OwnedArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.reporting import report_application
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact


@pytest.mark.parametrize("count,visible", [(1, 1), (2, 2), (3, 3), (4, 2), (8, 2)])
def test_progress_counts_artifacts_not_package_members(monkeypatch, count, visible):
    items = tuple(artifact(ArtifactType.SKILL, name=f"entry-{chr(97 + index)}") for index in range(count))
    files = tuple(
        OwnedArtifactFile(
            path=f"/skills/{item.content.name}/{member}", sha256="a" * 64, origins=(item.origin_identity,)
        )
        for item in items
        for member in ("SKILL.md", "extra.txt")
    )
    messages: list[str] = []
    monkeypatch.setattr(output, "info", messages.append)
    report_application(received(*items), integration="native", owner="user", files=files)
    assert len(messages) == 1
    assert str(count) in messages[0]
    for index, item in enumerate(items):
        assert (item.content.name in messages[0]) == (index < visible)


def test_skipped_and_unpublished_inputs_are_not_reported_as_applied(monkeypatch):
    applied = artifact(ArtifactType.RULE, name="published")
    skipped = artifact(ArtifactType.RULE, name="malformed")
    unpublished = artifact(ArtifactType.RULE, name="unpublished")
    owned = OwnedArtifactFile(
        path="/AGENTS.md", sha256="a" * 64, origins=(applied.origin_identity, skipped.origin_identity)
    )
    messages: list[str] = []
    monkeypatch.setattr(output, "info", messages.append)
    report_application(
        received(applied, skipped, unpublished),
        integration="native",
        owner="user",
        files=(owned,),
        skipped=(ArtifactSkip(path="/AGENTS.md", origins=(skipped.origin_identity,), reason="broken markers"),),
    )
    assert len(messages) == 1
    assert applied.content.name in messages[0]
    assert skipped.content.name not in messages[0]
    assert unpublished.content.name not in messages[0]


def test_deferrals_keep_routes_reasons_and_duplicate_owner_names(monkeypatch):
    first = artifact(ArtifactType.RULE, name="setup", owner="alice")
    second = artifact(ArtifactType.RULE, name="setup", owner="bob")
    third = artifact(ArtifactType.SKILL, name="build")
    routes = tuple(
        ArtifactDeferral(input_id=item.identity, destination=destination, reason=reason)
        for item, destination, reason in (
            (first, "user", "reason-a"),
            (second, "user", "reason-a"),
            (third, "session", "reason-b"),
        )
    )
    messages: list[str] = []
    monkeypatch.setattr(output, "warn", messages.append)
    monkeypatch.setattr(output, "info", lambda message: None)
    report_application(received(first, second, third), integration="native", owner="vm", deferred=routes)
    assert len(messages) == 2
    assert all(value in messages[0] for value in ("2", "alice", "bob", routes[0].reason, routes[0].destination))
    assert all(value in messages[1] for value in ("1", third.content.name, routes[2].reason, routes[2].destination))


def test_aggregate_file_reports_each_artifact_type(monkeypatch):
    rule = artifact(ArtifactType.RULE, name="policy")
    hint = replace(rule, content=replace(rule.content, type=ArtifactType.HINT))
    owned = OwnedArtifactFile(path="/AGENTS.md", sha256="a" * 64, origins=(rule.origin_identity, hint.origin_identity))
    messages: list[str] = []
    monkeypatch.setattr(output, "info", messages.append)
    report_application(received(rule, hint), integration="native", owner="user", files=(owned,))
    assert len(messages) == 2
    assert hint.content.type.value in messages[0]
    assert rule.content.type.value in messages[1]


def test_terminal_deferrals_group_names_but_retain_original_owner(monkeypatch):
    items = tuple(artifact(ArtifactType.RULE, name=name, owner="original-owner") for name in ("one", "two", "three"))
    deferred = tuple(
        ArtifactDeferral(input_id=item.identity, destination="session", reason="enable-the-carrier") for item in items
    )
    messages: list[str] = []
    progress: list[str] = []
    monkeypatch.setattr(output, "warn", messages.append)
    monkeypatch.setattr(output, "info", progress.append)
    report_application(received(*items), integration="native", owner="session facet", deferred=deferred, terminal=True)
    assert len(messages) == 1
    assert all(item.content.name in messages[0] for item in items)
    assert items[0].origin.resource_name in messages[0]
    assert items[0].origin.bundle in messages[0]
    assert deferred[0].reason in messages[0]
    assert not progress
