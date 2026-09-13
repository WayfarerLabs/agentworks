"""Integration-owned delivery decisions and persisted artifact file ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from agentworks.artifacts.model import ArtifactFacet
from agentworks.schema import AgwModel

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.artifacts.model import ArtifactInput


class ArtifactDeferral(AgwModel):
    """An unchanged input routed by an integration, validated at its boundary."""

    input_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    destination: ArtifactFacet
    reason: Annotated[str, Field(min_length=1)]


@dataclass(frozen=True)
class ArtifactFile:
    """One concrete native file selected by an integration for publication."""

    path: str
    data: bytes = field(repr=False)
    origins: tuple[str, ...]
    executable: bool = False
    native_identity: str | None = None


class OwnedArtifactFile(AgwModel):
    """Confirmed whole-file effects within the owner's existing applied state."""

    path: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    origins: Annotated[tuple[str, ...], Field(strict=False)]
    executable: bool = False
    native_identity: str | None = None


@dataclass(frozen=True)
class ArtifactApplication:
    """A native delivery plan; only deferred inputs need a later facet."""

    files: tuple[ArtifactFile, ...] = ()
    deferred: tuple[ArtifactDeferral, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, kw_only=True)
class SessionArtifactContext:
    """Core-prepared immutable inputs and identities for a prospective launch."""

    inputs: tuple[ArtifactInput, ...]
    home: str
    directory: str
    session_uuid: str
    run_id: str
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
