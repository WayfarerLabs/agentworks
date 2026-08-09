"""Value-free secret resolution outcomes and operation error projection."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import (
    AgentworksError,
    ConnectivityError,
    ExternalError,
    SecretMappingError,
    SecretUnavailableError,
    StateError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class ResolutionCategory(StrEnum):
    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"
    REFUSED_INTERACTION = "refused-interaction"
    TIMEOUT = "timeout"
    RESOLUTION_FAILURE = "resolution-failure"


class ResolutionDetail(StrEnum):
    RESOLVED = "resolved"
    NO_ACTIVE_SOURCE = "no-active-source"
    NO_ATTEMPTABLE_SOURCE = "no-attemptable-source"
    SOURCE_NOT_READY = "source-not-ready"
    SOURCE_BACKEND_PLUGIN_DISABLED = "source-backend-plugin-disabled"
    SOFT_MISS = "soft-miss"
    INTERACTION_REFUSED = "interaction-refused"
    BATCH_DOOMED = "batch-doomed-before-interaction"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    HARD_MAPPING = "hard-mapping"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    EXTERNAL = "external"
    MALFORMED_VALUE = "malformed-value"
    BACKEND_PROTOCOL = "backend-protocol"
    UNEXPECTED = "unexpected"


class ResolutionRemediation(StrEnum):
    NONE = "none"
    CONFIGURE_SOURCE = "configure-source"
    ENABLE_SOURCE = "enable-source"
    ENABLE_PLUGIN = "enable-plugin"
    ALLOW_INTERACTION = "allow-interaction"
    RESOLVE_BLOCKING_SECRETS = "resolve-blocking-secrets"
    CHECK_MAPPING = "check-mapping"
    SIGN_IN = "sign-in"
    CHECK_CONNECTIVITY = "check-connectivity"
    INCREASE_TIMEOUT = "increase-timeout"
    REMOVE_CONTROL_CHARACTERS = "remove-control-characters"
    RETRY = "retry"
    REPORT_BACKEND = "report-backend"


@dataclass(frozen=True, slots=True)
class OutcomeRule:
    category: ResolutionCategory
    remediation: ResolutionRemediation
    source_required: bool
    identifier_allowed: bool
    remediation_target_required: bool = False


OUTCOME_RULES: dict[ResolutionDetail, OutcomeRule] = {
    ResolutionDetail.RESOLVED: OutcomeRule(ResolutionCategory.RESOLVED, ResolutionRemediation.NONE, True, True),
    ResolutionDetail.NO_ACTIVE_SOURCE: (
        OutcomeRule(ResolutionCategory.UNAVAILABLE, ResolutionRemediation.CONFIGURE_SOURCE, False, False)
    ),
    ResolutionDetail.NO_ATTEMPTABLE_SOURCE: (
        OutcomeRule(ResolutionCategory.UNAVAILABLE, ResolutionRemediation.CONFIGURE_SOURCE, False, False)
    ),
    ResolutionDetail.SOURCE_NOT_READY: (
        OutcomeRule(ResolutionCategory.UNAVAILABLE, ResolutionRemediation.ENABLE_SOURCE, True, True)
    ),
    ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED: (
        OutcomeRule(
            ResolutionCategory.UNAVAILABLE,
            ResolutionRemediation.ENABLE_PLUGIN,
            True,
            True,
            remediation_target_required=True,
        )
    ),
    ResolutionDetail.SOFT_MISS: (
        OutcomeRule(ResolutionCategory.UNAVAILABLE, ResolutionRemediation.CONFIGURE_SOURCE, True, True)
    ),
    ResolutionDetail.INTERACTION_REFUSED: (
        OutcomeRule(ResolutionCategory.REFUSED_INTERACTION, ResolutionRemediation.ALLOW_INTERACTION, True, True)
    ),
    ResolutionDetail.BATCH_DOOMED: (
        OutcomeRule(ResolutionCategory.UNAVAILABLE, ResolutionRemediation.RESOLVE_BLOCKING_SECRETS, False, False)
    ),
    ResolutionDetail.DEADLINE_EXCEEDED: (
        OutcomeRule(ResolutionCategory.TIMEOUT, ResolutionRemediation.INCREASE_TIMEOUT, True, True)
    ),
    ResolutionDetail.HARD_MAPPING: (
        OutcomeRule(ResolutionCategory.RESOLUTION_FAILURE, ResolutionRemediation.CHECK_MAPPING, True, True)
    ),
    ResolutionDetail.AUTHENTICATION: (
        OutcomeRule(ResolutionCategory.RESOLUTION_FAILURE, ResolutionRemediation.SIGN_IN, True, True)
    ),
    ResolutionDetail.CONNECTIVITY: (
        OutcomeRule(ResolutionCategory.RESOLUTION_FAILURE, ResolutionRemediation.CHECK_CONNECTIVITY, True, True)
    ),
    ResolutionDetail.EXTERNAL: (
        OutcomeRule(ResolutionCategory.RESOLUTION_FAILURE, ResolutionRemediation.RETRY, True, True)
    ),
    ResolutionDetail.MALFORMED_VALUE: (
        OutcomeRule(
            ResolutionCategory.RESOLUTION_FAILURE,
            ResolutionRemediation.REMOVE_CONTROL_CHARACTERS,
            True,
            True,
        )
    ),
    ResolutionDetail.BACKEND_PROTOCOL: (
        OutcomeRule(ResolutionCategory.RESOLUTION_FAILURE, ResolutionRemediation.REPORT_BACKEND, True, False)
    ),
    ResolutionDetail.UNEXPECTED: (
        OutcomeRule(ResolutionCategory.RESOLUTION_FAILURE, ResolutionRemediation.REPORT_BACKEND, True, True)
    ),
}


_UNSAFE_DIAGNOSTIC_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _safe_diagnostic_text(value: str) -> bool:
    """Reject text that can alter or forge a rendered diagnostic line."""
    return all(unicodedata.category(char) not in _UNSAFE_DIAGNOSTIC_CATEGORIES for char in value)


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """One stable diagnostic row that never carries a resolved value."""

    name: str
    category: ResolutionCategory
    detail: ResolutionDetail
    remediation: ResolutionRemediation
    source: str | None = None
    identifier: str | None = None
    remediation_target: str | None = None

    def __post_init__(self) -> None:
        rule = OUTCOME_RULES[self.detail]
        if self.category is not rule.category or self.remediation is not rule.remediation:
            raise ValueError("invalid resolution outcome category or remediation")
        if (self.source is not None) is not rule.source_required:
            raise ValueError("invalid resolution outcome source")
        if not rule.identifier_allowed and self.identifier is not None:
            raise ValueError("invalid resolution outcome identifier")
        if (self.remediation_target is not None) is not rule.remediation_target_required:
            raise ValueError("invalid resolution outcome remediation target presence")
        if rule.remediation_target_required and (
            not isinstance(self.remediation_target, str)
            or not self.remediation_target
            or "/" in self.remediation_target
        ):
            raise ValueError("enable-plugin remediation target must be non-empty and '/'-free")
        if not _safe_diagnostic_text(self.name):
            raise ValueError("invalid resolution outcome name")
        if self.source is not None and not _safe_diagnostic_text(self.source):
            raise ValueError("invalid resolution outcome source")
        if self.identifier is not None and not _safe_diagnostic_text(self.identifier):
            raise ValueError("invalid resolution outcome identifier")


def _escape_plugin_target(target: str) -> str:
    escaped: list[str] = []
    for char in target:
        codepoint = ord(char)
        if char.isascii() and (char.isalnum() or char in "._-"):
            escaped.append(char)
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)


def format_remediation(outcome: ResolutionOutcome) -> str:
    """Render one bounded remediation without provider-supplied text."""
    if outcome.remediation is ResolutionRemediation.ENABLE_PLUGIN:
        assert outcome.remediation_target is not None
        return f"enable plugin `{_escape_plugin_target(outcome.remediation_target)}`"
    return outcome.remediation.value


def format_outcome(outcome: ResolutionOutcome) -> str:
    """Render the stable value-free diagnostic fields for one outcome."""
    return (
        f"{outcome.category.value}/{outcome.detail.value}; "
        f"source={outcome.source or 'none'}; identifier={outcome.identifier or 'none'}; "
        f"remediation={format_remediation(outcome)}"
    )


def complete_resolution_error(outcomes: Sequence[ResolutionOutcome]) -> AgentworksError:
    """Map an incomplete value-free batch to the operation error taxonomy."""
    failed = tuple(outcome for outcome in outcomes if outcome.category is not ResolutionCategory.RESOLVED)
    if not failed:
        raise StateError("complete_resolution_error requires at least one non-resolved outcome") from None
    first = failed[0]
    if first.detail is ResolutionDetail.HARD_MAPPING:
        error_type: type[AgentworksError] = SecretMappingError
    elif first.detail is ResolutionDetail.CONNECTIVITY:
        error_type = ConnectivityError
    elif first.detail in {
        ResolutionDetail.AUTHENTICATION,
        ResolutionDetail.DEADLINE_EXCEEDED,
        ResolutionDetail.EXTERNAL,
        ResolutionDetail.MALFORMED_VALUE,
        ResolutionDetail.BACKEND_PROTOCOL,
        ResolutionDetail.UNEXPECTED,
    }:
        error_type = ExternalError
    else:
        error_type = SecretUnavailableError
    names = ", ".join(outcome.name for outcome in failed)
    hint = "\n".join(f"{outcome.name}: {format_outcome(outcome)}" for outcome in failed)
    return error_type(f"secret resolution failed for: {names}", hint=hint)
