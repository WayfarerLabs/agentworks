"""Private account-database resolution over one carrier attempt."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._account_bundle import FIXED_SOURCE
from agentworks.execution._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    AccountRequest,
    AccountRequestError,
    AccountResponseError,
    FileOwnership,
    FileOwnershipFailure,
    FileOwnershipRequest,
    FileOwnershipRequestError,
    FileOwnershipResponseError,
    decode_account_response,
    decode_file_ownership_response,
    encode_account_request,
    encode_file_ownership_request,
)
from agentworks.execution._helper_launcher import build_clean_helper_argv
from agentworks.execution.carrier import (
    CarrierIO,
    Dispatch,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from agentworks.execution._helper_identity import IdentityExpectation
    from agentworks.execution.carrier import Carrier, CarrierReport, Deadline, ExitStatus


class AccountObservationState(StrEnum):
    RESOLVED = "resolved"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class AccountObservationError(StrEnum):
    STREAMS = "streams"
    STDERR = "stderr"
    OVERSIZED = "oversized"
    MISSING = "missing"
    RESPONSE = "response"


@dataclass(frozen=True, slots=True)
class AccountObservation:
    state: AccountObservationState
    identity: IdentityExpectation | None = None
    failure: AccountFailure | None = None
    error: AccountObservationError | None = None


@dataclass(frozen=True, slots=True)
class AccountResolutionResult:
    """Carrier facts and the separate typed account observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: AccountObservation


FileOwnershipObservationState = AccountObservationState
FileOwnershipObservationError = AccountObservationError


@dataclass(frozen=True, slots=True)
class FileOwnershipObservation:
    state: AccountObservationState
    ownership: FileOwnership | None = None
    failure: FileOwnershipFailure | None = None
    error: AccountObservationError | None = None


@dataclass(frozen=True, slots=True)
class FileOwnershipResolutionResult:
    """Carrier facts and the separate typed file-ownership observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: FileOwnershipObservation


class _BoundedResponseSink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.oversized = False

    def try_write(self, data: memoryview) -> int:
        if data and not self.oversized:
            if len(data) > MAX_ACCOUNT_MESSAGE_BYTES - len(self.data):
                self.data.clear()
                self.oversized = True
            else:
                self.data.extend(data)
        return len(data)

    def clear(self) -> None:
        self.data.clear()
        self.oversized = False


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)

    def clear(self) -> None:
        self.saw_data = False


def _observe(
    response: _BoundedResponseSink,
    stderr: _DiagnosticSink,
    report: CarrierReport,
    nonce: str,
) -> AccountObservation:
    payload = _completed_response(response, stderr, report)
    if isinstance(payload, AccountObservationError):
        return AccountObservation(_error_state(payload), error=payload)
    try:
        decoded = decode_account_response(payload, nonce)
    except AccountResponseError:
        return AccountObservation(AccountObservationState.INVALID, error=AccountObservationError.RESPONSE)
    if decoded.identity is not None:
        return AccountObservation(AccountObservationState.RESOLVED, identity=decoded.identity)
    return AccountObservation(AccountObservationState.REFUSED, failure=decoded.failure)


def _completed_response(
    response: _BoundedResponseSink,
    stderr: _DiagnosticSink,
    report: CarrierReport,
) -> bytes | AccountObservationError:
    streams_complete = (
        report.stdout.retention is Retention.DELIVERED
        and report.stderr.retention is Retention.DELIVERED
        and report.stdout.complete
        and report.stderr.complete
    )
    if not streams_complete:
        return AccountObservationError.STREAMS
    if response.oversized:
        return AccountObservationError.OVERSIZED
    if stderr.saw_data:
        return AccountObservationError.STDERR
    if not response.data:
        return AccountObservationError.MISSING
    return bytes(response.data)


def _error_state(error: AccountObservationError) -> AccountObservationState:
    if error in (AccountObservationError.STREAMS, AccountObservationError.MISSING):
        return AccountObservationState.INCOMPLETE
    return AccountObservationState.INVALID


def _observe_file_ownership(
    response: _BoundedResponseSink,
    stderr: _DiagnosticSink,
    report: CarrierReport,
    nonce: str,
) -> FileOwnershipObservation:
    payload = _completed_response(response, stderr, report)
    if isinstance(payload, AccountObservationError):
        return FileOwnershipObservation(_error_state(payload), error=payload)
    try:
        decoded = decode_file_ownership_response(payload, nonce)
    except FileOwnershipResponseError:
        return FileOwnershipObservation(
            AccountObservationState.INVALID,
            error=AccountObservationError.RESPONSE,
        )
    if decoded.ownership is not None:
        return FileOwnershipObservation(
            AccountObservationState.RESOLVED,
            ownership=decoded.ownership,
        )
    return FileOwnershipObservation(AccountObservationState.REFUSED, failure=decoded.failure)


def resolve_account(
    carrier: Carrier,
    trusted_account: str,
    deadline: Deadline,
    runtime_path: str,
) -> AccountResolutionResult:
    """Resolve one core-bound account without replay or identity transition."""
    nonce = secrets.token_hex(16)
    try:
        request = encode_account_request(AccountRequest(nonce, trusted_account))
    except AccountRequestError as error:
        if error.failure is AccountFailure.OVERSIZED:
            raise ValidationError("Account lookup request exceeds its manifest bound") from None
        raise ValidationError("Account lookup requires a valid UTF-8 account") from None
    invocation = PreparedInvocation(
        build_clean_helper_argv(runtime_path=runtime_path, fixed_source=FIXED_SOURCE, nonce=nonce)
    )
    response = _BoundedResponseSink()
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(request, sensitive=True),
        output=SinkOutput(response, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(invocation, io=io, deadline=deadline)
        observation = _observe(response, stderr, report, nonce)
        return AccountResolutionResult(
            dispatch=report.dispatch,
            carrier_completion=report.completion,
            carrier_local_status=report.local_status,
            carrier_failure=report.failure,
            observation=observation,
        )
    finally:
        response.clear()
        stderr.clear()


def resolve_file_ownership(
    carrier: Carrier,
    trusted_owner: str,
    trusted_group: str,
    deadline: Deadline,
    runtime_path: str,
) -> FileOwnershipResolutionResult:
    """Resolve one core-bound owner/group pair without replay or identity transition."""
    nonce = secrets.token_hex(16)
    try:
        request = encode_file_ownership_request(FileOwnershipRequest(nonce, trusted_owner, trusted_group))
    except FileOwnershipRequestError as error:
        if error.failure is FileOwnershipFailure.OVERSIZED:
            raise ValidationError("File ownership lookup request exceeds its manifest bound") from None
        raise ValidationError("File ownership lookup requires valid UTF-8 names") from None
    invocation = PreparedInvocation(
        build_clean_helper_argv(runtime_path=runtime_path, fixed_source=FIXED_SOURCE, nonce=nonce)
    )
    response = _BoundedResponseSink()
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(request, sensitive=True),
        output=SinkOutput(response, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(invocation, io=io, deadline=deadline)
        observation = _observe_file_ownership(response, stderr, report, nonce)
        return FileOwnershipResolutionResult(
            dispatch=report.dispatch,
            carrier_completion=report.completion,
            carrier_local_status=report.local_status,
            carrier_failure=report.failure,
            observation=observation,
        )
    finally:
        response.clear()
        stderr.clear()
