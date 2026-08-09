# Resource disable and provider resolution LLD

- Status: Independent review clean, ready for implementation
- Date: 2026-08-08
- Parent: [High-level architecture](./hla.md), decisions D1 through D5 and D7
- Implementation baseline: `main` at `615aa0da`

## Purpose and boundary

This design replaces publication-time resource collision decisions with a complete provider-claim
ledger, applies every disablement mechanism to every claim, and resolves each resource selector once
during finalize. The result makes an explicit resource disable policy safe, retains the provenance
of every disablement and sanctioned provider substitution, and rejects an enabled resource that
depends on a disabled resource.

This LLD covers the framework work in phases 1 and 2. The later plugin move only relocates existing
resource declarations. Nothing here moves or changes apt execution, install-command execution,
initializer orchestration, snap, mise, dotfiles, tmuxinator, or Claude setup. It adds no initializer
capability, consumer gate, raw-config-field gate, or default change.

## Current implementation and gaps

At the baseline:

- `Registry.add` resolves a collision against the row currently occupying a selector. Its answer can
  therefore depend on publication order.
- A disabled plugin manifest publishes with `weak=True`. A weak row may disappear before finalize,
  which loses both its origin and the fact that an operator row replaced it.
- `compose_enablement` retains only the first `DisabledMark` for a selector, and the frozen graph
  stores only the derived binary `Enablement` value.
- `describe` reconstructs a plugin disable reason from the active row's origin. It cannot show
  multiple causes or a displaced provider.
- Resource edges to disabled rows participate in readiness, but finalize does not enforce the R7
  enabled-source-to-disabled-target invariant.
- Dynamic guide names enumerate every resolved row without filtering disabled rows.

The new shape fixes these gaps at the registry boundary. Publishers remain unaware of collision and
disablement policy.

## Data model and ownership

### Canonical selectors

`agentworks.resource_names` owns the lowest-level selector value because config parsing, Registry,
errors, projections, and tests all need it without importing the `agentworks.resources` package.

```python
@dataclass(frozen=True, order=True)
class ResourceSelector:
    kind: str
    name: str

    @classmethod
    def parse(cls, value: str) -> ResourceSelector: ...

    def __str__(self) -> str:
        return f"{self.kind}/{self.name}"
```

`ResourceSelector.parse` accepts only a string that is already canonical:

1. The value has no leading or trailing whitespace.
2. It contains exactly one `/`, with a non-empty kind and name.
3. Both components match `RESOURCE_NAME_RE`. Uppercase, spaces, repeated separators, and empty
   components are errors, not inputs to normalize.
4. Each component is at most `MAX_RESOURCE_NAME_LENGTH` bytes when encoded as UTF-8.

The value object raises `ValueError` for bad syntax. The config loader catches it and raises
`ConfigError` with the array index and expected `kind/name` form. Semantic validation that the kind
exists and that an app-shipped provider matches the selector happens against the complete ledger
during finalize.

### Settings model

`agentworks.config.models` owns the settings-only model:

```python
@dataclass(frozen=True)
class ResourcePolicyConfig:
    disabled: tuple[ResourceSelector, ...] = ()

@dataclass(frozen=True)
class Config:
    # Existing fields omitted.
    resource_policy: ResourcePolicyConfig = field(default_factory=ResourcePolicyConfig)
```

The loader accepts this exact shape:

```toml
[resource_policy]
disabled = ["apt-package/gh", "user-install-command/nvm"]
```

Absence means an empty tuple. `[resource_policy]` must be a table, `disabled` must be an array of
strings, and no other key is accepted. Duplicate selectors are a hard error naming both array
positions. Silent deduplication would conceal an operator mistake in a safety policy.

Parsing validates shape and canonical spelling only. It does not import `KIND_REGISTRY`, inspect
plugins, or decide whether a provider exists.

### Claims and provider identity

`agentworks.resources.providers` is a new policy and provenance module. It owns these immutable
records:

```python
class ProviderClass(Enum):
    OPERATOR = "operator"
    SHIPPED_BUILTIN = "shipped-builtin"
    SHIPPED_SYSTEM_PLUGIN = "shipped-system-plugin"
    SYNTHESIZED_DEFAULT = "synthesized-default"

@dataclass(frozen=True)
class ProviderIdentity:
    provider_class: ProviderClass
    variant: str
    source: str | tuple[str, str] | None
    plugin: str | None
    file: Path | None
    line: int | None

    def sort_key(self) -> tuple[str, ...]: ...

@dataclass(frozen=True)
class ProviderClaim:
    selector: ResourceSelector
    provider: ProviderIdentity
    origin: Origin
    resource: object
    category: Literal["declarable", "capability"]

    @property
    def key(self) -> ProviderClaimKey: ...

@dataclass(frozen=True)
class ProviderClaimKey:
    selector: ResourceSelector
    provider: ProviderIdentity

    def sort_key(self) -> tuple[str, ...]: ...
```

`ProviderIdentity.from_origin` copies the exact stamped `Origin` fields. It does not resolve paths,
fold case, or discard line numbers. Two publications have the same provider only when their provider
identities compare equal. `sort_key` renders each optional field to a stable string tuple for
errors; it does not rely on ordering unlike union types such as `str | tuple[str, str]`.
`ProviderClaimKey` is the immutable identity used by idempotent collapse, disablement maps, active
outcomes, and substitutions. No phase switches to a selector-only key while multiple claims still
exist. Its explicit sort key is `(selector.kind, selector.name, *provider.sort_key())`; callers
never ask dataclass field ordering to compare `ProviderIdentity.source`'s non-orderable union.

