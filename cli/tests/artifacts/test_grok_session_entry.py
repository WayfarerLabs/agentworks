"""A stable native entry discovers the current run's ordinary private index."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import NativeSessionArtifacts
from agentworks.artifacts.session import validate_session_application
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessStart, ShellIntegration
from agentworks.errors import StateError
from agentworks.plugins.grok.artifacts import outer_artifacts, session_artifacts
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context, owned
from tests.artifacts.test_session_lifecycle import lifecycle as lifecycle
from tests.native_setup_fixtures import LocalFixtureTransport


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


@pytest.fixture
def login_target(tmp_path: Path) -> LocalFixtureTransport:
    target = LocalFixtureTransport(tmp_path / "guest")
    (target.root / "login-shell").write_text(
        '#!/bin/bash\ncase "$1" in -lc|-lic) ;; *) exit 9 ;; esac\n'
        '. "$HOME/.profile"\nexec /bin/bash --noprofile --norc -c "$2"\n'
    )
    binary = target.root / "bin/grok"
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\ntouch "$HOME/native-executed"\nexit 90\n')
    binary.chmod(0o700)
    return target


def start(
    target: LocalFixtureTransport,
    current: SessionArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
    workarounds: list[str],
) -> HarnessStart:
    integration = GrokBuildIntegration(
        "grok-build",
        {"enabled_workarounds": workarounds},
        session_name="fixture",
        vm_name="vm",
        workspace_name="workspace",
        workspace_path=str(target.home / "workspace"),
        target=None,
        admin=True,
        state={},
        artifact_context=current,
    )
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "fixture-launch")
    return integration.start(RunContext(admin_access=target.access()))


@pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux login environment")
@pytest.mark.parametrize("ancestor,local", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("override", ["relative", "trailing"])
def test_disabled_grok_publication_keeps_invalid_home_and_ancestor_checks_advisory(
    login_target, monkeypatch, captured_output, ancestor, local, override
):
    rule = artifact(ArtifactType.RULE)
    earlier = outer_artifacts(received(rule), str(login_target.home / ".grok")).files[0]
    current = replace(
        context(*(rule,) if local else (), ancestors=(owned(earlier),) if ancestor else ()),
        home=str(login_target.home),
        environment={
            "GROK_HOME": "relative-grok" if override == "relative" else str(login_target.home / ".grok") + "/"
        },
    )
    result = start(login_target, current, monkeypatch, [])
    assert not result.artifacts.files
    assert {item.input_id for item in result.artifacts.deferred} == ({rule.identity} if local else set())
    assert bool(login_target.logged_output) == ancestor
    if ancestor:
        assert captured_output.warnings
    assert not (login_target.home / "native-executed").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux login environment")
def test_unhandled_activation_controls_do_not_require_new_home_resolution(login_target, monkeypatch):
    skill = artifact(ArtifactType.SKILL)
    skill = replace(skill, content=replace(skill.content, metadata_json=json.dumps({"paths": ["*.py"]})))
    earlier = outer_artifacts(received(artifact(ArtifactType.RULE)), str(login_target.home / ".grok")).files[0]
    current = replace(
        context(skill, ancestors=(owned(earlier),)),
        home=str(login_target.home),
        environment={"GROK_HOME": "relative-grok"},
    )
    result = start(login_target, current, monkeypatch, ["session-home-skills"])
    assert not result.artifacts.files
    assert {item.input_id for item in result.artifacts.deferred} == {skill.identity}
    assert not (login_target.home / "native-executed").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux login environment")
@pytest.mark.parametrize("outside_home", [False, True])
def test_grok_session_entry_uses_actual_profile_native_home_before_proposed_delivery(
    login_target, monkeypatch, outside_home
):
    root = (login_target.root if outside_home else login_target.home) / "alternate-grok"
    profile = login_target.home / ".profile"
    profile.write_text(profile.read_text() + f"export GROK_HOME={shlex.quote(str(root))}\n")
    current = replace(
        context(artifact(ArtifactType.RULE)),
        home=str(login_target.home),
        directory=str(login_target.home / ".agentworks-artifacts/session/u/r"),
        environment={},
    )
    result = start(login_target, current, monkeypatch, ["session-home-skills"])
    planned = next(file for file in result.artifacts.files if file.native_identity)
    assert planned.path.startswith(str(root / "skills") + "/")
    assert not planned.path.startswith(str(login_target.home / ".grok") + "/")
    if outside_home:
        with pytest.raises(StateError):
            validate_session_application(result.artifacts, current, integration="grok-build")
    else:
        validate_session_application(result.artifacts, current, integration="grok-build")
    assert not (login_target.home / "native-executed").exists()
