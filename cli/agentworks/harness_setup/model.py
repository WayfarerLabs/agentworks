"""Compact metadata evidence for completed and interrupted native setup."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from agentworks.schema import AgwModel

type SetupFacet = Literal["vm", "user", "workspace"]
type SetupComponent = Literal["vm", "admin", "agent", "workspace"]

_Text = Annotated[str, Field(min_length=1)]
_Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class NativeClaim(AgwModel):
    """An integration's confirmed association, never native document contents.

    Native identifiers and optional source identity distinguish registrations
    from coincidentally matching names. Settings mappings create no claims.
    """

    role: _Text
    identifier: _Text
    destination: _Text
    source: str | None = None
    # Read compatibility for settings receipts already produced by this effort.
    # New setup drops those receipts and never emits these fields with values.
    sha256: _Hash | None = None
    strategy: str | None = None
    # JSON carries tuples as arrays; only sequence representation is lenient.
    written_keys: Annotated[tuple[Annotated[tuple[str, ...], Field(strict=False)], ...], Field(strict=False)] = ()


class SetupRecord(AgwModel):
    """One integration activation's confirmed mutation prefix at one destination."""

    component: SetupComponent
    integration: _Text
    destination_id: _Hash
    declaration: dict[str, JsonValue]
    complete: bool = False
    pending_cleanup: bool = False
    claims: Annotated[tuple[NativeClaim, ...], Field(strict=False)] = ()

    @model_validator(mode="after")
    def _unique_claims(self) -> SetupRecord:
        """Reject ambiguous ownership keys in persisted domain input."""
        keys = [(claim.role, claim.identifier, claim.destination) for claim in self.claims]
        if len(keys) != len(set(keys)):
            raise ValueError("native setup contains duplicate claims")
        if self.complete and self.pending_cleanup:
            raise ValueError("pending cleanup cannot be complete setup")
        return self


class NativeSetupState(AgwModel):
    """Ordered integration evidence within one instance-state slice."""

    records: Annotated[tuple[SetupRecord, ...], Field(strict=False)] = ()

    @model_validator(mode="after")
    def _unique_records(self) -> NativeSetupState:
        """Validate persisted component/integration identity uniqueness."""
        keys = [(record.component, record.integration) for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("native setup contains duplicate integration records")
        return self