Provider class and resource category are separate axes:

| Origin and kind                                  | Provider class          | Category     | Explicit policy applies |
| ------------------------------------------------ | ----------------------- | ------------ | ----------------------- |
| `operator-declared` declarable row               | `OPERATOR`              | `declarable` | No                      |
| `built-in` declarable row                        | `SHIPPED_BUILTIN`       | `declarable` | Yes                     |
| `system-plugin` declarable row                   | `SHIPPED_SYSTEM_PLUGIN` | `declarable` | Yes                     |
| `built-in` capability row                        | `SHIPPED_BUILTIN`       | `capability` | Yes                     |
| `system-plugin` capability row                   | `SHIPPED_SYSTEM_PLUGIN` | `capability` | Yes                     |
| Reserved or reference-driven `auto-declared` row | `SYNTHESIZED_DEFAULT`   | `declarable` | No                      |

Capability is not another provider class. A capability row still has a built-in or system-plugin
provider identity, so plugin opt-in and explicit disablement apply consistently. Operator manifests
cannot declare capability kinds, and capability implementation seating continues to reject duplicate
implementations before publication. The claim resolver remains the backstop for a bad publisher that
nevertheless contributes multiple capability rows under one selector.

External-plugin origins remain out of scope because that origin variant is not constructible.

### Disablement provenance

The same module owns retained disablement data:

```python
@dataclass(frozen=True, order=True)
class DisabledMark:
    rank: int
    source: str
    reason: str
    remediation: str

@dataclass(frozen=True)
class Disablement:
    marks: tuple[DisabledMark, ...] = ()

    @property
    def is_disabled(self) -> bool:
        return bool(self.marks)
```

`reason` states why the claim is disabled. `remediation` is an exact next action suitable for error,
describe, and doctor output. Marks are sorted by `(rank, source, reason, remediation)`, and exact
duplicate marks collapse. Source ranks are framework constants:

| Rank | Source            | Meaning                                                |
| ---- | ----------------- | ------------------------------------------------------ |
| 10   | `resource-policy` | Exact selector appears in `[resource_policy].disabled` |
| 20   | `plugin-opt-in`   | System plugin is absent from `[plugins].system`        |

Adding another source requires a unique rank and source identifier. Registry rejects duplicate
external source identifiers or ranks, and any external collision with the internal resource-policy
rank or identifier, as `StateError`; caller order cannot change retained mark order.

An `EnablementSource` evaluates one logical claim at a time:

```python
@dataclass(frozen=True)
class EnablementSource:
    rank: int
    source: str
    evaluate: Callable[[ProviderClaim], DisabledMark | None]
```

The Registry validates that a returned mark repeats its source's rank and identifier. A source
cannot mark a different selector or replace another source's result.

Explicit resource policy is not an externally injected `EnablementSource`. Registry derives its
`resource-policy` mark internally from its stored `ProviderResolutionPolicy` for every
`SHIPPED_BUILTIN` and `SHIPPED_SYSTEM_PLUGIN` claim, before provider choice. It never marks operator
or synthesized claims. This makes the mark inseparable from collision authorization, including for
direct `Registry.empty(policy=...)` callers.

`plugin_enablement_source` remains externally injected because plugin opt-in is application state.
It marks every claim whose provider class is `SHIPPED_SYSTEM_PLUGIN` and whose plugin is not
enabled. Both mechanisms therefore apply to a displaced plugin claim as well as to a surviving one.

### Provider substitutions

```python
@dataclass(frozen=True)
class ProviderSubstitution:
    selector: ResourceSelector
    active_claim: ProviderClaimKey
    displaced_claim: ProviderClaimKey
    active_origin: Origin
    displaced_origin: Origin
    authority: Literal["resource-policy", "synthesized-default-contract"]
    displaced_disablement: Disablement
```

The explicit disable-and-redeclare flow stores `authority="resource-policy"` and every mark on the
displaced shipped claim. An operator override of a reserved synthesized default stores
`authority="synthesized-default-contract"`; its disablement is empty because it was displaced by the
kind's default contract, not disabled by settings.

One immutable `Mapping[ResourceSelector, ProviderSubstitution]` is committed at finalize. Registry
exposes `substitution_for(kind, name)` and `substitutions`; `DependencyGraph` stores the same frozen
mapping and exposes `substitution_of(kind, name)`. No projection infers a substitution from the
active row's origin.

## Registry policy and lifecycle

### Construction

Registry stays independent of `Config`. Bootstrap translates settings into an injected policy:

```python
@dataclass(frozen=True)
class ProviderResolutionPolicy:
    disabled: tuple[ResourceSelector, ...] = ()

    @property
    def disabled_set(self) -> frozenset[ResourceSelector]: ...

policy = ProviderResolutionPolicy(disabled=config.resource_policy.disabled)
registry = Registry.empty(policy=policy)
```

The name avoids collision with `agentworks.capabilities.descriptor.RegistryPolicy`, which describes
capability seating rather than resource-provider resolution. The tuple preserves operator order for
exact replacement snippets. Membership checks use the derived frozen set. Policy construction
rejects duplicates defensively even though the config loader already rejects them, so direct library
assembly cannot create two contradictory positions for one selector.

`Registry.empty()` retains an all-enabled default `ProviderResolutionPolicy` for direct tests and
library assembly. Registry internally derives resource-policy marks from its stored policy.
Bootstrap injects only plugin-opt-in sources at finalize. The Registry does not import config or
plugin modules.

### Publication ledger

The mutable publication state is:

