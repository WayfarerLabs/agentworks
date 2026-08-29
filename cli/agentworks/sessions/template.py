"""``SessionTemplate`` and ``NamedConsoleConfig``: the operator-declared
session/console template rows.

Moved out of ``agentworks.config`` so the ``sessions`` domain owns its
declared-resource types next to the resolver
(``agentworks.sessions.templates``) and the kinds
(``agentworks.sessions.kinds``). ``NamedConsoleConfig`` imports its
layout default from ``agentworks.sessions.layouts``. The
``agentworks.config`` package keeps only the legacy TOML loaders that
construct these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import EnvTable, env_references
from agentworks.schema import CapabilityBlock, RefOwner, ResourceRef
from agentworks.schema.reference import RefRelationship
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, TmuxLayout

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.inheritance import LayerSource
    from agentworks.resources.reference import ResourceReference
    from agentworks.sessions.templates import EffectiveSessionTemplate, ResolvedSessionTemplate
    from agentworks.source_location import SourceLocation
    from agentworks.value_provenance import ProvenancePath


def effective_references(
    effective: ResolvedSessionTemplate | EffectiveSessionTemplate,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
) -> tuple[ResourceReference, ...]:
    """References required by one effective session declaration."""
    from agentworks.capabilities.config import capability_config_references
    from agentworks.resources.reference import ResourceReference as _ResourceRef
    from agentworks.resources.reference import sourced_references
    from agentworks.sessions.templates import EffectiveSessionTemplate
    from agentworks.value_provenance import longest_prefix_value

    def owner(path: ProvenancePath) -> tuple[str, str] | None:
        sources = longest_prefix_value(provenance, path) or ()
        return None if not sources else (sources[-1].resource_kind, sources[-1].name)

    if isinstance(effective, EffectiveSessionTemplate):
        resolved = effective.resolved
        integration = effective.harness.name
        integration_config = effective.harness.config
    else:
        resolved = effective
        integration = effective.harness_integration
        integration_config = effective.harness_integration_config

    by_env = {key: declared_by for key in resolved.env if (declared_by := owner(("env", key))) is not None}
    refs: list[ResourceReference] = list(env_references(resolved.env, source, by_env))
    if integration is None:
        return tuple(refs)
    selector_owner = owner(("harness_integration",))
    refs.append(
        _ResourceRef(
            name=integration,
            kind="harness-integration",
            usage="the session harness integration",
            source=source,
            declared_by=selector_owner,
        )
    )
    refs.extend(
        sourced_references(
            capability_config_references(
                kind="harness-integration",
                config={"name": integration, **integration_config},
                owner=RefOwner(kind=source[0], name=source[1]),
            ),
            source,
            selector_owner,
        )
    )
    return tuple(refs)


def _capability_provenance(
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
) -> dict[ProvenancePath, RefOwner]:
    """Project session-layer paths into the harness config's local model."""
    from agentworks.value_provenance import longest_prefix_value

    local: dict[ProvenancePath, RefOwner] = {}
    selector = longest_prefix_value(provenance, ("harness_integration",)) or ()
    if selector:
        source = selector[-1]
        local[("name",)] = RefOwner(kind=source.resource_kind, name=source.name)
    prefix = ("harness_integration_config",)
    for path, sources in provenance.items():
        if sources and path[:1] == prefix:
            source = sources[-1]
            local[path[1:]] = RefOwner(kind=source.resource_kind, name=source.name)
    return local


def validate_effective_harness(
    effective: ResolvedSessionTemplate | EffectiveSessionTemplate,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
    *,
    location: SourceLocation | None = None,
) -> None:
    """Validate one merged harness block with its projected ownership."""
    from agentworks.capabilities.config import validate_capability_config
    from agentworks.sessions.templates import EffectiveSessionTemplate

    if isinstance(effective, EffectiveSessionTemplate):
        name = effective.harness.name
        config = effective.harness.config
    else:
        name = effective.harness_integration
        config = effective.harness_integration_config
    if name is None:
        from agentworks.sessions.templates import DEFAULT_HARNESS_INTEGRATION

        name = DEFAULT_HARNESS_INTEGRATION

    validate_capability_config(
        kind="harness-integration",
        config={"name": name, **config},
        owner=RefOwner(kind=source[0], name=source[1]),
        location=location,
        provenance=_capability_provenance(provenance),
    )


