"""Closed rendering behavior for every actual-resolution result variant."""

from __future__ import annotations

import pytest

from agentworks.capabilities.secret_backend import BlockReason, FailureReason
from agentworks.secrets.outcomes import (
    ResolutionBlocked,
    ResolutionFailed,
    ResolutionMissing,
    ResolutionOutcome,
    ResolutionResolved,
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
            "failed",
            ResolutionFailed(FailureReason.CONNECTIVITY),
            source="fixture",
        ),
    ],
)
def test_format_outcome_is_safe_for_every_accepted_result(outcome: ResolutionOutcome) -> None:
    rendered = format_outcome(outcome)
    assert outcome.status.value in rendered