```python
self._claims: dict[ResourceSelector, list[ProviderClaim]]
self._selector_order: list[ResourceSelector]
self._resources: dict[str, dict[str, object]] | None = None
self._active_claims: Mapping[ResourceSelector, ProviderClaimKey] | None = None
self._graph: DependencyGraph | None = None
self._substitutions: Mapping[ResourceSelector, ProviderSubstitution] | None = None
self._frozen = False
```

`Registry.add(kind, name, resource, origin)` validates the selector, stamps the resource with the
origin, classifies the claim, and appends it to `_claims`. On the selector's first claim, it also
appends the selector to `_selector_order`. It never chooses a winner and has no `weak` parameter.
Plugin manifests publish unconditionally and strongly.

Provider choice, ambiguity detection, and error ordering do not use `_selector_order`. After every
outcome has been computed, Registry projects the nested active resource map by iterating
`_selector_order`. This preserves the current first-selector encounter order for the later reference
walk, including first-referrer origin attribution, inbound edge order, and auto-declared description
wording. Reversing providers under one selector cannot change the result. Reversing which distinct
selector is first may intentionally change first-referrer attribution exactly as it does today.

Pre-finalize `lookup`, `iter_kind`, `iter_kind_items`, and `iter_kinds` raise `StateError` because
there is no resolved resource map yet. Production assembly does not query during publication. Tests
that currently use pre-finalize lookup to observe a pairwise winner must instead finalize and assert
the resolved result or error. A public claims inspection API is not added.

### Exact idempotent republication

Before provider resolution, claims are grouped by selector and then by `ProviderIdentity`.

- Repeated claims with equal provider identity and equal stamped resource value collapse to one
  logical claim.
- Equal provider identity with unequal resource values is an attributed ambiguity error. A source
  cannot redefine its own row during one build.
- Different identities never collapse merely because their resource values happen to compare equal.
  Provider identity, not payload equality, determines provenance.

This preserves legitimate idempotent republication without reviving built-in-over-built-in
last-writer-wins behavior.

### Reserved synthesized defaults

The existing `ResourceKind.auto_declare_names` declaration remains the explicit kind contract for a
reserved synthesized default and its operator override.

For each reserved selector, finalize adds one synthesized claim to its local working ledger when no
app-shipped claim exists. It does this even when an operator claim exists, so the operator-over-
default substitution is retained. It does not synthesize when a shipped claim occupies the reserved
selector because that shipped provider, rather than the fallback, supplies the app's default.

Reference-driven synthesis remains a later materialization step. It occurs only for a selector with
no resolved claim and only when an enabled referrer drives materialization. Such a claim is the sole
provider for its selector, so it does not reopen provider resolution.

The old `ResourceKind.builtin_override` flag is removed. App-shipped declarations now use the
universal explicit disable-and-redeclare contract. Synthesized defaults use `auto_declare_names`,
their existing explicit fallback contract. Keeping `builtin_override` would describe a silent
shipped-provider override that no longer exists.

## Order-independent resolution

Finalize performs one provider-resolution pass over selectors sorted by `(kind, name)`. Every
logical claim has its complete `Disablement` before this pass. Publisher encounter order is not an
input.

For one selector, define:

- `operators`: logical `OPERATOR` claims
- `shipped`: logical `SHIPPED_BUILTIN` and `SHIPPED_SYSTEM_PLUGIN` claims
- `defaults`: logical `SYNTHESIZED_DEFAULT` claims
- `policy_match`: the selector appears in `ProviderResolutionPolicy.disabled_set`

The resolver applies these rules:

1. More than one operator, shipped, or synthesized-default claim is ambiguous. It fails naming every
   provider identity, even if explicit policy exists.
2. Any capability claim paired with an operator or synthesized claim is a publisher invariant
   violation. Operator manifests should have rejected the capability kind earlier.
3. Exactly one shipped claim and no operator claim resolves to the shipped row. Its binary
   enablement derives from all its marks.
4. Exactly one operator and exactly one shipped claim without `policy_match` is a provider
   collision. Plugin non-opt-in does not authorize replacement.
5. Exactly one operator and exactly one shipped claim with `policy_match` resolves to the operator
   row and retains a `resource-policy` substitution for the shipped row.
6. Exactly one operator and no shipped or default claim resolves to the operator row.
7. Exactly one synthesized default and no operator resolves to the synthesized row.
8. Exactly one operator and exactly one synthesized default resolves to the operator and retains a
   `synthesized-default-contract` substitution. This case is valid only for a selector declared by
   the kind's `auto_declare_names` contract.
9. Any other non-empty shape is ambiguous and fails. An empty shape is absent, not a resolved row.

The policy selector is considered matched only when at least one shipped claim has that selector,
including a shipped claim retained as displaced. After resolution:

- a configured selector with no claims is `unknown`;
- a configured selector with operator or synthesized claims but no shipped claim is `inapplicable`;
- a selector with a shipped claim is matched even when another ambiguity causes resolution to fail.

Unknown and inapplicable selectors are hard `ConfigError`s. Errors are collected without mutating
Registry state, sorted by selector and error code, and rendered in one attributed configuration
error. Provider identities within each clause are sorted by `ProviderIdentity.sort_key`. This makes
failures deterministic while letting the operator fix all policy typos in one edit.

### Resolution pseudocode

