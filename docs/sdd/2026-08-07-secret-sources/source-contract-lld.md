# LLD: Secret Source and Backend Contracts

<!-- cspell:ignore abstractmethod isfinite -->

- Status: Reviewed, implemented, and locked
- Scope: Phase 1 source contract LLD, paired with the Phase 2 resolution lifecycle LLD and consumed
  by implementation Phases 3, 4, and 5
- Governing artifacts: [FRD](./frd.md), [HLA](./hla.md),
  [migration strategy](./migration-strategy.md), and [prior-art research](./prior-art-research.md)
- Code baseline: detached Phase 1 worktree at `cd862671`

## Purpose and fixed rulings

This document fixes the code-level source/backend contract before the migration starts. It
complements, but does not redesign, the request, client, lifetime, and cleanup contracts owned
solely by `resolution-lifecycle-lld.md`. That lifecycle LLD is Phase 2 and MUST be reviewed to
completion before any code phase starts. Together the two LLDs fix the identities, models,
selectors, graph behavior, registry payloads, diagnostics, and lifecycle that implementation
consumes.

The following are inputs, not choices left to implementation:

- A `secret-backend` is an implementation class. A `secret-source` is one declarable configured
  instance of exactly one backend.
- Every settings, mapping, graph, inspection, and runtime name resolves as a `secret-source`. There
  is no backend fallback. Direct configured-backend references, including `onepassword`, hard-break
  in 0.14.
- The app publishes `env-var` and `prompt` source rows under those exact spellings. Their default
  chain order, env-name derivation, prompt behavior, precedence, and explicit `false` opt-out do not
  change.
- A source lookup always happens first. Exact direct-backend remediation is considered only after
  that lookup misses. Therefore a declared or built-in source whose name equals a backend always
  wins.
- The whole feature remains one branch and one PR. Phase commits may contain internal adapters, but
  the final PR contains neither compatibility imports nor a source/backend dual interpretation.

## Baseline evidence and change anchors

| Concern                | Evidence at the baseline                                                                                                                    | Required destination                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Backend shape          | `cli/agentworks/secrets/backends.py:57` defines a structural `Protocol`; `:164` stores instances                                            | Nominal ABC and class registry under `capabilities/secret_backend`                               |
| Descriptor exception   | `cli/agentworks/capabilities/descriptor.py:59-73` defines `CONSTRUCTED_SINGLETON`; `secrets/kinds.py:290-328` uses one root config contract | Delete the enum arm and give the descriptor separate source-config and mapping contracts         |
| Plugin instance branch | `cli/agentworks/plugins/adapters.py:101-139` branches in `matches` and `prepare`                                                            | Class identity and class payload for every kind                                                  |
| Shared config bridge   | `cli/agentworks/capabilities/config.py:72-226` selects, validates, and extracts one `config_model`                                          | Keep this path for source config; add parallel mapping-model operations using the same internals |
| Current secret graph   | `cli/agentworks/secrets/base.py:91-169` emits backend edges through `FinalizeContext.available_backends`                                    | Emit source edges and select a source's backend through one helper                               |
| Finalize context       | `cli/agentworks/resources/graph.py:320-417` carries the backend-specific instance tuple                                                     | Generic read-only capability-class projection plus rows                                          |
| Readiness precedent    | `cli/agentworks/vms/sites.py:87-202` owns dependency, readiness, and tagged-config validation for `vm-site`                                 | `SecretSourceDecl` follows the same consuming-resource pattern                                   |
| Publication order      | `cli/agentworks/bootstrap.py:52-135` publishes bundles, capability rows, plugins, then operator manifests                                   | Insert default-source publication after plugin publication and before operator manifests         |
| Collision provenance   | `cli/agentworks/resources/registry.py:103-190` stamps origins; `:215-218` permits operator-over-built-in only by kind policy                | `secret-source.builtin_override = "allow"`; derive override status from the surviving row        |
| Static spec splice     | `cli/agentworks/manifests/spec_model.py:66-104` handles one tagged field and explicitly omits map-keyed hosting at `:177-195`               | Extend the same model projection for `mapping_host`                                              |
| Miss timing            | `cli/agentworks/resources/registry.py:387-467` resolves missing edges before validation; `:621-657` owns the error-policy miss              | Add a parallel validation-only set resolved at each existing resolve point                       |
| Settings misses        | `cli/agentworks/config/references.py:55-168` generically resolves setting names after finalize                                              | Point the chain at `secret-source` and invoke the same source-miss diagnosis                     |

## Final backend contract

### Nominal ABC and declared models

`agentworks.capabilities.secret_backend.base.SecretBackend` is a nominal abstract subclass of
`Capability`, not a `Protocol`:

```python
class SecretBackend(Capability, ABC):
    owner_kind: ClassVar[str] = "secret-source"
    contract_version: ClassVar[int]
    config_model: ClassVar[type[AgwModel]]
    mapping_model: ClassVar[type[AgwRootModel[Any]]]
    interactive: ClassVar[bool]

    @classmethod
    @abstractmethod
    def backend_readiness(cls) -> Readiness: ...

    @classmethod
    @abstractmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool: ...

    @classmethod
    @abstractmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None: ...

    @final
    def preflight(self, ctx: RunContext) -> None:
        return None

    @final
    def runup(self, ctx: RunContext) -> None:
        return None
```

The displayed class is the source-contract-owned portion of the final ABC. This LLD deliberately
does not declare, reserve, or require a client-factory method. The Phase 2 lifecycle LLD is the sole
owner of the exact final factory name, signature, request, return type, client, context-manager,
cleanup, broker, and remaining-budget protocols. Once that contract has passed review, the final ABC
adds its exact abstract method and Phase 3 conformance requires it. No implementation phase may add
a disposable stub, an ellipsis signature, or a temporary public client protocol. A
feature-branch-only private adapter may continue to call existing `batch_get` code until the Phase 5
atomic cutover.

