"""Test-local fixture plugin and capability impls for the plugin framework.

``register_plugin`` checks contract conformance before it seats anything, so
a fixture impl has to be a REAL implementation of its kind's contract:
derived from the kind's nominal base, concrete, and declaring a
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

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, create_model

from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.git_credential.base import (
    CredentialPayload,
    GitCredentialProvider,
    HttpsCredentialScope,
)
from agentworks.capabilities.harness_integration.base import HarnessIntegration
from agentworks.capabilities.secret_backend import (
    BackendPreview,
    BackendResolution,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    SecretBackend,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.plugins import Plugin
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow, VMStatus
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


def _declare_fixture_config(cls: type, kind: str) -> None:
    """Give a fixture impl a config model matching its own name.

    Every capability declares one, and a fixture's has to carry ITS tag,
    so it cannot be inherited from a shared base. Done in
    ``__init_subclass__`` rather than spelled per fixture because a
    fixture that names itself has said everything the model needs, and
    thirty restatements would be thirty chances to get the tag wrong. A
    fixture that declares its own ``config_model`` (one with real fields)
    keeps it.
    """
    if "name" in cls.__dict__ and "config_model" not in cls.__dict__:
        cls.config_model = config_model_for(kind, cls.name)  # type: ignore[attr-defined]


class ConformingVMPlatform(VMPlatform):
    """A concrete ``VMPlatform`` with every abstract operation implemented.

    Subclasses add ``name`` / ``description``.
    """

    contract_version = 1

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _declare_fixture_config(cls, "vm-platform")

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

    contract_version = 2

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _declare_fixture_config(cls, "harness-integration")

    def start(self, ctx: RunContext) -> str:
        raise NotImplementedError

    def resume(self, ctx: RunContext) -> str:
        raise NotImplementedError

    def _probe_target(self, transport: Transport) -> None:
        raise NotImplementedError


class ConformingGitCredentialProvider(GitCredentialProvider):
    """A concrete ``GitCredentialProvider``. Subclasses add ``name`` /
    ``description``."""

    contract_version = 3

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _declare_fixture_config(cls, "git-credential-provider")

    def credential_scopes(self) -> tuple[HttpsCredentialScope, ...]:
        return (HttpsCredentialScope("example.test"),)

    def credential_material(self, ctx: RunContext) -> CredentialPayload:
        raise NotImplementedError


class _FixtureSecretClient:
    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        return {}

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        return {}


class ConformingSecretBackend(SecretBackend):
    """A minimal concrete nominal secret backend for registration tests."""

    config_model: ClassVar[type[AgwModel]] = AgwModel
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = AgwRootModel[str]

    contract_version = 1
    supports_tty_interaction = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _declare_fixture_config(cls, "secret-backend")

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        return nullcontext(_FixtureSecretClient())


class FixtureVMPlatform(ConformingVMPlatform):
    name = "fixture-vm"
    description = "Fixture VM platform"


class FixtureHarnessIntegration(ConformingHarnessIntegration):
    name = "fixture-harness"
    description = "Fixture harness"


class FixtureProvider(ConformingGitCredentialProvider):
    name = "fixture-provider"
    description = "Fixture git credential provider"


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
    return type(name.replace("-", "_"), (base,), {"name": name, "description": description})