```python
def resolve_claims(claim_ledger, selector_order, policy, plugin_sources):
    working = copy_claims(claim_ledger)
    logical = collapse_exact_republications(working)
    working_order = list(selector_order)
    add_reserved_default_claims(logical, working_order)

    ordered_sources = validate_and_sort_plugin_sources(plugin_sources)
    disablement = {
        claim.key: Disablement(
            dedupe_sorted(
                [
                    *policy_marks_for(claim, policy),
                    *(
                        mark
                        for source in ordered_sources
                        if (mark := source.evaluate(claim)) is not None
                    ),
                ]
            )
        )
        for claim in every_claim(logical)
    }

    outcomes = {}
    substitutions = {}
    matched_policy = set()
    problems = []

    for selector in sorted(logical):
        claims = logical[selector]
        operators, shipped, defaults = classify(claims)
        if shipped and selector in policy.disabled_set:
            matched_policy.add(selector)
        outcome = resolve_one(selector, operators, shipped, defaults, policy, disablement)
        if outcome.problem:
            problems.append(outcome.problem)
            continue
        outcomes[selector] = outcome.active.key
        if outcome.substitution:
            substitutions[selector] = outcome.substitution

    problems.extend(policy_match_problems(policy.disabled_set - matched_policy, logical))
    if problems:
        raise ConfigError(render_sorted(problems))

    resources = project_active_rows(working_order, outcomes, logical)
    active_disablement = {key: disablement[key] for key in outcomes.values()}
    return resources, outcomes, active_disablement, substitutions, working_order
```

`add_reserved_default_claims` appends a selector to `working_order` only when it was not already
encountered. Error computation iterates sorted selectors, while `project_active_rows` iterates
`working_order`. This intentional split keeps provider results order-independent without changing
the established resource-walk order.

## Finalize graph rules

Provider resolution precedes dependency extraction. The remaining finalize stages operate on a local
resolved resource map.

### Owner-validated reference walking

Dependencies are extracted for every active resolved row so describe and graph provenance remain
complete. The walker never trusts `ResourceReference.source` to establish ownership:

```python
def walk_claim(owner: ResourceSelector, claim: ProviderClaim, context: FinalizeContext):
    for ref in dependencies(claim.resource, context):
        if ref.source != (owner.kind, owner.name):
            raise StateError(owner_source_mismatch(owner, claim.origin, ref.source))
        yield OwnedReference(owner=owner, reference=ref)
```

Every enablement lookup, synthesis gate, outbound-map key, active-edge decision, and shadow decision
uses `OwnedReference.owner` or `ProviderClaim.key`. `ResourceReference.source` remains useful only
after the equality invariant has passed. `ResourceReference.declarer` remains attribution for the
declaration an operator must edit; it never changes edge ownership.

Name and graph correctness are unconditional for all active rows, including disabled rows:

- An absent target under an error miss policy is the ordinary missing-reference `ConfigError`.
- An absent target outside an auto-declare allowlist is the existing disallowed-name error.
- Every active row participates in cycle detection regardless of enablement. Enabled-only,
  disabled-only, and mixed cycles all fail.
- An enabled owner referencing a disabled present target is a hard finalize error.
- A disabled owner may reference an enabled or disabled present target. The valid edge is retained,
  but the owner does not drive synthesis or readiness propagation.

Thus disabled means inert for availability and execution, not exempt from declaration correctness.
When the declarer's active row has an operator origin, an error includes its manifest path and line
through `format_origin_line`.

For an enabled source to disabled target, the error includes:

1. `source-kind/source-name` and the reference `usage`;
2. the disabled `target-kind/target-name`;
3. every retained disable reason in mark order;
4. every exact remediation in the same order;
5. the alternatives to remove or change the reference;
6. when the disabled target is app-shipped and declarable, the explicit disable-and-redeclare flow.

Capability kinds have no operator declaration surface. Their errors offer only re-enabling the
provider or removing or changing the reference, never an impossible same-name YAML declaration.

### Active graph sequence and late synthesis

The initial active graph uses this exact sequence:

1. Walk every active row with actual-owner validation and build the local outbound and inbound
   inputs in preserved selector encounter order.
2. Resolve misses. Error-policy misses and disallowed auto-declare names fail for every active
   owner, including a disabled owner. Permitted auto-declare misses enter the deferred set.
3. Run unconditional cycle detection across every active present row, including enabled-only,
   disabled-only, and mixed graphs.
4. Reject every enabled-owner-to-disabled-target edge.
5. Fold readiness for the active present rows.
6. Materialize permitted deferred rows to a fixed point under the existing readiness gate.

A deferred target is driven only by a present referrer whose actual owner is enabled and whose
readiness is either ready, or unavailable specifically because `probe_host_readiness=False`
suppressed probing. A blocked referrer does not drive synthesis. A genuinely unavailable referrer
when probing ran does not drive synthesis. A disabled referrer does not drive synthesis. With mixed
referrers, any one qualifying referrer is sufficient.

For each late synthesized row, before folding its readiness, finalize:

1. stamps and validates the synthesized claim's provider and actual owner;
2. walks it and validates every `ref.source` against that owner;
3. resolves its misses, adding any permitted new deferred targets;
4. reruns unconditional cycle detection over the whole active present graph including the new row;
5. reruns the enabled-owner-to-disabled-target check for all edges that can be affected by the new
   row;
6. folds readiness for the new row, then continues the bounded fixed-point loop.

No late row becomes visible merely because it was constructed. A bad owner, miss, cycle, disabled
edge, or readiness fold aborts the transaction before the local row map is committed.

### Displaced shipped-claim validation

Provider substitution must not let an operator declaration hide a malformed app-shipped declaration.
Every displaced shipped claim receives a separate claim-aware shadow validation before graph
construction:

1. Build a temporary shadow row map that replaces the active operator claim at the selector with the
   displaced shipped claim. Other selectors keep their active rows.
2. Build that claim's own `FinalizeContext` from the shadow map, preserving its own origin and any
   inheritance reads.
3. Walk only the displaced claim with its actual selector as owner. A mismatched `ref.source` is
   `StateError`.
4. Run its unconditional `validate_config` against the shadow context, with errors attributed to the
   shipped origin.
5. Validate every shadow reference whose target is missing under an error policy or whose name is
   outside an auto-declare allowlist. A permitted auto-declare miss is accepted but never drives
   synthesis.
6. Create a temporary outbound view by replacing the active claim's outbound edges with the shadow
   claim's edges. Run cycle detection across all active selectors in that view. This catches a
   displaced self-cycle and a cycle through active targets.

Shadow validation runs once per displaced claim in
`sorted(claims, key=lambda claim: claim.key.sort_key())` order. A shadow claim never contributes an
edge, inbound reference, readiness verdict, synthesized target, guide topic, or active graph node.
Each shadow view contains only that one displaced substitution, so validation of two substitutions
cannot affect one another.

After late materialization reaches its fixed point, finalize runs every shadow validation, then runs
unconditional `validate_config` for every active resolved row, constructs the frozen graph and
projections in local variables, and commits only when all stages have succeeded.

Normal app packaging and default-registry tests validate the same shipped cohort without operator
substitutions. Shadow tests add malformed config, error-policy miss, disallowed auto-declare name,
self-cycle, cycle through an active target, and no-edge-leak cases.

### Settings references

`config.references.validate_setting_references` remains a post-finalize presence check. A setting
that names a disabled row is valid because the row exists. Settings references do not become
`ResourceReference` edges and do not participate in the enabled-edge invariant.

Doctor reads `setting_references(config)` and reports a warning when a referenced row is disabled,
including every mark and remediation. It does not turn that state into a config-load failure.

### Frozen graph

`DependencyGraph._Node` gains `claim: ProviderClaimKey` and `disablement: Disablement`.
`Disablement` deliberately exposes only `is_disabled`, avoiding an import from providers back into
graph. Graph construction obtains disablement by the active `ProviderClaimKey`, not by selector
while other provider keys still exist. The graph-owned `Enablement` enum remains the public binary
axis, and `enablement_of` derives it from `node.disablement.is_disabled`. The graph adds:

```python
def disablement_of(self, kind: str, name: str) -> Disablement: ...
def substitution_of(self, kind: str, name: str) -> ProviderSubstitution | None: ...
```

Missing nodes retain the current tolerant binary behavior: `enablement_of` returns enabled.
`disablement_of` returns an empty `Disablement`. `substitution_of` returns `None`. Existing callers
that only need a boolean do not change; provenance consumers stop reconstructing reasons.

## Exact remediation rendering

The plugin enablement source receives the complete `Config`, so it computes the exact replacement
section once when it creates a mark. The helper preserves the tuple's current order and every
existing entry, appending the required plugin only when absent. Strings use a TOML basic-string
encoder, not interpolation.

For an empty list and required plugin `apt`, the exact remediation is:

```toml
[plugins]
system = ["apt"]
```

For `enabled_system_plugins == ("onepassword", "claude")`, it is:

```toml
[plugins]
system = ["onepassword", "claude", "apt"]
```

The surrounding message calls this a replacement for the complete `[plugins]` section so an operator
does not paste a second table header into the same file. The snippet is tested by parsing it with
`tomllib`, including plugin names requiring TOML escaping. Rendering must never emit only
`system = ["apt"]` when other plugins are enabled.

An explicit-policy mark similarly names the exact selector and renders the replacement
`[resource_policy]` section with that selector removed while preserving the remaining configured
order. A collision that needs authorization renders a complete replacement section, preserving the
configured order and appending the missing selector once. For an empty current policy, that is:

```toml
[resource_policy]
disabled = ["apt-package/gh"]
```

It also states that the operator must keep a same-name YAML declaration. The policy snippet alone
disables the shipped row; it does not create a replacement. When other selectors already exist, the
snippet includes them instead of emitting a second table header or discarding policy:

```toml
[resource_policy]
disabled = ["user-install-command/nvm", "apt-package/gh"]
```

Tests parse policy additions and removals with `tomllib` and assert exact ordered preservation.

## Projection contracts

All projections read the finalized graph. None inspect config, plugin registration, or origin to
reconstruct disablement.

Guide constructs one visibility-filtered catalog before names-only output or direct-request
validation:

- Bare-kind schema topics remain visible because they describe a kind, not one provider row.
- Capability implementation schema topics are `ImplementationAnchor(kind, name)` topics and are
  removed when that resolved capability row is disabled.
- Authored topics with `ResourceAnchor(kind, name)` or `ImplementationAnchor(kind, name)` are
  removed when their resolved row is disabled.
- `ConceptAnchor` topics remain visible. This includes the conceptual plugin topics that teach how
  to enable or replace their resources.
- Other authored kind-level topics remain visible.
- Dynamic resource topics include only enabled resolved rows.

The filtered authored, schema, and dynamic name sets are the sole input to `--names-only`, unknown
topic suggestions, and direct-request validation. No branch consults raw `schema.names()`. If live
Registry facts are unavailable, row-anchored topics are unavailable rather than admitted from an
unfiltered schema set; config-free concept and bare-kind teaching remains available with the
existing fail-soft system diagnostic.

