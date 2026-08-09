# LLD: Secret Resolution Lifecycle

<!-- cspell:ignore abstractmethod classmethod contextmanager globalns isatty -->
<!-- cspell:ignore localns nonblocking staticmethod -->
<!-- cspell:ignore repr soft-missed traceback unrequested -->

- Status: Reviewed and implemented; operator contract correction in progress
- Scope: Phase 2 resolution lifecycle and typed runtime contract, paired with the reviewed
  [source contract LLD](./source-contract-lld.md) and consumed by implementation Phases 3, 5, and 7
- Governing artifacts: [FRD](./frd.md), [HLA](./hla.md),
  [migration strategy](./migration-strategy.md), and [prior-art research](./prior-art-research.md)
- Code baseline: detached Phase 2 worktree at `37cb21ff`

## Purpose and ownership boundary

This document fixes the runtime contract before any backend or resolver implementation changes. It
owns request projection, client construction and lifetime, interaction authority, timeout
accounting, result categories, value containment, source-turn control flow, and the temporary
complete-or-raise boundary.

The reviewed source contract LLD remains the sole owner of source identity, source-to-backend
selection, config and mapping models, validation, reference extraction, static schema, graph edges,
readiness folding, direct-backend diagnostics, publication, provenance, relocation, and package
exports. In particular, this LLD does not add another source selector, reinterpret `false`, cache a
validated model on a resource row, or create a backend fallback. Runtime calls the selector and
mapping operations fixed there.

The following are fixed inputs:

- An active chain entry names one `secret-source`, not a backend. A same-name source wins before
  direct-backend remediation is considered.
- Backend registries and graph nodes carry classes. Authenticated clients and resolved values are
  operation-local.
- The core always computes typed per-secret results. Command all-or-nothing and inspection partial
  success are projections of that one result, not alternate resolution loops.
- `env-var` and `prompt` retain their current precedence, default mapping, opt-out, deduplication,
  and first-hit behavior.
- An explicit hard mapping failure halts that secret. A soft miss falls through. A source-level
  batch failure is attributed to every request in that source batch.
- No source client receives a declaration, description, hint, registry, graph, resolver, other
  source's mapping, or previously resolved value.

## Baseline evidence and destinations

| Concern              | Evidence at the baseline                                                                                                                            | Required destination                                                                          |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Runtime entry        | `cli/agentworks/secrets/resolve.py:306-449` combines dedupe, readiness, interaction doom, backend errors, values, warnings, and two caller policies | One typed batch core with narrow command and inspection projections                           |
| Active entry         | `secrets/resolve.py:27-78` stores a constructed backend instance and directly calls `batch_get`                                                     | Frozen `ActiveSource` with declaration, class, validated config, and readiness, but no client |
| Backend contract     | `secrets/backends.py:57-167` gives process-global instances a value-bearing `batch_get`                                                             | Class factory plus source-bound context and client protocols                                  |
| Prompt authority     | `secrets/prompt.py:102-116` reads global interaction state and receives the full `SecretDecl`                                                       | Explicit caller policy and a name-only `InteractionBroker`                                    |
| OnePassword boundary | `plugins/onepassword/backend.py:128-159` has one subprocess seam with no timeout; `:383-445` includes raw stderr in exceptions                      | Remaining-time subprocess enforcement and typed, value-free translation                       |
| Command result       | `secrets/resolver.py:161-180` expects a complete `dict[str, str]`                                                                                   | Temporary `ResolutionBatch.complete_or_raise()` adapter                                       |
| Inspection result    | `env/show.py:372-435` passes an error dictionary and intentionally keeps partial values                                                             | Private inspection projection from the same batch                                             |
| Interaction policy   | `output.py:545-566` owns the global flag and TTY probe; backends call it indirectly                                                                 | Operation boundary derives an immutable policy; backends never inspect global state           |
| Control guard        | `secrets/resolve.py:423-444` rejects NUL, carriage return, and newline before storing a value                                                       | The typed core performs the same guard before `resolved` is possible                          |

## Module placement

Phase 3 creates the provider-facing contracts in `agentworks.capabilities.secret_backend.client` and
adds the factory methods below to `agentworks.capabilities.secret_backend.base.SecretBackend`. Phase
5 puts the consuming-domain types and algorithm in `agentworks.secrets.resolve`.

`ResolutionBatch` and the compatibility projection helpers are internal runtime types. They are not
re-exported from `agentworks.secrets` during Phase 5. The Phase 6 operator-surface LLD owns the
final consumer/export inventory. `ResolutionOutcome` is the shared value-free record that later
operator surfaces may consume, but Phase 6 still decides its public import path.

## Provider-facing client contract

### Immutable lookup projection

The provider-facing request is exactly:

```python
@dataclass(frozen=True, slots=True)
class SecretLookupRequest:
    name: str
    mapping: BaseModel | None
```

`mapping` is either `None` for an absent mapping or the frozen model instance returned by
`validate_capability_mapping` for this request's selected source backend. Framework `False` never
reaches request construction. `AgwModel` and `AgwRootModel` are already frozen, so both layers of
the projection are immutable.

A request has no `source` field. Its source anchor is structural: it is built inside exactly one
`ActiveSource` turn, validated against that source's selected backend, passed only to the client
created for that turn, and discarded before the next turn. Copying source identity onto every
request would add a second fact that could disagree with the source-bound client.

The builder accepts a `SecretDecl` only on the core side and emits this projection. It does not pass
through the declaration or retain it on the request. A backend receives no description, hint,
origin, file location, raw mapping tree, mappings for other sources, or resource authority.

### Interaction broker and metadata isolation

The caller-facing capability is:

```python
class InteractionBroker(Protocol):
    def request_secret(self, name: str, /) -> str: ...
```

The ordinary CLI implementation is caller-owned. It holds a frozen private map from secret name to
`(description, hint)` and the output handler needed to render `output.prompt_secret`. The provider
sees only `request_secret(name)`. Unknown names raise `StateError` without prompting. The broker's
representation reports only the number of registered prompts; it does not render descriptions or
hints.

The source orchestrator passes a non-`None` broker only to the shipped `PromptBackend` factory and
only under `InteractionPolicy.ALLOW`. Every other factory receives `None`, including interactive
OnePassword, whose possible biometric or authentication UI is owned by `op`, not by Agentworks'
secret prompt metadata. If prompt is attemptable under `ALLOW` and the caller supplied no broker,
the core runs the pure doom prediction first, then raises `StateError` before starting a budget or
constructing any prompt client. Broker presence therefore cannot bypass or precede the required
interactive-turn doom check.