The model split is strict:

- `config_model` is mapping-shaped `AgwModel`, includes `name: Literal[backend-name]`, and describes
  the keys beside `spec.backend.name` on a `secret-source`. `Capability.__init__` binds the untagged
  `CapabilityBlock.config` after `validate_own_config` injects the class's tag.
- `mapping_model` is `AgwRootModel[Any]` and describes one non-`false` value in
  `SecretDecl.backend_mappings`. It carries no backend or source tag because the outer map key is
  the source selector. Registration requires its entire accepted annotation tree to use the
  descriptor's JSON-native input vocabulary; model validators may reject a subset.
- When `MappingHost.false_opt_out` is true, the exact singleton `False` belongs to the framework map
  host. It is never an arm of a backend's `mapping_model` and never reaches `would_attempt`,
  `describe_lookup`, or a client. `True` has no framework meaning and reaches the selected mapping
  model like every other JSON-native value.
- `would_attempt` receives only the secret name and mapping presence, which is sufficient for
  default-address and mapping-required policies and keeps the capability package independent of
  `SecretDecl`. It is total, offline, non-constructing, and non-raising for every string and
  boolean.
- `describe_lookup` receives a validated mapping-model instance or `None`, performs no I/O, and
  returns only a safe identifier. A backend unwraps its own root model; callers do not inspect it.
- `backend_readiness` is config-independent, offline, total, and class-level. It returns
  `Readiness.ready()` or a truthful `Readiness.blocked(reason)`, including an installable local tool
  being absent; the descriptor uses that verdict directly for the backend row. Source-specific
  config readiness remains the inherited `Capability.not_ready(config)` classmethod. Network,
  authentication, biometric, and account checks belong to the lifecycle client's `prepare`, never
  either static readiness operation.
- `interactive` is a class-level capability fact. Interaction policy and the client protocol remain
  Phase 2 lifecycle concerns; backends never read TTY state through this attribute.

`EnvVarBackend.backend_readiness()` and `PromptBackend.backend_readiness()` always return
`Readiness.ready()`. `OnePasswordBackend.backend_readiness()` performs only `shutil.which("op")`:
absence returns `Readiness.blocked("op CLI not installed")`, and presence returns ready. It does not
run `op`, inspect accounts, or authenticate.

The built-in declarations are:

| Backend       | `config_model`                                                                                                       | `mapping_model`                                                                                        | Attempt policy                                                                       |
| ------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| `env-var`     | Tag-only `EnvVarSourceConfig`                                                                                        | Existing non-empty env-name root model                                                                 | Always; absent mapping derives `AW_SECRET_<NAME>` exactly as today                   |
| `prompt`      | Tag-only `PromptSourceConfig`                                                                                        | `AgwRootModel[Any]` whose runtime validator rejects every value and whose JSON Schema is `{"not": {}}` | Always unless framework-owned `false`; prompt text and TTY behavior remain unchanged |
| `onepassword` | `OnePasswordSourceConfig(name, account=None, timeout=30.0)` with non-empty optional account and finite `timeout > 0` | A single validated `op://` reference root model                                                        | Only when a mapping is present                                                       |

The old OnePassword `{account, reference}` mapping is not an arm of the final mapping model.
`account` moves to source config; `reference` is the resulting scalar mapping. `timeout` is seconds
and accepts an integer where strict Pydantic accepts it for `float`; zero, negative, NaN, and
infinity are rejected.

The secret-backend descriptor's supported `contract_version` is exactly `2`, and the ABC supplies no
default that an unmigrated implementation could inherit. `EnvVarBackend`, `PromptBackend`, and the
bundled `OnePasswordBackend` each explicitly declare `contract_version = 2` in the Phase 3 hard
cutover commit. A plugin declaring version 1 is not adapted or seated; it receives the existing
exact conformance reason:

```text
it declares contract_version 1, but this build supports secret-backend contract version 2
```

An external backend migrates by nominally subclassing `SecretBackend`, splitting source config from
per-secret mapping config, implementing `backend_readiness`, `would_attempt`, and `describe_lookup`,
accepting the Phase 2 lifecycle factory/client contract, retaining final lifecycle no-ops, and
declaring version 2. Plugin validation completes all checks before seating, so a mixed version-1 and
version-2 plugin contribution remains all-or-nothing.

### Fixed ordinary lifecycle

`SecretBackend.preflight` and `SecretBackend.runup` are concrete empty bodies decorated `@final`.
They intentionally do not call `super()`. Source resolution is upstream of every ordinary capability
runup, so either hook on a backend would run at the wrong layer.

Registration enforces the invariant in addition to type checking. For each forbidden name, find the
first class in `impl.__mro__` whose `__dict__` contains that name. It MUST be `SecretBackend`.
Defining, assigning, or inheriting an override below `SecretBackend` is rejected, even if its body
also happens to be a no-op. The stable error reasons are:

```text
it overrides final SecretBackend.preflight; secret backends resolve before ordinary capability preflight/runup
it overrides final SecretBackend.runup; secret backends resolve before ordinary capability preflight/runup
```

### Registration conformance

`conformance_error` remains non-constructing and checks in this order so the first answer is stable:

1. nominal `issubclass(impl, SecretBackend)`;
2. universal metadata and abstract-method constructibility;
3. required attributes and source-contract operations (`backend_readiness`, `would_attempt`, and
   `describe_lookup`), plus the exact client-factory operation only after Phase 2 adds it to the
   reviewed final ABC;
