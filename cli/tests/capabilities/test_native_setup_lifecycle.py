"""Setup shares core ownership, eager inputs, and the owning row's commit."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from agentworks.agents.initializer import create_new_agent_user
from agentworks.agents.realize import realize_agent
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.artifacts.application import ArtifactApplication
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.env.entry import EnvEntry
from agentworks.errors import ExternalError, StateError
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, Result
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.lifecycle import prepare_agent_setup, prepare_vm_setup, prepare_workspace_setup
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityConfig
from agentworks.secrets.orchestration import SecretTarget
from agentworks.ssh import SSHLogger as _RealSSHLogger
from agentworks.transports import Transport
from agentworks.vms.admin import AdminConfig
from agentworks.vms.templates import resolve_from_dict as resolve_vm
from agentworks.workspaces.realize import realize_workspace
from agentworks.workspaces.templates import ResolvedTemplate
from tests.vm_access_support import answer_destination_identity

# Every access a command builds is the recording fake: these tests run no ssh.
pytestmark = pytest.mark.usefixtures("vm_access_recorder")


def _env(**values: str) -> dict[str, EnvEntry]:
    return {key: EnvEntry.model_validate(value) for key, value in values.items()}


@pytest.fixture
def registry():
    result = Registry.empty()
    origin = Origin.built_in(source="fixture")
    result.add("harness-integration", "shell", HarnessIntegrationEntry("shell", origin), origin)
    result.finalize()
    return result


@pytest.fixture
def vm(db):
    return db.insert_vm("box", site="fixture", hostname="box", admin_username="admin")


def test_empty_setup_does_not_resolve_ancestor_env(db, registry, vm, monkeypatch):
    monkeypatch.setattr("agentworks.vms.templates.resolve_live_template", lambda *a: pytest.fail("unused ancestor env"))
    assert prepare_agent_setup(db, registry, vm=vm, name="agent", template=ResolvedAgentTemplate("default")) is None
    assert prepare_workspace_setup(db, registry, vm=vm, name="project", template=ResolvedTemplate("default")) is None
    assert prepare_vm_setup(db, registry, name=vm.name, template=resolve_vm({}), admin=AdminConfig()) == ()


def test_component_inputs_keep_only_ancestors_and_retirement(db, registry, vm, monkeypatch):
    parent = resolve_vm({})
    parent.env = _env(VM="vm")
    monkeypatch.setattr("agentworks.vms.templates.resolve_live_template", lambda *a: parent)
    selected = {"shell": CapabilityConfig.model_validate({})}
    agent = prepare_agent_setup(
        db,
        registry,
        vm=vm,
        name="agent",
        template=ResolvedAgentTemplate("default", env=_env(USER="agent"), harness_integrations=selected),
    )
    project = prepare_workspace_setup(
        db,
        registry,
        vm=vm,
        name="project",
        template=ResolvedTemplate("default", env=_env(PROJECT="project"), harness_integrations=selected),
    )
    assert agent is not None and project is not None
    assert agent.target.vm == parent.env and agent.target.agent == _env(USER="agent")
    assert agent.target.admin is None and agent.target.workspace is None
    assert project.target.vm == parent.env and project.target.workspace == _env(PROJECT="project")
    assert project.target.admin is None and project.target.agent is None
    parent.harness_integrations = selected
    admin = AdminConfig(env=_env(USER="admin"), harness_integrations=selected)
    system, user = prepare_vm_setup(db, registry, name=vm.name, template=parent, admin=admin)
    assert system.target.admin is None and user.target.admin == admin.env
    assert system.kind == user.kind == "vm" and system.name == user.name == vm.name
    assert user.component == "admin"
    write_native_setup(
        db,
        "agent",
        "agent",
        NativeSetupState(
            records=(SetupRecord(component="agent", integration="shell", destination_id="a" * 64, declaration={}),)
        ),
        operation="agent-create",
    )
    retired = prepare_agent_setup(db, registry, vm=vm, name="agent", template=ResolvedAgentTemplate("default"))
    assert retired is not None and retired.activations == {}


@pytest.fixture
def native(monkeypatch, tmp_path, vm_access_recorder):
    vm_access_recorder.answer = answer_destination_identity
    target = Mock(spec=Transport)
    target.logger = None
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.harness_setup.lifecycle.site_platform_name", lambda *a: "fixture")
    monkeypatch.setattr("agentworks.agents.initializer.create_new_agent_user", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.agents.initializer.create_agent_on_vm", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.create_vm_workspace", lambda *a, **k: "/work/project")
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.generate_vscode_workspace",
        lambda *a, **k: tmp_path / "project.code-workspace",
    )
    monkeypatch.setattr("agentworks.agents.grants.materialize_grant_all_agents", lambda *a, **k: None)
    removed_agent = Mock()
    removed_project = Mock()
    monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", removed_agent)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", removed_project)
    loggers = []

    class Logger:
        display_path = "fixture.log"
        closed = False

        def __init__(self, *args, redactions=()):
            self.redactions = redactions
            loggers.append(self)

        def sanitize(self, text):
            for value in self.redactions:
                text = text.replace(value, "[REDACTED]")
            return text

        def warning(self, text):
            pass

        def close(self):
            self.closed = True

        def record(self, request, result):
            pass

    monkeypatch.setattr("agentworks.ssh.SSHLogger", Logger)
    return SimpleNamespace(target=target, loggers=loggers, removed_agent=removed_agent, removed_project=removed_project)


@pytest.mark.parametrize("kind", ["agent", "workspace"])
@pytest.mark.parametrize("fail_insert", [False, True])
@pytest.mark.parametrize("fail_setup", [False, True])
def test_fresh_owner_buffers_setup_until_atomic_row_commit(
    db, registry, vm, native, monkeypatch, kind, fail_insert, fail_setup
):
    name = "agent" if kind == "agent" else "project"
    env = _env(TOKEN={"secret": "setup-token"}, AGENTWORKS_SESSION="forged")
    inputs = SetupInputs(
        kind,
        name,
        kind,
        {"shell": CapabilityConfig.model_validate({})},
        SecretTarget(
            vm=_env(VM="parent"), agent=env if kind == "agent" else None, workspace=env if kind == "workspace" else None
        ),
    )
    calls = []

    def initialize(self, invocation):
        assert db.get_agent(name) is None and db.get_workspace(name) is None
        assert read_native_setup(db, kind, name).records == ()
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, vm.name):
            pass
        assert invocation.environment["TOKEN"] == "private-value"
        assert invocation.environment["VM"] == "parent"
        assert "AGENTWORKS_SESSION" not in invocation.environment
        assert invocation.environment["AGENTWORKS_VM"] == vm.name
        assert "private-value" in native.loggers[0].redactions
        assert invocation.secrets == {}  # Shell declares no config secrets.
        claim = NativeClaim(role="fixture", identifier="installed", destination="/fixture")
        invocation.checkpoint((claim,))
        assert read_native_setup(db, kind, name).records == ()
        calls.append(claim)
        if fail_setup:
            raise ExternalError("fixture setup interruption: private-value")
        return ArtifactApplication()

    monkeypatch.setattr(ShellIntegration, "user_init" if kind == "agent" else "workspace_init", initialize)
    if fail_insert:

        def fail(*args, **kwargs):
            raise RuntimeError("local insertion failed")

        monkeypatch.setattr(db, "insert_agent" if kind == "agent" else "insert_workspace", fail)

    failures: list[SetupFailure] = []

    def create() -> None:
        if kind == "agent":
            realize_agent(
                db,
                MagicMock(),
                registry,
                setup_failures=failures,
                name=name,
                vm=vm,
                template=ResolvedAgentTemplate("default"),
                credential_requests=(),
                credential_redactions=(),
                setup_inputs=inputs,
                setup_values={"setup-token": "private-value"},
            )
        else:
            realize_workspace(
                db,
                MagicMock(),
                registry,
                setup_failures=failures,
                name=name,
                vm=vm,
                template=ResolvedTemplate("default"),
                setup_inputs=inputs,
                setup_values={"setup-token": "private-value"},
            )

        raise_setup_failures(failures)

    if fail_insert:
        with pytest.raises(ExternalError):
            create()
        assert read_native_setup(db, kind, name).records == ()
        (native.removed_agent if kind == "agent" else native.removed_project).assert_called_once()
    else:
        if fail_setup:
            with pytest.raises(StateError):
                create()
        else:
            create()
        (record,) = read_native_setup(db, kind, name).records
        assert record.complete is not fail_setup and record.claims == tuple(calls)
        assert "private-value" not in record.model_dump_json()
        assert (db.get_agent(name) if kind == "agent" else db.get_workspace(name)) is not None
        (native.removed_agent if kind == "agent" else native.removed_project).assert_not_called()
    assert calls and native.loggers[0].closed


@pytest.mark.parametrize("existing", ["user", "home", "race", "absent"])
def test_exclusive_agent_user_refuses_native_residue_and_races(vm, existing, vm_access_recorder):
    useradd = Command(("useradd", "-m", "-U", "-s", "/bin/bash", "agt-a"), elevated=True)

    def answer(call):
        request = call.request
        if isinstance(request, Command) and request.argv[:2] == ("getent", "passwd"):
            return Result(Completed(0 if existing == "user" else 2))
        if isinstance(request, Script):
            return Result(Completed(0 if existing == "home" else 1))
        if request == useradd and existing == "race":
            return CommandFailed(vm.name, 9, Result(Completed(9)), streams_logged=True)
        return None

    vm_access_recorder.answer = answer
    if existing in {"user", "home"}:
        with pytest.raises(StateError):
            create_new_agent_user(vm, MagicMock(), "agt-a", shell="bash", logger=Mock())
        assert useradd not in vm_access_recorder.requests("run")
    elif existing == "race":
        with pytest.raises(CommandFailed):
            create_new_agent_user(vm, MagicMock(), "agt-a", shell="bash", logger=Mock())
    else:
        create_new_agent_user(vm, MagicMock(), "agt-a", shell="bash", logger=Mock())
        assert vm_access_recorder.requests("run")[-1] == useradd
    assert all(request.elevated for request in vm_access_recorder.requests("run"))
    assert not any(
        isinstance(request, Command) and request.argv[0] == "userdel" for request in vm_access_recorder.requests("run")
    )


def test_failed_exclusive_creation_never_arms_realizer_rollback(db, registry, vm, native, monkeypatch):
    def race(*args, **kwargs):
        raise RuntimeError("competing useradd")

    monkeypatch.setattr("agentworks.agents.initializer.create_new_agent_user", race)
    with pytest.raises(RuntimeError):
        realize_agent(
            db,
            MagicMock(),
            registry,
            setup_failures=[],
            name="a",
            vm=vm,
            template=ResolvedAgentTemplate("default"),
            credential_requests=(),
            credential_redactions=(),
        )
    native.removed_agent.assert_not_called()
    assert db.get_agent("a") is None and native.loggers[0].closed


def test_config_secret_registration_and_delivery_share_actual_owner(db, vm, native):
    from typing import Annotated

    from agentworks.capabilities.descriptor import Facet
    from agentworks.harness_setup.lifecycle import apply_agent_setup
    from agentworks.plugins import Plugin, seated_plugin
    from agentworks.schema import AgwModel, SecretRef
    from tests.plugins._fixtures import ConformingHarnessIntegration

    class Config(AgwModel):
        token: Annotated[str, SecretRef(usage="fixture credential")]

    observed = []

    class Harness(ConformingHarnessIntegration):
        name = "lifecycle-token"
        description = "Lifecycle secret delivery fixture"

        @classmethod
        def config_for(cls, facet: Facet | None = None):
            return Config if facet == "user" else super().config_for(facet)

        def user_init(self, invocation):
            assert self.owner_kind == "agent" and self.owner_name == "a"
            assert invocation.secrets == {"config-secret": "config-private"}
            assert invocation.environment["ENV_TOKEN"] == "env-private"
            observed.append(invocation)
            from agentworks.artifacts.application import ArtifactApplication

            return ArtifactApplication()

    with seated_plugin(Plugin(name="lifecycle-token", capabilities={"harness-integration": (Harness,)})):
        from tests.conftest import registry_with_shell

        registry = registry_with_shell()
        origin = Origin.built_in(source="fixture")
        registry.add("harness-integration", Harness.name, HarnessIntegrationEntry(Harness.name, origin), origin)
        registry.finalize()
        inputs = SetupInputs(
            "agent",
            "a",
            "agent",
            {Harness.name: CapabilityConfig.model_validate({"token": "config-secret"})},
            SecretTarget(vm={}, agent=_env(ENV_TOKEN={"secret": "env-secret"})),
        )
        resolver = Mock()
        inputs.register(resolver, registry)
        resolver.register_targets.assert_called_once_with([inputs.target])
        resolver.register_name.assert_called_once_with("config-secret")
        with native_mutation_guard(db.path, vm.name) as guard:
            apply_agent_setup(
                db,
                MagicMock(),
                registry,
                inputs=inputs,
                vm=vm,
                username="agt-a",
                values={
                    "config-secret": "config-private",
                    "env-secret": "env-private",
                    "other-secret": "other-private",
                },
                logger=Mock(redactions=()),
                held=guard,
                operation="agent-create",
                failures=[],
                buffered=True,
            )
        assert len(observed) == 1


def test_applied_state_appearing_after_empty_preparation_refuses_before_core(db, registry, vm, native, monkeypatch):
    template = ResolvedAgentTemplate("default")
    prepared = prepare_agent_setup(db, registry, vm=vm, name="a", template=template)
    assert prepared is None
    later = NativeSetupState(
        records=(SetupRecord(component="agent", integration="shell", destination_id="a" * 64, declaration={}),)
    )
    write_native_setup(db, "agent", "a", later, operation="agent-create")
    exclusive = Mock()
    monkeypatch.setattr("agentworks.agents.initializer.create_new_agent_user", exclusive)
    with pytest.raises(StateError):
        realize_agent(
            db,
            MagicMock(),
            registry,
            setup_failures=[],
            name="a",
            vm=vm,
            template=template,
            credential_requests=(),
            credential_redactions=(),
            setup_inputs=prepared,
        )
    exclusive.assert_not_called()
    native.removed_agent.assert_not_called()
    assert read_native_setup(db, "agent", "a") == later


@pytest.mark.parametrize("failed_components", [(), ("vm",), ("admin",), ("vm", "admin")])
def test_vm_and_admin_setup_follow_core_before_terminal_checkpoint(
    db, registry, vm, native, monkeypatch, failed_components, vm_access_recorder
):
    from agentworks.db import InitStatus
    from agentworks.debian import DebianRelease
    from agentworks.vms.initializer.driver import VMInitializationOperation, run_initialization
    from agentworks.vms.initializer.ssh_keys import AuthorizedKeysUnproven

    template = resolve_vm({})
    template.harness_integrations = {"shell": CapabilityConfig.model_validate({})}
    template.env = _env(VM_ONLY="system", TOKEN={"secret": "vm-token"})
    admin = AdminConfig(
        env=_env(ADMIN_ONLY="admin"), harness_integrations={"shell": CapabilityConfig.model_validate({})}
    )
    inputs = prepare_vm_setup(db, registry, name=vm.name, template=template, admin=admin)
    events = []

    def core(*args, **kwargs):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, vm.name):
            pass
        events.append("core")
        return AuthorizedKeysUnproven()

    def vm_setup(self, invocation):
        assert events == ["core"]
        # Setup runs on Phase B's own access and bound.
        assert invocation.access is vm_access_recorder.access(vm_name=vm.name)
        assert invocation.files.deadline == 60
        assert invocation.environment["TOKEN"] == "vm-private"
        assert "ADMIN_ONLY" not in invocation.environment
        assert "AGENTWORKS_AGENT" not in invocation.environment
        events.append("vm")
        if "vm" in failed_components:
            raise ExternalError("VM setup failed: vm-private")
        return ArtifactApplication()

    def user_setup(self, invocation):
        assert events == ["core", "vm"]
        assert invocation.username == vm.admin_username
        assert invocation.environment["ADMIN_ONLY"] == "admin"
        assert self.owner_kind == "vm" and self.owner_name == vm.name
        assert db.get_vm(vm.name).init_status != InitStatus.COMPLETE.value
        events.append("admin")
        if "admin" in failed_components:
            raise ExternalError("admin setup failed: vm-private")
        return ArtifactApplication()

    def final_keys(*args, **kwargs):
        assert events == ["core", "vm", "admin"]
        events.append("keys")
        return AuthorizedKeysUnproven()

    monkeypatch.setattr("agentworks.vms.initializer.driver._phase_b_setup", core)
    monkeypatch.setattr("agentworks.vms.initializer.driver._reconcile_authorized_keys", final_keys)
    monkeypatch.setattr(ShellIntegration, "vm_init", vm_setup)
    monkeypatch.setattr(ShellIntegration, "user_init", user_setup)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", db.path.parent / "logs")
    logger = _RealSSHLogger(vm.name, "setup-fixture", redactions=("vm-private",))

    failures: list[SetupFailure] = []

    def run() -> None:
        run_initialization(
            db,
            MagicMock(),
            registry,
            template,
            admin,
            vm.name,
            vm_access_recorder.access(vm_name=vm.name),
            (),
            "/home/admin",
            "admin",
            logger,
            legacy_target=native.target,
            debian_release=DebianRelease.TRIXIE,
            operation=VMInitializationOperation.VM_REINIT,
            setup_inputs=inputs,
            setup_values={"vm-token": "vm-private"},
            setup_failures=failures,
        )

    run()
    assert len(failures) == len(failed_components)
    assert events == ["core", "vm", "admin", "keys"]
    logger.close()
    assert all("vm-private" not in (event.detail or "") for event in db.list_vm_events(vm.name))
    records = read_native_setup(db, "vm", vm.name).records
    assert records[0].component == "vm" and records[0].complete == ("vm" not in failed_components)
    assert records[1].component == "admin" and records[1].complete == ("admin" not in failed_components)
    assert db.get_vm(vm.name).init_status == (InitStatus.PARTIAL if failed_components else InitStatus.COMPLETE).value
