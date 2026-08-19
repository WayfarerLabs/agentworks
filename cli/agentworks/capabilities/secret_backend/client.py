"""Provider-facing contracts for bounded secret-source clients."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class SecretLookupRequest:
    """The only per-secret data a source-bound client receives."""

    name: str
    mapping: BaseModel | None


class InteractionBroker(Protocol):
    """Caller-owned access to an explicitly authorized secret prompt."""

    def request_secret(self, name: str, /) -> str: ...


type RemainingTime = Callable[[], float | None]
"""A live view of the current source-turn budget."""


class SecretSourceClient(Protocol):
    """One operation-bounded client for one configured secret source."""

    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> None: ...

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> Mapping[str, str]: ...


class SecretClientFailureKind(StrEnum):
    """Value-free provider failure categories consumed by the resolver."""

    HARD_MAPPING = "hard-mapping"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    EXTERNAL = "external"


class SecretClientRemediation(StrEnum):
    """The fixed remediation associated with a provider failure kind."""

    CHECK_MAPPING = "check-mapping"
    SIGN_IN = "sign-in"
    CHECK_CONNECTIVITY = "check-connectivity"
    RETRY = "retry"


class SecretClientFailure(Exception):
    """A value-free, safely representable client failure."""

    __slots__ = ("kind", "remediation")

    kind: SecretClientFailureKind
    remediation: SecretClientRemediation

    def __init__(
        self,
        *,
        kind: SecretClientFailureKind,
        remediation: SecretClientRemediation,
    ) -> None:
        expected = {
            SecretClientFailureKind.HARD_MAPPING: SecretClientRemediation.CHECK_MAPPING,
            SecretClientFailureKind.AUTHENTICATION: SecretClientRemediation.SIGN_IN,
            SecretClientFailureKind.CONNECTIVITY: SecretClientRemediation.CHECK_CONNECTIVITY,
            SecretClientFailureKind.EXTERNAL: SecretClientRemediation.RETRY,
        }[kind]
        if remediation is not expected:
            raise ValueError("invalid secret client failure remediation")
        super().__init__()
        self.kind = kind
        self.remediation = remediation

    def __str__(self) -> str:
        return "secret client failure"

    def __repr__(self) -> str:
        return f"SecretClientFailure(kind={self.kind.value!r}, remediation={self.remediation.value!r})"


class TimeoutGuidance(StrEnum):
    """A closed set of backend-selectable timeout causes.

    A backend never supplies its own guidance text: it selects a member of
    this enum, and core (``secrets.outcomes.format_remediation``) owns the
    fixed prose each member maps to. This is what keeps the channel
    value-free even though ``SecretClientTimeout`` is raised from plugin
    code we do not type-check: a character screen on free text cannot prove
    the text is static rather than provider output, but membership in a
    closed, core-defined enum can be checked and enforced outright.
    """

    ONEPASSWORD_PENDING_APPROVAL = "onepassword-pending-approval"
    """A pending approval prompt in the 1Password desktop app is a common
    cause of a onepassword backend timeout."""


class SecretClientTimeout(Exception):
    """A client boundary timed out after its underlying work stopped.

    ``guidance`` is optional: a backend selects a member of the closed
    ``TimeoutGuidance`` set, never raw text. This exception carries no
    native text at all (that boundary is what keeps a timeout value-free),
    and construction itself rejects anything that is not an actual
    ``TimeoutGuidance`` member, so a plugin cannot forge one by passing a
    plain string with a matching value. ``None`` when the backend has
    nothing more specific to say than "it timed out."
    """

    __slots__ = ("guidance",)

    guidance: TimeoutGuidance | None

    def __init__(self, *, guidance: TimeoutGuidance | None = None) -> None:
        if guidance is not None and not isinstance(guidance, TimeoutGuidance):
            raise ValueError("invalid secret client timeout guidance")
        super().__init__()
        self.guidance = guidance

    def __str__(self) -> str:
        return "secret client operation timed out"

    def __repr__(self) -> str:
        return f"SecretClientTimeout(guidance={self.guidance!r})"