4. exact contract version 2, so an implementation written for an older public model contract gets
   the version migration error before a secondary model-shape error;
5. `config_model` against `descriptor.config_schema`;
6. `mapping_model` against `descriptor.mapping_schema`, including its input domain;
7. forbidden reference kinds on `config_model`;
8. forbidden `preflight`/`runup` overrides.

Both model checks require a class, the declared base, successful model construction, legal marker
placement, and the required tag only when that contract has a discriminator. The common checker
takes `(attribute_name, ConfigContract)` so `config_model` and `mapping_model` cannot drift into two
implementations. Error text names the actual attribute.

`ConfigContract` gains two general constraints:

```python
class ModelInputDomain(Enum):
    PYTHON = "python"
    JSON_NATIVE = "json-native"

input_domain: ModelInputDomain = ModelInputDomain.PYTHON
forbidden_reference_kinds: frozenset[str] = frozenset()
```

`PYTHON` is the current unrestricted behavior: conformance leaves the model's input vocabulary to
Pydantic. It is the default, so existing capability contracts do not change. `JSON_NATIVE` restricts
the declared annotation vocabulary to the allow-list suitable for a JSON-native manifest carrier;
the sanctioned `Any`/`object` wildcards rely on that carrier boundary. Validators may narrow the
vocabulary further.

The secret-backend `config_schema` sets `forbidden_reference_kinds=frozenset({"secret"})`; every
other existing contract keeps the empty default. A schema helper recursively enumerates every placed
`RefMarker.kind` reachable through nested models, discriminated arms, collection elements, and root
models, with a visited-model set. Conformance rejects the first sorted intersection and names the
field path. Merely scanning top-level fields is insufficient and is not conforming.

### JSON-native model input conformance

After a model is complete and its reference markers are legal, the common model checker applies its
contract's `input_domain`. `PYTHON` returns immediately. `JSON_NATIVE` walks the resolved annotation
tree depth-first, preserving field declaration and union-arm order, with a visited-model set for
recursive models:

- `None`, `str`, `bool`, `int`, and `float` are allowed value leaves. These are exact built-in
  leaves, not arbitrary subclasses; `Enum` and custom-class checks occur before primitive ancestry.
- `Any` and `object` are allowed value leaves because the raw carrier still limits actual inputs to
  JSON-native values.
- `Annotated[T, ...]` walks `T`. `Literal[...]` is allowed only when every literal is a JSON-native
  scalar (`None`, string, boolean, integer, or finite float).
- `T | U` and `Union[T, U]` walk every arm. `list[T]` walks `T` at `[]`.
- `dict[K, V]` requires `K` to accept strings only, then walks `V` at `{value}`. A string-only key
  annotation may be `str`, an `Annotated` string, a string-valued `Literal`, or a union composed
  only of those forms. `Any`, `object`, or a mixed/string-coercing key annotation is not
  string-only.
- An `AgwModel` walks its fields in declaration order; an `AgwRootModel` walks its root. Recursive
  occurrences reuse the visited-model set.
- Everything else is rejected by the allow-list. This includes `date`, `datetime`, `bytes`,
  `bytearray`, `set`, `frozenset`, `tuple`, `Enum` subclasses (including string enums), arbitrary
  custom classes/dataclasses, other collection origins, non-string mapping keys, and any occurrence
  of them inside a model, collection, `Annotated`, or union.

The first violation has one of these stable forms:

```text
its mapping_model BadMapping accepts non-JSON-native type datetime.date at root.expires; secret-backend mapping input is limited to JSON-native types
its mapping_model BadMapping accepts non-string mapping key type int at root.lookup{key}; secret-backend mapping input is limited to JSON-native types
```

Paths start at `root`, append `.field` for model fields, `[]` for list elements, and `{key}` or
`{value}` for mappings. Type labels use the built-in name or the offending class's stable
module-qualified name. Registration remains non-constructing: it inspects annotations and models but
does not run validators. Consequently `PromptMapping` conforms even though its validator
intentionally rejects every value; this contract guarantees JSON-native type vocabulary, not model
acceptance of any particular value.

Core built-ins are checked by the descriptor self-test just like plugin classes. Plugin validation
runs every check before adapter preparation or any registry mutation, preserving all-or-nothing
seating.

## Descriptor and shared model operations

### Descriptor types

The descriptor records both model surfaces explicitly:

```python
@dataclass(frozen=True)
class MappingHost:
    host_kind: str
    field_name: str
    key_reference: ResourceRef
    false_opt_out: bool

@dataclass(frozen=True)
class CapabilityKindDescriptor:
    # existing fields...
    config_schema: ConfigContract
    manifest_section: HostSurface | None
    mapping_schema: ConfigContract | None = None
    mapping_host: MappingHost | None = None
```

`HostSurface.config_field` becomes `str | None`. Existing hosts keep their retired sibling field;
the new `secret-source` host sets it to `None`, because no released source declaration has a sibling
`backend_config` shape to diagnose. Decode calls the retired-shape guard only when this field is
non-`None`.

Descriptor construction/self-tests enforce both-or-neither for `mapping_schema` and `mapping_host`,
that the host kind is declarable, that `field_name` exists and is mapping-shaped on the host row,
and that only one descriptor claims a `(host_kind, field_name)` pair. The
`key_reference.relationship` MUST be `RefRelationship.USES`, and its target kind MUST exist with
`miss_policy="error"`; an auto-declaring or other relationship is a descriptor `StateError`. These
constraints make map-key checking validation-only: it never materializes a target and never records
a dependency edge. `key_reference` remains the single carrier of the target kind, usage text,
relationship, and `x-agw-ref` schema extension.

