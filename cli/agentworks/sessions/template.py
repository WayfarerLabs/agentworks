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

from typing import TYPE_CHECKING

from pydantic import Field

from agentworks.declared_resource import DeclaredResource
from agentworks.env import EnvEntry
from agentworks.env.entry import env_references
from agentworks.schema import RefOwner
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


class NamedConsoleConfig(DeclaredResource):
    """Settings for the `console` subcommand group (named multi-session
    consoles). Section is `[named_console]` in the TOML to disambiguate from
    the workspace console template. Only named consoles read these values.

    Inheriting ``DeclaredResource`` gives this the uniform metadata every
    declared resource carries, including ``name``. The console surface is a
    singleton today, so its two construction sites pass ``name="default"``;
    this is metadata uniformity, not a per-console template selector.
    """

    tmux_layout: str = AW_SESSION_VERTICAL_LAYOUT


class SessionTemplate(DeclaredResource):
    """Session template definition. All fields optional (None = inherit/default).

    The workload the session runs is selected by the ``harness_integration`` /
    ``harness_integration_config`` pair (the inline capability reference, ADR
    0016): ``harness_integration`` names the capability and ``harness_integration_config`` is
    the blob whose shape that capability declares and the core validates. ``None`` on either
    means "not declared here" (distinct from a declared-empty blob),
    so inheritance can tell a restating child from a silent one (FRD
    R5). An undeclared harness_integration resolves to the ``shell`` built-in (a
    plain login shell), preserving the behavior from before harness integrations. The
    flat ``command`` / ``resume_command`` / ``required_commands``
    fields are gone: they are ``shell``'s config vocabulary and live
    under ``harness_integration_config`` now; the TOML loader hoists them for
    backward compatibility, manifests reject them (FRD R2/R6).
    """

    inherits: list[str] = Field(default_factory=list)
    harness_integration: str | None = None
    harness_integration_config: dict[str, object] | None = None
    env: dict[str, EnvEntry] | None = None

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
        by_env = declarers(merge_layers(rows, self.name), "session-template", lambda t: t.env or {})
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
                        name=integration,
                        blob=effective.harness.config,
                        owner=RefOwner(kind="session-template", name=self.name),
                    ),
                    source,
                    declared_by,
                )
            )
        return refs

    def validate_config(self, enabled_backends: frozenset[str], context: FinalizeContext) -> None:
        """Throwing shape check for the EFFECTIVE ``harness_integration_config``
        blob, run by the finalize ``validate`` pass (``enabled_backends`` is the
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
        validate_capability_config(
            kind="harness-integration",
            name=effective.harness.name or DEFAULT_HARNESS_INTEGRATION,
            blob=effective.harness.config,
            owner=RefOwner(kind="session-template", name=self.name),
            location=self.error_location,
            provenance=effective.harness.provenance,
        )