| Surface                            | Contract                                                                                                                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `resource list`                    | Excludes rows whose `Disablement` is non-empty. A substituted selector appears once as its active operator row.                                                          |
| `resource list --include-disabled` | Adds disabled resolved rows and marks them disabled. Displaced claims do not become duplicate rows.                                                                      |
| `resource list --names-only`       | Uses the same default filtering and therefore remains the resource completion source.                                                                                    |
| `resource describe KIND/NAME`      | Always renders a resolved row, including every disable mark and remediation plus any substitution's active and displaced origins.                                        |
| `doctor`                           | Reports disabled resolved rows, all marks, sanctioned substitutions, and disabled settings targets. It never reports a silent provider change because none can finalize. |
| Dynamic resource guide topics      | Include only enabled resolved rows. A direct request for a disabled dynamic topic follows the ordinary unknown-topic path.                                               |
| Capability schema topics           | Keep bare-kind schema teaching; filter disabled implementation anchors from names and direct requests.                                                                   |
| Authored guide topics              | Filter disabled resource and implementation anchors; retain concept and kind anchors, including plugin remediation topics.                                               |
| Guide `--names-only`               | Emits the same filtered authored, schema, and dynamic names accepted by direct requests. It remains the guide completion source.                                         |
| Shell completions                  | Continue calling the two `--names-only` commands and inherit their filtering. No shell-specific policy logic is added.                                                   |

`ensure_reference_enabled` remains a use-time defensive guard for persisted identities and direct
lookups that are not represented by current resource edges. It renders retained marks rather than
deriving a plugin from origin. The finalize invariant becomes authoritative for declared
resource-to-resource edges.

## Failure and transaction guarantees

Publication mutates only `_claims`. Finalize performs default claim creation, idempotent collapse,
mark evaluation, provider resolution, reference extraction, miss handling, synthesis, cycle
detection, enabled-edge validation, readiness folding, resource validation, graph construction, and
projection construction in local working structures. `_selector_order` is copied before reserved or
late synthesized selectors are appended.

Only after every stage succeeds does finalize atomically assign `_resources`, `_active_claims`,
`_graph`, and `_substitutions`, then set `_frozen = True`. If any stage raises:

- no resolved map, active-claim map, graph, or substitution projection becomes visible;
- the original claim ledger and selector-order list are unchanged, including no leaked reserved or
  late synthesized claims;
- no VM, filesystem, credential, network, or database operation has occurred;
- callers cannot query a partially resolved registry;
- a retry over the same ledger is deterministic, though a bad published claim still requires
  constructing a corrected Registry because claims have no removal API.

Enablement sources are pure claim-to-mark functions. A source that raises aborts finalize under the
same transaction. Projection rendering occurs only after freeze and cannot alter policy state.

Stage-injection tests force a failure immediately after reserved-default creation, provider
resolution, reference extraction, late synthesis, active and shadow validation, and graph
construction. Each test snapshots the private claim ledger and selector order, asserts that
`lookup`, `graph`, and substitutions remain unavailable after failure, asserts exact snapshot
equality, then retries. The retry must produce the same success or the same deterministic error as a
fresh Registry with the identical publishers. A graph object built in a local variable before an
injected failure must not become observable.

## File-by-file implementation map

### Core models and config

- `cli/agentworks/resource_names.py`: add `ResourceSelector` and its canonical parser.
- `cli/agentworks/config/models.py`: add `ResourcePolicyConfig` and `Config.resource_policy`.
- `cli/agentworks/config/loaders_policy.py`: add the strict `[resource_policy]` loader.
- `cli/agentworks/config/load.py`: admit the top-level table, call the loader, and pass its result
  to both config construction paths.
- `cli/agentworks/config/__init__.py`: export the public settings type as needed.
- `cli/agentworks/sample-config.toml`: document the empty policy and disable-and-redeclare example.

### Claim resolution and graph

- `cli/agentworks/resources/providers.py`: add provider classification, identity, claims,
  disablement, plugin enablement sources, `ProviderResolutionPolicy`, substitutions, source
  ordering, and pure resolution helpers. It does not import graph.
- `cli/agentworks/resources/registry.py`: replace `_resources`-during-publication, `_weak`, and
  pairwise collision methods with `_claims`; make finalize transactional; run claim resolution
  before the existing graph work; enforce enabled edges; expose substitutions.
- `cli/agentworks/resources/graph.py`: move the transient `DisabledMark` contract to the provider
  module, retain `Disablement` and substitutions on the frozen graph, derive graph-owned
  `Enablement` from `Disablement.is_disabled`, and update readiness inputs to consume complete
  disablement.
- `cli/agentworks/resources/kind.py`: remove the retired `builtin_override` protocol field and its
  prose. Preserve `auto_declare_names` as the synthesized-default contract.
- `cli/agentworks/apt.py`, `cli/agentworks/install_commands.py`, `cli/agentworks/agents/kinds.py`,
  `cli/agentworks/sessions/kinds.py`, `cli/agentworks/workspaces/kinds.py`,
  `cli/agentworks/secrets/kinds.py`, `cli/agentworks/vms/kinds.py`,
  `cli/agentworks/capabilities/vm_platform/kinds.py`,
  `cli/agentworks/capabilities/git_credential/kinds.py`, and
  `cli/agentworks/capabilities/harness_integration/kinds.py`: remove every concrete
  `builtin_override` strategy.
- `cli/agentworks/resources/__init__.py`: export the stable policy and provenance query types.
- `cli/agentworks/bootstrap.py`: inject `ProviderResolutionPolicy` at construction and only the
  config-bound plugin opt-in source at finalize.

