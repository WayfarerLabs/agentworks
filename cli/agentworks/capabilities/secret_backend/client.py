"""Provider-facing contracts for bounded secret-source clients."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pydantic import BaseModel


_UNSAFE_IDENTITY_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def safe_identity(value: str) -> str:
    """Validate one backend-boundary identity field.

    Boundary: plugin-produced or operator-authored text that core may retain
    and render. Exact, non-empty strings only, with no characters capable of
    forging a diagnostic line.
    """
    if type(value) is not str or not value:
        raise ValueError("invalid secret identity")
    if any(unicodedata.category(char) in _UNSAFE_IDENTITY_CATEGORIES for char in value):
        raise ValueError("invalid secret identity")
    return value


class OperatorImpact(StrEnum):
    """The operator impact a value-free preview may cause."""

    NONE = "none"
    ALLOW = "allow"


class TtyInteractionAccess(StrEnum):
    """Whether a backend may use terminal input for this operation."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class IndeterminateReason(StrEnum):
    """Why broader preview impact could improve an answer."""

    OPERATOR_IMPACT_LIMITED = "operator-impact-limited"


class BlockReason(StrEnum):
    """Closed execution limitations retained by core."""

    TTY_UNAVAILABLE = "tty-unavailable"
    TTY_INTERACTION_DISABLED = "tty-interaction-disabled"
    SOURCE_NOT_READY = "source-not-ready"
    BACKEND_PLUGIN_DISABLED = "backend-plugin-disabled"
    NO_ACTIVE_SOURCE = "no-active-source"
    NO_ATTEMPTABLE_SOURCE = "no-attemptable-source"


class FailureReason(StrEnum):
    """Closed lookup and provider failure categories."""

    INVALID_MAPPING = "invalid-mapping"
    LOOKUP_REJECTED = "lookup-rejected"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    EXTERNAL = "external"
    MALFORMED_VALUE = "malformed-value"
    BACKEND_PROTOCOL = "backend-protocol"
    UNEXPECTED = "unexpected"


class LookupDisposition(StrEnum):
    """The static applicability of one declared backend mapping."""

    CANDIDATE = "candidate"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class LookupDescription:
    """A no-I/O declaration projection for one source lookup."""

    disposition: LookupDisposition
    identifier: str | None

    def __post_init__(self) -> None:
        if type(self.disposition) is not LookupDisposition:
            raise ValueError("invalid lookup disposition")
        if self.identifier is not None:
            safe_identity(self.identifier)
        if self.disposition is LookupDisposition.NOT_APPLICABLE and self.identifier is not None:
            raise ValueError("a non-applicable lookup cannot carry an identifier")


@dataclass(frozen=True, slots=True)
class SecretLookupRequest:
    """The only per-secret data a source-bound client receives."""

    name: str
    mapping: BaseModel | None

    def __post_init__(self) -> None:
        safe_identity(self.name)


class InteractionBroker(Protocol):
    """Caller-owned access to an explicitly authorized terminal prompt."""

    def request_secret(self, name: str, /) -> str: ...


type RemainingTime = Callable[[], float | None]
"""A live view of the current source-turn budget."""


@dataclass(frozen=True, slots=True)
class PreviewIntent:
    """Construct a source client for a value-free preview."""

    impact: OperatorImpact

    def __post_init__(self) -> None:
        if type(self.impact) is not OperatorImpact:
            raise ValueError("preview impact must be an exact OperatorImpact")


@dataclass(frozen=True, slots=True)
class ResolutionIntent:
    """Construct a source client for authoritative value resolution."""


type SecretClientIntent = PreviewIntent | ResolutionIntent


@dataclass(frozen=True, slots=True)
class PreviewAvailable:
    """A preview established that a valid value exists."""


@dataclass(frozen=True, slots=True)
class PreviewMissing:
    """A valid preview lookup established ordinary absence."""


@dataclass(frozen=True, slots=True)
class PreviewIndeterminate:
    """Broader operator impact could improve the preview answer."""

    reason: IndeterminateReason

    def __post_init__(self) -> None:
        if type(self.reason) is not IndeterminateReason:
            raise ValueError("invalid preview indeterminate reason")


@dataclass(frozen=True, slots=True)
class PreviewBlocked:
    """The preview cannot execute under a current capability fact."""

    reason: BlockReason

    def __post_init__(self) -> None:
        if type(self.reason) is not BlockReason:
            raise ValueError("invalid preview block reason")


@dataclass(frozen=True, slots=True)
class PreviewFailed:
    """The configured lookup or permitted provider work failed."""

    reason: FailureReason

    def __post_init__(self) -> None:
        if type(self.reason) is not FailureReason:
            raise ValueError("invalid preview failure reason")


type BackendPreview = PreviewAvailable | PreviewMissing | PreviewIndeterminate | PreviewBlocked | PreviewFailed


@dataclass(frozen=True, slots=True, repr=False)
class BackendResolved:
    """The only value-bearing backend result."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or "\0" in self.value:
            raise ValueError("invalid resolved secret value")

    def __repr__(self) -> str:
        return "BackendResolved(value=<redacted>)"


@dataclass(frozen=True, slots=True)
class BackendMissing:
    """A valid resolution lookup established ordinary absence."""


@dataclass(frozen=True, slots=True)
class BackendBlocked:
    """Authoritative resolution cannot execute under a TTY fact."""

    reason: BlockReason

    def __post_init__(self) -> None:
        if type(self.reason) is not BlockReason:
            raise ValueError("invalid backend block reason")


@dataclass(frozen=True, slots=True)
class BackendFailed:
    """The configured lookup or provider operation failed."""

    reason: FailureReason

    def __post_init__(self) -> None:
        if type(self.reason) is not FailureReason:
            raise ValueError("invalid backend failure reason")


type BackendResolution = BackendResolved | BackendMissing | BackendBlocked | BackendFailed


class SecretSourceClient(Protocol):
    """One operation-bounded client for one configured secret source."""

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]: ...

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]: ...
