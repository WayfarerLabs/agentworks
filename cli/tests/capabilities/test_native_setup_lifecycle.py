"""Setup shares core ownership, eager inputs, and the owning row's commit."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from agentworks.agents.initializer import create_exclusive_agent_user
from agentworks.agents.realize import realize_agent
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.env.entry import EnvEntry
from agentworks.errors import ExternalError, StateError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.lifecycle import prepare_agent_setup, prepare_vm_setup, prepare_workspace_setup
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityBlock
from agentworks.secrets.orchestration import SecretTarget
from agentworks.ssh import SSHLogger as _RealSSHLogger
from agentworks.transports import Transport
from agentworks.vms.admin import AdminConfig
from agentworks.vms.templates import resolve_from_dict as resolve_vm
from agentworks.workspaces.realize import realize_workspace
from agentworks.workspaces.templates import ResolvedTemplate


def _env(**values):
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
    selected = [CapabilityBlock.of("shell")]
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
    assert retired is not None and retired.attachments == ()


@pytest.fixture
def native(monkeypatch, tmp_path):
    target = Mock(spec=Transport)
    target.run.return_value = SimpleNamespace(stdout="native-identity", ok=True)
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.harness_setup.lifecycle.site_platform_name", lambda *a: "fixture")
    monkeypatch.setattr("agentworks.agents.initializer.create_exclusive_agent_user", lambda *a, **k: None)
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

        def close(self):
            self.closed = True

    monkeypatch.setattr("agentworks.ssh.SSHLogger", Logger)
    return SimpleNamespace(target=target, loggers=loggers, removed_agent=removed_agent, removed_project=removed_project)


@pytest.mark.parametrize("kind", ["agent", "workspace"])
@pytest.mark.parametrize("fail_insert", [False, True])
def test_fresh_owner_buffers_setup_until_atomic_row_commit(db, registry, vm, native, monkeypatch, kind, fail_insert):
    name = "agent" if kind == "agent" else "project"
    env = _env(TOKEN={"secret": "setup-token"}, AGENTWORKS_SESSION="forged")
    inputs = SetupInputs(
        kind,
        name,
        kind,
        (CapabilityBlock.of("shell"),),
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

    monkeypatch.setattr(ShellIntegration, "user_init" if kind == "agent" else "workspace_init", initialize)
    if fail_insert:

        def fail(*args, **kwargs):
            raise RuntimeError("local insertion failed")

        monkeypatch.setattr(db, "insert_agent" if kind == "agent" else "insert_workspace", fail)

    def create():
        if kind == "agent":
            realize_agent(
                db,
                MagicMock(),
                registry,
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
                name=name,
                vm=vm,
                template=ResolvedTemplate("default"),
                setup_inputs=inputs,
                setup_values={"setup-token": "private-value"},
            )

    if fail_insert:
        with pytest.raises(ExternalError):
            create()
        assert read_native_setup(db, kind, name).records == ()
        (native.removed_agent if kind == "agent" else native.removed_project).assert_called_once()
    else:
        create()
        (record,) = read_native_setup(db, kind, name).records
        assert record.complete and record.claims == tuple(calls)
        assert "private-value" not in record.model_dump_json()
    assert calls and native.loggers[0].closed


@pytest.mark.parametrize("existing", ["user", "home", "race", "absent"])
def test_exclusive_agent_user_refuses_native_residue_and_races(vm, monkeypatch, existing):
    target = Mock(spec=Transport)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command.startswith("getent"):
            return SimpleNamespace(ok=existing == "user")
        if command.startswith("test"):
            return SimpleNamespace(ok=existing == "home")
        if existing == "race":
            raise RuntimeError("useradd: name taken")
        return SimpleNamespace(ok=True)

    target.run.side_effect = run
    monkeypatch.setattr("agentworks.agents.initializer.transport", lambda *a, **k: target)
    if existing in {"user", "home"}:
        with pytest.raises(StateError):
            create_exclusive_agent_user(vm, MagicMock(), "agt-a", shell="bash", logger=Mock())
        assert not any(command.startswith("useradd") for command in commands)
    elif existing == "race":
        with pytest.raises(RuntimeError):
            create_exclusive_agent_user(vm, MagicMock(), "agt-a", shell="bash", logger=Mock())
    else:
        create_exclusive_agent_user(vm, MagicMock(), "agt-a", shell="bash", logger=Mock())
        assert commands[-1] == "useradd -m -U -s /bin/bash agt-a"
    assert not any(command.startswith("userdel") for command in commands)


def test_failed_exclusive_creation_never_arms_realizer_rollback(db, registry, vm, native, monkeypatch):
    def race(*args, **kwargs):
        raise RuntimeError("competing useradd")

    monkeypatch.setattr("agentworks.agents.initializer.create_exclusive_agent_user", race)
    with pytest.raises(RuntimeError):
        realize_agent(
            db,
            MagicMock(),
            registry,
            name="a",
            vm=vm,
            template=ResolvedAgentTemplate("default"),
            credential_requests=(),
            credential_redactions=(),
        )
    native.removed_agent.assert_not_called()
    assert db.get_agent("a") is None and native.loggers[0].closed


def test_config_secret_registration_and_delivery_share_actual_owner(db, vm, native):
    from typing import Annotated, Literal

    from agentworks.capabilities.descriptor import Facet
    from agentworks.harness_setup.lifecycle import apply_agent_setup
    from agentworks.plugins import Plugin, seated_plugin
    from agentworks.schema import AgwModel, SecretRef
    from tests.plugins._fixtures import ConformingHarnessIntegration

    class Config(AgwModel):
        name: Literal["lifecycle-token"]
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
            (CapabilityBlock.of(Harness.name, token="config-secret"),),
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
                logger=Mock(),
                held=guard,
                operation="agent-create",
                buffered=True,
            )
        assert len(observed) == 1


def test_receipt_appearing_after_empty_preparation_refuses_before_core(db, registry, vm, native, monkeypatch):
    template = ResolvedAgentTemplate("default")
    prepared = prepare_agent_setup(db, registry, vm=vm, name="a", template=template)
    assert prepared is None
    later = NativeSetupState(
        records=(SetupRecord(component="agent", integration="shell", destination_id="a" * 64, declaration={}),)
    )
    write_native_setup(db, "agent", "a", later, operation="agent-create")
    exclusive = Mock()
    monkeypatch.setattr("agentworks.agents.initializer.create_exclusive_agent_user", exclusive)
    with pytest.raises(StateError):
        realize_agent(
            db,
            MagicMock(),
            registry,
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


@pytest.mark.parametrize("fail_user", [False, True])
def test_vm_and_admin_setup_follow_core_before_terminal_checkpoint(db, registry, vm, native, monkeypatch, fail_user):
    from agentworks.db import InitStatus
    from agentworks.debian import DebianRelease
    from agentworks.vms.initializer.driver import VMInitializationOperation, run_initialization
    from agentworks.vms.initializer.ssh_keys import AuthorizedKeysUnproven

    template = resolve_vm({})
    template.harness_integrations = [CapabilityBlock.of("shell")]
    template.env = _env(VM_ONLY="system", TOKEN={"secret": "vm-token"})
    admin = AdminConfig(env=_env(ADMIN_ONLY="admin"), harness_integrations=[CapabilityBlock.of("shell")])
    inputs = prepare_vm_setup(db, registry, name=vm.name, template=template, admin=admin)
    events = []

    def core(*args, **kwargs):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, vm.name):
            pass
        events.append("core")
        return AuthorizedKeysUnproven()

    def vm_setup(self, invocation):
        assert events == ["core"]
        assert invocation.environment["TOKEN"] == "vm-private"
        assert "ADMIN_ONLY" not in invocation.environment
        assert "AGENTWORKS_AGENT" not in invocation.environment
        events.append("vm")

    def user_setup(self, invocation):
        assert events == ["core", "vm"]
        assert invocation.username == vm.admin_username
        assert invocation.environment["ADMIN_ONLY"] == "admin"
        assert self.owner_kind == "vm" and self.owner_name == vm.name
        assert db.get_vm(vm.name).init_status != InitStatus.COMPLETE.value
        events.append("admin")
        if fail_user:
            raise RuntimeError("admin setup failed: vm-private")

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

    def run():
        run_initialization(
            db,
            MagicMock(),
            registry,
            template,
            admin,
            vm.name,
            native.target,
            (),
            "/home/admin",
            "admin",
            logger,
            debian_release=DebianRelease.TRIXIE,
            operation=VMInitializationOperation.VM_REINIT,
            setup_inputs=inputs,
            setup_values={"vm-token": "vm-private"},
        )

    if fail_user:
        with pytest.raises(RuntimeError):
            run()
    else:
        run()
    assert events == ["core", "vm", "admin"] + ([] if fail_user else ["keys"])
    logger.close()
    assert all("vm-private" not in (event.detail or "") for event in db.list_vm_events(vm.name))
    records = read_native_setup(db, "vm", vm.name).records
    assert records[0].component == "vm" and records[0].complete
    assert records[1].component == "admin" and records[1].complete is not fail_user
    assert db.get_vm(vm.name).init_status == (InitStatus.FAILED if fail_user else InitStatus.COMPLETE).value