The secret-backend record becomes:

```python
registry_policy=RegistryPolicy.CLASS_BY_NAME
config_schema=ConfigContract(
    base=AgwModel,
    discriminator="name",
    forbidden_reference_kinds=frozenset({"secret"}),
)
manifest_section=HostSurface(
    host_kind="secret-source",
    naming_field="backend",
    config_field=None,
)
mapping_schema=ConfigContract(
    base=AgwRootModel,
    discriminator=None,
    input_domain=ModelInputDomain.JSON_NATIVE,
)
mapping_host=MappingHost(
    host_kind="secret",
    field_name="backend_mappings",
    key_reference=ResourceRef(
        kind="secret-source",
        usage="a source for resolving this secret",
        relationship=RefRelationship.USES,
    ),
    false_opt_out=True,
)
```

`RegistryPolicy.CONSTRUCTED_SINGLETON` is deleted, not deprecated. `registry_policy` can remain in
the descriptor while all records say `CLASS_BY_NAME`; it still documents and self-tests the payload
shape. Adapter `matches` becomes `occupant is impl_cls`, `prepare` returns `impl_cls`, and the
constructor-failure branch and wording disappear.

### Parallel generic mapping operations

`agentworks.capabilities.config` exposes four mapping counterparts to the existing config
operations:

```python
capability_mapping_model(kind: str, name: str) -> type[BaseModel] | None
validate_capability_mapping(*, kind: str, name: str, mapping: object,
                            owner: RefOwner, location: SourceLocation | None) -> BaseModel | None
capability_mapping_references(*, kind: str, name: str, mapping: object,
                              owner: RefOwner) -> tuple[ConfigReference, ...]
capability_mapping_union(kind: str) -> type[BaseModel]
mapping_value_is_opt_out(kind: str, value: object) -> bool
```

They share private selection, validation, extraction, union construction, cache-key, error bridge,
and reference-hint machinery with the config functions. The only varying inputs are model attribute
(`config_model` or `mapping_model`) and descriptor contract (`config_schema` or `mapping_schema`).
They never invoke backend code. Asking mapping operations of a descriptor with no mapping contract
is a `StateError`, because that is a framework call-site error. `mapping_value_is_opt_out` is true
only for the singleton `False` when the descriptor's `mapping_host.false_opt_out` is true; it is the
one predicate used by validation, reference extraction, candidate construction, and runtime.

`capability_mapping_union` returns a generated root model. Its authored arms are the registered
`mapping_model` classes; it appends a `Literal[False]` arm only when
`mapping_host.false_opt_out=True`. The cache key contains the ordered model identities and the
opt-out boolean, so changing either cannot reuse a stale union. A descriptor with
`false_opt_out=False` passes `False` to its authored models and does not advertise a framework false
arm.

## Source declaration and the one selector

### `SecretSourceDecl`

`agentworks.secrets.sources.SecretSourceDecl` is:

```python
class SecretSourceDecl(DeclaredResource):
    NAME_MAX_LENGTH: ClassVar[int | None] = MAX_FREEFORM_NAME_LENGTH
    backend: CapabilityBlock
```

There is no copied backend class, mapping model, readiness field, client, resolved value, or
override flag on the row. `backend.name` is the implementation selector and `backend.config` is its
per-source config. The source kind has `category="declarable"`, `miss_policy="error"`, no
auto-declare names, and `builtin_override="allow"`.

`SecretDecl.backend_mappings` becomes the lossless JSON-native raw carrier:

```python
from math import isfinite
from typing import Annotated, cast

from pydantic import BeforeValidator

def require_exact_json_value(value: object) -> object:
    value_type = type(value)
    if value_type not in (type(None), bool, int, float, str, list, dict):
        raise ValueError(
            f"must use exact JSON-native runtime types (got {value_type.__name__})"
        )
    if value_type is float and not isfinite(cast("float", value)):
        raise ValueError("JSON numbers must be finite")
    return value

def require_exact_json_string(value: object) -> object:
    if type(value) is not str:
        raise ValueError(f"must use exact JSON string keys (got {type(value).__name__})")
    return value

type JsonString = Annotated[str, BeforeValidator(require_exact_json_string)]
type MappingValue = Annotated[
    None | bool | int | float | str | list[MappingValue] | dict[JsonString, MappingValue],
    BeforeValidator(require_exact_json_value),
]
backend_mappings: dict[JsonString, MappingValue]
```

It accepts JSON null, booleans (including `True`), strings, integers, finite numbers, arrays, and
string-keyed objects recursively. It rejects values outside that envelope, including YAML-native
timestamps, sets, binary/tagged objects, non-string object keys, enum members, primitive subclasses,
and non-finite numbers. It performs no backend-shape coercion: the recursive aliases attach exact
type checks before Pydantic can normalize either a value or mapping key, and each validator returns
the original object unchanged. Every value this carrier accepts is delivered unchanged to the
selected backend's `mapping_model`, subject only to framework `False` interception when opt-out is
enabled. The model then accepts or rejects that exact JSON-native value. The carrier does not make
arbitrary Python inputs accepted by an `AgwRootModel` reachable: dates, bytes, tuples, sets, enum
members, custom objects, and non-string-keyed mappings cannot arrive, and registration rejects a
mapping model whose annotation vocabulary asks for them. The descriptor-derived spec projection
replaces this raw field with the installed mapping union for emitted schema without narrowing what
decode can carry to runtime validation.

Its methods follow the existing `VMSiteDecl` contract:

- `dependencies(context)` always emits `secret-source/<source> -> secret-backend/<backend.name>`
  first, then appends config-implied references from
  `capability_config_references(kind="secret-backend", config=backend.tagged, ...)`. It is total and
  never validates or constructs.
- `validate_config(context)` calls `validate_capability_config` on the full tagged backend block,
  with `RefOwner(kind="secret-source", name=self.name)` and `self.error_location`. It does not wrap
  the bridge's file/line error.
- `not_ready(deps)` reads only the declared backend dependency. A disabled backend returns
  `depends on secret-backend '<name>', which is disabled; <carried reason or enable its unit>`. A
  non-ready backend verdict propagates verbatim. A present backend class then receives
  `backend.config` through its total `not_ready` classmethod. A missing impl returns ready only as a
  defensive totality case; the dangling backend edge has already hard-failed finalize.

### Generic class projection

`FinalizeContext.available_backends` is deleted. `build_context` instead creates one immutable
projection:

```python
capability_classes: Mapping[str, Mapping[str, type]]

def capability_class(self, kind: str, name: str) -> type | None: ...
```

The outer and inner maps are `MappingProxyType` snapshots derived only for descriptor kinds whose
policy is `CLASS_BY_NAME`; `rows` remains the existing read-only live row map. An unknown capability
kind raises `StateError`; a known kind/name miss returns `None`. The graph builder remains the sole
live-registry reader. `DependencyGraph.impl_of` and `DependencyState.impl` consequently carry
classes for every capability kind.

### One source-to-backend selector

`agentworks.secrets.sources.source_backend_class` is the only mapping from source name to backend
class. It reads through a narrow lookup protocol so finalize-time and post-finalize consumers use
the same selection control flow:

```python
class SourceBackendLookup(Protocol):
    def source_row(self, name: str) -> object | None: ...
    def backend_class(self, name: str) -> type | None: ...

def source_backend_class(
    lookup: SourceBackendLookup,
    source_name: str,
) -> tuple[SecretSourceDecl, type[SecretBackend]] | None:
    ...
```

The sources module supplies two private adapters: one over `FinalizeContext.rows_of` plus
`capability_class`, and one over finalized `Registry.lookup` plus `DependencyGraph.impl_of`.
Adapters contain retrieval only, no selection or fallback policy. Validation, extraction, default
attemptability, active-chain construction, inspection, and runtime construction all call the one
selector with the adapter for their phase.

Control flow is fixed:

1. Call `lookup.source_row(source_name)`.
2. If it is absent, return `None`. Do not inspect backend names.
3. Narrow the row to `SecretSourceDecl`; a wrong row type is `StateError`.
4. Call `lookup.backend_class(source.backend.name)`.
5. Return `None` on a missing class so graph construction and extraction remain total; otherwise
   enforce `issubclass(..., SecretBackend)` and return `(source, backend_class)`.

Both `SecretDecl.validate_config` and `SecretDecl.dependencies` call this helper. No validation,
extraction, preview, active-chain, inspection, or runtime site may reproduce the two lookups.

### Map-key validation references, activation, and `false`

Phase 4 adds and unit-tests the generic collector without calling it from `Registry.finalize`,
`Registry._walk_into`, or any production consumer:

```python
def capability_mapping_key_references(
    *, descriptor: CapabilityKindDescriptor, row: DeclaredResource
) -> tuple[ResourceReference, ...]:
    ...
```

It reads the descriptor's `mapping_host.field_name` and returns one reference per authored string
key, in map order, targeting `key_reference.kind` with its usage and `USES` relationship. Values are
not inspected, so `False`, `True`, and ordinary mappings produce identical validation references.
The collector is total and non-raising, never asks for a backend class, and does not add its output
to graph inbound or outbound maps. Through all of Phase 4 the helper is dormant: current production
interpretation continues accepting direct `onepassword` mapping keys, including a value of `false`.
There is no partial source-only enforcement before the Phase 5 atomic cutover.

Phase 5 activates collection at the start of `Registry._walk_into`, the common row-entry point used
by both the initial build walk and each row added by readiness-gated materialization. For each row,
`_walk_into` first appends the collector's output to a separate validation-only reference map, then
collects the row's ordinary `dependencies(context)` into the existing graph maps. Neither collection
resolves or throws, preserving the build walk's total/non-throwing contract and its existing
error-before-cycle precedence.

The accumulators and interleaved first-target encounter order are fixed:

```python
mapping_key_refs: dict[tuple[str, str], list[ResourceReference]]
miss_schedule: dict[tuple[str, str], None]

for descriptor in mapping_descriptors_for_host(kind):
    for ref in capability_mapping_key_references(descriptor=descriptor, row=resource):
        target = (ref.kind, ref.name)
        mapping_key_refs.setdefault(target, []).append(ref)
        miss_schedule.setdefault(target, None)
for ref in resource.dependencies(context):
    target = (ref.kind, ref.name)
    all_refs.setdefault(target, []).append(ref)
    all_outbound.setdefault(ref.source, []).append(ref)
    miss_schedule.setdefault(target, None)
```

`mapping_descriptors_for_host` preserves descriptor registration order. The first loop never writes
`all_refs` or `all_outbound`; the second loop is the existing dependency collection unchanged. Both
loops first-insert targets into the same ordered dictionary. Thus targets remain interleaved exactly
where the row walk first encountered them, while validation references remain outside graph maps.

The corresponding existing resolve stage iterates `miss_schedule` once in insertion order. A present
or already-deferred target keeps the existing skip behavior. For a missing target:

- when `mapping_key_refs[target]` is non-empty, its first reference owns framing. The target kind is
  necessarily the descriptor-self-tested `miss_policy="error"` kind; resolution calls its
  `missing_reference_error` hook, then ordinary error-policy framing when the hook returns `None`;
- otherwise, the first `all_refs[target]` follows ordinary miss resolution unchanged, including
  allowed auto-declaration deferral.

Existing ordinary targets retain their relative order because the schedule inserts them at their
original dependency encounter. A map-key target from a later row therefore cannot preempt a distinct
ordinary miss from an earlier row, and the reverse row order produces the reverse diagnostic order.
When validation and candidate references share a target, the schedule contains that target once and
the first validation reference owns its single source diagnostic, regardless of which reference set
first encountered that target across rows.

The initial resolve consumes the schedule built from initially published rows. Each
materialization-loop walk appends only newly encountered targets to that same ordered schedule, then
the loop invokes the same resolver before folding the new row. Earlier targets keep their positions;
present/deferred ones skip, and new misses resolve in incremental encounter order. Later
materialized host rows therefore cannot bypass or reorder the contract. Validation references are
never passed to graph construction, readiness, closure, or inspection.

Dependencies remain the sole owner of candidate graph edges. A known key whose value is `False` when
`false_opt_out=True` has a validation reference but no `secret -> secret-source` candidate edge,
mapping-implied references, or runtime attempt. An unknown false key receives the same
direct-backend or ordinary source-miss framing as the identical non-false key.

For every mapping that is not a framework opt-out:

- dependencies emit the explicit `secret -> secret-source` candidate edge; direct unit calls remain
  total and may return a dangling edge; Phase 5 production schedules its separate validation
  reference and candidate target once;
- if the selector succeeds, dependencies also call `capability_mapping_references` with the selected
  backend name and mapping, preserving total extraction;
- validation calls `validate_capability_mapping` with the same selected backend name and the owner
  label `secret/<secret>.backend_mappings.<source>`;
- if the selector returns `None`, validation does nothing. The explicit dangling source or backend
  edge reports the missing resource once.

For default mappings, `SecretDecl.dependencies` iterates `context.rows_of("secret-source")` in
registry order, selects each backend through the helper, and calls
`backend.would_attempt(self.name, mapping_present=False)`. It emits that source edge when true. An
explicit framework `false` for the source suppresses the default candidate only after the key passed
source existence validation in Phase 5. Explicit non-opt-out keys are then appended and deduplicated
by source name in first-encountered order. Activity and precedence remain settings concerns.

## Built-in sources and provenance

`publish_builtin_secret_sources(registry)` is a domain publisher in `agentworks.secrets.sources`. It
publishes exactly `env-var` then `prompt`, not every backend that happens to have an empty config
model. Each row is a normal `SecretSourceDecl` with `CapabilityBlock.of(name)`, added with:

```python
Origin.built_in(source="agentworks.secrets.sources")
```

`build_registry` order becomes:

1. bundled built-in manifests;
2. generic built-in capability rows;
3. system-plugin capability rows and bundled manifests;
4. built-in secret sources;
5. `Config.publish_to` (still a no-op);
6. operator manifests;
7. finalize, settings-reference validation, then secret chain semantics.

This guarantees both backend classes exist before source rows publish and built-in source rows exist
before operator collision handling. Because the source kind allows built-in override, an operator
manifest replaces `env-var` or `prompt` through the ordinary Registry path and the surviving row has
its operator origin.

Provenance is derived, never stored separately:

```python
class SourceProvenance(Enum):
    SYNTHESIZED_DEFAULT = "synthesized-default"
    OPERATOR_OVERRIDE = "operator-override-of-synthesized-default"
    DECLARED = "declared"
```

A row is `SYNTHESIZED_DEFAULT` only when its name is in `{"env-var", "prompt"}` and its origin is
the exact built-in source above. It is `OPERATOR_OVERRIDE` only when its name is reserved and its
origin is `operator-declared`. Every other origin/name combination is `DECLARED`. Describe and
doctor consume this function, so no shadow row or override boolean can drift from Registry truth.

## Static schema and exact runtime narrowing

`spec_model(kind)` applies descriptor projections in this order: the existing tagged-host field,
then every map host for that kind. For the secret map host it replaces only the `backend_mappings`
field annotation, preserving its authored `FieldInfo`, with:

```python
dict[
    Annotated[NonEmptyStr, mapping_host.key_reference],
    capability_mapping_union("secret-backend"),
]
```

The key marker emits `propertyNames.x-agw-ref` targeting `secret-source`. The value union is built
from every registered backend class's `mapping_model`, in registry order with duplicate model
classes removed. Its generated root model adds exactly one `false` arm when, and only when, the
descriptor's `mapping_host.false_opt_out` is true. Installed plugin capabilities are seated before
this projection, as they are for the tagged union.

JSON Schema cannot correlate a map key with a `secret-source` document in another YAML file, then
follow that source's `backend.name` to select one value schema. The emitted union is therefore
intentionally under-constrained: an env-var-shaped value may be accepted under a key whose declared
source uses another backend. It MUST NOT special-case `env-var`, `prompt`, or `onepassword` property
names, because operator declarations may override those names with a different backend.

Runtime is exact and source-first: resolve the key to a source, call `source_backend_class`, then
deliver the raw carrier's unchanged JSON-native value to only that class's `mapping_model` for
validation and extraction. A value that matches some other union arm still fails. Describe-kind
explicitly says the editor schema offers all installed mapping shapes while manifest loading narrows
each value through the named source.

## Direct-backend diagnostics

### Dispatch rule

One domain function owns both config and manifest framing:

