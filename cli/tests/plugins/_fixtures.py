"""Test-local fixture plugin and capability impls for the plugin framework.

``register_plugin`` checks contract conformance before it seats anything, so
a fixture impl has to be a REAL implementation of its kind's contract:
derived from the kind's base (or, for ``secret-backend``, structurally
satisfying the ``SecretBackend`` Protocol), concrete, and declaring a
supported ``contract_version``. That is the point of the check, and a
fixture that dodged it would be proving the framework against something the
framework would never accept.

The ``Conforming*`` bases below are the minimal way to satisfy it: every
required operation implemented, each raising ``NotImplementedError``,
because the framework tests seat and publish impls, never run them. Fixtures
in the other plugin test modules derive from these and add only what their
test actually needs (a permissive ``validate``, a host-support verdict, a
throwing constructor). The four ``Fixture*`` impls here are the ready-made
one-of-each set ``fixture_plugin`` seats.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, create_model

from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.git_credential.base import GitCredentialProvider, HelperEntry
from agentworks.capabilities.harness_integration.base import HarnessIntegration
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.plugins import Plugin
from agentworks.resources.graph import Readiness

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow, VMStatus
    from agentworks.resources.reference import ConfigReference
    from agentworks.secrets.base import MappingValue, SecretDecl
    from agentworks.transports import Transport


def config_model_for(kind: str, name: str) -> type[BaseModel]:
    """A minimal conforming config model for capability ``name`` of
    ``kind``: no fields but the tag its kind dispatches on.

    Built rather than authored, because a fixture's name is often chosen
    at call time and a ``Literal`` tag has to match it. Authored models
    are always written out as classes (attribute docstrings need source);
    a fixture that documents nothing loses nothing by being generated.
    """
    contract = descriptor_for(kind).config_schema
    if contract.discriminator is None:
        return contract.base
    model: type[BaseModel] = create_model(
        f"{name.replace('-', '_')}_config",
        __base__=contract.base,
        **{contract.discriminator: (Literal[name], ...)},  # type: ignore[call-overload]
    )
    return model


class ConformingVMPlatform(VMPlatform):
    """A concrete ``VMPlatform``: the six abstract power ops implemented so
    the class is seatable. Subclasses add ``name`` / ``description``."""

    contract_version = 1

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        raise NotImplementedError

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        raise NotImplementedError

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        raise NotImplementedError

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        raise NotImplementedError

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        raise NotImplementedError

    def display_backend_name(self, vm: VMRow) -> str:
        raise NotImplementedError


class ConformingHarnessIntegration(HarnessIntegration):
    """A concrete ``HarnessIntegration``. Subclasses add ``name`` /
    ``description``."""

    contract_version = 1

    def start(self, ctx: RunContext) -> str:
        raise NotImplementedError

    def resume(self, ctx: RunContext) -> str:
        raise NotImplementedError

    def _probe_target(self, transport: Transport) -> None:
        raise NotImplementedError


class ConformingGitCredentialProvider(GitCredentialProvider):
    """A concrete ``GitCredentialProvider``. Subclasses add ``name`` /
    ``description``."""

    contract_version = 1

    def _verify_token(self, token: str) -> None:
        raise NotImplementedError

    def helper_entry(self) -> HelperEntry:
        raise NotImplementedError

    def credential_lines(self, token: str) -> list[str]:
        raise NotImplementedError


class ConformingSecretBackend:
    """A structural ``SecretBackend``: a plain class, because the contract is
    a ``Protocol``. It spells ``contract_version`` for the same reason every
    real backend does (Protocol bodies are not inherited)."""

    contract_version = 1
    interactive = False

    def not_ready(self) -> Readiness:
        return Readiness.ready()

    def validate_mapping(self, owner: str, mapping: MappingValue) -> None:
        raise NotImplementedError

    def dependencies(self, mapping: MappingValue) -> tuple[ConfigReference, ...]:
        return ()

    def would_attempt(self, secret: SecretDecl, mapping: MappingValue | None) -> bool:
        return False

    def describe_lookup(self, secret: SecretDecl, mapping: MappingValue | None) -> str | None:
        return None

    def batch_get(self, wants: list[tuple[SecretDecl, MappingValue | None]]) -> dict[str, str]:
        return {}


class FixtureVMPlatform(ConformingVMPlatform):
    name = "fixture-vm"
    description = "Fixture VM platform"
    config_model = config_model_for("vm-platform", "fixture-vm")


class FixtureHarnessIntegration(ConformingHarnessIntegration):
    name = "fixture-harness"
    description = "Fixture harness"
    config_model = config_model_for("harness-integration", "fixture-harness")


class FixtureProvider(ConformingGitCredentialProvider):
    name = "fixture-provider"
    description = "Fixture git credential provider"
    config_model = config_model_for("git-credential-provider", "fixture-provider")


class FixtureBackend(ConformingSecretBackend):
    name = "fixture-backend"
    description = "Fixture secret backend"


def fixture_plugin(name: str = "fixture") -> Plugin:
    """A plugin seating one impl of every capability kind."""
    return Plugin(
        name=name,
        description="a test fixture plugin",
        capabilities={
            "vm-platform": (FixtureVMPlatform,),
            "harness-integration": (FixtureHarnessIntegration,),
            "git-credential-provider": (FixtureProvider,),
            "secret-backend": (FixtureBackend,),
        },
    )


def conforming_impl(kind: str, name: str, description: str = "a conforming fixture impl") -> type[Any]:
    """Build a minimal conforming impl of ``kind`` under ``name``.

    For the many tests whose subject is something other than the impl (a
    name collision, an atomicity guarantee, a publication surface), where
    the impl only has to be one registration would accept.
    """
    base: type[Any] = {
        "vm-platform": ConformingVMPlatform,
        "harness-integration": ConformingHarnessIntegration,
        "git-credential-provider": ConformingGitCredentialProvider,
        "secret-backend": ConformingSecretBackend,
    }[kind]
    members: dict[str, Any] = {"name": name, "description": description}
    if kind != "secret-backend":
        members["config_model"] = config_model_for(kind, name)
    return type(name.replace("-", "_"), (base,), members)