Prompt has no source timeout, so human wait has no source budget to pause. There is no timed-broker
wrapper, deadline-pause operation, or other unused abstraction. OnePassword receives no broker;
biometric and reauthentication wait stays inside its `op` subprocess and is charged to that source's
external-operation timeout.

### Remaining time and client protocols

```python
type RemainingTime = Callable[[], float | None]

class SecretSourceClient(Protocol):
    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> None: ...

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> Mapping[str, str]: ...
```

The mapping returned by `resolve` contains only successful values. An absent requested name is a
soft miss. A client must not return an unrequested key. It raises the typed errors below for a hard
mapping or source failure. `prepare` is authenticated, read-only setup amortized across the tuple;
it is called exactly once before `resolve`, even for a one-request batch. Both methods are total on
an empty tuple, but the orchestrator never creates a client for one.

The context is the standard synchronous `AbstractContextManager[SecretSourceClient]`. Its
constructor receives `remaining_time` through the backend factory. `__enter__` and `__exit__` call
it immediately before any non-human blocking external operation. This preserves ordinary context
semantics while giving entry and cleanup the current remainder rather than a stale number captured
at factory time.

Context obligations are exact:

- `__enter__` returns one client bound to the source config given to the factory.
- If `__enter__` partially acquires state and then raises, it releases that partial state itself;
  Python does not call `__exit__` for a failed entry.
- A context should return `None` or `False` from `__exit__`. The core-owned driver below refuses
  suppression even when a faulty context returns a truthy value.
- At zero remaining time, exit cancels in-flight work and releases local state but starts no new
  remote cleanup. A backend whose external API cannot meet that contract cannot implement this
  protocol with that API. A normal local-only exit that starts at zero is required cleanup, not a
  second timeout or cleanup failure.
- The context and client do not outlive their source turn and are never cached.

### Final `SecretBackend` factory signature

The reviewed Phase 1 ABC gains these lifecycle-owned members with no interim public signature:

```python
class SecretBackend(Capability, ABC):
    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float | None:
        return None

    @classmethod
    @abstractmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]: ...
```

`create_client` is a classmethod because the registry payload and `ActiveSource` carry the backend
class, and a second process-global or pre-turn backend instance would recreate the lifetime problem
this migration removes. `source_name` is safe identity for local client state and diagnostics.
`config` is the exact validated source-config model selected for that source, not a raw mapping. The
core checks `isinstance(config, backend_class.config_model)` before starting a budget; a mismatch is
`StateError` and no factory runs.

`external_operation_timeout` is a concrete no-I/O declaration rather than a second abstract factory
operation. Env-var and prompt inherit `None`; OnePassword overrides it and returns its validated
positive `config.timeout`. A backend that performs non-human blocking I/O must override it with a
finite positive value and enforce the supplied remainder at every interruptible external boundary.
Registration requires the one exact abstract `create_client` operation assigned by the Phase 1 LLD
through the focused secret-backend conformance helper below.

`create_client` is resource-free and nonblocking. It may validate no new operator input and may only
construct the context object around the already validated arguments. File descriptors, subprocesses,
sessions, credentials, locks, network calls, authentication, and other acquisition belong to
`__enter__`, `prepare`, or `resolve`. Consequently a context discarded because the budget expired
immediately after factory return owns nothing and needs no cleanup. A factory that performs
acquisition or external work violates the backend contract.

### Focused `create_client` registration conformance

Phase 3 adds `_create_client_conformance_error(impl: type[SecretBackend]) -> str | None` in
`agentworks.capabilities.secret_backend.conformance`. It is a secret-backend contract check, not a
descriptor feature: `CapabilityKindDescriptor` gains no operation-signature field or supporting
types, and every other capability kind's descriptor and conformance behavior remain unchanged.

At the existing required-operation seam in shared `conformance_error`, the secret-backend
descriptor's existing `required_operations` set includes `create_client`, and that generic presence
check runs first. When `descriptor.implementation_contract is SecretBackend`, that seam then calls
the focused helper. This dispatch is by the existing contract-class identity, not by a kind-name
string. It occurs after metadata and abstract constructibility and before either model contract,
preserving the Phase 1 registration order.

The helper uses `inspect.getattr_static(impl, "create_client")`, never a bound attribute. It finds
the defining owner as the first class in `impl.__mro__` whose `__dict__` contains `create_client`
and requires the raw descriptor to be that owner's `classmethod`. Inheriting a concrete conforming
classmethod from the selected MRO owner is legal. The helper inspects `raw.__func__`: its initial
parameter must be named `cls`, have kind `POSITIONAL_OR_KEYWORD`, and have neither a default nor an
annotation. The four remaining parameters must exactly match the final factory signature's order,
names, keyword-only kinds, lack of defaults, resolved annotations, and resolved return. Compatible
but different shapes fail.

Annotation resolution is anchored to the function's definition site, not the registering subclass:

```python
owner = next(cls for cls in impl.__mro__ if "create_client" in cls.__dict__)
raw = inspect.getattr_static(impl, "create_client")
function = raw.__func__
hints = typing.get_type_hints(
    function,
    globalns=function.__globals__,
    localns=dict(vars(owner)),
)
```

Using `raw.__func__.__globals__` resolves imports and aliases from the module that defined an
inherited method. Using the defining MRO owner's namespace resolves its class-local aliases. A
subclass registered from another module must not change either namespace.

A backend that does not override the ABC's abstract `create_client` cannot reach this helper: the
earlier constructibility check returns the existing
`it is abstract (unimplemented operations: create_client)` reason. A concrete class that replaces
the operation with a non-callable value fails the existing generic required-operation check first.
There is therefore no truthful helper-level "missing classmethod" branch. For classes that reach the
helper, its stable rejection order and text are:

1. Wrong raw binding, including an instance method or `staticmethod`:
   `its create_client must be declared as @classmethod`.
2. Wrong binding-parameter name:
   `its create_client first parameter must be named 'cls' (got '<actual>')`.
3. Wrong binding-parameter kind: `its create_client parameter 'cls' must be positional-or-keyword`.
4. A binding-parameter default: `its create_client parameter 'cls' must not have a default`.
5. A binding-parameter annotation: `its create_client parameter 'cls' must not have an annotation`.
6. Unresolvable annotations: `its create_client annotations could not be resolved: <ExceptionType>`.
7. Wrong explicit parameter count:
   `its create_client must declare 4 parameters after cls (got <count>)`.
8. For each position in order, wrong name:
   `its create_client parameter <position> must be named '<expected>' (got '<actual>')`.
9. Wrong kind: `its create_client parameter '<name>' must be keyword-only`.
10. Any default: `its create_client parameter '<name>' must not have a default`.
11. Wrong resolved annotation:
    `its create_client parameter '<name>' must be annotated as <expected> (got <actual>)`.
