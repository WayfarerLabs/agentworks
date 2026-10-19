"""Ordinary host composition with real local helpers and exact retained claims.

The local Linux fixture proves no native macOS or installed SSH behavior.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from agentworks.capabilities.base import OperationScope as DescriptiveScope
from agentworks.capabilities.base import RunContext, ScopeLevel, TargetAbsence
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.db.operations import OperationRepository
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _platform_host_context as host
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityStatus,
    prepare_target_identity,
)
from agentworks.execution.access import FileAccess
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Create, NewMetadata
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionIdentity, ExecutionRoute
from agentworks.operations import OperationBorrow, OperationOwner
from agentworks.orchestration.secrets import ScopedSecrets
from tests.execution.files._file_publication_support import install_fixture_bundle as publication_bundle
from tests.execution.files._file_read_support import install_fixture_bundle as read_bundle
from tests.execution.files._file_snapshot_support import install_fixture_bundle as snapshot_bundle
from tests.execution.files._file_stage_support import install_fixture_bundle as stage_bundle
from tests.execution.test_inline_helper import LocalCarrier
from tests.execution.test_target_identity import SyntheticCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local ordinary helper proof requires Linux")
_SCOPE = OperationScope(OperationResourceKind.PLATFORM_HOST, "explicit-shared-host")
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
_IDENTITY = IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    import grp
    import pwd

    db = Database(tmp_path / "state.db")
    account = pwd.getpwuid(os.geteuid()).pw_name
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o1777)
    scratch.chmod(0o1777)
    stage_bundle(monkeypatch)
    publication_bundle(monkeypatch)
    read_bundle(monkeypatch)
    snapshot_bundle(monkeypatch, scratch)
    lifetimes = []
    prepare = host._prepare_context

    def capture(lifetime, *args, **kwargs):
        lifetimes.append(lifetime)
        return prepare(lifetime, *args, **kwargs)

    monkeypatch.setattr(host, "_prepare_context", capture)
    carrier = LocalCarrier()
    metadata = NewMetadata(account, grp.getgrgid(os.getegid()).gr_name, 0o600)

    def context(ctx=None, **kwargs):
        options = dict(
            delivery_account=account,
            workload_account=account,
            include_elevated=False,
            runtime_selection=_RUNTIME,
            trusted_root=PurePosixPath(root),
            deadline=Deadline.after(30),
        )
        options.update(kwargs)
        return host.platform_host_execution_context(
            db, RunContext() if ctx is None else ctx, _SCOPE, carrier, **options
        )

    try:
        yield db, carrier, root, scratch, metadata, lifetimes, context
    finally:
        for lifetime in lifetimes:
            assert lifetime.owner.close_local_delivery(Deadline.after(3))
        assert carrier.local_delivery.settled
        db.close()


@pytest.mark.parametrize("include_elevated", [False, True])
def test_actual_context_command_files_identity_and_closed_derived_views(fixture, include_elevated):
    db, carrier, root, scratch, metadata, lifetimes, context = fixture
    with context(include_elevated=include_elevated) as ctx:
        lifetime = lifetimes[0]
        assert lifetime.owner.ownership.scope == _SCOPE
        target = ctx.platform_host_execution_target()
        assert target.route is ExecutionRoute.PLATFORM_HOST
        assert target.identity == ExecutionIdentity(metadata.owner, _IDENTITY.euid, _IDENTITY.egid, _IDENTITY.groups)
        assert target.features == carrier.features
        execution, files = target.execution(), target.files()
        derived = execution.with_env({"HOST_FIXTURE": "selected"})
        before = carrier.calls
        assert target.execution() is execution and target.files() is files
        assert ctx.platform_host_execution_target_absence() is None
        assert carrier.calls == before
        code = (
            "import json,os; print(json.dumps([os.geteuid(),os.getegid(),sorted(set(os.getgroups())|{os.getegid()})]))"
        )
        result = execution.run(Command([sys.executable, "-I", "-S", "-B", "-c", code]), profile=Protection.DIRECT)
        assert result.ok and json.loads(result.stdout.data) == [_IDENTITY.euid, _IDENTITY.egid, list(_IDENTITY.groups)]
        result = derived.run(
            Script('read value; printf "%s:%s" "$HOST_FIXTURE" "$value"; printf error >&2', Shell.SH),
            stdin=Input.bytes(b"finite\n"),
            output=Output.capture(64),
            profile=Protection.DIRECT,
        )
        assert result.ok and result.stdout.data == b"selected:finite" and result.stderr.data == b"error"
        discarded = execution.run(
            Command(["/bin/echo", "discarded"]), output=Output.discard(), profile=Protection.DIRECT
        )
        assert discarded.ok
        if not include_elevated:
            before = carrier.calls
            with pytest.raises(StateError):
                execution.run(Command(["/bin/true"]), sudo=True, profile=Protection.DIRECT)
            assert carrier.calls == before
        else:
            assert lifetime.identity.elevated_plan is not None
        files.write_file(PurePosixPath(root / "data"), b"host-context", condition=Create(), create_metadata=metadata)
        read = files.read_file(PurePosixPath(root / "data"), max_bytes=64)
        assert read.data == b"host-context"
        before = carrier.calls
        with pytest.raises(ValidationError):
            files.read_file(PurePosixPath(root.parent / "outside"), max_bytes=64)
        with pytest.raises(ValidationError):
            files.write_file(
                PurePosixPath(root / "invalid-mode"),
                b"refused",
                condition=Create(),
                create_metadata=NewMetadata(metadata.owner, metadata.group, 0o4777),
            )
        assert carrier.calls == before and not root.joinpath("invalid-mode").exists()
        assert lifetime.files._target == _SCOPE and lifetime.files._bootstrap is None
        assert lifetime.execution._managed_target is None and lifetime.execution._bootstrap is None
        assert not lifetime.files.has_pending_custody and not lifetime.execution.has_pending_custody
        assert lifetime.owner.list_pending_lifecycle_obligations()
    assert db.operations.inspect(_SCOPE) is None and list(scratch.iterdir()) == []
    before = carrier.calls
    for access in (execution, derived):
        with pytest.raises(StateError):
            access.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    with pytest.raises(StateError):
        files.read_file(PurePosixPath(root / "data"), max_bytes=64)
    assert carrier.calls == before


@pytest.mark.parametrize("binding", [False, True])
def test_context_preserves_exact_descriptive_secret_legacy_and_guest_world(fixture, binding):
    from tests.test_run_context import _UntouchableTarget

    db, carrier, _, _, _, _, context = fixture
    sentinel = _UntouchableTarget()
    descriptive = DescriptiveScope(ScopeLevel.VM, vm="unrelated-description")
    values = {"allowed": "one", "other": "hidden"}
    secrets = ScopedSecrets(values, ("allowed",))
    incoming = RunContext(
        config=sentinel,
        operation_scope=descriptive,
        admin_target=sentinel,
        agent_target=sentinel,
        admin_execution_target=sentinel if binding else None,
        agent_execution_target=sentinel if binding else None,
        admin_execution_target_absence=TargetAbsence.WITHHELD,
        agent_execution_target_absence=TargetAbsence.WITHHELD,
        platform_host_execution_target=sentinel,
        secrets=secrets,
    )
    with context(incoming) as ctx:
        before = carrier.calls
        assert ctx.config is sentinel and ctx.operation_scope is descriptive
        assert ctx.admin_target() is sentinel and ctx.agent_target() is sentinel
        assert ctx.admin_execution_target() is (sentinel if binding else None)
        assert ctx.agent_execution_target() is (sentinel if binding else None)
        assert ctx.admin_execution_target_absence() is (None if binding else TargetAbsence.WITHHELD)
        assert ctx.agent_execution_target_absence() is (None if binding else TargetAbsence.WITHHELD)
        assert ctx._secrets is secrets and ctx.secret("allowed") == "one"
        values["allowed"] = "two"
        assert ctx.secret("allowed") == "two"
        with pytest.raises(StateError):
            ctx.secret("other")
        assert incoming.platform_host_execution_target() is sentinel
        assert carrier.calls == before and db.operations.inspect(_SCOPE) is not None
    assert db.operations.inspect(_SCOPE) is None


def test_claim_precedes_first_real_preparation_and_conflict_prevents_effects(fixture, monkeypatch):
    db, carrier, _, _, _, lifetimes, context = fixture
    execute = carrier.execute

    def checked(*args, **kwargs):
        claim = db.operations.inspect(_SCOPE)
        assert claim is not None and claim.ownership == lifetimes[0].owner.ownership
        return execute(*args, **kwargs)

    monkeypatch.setattr(carrier, "execute", checked)
    with context():
        before = carrier.calls
        with pytest.raises(StateError), context():
            pytest.fail("conflicting host context entered")
        assert carrier.calls == before and len(lifetimes) == 1
    assert db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize(
    "invalid",
    [
        {"deadline": Deadline.after(None)},
        {"deadline": Deadline.after(0)},
        {"delivery_account": "invalid\0account"},
        {"workload_account": ""},
        {"include_elevated": 1},
        {"runtime_selection": None},
        {"trusted_root": PurePosixPath("relative")},
    ],
)
def test_boundary_refuses_before_claim_or_carrier(fixture, invalid, monkeypatch):
    db, carrier, _, _, _, _, context = fixture

    def forbidden(*args, **kwargs):
        pytest.fail("invalid context reached claim acquisition")

    monkeypatch.setattr(OperationOwner, "acquire", forbidden)
    with pytest.raises(ValidationError), context(**invalid):
        pytest.fail("invalid host context entered")
    assert carrier.calls == 0 and db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize(
    "profile,lifetime",
    [
        (Protection.MANAGED, Lifetime.OPERATION),
        (Protection.MANAGED, Lifetime.INDEPENDENT),
        (Protection.DIRECT, Lifetime.INDEPENDENT),
    ],
)
def test_host_managed_and_independent_requests_refuse_without_effect(fixture, profile, lifetime):
    _, carrier, _, _, _, _, context = fixture
    with context() as ctx:
        execution = ctx.platform_host_execution_target().execution()
        before = carrier.calls
        with pytest.raises(StateError):
            execution.run(Command(["/bin/true"]), profile=profile, lifetime=lifetime)
        with pytest.raises(StateError):
            execution.observe(JobRef("a" * 32))
        assert carrier.calls == before


@pytest.mark.parametrize("target_os", [RuntimeTargetOS.LINUX, RuntimeTargetOS.DARWIN])
def test_explicit_accounts_runtime_and_elevation_are_selected_without_bootstrap(fixture, monkeypatch, target_os):
    db, _, root, _, _, lifetimes, _ = fixture
    carrier = SyntheticCarrier(
        identities={"delivery": IdentityExpectation(0, 0, (0,)), "worker": IdentityExpectation(1234, 1235, (1235,))}
    )
    runtime = RuntimeSelection(target_os, "/selected/python3")
    prepare = prepare_target_identity

    def checked(*args, **kwargs):
        assert db.operations.inspect(_SCOPE) is not None
        assert "bootstrap" not in kwargs
        assert kwargs["runtime_selection"] is runtime
        return prepare(*args, **kwargs)

    monkeypatch.setattr(host, "prepare_target_identity", checked)
    with host.platform_host_execution_context(
        db,
        RunContext(),
        _SCOPE,
        carrier,
        delivery_account="delivery",
        workload_account="worker",
        include_elevated=True,
        runtime_selection=runtime,
        trusted_root=PurePosixPath(root),
        deadline=Deadline.after(30),
    ) as ctx:
        target = ctx.platform_host_execution_target()
        assert target is not None
        assert target.identity == ExecutionIdentity("worker", 1234, 1235, (1235,))
        assert carrier.calls == ["delivery", "worker"]
        assert lifetimes[0].identity.elevated_plan.expected.euid == 0
    assert db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("phase", ["file", "execution", "context"])
def test_partial_construction_preserves_control_identity_and_cause(fixture, monkeypatch, phase):
    db, _, _, _, _, lifetimes, context = fixture
    control = KeyboardInterrupt()
    original_cause = ValueError("original")
    control.__cause__ = original_cause

    def interrupted(*args, **kwargs):
        raise control

    if phase == "context":
        original = FileAccess

        def assembly(*args, **kwargs):
            files = original(*args, **kwargs)
            monkeypatch.setattr(host, "RunContext", interrupted)
            return files

        monkeypatch.setattr(host, "FileAccess", assembly)
    else:
        monkeypatch.setattr(host, {"file": "FileOperation", "execution": "ExecutionOperation"}[phase], interrupted)
    with pytest.raises(KeyboardInterrupt) as caught, context():
        pytest.fail("partial construction yielded")
    assert caught.value is control and control.__cause__ is original_cause
    assert db.operations.inspect(_SCOPE) is None
    if phase == "execution":
        assert lifetimes[0].files is not None


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_release_retries_exact_finalization_without_preparation_or_body(fixture, monkeypatch, committed):
    db, carrier, _, _, _, _, context = fixture
    release = OperationRepository.release_resolved
    calls = []
    control = KeyboardInterrupt()

    def interrupted(repository, ownership):
        calls.append(ownership)
        if len(calls) == 1:
            if committed:
                release(repository, ownership)
            raise control
        release(repository, ownership)

    monkeypatch.setattr(OperationRepository, "release_resolved", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught, context() as ctx:
        execution = ctx.platform_host_execution_target().execution()
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, host.PlatformHostContextControlFact)
    assert (db.operations.inspect(_SCOPE) is None) is committed
    before = carrier.calls
    fact.retry_cleanup(Deadline.after(10))
    fact.retry_cleanup(Deadline.after(10))
    assert len(calls) == (1 if committed else 2) and len(set(calls)) == 1
    assert carrier.calls == before and db.operations.inspect(_SCOPE) is None
    with pytest.raises(StateError):
        execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)


@pytest.mark.parametrize("phase", ["delivery", "execution", "file", "ledger"])
def test_cleanup_control_retains_original_body_and_retries_only_exact_cleanup(fixture, monkeypatch, phase):
    db, carrier, root, _, _, lifetimes, context = fixture
    primary = KeyboardInterrupt()
    cause = ValueError("body cause")
    primary.__cause__ = cause
    interrupted = RuntimeError("cleanup observation interrupted")
    with pytest.raises(KeyboardInterrupt) as caught, context() as ctx:
        lifetime = lifetimes[0]
        owner = lifetime.owner
        execution = ctx.platform_host_execution_target().execution().with_env({"KEY": "value"})
        methods = {
            "delivery": (OperationOwner, "close_local_delivery"),
            "execution": (ExecutionOperation, "finish"),
            "file": (FileOperation, "settle_local_file_bookkeeping"),
            "ledger": (OperationOwner, "list_pending_lifecycle_obligations"),
        }
        cls, method = methods[phase]
        original = getattr(cls, method)

        def fail(*args, **kwargs):
            raise interrupted

        monkeypatch.setattr(cls, method, fail)
        raise primary
    assert caught.value is primary
    fact = primary.__cause__
    assert isinstance(fact, host.PlatformHostContextControlFact) and fact.__cause__ is cause
    assert fact._workflow is lifetime and fact._workflow.owner is owner
    assert db.operations.inspect(_SCOPE).ownership == owner.ownership
    before = carrier.calls
    with pytest.raises(StateError):
        execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    with pytest.raises(StateError):
        ctx.platform_host_execution_target().files().read_file(PurePosixPath(root / "data"), max_bytes=64)
    for deadline in (Deadline.after(None), Deadline.after(0)):
        with pytest.raises(ValidationError):
            fact.retry_cleanup(deadline)
    monkeypatch.setattr(cls, method, original)
    fact.retry_cleanup(Deadline.after(10))
    assert carrier.calls == before and db.operations.inspect(_SCOPE) is None


def test_uncertain_identity_retains_preparation_and_original_control(fixture, monkeypatch):
    db, carrier, _, _, _, lifetimes, context = fixture
    control = KeyboardInterrupt()
    cause = ValueError("preparation cause")
    control.__cause__ = cause

    def unknown(invocation, *, io, deadline, custody):
        raise control

    monkeypatch.setattr(carrier, "execute", unknown)
    with pytest.raises(KeyboardInterrupt) as caught, context():
        pytest.fail("uncertain preparation yielded")
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, host.PlatformHostContextControlFact)
    assert isinstance(fact.__cause__, TargetIdentityControlFact)
    assert fact.__cause__.__cause__ is cause
    identity = lifetimes[0].identity
    assert identity.status is TargetIdentityStatus.UNCERTAIN and identity.requires_owner_retention
    assert db.operations.inspect(_SCOPE) is not None
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert lifetimes[0].identity is identity


@pytest.mark.parametrize("component", ["identity", "ledger"])
def test_unsettled_component_prevents_release_without_replay(fixture, component):
    db, carrier, _, _, _, lifetimes, context = fixture
    with pytest.raises(StateError) as caught, context():
        lifetime = lifetimes[0]
        if component == "identity":
            lifetime.identity = replace(lifetime.identity, pending_remote_effects=True)
        else:
            lifetime.owner.register_lifecycle_obligation("fixture", payload_version=1, payload=b"")
    fact = caught.value.__cause__
    assert isinstance(fact, host.PlatformHostContextControlFact)
    assert db.operations.inspect(_SCOPE) is not None
    before = carrier.calls
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert carrier.calls == before and fact._workflow is lifetime


@pytest.mark.parametrize("component", ["file", "execution"])
def test_actual_lost_helper_completion_retains_concrete_custody(fixture, monkeypatch, component):
    from agentworks.errors import AgentworksError

    db, carrier, root, _, _, lifetimes, context = fixture
    primary = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught, context() as ctx:
        execute = carrier.execute

        def lost_completion(*args, **kwargs):
            return replace(execute(*args, **kwargs), completion=None)

        monkeypatch.setattr(carrier, "execute", lost_completion)
        target = ctx.platform_host_execution_target()
        if component == "file":
            with pytest.raises(AgentworksError):
                target.files().stat(PurePosixPath(root / "absent"))
            assert lifetimes[0].files.has_pending_custody
        else:
            target.execution().run(Command(["/bin/true"]), profile=Protection.DIRECT)
            assert lifetimes[0].execution.has_pending_custody
        raise primary
    assert caught.value is primary
    fact = primary.__cause__
    assert isinstance(fact, host.PlatformHostContextControlFact)
    assert db.operations.inspect(_SCOPE) is not None
    before = carrier.calls
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert carrier.calls == before


def test_retained_actual_local_file_original_closes_on_exact_retry(fixture):
    db, carrier, root, _, _, lifetimes, context = fixture
    source = root / "original"
    source.write_bytes(b"retained original")

    class Original:
        def __init__(self) -> None:
            self.file = source.open("rb")
            self.interrupted = False
            self.deadlines: list[Deadline] = []

        @property
        def settled(self):
            return self.file.closed

        def close(self, deadline):
            assert deadline.expires_at is not None
            self.deadlines.append(deadline)
            if not self.interrupted:
                self.interrupted = True
                raise RuntimeError("local original close interrupted")
            self.file.close()

    original = Original()
    try:
        with pytest.raises(RuntimeError) as caught, context():
            operation = lifetimes[0].files
            call = operation.begin_local_file()
            call.resource = original
            call.reduction_complete = True
        fact = caught.value.__cause__
        assert isinstance(fact, host.PlatformHostContextControlFact)
        assert not original.settled and operation.has_pending_custody
        assert db.operations.inspect(_SCOPE) is not None
        before = carrier.calls
        object.__setattr__(lifetimes[0].deadline, "expires_at", 0.0)
        fresh = Deadline.after(10)
        fact.retry_cleanup(fresh)
        assert original.deadlines[0] is lifetimes[0].deadline
        assert all(deadline is fresh for deadline in original.deadlines[1:])
        assert original.settled and not operation.has_pending_custody
        assert db.operations.inspect(_SCOPE) is None and carrier.calls == before
    finally:
        original.file.close()


def test_actual_account_helper_delivery_retains_exact_local_original(fixture, monkeypatch):
    db, carrier, root, _, _, lifetimes, context = fixture
    source = root / "delivery-original"
    source.write_bytes(b"held delivery")
    original = source.open("rb")
    execute = carrier.execute
    held = []

    class Cleanup:
        @property
        def settled(self):
            return original.closed

        def close(self, deadline):
            assert deadline.expires_at is not None

    cleanup = Cleanup()

    def retaining(*args, **kwargs):
        result = execute(*args, **kwargs)
        custody = kwargs["custody"]
        assert custody._owner is not None
        custody.retain_cleanup(custody._owner, cleanup)
        held.append(custody)
        return result

    monkeypatch.setattr(carrier, "execute", retaining)
    try:
        with pytest.raises(StateError) as caught, context():
            pytest.fail("unsettled delivery yielded a host context")
        fact = caught.value.__cause__
        assert isinstance(fact, host.PlatformHostContextControlFact)
        assert lifetimes[0].identity.requires_owner_retention
        assert len(held) == 1 and not held[0].settled
        assert held[0]._cleanup is cleanup and db.operations.inspect(_SCOPE) is not None
        before = carrier.calls
        with pytest.raises(StateError):
            fact.retry_cleanup(Deadline.after(10))
        assert carrier.calls == before and not original.closed
        original.close()
        with pytest.raises(StateError):
            fact.retry_cleanup(Deadline.after(10))
        assert held[0].settled and db.operations.inspect(_SCOPE) is not None
    finally:
        original.close()


@pytest.mark.parametrize("run_command", [False, True])
def test_completed_upload_bookkeeping_retry_keeps_original_call_without_replay(fixture, monkeypatch, run_command):
    db, carrier, root, _, metadata, lifetimes, context = fixture
    control = KeyboardInterrupt()
    close = OperationBorrow.close

    def interrupted(borrow):
        raise control

    with pytest.raises(KeyboardInterrupt) as caught, context() as ctx:
        execution = ctx.platform_host_execution_target().execution()
        if run_command:
            result = execution.run(Command(["/bin/sh", "-c", "printf completed-command"]), profile=Protection.DIRECT)
            assert result.ok and result.stdout.data == b"completed-command"
            assert lifetimes[0].execution._dispatch_obligation is not None
        monkeypatch.setattr(OperationBorrow, "close", interrupted)
        ctx.platform_host_execution_target().files().write_file(
            PurePosixPath(root / "published"), b"published once", condition=Create(), create_metadata=metadata
        )
    assert caught.value is control and root.joinpath("published").read_bytes() == b"published once"
    fact = control.__cause__
    assert isinstance(fact, host.PlatformHostContextControlFact)
    operation = lifetimes[0].files
    retained = operation.active_uploads
    assert len(retained) == 1 and retained[0].outcome.publication_confirmed
    assert operation.has_pending_custody and db.operations.inspect(_SCOPE) is not None
    before = carrier.calls
    monkeypatch.setattr(OperationBorrow, "close", close)
    fact.retry_cleanup(Deadline.after(10))
    assert not operation.has_pending_custody and db.operations.inspect(_SCOPE) is None
    assert carrier.calls == before and root.joinpath("published").read_bytes() == b"published once"
    if run_command:
        assert result.ok and result.stdout.data == b"completed-command"