class NamedConsoleConfig(DeclaredResource):
    """Settings for the `console` subcommand group (named multi-session
    consoles). Only named consoles read these values.

    Inheriting ``DeclaredResource`` gives this the uniform metadata every
    declared resource carries, including ``name``. The console surface is a
    singleton today, so its two construction sites pass ``name="default"``;
    this is metadata uniformity, not a per-console template selector.
    """

    tmux_layout: TmuxLayout = AW_SESSION_VERTICAL_LAYOUT
    """How panes are arranged in a named console's session window. Every
    value but ``aw-session-vertical`` is a tmux built-in an operator can
    also apply on the fly with ``tmux select-layout``."""


class SessionTemplate(DeclaredResource):
    """Session template definition. Every field is optional and ``None``
    means "not declared here", never "off".

    ``harness_integration.name`` selects the workload capability and its
    remaining keys configure it. ``None`` inherits and ultimately defaults
    to the ``shell`` integration.
    """

    inherits: list[
        Annotated[
            str,
            ResourceRef(
                kind="session-template",
                usage="a parent template",
                relationship=RefRelationship.INHERITS,
            ),
        ]
    ] = Field(default_factory=list)
    """Parent templates this one composes, nearest last."""

    harness_integration: CapabilityBlock | None = None
    """The workload this session runs: one table whose ``name`` selects the
    harness integration and whose remaining keys are that integration's own
    config (``{name: shell, command: htop}``)."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported in this session, as a plaintext value
    or a ``{secret: <name>}`` reference per key. Merged
    child-overrides-parent at resolution; an empty table adds nothing."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """The ``inherits`` edges as declared, plus the runtime needs of
        the EFFECTIVE declaration (FR17; see ``VMTemplate.dependencies``
        for the rule the four inheriting kinds share).

        The harness pair is read off the merged lineage rather than off
        this row, which is what makes a child that overrides only one key
        of an inherited config still depend on the whole merged blob's
        secrets, and a child that overrides the secret NAME depend on its
        override alone.

        The pair's edges are attributed to the layer that SELECTED the
        integration, which is the granularity the merge has: switching
        integration discards the accumulated config, so the block belongs
        to whoever named it. That is exact for every shape a template can
        write today, because a child that wants to change one key has to
        restate the selector beside it (a config block without one does
        not load), which makes the child the selecting layer. A future
        per-key attribution would need ``extract_references`` to carry the
        field its edge came from; no shipped integration marks a reference
        field, so no live edge turns on it.
        """
        from agentworks.resources.reference import (
            inherits_reference,
        )
        from agentworks.sessions.templates import effective_template_with_provenance

        source = ("session-template", self.name)
        rows = {**context.rows_of("session-template"), self.name: self}
        layered = effective_template_with_provenance(rows, self.name)
        refs = list(effective_references(layered.value, source, layered.provenance))
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        return refs

    def validate_config(self, context: FinalizeContext) -> None:
        """Throwing shape check for the EFFECTIVE harness config block,
        run by the finalize validate pass. Mirrors ``dependencies``:
        the CORE validates the blob against the named integration's declared
        model, and no integration code runs. An unknown integration name is
        a no-op here (the miss policy already reported it).

        The MERGED blob is what validates, never this row's declared one
        (FR12): a child's declaration is legitimately partial until the
        chain completes it, so a model's required field would wrongly
        reject a child that its parent completes. The merge's per-key
        provenance rides along so an error on an inherited key names the
        template that declared it.

        Unlike ``dependencies``, this uses the ``shell`` DEFAULT when the
        lineage names no integration, and the asymmetry is deliberate: an
        edge records what the operator named, while validation checks what
        the session will actually run.
        """
        from agentworks.sessions.templates import effective_template_with_provenance

        layered = effective_template_with_provenance(
            {**context.rows_of("session-template"), self.name: self},
            self.name,
        )
        effective = layered.value
        validate_effective_harness(
            effective,
            ("session-template", self.name),
            layered.provenance,
            location=self.error_location,
        )