12. Wrong resolved return:
    `its create_client must return AbstractContextManager[SecretSourceClient] (got <actual>)`.

The helper's formatter owns stable type labels and never includes a callable representation. Tests
cover direct and inherited valid classmethods, including a future-annotated concrete method defined
in one module and inherited by a subclass registered from another. Negative coverage includes the
earlier abstract and non-callable reasons, instance method, staticmethod, extra or missing
parameter, every wrong `cls` and explicit parameter property, unresolved forward reference, and
wrong return.

### Safe provider-to-core failure signals

Backends translate native failures exactly once into these provider-facing types. None accepts a
free-form message, cause string, stderr, secret value, or arbitrary remediation.

```python
class SecretClientFailureKind(StrEnum):
    HARD_MAPPING = "hard-mapping"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    EXTERNAL = "external"

class SecretClientRemediation(StrEnum):
    CHECK_MAPPING = "check-mapping"
    SIGN_IN = "sign-in"
    CHECK_CONNECTIVITY = "check-connectivity"
    RETRY = "retry"

class SecretClientFailure(Exception):
    __slots__ = ("kind", "remediation")

    kind: SecretClientFailureKind
    remediation: SecretClientRemediation

    def __init__(
        self,
        *,
        kind: SecretClientFailureKind,
        remediation: SecretClientRemediation,
    ) -> None:
        expected = {
            SecretClientFailureKind.HARD_MAPPING: SecretClientRemediation.CHECK_MAPPING,
            SecretClientFailureKind.AUTHENTICATION: SecretClientRemediation.SIGN_IN,
            SecretClientFailureKind.CONNECTIVITY: SecretClientRemediation.CHECK_CONNECTIVITY,
            SecretClientFailureKind.EXTERNAL: SecretClientRemediation.RETRY,
        }[kind]
        if remediation is not expected:
            raise ValueError("invalid secret client failure remediation")
        super().__init__()
        self.kind = kind
        self.remediation = remediation

    def __str__(self) -> str:
        return "secret client failure"

    def __repr__(self) -> str:
        return (
            "SecretClientFailure("
            f"kind={self.kind.value!r}, remediation={self.remediation.value!r})"
        )

class SecretClientTimeout(Exception):
    __slots__ = ()

    def __str__(self) -> str:
        return "secret client operation timed out"

    def __repr__(self) -> str:
        return "SecretClientTimeout()"
```

Both exception classes define a constant, value-free `__str__` and redacted `__repr__` and use
`__slots__`. `SecretClientTimeout` means more than a clock comparison: the boundary has stopped,
cancelled, or killed and joined its underlying work before raising. A backend must not raise it
while a worker thread, subprocess, request, or authentication task continues.

Translation does not expose a native exception or result through the provider-facing exception
object. A native exception or result may carry a secret in its message, arguments, captured output,
cause, or context. The boundary records only failure and remediation enums and raises a value-free
provider-facing failure with `from None`:

```python
failure: tuple[SecretClientFailureKind, SecretClientRemediation] | None = None
timed_out = False
value: str | None = None
try:
    native_result = native_call()
except NativeTimeout:
    timed_out = True
except NativeFailure as native_error:
    failure = classify_native(native_error)  # returns enums only
else:
    failure = classify_result(native_result)  # returns enums only
    if failure is None:
        value = extract_success_value(native_result)
    native_result = None

if timed_out:
    raise SecretClientTimeout() from None
if failure is not None:
    kind, remediation = failure
    raise SecretClientFailure(kind=kind, remediation=remediation) from None
assert value is not None
return value
```

The real implementation uses the backend's native exception types. `classify_native` and
`classify_result` are total and return no native object or text. The raised provider failure has
`__cause__ is None` and `__context__ is None`; its message, arguments, and attributes are
value-free. The core's unexpected-exception conversion follows the same exception-object rule.

The workstation process is the trust boundary. Ordinary Python traceback frames, immutable-string
copies, and process memory are explicitly outside the non-disclosure guarantee. Clearing a local
reference is an optional courtesy only when it is a one-line, semantics-free operation. The runtime
MUST NOT add ownership graphs, frame walking, traceback rewriting, broad `BaseException` handling,
or cleanup abstractions to simulate memory erasure. If a future requirement needs strong in-memory
erasure, the sanctioned design is a short-lived isolated process whose address space exits.

A client that resolves more than one request discards partial successful results before returning a
typed failure. Prompt likewise discards answers already collected in that call before propagating
`UserAbort` or other control flow. These are ownership and result-semantics rules, not promises that
the interpreter has erased every in-memory copy.

`UserAbort`, `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` are control flow, not provider
failures. The orchestrator always cleans up and then re-raises them unchanged.

## Consuming-domain types

### `ActiveSource` holds no client

```python
@dataclass(frozen=True, slots=True)
class ActiveSource:
    source: SecretSourceDecl
    backend_class: type[SecretBackend]
    config: AgwModel
    readiness: Readiness

    @property
    def name(self) -> str:
        return self.source.name
```

Construction uses `source_backend_class` from the source contract LLD and the finalized graph's
stored readiness. The config is the validated tagged source config returned by the shared
capability-config bridge. `__post_init__` enforces that `source.backend.name` equals
`backend_class.name` and that `config` is an instance of `backend_class.config_model`. The record
contains no factory result, context, client, broker, budget, request, or value.

The active-chain builder preserves settings order and filters disabled sources. A present but
not-ready source remains in the tuple so resolution can attribute the skip. An unknown source is the
source contract's ordinary or exact direct-backend `ConfigError`, never a `KeyError` or backend
lookup fallback.

### Interaction and completion policy

```python
class InteractionPolicy(StrEnum):
    ALLOW = "allow"
    REFUSE = "refuse"

class CompletionPolicy(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"

@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    interaction: InteractionPolicy
    completion: CompletionPolicy
```

There is no default on `ResolutionPolicy`: every caller states its intent. `COMPLETE` is the
ordinary command path and enables fail-before-interaction doom handling. `PARTIAL` is an explicit
inspection read that retains independently resolved values and does not skip useful interaction
merely because a different secret is already doomed.

The typed entry and active-chain builder are exactly:

```python
def active_sources(config: Config, registry: Registry) -> tuple[ActiveSource, ...]: ...

def resolve_batch(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    policy: ResolutionPolicy,
    interaction_broker: InteractionBroker | None,
) -> ResolutionBatch: ...
```

Both sequences are read-only inputs. `resolve_batch` copies only the deduplicated declaration order
and its private result state. It never mutates a declaration, source, mapping model, broker, or
caller collection.

