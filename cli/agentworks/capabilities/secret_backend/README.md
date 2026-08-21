# Secret backend capability contract

<!-- cspell:ignore contextlib nullcontext -->

A secret backend is provider code behind one or more configured `secret-source` resources. Backends
are nominal, class-registered capabilities: implementations subclass `SecretBackend`, plugins
contribute the class itself, and registration never constructs one.

This is the permanent authoring contract. The supported secret-backend contract version is exactly
`1`; there is no adapter for an older shape.

## Required class surface

Every implementation declares:

- a non-empty, slash-free `name` and a string `description`;
- `contract_version = 1`;
- `supports_tty_interaction` as an exact `bool`;
- `config_model`, an `AgwModel` for one source's shared configuration; and
- `mapping_model`, an `AgwRootModel` for one secret's optional lookup address.

`supports_tty_interaction` means only that the backend can consume terminal input through the
framework's broker. It does not classify biometric, app, browser, device, or other out-of-band
operator work. Declare `True` only when the backend can actually request terminal input. The shipped
prompt backend is `True`; env-var and OnePassword are `False`.

The config model includes `name: Literal["<backend-name>"]`. It cannot contain a `SecretRef`, even
through a nested model, because a source cannot need a secret in order to become capable of
resolving secrets.

The mapping model has no discriminator tag. Its complete annotation tree must be JSON-native:
`None`, strings, booleans, integers, finite floats, `Any`, `object`, JSON-valued `Literal`s, unions,
`Annotated`, lists, string-keyed dictionaries, and nested Agentworks models composed from those
types. Dates, bytes, sets, tuples, enums, arbitrary classes, and non-string dictionary keys are not
valid mapping input annotations. Validators may narrow the accepted vocabulary further.

The framework owns exact `False` as a per-source opt-out. It is never passed to the mapping model or
backend code.

## Static lookup description

`describe_lookup(secret_name, mapping)` is a total, no-I/O class method. It receives a validated
mapping-model instance or `None` and returns `LookupDescription`:

- `LookupDisposition.CANDIDATE` means this source has an applicable lookup. `identifier` is a safe,
  value-free provider address when one exists, or `None` for a lookup such as prompt.
- `LookupDisposition.NOT_APPLICABLE` means there is no lookup. Its identifier must be `None`.

The description never claims that a value exists. Core uses it for dependency edges, static list and
JSON compatibility projections, and runtime request construction. It contains no provider I/O and no
secret value.

## Operation intent and terminal access

Core constructs each client with one tagged intent:

- `PreviewIntent(impact)` requests a value-free answer. `OperatorImpact.NONE` prohibits operator
  action; `OperatorImpact.ALLOW` permits it and is the maximum impact.
- `ResolutionIntent()` requests the actual value. Actual resolution has no `OperatorImpact` input.

Preview impact is a ceiling, not a shortcut. A backend must go as far as it can within the ceiling.
It returns `PreviewIndeterminate(OPERATOR_IMPACT_LIMITED)` only when broader operator impact could
improve the answer after all work permitted at the current impact has been exhausted. Under
`OperatorImpact.ALLOW`, indeterminate is illegal and core converts it to a backend-protocol failure.

Terminal input is a separate fact:

- `TtyInteractionAccess.AVAILABLE`: usable terminal input exists and policy permits it;
- `TtyInteractionAccess.UNAVAILABLE`: policy permits it, but usable terminal input does not exist;
- `TtyInteractionAccess.DISABLED`: global policy forbids terminal input, even if a TTY exists.

Global `--non-interactive` creates `DISABLED`. That flag means only "do not use the TTY for
interactions, even if one is present." It does not suppress app authentication, biometrics,
browsers, device approval, or other out-of-band provider work, and it does not change color or
presentation.

Core supplies an `InteractionBroker` only when all of these are true:

1. `supports_tty_interaction` is `True`;
2. TTY access is `AVAILABLE`; and
3. the operation is actual resolution, or it is preview at `OperatorImpact.ALLOW`.

