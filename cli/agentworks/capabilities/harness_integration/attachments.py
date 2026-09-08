"""Effective setup attachment validation and dependency projection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.errors import ConfigError
from agentworks.resources.reference import ResourceReference, sourced_references
from agentworks.schema import RefOwner
from agentworks.schema.errors import located
from agentworks.value_provenance import longest_prefix_value

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.capabilities.descriptor import Facet
    from agentworks.resources.inheritance import LayerSource
    from agentworks.schema import CapabilityBlock
    from agentworks.source_location import SourceLocation
    from agentworks.value_provenance import ProvenancePath


def attachment_references(
    attachments: Sequence[CapabilityBlock],
    *,
    facet: Facet,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
) -> tuple[ResourceReference, ...]:
    """Project the selected capabilities and config references without validating.

    Lists replace as a whole, so each block and its config share the source
    of that list declaration. The index resolves its provenance without
    losing the order in which attachments are declared.
    """
    refs: list[ResourceReference] = []
    for index, block in enumerate(attachments):
        layers = longest_prefix_value(provenance, ("harness_integrations", index)) or ()
        declared_by = (layers[-1].resource_kind, layers[-1].name) if layers else None
        refs.append(
            ResourceReference(
                kind="harness-integration",
                name=block.name,
                usage=f"the {facet} harness integration",
                source=source,
                declared_by=declared_by,
            )
        )
        refs.extend(
            sourced_references(
                capability_config_references(
                    kind="harness-integration",
                    facet=facet,
                    config=block.tagged,
                    owner=RefOwner(kind=source[0], name=source[1]),
                ),
                source,
                declared_by,
            )
        )
    return tuple(refs)


def validate_attachments(
    attachments: Sequence[CapabilityBlock],
    *,
    facet: Facet,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
    location: SourceLocation | None = None,
) -> None:
    """Validate an effective operator-authored attachment list at finalization.

    Config errors retain the block index and the declaring resource. Unknown
    implementations remain the graph's miss-policy responsibility.
    """
    seen: set[str] = set()
    for index, block in enumerate(attachments):
        prefix = ("harness_integrations", index)
        owner = _owner(source, index)
        local: dict[ProvenancePath, RefOwner] = {}
        layers = longest_prefix_value(provenance, prefix) or ()
        if layers:
            last = layers[-1]
            local[()] = _owner((last.resource_kind, last.name), index)
        if block.name in seen:
            declaring_owner = local.get((), owner)
            raise ConfigError(located(location, f"{declaring_owner.display}: duplicate integration {block.name!r}"))
        seen.add(block.name)
        validate_capability_config(
            kind="harness-integration",
            facet=facet,
            config=block.tagged,
            owner=owner,
            provenance=local,
            location=location,
        )


def _owner(source: tuple[str, str], index: int) -> RefOwner:
    return RefOwner(kind=source[0], name=source[1], label=f"{source[0]}/{source[1]}.harness_integrations[{index}]")
