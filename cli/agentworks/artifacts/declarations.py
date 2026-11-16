"""Operator-authored artifact entries, direct declarations, and bundle references."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import ConfigDict, Field, ValidationError, ValidatorFunctionWrapHandler, field_validator, model_validator

from agentworks.artifacts.mcp import MCPDefinition, definition_error
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.names import ARTIFACT_NAME_PATTERN, MAX_ARTIFACT_NAME_LENGTH
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
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE

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


class _FileArtifactSpec(_ArtifactSpec):
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


class _TextArtifactSpec(_FileArtifactSpec):
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


class RuleArtifactSpec(_TextArtifactSpec):
    """Standing Markdown guidance with optional description-only YAML frontmatter."""


class SkillArtifactSpec(_FileArtifactSpec):
    """A complete standard Agent Skills directory with SKILL.md at its root."""

    source: NonBlankStr = Field(examples=["file::~/agent-content/skills/review"])
    """Explicit workstation directory or Git subdirectory containing SKILL.md."""


class AgentArtifactSpec(_FileArtifactSpec):
    """An agent persona with a name, description, and instruction body."""

    source: NonBlankStr = Field(examples=["file::~/agent-content/reviewer.md"])
    """Workstation or Git Markdown with name, description and optional native_options frontmatter."""


class MCPServerArtifactSpec(_ArtifactSpec):
    """One canonical MCP connection, inline or selected from a JSON/JSONC file."""

    definition: MCPDefinition | None = None
    source: NonBlankStr | None = None

    @field_validator("definition", mode="wrap")
    @classmethod
    def _safe_definition_errors(cls, value: object, handler: ValidatorFunctionWrapHandler) -> object:
        try:
            return handler(value)
        except ValidationError as error:
            raise ValueError(definition_error(error)) from None

    @model_validator(mode="after")
    def _one_input(self) -> Self:
        if (self.definition is None) == (self.source is None):
            raise ValueError("specify exactly one of definition or source")
        return self


type ArtifactSpec = HintArtifactSpec | RuleArtifactSpec | SkillArtifactSpec | AgentArtifactSpec | MCPServerArtifactSpec

ArtifactName = Annotated[str, Field(pattern=ARTIFACT_NAME_PATTERN, max_length=MAX_ARTIFACT_NAME_LENGTH)]


class ArtifactDefinitions(AgwModel):
    """Artifact type maps merge by key; an overridden entry replaces the complete definition."""

    hints: dict[ArtifactName, HintArtifactSpec] = Field(default_factory=dict)
    """Small setup facts, keyed by canonical artifact name."""

    rules: dict[ArtifactName, RuleArtifactSpec] = Field(default_factory=dict)
    """Unconditional instructions, keyed by canonical artifact name."""

    skills: dict[ArtifactName, SkillArtifactSpec] = Field(default_factory=dict)
    """Complete skill directories; each key must match the SKILL.md name."""

    agents: dict[ArtifactName, AgentArtifactSpec] = Field(default_factory=dict)
    """Agent personas; each key must match the definition's frontmatter name."""

    mcp_servers: dict[ArtifactName, MCPServerArtifactSpec] = Field(default_factory=dict)
    """Canonical MCP server connections, keyed by artifact name."""

    def type_maps(self) -> tuple[tuple[ArtifactType, Mapping[str, ArtifactSpec]], ...]:
        return (
            (ArtifactType.HINT, self.hints),
            (ArtifactType.RULE, self.rules),
            (ArtifactType.SKILL, self.skills),
            (ArtifactType.AGENT, self.agents),
            (ArtifactType.MCP_SERVER, self.mcp_servers),
        )

    def __bool__(self) -> bool:
        return any(entries for _, entries in self.type_maps())


class ArtifactsConfig(AgwModel):
    """Bundle selection and direct artifact definitions at one owning scope."""

    bundles: Annotated[
        list[Annotated[str, ResourceRef(kind="artifact-bundle", usage="an artifact bundle")]],
        MergeStrategy.REPLACE,
    ] = Field(default_factory=list)
    """Ordered bundle references. An authored list replaces inherited references;
    an empty list suppresses this owner's inherited selection."""

    declare: ArtifactDefinitions | None = None
    """Direct definitions applied after selected bundles. Type maps merge by key,
    replacing each overridden entry completely. Omission or an empty object inherits;
    null clears inherited direct definitions without changing bundle selection."""

    def __bool__(self) -> bool:
        return bool(self.bundles or self.declare)

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