The broker is `None` in every other case. A backend that declares no TTY support receives no broker,
must not inspect stdin, and cannot return a TTY block. A TTY-capable backend checks access before a
broker read. `UNAVAILABLE` produces `TTY_UNAVAILABLE`; `DISABLED` produces
`TTY_INTERACTION_DISABLED`.

## Closed results and reason ownership

`preview(requests)` returns one exact map whose values are:

- `PreviewAvailable()`: a valid value was proven to exist;
- `PreviewMissing()`: a valid lookup proved ordinary absence;
- `PreviewIndeterminate(OPERATOR_IMPACT_LIMITED)`: broader impact could improve the answer;
- `PreviewBlocked(TTY_UNAVAILABLE | TTY_INTERACTION_DISABLED)`: a TTY fact prevented this backend
  from executing; or
- `PreviewFailed(reason)`: the configured lookup or permitted provider work failed.

`resolve(requests)` returns one exact map whose values are:

- `BackendResolved(value)`: the only value-bearing result;
- `BackendMissing()`: a valid lookup proved ordinary absence;
- `BackendBlocked(TTY_UNAVAILABLE | TTY_INTERACTION_DISABLED)`: a TTY fact prevented execution; or
- `BackendFailed(reason)`: the lookup or provider operation failed.

Backend-returnable failure reasons are:

- `INVALID_MAPPING`
- `LOOKUP_REJECTED`
- `AUTHENTICATION`
- `CONNECTIVITY`
- `DEADLINE_EXCEEDED`
- `EXTERNAL`
- `MALFORMED_VALUE`

`BACKEND_PROTOCOL` and `UNEXPECTED` are core-owned failure reasons. `SOURCE_NOT_READY`,
`BACKEND_PLUGIN_DISABLED`, `NO_ACTIVE_SOURCE`, and `NO_ATTEMPTABLE_SOURCE` are core-owned block
reasons. Aggregate preview also has a dedicated no-candidate result when no runtime lookup ran; it
is not synthetic missing.

A backend returns missing only with provider-specific evidence of normal absence. Invalid mapping,
invalid reference, ambiguous rejection, authentication trouble, timeout, connectivity trouble, and
unknown provider failure are failed, never missing. Provider-native text is not a result field and
must not cross the boundary.

## Fixed core flow

Core validates the complete returned map before copying any resolved value. Keys must exactly equal
the requested names, and every value must be an allowed exact variant with a legal reason.

For each secret, source precedence is fixed:

- available/resolved completes the secret;
- missing falls through to the next candidate;
- blocked falls through while retaining evidence for exhaustion;
- failed hard-stops that secret and prevents a lower-precedence lookup; and
- indeterminate falls through only in preview, retaining ordered evidence.

When preview exhausts the chain, the first indeterminate outranks the first TTY block, which
outranks the first ordinary missing result, which outranks the first core source block. A later
available or failed result is the aggregate and keeps the earlier attempts as evidence. When actual
resolution exhausts the chain, a TTY block outranks ordinary missing, which outranks a core source
block. Core performs one source-first pass and does not reuse preview as resolution.

## Lifecycle and exception boundary

`create_client(...)` returns an unentered, resource-free context manager. Factory construction and
context entry perform no provider operation, authentication, browser launch, biometric request,
broker call, or stdin read. The selected `preview` or `resolve` method performs the authorized work.
Context exit always handles cleanup. A primary exception is never suppressed or masked by cleanup.
With no primary exception, user abort, cancellation, and other protected cleanup exits propagate; an
ordinary cleanup failure emits fixed source-only warning text and returns normally.

The client is operation-local and is never cached in a registry. Provider deadlines are
backend-owned source configuration, not part of the generic factory contract. A backend that
supports a deadline validates it in its source model and applies one shrinking deadline across the
complete source turn. The 1Password backend follows this rule for every provider read in the batch.
It returns `DEADLINE_EXCEEDED` rather than exposing a provider timeout exception.