```python
def direct_backend_source_error(
    *, name: str, registry: Registry, referrer: SettingReference | ResourceReference,
) -> ConfigError | None:
    ...
```

It is called only after `registry.lookup("secret-source", name)` or the equivalent finalize target
check has raised/missed. It then checks whether a `secret-backend` row of the exact same name
exists. If not, it returns `None` and the ordinary unknown-source diagnostic runs. It never creates
a row, normalizes a name, parses a compatibility format, or populates deprecation state.

`validate_setting_references` calls it for the chain's `SettingReference`. For manifest map keys,
Phase 5's shared scheduled miss resolver offers the `secret-source` kind's optional
`missing_reference_error` hook when the missing target has validation-only map-key references;
`_SecretSourceKind` delegates to the same function. The hook receives the first validation reference
and a read-only lookup of its declaring row, which is needed to render the mapping rewrite. The
value is deliberately irrelevant to dispatch, so a direct backend name mapped to `false` receives
this same remediation. Phase 4 defines and tests this hook path without activating it. Other target
kinds implement no hook and retain their current ordinary miss messages.

### Stable framing

For `[secret_config].backends`, the error is:

```text
[secret_config].backends references unknown secret-source '<backend>'
```

For a secret mapping, the error is:

```text
secret/<secret>.backend_mappings.<backend> references unknown secret-source '<backend>'
```

Both use a multiline hint with the same opening and a symbolic `<source-name>` that is intentionally
operator-chosen:

```text
'<backend>' is a secret-backend implementation, not a configured secret-source. In 0.14, declare a source under ~/.config/agentworks/resources/ (any filename):

apiVersion: agentworks/v1
kind: secret-source
metadata:
  name: <source-name>
spec:
  backend:
    name: <backend>

Then replace '<backend>' in <reference-path> with '<source-name>'.
```

For an old OnePassword table, the manifest variant renders the exact structural rewrite, preserving
the operator's literal account and reference safely because both are configuration, not resolved
values:

```yaml
spec:
  backend:
    name: onepassword
    account: <existing-account>
---
spec:
  backend_mappings:
    <source-name>: <existing-op-reference>
```

It also states that `timeout: 30` is the default and may be set on the source. A malformed old table
that lacks a usable `account` or `reference` gets the generic source declaration and path rename,
then final mapping validation reports its shape after the source exists. A bare `op://` mapping gets
the generic declaration plus the exact key rename. Settings framing never guesses source config it
cannot see.

The lookup order is tested explicitly: a built-in or operator source named `onepassword` resolves
normally and receives no migration diagnostic even though a backend of that name exists.

## Relocation and export sequence

The Phase 3 move is one commit and uses `git mv` where a whole file survives:

1. Create `agentworks.capabilities.secret_backend` and move the backend base/registry, env-var, and
   prompt modules there. Split the backend kind, entry, and descriptor out of
   `agentworks.secrets.kinds` into `capabilities/secret_backend/kinds.py`; secret and source kinds
   remain in `agentworks.secrets.kinds`.
2. Change the registry to `dict[str, type[SecretBackend]]` and core entries to classes. Repoint the
   descriptor collector, resource-kind import index, graph builder, capability publisher, plugin
   adapter, plugin snapshot/restore, OnePassword plugin, and tests before deleting old imports.
3. Delete `CONSTRUCTED_SINGLETON` and both adapter branches. No compatibility module remains at
   `agentworks.secrets.backends`, `.env_var`, or `.prompt`.
4. Add source-domain files only after the capability package is authoritative. This preserves the
   physical dependency direction: `secrets` may import `capabilities.secret_backend`; the capability
   package never imports `agentworks.secrets`.

This LLD fixes only capability/source ownership exports:

- `agentworks.capabilities.secret_backend`: `SECRET_BACKEND_REGISTRY`, `SecretBackend`,
  `SecretBackendEntry`, `EnvVarBackend`, `EnvVarMapping`, `EnvVarSourceConfig`, `PromptBackend`,
  `PromptMapping`, `PromptSourceConfig`, and `env_var_name_for`.
- `agentworks.secrets`: adds `SecretSourceDecl` as the source declaration's public type and retains
  consuming-domain ownership. This LLD does not add or name a typed resolution entry point.
- `agentworks.capabilities`: unchanged shared exports only. It does not flatten kind-specific names.

`SECRET_BACKEND_REGISTRY`, `ActiveBackend`, and `active_backends` do not survive as exports from
`agentworks.secrets`. The latter two may exist as feature-branch-only adapters until consumer
migration, but permanent code and docs must not import or teach them. There are no import aliases
from old module paths. All other consumer/runtime public-export decisions, including the disposition
of current resolver, chain, target, and inspection helpers, are explicitly deferred to
`operator-surfaces-lld.md`; Phase 5 MUST follow that reviewed inventory rather than infer an export
from this document.

## Implementation and test matrix

