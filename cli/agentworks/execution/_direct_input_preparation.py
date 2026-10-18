"""Passive finite DIRECT delivery selection before any stage can be created."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._file_stage_bundle import ROOT_SCRATCH_PROGRAM
from ._file_stage_protocol import (
    MAX_STAGE_CHUNK_BYTES,
    FileStageBeginRequest,
    FileStageChunkRequest,
    encode_file_stage_request,
)
from ._helper_bundle import FixedFileHelperBundle
from ._inline import PreparedInlineCandidate, prepare_inline_candidate
from ._inline_bundle import PREPARED_PROGRAM
from ._runtime_prerequisite import _root_guest_bootstrap_source, build_root_guest_bootstrap_argv
from ._scratch import ScratchReference
from ._scratch_receipt import ScratchOperation, ScratchOwnership, ScratchReceiptContext, _Identity
from ._staged_helper_bundle import build_staged_helper_program
from ._staged_helper_protocol import MAX_REMAINING_NS, StagedHelperRequest, encode_request
from .carrier import FiniteInput, PreparedInvocation

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._helper_launcher import IdentityPlan
    from ._runtime_prerequisite import RuntimeSelection, _NumericGuestBootstrap
    from .carrier import Carrier, Deadline
    from .models import Command, Script


@dataclass(slots=True, repr=False)
class DirectInputPreparation:
    """One frozen request and fixed activation, without scratch authority."""

    candidate: PreparedInlineCandidate
    data: bytes
    activation_prefix: bytes
    digest: bytes

    def activate(self, request: StagedHelperRequest) -> None:
        """Replace only the candidate's input with its exact retained stage control."""
        if request.nonce != self.candidate.nonce or request.sha256 != self.digest:
            raise ValidationError("Prepared DIRECT activation differs from its frozen request")
        self.candidate.io = replace(
            self.candidate.io, input=FiniteInput(self.activation_prefix + encode_request(request), sensitive=True)
        )

    def clear(self) -> None:
        """Release caller bytes independently from retained cleanup custody."""
        self.data = self.activation_prefix = self.digest = b""


def _probe_reference(bootstrap: _NumericGuestBootstrap, length: int) -> ScratchReference:
    maximum = _Identity(2**64 - 1, 2**64 - 1)
    return ScratchReference(
        ScratchOwnership(
            bytes(16),
            ScratchReceiptContext(ScratchOperation.STAGE, bootstrap.root_entry.expected),
            maximum,
            maximum,
            maximum,
            2**64 - 1,
            length,
            maximum,
        )
    )


def _validate_stage_carriers(
    carrier: Carrier,
    candidate: PreparedInlineCandidate,
    data_length: int,
    bootstrap: _NumericGuestBootstrap,
    runtime: RuntimeSelection,
) -> None:
    """Validate maximum-width serial helper envelopes at the carrier boundary.

    Chunk data is base64, so its contents cannot change JSON quoting overhead.
    Reference numbers, offsets and remaining time use their maximum encoded
    widths here. These local shapes convey no live scratch ownership.
    """
    nonce, identity = candidate.nonce, bootstrap.root_entry.expected
    reference = _probe_reference(bootstrap, data_length)
    argv, _, _ = build_root_guest_bootstrap_argv(
        bootstrap.root_entry,
        identity,
        selection=runtime,
        program=ROOT_SCRATCH_PROGRAM,
        nonce=nonce,
        expected_guest=bootstrap.guest,
    )
    remaining = 1.7976931348623157e308
    chunk = bytes(MAX_STAGE_CHUNK_BYTES)
    requests = (
        FileStageBeginRequest(nonce, "/tmp", "prepared-input", bytes(16), data_length, identity, remaining),
        FileStageChunkRequest(
            nonce,
            "/tmp",
            "prepared-input",
            bytes(16),
            reference,
            2**63 - 1,
            chunk,
            hashlib.sha256(chunk).digest(),
            identity,
            remaining,
        ),
    )
    for request in requests:
        carrier.validate(
            PreparedInvocation(argv),
            io=replace(
                candidate.io,
                input=FiniteInput(ROOT_SCRATCH_PROGRAM.prefix + encode_file_stage_request(request), sensitive=True),
                sensitive=True,
            ),
        )


def prepare_direct_input(
    carrier: Carrier,
    request: Command | Script,
    *,
    plan: IdentityPlan,
    bootstrap: _NumericGuestBootstrap,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    stdin: bytes,
    env: Mapping[str, str] | None,
    cwd: str | None,
    capture_limit: int | None,
    sensitive: bool,
    capture_tokens: tuple[bytes, bytes] | None = None,
) -> DirectInputPreparation:
    """Validate the complete staged activation and chunk carrier before effects."""
    remaining = deadline.remaining()
    if remaining is None or not 0 < remaining <= MAX_REMAINING_NS / 1_000_000_000:
        raise ValidationError("Prepared DIRECT input requires a finite live execution budget")
    candidate = prepare_inline_candidate(
        request,
        plan=plan,
        bootstrap=bootstrap,
        runtime_selection=runtime_selection,
        stdin=stdin,
        env=env,
        cwd=cwd,
        capture_limit=capture_limit,
        sensitive=sensitive,
        _prepared_input=True,
        _capture_tokens=capture_tokens,
    )
    try:
        assert isinstance(candidate.io.input, FiniteInput)
        data = candidate.io.input.data
        input_program = PREPARED_PROGRAM
        if capture_tokens is not None:
            from ._capture_bundle import CAPTURE_PROGRAM

            input_program = CAPTURE_PROGRAM
        fixed = FixedFileHelperBundle(
            _root_guest_bootstrap_source(plan.expected, input_program, bootstrap.guest),
            input_program.prefix,
        )
        program = build_staged_helper_program(fixed)
        argv, _, _ = build_root_guest_bootstrap_argv(
            bootstrap.root_entry,
            bootstrap.root_entry.expected,
            selection=runtime_selection,
            program=program,
            nonce=candidate.nonce,
            expected_guest=bootstrap.guest,
        )
        candidate.invocation = PreparedInvocation(argv)
        digest = hashlib.sha256(data).digest()
        probe = StagedHelperRequest(
            candidate.nonce,
            bootstrap.root_entry.expected,
            bytes(16),
            _probe_reference(bootstrap, len(data)),
            digest,
            MAX_REMAINING_NS,
        )
        candidate.io = replace(candidate.io, input=FiniteInput(program.prefix + encode_request(probe), sensitive=True))
        carrier.validate(candidate.invocation, io=candidate.io)
        _validate_stage_carriers(carrier, candidate, len(data), bootstrap, runtime_selection)
        return DirectInputPreparation(candidate, data, program.prefix, digest)
    except BaseException:
        candidate.release()
        raise
