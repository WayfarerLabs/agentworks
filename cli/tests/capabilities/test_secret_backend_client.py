"""Closed value and identity invariants at the secret-backend boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from agentworks.capabilities.secret_backend import (
    BackendBlocked,
    BackendFailed,
    BackendResolved,
    BlockReason,
    FailureReason,
    IndeterminateReason,
    LookupDescription,
    LookupDisposition,
    PreviewBlocked,
    PreviewFailed,
    PreviewIndeterminate,
    SecretLookupRequest,
)
from agentworks.capabilities.secret_backend.client import safe_identity


@pytest.mark.parametrize(
    "value",
    ["", "line\nbreak", "tab\tbreak", "format\u200bbreak", "surrogate\ud800break", "line\u2028break"],
)
def test_safe_identity_rejects_empty_and_line_forging_text(value: str) -> None:
    with pytest.raises(ValueError):
        safe_identity(value)


def test_safe_identity_requires_an_exact_string() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(ValueError):
        safe_identity(StringSubclass("apparently-safe"))


def test_lookup_description_enforces_disposition_and_identifier_pairing() -> None:
    with pytest.raises(ValueError):
        LookupDescription(LookupDisposition.NOT_APPLICABLE, "identifier")
    with pytest.raises(ValueError):
        LookupDescription("candidate", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LookupDescription(LookupDisposition.CANDIDATE, "surrogate\ud800identifier")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PreviewIndeterminate(cast("IndeterminateReason", "operator-impact-limited")),
        lambda: PreviewBlocked(cast("BlockReason", "tty-unavailable")),
        lambda: PreviewFailed(cast("FailureReason", "external")),
        lambda: BackendBlocked(cast("BlockReason", "tty-unavailable")),
        lambda: BackendFailed(cast("FailureReason", "external")),
    ],
)
def test_closed_result_reasons_require_exact_enum_members(factory: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize("reason", list(IndeterminateReason))
def test_closed_indeterminate_reason_members_are_accepted(reason: IndeterminateReason) -> None:
    assert PreviewIndeterminate(reason).reason is reason


def test_closed_result_reason_members_and_request_identity_are_accepted() -> None:
    assert PreviewBlocked(BlockReason.TTY_UNAVAILABLE).reason is BlockReason.TTY_UNAVAILABLE
    assert PreviewFailed(FailureReason.EXTERNAL).reason is FailureReason.EXTERNAL
    assert BackendBlocked(BlockReason.TTY_INTERACTION_DISABLED).reason is BlockReason.TTY_INTERACTION_DISABLED
    assert BackendFailed(FailureReason.CONNECTIVITY).reason is FailureReason.CONNECTIVITY
    assert SecretLookupRequest("token", None).name == "token"


def test_resolved_value_is_redacted_and_rejects_nul() -> None:
    result = BackendResolved("must-not-escape")
    assert "must-not-escape" not in repr(result)
    with pytest.raises(ValueError):
        BackendResolved("bad\0value")
