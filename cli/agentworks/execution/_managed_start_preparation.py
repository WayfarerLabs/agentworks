"""Passive managed body preparation, followed by one actual late lease tail."""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError

from ._managed_lease_wire import MAX_LEASE_BYTES, LeaseError, OperationLease, checked_lease, sampled_lease
from ._managed_runs import ManagedRunLifetime
from ._managed_start_bundle import FIXED_BUNDLE
from ._managed_start_exchange import _DiagnosticSink, _finalize_managed_start, _validate_managed_start_context
from ._managed_start_protocol import ManagedStartError, ManagedStartRequest, encode_body, encode_tail
from ._runtime_prerequisite import build_root_guest_bootstrap_argv, build_runtime_identity_helper_argv
from ._scratch import ScratchReference
from ._scratch_receipt import ScratchOperation, ScratchOwnership, ScratchReceiptContext, _Identity
from ._staged_helper_bundle import build_staged_helper_program
from ._staged_helper_protocol import MAX_REMAINING_NS, StagedHelperRequest
from ._staged_helper_protocol import encode_request as encode_staged_request
from .carrier import CarrierIO, FiniteInput, PreparedInvocation, SinkOutput

if TYPE_CHECKING:
    from _hashlib import HASH

    from ._helper_identity import IdentityExpectation
    from ._helper_launcher import IdentityPlan
    from ._managed_job_request import ManagedJobRequest
    from ._managed_runs import ManagedOutputPolicy, ManagedRunIdentity, ManagedRunSpec
    from ._managed_start_exchange import _PreparedAttempt
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier, Deadline


def _probe_io(data: bytes) -> CarrierIO:
    return CarrierIO(
        input=FiniteInput(data, sensitive=True),
        output=SinkOutput(_DiagnosticSink(), _DiagnosticSink(), require_live=False),
        sensitive=True,
    )


def _probe_control(nonce: str, identity: IdentityExpectation, length: int) -> StagedHelperRequest:
    """Maximum-width syntax for size evidence, never live ownership evidence."""
    maximum = _Identity(2**64 - 1, 2**64 - 1)
    token = bytes(16)
    reference = ScratchReference(
        ScratchOwnership(
            token,
            ScratchReceiptContext(ScratchOperation.STAGE, identity),
            maximum,
            maximum,
            maximum,
            2**64 - 1,
            length,
            maximum,
        )
    )
    return StagedHelperRequest(nonce, identity, token, reference, bytes(32), MAX_REMAINING_NS)