An ordinary operation boundary derives `ALLOW` only when stdin is a TTY and the global
`--non-interactive` flag is false; global refusal wins. `agw secret verify` uses `REFUSE` unless its
explicit allow flag is present and the global flag is false. These are caller inputs. Neither
`SecretBackend`, a client, nor the typed core reads `sys.stdin`, `output.is_interactive()`, or the
global non-interactive flag.

### Stable outcomes

```python
class ResolutionCategory(StrEnum):
    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"
    REFUSED_INTERACTION = "refused-interaction"
    TIMEOUT = "timeout"
    RESOLUTION_FAILURE = "resolution-failure"

class ResolutionDetail(StrEnum):
    RESOLVED = "resolved"
    NO_ACTIVE_SOURCE = "no-active-source"
    NO_ATTEMPTABLE_SOURCE = "no-attemptable-source"
    SOURCE_NOT_READY = "source-not-ready"
    SOURCE_BACKEND_PLUGIN_DISABLED = "source-backend-plugin-disabled"
    SOFT_MISS = "soft-miss"
    INTERACTION_REFUSED = "interaction-refused"
    BATCH_DOOMED = "batch-doomed-before-interaction"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    HARD_MAPPING = "hard-mapping"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    EXTERNAL = "external"
    MALFORMED_VALUE = "malformed-value"
    BACKEND_PROTOCOL = "backend-protocol"
    UNEXPECTED = "unexpected"

class ResolutionRemediation(StrEnum):
    NONE = "none"
    CONFIGURE_SOURCE = "configure-source"
    ENABLE_SOURCE = "enable-source"
    ENABLE_PLUGIN = "enable-plugin"
    ALLOW_INTERACTION = "allow-interaction"
    RESOLVE_BLOCKING_SECRETS = "resolve-blocking-secrets"
    CHECK_MAPPING = "check-mapping"
    SIGN_IN = "sign-in"
    CHECK_CONNECTIVITY = "check-connectivity"
    INCREASE_TIMEOUT = "increase-timeout"
    REMOVE_CONTROL_CHARACTERS = "remove-control-characters"
    RETRY = "retry"
    REPORT_BACKEND = "report-backend"

@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    name: str
    category: ResolutionCategory
    detail: ResolutionDetail
    remediation: ResolutionRemediation
    source: str | None = None
    identifier: str | None = None
    remediation_target: str | None = None
```

`ResolutionOutcome.__post_init__` uses this exhaustive table as its sole legality map. `Required`
means `source` must be non-`None`; `Forbidden` means it must be `None`. `Allowed` means `identifier`
may be a safe string or `None`; `Forbidden` means it must be `None`. The target column applies the
same presence rule to `remediation_target`.

| Detail                            | Category              | Remediation                 | Source    | Identifier | Target    |
| --------------------------------- | --------------------- | --------------------------- | --------- | ---------- | --------- |
| `resolved`                        | `resolved`            | `none`                      | Required  | Allowed    | Forbidden |
| `no-active-source`                | `unavailable`         | `configure-source`          | Forbidden | Forbidden  | Forbidden |
| `no-attemptable-source`           | `unavailable`         | `configure-source`          | Forbidden | Forbidden  | Forbidden |
| `source-not-ready`                | `unavailable`         | `enable-source`             | Required  | Allowed    | Forbidden |
| `source-backend-plugin-disabled`  | `unavailable`         | `enable-plugin`             | Required  | Allowed    | Required  |
| `soft-miss`                       | `unavailable`         | `configure-source`          | Required  | Allowed    | Forbidden |
| `interaction-refused`             | `refused-interaction` | `allow-interaction`         | Required  | Allowed    | Forbidden |
| `batch-doomed-before-interaction` | `unavailable`         | `resolve-blocking-secrets`  | Forbidden | Forbidden  | Forbidden |
| `deadline-exceeded`               | `timeout`             | `increase-timeout`          | Required  | Allowed    | Forbidden |
| `hard-mapping`                    | `resolution-failure`  | `check-mapping`             | Required  | Allowed    | Forbidden |
| `authentication`                  | `resolution-failure`  | `sign-in`                   | Required  | Allowed    | Forbidden |
| `connectivity`                    | `resolution-failure`  | `check-connectivity`        | Required  | Allowed    | Forbidden |
| `external`                        | `resolution-failure`  | `retry`                     | Required  | Allowed    | Forbidden |
| `malformed-value`                 | `resolution-failure`  | `remove-control-characters` | Required  | Allowed    | Forbidden |
| `backend-protocol`                | `resolution-failure`  | `report-backend`            | Required  | Forbidden  | Forbidden |
| `unexpected`                      | `resolution-failure`  | `report-backend`            | Required  | Allowed    | Forbidden |

There is no fallback row. Adding a detail requires adding its complete tuple here and in the
validator. No outcome has a value, exception, raw stderr, free-form detail, or free-form hint.

`source-backend-plugin-disabled` is the one structured readiness attribution. The active source
derives its target from the disabled `secret-backend` row's `system-plugin` origin, never from the
free-form folded readiness reason. The target follows the plugin registration contract: a non-empty,
slash-free string, including a `str` subclass. Human rendering uses the fixed `enable plugin`
prefix, wraps the target in backticks, and deterministically ASCII-escapes every target character
outside the safe alphanumeric, dot, underscore, and hyphen set. Generic not-ready sources remain
`source-not-ready/enable-source` with no target. Printable or control-bearing readiness reasons are
never copied into outcomes, errors, or renderer inputs.

`identifier` comes only from `backend_class.describe_lookup` for the already validated request.
Before storage, the core rejects any identifier containing a Unicode control or format character. An
unsafe or raising identifier provider produces `resolution-failure/backend-protocol`, discards any
returned value for that request, and stores no identifier. This makes outcomes safe for human and
future JSON rendering without allowing a configured identifier to inject terminal control.

Provider failures map exactly: `HARD_MAPPING` to `hard-mapping/check-mapping`, `AUTHENTICATION` to
`authentication/sign-in`, `CONNECTIVITY` to `connectivity/check-connectivity`, and `EXTERNAL` to
`external/retry`. `SecretClientTimeout` maps to `timeout/deadline-exceeded/increase-timeout`. Core
value validation maps to `resolution-failure/malformed-value/remove-control-characters`; invalid
provider shape maps to `resolution-failure/backend-protocol/report-backend`; every other provider
exception maps to `resolution-failure/unexpected/report-backend`.

### Private value-bearing batch

`ResolutionBatch` is a hand-written slotted class, not a dataclass, Pydantic model, mapping, or
iterable:

```python
class ResolutionBatch:
    __slots__ = ("_outcomes", "_values")

    @property
    def outcomes(self) -> tuple[ResolutionOutcome, ...]: ...

    def complete_or_raise(self) -> dict[str, str]:
        if all(outcome.category is ResolutionCategory.RESOLVED for outcome in self._outcomes):
            return dict(self._values)
        raise _compatibility_error(self._outcomes)

    def __repr__(self) -> str:
        return (
            f"ResolutionBatch(outcomes={len(self._outcomes)}, "
            f"resolved={len(self._values)}, values=<redacted>)"
        )
```

Construction is module-private and enforces unique outcome names, exact request order, a value for
every and only `resolved` outcome, and string keys and values. It copies both inputs. It has no
`__dict__`, serializer, `model_dump`, `asdict`, generic item access, iterator, partial-values
property, or value-bearing equality/repr. `str(batch)` is the redacted `repr`.

`complete_or_raise` returns a fresh dictionary only when every outcome is `resolved`. It leaves the
batch's private values intact on success because the operation-scoped consumer immediately owns the
fresh copy and the batch then falls out of scope.

On any incomplete outcome, the method constructs and raises the existing `SecretUnavailableError`
compatibility type from value-free outcomes, with requested names plus stable category/detail codes
and enum-derived remediation. The error retains no provider exception or value mapping. The private
batch may remain reachable through ordinary process memory or a traceback; that is inside the
trusted workstation process and is not an erasure target. Phase 6 owns the final outcome-to-error
mapping; this compatibility behavior exists only until Phase 7 deletes the dict-returning boundary.

The module-private inspection projection is the only partial-value escape hatch. It receives a
batch, copies resolved values into the existing explicit reveal path, and converts non-resolved
outcomes to value-free per-secret inspection records. It is not a method and is not exported. That
preserves today's deliberate `env show --resolve` value reveal while preventing a generic caller or
renderer from receiving `ResolutionBatch`. Outside that explicit reveal operation, renderers receive
only outcomes. Diagnostic renderers, errors, logs, warnings, and object representations never
receive values.

## Monotonic budget and external-boundary enforcement

### Budget ownership

The consuming-domain core owns one private `_MonotonicBudget` per attempted source turn. It calls
`time.monotonic()` once immediately before `create_client` and computes:

```python
class _MonotonicBudget:
    __slots__ = ("_clock", "_deadline")

    @classmethod
    def start(
        cls,
        timeout: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> Self: ...

    def remaining(self) -> float | None:
        return None if self._deadline is None else max(0.0, self._deadline - self._clock())
```

The timeout comes from `backend_class.external_operation_timeout(active.config)`. The core rejects a
non-`None` value that is non-finite or not positive as `StateError` before the factory. Budget state
is never shared across sources and never survives the source turn.

The same `remaining_time` callable is supplied to factory, provider context, core context driver,
`prepare`, and `resolve`. Before invoking factory, entry, `prepare`, and `resolve`, the orchestrator
itself checks the current remainder. A zero remainder produces `SecretClientTimeout` without
starting that boundary. Exit is the sole exception: after successful entry it always runs, including
when its starting remainder is zero. The implementation checks again immediately before each actual
external call, because local work between method entry and the external seam also consumes budget.

The orchestrator checks again after factory, entry, prepare, and resolve return. If a boundary
returns only after the budget reaches zero, the work has stopped, so reporting timeout is truthful:
the core starts no next boundary, discards any returned values, exits an entered context, and
attributes timeout to the attempted tuple. Exit is different because cleanup may not mask the
primary result. The driver samples the remainder immediately before and after a normal exit. A
normal exit that starts with a positive finite remainder and returns at zero overran its allowance
and is the cleanup failure specified below. A normal exit that starts at zero is permitted
local-only cleanup and creates no additional cleanup failure. Raising or truthy exit behavior is
still a cleanup failure at either starting remainder. No later source starts until exit returns or
raises.

Prompt inherits a `None` timeout, so its factory, local context, no-op prepare, broker call, and
human wait have no deadline. OnePassword biometric or reauthentication time occurs inside `op read`
and is charged to its external-operation timeout. No budget pause or deadline extension exists.

### Timeout truthfulness

The orchestrator does not wrap blocking calls in threads, futures, signals, or generic timeout
helpers. A backend enforces time at its native interruptible boundary. Reporting `timeout` is legal
only after that boundary guarantees the work has stopped.

At budget exhaustion:

1. No later factory, entry, prepare, or resolve work begins for the source.
2. The context still exits if entry completed.
3. Exit sees `remaining_time() == 0.0`; it may cancel and release local state but starts no
   unbounded remote cleanup. A normal false-returning local exit in this state does not warn merely
   because it began at zero.
4. Every request in the attempted batch receives `timeout/deadline-exceeded` with the source name.
5. Those secrets halt and do not fall through. The next source may still serve unrelated missing
   secrets after this context closes.

A backend with no non-human blocking operation returns `None` and receives
`remaining_time() is None`. It must not invent a timeout. Prompt has no external timeout. Env-var
performs only local environment reads.

## Exact source-turn control flow

The core input is the deduplicated declarations, ordered active sources, explicit policy, and the
optional caller broker. Duplicate names collapse in first-encounter order and the first declaration
owns mapping and prompt metadata, matching current `Resolver.register` and `resolve_secrets`. There
is exactly one outcome per unique name in that order.

Within each source turn, before that source's client opens, the core lazily builds only that
source's request tuple from still-missing secrets:

1. Read this source's mapping through the source-contract helper.
2. Drop the request when the value is framework `False`.
3. Call `backend_class.would_attempt(name, mapping_present=...)`.
4. For a present mapping, obtain its frozen validated model through `validate_capability_mapping`;
   for an absent mapping use `None`.
5. Build `SecretLookupRequest(name, validated_mapping)`.

Mapping validation has already run during Registry finalize. Revalidation here is a defensive, pure
projection step for hand-built runtime inputs and must agree with finalize because both call the
same helper. A disagreement is a `StateError` before any client or prompt, not a source failure.

For each source in chain order:

1. If no secret is missing, stop. No later source is inspected or constructed.
2. Build the source's request tuple from still-missing names. If empty, continue without a budget or
   factory.
3. If readiness is not ready, record not-ready evidence for each request and continue. No budget,
   factory, context, broker call, or warning occurs in the typed core.
4. If the backend is interactive and policy is `REFUSE`, record refused evidence for each request
   and continue without construction.
5. Under `COMPLETE`, immediately before every allowed interactive source turn, run the doom check
   below. This includes Prompt, OnePassword, and interactive plugin backends and does not depend on
   whether the source receives an Agentworks broker.
