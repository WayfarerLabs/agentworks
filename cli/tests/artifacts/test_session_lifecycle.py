"""Session artifact lifecycle through real managers, local files and persisted state.

Only runtime process ownership and unrelated VM transport gates are fixtures.
Artifact capture, routing, native rendering, publication and DB writes are real.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig, HintArtifactSpec
from agentworks.artifacts.session import cleanup_session_artifacts
from agentworks.capabilities.harness_integration import HarnessStart, ShellIntegration
from agentworks.db import Database, SessionStatus
from agentworks.errors import StateError
from agentworks.harness_setup.state import read_native_setup
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from agentworks.sessions.templates import ResolvedSessionTemplate
from tests.conftest import stub_build_registry
from tests.native_setup_fixtures import LocalFixtureTransport
from tests.sessions.test_create_start_restart_orchestrated import _patch_transports, _restart_fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution")


@dataclass
class Lifecycle:
    db: Database
    config: Any
    template: ResolvedSessionTemplate
    target: LocalFixtureTransport
    events: list[str]

    def restart(self) -> None:
        manager.restart_session(self.db, self.config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    def files(self) -> tuple[Path, ...]:
        return tuple(
            Path(file.path)
            for record in read_native_setup(self.db, "session", "s1").records
            for file in record.artifact_files
        )


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    stub_build_registry(monkeypatch)
    db, events = _restart_fixture(tmp_path, monkeypatch)
    target = LocalFixtureTransport(tmp_path / "native")
    _patch_transports(monkeypatch, target, target)
    bundle = ArtifactBundle(name="team", hints={"setup": HintArtifactSpec(text="Use the project tools.\n")})
    template = ResolvedSessionTemplate(name="shell-artifacts", artifacts=ArtifactsConfig(bundles=["team"]))
    monkeypatch.setattr(manager, "_resolve_template", lambda *a, **k: template)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=1), artifact_bundles={"team": bundle})
    yield Lifecycle(db, config, template, target, events)
    db.close()


def test_shell_restart_publishes_under_actual_home_and_records_run(lifecycle):
    previous = lifecycle.db.get_session("s1")
    assert previous is not None
    lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None
    assert current.session_uuid == previous.session_uuid
    assert current.run_id is not None and current.run_id != previous.run_id
    files = lifecycle.files()
    assert len(files) == 2
    root = lifecycle.target.home / ".agentworks-artifacts" / "session" / current.session_uuid / current.run_id
    assert all(file.is_relative_to(root) and file.exists() for file in files)
    index = json.loads((root / "index.json").read_text())
    assert index["groups"][0]["owner"]["component"] == "session"
    record = read_native_setup(lifecycle.db, "session", "s1").records[0]
    assert record.complete and record.artifact_inputs


@pytest.mark.parametrize("operation", ["create", "restart"])
def test_launch_environment_exposes_only_the_prepared_run_directory(lifecycle, monkeypatch, operation):
    from agentworks.sessions import tmux

    launch = tmux.create_session
    observed = []

    def inspect(*args, **kwargs):
        observed.append(dict(kwargs["env"]))
        return launch(*args, **kwargs)

    monkeypatch.setattr(tmux, "create_session", inspect)
    if operation == "create":
        lifecycle.db.insert_agent_grant("a1", "ws1", "explicit")
        monkeypatch.setattr("agentworks.agents.manager._assert_agent_ssh_works", lambda *a, **k: None)
        manager.create_session(
            lifecycle.db,
            lifecycle.config,
            name="s2",
            workspace="ws1",
            agent="a1",
            interaction=TtyInteractionPolicy.REFUSE,
        )
    else:
        lifecycle.restart()
    current = lifecycle.db.get_session("s2" if operation == "create" else "s1")
    assert current is not None and current.run_id is not None
    directory = lifecycle.target.home / ".agentworks-artifacts" / "session" / current.session_uuid / current.run_id
    assert len(observed) == 1
    assert observed[0]["AGENTWORKS_ARTIFACTS_DIR"] == str(directory)
    assert (directory / "index.json").is_file()


def test_forged_artifact_directory_preserves_running_session(lifecycle, monkeypatch):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    files = lifecycle.files()
    lifecycle.events.clear()
    monkeypatch.setattr(
        ShellIntegration,
        "start",
        lambda *a, **k: HarnessStart("", artifacts=ArtifactApplication(artifacts_dir="/outside")),
    )
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None and current.run_id == previous.run_id
    assert files == lifecycle.files() and all(file.exists() for file in files)
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events


def test_restart_stages_new_files_before_kill_and_cleans_old_after(lifecycle, monkeypatch):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    old_files = lifecycle.files()
    assert previous is not None
    observed = []

    def teardown(*args, **kwargs):
        owned = lifecycle.files()
        assert all(path.exists() for path in old_files)
        assert len(owned) == 4
        assert all(path.exists() for path in owned)
        row = lifecycle.db.get_session("s1")
        assert row is not None and row.run_id == previous.run_id
        observed.append("kill")

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", teardown)
    lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.run_id != previous.run_id
    assert current.session_uuid == previous.session_uuid
    assert observed == ["kill"]
    assert all(not path.exists() for path in old_files)
    assert len(lifecycle.files()) == 2
    assert all(path.exists() for path in lifecycle.files())


def test_start_running_is_noop_for_run_identity_and_artifact_files(lifecycle):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    before = [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in lifecycle.files()]
    lifecycle.events.clear()
    manager.start_session(lifecycle.db, lifecycle.config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None
    assert (current.session_uuid, current.run_id) == (previous.session_uuid, previous.run_id)
    assert [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in lifecycle.files()] == before
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events


def test_source_failure_preserves_old_runtime_run_and_files(lifecycle):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    files = lifecycle.files()
    lifecycle.config.artifact_bundles["team"] = ArtifactBundle(
        name="team", hints={"setup": HintArtifactSpec(source="/missing/artifact-source.txt")}
    )
    lifecycle.events.clear()
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert previous is not None and current is not None and current.run_id == previous.run_id
    assert all(file.exists() for file in files)
    assert "kill" not in lifecycle.events


def test_remaining_deferral_refuses_before_runtime_or_file_changes(lifecycle, monkeypatch):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    files = lifecycle.files()
    lifecycle.events.clear()

    def defer(self, ctx, **kwargs):
        context = self._session_binding.artifact_context
        assert context is not None
        return HarnessStart(
            "",
            artifacts=ArtifactApplication(
                deferred=(
                    ArtifactDeferral(
                        input_id=tuple(context.inputs.items())[0].identity,
                        destination="session",
                        reason="unsupported fixture",
                    ),
                )
            ),
        )

    monkeypatch.setattr(ShellIntegration, "start", defer)
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None and current.run_id == previous.run_id
    assert files == lifecycle.files() and all(file.exists() for file in files)
    assert "kill" not in lifecycle.events


def test_unknown_runtime_preserves_artifact_ownership(lifecycle, monkeypatch):
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    files = lifecycle.files()
    records = read_native_setup(lifecycle.db, "session", "s1")
    lifecycle.events.clear()
    monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: SessionStatus.UNKNOWN)
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None and current.run_id == previous.run_id
    assert records == read_native_setup(lifecycle.db, "session", "s1")
    assert all(path.exists() for path in files)
    assert "kill" not in lifecycle.events


def test_session_env_is_prepared_before_harness_start(lifecycle, monkeypatch):
    expected = {"PROJECT_CONTEXT": "ready"}
    monkeypatch.setattr(manager, "_resolve_session_env", lambda *a, **k: dict(expected))
    original = ShellIntegration.start
    observed = []

    def inspect(self, ctx, **kwargs):
        context = self._session_binding.artifact_context
        assert context is not None
        observed.append(dict(context.environment))
        return original(self, ctx, **kwargs)

    monkeypatch.setattr(ShellIntegration, "start", inspect)
    lifecycle.restart()
    assert observed == [expected]


def test_deleted_name_reuses_no_private_run_or_uuid(lifecycle):
    from agentworks.db import SessionMode

    lifecycle.restart()
    old = lifecycle.db.get_session("s1")
    assert old is not None
    files = lifecycle.files()
    cleanup_session_artifacts(lifecycle.db, old, lifecycle.target)
    lifecycle.db.delete_session("s1")
    assert all(not path.exists() for path in files)
    replacement = lifecycle.db.insert_session(
        "s1", "ws1", "claude", SessionMode.AGENT, agent_name="a1", socket_path="/tmp/s1.sock"
    )
    assert replacement.session_uuid != old.session_uuid
    lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.session_uuid == replacement.session_uuid
    assert current.run_id != old.run_id
    assert all(old.session_uuid not in str(path) for path in lifecycle.files())


def test_modified_session_file_keeps_ownership_for_recovery(lifecycle):
    lifecycle.restart()
    session = lifecycle.db.get_session("s1")
    assert session is not None
    files = lifecycle.files()
    files[0].write_text("operator modification")
    with pytest.raises(StateError):
        cleanup_session_artifacts(lifecycle.db, session, lifecycle.target)
    assert lifecycle.db.get_session("s1") is not None
    assert files[0].exists()
    assert any(
        str(files[0]) == owned.path
        for record in read_native_setup(lifecycle.db, "session", "s1").records
        for owned in record.artifact_files
    )


def test_create_and_delete_keep_same_user_sessions_separate(lifecycle, monkeypatch):
    lifecycle.restart()
    first = lifecycle.db.get_session("s1")
    first_files = lifecycle.files()
    assert first is not None
    lifecycle.db.insert_agent_grant("a1", "ws1", "explicit")
    monkeypatch.setattr("agentworks.agents.manager._assert_agent_ssh_works", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: lifecycle.events.append("delete-kill"))
    manager.create_session(
        lifecycle.db, lifecycle.config, name="s2", workspace="ws1", agent="a1", interaction=TtyInteractionPolicy.REFUSE
    )
    second = lifecycle.db.get_session("s2")
    assert second is not None and second.session_uuid != first.session_uuid
    assert second.run_id is not None and second.run_id != first.run_id
    second_files = tuple(
        Path(file.path)
        for record in read_native_setup(lifecycle.db, "session", "s2").records
        for file in record.artifact_files
    )
    assert second_files and not set(second_files) & set(first_files)
    assert all(path.exists() and path.is_relative_to(lifecycle.target.home) for path in (*first_files, *second_files))
    manager.delete_session(lifecycle.db, lifecycle.config, name="s2", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert lifecycle.db.get_session("s2") is None
    assert all(not path.exists() for path in second_files)
    assert all(path.exists() for path in first_files)
    assert lifecycle.db.get_session("s1") is not None


def test_unconfirmed_new_runtime_keeps_its_run_and_artifacts(lifecycle, monkeypatch):
    from agentworks.errors import ExternalError
    from agentworks.sessions import tmux
    from agentworks.sessions.tmux import FingerprintProbe, ProbeStatus

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    assert previous is not None
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", lambda **kwargs: FingerprintProbe(ProbeStatus.UNKNOWN))
    monkeypatch.setattr(tmux, "kill_server_and_probe", lambda **kwargs: ProbeStatus.UNKNOWN)
    with pytest.raises(ExternalError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.run_id != previous.run_id
    assert current.pid is None and current.socket_path is not None
    assert lifecycle.files() and all(path.exists() for path in lifecycle.files())
    assert all(current.run_id in str(path) for path in lifecycle.files())


def test_partial_publication_retains_old_run_and_retry_cleans_staged_files(lifecycle, monkeypatch):
    from agentworks.native_files import NativeFiles

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    old_files = lifecycle.files()
    assert previous is not None
    original = NativeFiles.publish
    calls = 0

    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("fixture interrupted publication")
        return original(self, *args, **kwargs)

    lifecycle.events.clear()
    monkeypatch.setattr(NativeFiles, "publish", fail_second)
    with pytest.raises(StateError):
        lifecycle.restart()
    interrupted_files = lifecycle.files()
    assert len(interrupted_files) == 3 and all(path.exists() for path in interrupted_files)
    assert set(old_files) <= set(interrupted_files)
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.run_id == previous.run_id
    assert "kill" not in lifecycle.events
    assert not read_native_setup(lifecycle.db, "session", "s1").records[0].complete
    monkeypatch.setattr(NativeFiles, "publish", original)
    lifecycle.restart()
    assert len(lifecycle.files()) == 2
    assert all(not path.exists() for path in interrupted_files)
    assert read_native_setup(lifecycle.db, "session", "s1").records[0].complete


def test_shell_launch_receives_discovery_env_after_complete_publication(lifecycle, monkeypatch):
    from agentworks.sessions import tmux

    monkeypatch.setattr(manager, "_resolve_session_env", lambda *a, **k: {"BASE_CONTEXT": "ready"})
    original = tmux.create_session
    observed = []

    def create(*args, **kwargs):
        env = kwargs["env"]
        root = Path(env["AGENTWORKS_ARTIFACTS_DIR"])
        assert (root / "index.json").is_file()
        assert all(path.exists() for path in lifecycle.files())
        assert read_native_setup(lifecycle.db, "session", "s1").records[0].complete
        observed.append(env["BASE_CONTEXT"])
        return original(*args, **kwargs)

    monkeypatch.setattr(tmux, "create_session", create)
    lifecycle.restart()
    assert observed == ["ready"]


def test_session_delete_refuses_while_vm_native_mutation_is_held(lifecycle):
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

    lifecycle.restart()
    files = lifecycle.files()
    lifecycle.events.clear()
    with native_mutation_guard(lifecycle.db.path, "vm1"), pytest.raises(NativeSetupBusyError):
        manager.delete_session(
            lifecycle.db, lifecycle.config, name="s1", yes=True, interaction=TtyInteractionPolicy.REFUSE
        )
    assert lifecycle.db.get_session("s1") is not None
    assert all(path.exists() for path in files)
    assert "kill" not in lifecycle.events


@pytest.mark.parametrize("operation", ["start", "restart", "create"])
def test_uncaptured_ancestor_refuses_before_secrets(lifecycle, monkeypatch, operation):
    from agentworks.vms.templates import ResolvedVMTemplate

    monkeypatch.setattr(
        "agentworks.vms.templates.resolve_live_template",
        lambda *a, **k: ResolvedVMTemplate("default", artifacts=ArtifactsConfig(bundles=["team"])),
    )
    if operation == "start":
        monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED)
    if operation == "create":
        lifecycle.db.insert_agent_grant("a1", "ws1", "explicit")
        monkeypatch.setattr("agentworks.agents.manager._assert_agent_ssh_works", lambda *a, **k: None)
    lifecycle.events.clear()
    with pytest.raises(StateError):
        if operation == "create":
            manager.create_session(
                lifecycle.db,
                lifecycle.config,
                name="s2",
                workspace="ws1",
                agent="a1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        elif operation == "start":
            manager.start_session(lifecycle.db, lifecycle.config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        else:
            lifecycle.restart()
    assert "resolve" not in lifecycle.events and "resolve_env" not in lifecycle.events
    assert "kill" not in lifecycle.events and not lifecycle.files()


def test_new_agent_does_not_hide_known_vm_artifact_gap(lifecycle, monkeypatch):
    from agentworks.vms.templates import ResolvedVMTemplate

    monkeypatch.setattr(
        "agentworks.vms.templates.resolve_live_template",
        lambda *a, **k: ResolvedVMTemplate("default", artifacts=ArtifactsConfig(bundles=["team"])),
    )
    lifecycle.events.clear()
    with pytest.raises(StateError) as error:
        manager.create_session(
            lifecycle.db,
            lifecycle.config,
            name="s2",
            workspace="ws1",
            new_agent=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert error.value.entity_kind == "vm" and error.value.entity_name == "vm1"
    assert "resolve" not in lifecycle.events and "resolve_env" not in lifecycle.events
    assert lifecycle.db.get_agent("s2") is None and lifecycle.db.get_session("s2") is None
