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
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


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

    The workload the session runs is selected by one tagged
    ``harness_integration`` table: its ``name`` names the capability and
    its remaining keys are the config whose shape that capability declares
    and the core validates. ``None`` means "not declared here", distinct
    from a table with no config keys, so inheritance can tell a restating
    child from a silent one (FRD R5). An undeclared harness_integration
    resolves to the ``shell`` built-in (a plain login shell), preserving
    the behavior from before harness integrations. The flat ``command`` /
    ``resume_command`` / ``required_commands`` fields are ``shell``'s
    config vocabulary and live inside the table (FRD R2/R6).
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
    child-overrides-parent at resolution; an empty table adds nothing, so
    there is no separate "unset" to distinguish (this matches the other
    three template kinds, whose ``env`` has always defaulted empty)."""

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
        from agentworks.resources.inheritance import declarers, merge_layers
        from agentworks.resources.reference import (
            ResourceReference as _ResourceRef,
        )
        from agentworks.resources.reference import (
            inherits_reference,
            sourced_references,
        )
        from agentworks.sessions.templates import effective_template

        source = ("session-template", self.name)
        rows = {**context.rows_of("session-template"), self.name: self}
        effective = effective_template(rows, self.name)
        integration = effective.harness.name
        declared_by = effective.harness.declared_by
        by_env = declarers(merge_layers(rows, self.name), "session-template", lambda t: t.env)
        refs: list[ResourceReference] = list(env_references(effective.resolved.env, source, by_env))
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        if integration is not None:
            # The selector edge: a declared harness_integration references the
            # capability row, so a typo is a finalize-time miss-policy
            # error naming the template that wrote the name, and the harness
            # integration row's "Referenced by:" lists its templates (FRD R2).
            refs.append(
                _ResourceRef(
                    name=integration,
                    kind="harness-integration",
                    usage="the session harness integration",
                    source=source,
                    declared_by=declared_by,
                )
            )
            # Plus whatever the selected harness integration's config block
            # implies, read structurally off its declared model by the core
            # (a future secret-declaring integration gets auto-declaration
            # and reachability for free; every built-in implies nothing).
            # Total and non-throwing; an unknown name contributes nothing
            # and the miss policy reports it.
            from agentworks.capabilities.config import capability_config_references

            refs.extend(
                sourced_references(
                    capability_config_references(
                        kind="harness-integration",
                        config={"name": integration, **effective.harness.config},
                        owner=RefOwner(kind="session-template", name=self.name),
                    ),
                    source,
                    declared_by,
                )
            )
        return refs

    def validate_config(self, enabled_backends: frozenset[str], context: FinalizeContext) -> None:
        """Throwing shape check for the EFFECTIVE harness config block,
        run by the finalize validate pass (``enabled_backends`` is the
        secret-only R9.9 input, ignored here). Mirrors ``dependencies``:
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
        from agentworks.capabilities.config import validate_capability_config
        from agentworks.sessions.templates import DEFAULT_HARNESS_INTEGRATION, effective_template

        effective = effective_template({**context.rows_of("session-template"), self.name: self}, self.name)
        name = effective.harness.name or DEFAULT_HARNESS_INTEGRATION
        validate_capability_config(
            kind="harness-integration",
            config={"name": name, **effective.harness.config},
            owner=RefOwner(kind="session-template", name=self.name),
            location=self.error_location,
            provenance=effective.harness.provenance,
        )