6. If this is Prompt and the caller supplied no broker, raise `StateError`. This check occurs only
   after the mandatory doom prediction and before a budget or factory.
7. Start the source budget and call the resource-free factory once. If the post-factory check finds
   the budget exhausted, discard the context, which owns no resources, and attribute timeout. Else
   drive entry once, `prepare` once, and `resolve` once with the identical ordered tuple.
8. Validate the returned mapping without rendering it: keys must be a subset of request names and
   values must be strings. A protocol violation fails the whole attempted batch and discards every
   returned value.
9. For each returned name in request order, reject NUL, carriage return, or newline exactly as the
   current transport guard does. That request becomes `resolution-failure/malformed-value`, its
   value is discarded, and it hard-halts. Other valid returned names become `resolved`; absent names
   record soft-miss evidence and remain missing.
10. Exit the context before considering another source.

First resolved source wins because resolved names leave the missing set permanently. The client
mapping's iteration order never affects outcomes; request order owns all attribution.

### Fail-before-interaction doom

The doom check runs only for `CompletionPolicy.COMPLETE`, after all earlier source turns have
produced their real soft misses and before every allowed interactive source turn. It applies to
Prompt, OnePassword biometric or reauthentication paths, and every interactive plugin, independent
of broker availability. For every still-missing secret, it examines the current and later sources
through only pure `would_attempt`, framework opt-out, folded readiness, and interaction policy. It
does not construct a context, inspect a broker, probe authentication, or perform I/O. A source that
is disabled, not ready, refused, or opted out does not save a secret.

If any secret has no remaining attemptable source, the complete operation cannot succeed. The core
does not call the interactive factory, enter its context, start `op`, invoke an interactive plugin,
or call a broker. Causal secrets receive the ordinary terminal unavailable detail
(`source-not-ready`, `source-backend-plugin-disabled`, `soft-miss`, or `no-attemptable-source`).
Other still-missing secrets that were skipped solely because the batch was already doomed receive
`unavailable/batch-doomed-before-interaction/resolve-blocking-secrets`. The compatibility boundary
then raises once. This preserves the command protection while making every requested secret's typed
state total.

`PARTIAL` inspection does not apply this command-only optimization. It may resolve independent
secrets, including through an allowed prompt, even when another secret will finish unavailable. This
is the explicit reconciliation between command all-or-nothing and inspection partial success.

### Evidence collapse at end of chain

Soft misses, not-ready skips, and policy refusals are internal evidence, not intermediate outcomes.
At end of chain each still-missing secret collapses deterministically:

1. If any ready interactive candidate was refused, use `refused-interaction/interaction-refused`,
   name the first such source in chain order, and recommend `allow-interaction`.
2. Else if at least one ready source attempted and soft-missed, use `unavailable/soft-miss` and
   `configure-source`, naming the first soft-missing source in chain order.
3. Else if at least one candidate was not ready, name the first such source in chain order. If its
   backend row is disabled by a system plugin, use
   `unavailable/source-backend-plugin-disabled/enable-plugin` with that validated plugin identity as
   the target. Otherwise use `unavailable/source-not-ready/enable-source` without a target.
4. Else if the active chain is empty, use `unavailable/no-active-source`.
5. Else use `unavailable/no-attemptable-source`.

Hard failures and timeouts are terminal when observed and never enter this collapse.

### Batch failures and interruptions

Factory, entry, prepare, and resolve failures use one attribution rule:

- `SecretClientTimeout` gives every attempted request `timeout/deadline-exceeded`.
- `SecretClientFailure` gives every attempted request `resolution-failure` with its mapped detail
  and remediation.
- Any other `Exception` from provider code gives every attempted request
  `resolution-failure/unexpected` with `report-backend`. Its message, arguments, traceback, and
  locals are not copied to the outcome, warning, log, or compatibility error.
- `UserAbort` and non-`Exception` control flow are re-raised after cleanup and produce no batch.

All four terminal failure cases halt every request in that attempted batch. The core may continue
later sources only for other still-missing secrets that were not in the batch.

### Cleanup and non-masking rules

The core never uses a provider context directly. It wraps it in this manual, no-suppression driver.
The driver receives the same `remaining_time` callable as the provider context and the validated
`ActiveSource.name`. Source names have already passed the source resource's identity validation, so
rendering the string with `!r` cannot inject terminal controls or invoke provider code.
`_warn_cleanup_failure` attempts exactly one emission from the fixed template and catches any
output-handler failure, including control flow, so warning delivery itself cannot replace the
primary result:

```python
_CLEANUP_WARNING = "secret source {source_name!r}: cleanup failed; primary result unchanged"

def _warn_cleanup_failure(source_name: str) -> None:
    try:
        output.warn(_CLEANUP_WARNING.format(source_name=source_name))
    except BaseException:
        pass

class _SourceContextDriver(AbstractContextManager[SecretSourceClient]):
    def __init__(
        self,
        inner: AbstractContextManager[SecretSourceClient],
        *,
        source_name: str,
        remaining_time: RemainingTime,
    ) -> None:
        self._inner = inner
        self._source_name = source_name
        self._remaining_time = remaining_time

    def __enter__(self) -> SecretSourceClient:
        return self._inner.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        started = self._remaining_time()
        failed = False
        try:
            suppressed = self._inner.__exit__(exc_type, exc, traceback)
            failed = bool(suppressed)
        except BaseException:
            failed = True
        else:
            finished = self._remaining_time()
            if started is not None and started > 0.0 and finished == 0.0:
                failed = True
        if failed:
            _warn_cleanup_failure(self._source_name)
        return False
```

The wrapper's `__exit__` receives the body's original `exc_type`, exception object, and traceback
from Python and forwards those exact three objects to the provider even when `started == 0.0`; there
is no pre-exit timeout short-circuit. It catches every provider-exit exception, including
cleanup-time interrupts, and treats a truthy return as the same protocol failure. A normal false or
`None` exit warns for budget overrun only when `started` was positive and finite and `finished` is
zero. Starting at zero permits the required local-only cleanup without that extra warning. Raising,
truthy return, and positive-to-zero overrun are coalesced into exactly one warning if more than one
condition applies.

The warning includes only the validated source identity and the fixed phrase; the cleanup exception,
return object, traceback, config, identifier, request, and value are never rendered or logged.
Returning literal `False` means the wrapper never suppresses the body's exception or control flow.

