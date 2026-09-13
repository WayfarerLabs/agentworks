"""Operator-authored artifact entries and scoped bundle references."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from agentworks.package_sources import validate_artifact_source
from agentworks.schema import AgwModel, MergeStrategy, NonBlankStr, ResourceRef
from agentworks.sources import SourceRefError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.inheritance import LayerSource
    from agentworks.resources.reference import ResourceReference
    from agentworks.value_provenance import ProvenancePath


class _ArtifactSpec(AgwModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    @field_validator("source", check_fields=False)
    @classmethod
    def _source_reference(cls, source: str | None) -> str | None:
        """Keep credentials out of persisted declarations and rendered errors."""
        if source is not None:
            try:
                validate_artifact_source(source)
            except SourceRefError as error:
                raise ValueError(str(error)) from None
        return source

    preserve_bytes: list[NonBlankStr] = Field(default_factory=list)
    """Relative paths or globs for supporting files whose bytes must not be normalized.
    A skill's SKILL.md always uses normalized UTF-8 text."""

    @field_validator("preserve_bytes")
    @classmethod
    def _relative_patterns(cls, patterns: list[str]) -> list[str]:
        """Reject escaping patterns at the declaration boundary."""
        for pattern in patterns:
            if (
                pattern.startswith(("/", "~"))
                or "\\" in pattern
                or any(part in {"", ".", ".."} for part in pattern.split("/"))
            ):
                raise ValueError("preserve_bytes patterns must be relative paths without traversal")
        return patterns


class _TextArtifactSpec(_ArtifactSpec):
    text: NonBlankStr | None = None
    """Inline context text; specify exactly one of text or source."""

    source: NonBlankStr | None = None
    """Workstation file or Git file source; mutually exclusive with text."""

    @model_validator(mode="after")
    def _one_input(self) -> Self:
        """Require one content source in an operator declaration."""
        if (self.text is None) == (self.source is None):
            raise ValueError("specify exactly one of text or source")
        return self


class HintArtifactSpec(_TextArtifactSpec):
    """Small contextual information about the Agentworks setup."""

    type: Literal["hint"] = "hint"
    """Artifact type."""


class RuleArtifactSpec(_TextArtifactSpec):
    """Instructions always loaded into the agent's context."""

    type: Literal["rule"] = "rule"
    """Artifact type."""


class SkillArtifactSpec(_ArtifactSpec):
    """A complete standard Agent Skills directory with SKILL.md at its root."""

    type: Literal["skill"] = "skill"
    """Artifact type."""

    source: NonBlankStr = Field(examples=["file::~/agent-content/skills/review"])
    """Explicit workstation directory or Git subdirectory containing SKILL.md."""


class AgentArtifactSpec(_ArtifactSpec):
    """An agent persona with a name, description, and instruction body."""

    type: Literal["agent"] = "agent"
    """Artifact type."""

    source: NonBlankStr = Field(examples=["file::~/agent-content/reviewer.md"])
    """Workstation or Git Markdown definition with name and description frontmatter."""


type ArtifactSpec = Annotated[
    HintArtifactSpec | RuleArtifactSpec | SkillArtifactSpec | AgentArtifactSpec,
    Field(discriminator="type"),
]


class ArtifactsConfig(AgwModel):
    """Bundle selection at one owning scope."""

    bundles: Annotated[
        list[Annotated[str, ResourceRef(kind="artifact-bundle", usage="an artifact bundle")]],
        MergeStrategy.REPLACE,
    ] = Field(default_factory=list)
    """Ordered bundle references. An authored list replaces inherited references;
    an empty list suppresses this owner's inherited selection."""

    @field_validator("bundles")
    @classmethod
    def _unique_bundles(cls, bundles: list[str]) -> list[str]:
        """Reject repeated references in an operator-authored selection."""
        if len(set(bundles)) != len(bundles):
            raise ValueError("artifact bundle references must be unique")
        if any(not name.strip() for name in bundles):
            raise ValueError("artifact bundle references must not be blank")
        return bundles


def artifact_references(
    config: ArtifactsConfig,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
) -> tuple[ResourceReference, ...]:
    """Project effective bundle references with their declaring layer."""
    from agentworks.resources.reference import ResourceReference
    from agentworks.value_provenance import longest_prefix_value

    refs: list[ResourceReference] = []
    for index, name in enumerate(config.bundles):
        layers = longest_prefix_value(provenance, ("artifacts", "bundles", index)) or ()
        declared_by = (layers[-1].resource_kind, layers[-1].name) if layers else None
        refs.append(
            ResourceReference(
                kind="artifact-bundle",
                name=name,
                usage="an artifact bundle",
                source=source,
                declared_by=declared_by,
            )
        )
    return tuple(refs)
