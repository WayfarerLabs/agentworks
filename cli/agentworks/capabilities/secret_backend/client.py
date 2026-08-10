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


class SecretClientTimeout(Exception):
    """A client boundary timed out after its underlying work stopped."""

    __slots__ = ()

    def __str__(self) -> str:
        return "secret client operation timed out"

    def __repr__(self) -> str:
        return "SecretClientTimeout()"
