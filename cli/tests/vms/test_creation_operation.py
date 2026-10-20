"""Creation orchestration with real SQLite, owners, route assembly and closeout.

Provider provisioning/rollback, guest/account preparation, public bootstrap/join,
release observations and SSH trust maintenance are simulated boundaries. No guest
program or SSH delivery executes. These tests establish neither provider resource
disposition nor native bootstrap, credential, secret delivery or availability proof.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import OperationScope as ContextScope
from agentworks.capabilities.base import RunContext, ScopeLevel, TargetAbsence
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import (
    Database,
    InitStatus,
    OperationResourceKind,
    OperationScope,
    ProvisioningStatus,
    VMStatus,
)
from agentworks.errors import AgentworksError, AlreadyExistsError, ConfigError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._process import LocalProcessOwner
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._target_identity import TargetIdentityPreparation, TargetIdentityStatus
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.binding import NativeExecutionBinding, _IndependentJobAvailability
from agentworks.execution.carrier import Deadline, PreparedInvocation
from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection, SSHSettings, SSHTrustFiles
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv
from agentworks.execution.carriers.ssh.trust import block_trust, refresh_trust, resolve_trust, trust_status
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionIdentity, ExecutionRoute, ExecutionTarget
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner
from agentworks.vms import _creation_operation as creation
from agentworks.vms import _creation_trust, _native_operation
from agentworks.vms._creation_access import VMCreationResult
from agentworks.vms._native_operation import VMOperationControlFact
from agentworks.vms._ssh_native_binding import vm_ssh_connection
from agentworks.vms.target_identity import compose_managed_vm_target_identity
from agentworks.vms.target_preparation import VMTargetPreparation, VMTargetPreparationStatus
from tests.plugins._fixtures import FixtureVMPlatform
from tests.vms.test_creation_guest import creation_request as creation_request
from tests.vms.test_creation_trust import fixture as fixture

_ADDRESS = "100.64.0.7"
_SCOPE = OperationScope(OperationResourceKind.VM, "created")


class _CreationAccess:
    """Simulate only the provider adapter, retaining actual durable fixture debt."""

    guest_check = None
    route_check = None
    independent_availability = _IndependentJobAvailability.NO_IDLE_STOP

    def __init__(self, harness: SimpleNamespace, owner: OperationOwner, custody: LocalDeliveryCustody) -> None:
        self.harness, self.owner, self.custody = harness, owner, custody
        self.binding: NativeExecutionBinding = harness.binding
        self.locator: ProviderLocator = harness.locator
        self.platform_metadata: dict[str, str] = {}
        self.debt: LifecycleObligation | None = None
        self.settled = True
        self.resolve_debt = True
        self.failure: BaseException | None = None
        self.transferred = False
        self.rollback = False

    def provision(self, deadline: Deadline) -> VMCreationResult:
        self.harness.note("provision", deadline)
        self.debt = self.owner.register_lifecycle_obligation("fixture-creation", payload_version=1, payload=b"{}")
        self.debt.mark_possible_effect()
        self.platform_metadata = {"opaque/receipt": "provider-owned:not-a-candidate-id"}
        if self.failure is not None:
            raise self.failure
        return VMCreationResult(
            self.platform_metadata, self.binding, self.locator, ssh_trust_published=self.harness.ssh_trust_published
        )

    def complete_bootstrap(self, deadline: Deadline) -> None:
        self.harness.note("transfer", deadline)
        assert not self.transferred and not self.rollback
        assert self.harness.db.get_vm("created").tailscale_host == _ADDRESS
        self.transferred = True

    def request_rollback(self) -> None:
        assert not self.transferred
        self.rollback = True

    def observe_power(self, deadline: Deadline) -> VMStatus:
        self.harness.note("power", deadline)
        return VMStatus.RUNNING

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        self.harness.note("locator", deadline)
        return self.locator

    def settle(self, deadline: Deadline) -> bool:
        self.harness.note("settle", deadline)
        assert self.custody.settled
        if self.settled and self.resolve_debt and self.debt is not None:
            self.debt.resolve()
        return self.settled


@pytest.fixture
def harness(tmp_path, monkeypatch, creation_request):
    with closing(Database(tmp_path / "state.db")) as db:
        settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
        policy = SSHTrustFiles((tmp_path / "known-hosts",))
        carrier = SSHCarrier(SSHConnection("127.0.0.1", "agentworks", settings.identity_file, policy))
        binding = NativeExecutionBinding(
            carrier,
            "agentworks",
            RuntimeSelection(RuntimeTargetOS.LINUX),
            _independent_availability=_IndependentJobAvailability.NO_IDLE_STOP,
        )
        host = ExecutionTarget(
            None, None, carrier.features, ExecutionIdentity("host", 1000, 1000, (1000,)), ExecutionRoute.PLATFORM_HOST
        )
        h = SimpleNamespace(
            db=db,
            request=creation_request,
            settings=settings,
            binding=binding,
            platform=FixtureVMPlatform("fixture", {}),
            locator=ProviderLocator("opaque:created:association"),
            deadline=Deadline.after(60),
            root=PurePosixPath("/trusted"),
            events=[],
            routes=[],
            access=None,
            host=host,
            secrets={"provider": "resolved-fixture-value"},
            workflow=None,
            ssh_trust_published=False,
        )
        h.ctx = RunContext(
            config=SimpleNamespace(operator=SimpleNamespace(ssh=settings)),
            operation_scope=ContextScope(ScopeLevel.VM, vm="created"),
            platform_host_execution_target=host,
            secrets=h.secrets,
        )

        def note(event: str, deadline: Deadline) -> None:
            assert type(deadline) is Deadline and deadline.expires_at is not None and not deadline.expired
            h.events.append((event, deadline))

        h.note = note

        def deny(*args, **kwargs):
            pytest.fail("unexpected legacy operation or actual SSH delivery")

        for name in ("create", "native_transport", "build_native_execution_access", "start", "delete"):
            monkeypatch.setattr(h.platform, name, deny)
        monkeypatch.setattr(SSHCarrier, "execute", deny)

        def admit_policy(connection):
            assert connection.host == "0.0.0.0" and h.access is None
            assert connection.host_key_alias == "agw-vm-" + h.request.instance_marker
            assert db.operations.inspect(_SCOPE) is not None
            return policy

        monkeypatch.setattr(creation, "admit_connection", admit_policy)

        def build(request, ctx, *, owner, custody):
            assert request is h.request and ctx is h.ctx
            claim = db.operations.inspect(_SCOPE)
            vm = db.get_vm("created")
            assert claim is not None and claim.ownership == owner.ownership
            assert vm is not None and vm.provisioning_status == ProvisioningStatus.IN_PROGRESS.value
            assert h.access is None
            h.access = _CreationAccess(h, owner, custody)
            h.events.append(("build", h.deadline))
            return h.access

        monkeypatch.setattr(h.platform, "build_creation_access", build)

        def prepare(vm, platform, locator, selected_binding, *, deadline, owner, observe_locator):
            note("guest", deadline)
            assert owner is h.access.owner and observe_locator.__self__ is h.access
            guest = VMGuestIdentity(vm.instance_marker, "12345678-1234-1234-1234-123456789abc", 1)
            target = compose_managed_vm_target_identity(vm, locator, guest)
            return VMTargetPreparation(
                VMTargetPreparationStatus.PREPARED, target, SimpleNamespace(observation=SimpleNamespace(identity=guest))
            )

        def identity(selected_carrier, *, deadline, owner, **kwargs):
            note("account", deadline)
            assert owner is h.access.owner
            ordinary = IdentityPlan(IdentityExpectation(1000, 1000, (1000,)), IdentityMode.DIRECT)
            elevated = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
            return TargetIdentityPreparation(TargetIdentityStatus.PREPARED, ordinary, elevated, None, None, None)

        monkeypatch.setattr(_native_operation, "prepare_managed_vm_target_from_platform", prepare)
        monkeypatch.setattr(_native_operation, "prepare_target_identity", identity)
        h.prepare_identity = identity
        assemble = _native_operation._prepare_route

        def route(workflow, *args, **kwargs):
            h.workflow = workflow
            result = assemble(workflow, *args, **kwargs)
            h.routes.append(result)
            return result

        monkeypatch.setattr(creation, "_prepare_route", route)

        def bootstrap(operation, request, deadline):
            note("bootstrap", deadline)
            assert operation is h.routes[0] and request is h.request
            return _ADDRESS

        def attest(operation, release, deadline):
            note("attest", deadline)
            assert operation is h.routes[0]
            return release

        monkeypatch.setattr(creation, "bootstrap_created_guest", bootstrap)
        monkeypatch.setattr(creation, "attest_created_guest", attest)

        class Trust:
            def __init__(
                self,
                owner: OperationOwner,
                *,
                vm_name: str,
                admin_username: str,
                instance_marker: str,
                endpoint: str,
                settings: SSHSettings,
                deadline: Deadline,
            ) -> None:
                assert owner is h.access.owner and deadline is h.deadline
                assert instance_marker == h.request.instance_marker
                vm_ssh_connection(vm_name, admin_username, endpoint, settings, instance_marker=instance_marker)

            def prepare(self) -> None:
                note("trust", h.deadline)

            def close(self, deadline: Deadline) -> bool:
                note("trust-close", deadline)
                return True

        monkeypatch.setattr(creation, "CreationSSHTrust", Trust)
        h.operation = lambda: creation.vm_creation_context(
            db, h.request, h.platform, h.ctx, deadline=h.deadline, trusted_root=h.root
        )
        yield h


@pytest.mark.parametrize("ssh_trust_published", [False, True])
def test_claim_precedes_row_and_two_routes_share_owner_and_original_budget(harness, monkeypatch, ssh_trust_published):
    h = harness
    h.ssh_trust_published = ssh_trust_published
    insert = h.db.insert_vm

    def insert_claimed(*args, **kwargs):
        assert h.db.operations.inspect(_SCOPE) is not None and h.access is None
        return insert(*args, **kwargs)

    monkeypatch.setattr(h.db, "insert_vm", insert_claimed)
    with h.operation() as ctx:
        native, canonical = h.routes
        assert native.owner is canonical.owner is h.access.owner
        assert h.workflow.access is h.workflow.creation is h.access
        assert len(h.workflow.routes) == 2 and h.workflow.routes[0].views is native
        assert (
            canonical.execution_operation._native_binding._independent_availability
            is native.execution_operation._native_binding._independent_availability
            is h.access.independent_availability
        )
        assert ctx.admin_execution_target() is canonical.target
        assert canonical.target.route is ExecutionRoute.CANONICAL
        assert ctx.admin_target() is ctx.agent_target() is ctx.agent_execution_target() is None
        assert ctx.agent_execution_target_absence() is TargetAbsence.UNAVAILABLE
        assert ctx.platform_host_execution_target() is h.host and ctx.secret("provider") == h.secrets["provider"]
        assert ctx.config is h.ctx.config and ctx.operation_scope is h.ctx.operation_scope
        assert h.workflow.routes[0].body_admission_closed and not h.workflow.body_admission_closed
        with pytest.raises(StateError):
            native.execution.run(Command(("true",)), profile=Protection.DIRECT)
        assert canonical.execution._deadline() is h.deadline
        assert h.db.get_vm("created").init_status == InitStatus.PENDING.value
        assert (h.workflow.trust is None) is ssh_trust_published
        assert [event for event, _ in h.events].count("trust") == (0 if ssh_trust_published else 1)
    assert all(budget is h.deadline for _, budget in h.events)
    assert h.db.operations.inspect(_SCOPE) is None
    assert h.access.transferred and not h.access.rollback
    with pytest.raises(StateError):
        canonical.execution.run(Command(("true",)), profile=Protection.DIRECT)


@pytest.mark.parametrize("fact", [None, 0, 1, "false"])
def test_creation_result_requires_exact_publication_fact(harness, fact):
    h = harness
    with pytest.raises(ValidationError):
        VMCreationResult({}, h.binding, h.locator, ssh_trust_published=fact)
    with pytest.raises(TypeError):
        VMCreationResult({}, h.binding, h.locator)
    assert not h.events and h.access is None


@pytest.mark.parametrize("policy", ["published", "refreshed", "blocked"])
def test_public_publication_has_one_writer_and_canonical_readmits_current_strict_policy(
    harness, fixture, monkeypatch, policy
):
    h, p = harness, fixture
    h.request.instance_marker = p.resource_id.removeprefix("vm:")
    h.settings = p.settings
    h.ctx.config.operator.ssh = p.settings
    h.ssh_trust_published = True
    provision, settle = _CreationAccess.provision, _CreationAccess.settle
    prepare = _native_operation.prepare_managed_vm_target_from_platform
    attest = creation.attest_created_guest
    publications, retained, canonical_admissions = [], [], []

    def publish(*args, **kwargs):
        publications.append(kwargs["authority"])
        return refresh_trust(*args, **kwargs)

    def provision_public(self, deadline):
        result = provision(self, deadline)
        p.db, p.owner = h.db, self.owner
        trust = _creation_trust.CreationSSHTrust(
            self.owner,
            vm_name=h.request.vm_name,
            admin_username=h.request.admin_username,
            instance_marker=h.request.instance_marker,
            endpoint=p.endpoint,
            settings=p.settings,
            deadline=deadline,
        )
        retained.append(trust)
        trust.prepare()
        h.binding = self.binding = replace(h.binding, carrier=SSHCarrier(trust._connection))
        return replace(result, binding=self.binding)

    def settle_public(self, deadline):
        assert retained[0].close(deadline)
        return settle(self, deadline)

    def attest_and_update(*args):
        release = attest(*args)
        generation = trust_status(p.bundle).generation
        if policy == "blocked":
            block_trust(p.bundle, expected_generation=generation)
        elif policy == "refreshed":
            refresh_trust(
                p.bundle, sources=resolve_trust(p.bundle), authority="fixture-update", expected_generation=generation
            )
        return release

    def prepare_strict(vm, platform, locator, selected_binding, **kwargs):
        if selected_binding is not h.binding:
            connection = selected_binding.carrier._connection
            assert connection.host == _ADDRESS and connection.host != p.endpoint
            assert connection.host_key_alias == retained[0]._connection.host_key_alias
            assert connection.trust == p.bundle
            current = admit_connection(connection)
            assert current == resolve_trust(p.bundle)
            argv = build_ssh_argv(connection, PreparedInvocation(("true",)), trust=current, executable="/fixture/ssh")
            assert "StrictHostKeyChecking=yes" in argv
            assert "StrictHostKeyChecking=accept-new" not in argv
            canonical_admissions.append(current)
        return prepare(vm, platform, locator, selected_binding, **kwargs)

    def no_canonical_enrollment(*args, **kwargs):
        pytest.fail("published instance attempted a second SSH enrollment")

    monkeypatch.setattr(_CreationAccess, "provision", provision_public)
    monkeypatch.setattr(_CreationAccess, "settle", settle_public)
    monkeypatch.setattr(_creation_trust, "refresh_trust", publish)
    monkeypatch.setattr(creation, "CreationSSHTrust", no_canonical_enrollment)
    monkeypatch.setattr(creation, "attest_created_guest", attest_and_update)
    monkeypatch.setattr(_native_operation, "prepare_managed_vm_target_from_platform", prepare_strict)
    outcome = pytest.raises(StateError) if policy == "blocked" else nullcontext()
    with outcome, h.operation() as ctx:
        assert policy != "blocked"
        assert ctx.admin_execution_target().route is ExecutionRoute.CANONICAL
    assert len(canonical_admissions) == (0 if policy == "blocked" else 1)
    assert publications == ["core-creation:" + p.resource_id]
    assert len(p.calls) == 2
    assert sum("StrictHostKeyChecking=accept-new" in argv for argv in p.calls) == 1
    assert h.workflow.trust is None and h.access.transferred and not h.access.rollback
    assert p.candidate.exists() and h.db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize(
    "refusal", ["claim", "row", "scope", "missing-policy", "identity", "client", "join-key", "nested"]
)
def test_refusals_precede_provider_effects_and_preserve_existing_rows(harness, refusal):
    h = harness
    other = None
    expected: type[AgentworksError] = StateError
    if refusal == "claim":
        other = OperationOwner.acquire(h.db.operations, _SCOPE, "conflict")
    elif refusal == "row":
        h.db.insert_vm("created", "other", "untouched")
        expected = AlreadyExistsError
    elif refusal == "scope":
        h.ctx = RunContext(config=h.ctx.config, operation_scope=ContextScope(ScopeLevel.VM, vm="other"))
        expected = ValidationError
    elif refusal == "missing-policy":
        h.ctx.config.operator.ssh = None
        expected = ConfigError
    elif refusal == "join-key":
        h.request.tailscale_auth_key = "invalid\nkey"
        expected = ValidationError
    elif refusal in {"identity", "client"}:
        updates = (
            {"identity_file": h.settings.identity_file.parent / "$KEY"}
            if refusal == "identity"
            else {"ssh_executable": "ssh --option"}
        )
        h.ctx.config.operator.ssh = replace(h.settings, **updates)
        expected = ValidationError
    before = h.db.get_vm("created")
    with (
        pytest.raises(expected),
        h.db.transaction() if refusal == "nested" else nullcontext(),
        h.operation(),
    ):
        pytest.fail("refused creation admitted body")
    assert h.db.get_vm("created") == before
    assert not h.events
    if other is not None:
        assert h.db.operations.inspect(_SCOPE).ownership == other.ownership
        other.close()
    else:
        assert h.db.operations.inspect(_SCOPE) is None


def test_local_policy_readiness_failure_leaves_no_row_and_allows_same_name_retry(harness, monkeypatch):
    h = harness

    def unavailable(connection):
        raise ValidationError("fixture unreadable identity or blocked trust")

    with monkeypatch.context() as policy_patch:
        policy_patch.setattr(creation, "admit_connection", unavailable)
        with pytest.raises(ValidationError), h.operation():
            pytest.fail("unavailable policy admitted creation")
    assert h.db.get_vm("created") is None and h.db.operations.inspect(_SCOPE) is None
    assert h.access is None and not h.events
    with h.operation():
        assert h.db.get_vm("created").tailscale_host == _ADDRESS
    assert h.db.operations.inspect(_SCOPE) is None


def test_body_exception_after_bootstrap_retains_addressable_vm_without_initialization_claim(harness):
    h = harness
    control = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught, h.operation():
        raise control
    assert caught.value is control and control.__cause__ is None
    vm = h.db.get_vm("created")
    assert vm.tailscale_host == _ADDRESS and vm.platform_metadata == h.access.platform_metadata
    assert vm.provisioning_status == ProvisioningStatus.COMPLETE.value and vm.init_status == InitStatus.PENDING.value
    assert h.access.transferred and not h.access.rollback and h.db.operations.inspect(_SCOPE) is None


def test_closing_keeps_provider_selection_checks_available_without_reopening_body(harness):
    h = harness
    with h.operation():
        canonical = h.routes[1]
        check = canonical.execution_operation._route_check
        assert check is not None
        h.workflow.body_admission_closed = True
        h.access.owner.stop_admission()
        fresh = Deadline.after(30)
        check(fresh)
        assert h.events[-2:] == [("power", fresh), ("locator", fresh)]
        with pytest.raises(StateError):
            canonical.execution.run(Command(("true",)), profile=Protection.DIRECT, deadline=fresh)
    assert h.db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("committed", [False, True])
def test_address_write_failure_reconciles_actual_commit_without_bootstrap_replay(harness, monkeypatch, committed):
    h = harness
    control = KeyboardInterrupt()
    transaction = h.db.transaction
    armed = True

    @contextmanager
    def lose_publication_reply():
        nonlocal armed
        publication = (
            h.workflow is not None and h.workflow.phase is creation._BootstrapPhase.ADDRESS_PUBLICATION and armed
        )
        if publication:
            armed = False
        with transaction():
            yield
            if publication and not committed:
                raise control
        if publication and committed:
            raise control

    monkeypatch.setattr(h.db, "transaction", lose_publication_reply)
    with pytest.raises(KeyboardInterrupt) as caught, h.operation():
        pytest.fail("interrupted publication admitted body")
    assert caught.value is control and control.__cause__ is None
    assert h.db.get_vm("created").tailscale_host == (_ADDRESS if committed else None)
    assert h.access.transferred is committed and h.access.rollback is not committed
    assert [event for event, _ in h.events].count("bootstrap") == 1
    assert not any(event in {"attest", "trust"} for event, _ in h.events)
    assert h.db.get_vm("created").provisioning_status == ProvisioningStatus.FAILED.value
    assert h.db.operations.inspect(_SCOPE) is None


def test_newly_proved_metadata_extends_addressability_without_replacing_prior_facts(harness):
    h = harness
    with h.operation():
        original = dict(h.db.get_vm("created").platform_metadata)
        ownership = h.workflow.owner.ownership
        h.access.platform_metadata = {**original, "later/receipt": "another-proved-resource"}
        h.workflow.persist_metadata()
        assert h.db.get_vm("created").platform_metadata == h.access.platform_metadata
        assert h.db.operations.inspect(_SCOPE).ownership == ownership


@pytest.mark.parametrize("change", ["replace", "remove", "clear"])
def test_published_metadata_cannot_be_replaced_or_removed(harness, change):
    h = harness
    with h.operation():
        original = dict(h.db.get_vm("created").platform_metadata)
        if change == "replace":
            h.access.platform_metadata = {"opaque/receipt": "different-resource"}
        elif change == "remove":
            h.access.platform_metadata = {"different/key": "cannot-hide-prior-resource"}
        else:
            h.access.platform_metadata = {}
        try:
            with pytest.raises(StateError):
                h.workflow.persist_metadata()
            assert h.db.get_vm("created").platform_metadata == original
            assert h.db.operations.inspect(_SCOPE).ownership == h.workflow.owner.ownership
        finally:
            h.access.platform_metadata = original


def test_cleanup_publishes_newly_observed_addressability_before_retaining_control(harness, monkeypatch):
    h = harness
    settle = _CreationAccess.settle
    control = StateError("interrupted resource observation")
    interrupted = False

    def observe(access, deadline):
        nonlocal interrupted
        access.platform_metadata = {**access.platform_metadata, "later/receipt": "proved-during-cleanup"}
        if not interrupted:
            interrupted = True
            raise control
        return settle(access, deadline)

    monkeypatch.setattr(_CreationAccess, "settle", observe)
    with pytest.raises(StateError) as caught, h.operation():
        pass
    assert caught.value is control
    assert h.db.get_vm("created").platform_metadata == h.access.platform_metadata
    fact = control.__cause__
    assert type(fact) is VMOperationControlFact
    assert h.db.operations.inspect(_SCOPE).ownership == h.workflow.owner.ownership
    fact.retry_cleanup(Deadline.after(5))
    assert h.db.operations.inspect(_SCOPE) is None


def test_partial_provision_failure_persists_only_adapter_proved_opaque_metadata(harness, monkeypatch):
    h = harness
    provision = _CreationAccess.provision
    control = StateError("fixture provider failure")

    def partial(access, deadline):
        access.failure = control
        return provision(access, deadline)

    monkeypatch.setattr(_CreationAccess, "provision", partial)
    with pytest.raises(StateError) as caught, h.operation():
        pytest.fail("failed provisioning admitted body")
    assert caught.value is control
    vm = h.db.get_vm("created")
    assert vm.platform_metadata == {"opaque/receipt": "provider-owned:not-a-candidate-id"}
    assert vm.tailscale_host is None and vm.provisioning_status == ProvisioningStatus.FAILED.value
    assert h.access.rollback and not h.access.transferred and not h.routes
    assert h.db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("stage", ["bootstrap", "attest", "canonical-account"])
def test_preparation_faults_respect_the_bootstrap_transfer_boundary(harness, monkeypatch, stage):
    h = harness
    control = KeyboardInterrupt()

    def stop(*args, **kwargs):
        raise control

    if stage == "canonical-account":
        prepare: Callable[..., TargetIdentityPreparation] = h.prepare_identity

        def second_account(*args, **kwargs):
            if len(h.workflow.routes) == 2:
                raise control
            return prepare(*args, **kwargs)

        monkeypatch.setattr(_native_operation, "prepare_target_identity", second_account)
    else:
        monkeypatch.setattr(creation, f"{stage}_created_guest", stop)
    with pytest.raises(KeyboardInterrupt) as caught, h.operation():
        pytest.fail("failed preparation admitted body")
    assert caught.value is control and control.__cause__ is None
    assert h.access.transferred is (stage != "bootstrap")
    assert h.access.rollback is (stage == "bootstrap")
    assert len(h.workflow.routes) == (2 if stage == "canonical-account" else 1)
    vm = h.db.get_vm("created")
    assert vm.init_status == InitStatus.PENDING.value and vm.provisioning_status == ProvisioningStatus.FAILED.value
    assert vm.tailscale_host == (None if stage == "bootstrap" else _ADDRESS)
    assert h.db.operations.inspect(_SCOPE) is None


def test_metadata_failure_still_retires_real_local_custody_and_unused_borrow(harness, monkeypatch):
    h = harness
    update = h.db.update_vm_platform_metadata
    control = StateError("fixture checkpoint failure")
    retained: tuple[LocalProcessOwner, OperationBorrow] | None = None

    def fail_checkpoint(*args):
        nonlocal retained
        if retained is None:
            retained = (h.access.custody.begin_process(), h.access.owner.borrow())
        raise control

    monkeypatch.setattr(h.db, "update_vm_platform_metadata", fail_checkpoint)
    with pytest.raises(StateError) as caught, h.operation():
        pytest.fail("failed checkpoint admitted body")
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, VMOperationControlFact)
    assert retained is not None
    process, borrow = retained
    terminal = process.snapshot().terminal
    assert terminal is not None and terminal.cleaned and borrow.closed and h.access.custody.settled
    assert h.db.operations.inspect(_SCOPE) is not None
    assert not any(event == "settle" for event, _ in h.events)
    monkeypatch.setattr(h.db, "update_vm_platform_metadata", update)
    fact.retry_cleanup(Deadline.after(30))
    assert h.access.rollback and h.db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("body_control", [False, True])
def test_cleanup_fact_retains_both_routes_and_claim_until_real_debt_resolves(harness, body_control):
    h = harness
    primary = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt if body_control else StateError) as caught, h.operation():
        h.access.settled = False
        if body_control:
            raise primary
    if body_control:
        assert caught.value is primary
    fact = caught.value.__cause__
    assert isinstance(fact, VMOperationControlFact) and fact._workflow is h.workflow
    assert len(fact._workflow.routes) == 2 and h.db.operations.inspect(_SCOPE) is not None
    assert len(h.access.owner.list_pending_lifecycle_obligations()) == 1
    assert not h.access.rollback and h.access.transferred
    before = [event for event, _ in h.events if event not in {"settle", "trust-close"}]
    h.access.settled, h.access.resolve_debt = True, False
    fresh = Deadline.after(30)
    with pytest.raises(StateError):
        fact.retry_cleanup(fresh)
    assert h.db.operations.inspect(_SCOPE) is not None
    h.access.debt.resolve()
    fact.retry_cleanup(fresh)
    assert h.db.operations.inspect(_SCOPE) is None
    assert before == [event for event, _ in h.events if event not in {"settle", "trust-close"}]
    assert all(budget is fresh for _, budget in h.events[-4:])


@pytest.mark.parametrize("change", ["row", "takeover", "snapshot-takeover"])
def test_exact_row_and_owner_fences_prevent_metadata_writes(harness, monkeypatch, change):
    h = harness
    h.db.insert_vm(
        "created", "fixture", "created", admin_username="agentworks", instance_marker=h.request.instance_marker
    )
    owner = OperationOwner.acquire(h.db.operations, _SCOPE, "row-write")
    workflow = creation._CreationWorkflow(
        owner=owner,
        deadline=h.deadline,
        db=h.db,
        vm_name="created",
        site="fixture",
        marker=h.request.instance_marker,
        admin_username="agentworks",
        hostname="created",
    )
    workflow.creation = SimpleNamespace(platform_metadata={"opaque": "must-not-commit"})
    with closing(Database(h.db.path)) as second:
        successor = None
        if change == "row":
            with second.transaction():
                second._conn.execute("UPDATE vms SET instance_marker = ? WHERE name = ?", ("b" * 32, "created"))
        elif change == "takeover":
            successor = OperationOwner.recover(second.operations, owner.ownership, "b" * 32)
        else:
            read_claim = owner.list_pending_lifecycle_obligations

            def takeover_after_snapshot_and_claim_read():
                nonlocal successor
                result = read_claim()
                assert h.db._conn.in_transaction
                successor = OperationOwner.recover(second.operations, owner.ownership, "b" * 32)
                return result

            monkeypatch.setattr(owner, "list_pending_lifecycle_obligations", takeover_after_snapshot_and_claim_read)
        with pytest.raises(sqlite3.OperationalError if change == "snapshot-takeover" else StateError) as caught:
            workflow.persist_metadata()
        if change == "snapshot-takeover":
            assert isinstance(caught.value, sqlite3.OperationalError)
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        assert h.db.get_vm("created").platform_metadata == {} and not h.db._conn.in_transaction
        if successor is not None:
            claim = second.operations.inspect(_SCOPE)
            assert claim is not None and claim.ownership == successor.ownership != owner.ownership
            successor.seal_lifecycle_obligations()
            successor.record_effects_resolved()
            successor.close()
        else:
            owner.close()
