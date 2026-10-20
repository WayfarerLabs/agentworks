"""Core trust composition with actual SQLite and owned policy files.

Only native client discovery/delivery and explicit fault boundaries are injected.
All files belong to pytest's caller-selected fixture namespace and are retained.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CapturedOutput, CarrierIO, Deadline, Failure
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHSettings, SSHTrustFiles
from agentworks.execution.carriers.ssh import enrollment as ssh_enrollment
from agentworks.execution.carriers.ssh.connection import admit_connection
from agentworks.execution.carriers.ssh.enrollment import SSHEnrollmentCustody, SSHEnrollmentError
from agentworks.execution.carriers.ssh.trust import import_trust, refresh_trust, resolve_trust, trust_status
from agentworks.operations import OperationOwner
from agentworks.vms import _creation_trust as composition
from agentworks.vms import _ssh_native_binding as binding

_MARKER = "c" * 32
_HOST = "192.0.2.24"
_LEARNED = b"192.0.2.24 ssh-ed25519 fixture-learned-key\n"


@dataclass
class Fixture:
    db: Database
    owner: OperationOwner
    settings: SSHSettings
    deadline: Deadline
    base: str
    calls: list[list[str]] = field(default_factory=list)
    budgets: list[Deadline] = field(default_factory=list)
    ordinary_before_publication: list[tuple[bytes, ...]] = field(default_factory=list)
    controls: list[BaseException] = field(default_factory=list)

    @property
    def bundle(self) -> ManagedSSHTrust:
        return ManagedSSHTrust(self.settings.trust_store)

    @property
    def candidate(self) -> Path:
        digest = hashlib.sha256(("vm:" + _MARKER).encode()).hexdigest()
        return self.settings.trust_store / ("enrollment-" + digest) / "known-hosts"

    def consumer(self) -> composition.CreationSSHTrust:
        return composition.CreationSSHTrust(
            self.owner, self.db.get_vm("box"), _HOST, self.settings, deadline=self.deadline
        )

    def run(
        self, argv: list[str], *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> ProcessResult:
        claim = self.db.operations.inspect(self.owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
        rows = self.owner.list_pending_lifecycle_obligations()
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
        document = json.loads(rows[0].payload)
        assert document["resource_id"] == "vm:" + _MARKER
        assert document["base_generation"] == self.base
        assert document["phase"] == "enrollment"
        self.calls.append(argv)
        self.budgets.append(deadline)
        self.ordinary_before_publication.append(
            tuple(path.read_bytes() for path in resolve_trust(self.bundle).known_hosts)
        )
        if "StrictHostKeyChecking=accept-new" in argv:
            self.candidate.write_bytes(_LEARNED)
        if self.controls:
            raise self.controls.pop(0)
        nonce = re.search(r"agw-enroll-[a-f0-9]{32}", argv[-1])
        assert nonce is not None
        return ProcessResult(True, 0, 0, CapturedOutput((nonce.group() + "\n").encode(), True), CapturedOutput(), None)


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fixture]:
    root = tmp_path.resolve()
    first, second, revoked, identity = (root / name for name in ("policy-a", "policy-b", "revoked", "identity"))
    first.write_bytes(b"existing-first-policy\n")
    second.write_bytes(b"existing-second-policy\n")
    revoked.write_bytes(b"fixture-revocation-bytes\x00\xff")
    identity.write_bytes(b"fixture identity, never used by a process")
    bundle = import_trust(root / "managed", sources=SSHTrustFiles((first, second), revoked), authority="fixture")
    base = trust_status(bundle).generation
    assert base is not None
    with closing(Database(root / "state.db")) as db:
        db.insert_vm("box", "fixture", "template", admin_username="admin", instance_marker=_MARKER)
        owner = OperationOwner.acquire(
            db.operations, OperationScope(OperationResourceKind.VM, "box"), "fixture-creation"
        )
        result = Fixture(db, owner, SSHSettings(bundle.directory, identity), Deadline.after(30), base)
        monkeypatch.setattr(ssh_enrollment, "resolve_client_executable", lambda connection: "/fixture/ssh")

        def version(executable, *, deadline, custody):
            assert executable == "/fixture/ssh"
            assert isinstance(custody, LocalDeliveryCustody)
            result.budgets.append(deadline)
            return None

        monkeypatch.setattr(ssh_enrollment, "check_client_version", version)
        monkeypatch.setattr(ssh_enrollment, "run_process", result.run)
        yield result


def test_constructor_and_common_connection_builder_are_passive(fixture, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("passive construction accessed an effect boundary")

    with monkeypatch.context() as patch:
        patch.setattr(fixture.owner._repository, "inspect", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(Path, "is_file", forbidden)
        patch.setattr(composition, "trust_status", forbidden)
        consumer = fixture.consumer()
        connection = binding.vm_ssh_connection(fixture.db.get_vm("box"), _HOST, fixture.settings)
        assert connection.trust == fixture.bundle
        assert connection.host == _HOST and connection.user == "admin"
        assert isinstance(consumer._custody, SSHEnrollmentCustody)
        assert consumer._custody.delivery is consumer._delivery
    assert consumer.close(Deadline.after(3))


@pytest.mark.parametrize("endpoint", ["host.invalid", "2001:db8::1", "192.0.2.1:22", None])
def test_common_connection_builder_requires_literal_ipv4(fixture, endpoint):
    with pytest.raises(StateError):
        binding.vm_ssh_connection(fixture.db.get_vm("box"), endpoint, fixture.settings)


@pytest.mark.parametrize("change", ["missing-marker", "wrong-scope", "unbounded"])
def test_constructor_rejects_invalid_core_provenance(fixture, change):
    vm = fixture.db.get_vm("box")
    deadline = fixture.deadline
    if change == "missing-marker":
        vm = replace(vm, instance_marker=None)
    elif change == "wrong-scope":
        vm = replace(vm, name="other")
    else:
        deadline = Deadline.after(None)
    with pytest.raises(ValidationError):
        composition.CreationSSHTrust(fixture.owner, vm, _HOST, fixture.settings, deadline=deadline)


@pytest.mark.parametrize("state", ["closed", "stale", "sealed", "recovery", "borrowed", "resolved"])
def test_owner_refusal_precedes_policy_io(fixture, monkeypatch, state):
    consumer = fixture.consumer()
    if state == "closed":
        fixture.owner.stop_admission()
    elif state == "stale":
        fixture.db.operations.recover_takeover(fixture.owner.ownership, "d" * 32)
    elif state == "sealed":
        fixture.owner.seal_lifecycle_obligations()
    elif state == "recovery":
        fixture.owner = OperationOwner.recover(fixture.db.operations, fixture.owner.ownership, "d" * 32)
        consumer = fixture.consumer()
    elif state == "borrowed":
        fixture.owner.borrow()
    else:
        fixture.owner.seal_lifecycle_obligations()
        fixture.owner.record_effects_resolved()

    def forbidden(*args, **kwargs):
        pytest.fail("refused owner reached policy I/O")

    monkeypatch.setattr(composition, "trust_status", forbidden)
    monkeypatch.setattr(composition, "resolve_trust", forbidden)
    monkeypatch.setattr(composition, "enroll_new_target", forbidden)
    monkeypatch.setattr(composition, "refresh_trust", forbidden)
    with pytest.raises(StateError):
        consumer.prepare()
    assert not fixture.calls
    assert consumer.close(Deadline.after(3))


def test_complete_publication_closes_writer_then_strictly_readmits(fixture, monkeypatch):
    consumer = fixture.consumer()
    original_close = consumer._custody.close
    events = []

    def close(deadline):
        assert deadline is fixture.deadline
        result = original_close(deadline)
        if result:
            events.append("writer-closed")
        return result

    def publish(*args, **kwargs):
        assert events == ["writer-closed"]
        assert consumer._custody._closed and consumer._custody._settled
        assert kwargs["expected_generation"] == fixture.base
        assert kwargs["authority"] == "core-creation:vm:" + _MARKER
        events.append("publication")
        return refresh_trust(*args, **kwargs)

    monkeypatch.setattr(consumer._custody, "close", close)
    monkeypatch.setattr(composition, "refresh_trust", publish)
    consumer.prepare()
    assert events == ["writer-closed", "publication"]
    assert len(fixture.calls) == 2
    assert "StrictHostKeyChecking=accept-new" in fixture.calls[0]
    assert "StrictHostKeyChecking=yes" in fixture.calls[1]
    assert all(budget is fixture.deadline for budget in fixture.budgets)
    assert all(_LEARNED not in selected for selected in fixture.ordinary_before_publication)
    current = admit_connection(binding.vm_ssh_connection(fixture.db.get_vm("box"), _HOST, fixture.settings))
    assert tuple(path.read_bytes() for path in current.known_hosts) == (
        _LEARNED,
        b"existing-first-policy\n",
        b"existing-second-policy\n",
    )
    assert current.revoked_host_keys is not None
    assert current.revoked_host_keys.read_bytes() == b"fixture-revocation-bytes\x00\xff"
    assert fixture.candidate.read_bytes() == _LEARNED
    assert not fixture.owner.list_pending_lifecycle_obligations()
    row = fixture.owner.inspect_lifecycle_obligation(consumer._obligation_id)
    assert row is not None and row.state is LifecycleObligationState.RESOLVED
    assert json.loads(row.payload)["published_generation"] == trust_status(fixture.bundle).generation
    monkeypatch.setattr(consumer._custody, "close", original_close)
    assert consumer.close(Deadline.after(3)) and consumer.close(Deadline.after(3))
    assert not fixture.owner.list_pending_lifecycle_obligations()
    claim = fixture.db.operations.inspect(fixture.owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    assert claim.obligations_sealed_at is None
    with pytest.raises(StateError):
        consumer.prepare()
    assert len(fixture.calls) == 2


def test_successful_prepare_resolves_before_route_borrow_and_cleanup_is_passive(fixture, monkeypatch):
    consumer = fixture.consumer()
    consumer.prepare()
    assert not fixture.owner.list_pending_lifecycle_obligations()
    fixture.owner.borrow()

    def forbidden(*args, **kwargs):
        pytest.fail("settled trust cleanup reached an effect boundary while the route borrowed its owner")

    monkeypatch.setattr(consumer._custody, "close", forbidden)
    monkeypatch.setattr(consumer._delivery, "close", forbidden)
    monkeypatch.setattr(fixture.owner._repository, "inspect", forbidden)
    monkeypatch.setattr(fixture.owner._repository, "inspect_lifecycle_obligation", forbidden)
    monkeypatch.setattr(fixture.owner._repository, "resolve_lifecycle_obligation", forbidden)
    monkeypatch.setattr(composition, "enroll_new_target", forbidden)
    monkeypatch.setattr(composition, "refresh_trust", forbidden)
    assert consumer.close(Deadline.after(3))
    assert consumer.close(Deadline.after(3))
    assert len(fixture.calls) == 2


def test_execution_binding_consumes_common_passive_connection(fixture, monkeypatch):
    connection = binding.vm_ssh_connection(fixture.db.get_vm("box"), _HOST, fixture.settings)
    selections = []

    def select(vm, endpoint, settings):
        selections.append((vm.name, endpoint, settings))
        return connection

    monkeypatch.setattr(binding, "vm_ssh_connection", select)
    result = binding.ssh_execution_binding(fixture.db.get_vm("box"), _HOST, fixture.settings)
    assert result.delivery_account == "admin"
    assert selections == [("box", _HOST, fixture.settings)]
    assert not fixture.calls


@pytest.mark.parametrize(
    "control", [KeyboardInterrupt(), SystemExit(19), SSHEnrollmentError(failure=Failure.OBSERVATION)]
)
def test_failed_first_contact_keeps_evidence_and_closes_only_local_debt(fixture, control):
    consumer = fixture.consumer()
    fixture.controls.append(control)
    with pytest.raises(type(control)) as caught:
        consumer.prepare()
    assert caught.value is control
    assert consumer.close(Deadline.after(3))
    assert fixture.candidate.read_bytes() == _LEARNED
    assert trust_status(fixture.bundle).generation == fixture.base
    assert not fixture.owner.list_pending_lifecycle_obligations()
    with pytest.raises(StateError):
        consumer.prepare()
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("fail_at", [1, 2])
def test_writer_cleanup_failure_retains_custody_and_prevents_publication(fixture, monkeypatch, fail_at):
    consumer = fixture.consumer()
    real_sync = ssh_enrollment._sync_candidate
    syncs = []

    def fail(candidate):
        syncs.append(True)
        if len(syncs) >= fail_at:
            raise OSError("fixture flush failure")
        real_sync(candidate)

    monkeypatch.setattr(ssh_enrollment, "_sync_candidate", fail)
    with pytest.raises(SSHEnrollmentError):
        consumer.prepare()
    with pytest.raises(SSHEnrollmentError):
        consumer.close(Deadline.after(3))
    assert consumer._custody._lock is not None
    assert fixture.owner.list_pending_lifecycle_obligations()
    assert trust_status(fixture.bundle).generation == fixture.base
    monkeypatch.setattr(ssh_enrollment, "_sync_candidate", real_sync)
    assert consumer.close(Deadline.after(3))
    assert fixture.candidate.read_bytes() == _LEARNED
    assert len(fixture.calls) == fail_at


def test_unsettled_actual_delivery_blocks_resolution(fixture, monkeypatch):
    consumer = fixture.consumer()
    fixture.controls.append(SSHEnrollmentError(failure=Failure.OBSERVATION))
    with pytest.raises(SSHEnrollmentError):
        consumer.prepare()
    real_close = consumer._delivery.close
    monkeypatch.setattr(consumer._delivery, "close", lambda deadline: False)
    assert not consumer.close(Deadline.after(3))
    assert consumer._custody._lock is not None
    assert fixture.owner.list_pending_lifecycle_obligations()
    monkeypatch.setattr(consumer._delivery, "close", real_close)
    assert consumer.close(Deadline.after(3))


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_publication_outcome_retains_debt_without_replay(fixture, monkeypatch, committed):
    consumer = fixture.consumer()
    control = KeyboardInterrupt()
    calls = []

    def publish(*args, **kwargs):
        calls.append(True)
        if committed:
            refresh_trust(*args, **kwargs)
        raise control

    monkeypatch.setattr(composition, "refresh_trust", publish)
    with pytest.raises(KeyboardInterrupt) as caught:
        consumer.prepare()
    assert caught.value is control
    assert not consumer.close(Deadline.after(3))
    assert not consumer.close(Deadline.after(3))
    (row,) = fixture.owner.list_pending_lifecycle_obligations()
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert json.loads(row.payload)["phase"] == "publication-pending"
    assert consumer._custody._settled
    assert calls == [True] and len(fixture.calls) == 2
    assert (trust_status(fixture.bundle).generation != fixture.base) is committed
    assert fixture.candidate.read_bytes() == _LEARNED


@pytest.mark.parametrize("transition", ["register", "arm", "pending-payload", "complete-payload", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
def test_exact_ledger_fault_reconciliation_never_replays_native_work(fixture, monkeypatch, transition, committed):
    consumer = fixture.consumer()
    repository = fixture.owner._repository
    method = {
        "register": "register_lifecycle_obligation",
        "arm": "mark_lifecycle_obligation_possible_effect",
        "pending-payload": "publish_lifecycle_obligation_payload",
        "complete-payload": "publish_lifecycle_obligation_payload",
        "resolve": "resolve_lifecycle_obligation",
    }[transition]
    original = getattr(repository, method)
    control = KeyboardInterrupt()
    faults: list[bool] = []

    def fault(*args, **kwargs):
        selected = transition not in ("pending-payload", "complete-payload") or (
            json.loads(kwargs["payload"])["phase"]
            == ("published" if transition == "complete-payload" else "publication-pending")
        )
        if selected and not faults:
            faults.append(True)
            if committed:
                original(*args, **kwargs)
            raise control
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, method, fault)
    with pytest.raises(KeyboardInterrupt) as caught:
        consumer.prepare()
    assert caught.value is control and faults == [True]
    native_calls = len(fixture.calls)
    assert consumer.close(Deadline.after(3))
    assert consumer.close(Deadline.after(3))
    assert len(fixture.calls) == native_calls
    assert not fixture.owner.list_pending_lifecycle_obligations()
    assert fixture.db.operations.inspect(fixture.owner.ownership.scope) is not None


def test_policy_change_during_snapshot_admission_refuses_before_enrollment(fixture, monkeypatch):
    consumer = fixture.consumer()

    def resolve(bundle):
        snapshot = resolve_trust(bundle)
        refresh_trust(bundle, sources=snapshot, authority="fixture-update", expected_generation=fixture.base)
        return snapshot

    monkeypatch.setattr(composition, "resolve_trust", resolve)
    with pytest.raises(StateError):
        consumer.prepare()
    assert not fixture.calls and not fixture.owner.list_pending_lifecycle_obligations()
    assert consumer.close(Deadline.after(3))


def test_generation_change_before_publication_never_falls_back(fixture, monkeypatch):
    consumer = fixture.consumer()
    publications = []

    def publish(bundle, **kwargs):
        publications.append(True)
        refresh_trust(
            bundle, sources=resolve_trust(bundle), authority="fixture-update", expected_generation=fixture.base
        )
        return refresh_trust(bundle, **kwargs)

    monkeypatch.setattr(composition, "refresh_trust", publish)
    with pytest.raises(StateError):
        consumer.prepare()
    assert not consumer.close(Deadline.after(3))
    assert publications == [True] and len(fixture.calls) == 2
    assert fixture.owner.list_pending_lifecycle_obligations()


def test_candidate_generation_must_match_retained_snapshot(fixture, monkeypatch):
    consumer = fixture.consumer()
    original = ssh_enrollment.enroll_new_target

    def enroll(*args, **kwargs):
        return replace(original(*args, **kwargs), base_generation="d" * 32)

    monkeypatch.setattr(composition, "enroll_new_target", enroll)
    with pytest.raises(StateError):
        consumer.prepare()
    assert trust_status(fixture.bundle).generation == fixture.base
    assert consumer.close(Deadline.after(3))
    assert len(fixture.calls) == 2


def test_original_budget_expiry_after_enrollment_prevents_publication(fixture, monkeypatch):
    consumer = fixture.consumer()
    original = ssh_enrollment.enroll_new_target
    remaining = Deadline.remaining
    expired: list[bool] = []

    def budget(deadline):
        return 0.0 if expired and deadline is fixture.deadline else remaining(deadline)

    def enroll(*args, **kwargs):
        candidate = original(*args, **kwargs)
        expired.append(True)
        return candidate

    monkeypatch.setattr(Deadline, "remaining", budget)
    monkeypatch.setattr(composition, "enroll_new_target", enroll)
    with pytest.raises(TimeoutError):
        consumer.prepare()
    assert trust_status(fixture.bundle).generation == fixture.base
    assert consumer.close(Deadline.after(3))
    assert len(fixture.calls) == 2