### Publishers

- `cli/agentworks/plugins/publish.py`: publish every manifest strongly and remove `weak` plumbing.
- `cli/agentworks/plugins/enablement.py`: evaluate individual system-plugin claims and produce exact
  plugin remediation marks.
- `cli/agentworks/manifests/loader.py` and `cli/agentworks/manifests/package.py`: remove `weak`
  parameters and forward only claim data.
- Other built-in, capability, and operator publishers keep calling `Registry.add`; they gain no
  policy branches.

### Operator projections

- `cli/agentworks/resources/inspect.py`: project complete marks and substitutions for list and
  describe instead of reconstructing plugin state from origin.
- `cli/agentworks/doctor.py`: add disabled-resource, substitution, and disabled-setting-reference
  findings backed by graph provenance.
- `cli/agentworks/config/references.py`: retain presence validation and expose the existing settings
  reference inventory to doctor.
- `cli/agentworks/resources/access.py`: render retained marks in defensive use-time errors.
- `cli/agentworks/guide/service.py`: construct one visibility-filtered authored, schema, and dynamic
  catalog; filter disabled resource and implementation anchors plus onboarding resource iteration;
  retain concept and bare-kind topics.
- Completion implementations require no logic change. Their dynamic command contracts receive
  regression coverage.

### Permanent documentation and code prose

The framework phase removes every statement that teaches weak publication, pairwise provider wins,
or `builtin_override`. The implementation inventory includes:

- `docs/adrs/0021-system-plugins.md`, `cli/agentworks/plugins/README.md`, `cli/README.md`,
  `docs/guides/resources.md`, `docs/guides/idempotency.md`, and `docs/guides/upgrading-to-0.14.md`;
- `cli/agentworks/capabilities/git_credential/README.md` and
  `cli/agentworks/capabilities/harness_integration/README.md`;
- module and function prose in `cli/agentworks/bootstrap.py`, `cli/agentworks/apt.py`,
  `cli/agentworks/install_commands.py`, `cli/agentworks/manifests/loader.py`,
  `cli/agentworks/manifests/package.py`, and `cli/agentworks/plugins/publish.py`;
- plugin module prose in `cli/agentworks/plugins/azure/__init__.py`,
  `cli/agentworks/plugins/claude/__init__.py`, and `cli/agentworks/plugins/codex/__init__.py`;
- built-in manifest ownership comments in `cli/agentworks/manifests/builtin.py`,
  `cli/agentworks/manifests/builtin/README.md`, and the YAML files under
  `cli/agentworks/manifests/builtin/`;
- `cli/agentworks/resources/kind.py` plus every concrete kind strategy listed in the core map above.

A drift test runs the equivalent of:

```bash
rg -n 'weak=True|weak rows|publish weak|published weak|add-if-absent|builtin_override' \
  cli/agentworks docs --glob '!docs/sdd/**' --glob '!CHANGELOG.md'
```

The expected result is empty after intentional unrelated matches are made structurally impossible by
the specific patterns. Historical SDDs and changelog entries remain historical and are excluded.

### Tests to migrate or replace

- Delete `cli/tests/resources/test_registry_weak_rows.py`; replace it with claim-ledger and
  disablement tests.
- Rewrite `cli/tests/resources/test_registry_collisions.py` around finalize-time, order-independent
  outcomes and remove `builtin_override` expectations.
- Update registry lifecycle tests that assert pre-finalize lookup. Publication tests should finalize
  before reading rows.
- Update plugin publication and enablement tests to assert strong publication, all retained marks,
  exact remediation snippets, and displaced provenance.
- Update resource inspect, doctor, guide, completions, config, settings-reference, graph, readiness,
  and packaged-plugin tests described below.

No compatibility shim keeps `weak=` available. It is an internal framework argument whose behavior
violates C4. A stale caller receives an immediate Python argument error during the implementation
branch, and all in-repository callers move in the same always-green phase.

## Test matrix

### Config and selectors

- Missing `[resource_policy]` produces an empty policy.
- Empty `disabled` is valid.
- Non-table section, non-array value, non-string member, extra key, and duplicate selector fail.
- Leading or trailing whitespace, uppercase, empty component, multiple slash, invalid character, and
  over-limit component fail without normalization.
- Unknown kind, unknown name, operator-only selector, and synthesized-only selector fail at finalize
  with distinct unknown or inapplicable wording.
- A selector matching a surviving or displaced built-in or system-plugin claim is accepted.

### Claim identity and resolution

- Exact same identity and equal resource republish collapses in either order.
- Same identity with different resource value fails and names the provider.
- Two operator identities fail and name both manifest locations.
- Two shipped identities fail and name both sources, including built-in plus plugin and two plugins.
- Capability duplicate claims fail as a publisher ambiguity.
- One shipped plus one operator fails without policy in both publication orders.
- Plugin disabled plus operator still fails without explicit policy.
- One shipped plus one operator with exact policy resolves to operator in both orders and retains
  the displaced origin and all marks.
- Reversing provider publication under an already-encountered selector does not change active
  provider, error text, disablement, substitutions, resource-map order, or reference attribution.
- Reversing first encounter of two distinct referrer selectors reverses their active map and inbound
  order, and the first-referrer origin used by synthesized descriptions follows that order.
- Explicit policy never selects among two operator or two shipped claims.
- Reserved synthesized default resolves when alone; an operator overrides it under the kind contract
  with substitution provenance; explicit resource policy cannot match it.
- Former `builtin_override="allow"` shipped collisions now require explicit policy; former
  `reserved` declarable built-ins can use the same explicit flow.

