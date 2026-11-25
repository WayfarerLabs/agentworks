"""Native setup fields shared by the Claude and Codex plugins."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field, field_validator

from agentworks.capabilities.harness_integration.settings import SettingsMapping
from agentworks.schema import AgwModel


def _validate_native_entry(value: str) -> str:
    """Validate one operator-supplied native setup entry at its list index."""
    if not value.strip() or "\x00" in value:
        raise ValueError("native setup entries must be nonblank and contain no NUL characters")
    return value


_NativeEntry = Annotated[str, AfterValidator(_validate_native_entry)]


class NativeWorkspaceConfig(AgwModel):
    """One project settings mapping, without user plugin installation."""

    settings: SettingsMapping | None = None
    """Map a workstation file to this facet's native settings role."""


class NativeUserConfig(NativeWorkspaceConfig):
    """Native marketplaces, plugins, and settings for one actual user.

    Selection lists append and deduplicate across layers. Omission or an empty
    list inherits; null clears inherited entries.
    """

    marketplaces: list[_NativeEntry] | None = Field(default_factory=list)
    """Non-secret marketplace sources accepted by the installed native CLI."""

    plugins: list[_NativeEntry] | None = Field(default_factory=list)
    """Plugin names or unambiguous plugin@marketplace identifiers to install."""

    @field_validator("marketplaces", "plugins")
    @classmethod
    def _unique_entries(cls, values: list[str] | None) -> list[str] | None:
        """Reject repeated effective entries before native work."""
        if values is None:
            return None
        if len(values) != len(set(values)):
            raise ValueError("native setup entries must be unique")
        return values

    @field_validator("marketplaces")
    @classmethod
    def _not_an_option(cls, values: list[str] | None) -> list[str] | None:
        """Reject a source the native CLI would read as an option; boundary: operator-authored config."""
        if values is not None and any(value.startswith("-") for value in values):
            raise ValueError(
                "native marketplace sources must not begin with '-', which a native CLI reads as an option"
            )
        return values
