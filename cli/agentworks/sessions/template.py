"""``SessionTemplate`` and ``NamedConsoleConfig``: the operator-declared
session/console template dataclasses.

Moved out of ``agentworks.config`` so the ``sessions`` domain owns its
declared-resource types next to the resolver
(``agentworks.sessions.templates``) and the kinds
(``agentworks.sessions.kinds``). ``NamedConsoleConfig`` imports its
layout default from ``agentworks.sessions.layouts``. ``config.py`` keeps
only the legacy TOML loaders that construct these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import env_references
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True, kw_only=True)
class NamedConsoleConfig(DeclaredResource):
    """Settings for the `console` subcommand group (named multi-session
    consoles). Section is `[named_console]` in the TOML to disambiguate from
    the legacy `vm console` and the workspace console template. Only named
    consoles read these values today.

    Inheriting ``DeclaredResource`` gives this the uniform metadata every
    declared resource carries, including ``name``. The console surface is a
    singleton today, so its two construction sites pass ``name="default"``;
    this is metadata uniformity, not a per-console template selector.
    """

    tmux_layout: str = AW_SESSION_VERTICAL_LAYOUT


@dataclass(frozen=True, kw_only=True)
class SessionTemplate(DeclaredResource):
    """Session template definition. All fields optional (None = inherit/default).

    The workload the session runs is selected by the ``harness`` /
    ``harness_config`` pair (the inline capability reference, ADR
    0016): ``harness`` names the capability and ``harness_config`` is
    the blob that capability owns and validates. ``None`` on either
    means "not declared here" (distinct from a declared-empty blob),
    so inheritance can tell a restating child from a silent one (FRD
    R5). An undeclared harness resolves to the ``shell`` built-in (a
    plain login shell), preserving the pre-harness behavior. The legacy
    flat ``command`` / ``restart_command`` / ``required_commands``
    fields are gone: they are ``shell``'s config vocabulary and live
    under ``harness_config`` now; the TOML loader hoists them for
    backward compatibility, manifests reject them (FRD R2/R6).
    """

    inherits: list[str] = field(default_factory=list)
    harness: str | None = None
    harness_config: dict[str, object] | None = None
    env: dict[str, EnvEntry] | None = None

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceRef,
        )
        from agentworks.resources.reference import (
            SecretReference,
            TemplateReference,
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
        if self.harness is not None:
            # The selector edge: a declared harness references the
            # capability row, so a typo is a finalize-time miss-policy
            # error naming this template, and the harness row's
            # "Referenced by:" lists its templates (FRD R2).
            refs.append(
                _ResourceRef(
                    name=self.harness,
                    kind="harness",
                    usage="the session harness",
                    source=source,
                )
            )
            # Plus whatever the selected harness's config block implies
            # (a future secret-declaring harness gets auto-declaration
            # and reachability for free; both built-ins imply nothing).
            # Unknown names skip: the miss policy reports them.
            from agentworks.capabilities.harness import HARNESS_REGISTRY

            capability = HARNESS_REGISTRY.get(self.harness)
            if capability is not None:
                for cref in capability.validate_config(
                    f"session-template/{self.name}", self.harness_config or {}
                ):
                    ref_cls = (
                        SecretReference if cref.kind == "secret" else _ResourceRef
                    )
                    refs.append(
                        ref_cls(
                            name=cref.name,
                            kind=cref.kind,
                            usage=cref.usage,
                            source=source,
                        )
                    )
        return refs
