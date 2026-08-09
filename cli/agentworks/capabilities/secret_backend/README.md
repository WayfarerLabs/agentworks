# Secret backend capability contract

<!-- cspell:ignore contextlib nullcontext -->

A secret backend is a nominal, class-registered capability. Implementations subclass
`SecretBackend`; plugins contribute the class itself, and the registry, graph, and published
capability row all preserve that exact class identity. Registration never constructs a backend.

The supported contract version is `2`. An implementation must declare a non-empty, slash-free
`name`, a string `description`, `interactive` as exactly `bool`, and both model surfaces:

- `config_model` is an `AgwModel` describing one source's shared configuration. It includes
  `name: Literal["<backend-name>"]` and may not contain a `SecretRef`, including through nested
  models. A source cannot require a secret in order to become capable of resolving secrets.
- `mapping_model` is an `AgwRootModel[Any]` describing one secret lookup's optional mapping. It has
  no discriminator tag. Its complete annotation tree must be JSON-native: `None`, strings, booleans,
  integers, floats, `Any`, `object`, finite JSON-valued `Literal`s, unions, `Annotated`, lists,
  string-keyed dictionaries, and nested Agentworks models composed from those types. Validators may
  narrow that vocabulary further.

Dates, date and time objects, bytes, byte arrays, sets, frozen sets, tuples, enums, arbitrary
classes, and non-string dictionary keys are not valid mapping input annotations. The framework owns
the exact `False` opt-out value at the mapping host; it is not an arm of a backend's model and is
never passed to backend code.

## Implementation shape

```python
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from agentworks.capabilities.secret_backend import (
    InteractionBroker,
    RemainingTime,
    SecretBackend,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr


class ExampleSourceConfig(AgwModel):
    name: Literal["example"]


class ExampleMapping(AgwRootModel[NonEmptyStr]):
    pass


class ExampleClient:
    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> None:
        return None

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> Mapping[str, str]:
        return {}


class ExampleBackend(SecretBackend):
    contract_version: ClassVar[int] = 2
    name: ClassVar[str] = "example"
    description: ClassVar[str] = "resolves through the example provider"
    interactive: ClassVar[bool] = False
    config_model: ClassVar[type[AgwModel]] = ExampleSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = ExampleMapping

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return mapping_present

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        return mapping.root if isinstance(mapping, ExampleMapping) else None

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        return nullcontext(ExampleClient())
```

All five provider operations have exact registration-checked classmethod signatures.
`backend_readiness`, `would_attempt`, `describe_lookup`, and `external_operation_timeout` match the
ABC's parameter kinds, annotations, and returns. For `create_client`, all four arguments after `cls`
are required and keyword-only. Annotation resolution is anchored to the defining class, so an
inherited conforming method cannot change meaning in a registering subclass. Registration checks the
timeout declaration's shape without invoking it or fabricating config; the resolution core validates
each actual timeout as finite-positive or `None`. `create_client` does no blocking work and is
resource-free. Acquisition belongs to context entry, authenticated read-only setup belongs to
`prepare`, lookups belong to `resolve`, and cleanup belongs to context exit. The context and client
are operation-local and are never cached in the registry.

`backend_readiness` is an offline, config-independent host-support verdict.
`would_attempt(secret_name, mapping_present=...)` is total and performs no I/O. `describe_lookup`
accepts only a validated mapping-model instance or `None` and returns a safe, value-free identifier.
A backend that performs non-human blocking work also overrides `external_operation_timeout(config)`
with its finite positive source-turn timeout and enforces the supplied `remaining_time` at every
cancellable external boundary.

`preflight` and `runup` are final no-ops. Secret resolution occurs before ordinary capability
lifecycle work, and registration rejects direct or inherited overrides of either method.

Provider failures cross the boundary only through `SecretClientFailure` and `SecretClientTimeout`.
`SecretClientFailure` fixes each failure kind to its allowed remediation and accepts no free-form
provider message, stderr, or secret-bearing context.

The declarable source and map-host schema are framework-owned, not backend authoring surfaces.
Capability implementations target this version-2 contract without inventing a parallel source API.