Core keeps the configured source name for its own ordered evidence and cleanup diagnostics. Source
identity does not cross the backend factory boundary.

User abort and cancellation propagate. Ordinary exceptions from the selected client `preview` or
`resolve` method are normalized by core to `UNEXPECTED`; ordinary `describe_lookup` exceptions are
normalized to `BACKEND_PROTOCOL` at runtime and a core-owned configuration failure during
finalization. Malformed maps, illegal variants, illegal reasons, unsafe identities, and maximum-
impact indeterminate also become `BACKEND_PROTOCOL`. Backends do not supply remediation or free-form
diagnostic text. Core derives exception classes and operator guidance from the closed result and
backend identity.

`backend_readiness()` remains an offline, config-independent host-support verdict. `preflight` and
`runup` are final no-ops because secret resolution precedes ordinary capability lifecycle work.

## Value containment

Preview result types cannot carry a value. If preview must fetch a value to prove existence, it
validates and discards that value inside the backend before returning. It must never send plaintext
to core as a probe.

`BackendResolved` is the only value-bearing boundary type and has a redacted representation. NUL is
invalid. Core keeps values in a private batch separate from value-free outcomes. Values must not
appear in identifiers, provider text, exceptions, logs, warnings, cleanup diagnostics, previews, or
representations.

## Complete implementation example

```python
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from agentworks.capabilities.secret_backend import (
    BackendFailed,
    BackendMissing,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    FailureReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    PreviewAvailable,
    PreviewFailed,
    PreviewMissing,
    SecretBackend,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.errors import ConfigError
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr


class ExampleSourceConfig(AgwModel):
    name: Literal["example"]


class ExampleMapping(AgwRootModel[NonEmptyStr]):
    pass


class ExampleClient:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        results: dict[str, BackendPreview] = {}
        for request in requests:
            value = self._values.get(request.name)
            if value is None:
                results[request.name] = PreviewMissing()
            elif "\0" in value:
                results[request.name] = PreviewFailed(FailureReason.MALFORMED_VALUE)
            else:
                results[request.name] = PreviewAvailable()
        return results

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        results: dict[str, BackendResolution] = {}
        for request in requests:
            value = self._values.get(request.name)
            if value is None:
                results[request.name] = BackendMissing()
            elif "\0" in value:
                results[request.name] = BackendFailed(FailureReason.MALFORMED_VALUE)
            else:
                results[request.name] = BackendResolved(value)
        return results


class ExampleBackend(SecretBackend):
    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "example"
    description: ClassVar[str] = "resolves through the example provider"
    supports_tty_interaction: ClassVar[bool] = False
    config_model: ClassVar[type[AgwModel]] = ExampleSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = ExampleMapping

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        if mapping is None:
            return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)
        if not isinstance(mapping, ExampleMapping):
            raise ConfigError("example received the wrong mapping model")
        return LookupDescription(LookupDisposition.CANDIDATE, mapping.root)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        if not isinstance(config, ExampleSourceConfig):
            raise ConfigError("example received the wrong source config model")
        # A real backend stores the inputs its methods need. This non-TTY
        # example has a local, resource-free provider seam.
        return nullcontext(ExampleClient({}))
```

Registration checks `backend_readiness`, `describe_lookup`, and `create_client` as class methods
with the exact names, parameter kinds, and requiredness above. `create_client` has four required,
keyword-only arguments after `cls`. Registration also checks the nominal base, exact version,
constructibility, exact boolean TTY capability, and both model surfaces. Runtime validation owns
returned map shapes and values.

`SecretBackend` declares the `contract_version` type but supplies no value. Every concrete backend
declares exact value `1`; registration rejects an implementation that omits or changes it.

The declarable source resource and mapping host are framework-owned. Implementations target this
single version-1 contract and do not invent a parallel source, certainty, remediation, or preview
API.
