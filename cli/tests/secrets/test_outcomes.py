"""Closed rendering behavior for every actual-resolution result variant."""

from __future__ import annotations

import pytest

from agentworks.capabilities.secret_backend import BlockReason, FailureReason
from agentworks.errors import ConnectivityError, ExternalError, SecretMappingError, SecretUnavailableError
from agentworks.secrets.outcomes import (
    ResolutionBlocked,
    ResolutionFailed,
    ResolutionMissing,
    ResolutionOutcome,
    ResolutionResolved,
    complete_resolution_error,
    format_hint,
    format_outcome,
)


@pytest.mark.parametrize(
    "outcome",
    [
        ResolutionOutcome("resolved", ResolutionResolved(), source="fixture"),
        ResolutionOutcome("missing", ResolutionMissing(), source="fixture"),
        ResolutionOutcome(
            "blocked",
            ResolutionBlocked(BlockReason.TTY_UNAVAILABLE),
            source="fixture",
        ),
        ResolutionOutcome(
            "structural",
            ResolutionBlocked(BlockReason.NO_ACTIVE_SOURCE),
        ),
        ResolutionOutcome(
            "batch-doomed",
            ResolutionBlocked(BlockReason.BATCH_DOOMED),
        ),
        ResolutionOutcome(
            "failed",
            ResolutionFailed(FailureReason.CONNECTIVITY),
            source="fixture",
        ),
    ],
)
def test_format_outcome_is_safe_for_every_accepted_result(outcome: ResolutionOutcome) -> None:
    rendered = format_outcome(outcome)
    assert outcome.status.value in rendered


def test_batch_doomed_is_an_unattributed_final_outcome_only() -> None:
    outcome = ResolutionOutcome("secret", ResolutionBlocked(BlockReason.BATCH_DOOMED))
    assert outcome.source is None
    with pytest.raises(ValueError):
        ResolutionOutcome("secret", ResolutionBlocked(BlockReason.BATCH_DOOMED), source="fixture")


def test_onepassword_deadline_uses_backend_specific_guidance() -> None:
    failure = ResolutionFailed(FailureReason.DEADLINE_EXCEEDED)
    onepassword = ResolutionOutcome("secret", failure, source="fixture", backend="onepassword")
    generic = ResolutionOutcome("secret", failure, source="fixture", backend="fixture")

    assert format_hint(onepassword) != format_hint(generic)


@pytest.mark.parametrize(
    ("reason", "error_type"),
    [
        (FailureReason.INVALID_MAPPING, SecretMappingError),
        (FailureReason.LOOKUP_REJECTED, SecretMappingError),
        (FailureReason.CONNECTIVITY, ConnectivityError),
        (FailureReason.DEADLINE_EXCEEDED, SecretUnavailableError),
        (FailureReason.AUTHENTICATION, ExternalError),
        (FailureReason.EXTERNAL, ExternalError),
        (FailureReason.MALFORMED_VALUE, ExternalError),
        (FailureReason.BACKEND_PROTOCOL, ExternalError),
        (FailureReason.UNEXPECTED, ExternalError),
    ],
)
def test_failed_resolution_maps_to_error_taxonomy(
    reason: FailureReason,
    error_type: type[Exception],
) -> None:
    outcome = ResolutionOutcome(
        "secret",
        ResolutionFailed(reason),
        source="fixture",
        identifier="safe-identifier",
        backend="onepassword",
    )

    error = complete_resolution_error((outcome,))

    assert type(error) is error_type
