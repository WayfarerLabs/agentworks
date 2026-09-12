"""Native setup fields shared by the Claude and Codex plugins."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator

from agentworks.capabilities.harness_integration.settings import SettingsMapping
from agentworks.schema import AgwModel, MergeStrategy


class NativeWorkspaceConfig(AgwModel):
    """One project settings mapping, without user plugin installation."""

    settings: SettingsMapping | None = None
    """Map a workstation file to this facet's native settings role."""


class NativeUserConfig(NativeWorkspaceConfig):
    """Native marketplaces, plugins, and settings for one actual user."""

    marketplaces: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)
    """Non-secret marketplace sources accepted by the installed native CLI."""

    plugins: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)
    """Plugin names or unambiguous plugin@marketplace identifiers to install."""

    @field_validator("marketplaces", "plugins")
    @classmethod
    def _unique_nonblank(cls, values: list[str]) -> list[str]:
        """Reject ambiguous operator-authored lists before native work."""
        if any(not value.strip() or "\x00" in value for value in values) or len(values) != len(set(values)):
            raise ValueError("native setup entries must be nonblank and unique")
        return values
