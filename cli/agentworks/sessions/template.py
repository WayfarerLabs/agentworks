"""``SessionTemplate`` and ``NamedConsoleConfig``: the operator-declared
session/console template dataclasses.

Moved out of ``agentworks.config`` so the ``sessions`` domain owns its
declared-resource types next to the resolver
(``agentworks.sessions.templates``) and the kinds
(``agentworks.sessions.kinds``). ``NamedConsoleConfig`` imports its
layout default from ``agentworks.sessions.layouts``. The
``agentworks.config`` package keeps only the legacy TOML loaders that
construct these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import env_references
from agentworks.schema import RefOwner
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.graph import BuildContext
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True, kw_only=True)
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


@dataclass(frozen=True, kw_only=True)
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

    inherits: list[str] = field(default_factory=list)
    harness_integration: str | None = None
    harness_integration_config: dict[str, object] | None = None
    env: dict[str, EnvEntry] | None = None

    def dependencies(self, context: BuildContext) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceRef,
        )
        from agentworks.resources.reference import (
            TemplateReference,
            sourced_references,
        )

        source = ("session-template", self.name)
        refs: list[ResourceReference] = list(env_references(self.env, source))
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="session-template",
                    usage="a parent template",
                    source=source,
                )
            )
        if self.harness_integration is not None:
            # The selector edge: a declared harness_integration references the
            # capability row, so a typo is a finalize-time miss-policy
            # error naming this template, and the harness integration row's
            # "Referenced by:" lists its templates (FRD R2).
            refs.append(
                _ResourceRef(
                    name=self.harness_integration,
                    kind="harness-integration",
                    usage="the session harness integration",
                    source=source,
                )
            )
            # Plus whatever the selected harness integration's config block
            # implies, read structurally off its DECLARED model by the core
            # (a future secret-declaring integration gets auto-declaration
            # and reachability for free; every built-in implies nothing).
            # Total and non-throwing; an unknown name contributes nothing
            # and the miss policy reports it.
            from agentworks.capabilities.config import capability_config_references

            refs.extend(
                sourced_references(
                    capability_config_references(
                        kind="harness-integration",
                        name=self.harness_integration,
                        blob=self.harness_integration_config or {},
                        owner=RefOwner(kind="session-template", name=self.name),
                    ),
                    source,
                )
            )
        return refs

    def validate(self, enabled_backends: frozenset[str]) -> None:
        """Throwing shape check for the ``harness_integration_config`` blob, run by
        the finalize ``validate`` pass (``enabled_backends`` is the
        secret-only R9.9 input, ignored here). Mirrors ``dependencies``:
        the CORE validates the blob against the named integration's declared
        model, and no integration code runs. An undeclared harness
        integration (``None``) or an unknown name is a no-op here (the miss
        policy reports the latter).

        This validates the DECLARED blob, which on an inheriting surface
        may legitimately be partial; the merged blob's own check runs at
        resolve (``sessions/templates``). Moving that one here is the
        effective-config work, which is not this step's.
        """
        if self.harness_integration is None:
            return
        from agentworks.capabilities.config import validate_capability_config

        validate_capability_config(
            kind="harness-integration",
            name=self.harness_integration,
            blob=self.harness_integration_config or {},
            owner=RefOwner(kind="session-template", name=self.name),
            location=self.error_location,
        )
