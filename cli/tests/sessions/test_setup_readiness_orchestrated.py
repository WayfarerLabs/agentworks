"""Session readiness gates secrets and consumes freshly realized owner setup."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from agentworks.capabilities.harness_integration import ShellIntegration
from agentworks.capabilities.harness_integration.setup import SetupEvidence, SetupGap, SetupReadiness
from agentworks.db import Database, SessionStatus
from agentworks.errors import BrokenStateError, StateError
from agentworks.harness_setup.model import NativeSetupState, SetupFacet, SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures
from agentworks.harness_setup.readiness import RequiredSetupMissingError
from agentworks.harness_setup.state import write_native_setup
from agentworks.instance_specs import persist_creation_overlay
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions import manager

from ..conftest import stub_build_registry, stub_vm_ssh_identity
from .test_create_start_restart_orchestrated import _create_stubs, _restart_fixture


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_build_registry(monkeypatch)
    stub_vm_ssh_identity(monkeypatch)


@pytest.fixture
def config() -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(history_limit=1), paths=SimpleNamespace(vm_workspaces="/srv"))


def _record_setup_check(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    facets: tuple[SetupFacet, ...] = ("user",),
    severity: Literal["required", "recommended"] = "required",
) -> list[SetupEvidence]:
    observed: list[SetupEvidence] = []

    def check(self: ShellIntegration, readiness: SetupReadiness) -> tuple[SetupGap, ...]:
        events.append("setup")
        evidence = tuple(getattr(readiness, facet) for facet in facets)
        observed.extend(evidence)
        return tuple(SetupGap(item, severity, "fixture prerequisite") for item in evidence if not item.current)

    monkeypatch.setattr(ShellIntegration, "check_setup", check)
    return observed


def _record_resolve(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    original = Resolver.resolve

    def resolve(self: Resolver) -> None:
        events.append("resolve")
        original(self)

    monkeypatch.setattr(Resolver, "resolve", resolve)


@pytest.mark.parametrize("operation", ["start", "restart"])
@pytest.mark.parametrize("severity", ["required", "recommended"])
def test_existing_session_setup_precedes_both_secret_passes_and_runtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, captured_output, operation, severity
) -> None:
    status = SessionStatus.STOPPED if operation == "start" else SessionStatus.RUNNING
    db, events = _restart_fixture(tmp_path, monkeypatch, status=status)
    before = db.get_session("s1")
    evidence = _record_setup_check(monkeypatch, events, severity=severity)
    launch = manager.start_session if operation == "start" else manager.restart_session
    launch_kwargs = {"yes": True} if operation == "restart" else {}
    try:
        if severity == "required":
            with pytest.raises(RequiredSetupMissingError) as caught:
                launch(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE, **launch_kwargs)
            assert (caught.value.entity_kind, caught.value.entity_name) == ("agent", "a1")
            assert events == ["probe", "setup"]
            assert db.get_session("s1") == before
            assert not db.has_any_grant("a1", "ws1")
            assert captured_output.warnings == []
        else:
            launch(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE, **launch_kwargs)
            assert events == [
                "probe",
                "setup",
                "resolve",
                "resolve_env",
                *(["kill"] if operation == "restart" else []),
                "tmux_create",
            ]
            assert len(captured_output.warnings) == 1
        assert [(item.owner_kind, item.owner_name, item.status) for item in evidence] == [("agent", "a1", "absent")]
    finally:
        db.close()


@pytest.mark.parametrize("status", [SessionStatus.RUNNING, SessionStatus.BROKEN])
def test_nonlaunch_start_does_not_check_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, status) -> None:
    db, events = _restart_fixture(tmp_path, monkeypatch, status=status)
    evidence = _record_setup_check(monkeypatch, events)
    try:
        if status is SessionStatus.BROKEN:
            with pytest.raises(BrokenStateError):
                manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        else:
            manager.start_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
        assert events == []
        assert evidence == []
    finally:
        db.close()


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("severity", ["required", "recommended"])
def test_create_existing_owners_checks_setup_before_secrets_or_session_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, captured_output, admin, severity
) -> None:
    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    db.insert_agent("a1", "vm1", "agt-a1")
    evidence = _record_setup_check(monkeypatch, events, severity=severity)
    _record_resolve(monkeypatch, events)
    monkeypatch.setattr("agentworks.agents.grants.add_to_workspace_group", lambda *a, **k: events.append("grant"))
    monkeypatch.setattr("agentworks.sessions.tmux.deploy_restricted_config", lambda *a, **k: events.append("deploy"))
    try:

        def create() -> None:
            manager.create_session(
                db,
                config,
                name="s1",
                workspace="ws1",
                agent=None if admin else "a1",
                admin=admin,
                interaction=TtyInteractionPolicy.REFUSE,
            )

        if severity == "required":
            with pytest.raises(RequiredSetupMissingError):
                create()
            assert events == ["probe", "setup"]
            assert db.get_session("s1") is None
            assert not db.has_any_grant("a1", "ws1")
            assert captured_output.warnings == []
        else:
            create()
            assert events == ["probe", "setup", "resolve", *([] if admin else ["grant"]), "deploy", "tmux_create"]
            assert db.get_session("s1") is not None
            assert len(captured_output.warnings) == 1
        owner = ("vm", "vm1") if admin else ("agent", "a1")
        assert [(item.owner_kind, item.owner_name) for item in evidence] == [owner]
    finally:
        db.close()


@pytest.mark.parametrize(("new_workspace", "new_agent"), [(False, True), (True, False), (True, True)])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("severity", ["required", "recommended"])
def test_pending_owners_check_fresh_applied_setup_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, new_workspace, new_agent, complete, severity
) -> None:
    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    db.insert_agent("a1", "vm1", "agt-a1")
    facets: tuple[SetupFacet, ...] = (*(("workspace",) if new_workspace else ()), *(("user",) if new_agent else ()))
    evidence = _record_setup_check(monkeypatch, events, facets=facets, severity=severity)
    _record_resolve(monkeypatch, events)
    monkeypatch.setattr("agentworks.harness_setup.readiness.destination_id", lambda *a, **k: "a" * 64)

    collectors = []
    failures = []
    reported = []

    def report(failures_: list[SetupFailure]) -> None:
        reported.append(tuple(failures_))
        raise_setup_failures(failures_)

    monkeypatch.setattr("agentworks.sessions.manager._create.raise_setup_failures", report)
    monkeypatch.setattr("agentworks.sessions.manager._create_roll.raise_setup_failures", report)

    def realize(db_: Database, config: Any, registry: Any, **kwargs: Any) -> None:
        inputs = kwargs["setup_inputs"]
        collectors.append(kwargs["setup_failures"])
        if not complete:
            failure = SetupFailure(inputs.kind, inputs.name, inputs.facet, "shell", "fixture native failure")
            kwargs["setup_failures"].append(failure)
            failures.append(failure)
        events.append(f"realize_{inputs.kind}")
        if inputs.kind == "agent":
            db_.insert_agent(inputs.name, "vm1", f"agt-{inputs.name}")
        else:
            db_.insert_workspace(inputs.name, "/srv/s1", "vm1", "ws-s1")
        persist_creation_overlay(db_, inputs.kind, inputs.name, kwargs["overlay"])
        record = SetupRecord(
            component=inputs.component,
            integration="shell",
            destination_id="a" * 64,
            declaration=inputs.declaration("shell", inputs.activations["shell"]),
            complete=complete,
        )
        write_native_setup(db_, inputs.kind, inputs.name, NativeSetupState(records=(record,)), operation="create")

    def delete_agent(db_: Database, config: Any, *, name: str, **kwargs: Any) -> None:
        events.append("delete_agent")
        db_.delete_agent(name)

    def delete_workspace(db_: Database, config: Any, *, name: str, **kwargs: Any) -> None:
        events.append("delete_workspace")
        db_.delete_workspace(name)

    monkeypatch.setattr("agentworks.agents.realize.realize_agent", realize)
    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", realize)
    monkeypatch.setattr("agentworks.agents.manager.delete_agent", delete_agent)
    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", delete_workspace)
    monkeypatch.setattr("agentworks.agents.grants.add_to_workspace_group", lambda *a, **k: events.append("grant"))
    monkeypatch.setattr("agentworks.sessions.tmux.deploy_restricted_config", lambda *a, **k: events.append("deploy"))
    spec = '{"harness_integrations": {"shell": {}}}'
    try:

        def create() -> None:
            manager.create_session(
                db,
                config,
                name="s1",
                workspace=None if new_workspace else "ws1",
                new_workspace=new_workspace,
                workspace_spec=spec if new_workspace else None,
                agent=None if new_agent else "a1",
                new_agent=new_agent,
                agent_spec=spec if new_agent else None,
                vm_name="vm1",
                interaction=TtyInteractionPolicy.REFUSE,
            )

        if complete:
            create()
        else:
            error_type = RequiredSetupMissingError if severity == "required" else StateError
            with pytest.raises(error_type) as caught:
                create()
            if severity == "required":
                assert caught.value.entity_kind == ("workspace" if new_workspace else "agent")
                assert caught.value.entity_name == "s1"
                assert caught.value.hint is not None
        assert all(collector is collectors[0] for collector in collectors)
        assert collectors[0] == failures
        assert reported == [tuple(failures)]
        before_setup = [
            *([] if new_agent else ["probe"]),
            "resolve",
            *(["realize_workspace"] if new_workspace else []),
            *(["realize_agent", "probe"] if new_agent else []),
            "setup",
        ]
        launched = complete or severity == "recommended"
        after_setup = ["grant", "deploy", "tmux_create"] if launched else []
        assert events == before_setup + after_setup
        assert [(item.owner_kind, item.owner_name, item.status) for item in evidence] == [
            ("workspace" if facet == "workspace" else "agent", "s1", "current" if complete else "incomplete")
            for facet in facets
        ]
        assert (db.get_session("s1") is not None) is launched
        if not launched:
            assert not db.has_any_grant("s1" if new_agent else "a1", "s1" if new_workspace else "ws1")
        if new_agent:
            assert db.get_agent("s1") is not None
            assert db.instance_state.get_desired_overlay("agent", "s1") is not None
        if new_workspace:
            assert db.get_workspace("s1") is not None
            assert db.instance_state.get_desired_overlay("workspace", "s1") is not None
    finally:
        db.close()


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_optional_setup_failure_does_not_disable_core_failure_unwind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, failure_type
) -> None:
    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    _record_setup_check(monkeypatch, events, facets=("workspace", "user"), severity="recommended")
    core_error = failure_type("fixture session failure")
    collectors = []

    def realize(db_: Database, config: Any, registry: Any, **kwargs: Any) -> None:
        inputs = kwargs["setup_inputs"]
        collectors.append(kwargs["setup_failures"])
        kwargs["setup_failures"].append(
            SetupFailure(inputs.kind, inputs.name, inputs.facet, "shell", "fixture native failure")
        )
        events.append(f"realize_{inputs.kind}")
        if inputs.kind == "agent":
            db_.insert_agent(inputs.name, "vm1", f"agt-{inputs.name}")
        else:
            db_.insert_workspace(inputs.name, "/srv/s1", "vm1", "ws-s1")

    def delete_agent(db_: Database, config: Any, *, name: str, **kwargs: Any) -> None:
        events.append("delete_agent")
        db_.delete_agent(name)

    def delete_workspace(db_: Database, config: Any, *, name: str, **kwargs: Any) -> None:
        events.append("delete_workspace")
        db_.delete_workspace(name)

    def fail_session(*args: Any, **kwargs: Any) -> None:
        events.append("tmux_fail")
        raise core_error

    monkeypatch.setattr("agentworks.agents.realize.realize_agent", realize)
    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", realize)
    monkeypatch.setattr("agentworks.agents.manager.delete_agent", delete_agent)
    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", delete_workspace)
    monkeypatch.setattr("agentworks.sessions.tmux.create_session", fail_session)
    try:
        with pytest.raises(failure_type) as caught:
            manager.create_session(
                db,
                config,
                name="s1",
                new_workspace=True,
                new_agent=True,
                vm_name="vm1",
                workspace_spec='{"harness_integrations": {"shell": {}}}',
                agent_spec='{"harness_integrations": {"shell": {}}}',
                interaction=TtyInteractionPolicy.REFUSE,
            )
        assert caught.value is core_error
        assert collectors[0] is collectors[1]
        assert len(collectors[0]) == 2
        assert events == [
            "realize_workspace",
            "realize_agent",
            "probe",
            "setup",
            "tmux_fail",
            "delete_agent",
            "delete_workspace",
        ]
        assert db.get_session("s1") is None
        assert db.get_agent("s1") is None
        assert db.get_workspace("s1") is None
    finally:
        db.close()