### Disablement and references

- Built-in and system-plugin claims receive explicit-policy marks; operator and synthesized claims
  do not.
- Direct `Registry.empty(policy=...)` builds retain policy marks on shipped rows and displaced
  substitutions without an externally injected resource-policy source.
- A disabled plugin claim also named in policy retains both marks in rank order.
- Reversing publisher order and external plugin-source order does not change marks or resolution.
- Duplicate marks collapse; duplicate source rank or identifier is a framework error.
- Active, late synthesized, and displaced shadow walkers reject a `ResourceReference.source` that
  does not equal the actual owner, and the error names the claim origin.
- Enabled source to disabled target fails with both selectors, declaration location, usage, every
  cause, and every remediation.
- Enabled edges and defensive use-time guards for disabled declarable targets offer the paired
  explicit-disable and same-name declaration alternative. The same cases for disabled capability
  targets offer only re-enable or remove-or-change-reference actions.
- Disabled source to disabled and enabled targets is inert and retains graph provenance.
- Disabled source to a missing error-policy target still fails; a permitted auto-declare target is
  not materialized by a disabled source.
- Ready enabled referrers drive permitted synthesis. Blocked, genuinely unavailable, and disabled
  referrers do not. An enabled referrer unavailable specifically because probe suppression is active
  does drive synthesis. Mixed referrers materialize when any one qualifies.
- Every late row reruns owner validation, miss resolution, full cycle detection, and enabled-edge
  validation before its readiness fold.
- Settings references to disabled rows pass config validation and appear as doctor warnings.
- Enabled-only, disabled-only, and mixed active cycles all fail. Pure schema validation remains
  unconditional for every present active row.
- A displaced shipped claim with malformed config, an error-policy miss, a disallowed auto-declare
  name, a self-cycle, or a cycle through an active target fails with shipped-origin attribution.
- A valid displaced claim contributes no active edge, inbound reference, readiness verdict,
  synthesized target, or guide topic.
- Default-registry and installed-package tests validate every app-shipped claim that is not
  displaced, so the shadow path is additional coverage, not the only validation of shipped data.

### Remediation and projections

- Plugin remediation for zero existing plugins parses and equals `("apt",)`.
- Plugin remediation preserves one, several, duplicated, and escapable existing plugin strings,
  appends the required plugin exactly once, and parses with `tomllib`.
- Collision remediation includes the exact resource-policy selector and the same-name declaration
  requirement.
- Collision remediation preserves an already-populated resource-policy list and parses with
  `tomllib`.
- Default list and both completion streams omit disabled rows.
- `--include-disabled` adds a disabled row once with its state marker.
- Describe renders all marks and both origins for substitutions.
- Doctor renders disabled resources, substitutions, and disabled settings targets from retained
  data.
- Dynamic guide topics, capability implementation schema topics, and authored resource or
  implementation anchors omit disabled rows in names-only and direct requests.
- Bare-kind schema topics and conceptual `plugin/apt/...` and `plugin/install-command/...` topics
  remain visible.
- Names-only output, direct request acceptance, and unknown-topic suggestions use the identical
  filtered catalog when Registry is available or unavailable.

### Transactional stages

- Injected failures after reserved-default creation, claim resolution, reference extraction, late
  synthesis, active and shadow validation, and graph construction expose no resources, graph,
  substitutions, selector-order additions, or synthesized claims.
- Each failed Registry retains its exact publication snapshot, and retry has the same deterministic
  result as a fresh Registry.

### Regression and packaging boundary

- Existing readiness, synthesis, reference attribution, settings presence, capability seating, and
  registry freeze suites stay green.
- All current core resources other than the later 16-row manifest move resolve to equal models and
  origins expected for their provider.
- Apt, install-command, snap, mise, dotfiles, tmuxinator, Claude, and initializer execution tests do
  not require runner changes in these phases.

## Rejected alternatives

- **Resolve each collision in `Registry.add`:** rejected because publication order chooses which
  origins and disable marks survive.
- **Keep weak rows as an optimization:** rejected because a weak claim can disappear before policy
  sees it, making replacement silent.
- **Treat plugin non-opt-in as replacement authorization:** rejected because availability and
  provider replacement are separate operator decisions.
- **Keep only the highest-priority disable reason:** rejected because describe, doctor, and errors
  must explain every applicable cause and action.
- **Apply enablement after choosing the active row:** rejected because displaced claim provenance
  would be incomplete and policy applicability could depend on the winner.
- **Allow a policy selector to disable an operator declaration:** rejected because the sanctioned
  flow needs the same-name operator replacement to remain active.
- **Let explicit policy choose among multiple shipped providers:** rejected because the selector
  says what name is disabled, not which ambiguous publisher wins.
- **Preserve `builtin_override`:** rejected because it encodes silent shipped-provider replacement.
  The universal explicit policy and the separate synthesized-default contract make the old flag both
  unsafe and misleading.
- **Fail settings references to disabled rows:** rejected because settings retain their established
  presence-not-availability contract.
- **Reject every otherwise-valid edge merely because its source is disabled:** rejected because
  disabled plugin cohorts are inert and may retain internal declarations safely. Their names, owner
  identity, config, and cycles are still validated unconditionally.
- **Hide conceptual plugin guide topics:** rejected because those topics teach the exact enablement
  and replacement remediation. Authored resource and implementation anchors still follow their
  resolved row's enablement.
- **Move installer executors or introduce consumer gating:** rejected by the operator's
  declared-resource-only ruling and unnecessary for this framework work.