Provider failures remain exceptions until they have left the wrapper, so `__exit__` sees the real
failure rather than a pre-converted outcome. Outside the wrapper the core translates them to batch
outcomes. On body success, exit failure warns and the successful result survives. On an ordinary
exception, `UserAbort`, `KeyboardInterrupt`, `SystemExit`, or `GeneratorExit`, exit failure or a
truthy suppression attempt warns first and the original object propagates unchanged. Entry failure
never invokes exit; the provider entry contract owns rollback of partial acquisition. No later
source begins until provider exit has returned or failed and the wrapper has returned `False`.

## Built-in client behavior

### Env-var

- Factory and context are local no-ops; `external_operation_timeout` remains `None`.
- `prepare` is a no-op.
- `resolve` unwraps `EnvVarMapping` or derives `AW_SECRET_<NAME>`, reads `os.environ`, strips only
  trailing CR/LF as today, and omits unset names as soft misses.
- It does not log names or values and never receives an interaction broker.

### Prompt

- Factory requires a non-`None` broker and has no external timeout.
- Context and `prepare` are no-ops.
- `resolve` calls `broker.request_secret(request.name)` in request order and returns those values.
  It does not inspect stdin, global flags, descriptions, or hints.
- `InteractionPolicy.REFUSE`, missing-broker checks, doom behavior, and value control checks all
  live in the orchestrator, not this client.

### OnePassword

`OnePasswordSourceConfig` and `OnePasswordMapping` retain the exact shapes fixed by the source
contract LLD: account and positive finite timeout are source config; one mapping is one validated
`op://` root value.

- `external_operation_timeout(config)` returns `config.timeout`.
- Factory, context, and `prepare` perform no subprocess or sign-in preflight. There remains no
  `op whoami` call.
- `resolve` walks requests in request order and invokes exactly
  `op read --no-newline [--account ACCOUNT] REFERENCE` with explicit argv and no shell.
- Immediately before every `subprocess.run`, call `remaining_time()`. If it is zero, raise
  `SecretClientTimeout` without spawning. Otherwise pass the numeric remainder as
  `subprocess.run(timeout=remaining)`.
- Catch `subprocess.TimeoutExpired` only at this seam and raise `SecretClientTimeout`. Python's
  `subprocess.run` kills and waits for the child before raising, so no `op` process continues.
- A race where `op` disappears after readiness becomes
  `SecretClientFailure(CONNECTIVITY, CHECK_CONNECTIVITY)` and renders only the enum-derived
  check-connectivity remediation at the consuming boundary.
- Existing signed-out markers become `SecretClientFailure(AUTHENTICATION, SIGN_IN)`. Existing narrow
  missing-item or missing-field markers become `SecretClientFailure(HARD_MAPPING, CHECK_MAPPING)`.
- Other nonzero exits become `SecretClientFailure(EXTERNAL, RETRY)`. Raw stderr is used only inside
  the local classifier and is then discarded, along with failure-path stdout. Neither appears in an
  exception, outcome, log, warning, repr, or renderer record.
- No result obtained earlier in the same call is returned after any failure. The core attributes
  that batch failure to every request, preserving the current safe fail-closed behavior.

The timeout, missing-binary, and result classifiers follow the provider translation template
exactly: each native handler records only enums or a timeout boolean, the `CompletedProcess` local
is set to `None`, and the typed exception is constructed and raised only after leaving every
`except` suite. If a later `op read` fails, `resolve` clears values from all earlier reads before it
raises the fresh typed failure. Tests require both `__cause__` and `__context__` to be `None` and
prove the typed exception object exposes no `TimeoutExpired`, `OSError`, `CompletedProcess`, stdout,
stderr, native result, or resolved value through its message, arguments, attributes, cause, or
context.

OnePassword remains `interactive=True`, so `REFUSE` excludes it before construction. It receives no
Agentworks prompt broker under `ALLOW`; any biometric or reauthentication UI belongs to the bounded
`op` subprocess.

## Compatibility boundary through Phase 7

Phase 5 lands one final typed core, then keeps the current caller shapes through private adapters:

```python
def _resolve_complete_for_legacy_callers(...) -> dict[str, str]:
    batch = resolve_batch(..., policy=ResolutionPolicy(..., CompletionPolicy.COMPLETE))
    return batch.complete_or_raise()

def _resolve_partial_for_inspection(...) -> tuple[dict[str, str], tuple[ResolutionOutcome, ...]]:
    batch = resolve_batch(..., policy=ResolutionPolicy(..., CompletionPolicy.PARTIAL))
    return _inspection_projection(batch)
```

The operation-scoped `Resolver` continues to store a dictionary until Phase 7, but that dictionary
can be obtained only from `complete_or_raise`; no incomplete batch seeds or replaces its cache.
Gate-seeded values remain excluded from the new batch and are joined only after the batch is
complete, preserving no-double-resolve and operation lifetime.

The legacy `resolve_secrets(..., errors=...)` spelling may remain as a feature-branch-only wrapper
while call sites move. Its `errors is None` arm uses the complete adapter; its dictionary arm uses
the private inspection projection. It contains no resolution logic. Phase 7 deletes this wrapper,
the string error out-parameter, and the global-interactivity bridge after every operation and
inspection caller passes explicit policy and consumes the appropriate typed surface.

During the bridge only, the ordinary compatibility call site may derive `ALLOW` from the existing
`output.is_interactive()` result to preserve behavior for unmigrated commands. That global read is
confined to the consuming-domain adapter. It is never in a backend, client, `resolve_batch`, or
provider-facing contract, and Phase 7 removes it.

## Non-disclosure invariants

The following are implementation requirements, not review advice:

- Values exist only in a client's local return mapping, `ResolutionBatch._values`, the temporary
  complete dictionary, the operation-scoped resolver cache, and the existing explicit value-reveal
  inspection path.
- Outcomes, provider failures, compatibility errors, cleanup warnings, readiness records, logs,
  graph rows, resource rows, diagnostic render records, and safe identifiers are value-free.
- No code logs a provider exception or traceback. An unexpected provider exception may contain a
  value in its message, arguments, captured output, or chained cause, so even debug logging of it is
  forbidden.
- `ResolutionBatch` never reaches an output handler, generic serializer, `vars`, dataclass helper,
  Pydantic model, or JSON encoder. Renderers receive outcomes or the existing explicitly authorized
  reveal value, never the batch.
- Provider-returned values are validated before an outcome changes to `resolved` and before an
  informational line could be built. The typed core itself emits no per-value success log.
- Outcome names, sources, identifiers, details, and remediation are independently safe; a sentinel
  value must not be recoverable by formatting or serializing any of them.