class ManagedStartPreparation:
    """Non-dispatchable frozen body; only finish creates a claimable attempt.

    The core parent retains and clears this object. Structural probes are local
    temporaries and never become attempts, stage references or durable records.
    """

    def __init__(
        self,
        carrier: Carrier,
        identity: ManagedRunIdentity,
        spec: ManagedRunSpec,
        policy: ManagedOutputPolicy,
        deadline: Deadline,
        expected_launch: bytes,
        root: IdentityExpectation,
        nonce: str,
        body: bytes,
        *,
        staged: bool,
        invocation: PreparedInvocation,
        candidates: tuple[str, ...],
        shim: str | None,
        delivery_prefix: bytes,
    ) -> None:
        self.nonce, self.staged = nonce, staged
        self.parts: tuple[bytes, ...] = (FIXED_BUNDLE.prefix, body)
        self.expected_length = sum(map(len, self.parts)) + MAX_LEASE_BYTES
        self._carrier: Carrier | None = carrier
        self._identity: ManagedRunIdentity | None = identity
        self._spec: ManagedRunSpec | None = spec
        self._policy: ManagedOutputPolicy | None = policy
        self._deadline: Deadline | None = deadline
        self._expected_launch = expected_launch
        self._root: IdentityExpectation | None = root
        self._operation = spec.lifetime is ManagedRunLifetime.OPERATION
        self._invocation: PreparedInvocation | None = invocation
        self._candidates, self._shim = candidates, shim
        self._delivery_prefix = delivery_prefix
        self._hash: HASH | None = hashlib.sha256()
        for part in self.parts:
            self._hash.update(part)
        self._tail: bytes | None = None
        self.digest: bytes | None = None
        self._tail_started = False
        self._finished = False
        self._cleared = False

    def _require_live(self) -> None:
        if self._cleared or self._deadline is None or self._deadline.expired:
            raise StateError("Managed body preparation is cleared or expired")

    def freeze_tail(self, lease: OperationLease | None) -> bytes:
        """Freeze actual authority once, without re-encoding or rescanning body."""
        self._require_live()
        if self._tail_started:
            raise StateError("Managed body tail freezing is one-shot")
        self._tail_started = True
        if self._operation:
            if type(lease) is not OperationLease:
                raise ValidationError("Operation body requires its actual lease")
            try:
                checked_lease(lease, self._expected_launch)
            except LeaseError:
                raise ValidationError("Operation lease differs from its prepared body") from None
        elif lease is not None:
            raise ValidationError("Independent body requires a null lease tail")
        tail = encode_tail(lease)
        assert self._hash is not None
        self._hash.update(tail)
        self.digest = self._hash.digest()
        self._hash = None
        self._tail = tail
        return tail

    def finish(self, control: StagedHelperRequest | None = None) -> _PreparedAttempt:
        """Finalize actual inline or staged delivery under the original binding."""
        self._require_live()
        if self._finished or self._tail is None:
            raise StateError("Managed body finish requires one successfully frozen tail")
        self._finished = True
        if self.staged:
            if (
                type(control) is not StagedHelperRequest
                or control.nonce != self.nonce
                or control.identity != self._root
                or control.reference._ownership._length != self.expected_length
                or control.sha256 != self.digest
            ):
                raise ValidationError("Staged activation differs from its frozen managed body")
            data = self._delivery_prefix + encode_staged_request(control)
        else:
            if control is not None:
                raise ValidationError("Inline managed body cannot use staged activation")
            data = b"".join(self.parts) + self._tail
        assert self._carrier is not None and self._identity is not None and self._spec is not None
        assert self._policy is not None and self._deadline is not None and self._invocation is not None
        return _finalize_managed_start(
            self._carrier,
            self._identity,
            self._spec,
            self._policy,
            self._deadline,
            self._expected_launch,
            nonce=self.nonce,
            invocation=self._invocation,
            candidates=self._candidates,
            shim=self._shim,
            input_data=data,
        )

    def clear(self) -> None:
        """Release caller bytes, hash state and delivery references permanently."""
        self._cleared = True
        self.parts = ()
        self._hash = None
        self._tail = self.digest = None
        self._carrier = self._identity = self._spec = self._policy = self._deadline = None
        self._root = self._invocation = None
        self._expected_launch = self._delivery_prefix = b""
        self._candidates, self._shim = (), None


def prepare_managed_start_body(
    carrier: Carrier,
    identity: ManagedRunIdentity,
    spec: ManagedRunSpec,
    output_policy: ManagedOutputPolicy,
    request: ManagedJobRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    guest: VMGuestIdentity,
) -> ManagedStartPreparation:
    """Validate immutable body and both chosen delivery limits before effects."""
    expected_launch = _validate_managed_start_context(
        carrier, identity, spec, output_policy, request, plan, deadline, runtime_selection, guest
    )
    if request.operation_lease is not None:
        raise ValidationError("Managed body preparation cannot carry a lease")
    nonce = secrets.token_hex(16)
    try:
        body = encode_body(ManagedStartRequest(nonce, plan.expected, request, guest))
    except ManagedStartError:
        raise ValidationError("Invalid managed start body") from None
    probe_lease = sampled_lease(expected_launch, 0) if spec.lifetime is ManagedRunLifetime.OPERATION else None
    probe_data = FIXED_BUNDLE.prefix + body + encode_tail(probe_lease)
    argv, candidates, shim = build_runtime_identity_helper_argv(
        plan, selection=runtime_selection, fixed_source=FIXED_BUNDLE.bootstrap, nonce=nonce
    )
    invocation = PreparedInvocation(argv)
    staged, delivery_prefix = False, b""
    try:
        carrier.validate(invocation, io=_probe_io(probe_data))
    except ValidationError:
        program = build_staged_helper_program(FIXED_BUNDLE)
        argv, candidates, shim = build_root_guest_bootstrap_argv(
            plan, plan.expected, selection=runtime_selection, program=program, nonce=nonce, expected_guest=guest
        )
        invocation = PreparedInvocation(argv)
        expected_length = len(FIXED_BUNDLE.prefix) + len(body) + MAX_LEASE_BYTES
        probe = encode_staged_request(_probe_control(nonce, plan.expected, expected_length))
        carrier.validate(invocation, io=_probe_io(program.prefix + probe))
        staged, delivery_prefix = True, program.prefix
    return ManagedStartPreparation(
        carrier,
        identity,
        spec,
        output_policy,
        deadline,
        expected_launch,
        plan.expected,
        nonce,
        body,
        staged=staged,
        invocation=invocation,
        candidates=candidates,
        shim=shim,
        delivery_prefix=delivery_prefix,
    )
