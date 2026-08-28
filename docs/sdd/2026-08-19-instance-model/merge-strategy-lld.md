# Schema-Directed Merge Strategies

- Status: Proposed for SDD checkpoint review
- Date: 2026-08-27
- Requirements: R4 in [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- CLI contract: [instance-spec-cli.md](./instance-spec-cli.md)

## Purpose

Template inheritance and final instance layers already share ordering and provenance machinery, but
five reducers and an imperative capability hook still decide field behavior. This correction makes
the typed model tree the single merge-policy authority for both core and capability-owned models.

The design must satisfy four constraints together:

1. Objects merge recursively by key unless the object node says otherwise.
2. Any object node, including a nested or root model, can replace its complete prior subtree.
3. Lists retain stable append-deduplication unless their field says to replace.
4. Invalid raw input reaches the existing final validation boundary unchanged rather than being
   filtered, coerced, or accidentally combined into a different value.

The mechanism applies equally to template inheritance and the optional final instance layer. It does
not add an instance-only patch language.

## Model contract

The schema package owns one closed vocabulary. Its enum values are also the Pydantic annotation
metadata, so field and model policy use the same concept:

```python
class MergeStrategy(StrEnum):
    MERGE = "merge"
    APPEND_DEDUPE = "append-dedupe"
    REPLACE = "replace"
```

A field override is metadata on the outer field annotation:

```python
extra_args: Annotated[list[str], MergeStrategy.REPLACE]
auth: Annotated[AuthConfig, MergeStrategy.REPLACE]
```

Mapping-shaped schema models also expose a class policy for uses where no containing field exists or
where the same object policy should follow the model everywhere:

```python
class Credentials(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE
```

Policy precedence is:

1. the containing field's `MergeStrategy` metadata;
2. the selected structured model's class policy; and
3. the schema shape default.

That order lets a field deliberately override a reusable model's normal policy. Model-level policy
is limited to object models in this change; field annotations cover list behavior. Strategy metadata
is code-owned model behavior. It is not a manifest key, desired-state payload field, JSON Schema
input, or operator-configurable extension point.

### Shape defaults

| Schema node             | Default         | Effect                                                         |
| ----------------------- | --------------- | -------------------------------------------------------------- |
| Object model or mapping | `merge`         | Preserve non-conflicting keys and recurse on conflicts.        |
| List                    | `append-dedupe` | Preserve first-seen order and add only unequal incoming items. |
| Scalar                  | `replace`       | Use the later authored value.                                  |

`replace` is valid for every node and is the useful override for objects and lists. `merge` requires
an object-like node. `append-dedupe` requires a list node. Explicit metadata may restate an
inherited model policy or shape default; it still names the effective policy without another
validation rule.

A strategy annotates the node as a whole. Sequence elements do not independently receive merge
policy because append-deduplication has equality but no element key through which two authored items
could recursively conflict. Mapping keys likewise have no policy; mapping value annotations are
valid because the map key supplies the identity for a conflict at arbitrary depth:

```python
services: dict[str, Annotated[ServiceConfig, MergeStrategy.REPLACE]]
```

## Layer inputs and absence

The existing inheritance linearization and `run_layer_fold` remain responsible for layer order,
source identity, and the default seed. Each domain supplies only fields that the layer actually
contributes. The merge engine does not materialize Pydantic defaults for every layer.

Thin domain adapters preserve current authored-field rules before invoking the generic merge:

- omitted optional template fields do not contribute;
- the existing core-template `None`-as-absence behavior remains unchanged;
- explicitly supplied admin fields retain their current `model_fields_set` distinction; and
- instance overlays continue to use their validated `exclude_unset` payloads and reject JSON `null`
  before the fold.

These are layer-input rules, not field merge policies. Keeping them at the adapter prevents the
generic engine from confusing omission with an authored nullable value.

## Recursive algorithm

The merger receives the schema node, previous raw value, incoming raw value, and current path. It
returns the effective raw value plus provenance operations for this layer.

```text
merge(node, previous, incoming, path):
  if a containing field says replace:
    replace the complete node

  if node is a union:
    select previous and incoming arms without constructing defaults
    if either arm cannot be selected or the arms differ:
      replace the complete union node
    otherwise:
      continue under the selected arm

  strategy = containing field marker, else selected model policy, else shape default

  if strategy is replace:
    replace the complete node

  if runtime shape disagrees with the strategy or schema:
    replace the node without coercion
    let final typed validation report the raw incoming value

  if strategy is merge:
    if either mapping has a non-exact-string key:
      replace the complete node
    preserve previous-only keys
    add incoming-only keys
    recursively merge known conflicting keys through their child schema nodes
    replace an unknown conflicting key with the incoming raw value
    return the resulting object

  if strategy is append-dedupe:
    retain previous items in order
    append each incoming item not structurally carrier-equal to an existing result item
    record every distinct layer contributing an equal item
    return the resulting list
```

Whole-node replacement emits a prefix reset and records the incoming layer at the node. Object
descendants inherit that source through longest-prefix lookup; later child merges can add narrower
records. List positions likewise inherit from a replaced list node; an index record materializes
only when a later append or equal contribution differs from that parent attribution.

List equality uses a closed structural, type-sensitive JSON-carrier comparison rather than Python
equality or hashing. Exact `None`, `bool`, `int`, finite `float`, and `str` values receive distinct
type tags. Lists compare in order recursively. Dictionaries compare the same string keys and
recursive values without regard to key order. Subclasses are outside the carrier, so `true` does not
deduplicate `1` and no authored equality method runs.

The equality helper remains total over the deliberately open `CapabilityBlock.config` raw boundary.
YAML aliases can supply cycles before capability-model validation, and YAML can also supply
non-string keys or non-finite floats. If either candidate contains one of those values or another
node outside the closed carrier, the candidates are not equal and the incoming item is retained. An
active pair-identity guard makes cyclic comparisons terminate. Provenance identifies result
positions rather than embedding item values in paths.

### Unknown and malformed values

Registry construction and stored-state inspection need a total merge even when final validation will
fail. The walker therefore follows these rules:

- an unknown object key is retained, and a later conflict replaces its earlier raw value;
- a wrong-shaped incoming value replaces the prior node and remains wrong-shaped;
- an unknown or invalid union arm replaces the prior union node;
- an invalid list item is retained and participates in the same defensive type-sensitive equality;
  and
- `null` is never interpreted as an empty object or list.

A wholly unknown harness integration has no schema and cannot produce a usable effective config. The
later declared config therefore replaces the complete prior config without inspecting it, while
Registry miss handling remains the authoritative selector error. This keeps the total finalize walk
without inventing an untyped merge policy for a value no consumer can use.

A later valid layer may replace an earlier invalid value at the same node, exactly as it may correct
any earlier declaration. Invalid surviving data is never silently repaired by the merge itself.
Strict inline JSON and persisted JSON sources are acyclic, but YAML aliases and Python-domain
capability input need not be. The walker tracks active container identities and stops descent with
the incoming raw node on a cycle, so registry construction cannot recurse indefinitely before final
validation receives and decides the value.

## Union and subtree boundaries

Structural and discriminated unions must not combine values from different arms or arms that cannot
be selected. Their executable precedence is:

1. a `replace` marker on the containing field replaces without selecting an arm;
2. otherwise the walker selects both arms using the schema package's existing introspection;
3. different arms, or arms that cannot be selected, replace the complete value and defer any error
   to validation; and
4. equal selected arms apply a containing `merge` when present, otherwise the selected model's root
   policy and then the object default; recurse only when that effective policy merges.

A same-arm composite can therefore be requested deliberately by a containing field even when the
reusable arm model normally replaces. That is ordinary field-over-model precedence, not a cross-arm
hybrid. A containing `merge` never overrides the arm-selection safety gate.

The shared environment-table type annotates its mapping value as
`Annotated[EnvEntry, MergeStrategy.REPLACE]`. Policy therefore sits at the actual per-key conflict
position. Changing between secret and plaintext forms, or overriding one entry with another of the
same form, preserves today's whole-entry replacement without placing mapping-only policy on an arm
that also accepts scalar shorthand.

An object-level replacement is a strict subtree boundary. The previous object and every child under
it are discarded before the incoming object is recorded at the node. Child annotations inside that
incoming object do not participate at that fixed replacement node. They still govern any merging use
of the same model elsewhere; uniform registration conformance remains model-wide.

## Provenance

The existing `LayerContribution` operation vocabulary expresses the required updates: replace one
path, add a contributor, or reset a prefix. Its contribution behavior gains one precise rule: when
the exact path has no record, seed its prior sources from the longest recorded prefix before adding
the current layer. This materializes inherited attribution at a narrower path only when another
layer contributes there. The generic merger emits those operations while the shared fold continues
to own the retained provenance map.

- Recursive object merge changes only incoming leaves; untouched descendants keep their sources.
- Scalar replacement records the incoming source at the scalar path.
- Append-deduplication records provenance by resulting list index. A newly appended index records a
  replacement from the incoming layer, while an equal existing index records a contribution and
  retains all distinct earlier sources.
- Object replacement resets the node prefix and records only the incoming source at the node. Its
  descendants inherit that source by longest prefix until a later child merge adds a narrower
  record. An empty replacement still records which layer cleared the node.
- List replacement resets the node prefix and records the node. Its positions inherit that source
  until a later append or equal contribution materializes an index record.
- A union-arm change is an object replacement and follows the same prefix-reset rule.

Validation attribution changes from top-level-key lookup to longest-prefix lookup over the error
location. The implementation introduces `ProvenancePath = tuple[str | int, ...]` across the complete
provenance API: `LayeredResolution.provenance`; `LayerContribution.path` and its `replacement`,
`contribution`, and `reset_prefix` constructors; `run_layer_fold` defaults and retained map; the
generic merger's current path; every domain `effective_references` signature and ownership lookup;
and capability-validation and error attribution. Their permanent docstrings migrate with their
signatures, including removal of the current value-keyed list-path contract. All provenance reads
use one longest-prefix helper.

String segments represent fields and valid mapping keys, while integer segments represent positions
in the returned effective list. An equal incoming item emits `contribution` at its existing result
index so longest-prefix seeding retains the earlier owner; a newly appended item emits `replacement`
at its new index so it does not inherit a parent owner that never contributed that value. List
replacement resets the prefix and records only the list node; longest-prefix lookup attributes its
positions without duplicate records. No authored item value or `repr` enters a provenance path.

`validate_capability_config` and `config_error_from` accept a capability-local
`Mapping[ProvenancePath, RefOwner]`. The session boundary projects the outer layered map into that
shape: local `("name",)` resolves from outer `("harness_integration",)`, while outer
`("harness_integration_config", *path)` becomes local `path`, including the empty root path, with
the latest `LayerSource` converted to `RefOwner`. Declared-template finalize, pending creation,
database-backed live publication, and lifecycle revalidation all use that same projection through a
provenance-aware `validate_effective_harness`. The helper accepts an optional `SourceLocation` and
forwards it to capability validation: declared-template finalize supplies the row location, while
the other three paths omit it. `MergedHarness` retains neither the second provenance map nor its
dead parallel `declared_by` ownership field.

Pydantic error locations normalize to the same representation while the model-aware path walk still
has structured segments: union-arm labels and key markers add no segment, and a non-string key in a
merge-by-key mapping stops normalization at its container without printing or hashing the key. List
reference consumers resolve against the resulting index. This is necessary once separate nested
siblings can come from separate layers and keeps paths value-safe.

## Core and capability integration

VM, admin, workspace, agent, and session resolution retain thin adapters for their seed and
authored-field extraction. Their hand-written scalar, map, and list policy tables disappear. The
generic merger receives each declaration model and returns the updated accumulator plus provenance
operations.

Existing shipped behavior is preserved with model policy:

- core additive lists continue to use the list default;
- capability lists that currently replace, including argument vectors, declare `replace`;
- shell required commands and Codex writable directories continue to append-dedupe;
- the environment-table value position declares whole-entry replacement; and
- ordinary scalar and scalar-valued map conflicts continue to replace.

Session harness selection has one necessary dynamic schema step. A same-name integration obtains the
registered capability's offered config model and merges through it. A changed integration name
resets the complete config subtree before using the new model. An unknown name takes the total
untyped path: the later config replaces the complete prior config without inspection, and normal
Registry miss handling remains the authoritative error.

The `HarnessIntegration.merge_config` callback and the resolver's sentinel-based inference are
removed. No callable escape hatch remains for cross-field transforms; a capability represents merge
behavior in its declared config model or changes its model shape so the desired behavior is local.

This removal is a public harness-integration contract break. The kind descriptor increments its
exact contract version from 1 to 2 in the same change. Every shipped integration migrates before the
descriptor moves; a third-party class still declaring version 1 fails registration with the existing
version-mismatch diagnostic. There is no version-1 adapter because invoking the old callback would
restore a second merge-policy authority and silently diverge from model annotations.

## Registration validation

One pure recursive `merge_contract_error(model)` conformance function owns metadata validation and
uses a model cycle guard. A capability kind whose config actually participates in layered merging
opts into that contract through its core-owned `ConfigContract`. Capability plugin registration then
checks that kind's registered offered config model and rejects an invalid third-party contract.
Kinds without a layered config surface retain their existing conformance contract; registration does
not impose merge constraints that their config never exercises. First-party core declaration models
and opted-in capability models are covered by exhaustive, registry-derived conformance tests rather
than a new import-time core rejection path. The pass rejects:

- more than one `MergeStrategy` metadata value at a node;
- `merge` on a non-object node;
- `append-dedupe` on a non-list node;
- a model-level strategy on anything except a mapping-shaped model;
- strategy metadata on an individual sequence element or mapping key.

Every list whose effective policy is `append-dedupe` must have an element annotation recursively
confined to that closed JSON carrier: exact JSON scalar types and literals, lists, string-keyed
mappings, unions of allowed arms, and mapping-shaped models composed only from those forms. A float
annotation qualifies only when it carries Pydantic's recognized `AllowInfNan(False)` constraint,
such as `FiniteFloat`; custom validators and model configuration do not establish merge conformance.
`Any`, `object`, an unconstrained float, non-string mapping keys, sets, tuples, Python-specific
scalar types, and opaque custom types fail conformance with guidance to mark that list `replace`.
This is deliberately narrower than the harness-integration kind's general Pydantic/Python input
domain. Static conformance is defined by the annotation-declared structural domain. Model and before
validators are not consulted and cannot make an otherwise nonconforming annotation conform. At
runtime they likewise do not enlarge the closed structural carrier: a validator-admitted raw value
outside that carrier remains unsupported-as-unequal and reaches final validation, even if validation
later accepts it. A validator-admitted value already inside the carrier remains comparable by its
raw concrete type. Final validation does not retroactively repeat deduplication.

Every mapping whose effective policy is `merge` likewise requires an exact-string key annotation.
Any other key schema fails conformance with guidance to mark the complete mapping `replace`. At
runtime a non-exact-string raw key against a merge-by-key schema makes the node wrong-shaped, so the
incoming mapping replaces the node without copying, hashing, comparing, or recording the key. This
keeps ordinary mapping merge within its provenance and conflict-identity contract while allowing a
Python-domain mapping with arbitrary keys behind a whole-node replacement boundary.

The v2 merge contract uniformly refuses validation aliases in every participating model. That
includes string and generated aliases, alias choices, and alias paths, even below a whole-node
`replace` boundary where copying could technically remain safe. The broader refusal keeps one
authored field name throughout the public plugin contract and avoids context-dependent registration.
Serialization-only aliases do not affect raw validation-key lookup and remain allowed.

Mapping value annotations and every model recursively reachable through them are traversed for
strategy placement and validation-alias conformance. A validation alias on a nested mapping-value
model is refused even when the containing mapping or an ancestor uses whole-node replacement. The
dynamic session merger reads the registered integration's offered config model through the same
central accessor used by validation and reference extraction. The harness-integration
`ConfigContract` opts into layered merging, so registration validates that exact offered model.
Capability projection metadata is not a merge contract and receives no speculative preservation
requirement.

## Compatibility and persistence

This correction needs no database migration and no desired-overlay payload-version change. Stored
overlays are authored declarations and continue to resolve under the current registered model, just
as template declarations do. The migration annotations preserve every existing shipped field's
behavior; the new recursive default becomes observable only for nested objects that previously had
no model-declared policy.

The absence of a data migration does not make the plugin API backward compatible. The
harness-integration contract version moves to 2 because version 1 promised `merge_config`; old
third-party implementations are refused at registration until their merge behavior is expressed on
their config model. Version 2 also requires participating models to use their field names for
validation, including below replacement boundaries; serialization-only aliases remain available. The
upgrade guidance names that constraint explicitly.

An empty unmarked map or list remains additive and changes nothing. Empty replacement values clear
only fields whose model explicitly chose replacement. There is still no key-removal tombstone,
JSON-path operation, or standalone instance-spec mutation surface.

## Verification

The implementation must prove:

- object recursion at more than one depth, non-conflicting retention, and scalar child replacement;
- nested and root object replacement, including empty-object clearing and discarded-child
  provenance;
- default append-deduplication, explicit list replacement, empty-list clearing, stable order, and
  duplicate contributors;
- containing replacement without arm selection, same-arm containing merge over model replacement,
  same-arm model replacement without an override, different-arm or selection-failure replacement
  despite containing merge, and same- and cross-arm whole-entry environment parity;
- total handling of unknown keys, unknown arms, unknown integrations, `null`, wrong shapes, and
  invalid list items until final validation, including later raw replacement and cycle refusal;
- type-sensitive list equality, including `true` versus `1`, `1` versus `1.0`, nested objects,
  object key order, non-finite-float defense, an unsupported Python object whose equality raises,
  and a cyclic list item;
- no input mutation and deterministic output;
- registration refusal for every invalid metadata placement or strategy, including uniform
  validation-alias refusal, non-string keys on merged mappings, and append-dedupe element types
  outside the comparable carrier, with exhaustive first-party model coverage and third-party
  registration coverage;
- validator-admitted values outside the carrier remaining unequal until final validation,
  carrier-resident values outside their declared annotation retaining concrete-type equality, and
  validation-alias refusal through nested mapping values and below replacement boundaries;
- acceptance of arbitrary mapping keys below a replacement boundary and malformed non-string raw
  keys replacing at the parent without merge-time interpretation;
- core template-to-template and template-to-instance parity across every owning kind;
- capability same-integration recursion, list policies, selector-change reset, and error
  attribution, plus a version-1 registration failure and migrated version-2 shipped integrations;
- string and integer validation-location normalization, wrong-shaped non-string map replacement,
  result-index list references, parent fallback for a malformed map key, and inherited parent
  provenance retained when a later equal list item contributes at a newly materialized position
  path, a newly appended item refusing unrelated parent provenance, and declared-template source
  framing through the consolidated validation helper, with no value- or `repr`-based path or
  exact-only ownership lookup remaining; and
- no database schema, desired-payload, persistence, or simple-case CLI change.

Mutation testing covers exactly four safety mutations and shows that the focused suite fails for
each: neutralize object replacement, list replacement, union-arm reset, and longest-prefix
provenance attribution.

## Permanent collateral shipped with implementation

The implementation PR updates the schema README, capability authoring README, harness-integration
README, ADR 0020, ADR 0023, and the active upgrade guidance in the same commit range that removes
the imperative hook and increments the contract version. The schema README describes shipped atomic
list-item behavior; ADR 0023 preserves the approved unsupported future identity direction. No
permanent artifact will depend on this SDD path.

## Future extension: model identity for list items

This correction keeps list items atomic and model-declared item identity unsupported.
[ADR 0023](../../adrs/0023-declared-schemas-and-the-kind-descriptor.md) is the permanent record for
the approved future direction; the implementation updates it before this ephemeral SDD can be
deleted.

## Out of scope

- Operator-authored merge strategies in YAML or inline JSON.
- Key or list-item deletion tombstones beyond replacing a complete annotated node.
- Custom capability merge callbacks or a general transform language.
- List-item identity merging described above, sorting, or set semantics beyond stable equality
  deduplication.
- The separate idea that a template may require an instance layer to supply a field.
- R3 applied-state capture and R5 resolved-spec presentation.
