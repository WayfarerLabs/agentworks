"""One effective setup declaration supplies validation, secrets, and execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks.capabilities.config import validate_capability_config
from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled
from agentworks.env.compose import compose_env
from agentworks.env.identity import ResourceContext, agentworks_identity_env
from agentworks.schema import RefOwner

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

    from agentworks.db.instance_state import InstanceKind
    from agentworks.harness_setup.model import SetupComponent, SetupFacet
    from agentworks.resources.registry import Registry
    from agentworks.schema import CapabilityBlock
    from agentworks.secrets.orchestration import SecretTarget
    from agentworks.secrets.resolver import Resolver

_FACET: dict[SetupComponent, SetupFacet] = {"vm": "vm", "admin": "user", "agent": "user", "workspace": "workspace"}


@dataclass(frozen=True)
class SetupInputs:
    """Already resolved attachment order and the owning scope's env dictionaries."""

    kind: InstanceKind
    name: str
    component: SetupComponent
    attachments: tuple[CapabilityBlock, ...]
    target: SecretTarget

    @property
    def facet(self) -> SetupFacet:
        return _FACET[self.component]

    def register(self, resolver: Resolver, registry: Registry) -> None:
        """Join the operation's eager resolution before mutation or log creation."""
        from agentworks.capabilities.harness_integration.attachments import attachment_references, validate_attachments

        source = (self.kind, self.name)
        validate_attachments(self.attachments, facet=self.facet, source=source, provenance={})
        for block in self.attachments:
            ensure_harness_integration_enabled(registry, block.name)
        resolver.register_targets([self.target])
        for reference in attachment_references(self.attachments, facet=self.facet, source=source, provenance={}):
            if reference.kind == "secret":
                resolver.register_name(reference.name)

    def environment(self, values: Mapping[str, str], context: ResourceContext) -> dict[str, str]:
        """Use the same scope dictionaries, protecting every core identity field."""
        merged = compose_env(
            values=values,
            ctx=context,
            vm=self.target.vm,
            admin=self.target.admin,
            agent=self.target.agent,
            workspace=self.target.workspace,
        )
        return {
            **{name: value for name, value in merged.items() if not name.startswith("AGENTWORKS_")},
            **agentworks_identity_env(context),
        }

    def declaration(self, block: CapabilityBlock) -> dict[str, JsonValue]:
        """Capture config, reference names, and freshness hashes without storing env values."""
        model = validate_capability_config(
            kind="harness-integration",
            facet=self.facet,
            config=block.tagged,
            owner=RefOwner(kind=self.kind, name=self.name),
        )
        config = block.tagged if model is None else model.model_dump(mode="json")
        env = {}
        for name, scope in (
            ("vm", self.target.vm),
            ("admin", self.target.admin),
            ("agent", self.target.agent),
            ("workspace", self.target.workspace),
        ):
            if scope is not None:
                env[name] = {
                    key: {"secret": entry.secret}
                    if entry.secret is not None
                    else {"sha256": hashlib.sha256((entry.value or "").encode()).hexdigest()}
                    for key, entry in scope.items()
                }
        return cast("dict[str, JsonValue]", {"config": config, "env": env})