Sentinel tests use distinct values in factory, entry, prepare, resolve, cleanup exception messages,
returned secrets, and chained exceptions. Assertions cover `str` and `repr` of batch, outcomes,
provider failures, compatibility errors, collected output events, `caplog`, human outcome render,
future JSON conversion, and every reachable exception object's message, arguments, captured-output
attributes, cause, and context. Tests do not inspect traceback frame locals or claim process-memory
erasure. The only allowed observable sentinel occurrence is the explicitly authorized value returned
by successful `complete_or_raise` or the existing value-reveal inspection cell.

## Implementation and test matrix

| Area                      | Required tests                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Request projection        | Frozen and slotted; mapping model frozen; absent/present/`false`; mapping validated against selected source backend; no declaration, metadata, raw mapping, source copy, registry, resolver, other mapping, or value field                                                                                                                                                   |
| Broker isolation          | Name-only protocol; caller owns description/hint map; unknown name fails before prompt; only PromptBackend receives non-`None`; OnePassword and plugin clients receive `None`; prompt has no source timeout or pause abstraction                                                                                                                                             |
| Factory signature         | Focused secret-backend helper at the contract seam; no descriptor changes; raw direct/inherited classmethod; defining-function globals and owner namespace, including cross-module inheritance; exact `cls`, parameter order/name/kind/default/resolved annotations/return; stable errors; abstract/non-callable earlier; instance/static/wrong signatures                   |
| Factory purity            | Factory constructs only an unentered context; no resource, subprocess, authentication, lock, or I/O; post-factory expiration discards a resource-free context; acquisition begins only at entry                                                                                                                                                                              |
| Active source             | Frozen/slotted and ordered; carries declaration, class, validated config, readiness; no client/factory/context/broker/budget/value; disabled filtered and not-ready retained                                                                                                                                                                                                 |
| Lazy lifetime             | No attempt tuple means no timeout query or factory; one factory, entry, prepare, resolve, and exit per attempted source; no later source constructed after completion; next source begins only after exit                                                                                                                                                                    |
| Budget                    | Monotonic start immediately before factory; one deadline across factory/entry/prepare/resolve/exit; shrinking remainder; zero prevents factory/entry/prepare/resolve but never exit; new source gets independent budget; `None` for env-var/prompt; invalid backend timeout is `StateError`                                                                                  |
| Native timeout            | Fake factory, entry, prepare, and resolve boundaries consume budget; timeout attributes whole attempted batch; no thread/future wrapper; fake work proves no callback/process continues after outcome; exit gets the same live remainder                                                                                                                                     |
| Cleanup                   | Driver receives source and remainder; exact `exc_info`; exit always runs; normal positive-to-zero exit warns; normal pre-zero local exit does not; truthy/raising exit warns at either remainder; overlapping failures emit one source-naming fixed-template warning; warning-sink failure cannot mask; always false; original result/control flow unchanged; entry rollback |
| Categories                | Exact five categories and exhaustive per-detail category/remediation/source/identifier table; exact provider mappings; deterministic first-source evidence; illegal tuple rejected; identifier controls rejected; frozen/slotted/value-free                                                                                                                                  |
| Batch privacy             | Constructor invariants; request order; redacted repr/str; no serializer/mapping/iteration/partial property; complete returns a copy; incomplete returns no values through its public result or exception object                                                                                                                                                              |
| Precedence and dedupe     | Duplicate first declaration wins; one outcome per name; first resolved source wins; later source never sees it; request and outcome order independent of client mapping order                                                                                                                                                                                                |
| Fallthrough and halt      | Soft miss continues; hard mapping, auth, connectivity, external, unexpected, malformed value, protocol violation, and timeout halt every attributed secret; unrelated missing names may continue                                                                                                                                                                             |
| Readiness and refusal     | Not-ready creates no client and falls through; refused interactive creates no client; final evidence precedence is refused, soft miss, not-ready, empty chain, no attempt; first refused/soft-miss/not-ready source attribution is deterministic                                                                                                                             |
| Batch attribution         | Factory, entry, prepare, and resolve failures mark every attempted request and discard partial returned work; no attempted secret falls through; unrelated names continue after cleanup                                                                                                                                                                                      |
| Control values            | NUL, CR, LF, leading/embedded/trailing cases fail before `resolved`; env-var trailing CR/LF stripping remains before guard; tabs and other values retain current behavior; sentinels absent from error/outcome/output                                                                                                                                                        |
| Doom                      | Complete mode checks before every allowed Prompt, OnePassword, and interactive-plugin turn, independent of broker; pure remaining-source prediction includes readiness/refusal/opt-out; no factory, broker, `op`, or plugin call when doomed; partial still resolves independent secrets                                                                                     |
| Command versus inspection | Same core batch; complete adapter returns all values or raises with none; inspection projection retains successes and typed failures; no prompt answer is repeated; no second resolution loop or error-text branch                                                                                                                                                           |
| Env-var                   | Default name, explicit validated mapping, unset soft miss, CR/LF strip, one batch, no broker, no timeout, no logging                                                                                                                                                                                                                                                         |
| Prompt                    | Explicit allow/refuse, missing broker is `StateError`, broker order and metadata rendering, no TTY/global read, abort/interrupt cleanup, control-value rejection                                                                                                                                                                                                             |
| OnePassword               | Config account/timeout; exact argv/remainder; zero does not spawn; timeout child stopped; biometric/reauth charged and covered by doom; exact typed translations; later failure returns no earlier values; no native stdout/stderr attached to rendered or raised exception objects                                                                                          |
| No disclosure             | Sentinels in values/native errors/results/every phase are absent from persisted state, argv, logs, rendered output, outcomes, and exception-object messages/arguments/attributes/cause/context; successful complete dictionary and explicit reveal cell are the authorized observable value surfaces                                                                         |
| Simple-case parity        | Absent settings, explicit env-var/prompt chain, env-name derivation, prompt opt-out, precedence, duplicate names, partial inspection, complete command failure, fail-before-interaction, operation cache, and gate-seed no-double-resolve match baseline                                                                                                                     |

## Phase handoff

Phase 3 adds the exact provider-facing contract and registration check without changing production
resolution. Phase 5 implements clients, budget, typed core, source-chain cutover, private adapters,
and all lifecycle and non-disclosure tests. Phase 6 inventories every remaining consumer and fixes
the permanent operator mappings. Phase 7 moves those consumers to explicit policy and typed
surfaces, then deletes the dict/error/global-interactivity adapters.

This LLD is complete when implementation can proceed without choosing a request field, prompt
authority, factory signature, timeout owner, cleanup rule, client lifetime, outcome category,
detail/remediation vocabulary, batch privacy boundary, miss precedence, failure attribution, command
versus inspection policy, OnePassword translation, or temporary compatibility behavior.
