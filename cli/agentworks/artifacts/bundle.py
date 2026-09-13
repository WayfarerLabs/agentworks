"""The ordinary artifact-bundle declared resource."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from agentworks.artifacts.declarations import ArtifactSpec
from agentworks.declared_resource import DeclaredResource


class ArtifactBundle(DeclaredResource):
    """An ordered collection of named, harness-independent artifact sources."""

    artifacts: dict[Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", max_length=64)], ArtifactSpec] = (
        Field(
            default_factory=dict,
            examples=[{"setup": {"type": "hint", "text": "Project tools are available through mise."}}],
        )
    )
    """Entries in declaration order. Entry names identify bundle content independently
    of native skill or agent names supplied by their definitions."""
