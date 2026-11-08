"""Large skill publication remains discoverable through an interrupted owning setup."""

from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.artifacts.application import OwnedArtifactFile
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.declarations import SkillArtifactSpec
from agentworks.artifacts.model import ArtifactOrigin
from agentworks.artifacts.publication import publish_artifacts
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.errors import StateError
from agentworks.harness_setup.model import SetupRecord
from agentworks.native_files import NativeFiles
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_probe import probe
from tests.conftest import requires_posix_shell
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = requires_posix_shell


@pytest.fixture
def large_skill_setup(tmp_path, db, monkeypatch):
    target = LocalFixtureTransport(tmp_path)
    probe(tmp_path, tool="claude", identities=())
    login = tmp_path / "login-shell"
    login.write_text(
        login.read_text().replace('[ "$1" = "-lc" ] || exit 9', 'case "$1" in -lc|-lic) ;; *) exit 9 ;; esac')
    )
    monkeypatch.setattr("agentworks.plugins.claude.harness_integration.setup_user", lambda *args: None)
    source = tmp_path / "source/review"
    (source / "support").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: review\ndescription: Review fixture\n---\nRead the fixture.\n")
    for index in range(514):
        folder = source / f"support/dir-{index:03}"
        folder.mkdir()
        (folder / f"file-{index:03}.txt").write_text("supporting content")
    kept_source = tmp_path / "source/kept"
    kept_source.mkdir()
    (kept_source / "SKILL.md").write_text("---\nname: kept\ndescription: Kept fixture\n---\nKeep.\n")
    captured = capture_artifacts(
        [
            (
                "team",
                ArtifactBundle(
                    name="team",
                    skills={
                        "review": SkillArtifactSpec(source=str(source)),
                        "kept": SkillArtifactSpec(source=str(kept_source)),
                    },
                ),
            )
        ],
        ArtifactOrigin("agent", "agent", "worker"),
    )
    item = captured.skills["review"]
    # Member iteration order is not publication authority, including for current programmatic inputs.
    item = replace(item, content=replace(item.content, members=(*item.content.members[1:], item.content.members[0])))
    integration = ClaudeCodeIntegration.for_setup(owner_kind="agent", owner_name="worker", facet="user", config={})
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "lima", "vm"),
        runner=target,
        prior=None,
        checkpoint=lambda claims: None,
        username="worker",
        home=str(target.home),
        environment=target.environment,
        artifacts=received(item, captured.skills["kept"]),
    )
    return target, integration, invocation


@pytest.mark.parametrize("package_root", [False, True])
def test_large_skill_entrypoint_is_published_before_supporting_files_and_setup_can_retry(
    large_skill_setup, monkeypatch, package_root
):
    target, integration, invocation = large_skill_setup
    plan = integration.user_init(invocation)
    files = tuple(replace(file, package_root=None) for file in plan.files) if not package_root else plan.files
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = [()]
    publish = NativeFiles.publish
    writes = []

    def interrupted(self, destination, *args, **kwargs):
        if destination.endswith("/file-513.txt"):
            raise StateError("fixture supporting publication interrupted")
        publish(self, destination, *args, **kwargs)
        writes.append(destination)

    monkeypatch.setattr(NativeFiles, "publish", interrupted)
    with pytest.raises(StateError):
        publish_artifacts(target, files, (), checkpoints.append, roots=(str(target.home),))
    entrypoint = target.home / ".claude/skills/review/SKILL.md"
    assert writes[0] == str(entrypoint)
    assert entrypoint.is_file() and len(list(entrypoint.parent.rglob("*.txt"))) == 513
    assert any(record.path == str(entrypoint) for record in checkpoints[-1])
    monkeypatch.setattr(NativeFiles, "publish", publish)
    invocation = replace(
        invocation,
        prior=SetupRecord(
            component="agent",
            integration="claude-code",
            destination_id="a" * 64,
            declaration={},
            artifact_files=checkpoints[-1],
        ),
    )
    # Exercise real user_init preflight before the retry publication.
    retry = integration.user_init(invocation)
    final = publish_artifacts(target, retry.files, checkpoints[-1], checkpoints.append, roots=(str(target.home),)).files
    assert len(final) == 516
    assert all(Path(file.path).read_bytes() == file.data for file in retry.files)


def test_inner_parent_permission_failure_retains_entrypoint_and_actual_setup_retry(large_skill_setup, monkeypatch):
    target, integration, invocation = large_skill_setup
    initial = integration.user_init(invocation)
    current = publish_artifacts(target, initial.files, (), lambda files: None, roots=(str(target.home),)).files
    assert invocation.artifacts.local is not None
    kept = invocation.artifacts.local.skills["kept"]
    invocation = replace(
        invocation,
        artifacts=received(kept),
        prior=SetupRecord(
            component="agent",
            integration="claude-code",
            destination_id="a" * 64,
            declaration={},
            artifact_files=current,
        ),
    )
    removal = integration.user_init(invocation)
    package = target.home / ".claude/skills/review"
    container = package / "support"
    checkpoints = [current]
    remove = NativeFiles.remove
    removed = []

    def record_remove(self, destination, *, expected):
        remove(self, destination, expected=expected)
        removed.append(destination)

    monkeypatch.setattr(NativeFiles, "remove", record_remove)
    container.chmod(0o500)
    try:
        with pytest.raises(StateError):
            publish_artifacts(target, removal.files, current, checkpoints.append, roots=(str(target.home),))
        assert len(removed) == 1 and not Path(removed[0]).exists()
        assert any(record.path == removed[0] for record in checkpoints[-1])
        assert (package / "SKILL.md").is_file()
        # Still readable by actual setup preflight despite more than 512 supporting directories.
        assert invocation.prior is not None
        pending = replace(invocation, prior=invocation.prior.model_copy(update={"artifact_files": checkpoints[-1]}))
        retry = integration.user_init(pending)
    finally:
        container.chmod(0o700)
    finished = publish_artifacts(
        target, retry.files, checkpoints[-1], checkpoints.append, roots=(str(target.home),)
    ).files
    assert all(record.native_identity == "skill:kept" for record in finished)
    assert removed.count(removed[0]) == 1
    assert not package.exists()
