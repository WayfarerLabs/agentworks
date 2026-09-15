"""Integration-owned delivery decisions and persisted artifact file ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, field_validator, model_validator

from agentworks.artifacts.model import ArtifactFacet, ArtifactInputs
from agentworks.schema import AgwModel

if TYPE_CHECKING:
    from collections.abc import Mapping


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
    package_root: str | None = None


class OwnedArtifactFile(AgwModel):
    """Confirmed whole-file effects within the owner's existing applied state."""

    path: Annotated[str, Field(max_length=4096)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    origins: Annotated[tuple[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], ...], Field(strict=False, min_length=1)]
    executable: bool = False
    native_identity: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[^\x00-\x1f\x7f]+$")] | None = None

    package_root: Annotated[str, Field(max_length=4096)] | None = None

    @field_validator("path")
    @classmethod
    def _owned_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or not value.isprintable()
            or any(part in ("", ".", "..") for part in value.split("/")[1:])
        ):
            raise ValueError("owned artifact paths must be absolute normalized paths")
        return value

    @field_validator("package_root")
    @classmethod
    def _owned_package_root(cls, value: str | None) -> str | None:
        return None if value is None else cls._owned_path(value)

    @model_validator(mode="after")
    def _package_contains_file(self) -> OwnedArtifactFile:
        if self.package_root is not None and not self.path.startswith(self.package_root + "/"):
            raise ValueError("owned artifact file is outside its recorded package root")
        return self


@dataclass(frozen=True)
class ArtifactApplication:
    """A native delivery plan; only deferred inputs need a later facet."""

    files: tuple[ArtifactFile, ...] = ()
    deferred: tuple[ArtifactDeferral, ...] = ()
    artifacts_dir: str | None = None


@dataclass(frozen=True, kw_only=True)
class SessionArtifactContext:
    """Core-prepared immutable inputs and identities for a prospective launch."""

    inputs: ArtifactInputs
    home: str
    directory: str
    session_uuid: str
    run_id: str
    ancestor_files: tuple[OwnedArtifactFile, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
