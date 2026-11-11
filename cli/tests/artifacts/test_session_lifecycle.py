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

from agentworks.artifacts.application import ArtifactApplication, ArtifactFile
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig, HintArtifactSpec
from agentworks.artifacts.native.common import defer
from agentworks.artifacts.sections import BEGIN
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

# Every access a command builds is the recording fake: these tests run no ssh.
pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem execution"),
    pytest.mark.usefixtures("vm_access_recorder"),
]


@dataclass
class Lifecycle:
    db: Database
    config: Any
    template: ResolvedSessionTemplate
    target: LocalFixtureTransport
    events: list[str]

    def restart(self) -> None:
        manager.restart_session(self.db, self.config, name="s1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

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
    # The shared lifecycle fixture simulates a completed runtime without
    # creating tmux. Keep its startup observation on that same runtime seam.
    monkeypatch.setattr("agentworks.sessions.tmux.startup_protection_enabled", lambda *a, **k: False)
    bundle = ArtifactBundle(name="team", hints={"setup": HintArtifactSpec(text="Use the project tools.\n")})
    template = ResolvedSessionTemplate(
        name="shell-artifacts",
        artifacts=ArtifactsConfig(bundles=["team"]),
        harness_integration_config={},
    )
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


def test_differing_prospective_session_file_preserves_running_session(lifecycle, monkeypatch):
    from agentworks.artifacts import session as artifact_session
    from agentworks.artifacts.publication import publish_artifacts

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    old_files = lifecycle.files()
    conflicts = []

    def interfere(runner, desired, *args, **kwargs):
        path = Path(desired[0].path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unowned prospective content")
        conflicts.append(path)
        return publish_artifacts(runner, desired, *args, **kwargs)

    monkeypatch.setattr(artifact_session, "publish_artifacts", interfere)
    lifecycle.events.clear()
    with pytest.raises(StateError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert previous is not None and current is not None and current.run_id == previous.run_id
    assert all(file.exists() for file in old_files)
    assert len(conflicts) == 1 and conflicts[0].read_bytes() == b"unowned prospective content"
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events
    record = read_native_setup(lifecycle.db, "session", "s1").records[0]
    assert not record.complete and record.skipped
    assert str(conflicts[0]) not in {file.path for file in record.artifact_files}


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


def _leave_session_inputs_unhandled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise core's unhandled protocol with a deliberately declining integration."""

    def start(integration, *args, **kwargs):
        context = integration._session_binding.artifact_context
        assert context is not None
        return HarnessStart("", artifacts=defer(context.inputs, "session", "Unsupported fixture artifacts"))

    monkeypatch.setattr(ShellIntegration, "start", start)


def test_skipped_session_section_is_recorded_without_claiming_delivery(lifecycle, monkeypatch, captured_output):
    def start(integration, *args, **kwargs):
        context = integration._session_binding.artifact_context
        item = next(context.inputs.items())
        path = Path(context.directory) / "AGENTS.md"
        # Simulate an externally malformed file at the integration-selected destination.
        path.parent.mkdir(parents=True)
        path.write_bytes(BEGIN + b"\nmissing end delimiter")
        file = ArtifactFile(str(path), b"replacement", (item.origin_identity,), generated_section=True)
        return HarnessStart("true", artifacts=ArtifactApplication(files=(file,)))

    monkeypatch.setattr(ShellIntegration, "start", start)
    lifecycle.restart()
    record = read_native_setup(lifecycle.db, "session", "s1").records[0]
    assert record.complete and len(record.skipped) == 1
    assert not record.artifact_files and not record.deferred
    assert Path(record.skipped[0].path).read_bytes() == BEGIN + b"\nmissing end delimiter"
    assert len(captured_output.warnings) == 1


def test_unhandled_session_inputs_warn_launch_and_retire_previous_files(lifecycle, monkeypatch, captured_output):
    from agentworks.sessions import tmux

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    files = lifecycle.files()
    lifecycle.events.clear()
    _leave_session_inputs_unhandled(monkeypatch)
    launch = tmux.create_session
    observed = []

    def inspect(*args, **kwargs):
        observed.append(dict(kwargs["env"]))
        return launch(*args, **kwargs)

    monkeypatch.setattr(tmux, "create_session", inspect)
    captured_output.warnings.clear()
    lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None and current.run_id != previous.run_id
    assert not lifecycle.files() and all(not file.exists() for file in files)
    assert len(observed) == 1 and "AGENTWORKS_ARTIFACTS_DIR" not in observed[0]
    assert len(captured_output.warnings) == 1
    record = read_native_setup(lifecycle.db, "session", "s1").records[0]
    assert record.complete and not record.artifact_files
    assert tuple(item.input_id for item in record.deferred) == record.artifact_inputs
    assert all(item.destination == "session" for item in record.deferred)
    lifecycle.restart()
    assert not lifecycle.files()


@pytest.mark.parametrize("operation", ["create", "start"])
def test_unhandled_session_inputs_warn_without_files_or_discovery_env(
    lifecycle, monkeypatch, captured_output, operation
):
    from agentworks.sessions import tmux

    _leave_session_inputs_unhandled(monkeypatch)
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
        monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED)
        manager.start_session(lifecycle.db, lifecycle.config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
    name = "s2" if operation == "create" else "s1"
    record = read_native_setup(lifecycle.db, "session", name).records[0]
    assert record.complete and not record.artifact_files
    assert tuple(item.input_id for item in record.deferred) == record.artifact_inputs
    assert len(captured_output.warnings) == 1
    assert len(observed) == 1 and "AGENTWORKS_ARTIFACTS_DIR" not in observed[0]
    assert not (lifecycle.target.home / ".agentworks-artifacts").exists()


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
    cleanup_session_artifacts(lifecycle.db, old, lifecycle.target.access())
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
        cleanup_session_artifacts(lifecycle.db, session, lifecycle.target.access())
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
    with pytest.raises(ExternalError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.run_id != previous.run_id
    assert current.pid is None and current.socket_path is not None
    assert lifecycle.files() and all(path.exists() for path in lifecycle.files())
    assert all(current.run_id in str(path) for path in lifecycle.files())


def test_partial_publication_retains_old_run_and_retry_cleans_staged_files(lifecycle, monkeypatch):
    from agentworks.execution.files import Files

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    old_files = lifecycle.files()
    assert previous is not None
    original = Files.write_if
    calls = 0

    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("fixture interrupted publication")
        return original(self, *args, **kwargs)

    lifecycle.events.clear()
    monkeypatch.setattr(Files, "write_if", fail_second)
    with pytest.raises(StateError):
        lifecycle.restart()
    interrupted_files = lifecycle.files()
    assert len(interrupted_files) == 3 and all(path.exists() for path in interrupted_files)
    assert set(old_files) <= set(interrupted_files)
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.run_id == previous.run_id
    assert "kill" not in lifecycle.events
    assert not read_native_setup(lifecycle.db, "session", "s1").records[0].complete
    monkeypatch.setattr(Files, "write_if", original)
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
def test_uncaptured_ancestor_does_not_block_session_operations(lifecycle, monkeypatch, operation):
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
    name = "s2" if operation == "create" else "s1"
    record = read_native_setup(lifecycle.db, "session", name).records[0]
    assert not record.complete and record.artifact_files and record.incomplete_reasons
    assert "resolve" in lifecycle.events and "tmux_create" in lifecycle.events


def test_new_agent_preflight_does_not_block_on_known_vm_artifact_gap(lifecycle, monkeypatch):
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.templates import ResolvedVMTemplate

    monkeypatch.setattr(
        "agentworks.vms.templates.resolve_live_template",
        lambda *a, **k: ResolvedVMTemplate("default", artifacts=ArtifactsConfig(bundles=["team"])),
    )

    class PassedPreflight(Exception):
        pass

    def stop_after_preflight(*args, **kwargs):
        raise PassedPreflight

    monkeypatch.setattr(Resolver, "resolve", stop_after_preflight)
    with pytest.raises(PassedPreflight):
        manager.create_session(
            lifecycle.db,
            lifecycle.config,
            name="s2",
            workspace="ws1",
            new_agent=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert lifecycle.db.get_agent("s2") is None and lifecycle.db.get_session("s2") is None


@pytest.mark.parametrize("status", ["stale", "incomplete", "unavailable"])
def test_upstream_placement_readiness_is_reported_without_blocking_restart(lifecycle, monkeypatch, status):
    from agentworks import output
    from agentworks.artifacts.model import ArtifactInputs
    from agentworks.artifacts.routing import RoutingResult
    from agentworks.capabilities.harness_integration.setup import SetupEvidence

    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)
    monkeypatch.setattr(
        "agentworks.artifacts.routing.session_artifacts",
        lambda *args, **kwargs: RoutingResult(ArtifactInputs(local=args[-1]), active_facets=("user",)),
    )
    monkeypatch.setattr(
        "agentworks.harness_setup.readiness._applicable_evidence",
        lambda *args, **kwargs: SetupEvidence(status, "agent", "a1", "fixture owning repair"),
    )
    lifecycle.restart()
    assert warnings and "kill" in lifecycle.events and "tmux_create" in lifecycle.events
    assert read_native_setup(lifecycle.db, "session", "s1").records[0].complete


@pytest.mark.parametrize(
    "tool,integration,workaround",
    [
        ("claude", "claude-code", "session-skill-plugin"),
        ("codex", "codex", "session-home-skills"),
        ("grok", "grok-build", "session-home-skills"),
    ],
)
def test_real_native_probe_failure_preserves_existing_runtime_before_teardown(
    lifecycle, monkeypatch, tool, integration, workaround
):
    from importlib import import_module
    from unittest.mock import Mock

    from agentworks.artifacts.native import probe as native
    from agentworks.errors import ExternalError

    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    files = [(path, path.read_bytes()) for path in lifecycle.files()]
    lifecycle.template.harness_integration = integration
    lifecycle.template.harness_integration_config = {"enabled_workarounds": [workaround]}
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    monkeypatch.setattr(module, "require_commands", lambda *args, **kwargs: None)
    observed = Mock(wraps=native.probe_native)
    monkeypatch.setattr(native, "probe_native", observed)
    monkeypatch.setattr(module, "probe_native", observed)
    lifecycle.events.clear()

    # The real probe runs in the fixture's account, whose login startup fails,
    # so discovery fails with the probe's own status rather than a fabricated exception.
    (lifecycle.target.home / ".profile").write_text("exit 1\n")
    with pytest.raises(ExternalError):
        lifecycle.restart()
    assert observed.called
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None
    assert (current.session_uuid, current.run_id, current.pid) == (previous.session_uuid, previous.run_id, previous.pid)
    assert [(path, path.read_bytes()) for path, _ in files] == files
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events


@pytest.mark.parametrize(
    "tool,integration,workaround,flag",
    [
        ("claude", "claude-code", "session-skill-plugin", "disable-slash-commands"),
        ("codex", "codex", "session-home-skills", "profile"),
        ("grok", "grok-build", "session-home-skills", "disable-skills"),
    ],
)
def test_rendered_carrier_override_refuses_restart_before_staging_or_teardown(
    lifecycle, monkeypatch, tool, integration, workaround, flag
):
    from importlib import import_module

    from agentworks.errors import ConfigError

    lifecycle.restart()
    workspace = lifecycle.db.get_workspace("ws1")
    assert workspace is not None
    lifecycle.db.insert_workspace(
        flag,
        workspace_path=workspace.workspace_path,
        vm_name=workspace.vm_name,
        linux_group=workspace.linux_group,
        template=workspace.template,
    )
    lifecycle.db._conn.execute("UPDATE sessions SET workspace_name = ? WHERE name = 's1'", (flag,))
    lifecycle.db._conn.commit()
    previous = lifecycle.db.get_session("s1")
    old_files = [(path, path.read_bytes()) for path in lifecycle.files()]
    root = lifecycle.target.home / ".agentworks-artifacts"
    old_paths = sorted(root.rglob("*"))
    lifecycle.template.harness_integration = integration
    lifecycle.template.harness_integration_config = {
        "enabled_workarounds": [workaround],
        "extra_args": ["--${agw:workspace_name}"],
    }
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    monkeypatch.setattr(module, "require_commands", lambda *args, **kwargs: None)
    lifecycle.events.clear()
    with pytest.raises(ConfigError):
        lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and previous is not None
    assert (current.session_uuid, current.run_id, current.pid) == (previous.session_uuid, previous.run_id, previous.pid)
    assert [(path, path.read_bytes()) for path, _ in old_files] == old_files
    assert sorted(root.rglob("*")) == old_paths
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events


@pytest.mark.parametrize("capture_state", ["changed", "missing"])
def test_unknown_passthrough_restart_records_missing_delivery_and_retires_old_files_after_teardown(
    lifecycle, monkeypatch, captured_output, capture_state
):
    from agentworks.artifacts.application import ArtifactDeferral
    from agentworks.artifacts.state import capture_owner, write_capture
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.model import NativeSetupState, SetupRecord
    from agentworks.harness_setup.state import write_native_setup
    from agentworks.schema import CapabilityConfig
    from agentworks.secrets.orchestration import SecretTarget
    from agentworks.vms.templates import ResolvedVMTemplate
    from tests.conftest import _StubRegistry

    outer = ArtifactBundle(name="outer", hints={"vm-tools": HintArtifactSpec(text="VM-ONLY-CONTENT")})
    lifecycle.config.artifact_bundles["outer"] = outer
    template = ResolvedVMTemplate(
        "default", artifacts=ArtifactsConfig(bundles=["outer"]), harness_integrations={"shell": CapabilityConfig()}
    )
    monkeypatch.setattr("agentworks.vms.templates.resolve_live_template", lambda *a, **k: template)
    registry = _StubRegistry(lifecycle.config)
    capture = capture_owner(registry, "vm", "vm1", "vm", template.artifacts)
    write_capture(lifecycle.db, "vm", "vm1", "vm", capture, operation="fixture")
    item = next(capture.inputs.items())
    owner = SetupInputs("vm", "vm1", "vm", template.harness_integrations, SecretTarget(vm={}), template.artifacts)
    record = SetupRecord(
        component="vm",
        integration="shell",
        destination_id="d" * 64,
        declaration=owner.declaration("shell", CapabilityConfig()),
        complete=True,
        artifact_inputs=(item.identity,),
        deferred=(ArtifactDeferral(input_id=item.identity, destination="user", reason="fixture user delivery"),),
    )
    write_native_setup(lifecycle.db, "vm", "vm1", NativeSetupState(records=(record,)), operation="fixture")
    lifecycle.restart()
    old_files = {path: path.read_bytes() for path in lifecycle.files()}
    assert any(b"VM-ONLY-CONTENT" in body for body in old_files.values())
    first = lifecycle.db.get_session("s1")
    assert first is not None

    if capture_state == "changed":
        outer.hints["vm-tools"] = HintArtifactSpec(text="UPDATED-VM-CONTENT")
        changed = capture_owner(registry, "vm", "vm1", "vm", template.artifacts)
        write_capture(lifecycle.db, "vm", "vm1", "vm", changed, operation="fixture")
    else:
        lifecycle.db._conn.execute(
            "DELETE FROM instance_records WHERE instance_kind = 'vm' "
            "AND instance_name = 'vm1' AND record_key = 'artifact-inputs'"
        )
        lifecycle.db._conn.commit()
    lifecycle.events.clear()
    captured_output.warnings.clear()
    teardowns = []

    def teardown(*args, **kwargs):
        assert all(path.read_bytes() == body for path, body in old_files.items())
        teardowns.append(True)
        lifecycle.events.append("kill")

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", teardown)
    lifecycle.restart()
    current = lifecycle.db.get_session("s1")
    assert current is not None and current.run_id != first.run_id
    assert teardowns == [True]
    assert "tmux_create" in lifecycle.events and "kill" in lifecycle.events
    state = read_native_setup(lifecycle.db, "session", "s1").records[0]
    assert not state.complete and state.incomplete_reasons
    assert state.artifact_inputs is not None
    assert item.identity in state.artifact_inputs
    assert any(entry.input_id == item.identity and entry.destination == "session" for entry in state.deferred)
    assert any("vm/vm1" in reason and item.identity[:12] in reason for reason in state.incomplete_reasons)
    assert captured_output.warnings
    assert all(not path.exists() for path in old_files)
    new_root = lifecycle.target.home / ".agentworks-artifacts" / "session" / current.session_uuid / current.run_id
    index = json.loads((new_root / "index.json").read_text())
    assert all(group["owner"]["component"] != "vm" for group in index["groups"])
    assert not any(b"VM-ONLY-CONTENT" in path.read_bytes() for path in new_root.rglob("*") if path.is_file())

    # Repair the outer routing, then restart: delivery returns to the complete
    # current input set without retaining a private history of unused runs.
    repaired = capture_owner(registry, "vm", "vm1", "vm", template.artifacts)
    write_capture(lifecycle.db, "vm", "vm1", "vm", repaired, operation="fixture")
    renewed = next(repaired.inputs.items())
    healed = record.model_copy(
        update={
            "artifact_inputs": (renewed.identity,),
            "deferred": (ArtifactDeferral(input_id=renewed.identity, destination="user", reason="fixture repair"),),
        }
    )
    write_native_setup(lifecycle.db, "vm", "vm1", NativeSetupState(records=(healed,)), operation="fixture")
    monkeypatch.setattr(
        "agentworks.sessions.manager._lifecycle._teardown_session", lambda *a, **k: lifecycle.events.append("kill")
    )
    lifecycle.restart()
    state = read_native_setup(lifecycle.db, "session", "s1").records[0]
    assert state.complete and not state.incomplete_reasons
    assert not any(entry.input_id == item.identity for entry in state.deferred)
    assert all(not path.exists() for path in old_files)