| Area                 | Required tests                                                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ABC                  | Non-subclass rejection; each source-contract abstract operation; class-readable metadata; descriptor and all three shipped implementations explicitly say version 2; exact version-1 plugin rejection text; no client factory exists until the reviewed lifecycle contract adds its exact abstract operation                                                                                                                               |
| Dual models          | Missing/wrong/unbuildable `config_model`; tag absent/wrong; missing/wrong/unbuildable `mapping_model`; mapping model gets no tag check                                                                                                                                                                                                                                                                                                     |
| Input domain         | Default `PYTHON` leaves existing contracts unchanged; positive direct/nested cases cover JSON primitives, `None`, list, string-keyed dict, JSON `Literal`, `Annotated`, unions, recursive `AgwModel`/`AgwRootModel`, `Any`, and `object`; negative direct/nested/union cases cover date/datetime, bytes, set/frozenset, tuple, Enum/custom class, and non-string keys; exact first field/type errors; all-rejecting `PromptMapping` passes |
| Mapping host         | Both-or-neither contracts; unique host/field; mapping-shaped field; missing target kind, auto-declare target, and non-`USES` relationship reject; helper output is authored-order validation references only and never graph edges                                                                                                                                                                                                         |
| Forbidden references | Direct, nested, list element, mapping value, root, and union-arm `SecretRef` in source config reject; non-secret refs and mapping-model secret refs follow their own contracts                                                                                                                                                                                                                                                             |
| Readiness            | Env-var/prompt class readiness is ready; OnePassword reports the exact blocked install-tool reason when `op` is absent and ready when present; source config readiness separately calls `Capability.not_ready`; neither path performs network/auth/account work                                                                                                                                                                            |
| Lifecycle            | Direct and inherited overrides of `preflight` and `runup` reject; unmodified inherited no-ops pass; neither hook is invoked by source resolution; the exact Phase 2 factory signature is required with no interim public signature                                                                                                                                                                                                         |
| Registry             | Core and plugin registries store exact class identities; graph `impl_of` and dependency states carry the same classes; adapter never constructs; a mixed valid/version-1 plugin contribution seats nothing                                                                                                                                                                                                                                 |
| Relocation           | Old modules fail import; no production or test import names the old paths; descriptor collector and resource-kind index load the new kind module once                                                                                                                                                                                                                                                                                      |
| Source decode        | Tagged backend block loads; scalar backend and retired sibling shape hard-error; unknown keys and wrong backend config get one file/line-framed error                                                                                                                                                                                                                                                                                      |
| Raw carrier          | Carrier-accepted list, integer/float, `True`, null, object, and nested JSON-native values reach the selected model unchanged; `False` reaches it when opt-out is disabled; YAML timestamps, sets, binary/tagged values, non-string keys, and non-finite numbers fail at decode with location framing                                                                                                                                       |
| Source graph         | Backend edge first; config reference edges follow; unknown backend dangles; disabled and both class/config not-ready verdicts propagate; source-specific readiness receives untagged source config                                                                                                                                                                                                                                         |
| Selector             | Unknown source returns `None` without backend lookup; wrong row/class is `StateError`; validation and extraction select the identical backend class; mutation test fails if either bypasses the helper                                                                                                                                                                                                                                     |
| Source-key lifecycle | Phase 4 collector tests do not affect Registry and current OnePassword string/table/`false` mappings still load; Phase 5 covers initial/materialized rows, cross-row ordinary-before-map and map-before-ordinary first-target order, same-target validation/candidate single diagnostic, known `false` with no candidate edge, and unknown false/non-false framing agreement                                                               |
| Secret edges         | Explicit non-opt-out source edge; framework `false` suppression only after source validation; default-attempt sources; dedupe/order; absent backend class remains total; no client construction                                                                                                                                                                                                                                            |
| Built-ins            | Exactly `env-var`, then `prompt`; exact built-in origin; publication precedes operator manifests; operator override wins and provenance says so; unrelated source is declared                                                                                                                                                                                                                                                              |
| Schema               | `propertyNames` references `secret-source`; every mapping model occurs once; `false_opt_out=True` adds exactly one false arm and `false_opt_out=False` omits it; prompt contributes impossible schema; list/number plugin fixture arms appear; closed nested keys remain closed                                                                                                                                                            |
| Runtime narrowing    | Every raw-carrier JSON-native value reaches the selected source's model unchanged; no Python-only `AgwRootModel` input is claimed reachable; a value accepted by another static arm still fails against the selected one; `True` is model-owned; false is host-owned only when enabled; validation/extraction agree; no property-name special case                                                                                         |
| Diagnostics          | Ordinary unknown source stays ordinary; exact backend match gets config framing; exact match gets manifest framing for false and non-false values; OnePassword table rewrite; bare reference rewrite; no near-match; same-name source wins                                                                                                                                                                                                 |
| Simple-case parity   | Default and explicit `env-var`/`prompt` chains, env-name derivation, prompt behavior, precedence, `false`, and operator overrides retain current spelling and results                                                                                                                                                                                                                                                                      |

Phase 1 is this source contract. Phase 2 completes and reviews the lifecycle LLD before code begins.
Phase 3 runs capability, conformance, registry, lifecycle-signature, and relocation rows. Phase 4
runs source, dormant map-key collector, selector, publication, carrier, schema, and static-narrowing
rows without wiring the collector into Registry or repointing production consumers. Phase 5
activates per-row collection/resolution in both initial and materialization walks, then runs
direct-diagnostic, active-chain, runtime narrowing, OnePassword rewrite, and simple-case golden rows
at the atomic source cutover.

## Permanent guidance and completion boundary

The capability author contract created with Phase 3 belongs in
`cli/agentworks/capabilities/secret_backend/README.md`. Source declaration, built-in override,
mapping, and runtime narrowing guidance lands with the behavior in the permanent resource and
secrets guides. Code comments and permanent docs explain their contracts locally and contain no link
to this feature directory.

This LLD is complete when the lead can review Phase 2's complementary lifecycle contract and then
assign implementation Phases 3, 4, and 5 without choosing a source/model owner, lookup order,
registry payload, ordinary-lifecycle exception, readiness fold, publication position, provenance
rule, schema approximation, runtime narrowing rule, diagnostic branch, relocation destination, or
the capability/source exports fixed here. Client lifecycle and remaining consumer/runtime exports
are intentionally not claimed by this completion boundary; their named LLD owners must fix them
before the relevant implementation.
